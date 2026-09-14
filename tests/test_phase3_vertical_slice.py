"""阶段三：InProcessInvoker 垂直切片集成测试（docs/00 §4）。

覆盖流程：
POST /api/v1/reviews → 创建父任务 → Coordinator 读取 Agent Card →
创建 Review / Impact 子任务 → 返回 Finding / ImpactReport Artifact →
Coordinator 校验 Artifact → 父任务进入 REVIEWED → 查询接口与 SSE 可回放 trace。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from a2a.examples import EXAMPLE_INPUT_DIFF
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import (
    ArtifactType,
    ChildTaskStatus,
    ChildTaskType,
    ParentTaskStatus,
)
from repositories.content_store import ContentStore
from repositories.models import A2AArtifact, A2AMessage, Approval, AuditEvent
from repositories.store import A2ATaskStore, ReviewTaskStore

DEV_HEADERS = {"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"}
ADMIN_HEADERS = {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}


def make_container(tmp_path, *, config: CodePilotConfig | None = None):
    url = f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}"
    container = build_container(url=url, auto_create=True, config=config)
    container.content_store = ContentStore(tmp_path / "var")
    container.coordinator.content_store = container.content_store
    return container


@pytest.fixture()
def client(tmp_path):
    container = make_container(tmp_path)
    app = create_app(container=container, recover_on_start=False, schedule_on_create=False)
    with TestClient(app) as test_client:
        test_client.container = container  # type: ignore[attr-defined]
        yield test_client


def create_review(client, *, key: str = "idem-000000000001", diff: str = EXAMPLE_INPUT_DIFF):
    response = client.post(
        "/api/v1/reviews",
        json={
            "input_type": "diff",
            "content": diff,
            "context_policy": "function",
            "base_commit": "synthetic-base-001",
        },
        headers={**DEV_HEADERS, "Idempotency-Key": key},
    )
    return response


# ---------------------------------------------------------------------------
# 主路径
# ---------------------------------------------------------------------------


def test_vertical_slice_reaches_reviewed(client) -> None:
    response = create_review(client)
    assert response.status_code == 200, response.text
    body = response.json()
    task_id = body["task"]["id"]
    assert body["created"] is True
    assert body["task"]["status"] == "DRAFT"

    # 等待后台编排结束
    container = client.container
    container.coordinator.run(task_id)

    detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV_HEADERS)
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["task"]["status"] == str(ParentTaskStatus.REVIEWED)
    assert payload["task"]["state_version"] >= 2

    child_types = {item["task_type"] for item in payload["child_tasks"]}
    assert child_types == {str(ChildTaskType.REVIEW), str(ChildTaskType.IMPACT)}
    assert all(item["status"] == str(ChildTaskStatus.COMPLETED) for item in payload["child_tasks"])

    artifact_types = {item["artifact_type"] for item in payload["artifacts"]}
    assert str(ArtifactType.FINDING) in artifact_types
    assert str(ArtifactType.IMPACT_REPORT) in artifact_types
    assert all(item["validated"] for item in payload["artifacts"])

    rule_ids = {item["rule_id"] for item in payload["comments"]}
    assert "R002_HARDCODED_SECRET" in rule_ids
    assert "R003_SHELL_TRUE" in rule_ids

    assert payload["task"]["summary"]["findings"] >= 2
    assert payload["task"]["summary"]["risk_level"] in {"low", "medium", "high"}


def test_artifacts_satisfy_schema_and_hash(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    container = client.container
    container.coordinator.run(task_id)

    with container.session() as session:
        rows = session.execute(select(A2AArtifact)).scalars().all()
    assert rows
    for row in rows:
        assert row.validated is True
        assert row.content_hash.startswith("sha256:")
        assert row.schema_version == "1.0"

    from a2a.schema_registry import validate_artifact_envelope, validate_artifact_payload

    for row in rows:
        validate_artifact_payload(row.artifact_type, row.data)
        validate_artifact_envelope(
            {
                "artifact_id": row.id,
                "task_id": row.task_id,
                "artifact_type": row.artifact_type,
                "schema_version": row.schema_version,
                "content_hash": row.content_hash,
                "size_bytes": row.size_bytes,
                "data": row.data,
            }
        )


def test_trace_is_complete(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)

    audit = client.get("/api/v1/audit", headers=ADMIN_HEADERS).json()
    event_types = {item["event_type"] for item in audit}
    for expected in {
        "task_started",
        "agent_card_resolved",
        "child_task_created",
        "agent_started",
        "artifact_received",
        "child_task_completed",
    }:
        assert expected in event_types, f"缺少事件 {expected}"

    with client.container.session() as session:
        from repositories.audit import trace_integrity
        from repositories.models import ReviewTask

        task = session.get(ReviewTask, task_id)
        integrity = trace_integrity(session, task.trace_id)
    assert integrity["complete"] is True
    assert integrity["has_artifact_event"] is True


def test_messages_are_recorded(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)
    with client.container.session() as session:
        messages = session.execute(select(A2AMessage)).scalars().all()
    types = {item.message_type for item in messages}
    assert "task.submitted" in types
    assert "task.completed" in types
    assert all(item.payload_hash.startswith("sha256:") for item in messages)


# ---------------------------------------------------------------------------
# 幂等 / 权限 / 校验
# ---------------------------------------------------------------------------


def test_repeated_submission_returns_same_task(client) -> None:
    first = create_review(client, key="idem-00000000000a").json()
    second = create_review(client, key="idem-00000000000a").json()
    assert first["task"]["id"] == second["task"]["id"]
    assert second["created"] is False


def test_same_key_different_input_conflicts(client) -> None:
    create_review(client, key="idem-00000000000b")
    other_diff = EXAMPLE_INPUT_DIFF.replace("sk-live-0987654321", "sk-live-1111111111")
    response = create_review(client, key="idem-00000000000b", diff=other_diff)
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_missing_headers_rejected(client) -> None:
    response = client.post(
        "/api/v1/reviews",
        json={"input_type": "diff", "content": EXAMPLE_INPUT_DIFF, "base_commit": "b"},
        headers={"Idempotency-Key": "idem-00000000000c"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_missing_idempotency_key_rejected(client) -> None:
    response = client.post(
        "/api/v1/reviews",
        json={"input_type": "diff", "content": EXAMPLE_INPUT_DIFF, "base_commit": "b"},
        headers=DEV_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_developer_cannot_read_audit(client) -> None:
    response = client.get("/api/v1/audit", headers=DEV_HEADERS)
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_agent_card_endpoint_requires_admin(client) -> None:
    assert client.get("/api/v1/agents", headers=DEV_HEADERS).status_code == 403
    response = client.get("/api/v1/agents", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    assert {item["agent_id"] for item in response.json()} == {
        "review-agent",
        "impact-agent",
        "fix-agent",
        "verify-agent",
    }


def test_invalid_input_rejected(client) -> None:
    response = create_review(client, key="idem-00000000000d", diff="   ")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


def test_path_traversal_diff_rejected(client) -> None:
    traversal = (
        "diff --git a/../../etc/passwd b/../../etc/passwd\n"
        "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    response = create_review(client, key="idem-00000000000e", diff=traversal)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# SSE / 恢复
# ---------------------------------------------------------------------------


def test_sse_stream_delivers_events_and_closes(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)

    with client.stream("GET", f"/api/v1/reviews/{task_id}/events", headers=DEV_HEADERS) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: task_started" in body
    assert "event: artifact_received" in body
    assert "stream_closed" in body
    payloads = [line[6:] for line in body.splitlines() if line.startswith("data: {")]
    parsed = [json.loads(item) for item in payloads if item.strip().startswith("{")]
    events = [item for item in parsed if "event_id" in item]
    assert events
    assert all("trace_id" in item and "event_type" in item for item in events)
    assert {item["event_type"] for item in events} >= {"task_started", "artifact_received"}


def test_sse_resume_with_last_event_id(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)

    first = client.get("/api/v1/audit", headers=ADMIN_HEADERS).json()
    cursor = first[0]["id"]
    with client.stream(
        "GET",
        f"/api/v1/reviews/{task_id}/events",
        headers={**DEV_HEADERS, "Last-Event-ID": cursor},
    ) as response:
        body = "".join(response.iter_text())
    ids = [line[4:] for line in body.splitlines() if line.startswith("id: ")]
    assert cursor not in ids


def test_recovery_after_restart_does_not_replay_completed_children(tmp_path) -> None:
    container = make_container(tmp_path)
    app = create_app(container=container, recover_on_start=False, schedule_on_create=False)
    with TestClient(app) as client:
        client.container = container  # type: ignore[attr-defined]
        task_id = create_review(client).json()["task"]["id"]
        container.coordinator.run(task_id)

        with container.session() as session:
            before = session.execute(select(A2AArtifact)).scalars().all()
            child_ids = [row.id for row in A2ATaskStore(session).list_by_parent(task_id)]

        plan = container.coordinator.recovery_plan(task_id)
        assert plan.completed_children or plan.next_action == "noop"

    # 模拟服务重启：同一数据库新建容器并触发恢复
    restarted = build_container(
        url=container.database_url,
        auto_create=False,
        config=container.config,
    )
    restarted.content_store = container.content_store
    restarted.coordinator.content_store = container.content_store
    restarted.coordinator.recover()

    with restarted.session() as session:
        after = session.execute(select(A2AArtifact)).scalars().all()
        task = ReviewTaskStore(session).get(task_id)
        children = A2ATaskStore(session).list_by_parent(task_id)

    assert len(after) == len(before)
    assert [row.id for row in children] == child_ids
    assert task.status == str(ParentTaskStatus.REVIEWED)


def test_restart_resumes_incomplete_task(tmp_path) -> None:
    container = make_container(tmp_path)
    app = create_app(container=container, recover_on_start=False, schedule_on_create=False)
    with TestClient(app) as client:
        client.container = container  # type: ignore[attr-defined]
        task_id = create_review(client).json()["task"]["id"]
        # 只把父任务推进到 REVIEWING，不执行子任务（模拟崩溃点）
        with container.session() as session:
            ReviewTaskStore(session).apply_intent(
                task_id,
                expected_version=1,
                owner="coordinator",
                changes={"status": str(ParentTaskStatus.REVIEWING)},
                reason="input_valid",
                idempotency_key=f"{task_id}:enter_reviewing",
            )

    restarted = build_container(url=container.database_url, auto_create=False, config=container.config)
    restarted.content_store = container.content_store
    restarted.coordinator.content_store = container.content_store
    restarted.coordinator.recover()

    with restarted.session() as session:
        task = ReviewTaskStore(session).get(task_id)
        children = A2ATaskStore(session).list_by_parent(task_id)
        artifacts = session.execute(select(A2AArtifact)).scalars().all()

    assert task.status == str(ParentTaskStatus.REVIEWED)
    assert len(children) == 2
    assert len(artifacts) >= 2


# ---------------------------------------------------------------------------
# 越权与安全
# ---------------------------------------------------------------------------


def test_review_agent_cannot_call_write_tools(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)
    from domain.errors import CodePilotError, ErrorCode
    from tools.registry import ToolCallContext, ToolRegistry

    container = client.container
    with container.session() as session:
        row = A2ATaskStore(session).list_by_parent(task_id)[0]
        workspace = container.coordinator._load_workspace(task_id, session)
        ctx = ToolCallContext(
            task_id=row.id,
            parent_task_id=task_id,
            agent_id="review-agent",
            actor_role="agent",
            trace_id=row.trace_id,
            workspace=workspace,
            session=session,
            budget=_budget(),
        )
        registry: ToolRegistry = container.tool_registry
        with pytest.raises(CodePilotError) as excinfo:
            registry.call(ctx, "write_patch", {"path": "a.py"}, idempotency_key="tool-idem-write")
        assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
        # 未注册工具同样默认拒绝
        with pytest.raises(CodePilotError):
            registry.call(ctx, "rm_rf", {}, idempotency_key="tool-idem-rm")


def test_readonly_tool_call_is_audited_and_idempotent(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)
    container = client.container
    from tools.registry import ToolCallContext

    with container.session() as session:
        row = A2ATaskStore(session).list_by_parent(task_id)[0]
        workspace = container.coordinator._load_workspace(task_id, session)
        ctx = ToolCallContext(
            task_id=row.id,
            parent_task_id=task_id,
            agent_id="review-agent",
            actor_role="agent",
            trace_id=row.trace_id,
            workspace=workspace,
            session=session,
            budget=_budget(),
        )
        first = container.tool_registry.call(
            ctx, "read_file", {"path": workspace.python_files[0]}, idempotency_key="tool-idem-read"
        )
        second = container.tool_registry.call(
            ctx, "read_file", {"path": workspace.python_files[0]}, idempotency_key="tool-idem-read"
        )
        events = session.execute(select(AuditEvent)).scalars().all()

    assert first.ok is True
    assert first.result_hash == second.result_hash
    assert second.replayed is True
    assert any(event.event_type == "tool_called" for event in events)


def test_invalid_tool_params_rejected(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)
    container = client.container
    from domain.errors import CodePilotError, ErrorCode
    from tools.registry import ToolCallContext

    with container.session() as session:
        row = A2ATaskStore(session).list_by_parent(task_id)[0]
        workspace = container.coordinator._load_workspace(task_id, session)
        ctx = ToolCallContext(
            task_id=row.id,
            parent_task_id=task_id,
            agent_id="review-agent",
            actor_role="agent",
            trace_id=row.trace_id,
            workspace=workspace,
            session=session,
            budget=_budget(),
        )
        with pytest.raises(CodePilotError) as excinfo:
            container.tool_registry.call(
                ctx, "read_file", {"path": "a.py", "unexpected": 1}, idempotency_key="tool-idem-bad"
            )
        assert excinfo.value.code is ErrorCode.INVALID_INPUT


def test_agent_cannot_write_parent_task(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)
    from domain.errors import CodePilotError, ErrorCode

    container = client.container
    with container.session() as session:
        store = ReviewTaskStore(session)
        task = store.get(task_id)
        with pytest.raises(CodePilotError) as excinfo:
            store.apply_intent(
                task_id,
                expected_version=task.state_version,
                owner="review-agent",
                changes={"status": str(ParentTaskStatus.FIXING)},
                reason="agent_override",
                idempotency_key="agent-write-0001",
            )
        assert excinfo.value.code is ErrorCode.PERMISSION_DENIED


def test_no_approval_records_created_in_slice(client) -> None:
    task_id = create_review(client).json()["task"]["id"]
    client.container.coordinator.run(task_id)
    with client.container.session() as session:
        assert session.execute(select(Approval)).scalars().all() == []


def test_offline_mode_runs_same_contract(client) -> None:
    response = client.post(
        "/api/v1/reviews",
        json={
            "input_type": "diff",
            "content": EXAMPLE_INPUT_DIFF,
            "base_commit": "synthetic-base-001",
            "mode": "offline",
        },
        headers={**DEV_HEADERS, "Idempotency-Key": "idem-offline-0001"},
    )
    assert response.status_code == 200
    task_id = response.json()["task"]["id"]
    assert response.json()["task"]["mode"] == "offline"
    outcome = client.container.coordinator.run(task_id)
    assert outcome.status is ParentTaskStatus.REVIEWED
    assert outcome.findings >= 2


def test_single_and_a2a_produce_same_contract(client) -> None:
    results = {}
    for index, mode in enumerate(("single", "a2a")):
        response = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "diff",
                "content": EXAMPLE_INPUT_DIFF,
                "base_commit": "synthetic-base-001",
                "mode": mode,
            },
            headers={**DEV_HEADERS, "Idempotency-Key": f"idem-mode-{index:04d}"},
        )
        task_id = response.json()["task"]["id"]
        client.container.coordinator.run(task_id)
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV_HEADERS).json()
        results[mode] = detail

    single, a2a = results["single"], results["a2a"]
    assert single["task"]["status"] == a2a["task"]["status"] == str(ParentTaskStatus.REVIEWED)
    assert {item["rule_id"] for item in single["comments"]} == {
        item["rule_id"] for item in a2a["comments"]
    }
    assert {item["artifact_type"] for item in single["artifacts"]} == {
        item["artifact_type"] for item in a2a["artifacts"]
    }


def _budget():
    from domain.budget import Budget

    return Budget()
