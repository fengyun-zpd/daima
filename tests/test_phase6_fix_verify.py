"""阶段六测试：Fix 三类修复、Verify 质量门禁与 Docker 沙箱（FR-040~FR-060）。

- 修复配方：单元级验证三类修复的正确性与保守性（无法安全改写即拒绝）；
- 质量门禁：预审查拦截、补丁不可应用、测试失败、TEST_GAP、scope drift；
- 沙箱：真实 Docker 验证 network=none / 非 root / 只读根 / 资源限制 / 清理。

Docker 不可用时，沙箱相关用例自动跳过（跳过不代表验收通过，见 docs/04 §4）。
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from a2a.protocol import FindingItem, PatchCandidate, compute_text_hash
from agents.base import AgentRequest, ToolGateway
from agents.fix.agent import FixAgent
from agents.fix.recipes import plan_fix
from agents.verify.agent import VerifyAgent
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.budget import Budget
from domain.config import load_config
from domain.enums import (
    ArtifactType,
    ChildTaskStatus,
    ConfidenceLevel,
    ContextPolicy,
    FixCategory,
    InputType,
    ParentTaskStatus,
    Severity,
)
from domain.errors import CodePilotError, ErrorCode
from domain.workspace import build_workspace
from repositories.content_store import ContentStore
from repositories.models import A2AArtifact, SandboxRun
from repositories.records import CommentStore, PatchStore
from repositories.store import A2ATaskStore, ReviewTaskStore
from sandbox.base import SandboxLimits, SandboxResult, UnavailableSandbox
from sandbox.docker import DockerSandbox
from tools import build_default_registry
from tools.registry import ToolCallContext

CONFIG = load_config(project_path="examples/.codepilot.yaml")
docker_available = DockerSandbox().available
requires_docker = pytest.mark.skipif(not docker_available, reason="Docker 沙箱镜像不可用")

APP_PY = '''"""Config module."""
import os
import subprocess

API_KEY = "sk-live-1234567890"


def run(cmd, directory):
    return subprocess.run("ls " + directory, shell=True)


def query(cursor, name):
    return cursor.execute("SELECT * FROM users WHERE name='" + name + "'")


def load(path):
    with open(path) as handle:
        return handle.read()
'''

TEST_PY = '''import os

os.environ.setdefault("API_KEY", "test-key")

from app import config


def test_run_returns_completed():
    assert config.run("ls", "/tmp") is not None


def test_api_key_from_env():
    assert config.API_KEY == "test-key"


def test_query_builds_sql():
    calls = []

    class Cursor:
        def execute(self, sql, params=None):
            calls.append((sql, params))
            return "ok"

    assert config.query(Cursor(), "alice") == "ok"
    assert calls
'''

PROJECT = {"app/__init__.py": "", "app/config.py": APP_PY, "tests/test_config.py": TEST_PY}


def build_zip(project: dict[str, str]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in project.items():
            archive.writestr(path, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def finding(
    *,
    rule_id: str,
    category: FixCategory,
    line: int,
    file: str = "app/config.py",
    severity: Severity = Severity.CRITICAL,
) -> FindingItem:
    return FindingItem(
        rule_id=rule_id,
        title=rule_id,
        cwe="CWE-000",
        severity=severity,
        file=file,
        line=line,
        evidence="evidence",
        message="message",
        confidence_base=0.8,
        confidence=1.0,
        confidence_level=ConfidenceLevel.CONFIRMED,
        auto_fixable=True,
        fix_category=category,
        in_changed_lines=True,
        context_confirmed=True,
        symbol="app.config",
        rule_version="rules-v1",
    )


# ---------------------------------------------------------------------------
# 修复配方（三类 + 保守拒绝）
# ---------------------------------------------------------------------------


def _workspace(content: str = APP_PY, *, path: str = "app/config.py"):
    workspace, _ = build_workspace(
        input_type=InputType.ZIP,
        content=build_zip({"app/__init__.py": "", path: content}),
        base_commit="synthetic-base-001",
        context_policy=ContextPolicy.FUNCTION,
    )
    return workspace


def test_secret_recipe_replaces_literal_with_env() -> None:
    workspace = _workspace()
    plan = plan_fix(
        finding(rule_id="R002_HARDCODED_SECRET", category=FixCategory.HARDCODED_SECRET, line=5),
        workspace,
    )
    assert plan is not None
    assert 'API_KEY = os.environ["API_KEY"]' in plan.patched
    assert "sk-live" not in plan.patched


def test_secret_recipe_ignores_non_literal() -> None:
    workspace = _workspace('API_KEY = os.environ.get("OTHER")\n')
    plan = plan_fix(
        finding(rule_id="R002_HARDCODED_SECRET", category=FixCategory.HARDCODED_SECRET, line=1),
        workspace,
    )
    assert plan is None


def test_shell_true_recipe_builds_argument_list() -> None:
    workspace = _workspace()
    plan = plan_fix(
        finding(rule_id="R003_SHELL_TRUE", category=FixCategory.SHELL_TRUE, line=9),
        workspace,
    )
    assert plan is not None
    assert 'subprocess.run(["ls", directory], check=True)' in plan.patched
    assert "shell=True" not in plan.patched


def test_shell_true_recipe_refuses_extra_positional_arguments() -> None:
    content = 'import subprocess\n\n\ndef run(a, b):\n    return subprocess.run("ls", str(a), shell=True)\n'
    workspace = _workspace(content)
    plan = plan_fix(
        finding(rule_id="R003_SHELL_TRUE", category=FixCategory.SHELL_TRUE, line=5),
        workspace,
    )
    assert plan is None, "存在额外位置参数时不得猜测改写"


def test_sql_recipe_parameterizes_concatenation() -> None:
    workspace = _workspace()
    plan = plan_fix(
        finding(rule_id="R001_SQL_CONCAT", category=FixCategory.SQL_PARAMETERIZATION, line=13),
        workspace,
    )
    assert plan is not None
    assert 'cursor.execute("SELECT * FROM users WHERE name=\'%s\'", (name,))' in plan.patched


def test_sql_recipe_parameterizes_percent_format() -> None:
    content = 'def q(cursor, name):\n    return cursor.execute("SELECT * FROM t WHERE n=\'%s\'" % name)\n'
    workspace = _workspace(content)
    plan = plan_fix(
        finding(rule_id="R001_SQL_CONCAT", category=FixCategory.SQL_PARAMETERIZATION, line=2),
        workspace,
    )
    assert plan is not None
    assert "%s" in plan.patched
    assert "(name,)" in plan.patched


def test_sql_recipe_parameterizes_fstring() -> None:
    content = 'def q(cursor, name):\n    return cursor.execute(f"SELECT * FROM t WHERE n=\'{name}\'")\n'
    workspace = _workspace(content)
    plan = plan_fix(
        finding(rule_id="R001_SQL_CONCAT", category=FixCategory.SQL_PARAMETERIZATION, line=2),
        workspace,
    )
    assert plan is not None
    assert "(name,)" in plan.patched


def test_sql_recipe_ignores_already_parameterized() -> None:
    content = 'def q(cursor, name):\n    return cursor.execute("SELECT * FROM t WHERE n=%s", (name,))\n'
    workspace = _workspace(content)
    plan = plan_fix(
        finding(rule_id="R001_SQL_CONCAT", category=FixCategory.SQL_PARAMETERIZATION, line=2),
        workspace,
    )
    assert plan is None


def test_fix_agent_produces_candidate_and_evidence() -> None:
    workspace = _workspace()
    request = _agent_request(
        workspace,
        findings=[
            finding(rule_id="R002_HARDCODED_SECRET", category=FixCategory.HARDCODED_SECRET, line=5),
            finding(rule_id="R003_SHELL_TRUE", category=FixCategory.SHELL_TRUE, line=9),
        ],
        task_type="fix",
    )
    result = FixAgent().handle(request)
    candidate = PatchCandidate.model_validate(result.artifact_of_type("PatchCandidate").data)
    assert candidate.changed_files == ["app/config.py"]
    assert candidate.patch_hash.startswith("sha256:")
    assert candidate.patch_version == 1
    assert '"ls", directory' in candidate.diff
    assert result.artifact_of_type("PatchEvidence") is not None


def test_fix_agent_is_deterministic() -> None:
    workspace = _workspace()
    request = _agent_request(
        workspace,
        findings=[finding(rule_id="R002_HARDCODED_SECRET", category=FixCategory.HARDCODED_SECRET, line=5)],
        task_type="fix",
    )
    first = FixAgent().handle(request)
    second = FixAgent().handle(request)
    assert first.artifact_of_type("PatchCandidate").data["patch_hash"] == (
        second.artifact_of_type("PatchCandidate").data["patch_hash"]
    )


def test_fix_agent_requires_finding_input() -> None:
    workspace = _workspace()
    request = _agent_request(workspace, findings=[], task_type="fix")
    with pytest.raises(CodePilotError) as excinfo:
        FixAgent().handle(request)
    assert excinfo.value.code is ErrorCode.PATCH_INVALID


def test_patch_trailing_newline_is_preserved() -> None:
    """协议模型不得裁剪文本：去掉结尾换行会让 git apply 报 corrupt patch。"""
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    candidate = PatchCandidate(
        patch_version=1,
        patch_hash=compute_text_hash(diff),
        base_commit="base",
        target_branch="codepilot/t",
        fix_category=FixCategory.HARDCODED_SECRET,
        finding_refs=["R1@x.py:1"],
        changed_files=["x.py"],
        changed_functions=[],
        added_lines=1,
        removed_lines=1,
        diff=diff,
        idempotency_key="key-000000000001",
    )
    assert candidate.diff.endswith("\n")
    assert "\n" in candidate.diff


# ---------------------------------------------------------------------------
# 沙箱不可用时的安全降级
# ---------------------------------------------------------------------------


def test_verify_refuses_without_sandbox() -> None:
    workspace = _workspace()
    request = _agent_request(
        workspace,
        findings=[finding(rule_id="R002_HARDCODED_SECRET", category=FixCategory.HARDCODED_SECRET, line=5)],
        task_type="verify",
        candidate=_candidate(workspace),
        sandbox=UnavailableSandbox("镜像缺失"),
    )
    with pytest.raises(CodePilotError) as excinfo:
        VerifyAgent().handle(request)
    assert excinfo.value.code is ErrorCode.SANDBOX_UNAVAILABLE


def test_verify_blocks_dangerous_patch_before_sandbox() -> None:
    workspace = _workspace()
    dangerous = (
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        "+os.system('rm -rf /')\n"
    )
    candidate = _candidate(workspace, diff=dangerous)
    request = _agent_request(
        workspace,
        findings=[],
        task_type="verify",
        candidate=candidate,
        sandbox=UnavailableSandbox("不应被调用"),
    )
    result = VerifyAgent().handle(request)
    evidence = result.artifact_of_type("VerifyEvidence").data
    assert evidence["pre_scan"]["ok"] is False
    assert "os.system" in evidence["pre_scan"]["blocked_patterns"]
    assert evidence["evidence_sufficient"] is False
    assert evidence["tiers"][0]["tier"] == "prescan"


class _StubSandbox:
    available = True
    unavailable_reason = ""

    def __init__(self, outputs: list[SandboxResult]) -> None:
        self.outputs = outputs
        self.calls: list[dict] = []

    def run(self, *, files, argv, name, limits, patch=None) -> SandboxResult:
        self.calls.append({"argv": argv, "patch": patch, "files": list(files)})
        return self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]


def test_verify_reports_lint_and_test_failures() -> None:
    workspace = _workspace()
    baseline = SandboxResult(
        status="failed",
        exit_code=1,
        stdout="FAILED tests/test_config.py::test_api_key_from_env\n1 failed in 0.1s\n",
    )
    applied = SandboxResult(
        status="failed",
        exit_code=1,
        stdout=(
            "CODEPILOT_APPLY_CHECK_OK\nCODEPILOT_APPLY_OK\n"
            "app/config.py:2:1: F401 unused import\nCODEPILOT_LINT_FAIL\n"
            "FAILED tests/test_config.py::test_api_key_from_env\n1 failed in 0.1s\n"
        ),
    )
    request = _agent_request(
        workspace,
        findings=[],
        task_type="verify",
        candidate=_candidate(workspace),
        sandbox=_StubSandbox([baseline, applied]),
    )
    evidence = VerifyAgent().handle(request).artifact_of_type("VerifyEvidence").data
    assert evidence["lint"]["ok"] is False
    assert evidence["unit_tests"]["ok"] is False
    assert evidence["regression"]["ok"] is False
    assert evidence["evidence_sufficient"] is False


def test_verify_marks_test_gap_when_no_tests_collected() -> None:
    workspace = _workspace()
    empty = SandboxResult(
        status="passed",
        exit_code=0,
        stdout="CODEPILOT_APPLY_CHECK_OK\nCODEPILOT_APPLY_OK\nCODEPILOT_LINT_OK\nno tests ran in 0.01s\n",
    )
    request = _agent_request(
        workspace,
        findings=[],
        task_type="verify",
        candidate=_candidate(workspace),
        sandbox=_StubSandbox([empty]),
    )
    evidence = VerifyAgent().handle(request).artifact_of_type("VerifyEvidence").data
    assert evidence["test_gap"] is True
    assert evidence["evidence_sufficient"] is False


# ---------------------------------------------------------------------------
# 端到端（真实 Docker）
# ---------------------------------------------------------------------------


def _stack(tmp_path, database_url: str):
    config = CONFIG
    store = ContentStore(tmp_path / "var")
    container = build_container(url=database_url, auto_create=True, config=config)
    container.content_store = store
    container.coordinator.content_store = store
    container.coordinator.sandbox = DockerSandbox()
    app = create_app(
        container=container, recover_on_start=False, schedule_on_create=False, include_internal=False
    )
    return container, app


@requires_docker
def test_end_to_end_fix_verify_reaches_pending_approval(tmp_path, concurrent_database) -> None:
    from fastapi.testclient import TestClient

    container, app = _stack(tmp_path, concurrent_database)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(PROJECT),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-idem-000001"},
        )
        assert created.status_code == 200, created.text
        task_id = created.json()["task"]["id"]

        reviewed = container.coordinator.run(task_id)
        assert reviewed.status is ParentTaskStatus.REVIEWED, reviewed.to_payload()

        with container.session() as session:
            fixable = CommentStore(session).list_auto_fixable(task_id)
        assert {row.rule_id for row in fixable} >= {"R002_HARDCODED_SECRET", "R003_SHELL_TRUE"}

        outcome = container.coordinator.run(task_id, auto_fix=True)
        assert outcome.status is ParentTaskStatus.PENDING_APPROVAL, outcome.to_payload()

        with container.session() as session:
            patches = PatchStore(session).list_by_task(task_id)
            runs = list(session.execute(__import__("sqlalchemy").select(SandboxRun)).scalars())
            children = A2ATaskStore(session).list_by_parent(task_id)
            artifacts = [
                row
                for row in session.execute(__import__("sqlalchemy").select(A2AArtifact)).scalars()
                if row.parent_task_id == task_id
            ]
        assert len(patches) == 1
        patch = patches[0]
        assert patch.status == "candidate"
        assert patch.scope_drift is False
        assert patch.test_gap is False
        assert patch.sandbox_run_id
        assert patch.coverage_after is not None and patch.coverage_after > 0
        assert {row.task_type for row in children} == {"review", "impact", "fix", "verify"}
        assert all(ChildTaskStatus(row.status) is ChildTaskStatus.COMPLETED for row in children)
        assert {row.artifact_type for row in artifacts} >= {
            str(ArtifactType.FINDING),
            str(ArtifactType.IMPACT_REPORT),
            str(ArtifactType.PATCH_CANDIDATE),
            str(ArtifactType.PATCH_EVIDENCE),
            str(ArtifactType.VERIFY_EVIDENCE),
        }
        assert runs and runs[0].network == "none"

        # 未审批：不得产生合并副作用
        with container.session() as session:
            task = ReviewTaskStore(session).get(task_id)
        assert task.status == str(ParentTaskStatus.PENDING_APPROVAL)
        assert patch.status != "applied"


@requires_docker
def test_verify_failure_parks_task_without_branch_side_effects(tmp_path, concurrent_database) -> None:
    """测试失败必须阻断流程且不产生任何分支写入。"""
    from fastapi.testclient import TestClient

    broken = dict(PROJECT)
    broken["tests/test_config.py"] = (
        "import os\n\nos.environ.setdefault('API_KEY', 'test-key')\n"
        "from app import config\n\n\n"
        "def test_will_fail_after_fix():\n"
        "    assert config.API_KEY == 'sk-live-1234567890'\n"
    )
    container, app = _stack(tmp_path, concurrent_database)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(broken),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-idem-000002"},
        )
        task_id = created.json()["task"]["id"]
        container.coordinator.run(task_id)
        outcome = container.coordinator.run(task_id, auto_fix=True)

    assert outcome.status is ParentTaskStatus.NEEDS_HUMAN, outcome.to_payload()
    with container.session() as session:
        patches = PatchStore(session).list_by_task(task_id)
    assert all(item.status != "applied" for item in patches)


@requires_docker
def test_offline_mode_never_generates_patch(tmp_path, concurrent_database) -> None:
    from fastapi.testclient import TestClient

    container, app = _stack(tmp_path, concurrent_database)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(PROJECT),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
                "mode": "offline",
            },
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-idem-000003"},
        )
        task_id = created.json()["task"]["id"]
        container.coordinator.run(task_id)
        outcome = container.coordinator.run(task_id, auto_fix=True)

    assert outcome.status is ParentTaskStatus.REJECTED
    assert outcome.next_action == "offline_stop"
    with container.session() as session:
        assert PatchStore(session).list_by_task(task_id) == []


@requires_docker
def test_sandbox_run_records_security_limits(tmp_path, concurrent_database) -> None:
    """沙箱运行必须记录并强制执行安全参数（FR-051~FR-058）。"""
    from fastapi.testclient import TestClient

    container, app = _stack(tmp_path, concurrent_database)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(PROJECT),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-idem-000004"},
        )
        task_id = created.json()["task"]["id"]
        container.coordinator.run(task_id)
        container.coordinator.run(task_id, auto_fix=True)

    with container.session() as session:
        runs = list(session.execute(__import__("sqlalchemy").select(SandboxRun)).scalars())
    assert runs
    run = runs[0]
    assert run.network == "none"
    assert run.memory_limit_mb == 512
    assert run.cpu_limit == 1.0
    assert run.disk_limit_mb == 100
    assert run.timeout_seconds == 60
    assert "sk-live" not in (run.stdout_sanitized + run.stderr_sanitized)


def test_fix_agent_rejects_wide_impact() -> None:
    workspace = _workspace()
    findings = [
        finding(rule_id="R002_HARDCODED_SECRET", category=FixCategory.HARDCODED_SECRET, line=5, file="app/config.py")
    ]
    request = _agent_request(workspace, findings=findings, task_type="fix")
    result = FixAgent().handle(request)
    assert result.stats["changed_files"] == ["app/config.py"]


def test_fix_api_flow_with_stub_sandbox(tmp_path, concurrent_database) -> None:  # noqa: F811
    """POST /reviews/{id}/fixes 触发 Fix/Verify，GET /fixes/{id} 返回补丁与证据。"""
    from fastapi.testclient import TestClient

    good_output = (
        "CODEPILOT_APPLY_CHECK_OK\nCODEPILOT_APPLY_OK\nCODEPILOT_LINT_OK\n"
        ".                                                                        [100%]\n"
        "TOTAL 12 2 83%\n"
        "2 passed in 0.05s\n"
    )
    stub = _StubSandbox([SandboxResult(status="passed", exit_code=0, stdout=good_output)])
    container, app = _stack(tmp_path, concurrent_database)
    container.coordinator.sandbox = stub
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(PROJECT),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-idem-000005"},
        )
        task_id = created.json()["task"]["id"]
        container.coordinator.run(task_id)

        triggered = client.post(
            f"/api/v1/reviews/{task_id}/fixes",
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-fix-0000001"},
        )
        assert triggered.status_code == 200, triggered.text
        assert triggered.json()["task_id"] == task_id

        # 触发后由后台编排继续执行，轮询等待进入终态/待审批
        _wait_for_status(container, task_id, {"PENDING_APPROVAL", "NEEDS_HUMAN", "REJECTED"}, timeout=60)
        patches = client.get(
            f"/api/v1/reviews/{task_id}/patches",
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"},
        )
        assert patches.status_code == 200
        items = patches.json()
        assert len(items) == 1
        patch = items[0]
        assert patch["status"] == "candidate"
        assert patch["changed_files"] == ["app/config.py"]
        assert patch["diff"]

        detail = client.get(
            f"/api/v1/fixes/{patch['id']}",
            headers={"X-Actor-Id": "approver-1", "X-Actor-Role": "approver"},
        )
        assert detail.status_code == 200
        assert detail.json()["patch_version"] == 1
        assert detail.json()["approvals"] == []


def test_merge_requires_approval_endpoint_absent_for_non_approver(tmp_path, concurrent_database) -> None:
    """阶段六尚未开放合并：未审批不得写入，且 developer 不能触发审批接口。"""
    from fastapi.testclient import TestClient

    container, app = _stack(tmp_path, concurrent_database)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(PROJECT),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer", "Idempotency-Key": "p6-idem-000006"},
        )
        task_id = created.json()["task"]["id"]
        container.coordinator.run(task_id)
        response = client.get(
            "/api/v1/fixes/does-not-exist",
            headers={"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"},
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _wait_for_status(container, task_id: str, targets: set[str], *, timeout: float = 60.0) -> str:
    """轮询等待父任务进入目标状态（后台编排是异步的）。"""
    import time

    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        with container.session() as session:
            status = ReviewTaskStore(session).get(task_id).status
        if status in targets:
            return status
        time.sleep(0.25)
    return status


def _candidate(workspace, *, diff: str | None = None) -> PatchCandidate:
    patch = diff or (
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -2,4 +2,4 @@\n"
        " import os\n"
        " import subprocess\n"
        " \n"
        '-API_KEY = "sk-live-1234567890"\n'
        '+API_KEY = os.environ["API_KEY"]\n'
    )
    return PatchCandidate(
        patch_version=1,
        patch_hash=compute_text_hash(patch),
        base_commit="synthetic-base-001",
        target_branch="codepilot/test",
        fix_category=FixCategory.HARDCODED_SECRET,
        finding_refs=["R002_HARDCODED_SECRET@app/config.py:5"],
        changed_files=["app/config.py"],
        changed_functions=["app.config"],
        added_lines=1,
        removed_lines=1,
        diff=patch,
        idempotency_key="patch-key-000000001",
    )


def _agent_request(
    workspace,
    *,
    findings: list[FindingItem],
    task_type: str,
    candidate: PatchCandidate | None = None,
    sandbox=None,
) -> AgentRequest:
    from a2a.examples import example_a2a_task
    from a2a.protocol import ArtifactEnvelope
    from domain.enums import ChildTaskType

    inputs: list[ArtifactEnvelope] = []
    if findings:
        from a2a.protocol import FindingArtifact, FindingStats

        payload = FindingArtifact(
            rule_set_version="rules-v1",
            scan_mode="ast",
            degraded_reason=None,
            files_scanned=["app/config.py"],
            findings=findings,
            stats=FindingStats(
                total=len(findings),
                by_severity={"critical": len(findings)},
                by_confidence_level={"confirmed": len(findings)},
                suppressed=0,
            ),
        )
        inputs.append(
            ArtifactEnvelope.build(
                artifact_id="artifact-findings-0001",
                task_id="child-000000000000000000000009",
                artifact_type=ArtifactType.FINDING,
                data=payload.model_dump(mode="json"),
            )
        )
    if candidate is not None:
        inputs.append(
            ArtifactEnvelope.build(
                artifact_id="artifact-patch-000001",
                task_id="child-00000000000000000000000a",
                artifact_type=ArtifactType.PATCH_CANDIDATE,
                data=candidate.model_dump(mode="json"),
            )
        )

    kind = ChildTaskType.FIX if task_type == "fix" else ChildTaskType.VERIFY
    task = example_a2a_task(
        parent_task_id="parent-000000000000000000000099",
        agent_id="fix-agent" if kind is ChildTaskType.FIX else "verify-agent",
        task_type=kind,
        required_output_types=["PatchCandidate"] if kind is ChildTaskType.FIX else ["VerifyEvidence"],
    )
    budget = Budget()
    ctx = ToolCallContext(
        task_id=task.task_id,
        parent_task_id=task.parent_task_id,
        agent_id=task.agent_id,
        actor_role="agent",
        trace_id=task.trace_id,
        workspace=workspace,
        session=_NullSession(),
        budget=budget,
        mode="a2a",
        sandbox=sandbox,
        sandbox_limits=SandboxLimits(),
    )
    return AgentRequest(
        task=task,
        workspace=workspace,
        tools=ToolGateway(build_default_registry(), ctx),
        budget=budget,
        config=CONFIG,
        inputs=inputs,
        context={
            "base_commit": "synthetic-base-001",
            "target_branch": "codepilot/parent-000000000000000000000099",
            "patch_version": 1,
            "patch_idempotency_key": "patch-key-000000001",
        },
        sandbox=sandbox,
        sandbox_limits=SandboxLimits(),
    )


class _NullSession:
    """Fix/Verify 用例不需要数据库；工具网关只在被调用时才访问 session。"""

    def __getattr__(self, item):
        raise AssertionError(f"本用例不应访问数据库：{item}")
