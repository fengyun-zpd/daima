"""阶段七测试：审批门、任务分支写入、审计与非法合并（FR-041~FR-043、FR-070~FR-074）。

覆盖：
- 只有 approver 能审批；developer/admin 审批被拒；
- 审批决定绑定 patch_version：旧版本审批新补丁 → VERSION_CONFLICT；
- 无审批记录禁止合并；拒绝必须填写原因；拒绝后任务进入 REJECTED；
- 合并只写任务分支，`main/develop` 一律 FORBIDDEN；
- 重复合并被幂等保护；
- 审批记录只追加（ORM 与数据库双重拦截）。
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from agents.coordinator.coordinator import Coordinator
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import ParentTaskStatus, PatchStatus
from domain.errors import CodePilotError, ErrorCode
from repositories.content_store import ContentStore
from repositories.models import Approval
from repositories.records import ApprovalStore, PatchStore
from repositories.repo_store import SyntheticRepo, assert_writable_branch
from repositories.store import ReviewTaskStore

CONFIG = CodePilotConfig.model_validate({"mode": "a2a", "a2a": {"child_task_timeout_seconds": 45}})

APP_PY = '''"""Config module."""
import os
import subprocess

API_KEY = "sk-live-1234567890"


def run(cmd, directory):
    return subprocess.run("ls " + directory, shell=True)
'''

TEST_PY = '''import os

os.environ.setdefault("API_KEY", "test-key")

from app import config


def test_run_returns_completed():
    assert config.run("ls", "/tmp") is not None
'''
PROJECT = {"app/__init__.py": "", "app/config.py": APP_PY, "tests/test_config.py": TEST_PY}

PATCH = (
    "--- a/app/config.py\n"
    "+++ b/app/config.py\n"
    "@@ -2,4 +2,4 @@\n"
    " import os\n"
    " import subprocess\n"
    " \n"
    '-API_KEY = "sk-live-1234567890"\n'
    '+API_KEY = os.environ["API_KEY"]\n'
)

APPROVER = {"X-Actor-Id": "approver-1", "X-Actor-Role": "approver"}
DEVELOPER = {"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"}
ADMIN = {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}


def build_zip(project: dict[str, str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in project.items():
            archive.writestr(path, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture()
def review_fixture(tmp_path, concurrent_database):
    """准备一个已进入 PENDING_APPROVAL 的任务与候选补丁（沙箱用桩，避免依赖 Docker）。"""
    from agents.verify.agent import VerifyAgent  # noqa: F401  (确保 handler 已注册)
    from sandbox.base import SandboxResult
    from tests.test_phase6_fix_verify import _StubSandbox

    good = (
        "CODEPILOT_APPLY_CHECK_OK\nCODEPILOT_APPLY_OK\nCODEPILOT_LINT_OK\n"
        "TOTAL 12 2 83%\n2 passed in 0.05s\n"
    )
    stub = _StubSandbox([SandboxResult(status="passed", exit_code=0, stdout=good)])

    store = ContentStore(tmp_path / "var")
    container = build_container(url=concurrent_database, auto_create=True, config=CONFIG)
    container.content_store = store
    container.coordinator.content_store = store
    container.coordinator.sandbox = stub
    container.coordinator.repo_root = tmp_path / "var"
    app = create_app(
        container=container, recover_on_start=False, schedule_on_create=False, include_internal=False
    )
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(PROJECT),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={**DEVELOPER, "Idempotency-Key": "p7-idem-000001"},
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task"]["id"]
        container.coordinator.run(task_id)
        container.coordinator.run(task_id, auto_fix=True)
        with container.session() as session:
            task = ReviewTaskStore(session).get(task_id)
            patch = PatchStore(session).latest(task_id)
        assert task.status == str(ParentTaskStatus.PENDING_APPROVAL), task.status
        assert patch is not None
        yield client, container, task_id, patch


# ---------------------------------------------------------------------------
# 审批门
# ---------------------------------------------------------------------------


def test_only_approver_can_decide(review_fixture) -> None:
    client, _container, _task_id, patch = review_fixture
    response = client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version, "reason": "ok"},
        headers={**DEVELOPER, "Idempotency-Key": "p7-approve-0001"},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] in {"FORBIDDEN", "PERMISSION_DENIED"}


def test_approval_binds_patch_version(review_fixture) -> None:
    client, container, _task_id, patch = review_fixture
    response = client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version + 1, "reason": "旧版本"},
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0002"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "VERSION_CONFLICT"
    with container.session() as session:
        approvals = ApprovalStore(session).list_by_task(patch.task_id)
    assert approvals == []


def test_reject_requires_reason_and_stops_flow(review_fixture) -> None:
    client, container, task_id, patch = review_fixture
    empty = client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "reject", "patch_version": patch.patch_version, "reason": "   "},
        headers={**APPROVER, "Idempotency-Key": "p7-reject-0001"},
    )
    assert empty.status_code == 400
    assert empty.json()["code"] == "INVALID_INPUT"

    rejected = client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "reject", "patch_version": patch.patch_version, "reason": "影响范围超出预期"},
        headers={**APPROVER, "Idempotency-Key": "p7-reject-0002"},
    )
    assert rejected.status_code == 200
    with container.session() as session:
        task = ReviewTaskStore(session).get(task_id)
        stored = PatchStore(session).get(patch.id)
    assert task.status == str(ParentTaskStatus.REJECTED)
    assert stored.status != str(PatchStatus.APPLIED)


def test_approval_records_are_append_only(review_fixture) -> None:
    client, container, _task_id, patch = review_fixture
    client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version, "reason": "证据充分"},
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0003"},
    )
    with container.session() as session:
        rows = session.query(Approval).all()
    assert len(rows) == 1
    row = rows[0]
    row.reason = "tampered"
    from repositories.audit import AuditImmutabilityError

    with pytest.raises(AuditImmutabilityError), container.session() as session:
        session.add(row)
        session.flush()


def test_approval_is_idempotent_for_same_decision(review_fixture) -> None:
    client, _container, _task_id, patch = review_fixture
    payload = {"decision": "approve", "patch_version": patch.patch_version, "reason": "ok"}
    first = client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json=payload,
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0004"},
    )
    second = client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json=payload,
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0005"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["approval_id"] == second.json()["approval_id"]
    assert second.json()["created"] is False


# ---------------------------------------------------------------------------
# 合并与任务分支
# ---------------------------------------------------------------------------


def test_merge_without_approval_is_forbidden(review_fixture) -> None:
    client, container, task_id, patch = review_fixture
    response = client.post(
        f"/api/v1/fixes/{patch.id}/merge",
        json={"patch_version": patch.patch_version},
        headers={**APPROVER, "Idempotency-Key": "p7-merge-0001"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    with container.session() as session:
        task = ReviewTaskStore(session).get(task_id)
        stored = PatchStore(session).get(patch.id)
    assert task.status == str(ParentTaskStatus.PENDING_APPROVAL)
    assert stored.status != str(PatchStatus.APPLIED)


def test_database_rejects_merge_without_approval_record(review_fixture) -> None:
    """绕过 API 直接调用 Coordinator 也必须被拒绝（FR-070 的纵深防御）。"""
    client, container, _task_id, patch = review_fixture
    with pytest.raises(CodePilotError) as excinfo:
        container.coordinator.merge(
            patch_id=patch.id,
            patch_version=patch.patch_version,
            actor_id="approver-1",
            actor_role="approver",
        )
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_merge_applies_to_task_branch(review_fixture, tmp_path) -> None:
    client, container, task_id, patch = review_fixture
    client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version, "reason": "测试证据充分"},
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0006"},
    )
    merged = client.post(
        f"/api/v1/fixes/{patch.id}/merge",
        json={"patch_version": patch.patch_version},
        headers={**APPROVER, "Idempotency-Key": "p7-merge-0002"},
    )
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["branch"] == f"codepilot/{task_id}"
    assert body["task_status"] == str(ParentTaskStatus.MERGED)
    assert body["commit"]

    with container.session() as session:
        task = ReviewTaskStore(session).get(task_id)
        stored = PatchStore(session).get(patch.id)
    assert task.status == str(ParentTaskStatus.MERGED)
    assert stored.status == str(PatchStatus.APPLIED)

    # 补丁确实写入了任务分支，且受保护分支未被触碰
    repo = SyntheticRepo(tmp_path / "var", task_id)
    assert repo.current_branch() == f"codepilot/{task_id}"
    content = (repo.path / "app" / "config.py").read_text(encoding="utf-8")
    assert 'os.environ["API_KEY"]' in content
    assert "sk-live-1234567890" not in content


def test_merge_twice_is_rejected(review_fixture) -> None:
    client, _container, _task_id, patch = review_fixture
    client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version, "reason": "ok"},
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0007"},
    )
    first = client.post(
        f"/api/v1/fixes/{patch.id}/merge",
        json={"patch_version": patch.patch_version},
        headers={**APPROVER, "Idempotency-Key": "p7-merge-0003"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/fixes/{patch.id}/merge",
        json={"patch_version": patch.patch_version},
        headers={**APPROVER, "Idempotency-Key": "p7-merge-0004"},
    )
    assert second.status_code in {403, 409}
    assert second.json()["code"] in {"FORBIDDEN", "ILLEGAL_STATE_TRANSITION", "CONFLICT"}


def test_developer_cannot_merge(review_fixture) -> None:
    client, _container, _task_id, patch = review_fixture
    client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version, "reason": "ok"},
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0008"},
    )
    response = client.post(
        f"/api/v1/fixes/{patch.id}/merge",
        json={"patch_version": patch.patch_version},
        headers={**DEVELOPER, "Idempotency-Key": "p7-merge-0005"},
    )
    assert response.status_code == 403


def test_audit_trail_covers_approval_and_merge(review_fixture) -> None:
    client, _container, task_id, patch = review_fixture
    client.post(
        f"/api/v1/fixes/{patch.id}/approval",
        json={"decision": "approve", "patch_version": patch.patch_version, "reason": "ok"},
        headers={**APPROVER, "Idempotency-Key": "p7-approve-0009"},
    )
    client.post(
        f"/api/v1/fixes/{patch.id}/merge",
        json={"patch_version": patch.patch_version},
        headers={**APPROVER, "Idempotency-Key": "p7-merge-0006"},
    )
    audit = client.get("/api/v1/audit", headers=ADMIN, params={"task_id": task_id}).json()
    event_types = {item["event_type"] for item in audit}
    assert "approval_recorded" in event_types
    assert "task_merged" in event_types
    decisions = [
        event
        for event in audit
        if event["event_type"] == "approval_recorded"
    ]
    assert decisions and decisions[0]["after_state"]["decision"] == "approve"


# ---------------------------------------------------------------------------
# 分支白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("branch", ["main", "master", "develop", "Main"])
def test_protected_branches_are_forbidden(branch: str) -> None:
    with pytest.raises(CodePilotError) as excinfo:
        assert_writable_branch(branch, task_id="task-1")
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_other_task_branch_is_forbidden() -> None:
    with pytest.raises(CodePilotError) as excinfo:
        assert_writable_branch("codepilot/other-task", task_id="task-1")
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_task_branch_is_allowed() -> None:
    assert assert_writable_branch("codepilot/task-1", task_id="task-1") == "codepilot/task-1"


def test_synthetic_repo_rolls_back_on_invalid_patch(tmp_path) -> None:
    repo = SyntheticRepo(tmp_path, "task-x")
    repo.ensure(PROJECT, base_commit="synthetic-base-001")
    head_before = repo.head_commit()
    broken = "--- a/app/config.py\n+++ b/app/config.py\n@@ -99,1 +99,1 @@\n-x\n+y\n"
    with pytest.raises(CodePilotError) as excinfo:
        repo.apply_patch(broken)
    assert excinfo.value.code is ErrorCode.PATCH_INVALID
    assert repo.head_commit() == head_before, "失败补丁不得改变任务分支"


def test_synthetic_repo_apply_patch_changes_files(tmp_path) -> None:
    repo = SyntheticRepo(tmp_path, "task-y")
    repo.ensure(PROJECT, base_commit="synthetic-base-001")
    result = repo.apply_patch(PATCH)
    assert result.changed_files == ["app/config.py"]
    assert result.checksums["app/config.py"].startswith("sha256:")
    assert 'os.environ["API_KEY"]' in (repo.path / "app" / "config.py").read_text(encoding="utf-8")


def test_write_tool_requires_approval_context(tmp_path) -> None:
    """写工具在缺少审批上下文时必须拒绝（纵深防御）。"""
    from tools.registry import ToolCallContext
    from tools.write import WritePatchParams, write_patch

    class _Ctx:
        parent_task_id = "task-1"
        task_id = "task-1"
        extra: dict = {}
        workspace = type("W", (), {"files": PROJECT})()

    with pytest.raises(CodePilotError) as excinfo:
        write_patch(_Ctx(), WritePatchParams(
            patch_id="patch-00000000001",
            patch_version=1,
            patch_hash="sha256:" + "a" * 64,
            diff=PATCH,
            target_branch="codepilot/task-1",
        ))
    assert excinfo.value.code is ErrorCode.FORBIDDEN
    assert isinstance(ToolCallContext, type)


def test_coordinator_write_tool_is_registered_as_write() -> None:
    from domain.enums import ToolAccess
    from tools import build_default_registry

    registry = build_default_registry()
    assert registry.get("write_patch").access is ToolAccess.WRITE
    assert registry.get("run_tests").access is ToolAccess.WRITE
    assert isinstance(Coordinator, type)
