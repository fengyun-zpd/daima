"""P0 修复验证：写请求的完整幂等链路（宪法第七条、docs/03 §6）。

覆盖：Fix / Approval / Merge / Eval / 内部 A2A 子任务创建与取消 / 人工恢复。
断言"同一键 + 同一请求 → 原结果"，"同一键 + 不同请求 → IDEMPOTENCY_CONFLICT"，
并校验不产生重复补丁、重复审批、重复合并、重复评测或重复子任务。
"""

from __future__ import annotations

import base64
import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import ApprovalDecision, ParentTaskStatus
from evals.fault_matrix import build_zip
from repositories.models import A2AArtifact, Approval, EvalRun, IdempotencyRecord
from repositories.models import A2ATask as A2ATaskRow
from repositories.records import ApprovalStore, PatchStore
from repositories.store import A2ATaskStore, ReviewTaskStore
from sandbox.base import SandboxResult

CONFIG = CodePilotConfig.model_validate({"mode": "a2a", "a2a": {"child_task_timeout_seconds": 45}})
DEVELOPER = {"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"}
APPROVER = {"X-Actor-Id": "approver-1", "X-Actor-Role": "approver"}
ADMIN = {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}
COORDINATOR = {"X-Actor-Id": "coordinator", "X-Actor-Role": "coordinator"}

GOOD_SANDBOX_OUTPUT = (
    "CODEPILOT_APPLY_CHECK_OK\nCODEPILOT_APPLY_OK\nCODEPILOT_LINT_OK\n"
    "TOTAL 12 2 83%\n2 passed in 0.05s\n"
)


class _StubSandbox:
    available = True
    unavailable_reason = ""

    def run(self, *, files, argv, name, limits, patch=None) -> SandboxResult:
        return SandboxResult(status="passed", exit_code=0, stdout=GOOD_SANDBOX_OUTPUT)


@pytest.fixture()
def client(tmp_path, concurrent_database):
    from repositories.content_store import ContentStore

    store = ContentStore(tmp_path / "var")
    container = build_container(url=concurrent_database, auto_create=True, config=CONFIG)
    container.content_store = store
    container.coordinator.content_store = store
    container.coordinator.repo_root = tmp_path / "var"
    container.coordinator.sandbox = _StubSandbox()
    app = create_app(
        container=container,
        recover_on_start=False,
        schedule_on_create=False,
        include_internal=True,
    )
    with TestClient(app) as test_client:
        test_client.container = container  # type: ignore[attr-defined]
        yield test_client


def _create_review(client, *, key: str = "p0-idem-review-1") -> str:
    response = client.post(
        "/api/v1/reviews",
        json={
            "input_type": "zip",
            "content": build_zip(),
            "context_policy": "function",
            "base_commit": "synthetic-base-001",
        },
        headers={**DEVELOPER, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]["id"]


def _reviewed_task(client, *, key: str = "p0-idem-review-1") -> str:
    task_id = _create_review(client, key=key)
    client.container.coordinator.run(task_id)  # type: ignore[attr-defined]
    return task_id


def _wait(client, task_id: str, targets: set[str], *, timeout: float = 90.0) -> str:
    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        with client.container.session() as session:  # type: ignore[attr-defined]
            status = ReviewTaskStore(session).get(task_id).status
        if status in targets:
            return status
        time.sleep(0.25)
    return status


def _to_pending_approval(client, *, key: str) -> tuple[str, str]:
    task_id = _reviewed_task(client, key=key)
    client.post(
        f"/api/v1/reviews/{task_id}/fixes",
        headers={**DEVELOPER, "Idempotency-Key": f"{key}-fix"},
    )
    status = _wait(client, task_id, {"PENDING_APPROVAL", "NEEDS_HUMAN", "REJECTED"})
    assert status == "PENDING_APPROVAL", status
    with client.container.session() as session:  # type: ignore[attr-defined]
        patch = PatchStore(session).latest(task_id)
    assert patch is not None
    return task_id, patch.id


# ---------------------------------------------------------------------------
# Fix 幂等
# ---------------------------------------------------------------------------


def test_fix_trigger_is_idempotent_for_same_key(client) -> None:
    task_id = _reviewed_task(client)
    payload_headers = {**DEVELOPER, "Idempotency-Key": "p0-fix-same-0001"}
    first = client.post(f"/api/v1/reviews/{task_id}/fixes", headers=payload_headers)
    second = client.post(f"/api/v1/reviews/{task_id}/fixes", headers=payload_headers)
    assert first.status_code == second.status_code == 200
    assert second.json().get("idempotent_replay") is True
    assert first.json()["task_id"] == second.json()["task_id"]
    assert first.json()["status"] == second.json()["status"]


def test_fix_trigger_conflicts_on_different_request(client) -> None:
    task_id = _reviewed_task(client, key="p0-idem-review-2")
    first = client.post(
        f"/api/v1/reviews/{task_id}/fixes",
        headers={**DEVELOPER, "Idempotency-Key": "p0-fix-conflict-1"},
    )
    assert first.status_code == 200
    # 同键但作用对象不同（任务 2）→ 冲突
    other = _reviewed_task(client, key="p0-idem-review-3")
    conflict = client.post(
        f"/api/v1/reviews/{other}/fixes",
        headers={**DEVELOPER, "Idempotency-Key": "p0-fix-conflict-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_repeated_fix_trigger_creates_single_patch(client) -> None:
    task_id, patch_id = _to_pending_approval(client, key="p0-idem-review-4")
    for index in range(3):
        client.post(
            f"/api/v1/reviews/{task_id}/fixes",
            headers={**DEVELOPER, "Idempotency-Key": f"p0-fix-extra-{index}"},
        )
    client.container.coordinator.run(task_id, auto_fix=True)  # type: ignore[attr-defined]
    with client.container.session() as session:  # type: ignore[attr-defined]
        patches = PatchStore(session).list_by_task(task_id)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id and row.artifact_type == "PatchCandidate"
        ]
    assert len(patches) == 1, "重复触发不得产生第二个补丁版本"
    assert patch_id == patches[0].id
    assert len(artifacts) == 1


# ---------------------------------------------------------------------------
# 审批幂等
# ---------------------------------------------------------------------------


def test_approval_is_idempotent_for_same_key(client) -> None:
    _task_id, patch_id = _to_pending_approval(client, key="p0-idem-review-5")
    body = {"decision": "approve", "patch_version": 1, "reason": "证据充分"}
    headers = {**APPROVER, "Idempotency-Key": "p0-approve-same-1"}
    first = client.post(f"/api/v1/fixes/{patch_id}/approval", json=body, headers=headers)
    second = client.post(f"/api/v1/fixes/{patch_id}/approval", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.json().get("idempotent_replay") is True
    assert first.json()["approval_id"] == second.json()["approval_id"]
    with client.container.session() as session:  # type: ignore[attr-defined]
        count = session.execute(select(func.count()).select_from(Approval)).scalar()
    assert count == 1, "重复审批不得产生第二条审批记录"


def test_approval_conflict_on_different_decision(client) -> None:
    _task_id, patch_id = _to_pending_approval(client, key="p0-idem-review-6")
    headers = {**APPROVER, "Idempotency-Key": "p0-approve-conflict-1"}
    ok = client.post(
        f"/api/v1/fixes/{patch_id}/approval",
        json={"decision": "approve", "patch_version": 1, "reason": "ok"},
        headers=headers,
    )
    assert ok.status_code == 200
    conflict = client.post(
        f"/api/v1/fixes/{patch_id}/approval",
        json={"decision": "reject", "patch_version": 1, "reason": "反悔"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------------
# 合并幂等
# ---------------------------------------------------------------------------


def test_merge_is_idempotent_for_same_key(client, tmp_path) -> None:
    task_id, patch_id = _to_pending_approval(client, key="p0-idem-review-7")
    client.post(
        f"/api/v1/fixes/{patch_id}/approval",
        json={"decision": "approve", "patch_version": 1, "reason": "ok"},
        headers={**APPROVER, "Idempotency-Key": "p0-approve-merge-1"},
    )
    headers = {**APPROVER, "Idempotency-Key": "p0-merge-same-0001"}
    first = client.post(
        f"/api/v1/fixes/{patch_id}/merge", json={"patch_version": 1}, headers=headers
    )
    second = client.post(
        f"/api/v1/fixes/{patch_id}/merge", json={"patch_version": 1}, headers=headers
    )
    assert first.status_code == second.status_code == 200, second.text
    assert second.json().get("idempotent_replay") is True
    assert first.json()["commit"] == second.json()["commit"]
    assert first.json()["branch"] == f"codepilot/{task_id}"
    with client.container.session() as session:  # type: ignore[attr-defined]
        task = ReviewTaskStore(session).get(task_id)
        patch = PatchStore(session).get(patch_id)
    assert task.status == str(ParentTaskStatus.MERGED)
    assert patch.status == "applied"


def test_merge_with_different_key_after_merge_is_rejected(client) -> None:
    _task_id, patch_id = _to_pending_approval(client, key="p0-idem-review-8")
    client.post(
        f"/api/v1/fixes/{patch_id}/approval",
        json={"decision": "approve", "patch_version": 1, "reason": "ok"},
        headers={**APPROVER, "Idempotency-Key": "p0-approve-merge-2"},
    )
    first = client.post(
        f"/api/v1/fixes/{patch_id}/merge",
        json={"patch_version": 1},
        headers={**APPROVER, "Idempotency-Key": "p0-merge-first-001"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/v1/fixes/{patch_id}/merge",
        json={"patch_version": 1},
        headers={**APPROVER, "Idempotency-Key": "p0-merge-second-01"},
    )
    assert second.status_code in {403, 409}
    assert second.json()["code"] in {"FORBIDDEN", "ILLEGAL_STATE_TRANSITION", "CONFLICT"}


# ---------------------------------------------------------------------------
# 评测幂等
# ---------------------------------------------------------------------------


def test_eval_run_is_idempotent_for_same_key(client) -> None:
    body = {"modes": ["offline"], "case_limit": 1, "runs_per_case": 1}
    headers = {**ADMIN, "Idempotency-Key": "p0-eval-same-0001"}
    first = client.post("/api/v1/evals/run", json=body, headers=headers)
    second = client.post("/api/v1/evals/run", json=body, headers=headers)
    assert first.status_code == second.status_code == 200, second.text
    assert first.json()["run_id"] == second.json()["run_id"], "重复提交不得启动第二次评测"
    assert second.json().get("idempotent_replay") is True
    with client.container.session() as session:  # type: ignore[attr-defined]
        count = session.execute(select(func.count()).select_from(EvalRun)).scalar()
    assert count == 1


def test_eval_run_conflicts_on_changed_payload(client) -> None:
    headers = {**ADMIN, "Idempotency-Key": "p0-eval-conflict-1"}
    first = client.post(
        "/api/v1/evals/run",
        json={"modes": ["offline"], "case_limit": 1, "runs_per_case": 1},
        headers=headers,
    )
    assert first.status_code == 200
    conflict = client.post(
        "/api/v1/evals/run",
        json={"modes": ["offline"], "case_limit": 2, "runs_per_case": 1},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------------
# 内部 A2A 子任务幂等
# ---------------------------------------------------------------------------


def test_internal_a2a_submit_is_idempotent(client) -> None:
    task_id = _reviewed_task(client, key="p0-idem-review-9")
    with client.container.session() as session:  # type: ignore[attr-defined]
        child = A2ATaskStore(session).list_by_parent(task_id)[0]
        payload = {
            "task_id": child.id,
            "parent_task_id": task_id,
            "trace_id": child.trace_id,
            "agent_id": child.agent_id,
            "task_type": child.task_type,
            "protocol_version": "0.1",
            "status": "submitted",
            "input_artifacts": [],
            "required_output_types": list(child.required_output_types or []),
            "deadline": child.deadline.isoformat(),
            "attempt": 1,
            "idempotency_key": child.idempotency_key,
            "correlation_id": child.correlation_id,
            "state_version": 1,
        }
    headers = {
        **COORDINATOR,
        "X-A2A-Protocol-Version": "0.1",
        "X-Trace-Id": payload["trace_id"],
        "X-Correlation-Id": payload["correlation_id"],
        "Idempotency-Key": payload["idempotency_key"],
    }
    first = client.post(
        f"/internal/a2a/agents/{payload['agent_id']}/tasks", json=payload, headers=headers
    )
    second = client.post(
        f"/internal/a2a/agents/{payload['agent_id']}/tasks", json=payload, headers=headers
    )
    assert first.status_code == second.status_code == 202, second.text
    assert first.json()["task_id"] == second.json()["task_id"]
    with client.container.session() as session:  # type: ignore[attr-defined]
        rows = session.execute(
            select(func.count())
            .select_from(A2ATaskRow)
            .where(A2ATaskRow.parent_task_id == task_id, A2ATaskRow.side == "agent")
        ).scalar()
    assert rows == 1, "重复提交不得创建第二个远端子任务"


def test_internal_a2a_submit_conflicts_on_changed_payload(client) -> None:
    task_id = _reviewed_task(client, key="p0-idem-review-10")
    with client.container.session() as session:  # type: ignore[attr-defined]
        child = A2ATaskStore(session).list_by_parent(task_id)[0]
        base = {
            "task_id": child.id,
            "parent_task_id": task_id,
            "trace_id": child.trace_id,
            "agent_id": child.agent_id,
            "task_type": child.task_type,
            "protocol_version": "0.1",
            "status": "submitted",
            "input_artifacts": [],
            "required_output_types": list(child.required_output_types or []),
            "deadline": child.deadline.isoformat(),
            "attempt": 1,
            "idempotency_key": "p0-a2a-conflict-key",
            "correlation_id": child.correlation_id,
            "state_version": 1,
        }
    headers = {
        **COORDINATOR,
        "X-A2A-Protocol-Version": "0.1",
        "X-Trace-Id": base["trace_id"],
        "X-Correlation-Id": base["correlation_id"],
        "Idempotency-Key": base["idempotency_key"],
    }
    first = client.post(
        f"/internal/a2a/agents/{base['agent_id']}/tasks", json=base, headers=headers
    )
    assert first.status_code == 202, first.text
    changed = dict(base, required_output_types=["ImpactReport"], deadline=base["deadline"])
    conflict = client.post(
        f"/internal/a2a/agents/{base['agent_id']}/tasks", json=changed, headers=headers
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_internal_a2a_cancel_is_idempotent(client) -> None:
    """取消使用同一个幂等键重复提交时返回原结果（不重复落库、不报错）。"""
    task_id = _reviewed_task(client, key="p0-idem-review-11")
    child_id = _seed_agent_task(client, task_id, idempotency_key="p0-cancel-seed-01")
    headers = {
        **COORDINATOR,
        "X-A2A-Protocol-Version": "0.1",
        "Idempotency-Key": "p0-a2a-cancel-key01",
    }
    first = client.post(f"/internal/a2a/tasks/{child_id}/cancel", headers=headers)
    second = client.post(f"/internal/a2a/tasks/{child_id}/cancel", headers=headers)
    assert first.status_code == second.status_code == 200, second.text
    assert second.json().get("idempotent_replay") is True
    assert first.json()["status"] == "canceled"
    assert second.json()["status"] == "canceled"


def _seed_agent_task(client, parent_task_id: str, *, idempotency_key: str) -> str:
    """直接在 Agent 侧创建一条 submitted 子任务记录（不触发执行），用于取消类断言。"""
    from domain.enums import ChildTaskStatus, ChildTaskType
    from domain.ids import new_child_task_id

    with client.container.session() as session:  # type: ignore[attr-defined]
        tasks = A2ATaskStore(session)
        child = A2ATaskStore(session).list_by_parent(parent_task_id)[0]
        payload = {
            "task_id": new_child_task_id("review"),
            "parent_task_id": parent_task_id,
            "trace_id": child.trace_id,
            "correlation_id": child.correlation_id,
            "agent_id": child.agent_id,
            "task_type": child.task_type,
            "status": ChildTaskStatus.SUBMITTED,
            "input_artifacts": [],
            "required_output_types": list(child.required_output_types or []),
            "deadline": child.deadline,
            "attempt": 1,
            "idempotency_key": idempotency_key,
        }
        from a2a.protocol import A2ATask

        row, _created = tasks.submit(
            A2ATask.model_validate(payload), transport="http", side="agent"
        )
        session.commit()
        return row.id
    del ChildTaskType


def test_internal_a2a_cancel_requires_key(client) -> None:
    task_id = _reviewed_task(client, key="p0-idem-review-12")
    with client.container.session() as session:  # type: ignore[attr-defined]
        child = A2ATaskStore(session).list_by_parent(task_id)[0]
    response = client.post(
        f"/internal/a2a/tasks/{child.id}/cancel",
        headers={**COORDINATOR, "X-A2A-Protocol-Version": "0.1"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


# ---------------------------------------------------------------------------
# 人工恢复幂等
# ---------------------------------------------------------------------------


def test_resume_is_idempotent_and_conflicts_on_change(client) -> None:
    task_id = _create_review(client, key="p0-idem-review-13")
    with client.container.session() as session:  # type: ignore[attr-defined]
        store = ReviewTaskStore(session)
        task = store.get(task_id)
        store.apply_intent(
            task_id,
            expected_version=task.state_version,
            owner="coordinator",
            changes={"status": str(ParentTaskStatus.NEEDS_HUMAN)},
            reason="needs_human",
            idempotency_key="p0-resume-needs-human",
        )
        version = store.get(task_id).state_version

    body = {"target_status": "REVIEWING", "expected_version": version, "reason": "人工继续"}
    headers = {**ADMIN, "Idempotency-Key": "p0-resume-same-0001"}
    first = client.post(f"/api/v1/reviews/{task_id}/resume", json=body, headers=headers)
    second = client.post(f"/api/v1/reviews/{task_id}/resume", json=body, headers=headers)
    assert first.status_code == second.status_code == 200, second.text
    assert second.json()["status"] == first.json()["status"]

    conflict = client.post(
        f"/api/v1/reviews/{task_id}/resume",
        json={"target_status": "FIXING", "expected_version": version, "reason": "改主意"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def test_idempotency_ledger_records_completed_commands(client) -> None:
    task_id = _reviewed_task(client, key="p0-idem-review-14")
    client.post(
        f"/api/v1/reviews/{task_id}/fixes",
        headers={**DEVELOPER, "Idempotency-Key": "p0-ledger-fix-001"},
    )
    with client.container.session() as session:  # type: ignore[attr-defined]
        rows = session.execute(select(IdempotencyRecord)).scalars().all()
    fix_rows = [row for row in rows if row.command_type == "trigger_fix"]
    assert fix_rows and fix_rows[0].aggregate_ref == task_id
    assert fix_rows[0].status in {"in_progress", "completed"}
    assert fix_rows[0].request_hash.startswith("sha256:")


def test_short_idempotency_key_rejected_on_all_write_endpoints(client) -> None:
    task_id = _reviewed_task(client, key="p0-idem-review-15")
    endpoints = [
        ("post", f"/api/v1/reviews/{task_id}/fixes", DEVELOPER, None),
        ("post", "/api/v1/fixes/patch-00000000001/approval", APPROVER,
         {"decision": "approve", "patch_version": 1, "reason": "x"}),
        ("post", "/api/v1/fixes/patch-00000000001/merge", APPROVER, {"patch_version": 1}),
        ("post", "/api/v1/evals/run", ADMIN, {"modes": ["offline"], "case_limit": 1}),
    ]
    for _method, url, headers, body in endpoints:
        response = client.post(url, json=body, headers={**headers, "Idempotency-Key": "short"})
        assert response.status_code == 400, url
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED", url
    with client.container.session() as session:  # type: ignore[attr-defined]
        task = ReviewTaskStore(session).get(task_id)
    assert task.status == str(ParentTaskStatus.REVIEWED), "非法幂等键不得触发任何副作用"


def test_approval_records_single_row_for_repeated_same_request(client) -> None:
    _task_id, patch_id = _to_pending_approval(client, key="p0-idem-review-16")
    body = {"decision": "approve", "patch_version": 1, "reason": "ok"}
    for index in range(2):
        client.post(
            f"/api/v1/fixes/{patch_id}/approval",
            json=body,
            headers={**APPROVER, "Idempotency-Key": f"p0-approve-loop-{index}"},
        )
    with client.container.session() as session:  # type: ignore[attr-defined]
        rows = ApprovalStore(session).list_by_task(_task_id)
    assert len(rows) == 1
    assert rows[0].decision == str(ApprovalDecision.APPROVE)


def test_zip_payload_is_deterministic() -> None:
    assert build_zip() == build_zip()


def test_zip_inside_helper_still_valid() -> None:
    raw = base64.b64decode(build_zip())
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert "app/main.py" in archive.namelist()
