"""阶段五测试：HTTP A2A（FR-091~FR-100、docs/05、docs/06 故障矩阵）。

覆盖：
- Agent Card 查询与协议版本/能力协商；
- HTTP Task 提交、状态查询、SSE 事件、取消；
- 超时先对账 + 最多一次安全重试 + 无重复副作用；
- SSE 断线后轮询补偿；
- Artifact Schema 与 content_hash 校验；
- Coordinator 重启恢复；
- single 与 a2a 使用同一套契约与 Artifact 模型（FR-099）。

传输层使用真实 HTTP 服务（uvicorn 后台线程），不使用 ASGI 直连替代。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select

from a2a.client import A2AClient
from a2a.examples import EXAMPLE_INPUT_DIFF, example_a2a_task
from a2a.http_invoker import A2AInvoker
from a2a.protocol import A2ATask, make_deadline
from apps.api.agent_service import FaultInjector
from apps.api.deps import attach_http_invoker, build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import (
    ArtifactType,
    ChildTaskStatus,
    ChildTaskType,
    ContextPolicy,
    InputType,
    ParentTaskStatus,
    RunMode,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_child_task_id
from repositories.content_store import ContentStore
from repositories.models import A2AArtifact, AuditEvent
from repositories.models import A2ATask as A2ATaskRow
from repositories.records import CommentStore
from repositories.store import A2ATaskStore, ReviewTaskStore

COORDINATOR_HEADERS = {"X-Actor-Id": "coordinator", "X-Actor-Role": "coordinator"}


class UvicornThread:
    """在后台线程运行真实 HTTP 服务。"""

    def __init__(self, app: FastAPI) -> None:
        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                for server in getattr(self.server, "servers", []):
                    for sock in server.sockets:
                        host, port = sock.getsockname()[:2]
                        return f"http://{host}:{port}"
            time.sleep(0.05)
        raise RuntimeError("uvicorn 未能在 30 秒内启动")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


@dataclass
class A2AStack:
    base_url: str
    agent_container: Any
    coordinator_container: Any
    server: UvicornThread
    app: FastAPI
    path: Path

    @property
    def agent_service(self):
        return self.app.state.agent_service

    def set_faults(self, **kwargs) -> None:
        self.agent_service.faults = FaultInjector(enabled=True, **kwargs)

    def clear_faults(self) -> None:
        self.agent_service.faults = FaultInjector(enabled=False)


def _config(timeout_seconds: int) -> CodePilotConfig:
    return CodePilotConfig.model_validate(
        {"mode": "a2a", "a2a": {"child_task_timeout_seconds": timeout_seconds}}
    )


def _build_stack(tmp_path: Path, *, timeout_seconds: int = 45, database_url: str | None = None) -> A2AStack:
    config = _config(timeout_seconds)
    url = database_url or f"sqlite+pysqlite:///{(tmp_path / 'a2a.db').as_posix()}"
    store = ContentStore(tmp_path / "var")

    agent_container = build_container(url=url, auto_create=True, config=config)
    agent_container.content_store = store
    agent_container.coordinator.content_store = store
    coordinator_container = build_container(url=url, auto_create=True, config=config)
    coordinator_container.content_store = store
    coordinator_container.coordinator.content_store = store

    app = create_app(
        container=agent_container,
        recover_on_start=False,
        schedule_on_create=False,
        include_internal=True,
    )
    server = UvicornThread(app)
    base_url = server.start()
    assert attach_http_invoker(coordinator_container, base_url=base_url) == "http"
    return A2AStack(
        base_url=base_url,
        agent_container=agent_container,
        coordinator_container=coordinator_container,
        server=server,
        app=app,
        path=tmp_path,
    )


def _close_stack(stack: A2AStack) -> None:
    invoker = stack.coordinator_container.coordinator.invoker
    if isinstance(invoker, A2AInvoker):
        invoker.close()
    stack.server.stop()


@pytest.fixture()
def stack(tmp_path, concurrent_database):
    resolved = _build_stack(tmp_path, database_url=concurrent_database)
    try:
        yield resolved
    finally:
        _close_stack(resolved)


@pytest.fixture()
def short_stack(tmp_path, concurrent_database):
    """子任务 deadline 缩短到 1 秒，用于超时/未知状态类故障场景。"""
    resolved = _build_stack(tmp_path, timeout_seconds=1, database_url=concurrent_database)
    try:
        yield resolved
    finally:
        _close_stack(resolved)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def create_review(container, *, key: str = "http-idem-00000001") -> str:
    from agents.coordinator.coordinator import ReviewRequest

    task, _ = container.coordinator.create_review(
        ReviewRequest(
            input_type=InputType.DIFF,
            content=EXAMPLE_INPUT_DIFF,
            base_commit="synthetic-base-001",
            actor_id="dev-1",
            actor_role="developer",
            idempotency_key=key,
            context_policy=ContextPolicy.FUNCTION,
            mode=RunMode.A2A,
        )
    )
    return task.id


def submit_direct(
    stack: A2AStack,
    parent_id: str,
    *,
    agent_id: str = "review-agent",
    task_type: ChildTaskType = ChildTaskType.REVIEW,
    required: list[str] | None = None,
    deadline_delta: int = 45,
    idempotency_key: str = "direct-case-0000001",
) -> dict:
    client = A2AClient(stack.base_url)
    payload = example_a2a_task(
        parent_task_id=parent_id,
        agent_id=agent_id,
        task_type=task_type,
        required_output_types=required or ["Finding"],
    ).model_dump(mode="json")
    payload.update(
        {
            "task_id": new_child_task_id(str(task_type)),
            "idempotency_key": idempotency_key,
            "correlation_id": f"{parent_id}:{task_type}",
            "deadline": make_deadline(deadline_delta).isoformat(),
        }
    )
    return client.submit_task(payload)


# ---------------------------------------------------------------------------
# 主路径与契约一致性
# ---------------------------------------------------------------------------


def test_http_vertical_slice_reaches_reviewed(stack: A2AStack) -> None:
    task_id = create_review(stack.coordinator_container)
    outcome = stack.coordinator_container.coordinator.run(task_id)
    assert outcome.status is ParentTaskStatus.REVIEWED, outcome.to_payload()

    with stack.coordinator_container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
    assert {row.transport for row in children} == {"http"}
    assert all(ChildTaskStatus(row.status) is ChildTaskStatus.COMPLETED for row in children)
    assert {row.artifact_type for row in artifacts} >= {
        str(ArtifactType.FINDING),
        str(ArtifactType.IMPACT_REPORT),
    }
    assert all(row.validated for row in artifacts)
    assert all(row.remote_task_id for row in children)


def test_single_and_a2a_share_contract(stack: A2AStack) -> None:
    container = stack.coordinator_container
    a2a_task_id = create_review(container, key="http-idem-00000002")
    a2a_outcome = container.coordinator.run(a2a_task_id)

    from a2a.inprocess import InProcessInvoker

    container.coordinator.invoker = InProcessInvoker(
        registry=container.coordinator.registry,
        handlers=container.coordinator.handlers,
        request_builder=container.coordinator._build_agent_request,
    )
    single_task_id = create_review(container, key="http-idem-00000003")
    single_outcome = container.coordinator.run(single_task_id)

    assert single_outcome.status is a2a_outcome.status is ParentTaskStatus.REVIEWED
    with container.session() as session:
        a2a_rules = {row.rule_id for row in CommentStore(session).list_by_task(a2a_task_id)}
        single_rules = {row.rule_id for row in CommentStore(session).list_by_task(single_task_id)}
        a2a_artifacts = {
            row.artifact_type
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == a2a_task_id
        }
        single_artifacts = {
            row.artifact_type
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == single_task_id
        }
    assert a2a_rules == single_rules and a2a_rules
    assert a2a_artifacts == single_artifacts


def test_agent_card_endpoints(stack: A2AStack) -> None:
    client = A2AClient(stack.base_url)
    cards = client.list_cards()
    assert {card["agent_id"] for card in cards} == {
        "review-agent",
        "impact-agent",
        "fix-agent",
        "verify-agent",
    }
    card = client.get_card("impact-agent")
    assert "impact.ast" in card["capabilities"]
    assert card["protocol_versions"] == ["0.1"]


def test_internal_endpoint_rejects_developer(stack: A2AStack) -> None:
    client = A2AClient(stack.base_url, actor_id="dev-1", actor_role="developer")
    with pytest.raises(CodePilotError) as excinfo:
        client.list_cards()
    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED


def test_internal_endpoint_rejects_missing_identity(stack: A2AStack) -> None:
    response = httpx.get(f"{stack.base_url}/internal/a2a/agents", timeout=10)
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"


def test_internal_endpoint_rejects_unknown_protocol_version(stack: A2AStack) -> None:
    response = httpx.get(
        f"{stack.base_url}/internal/a2a/agents",
        headers={**COORDINATOR_HEADERS, "X-A2A-Protocol-Version": "0.9"},
        timeout=10,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "PROTOCOL_VERSION_UNSUPPORTED"


def test_negotiation_rejects_incompatible_protocol_version(stack: A2AStack, monkeypatch) -> None:
    """远端 Card 声明的协议版本不兼容时必须拒绝创建子任务（FR-100）。"""
    import a2a.http_invoker as module

    monkeypatch.setattr(module, "validate_agent_card", lambda _payload: None)

    container = stack.coordinator_container
    invoker: A2AInvoker = container.coordinator.invoker
    client = invoker.client_for("review-agent")
    monkeypatch.setattr(
        client,
        "get_card",
        lambda agent_id: {
            "agent_id": agent_id,
            "card_version": "2.0",
            "protocol_versions": ["0.2"],
            "capabilities": ["review.rules"],
            "endpoint": f"/internal/a2a/agents/{agent_id}/tasks",
        },
    )

    parent_id = create_review(container, key="http-idem-00000004")
    task = example_a2a_task(
        parent_task_id=parent_id, agent_id="review-agent", task_type=ChildTaskType.REVIEW
    )
    with container.session() as session:
        with pytest.raises(CodePilotError) as excinfo:
            asyncio.run(invoker.submit(task, session=session))
        assert excinfo.value.code is ErrorCode.PROTOCOL_VERSION_UNSUPPORTED
        session.rollback()


# ---------------------------------------------------------------------------
# 幂等 / 取消 / SSE
# ---------------------------------------------------------------------------


def test_repeated_submission_is_idempotent(stack: A2AStack) -> None:
    container = stack.coordinator_container
    invoker: A2AInvoker = container.coordinator.invoker
    parent_id = create_review(container, key="http-idem-00000005")
    task = example_a2a_task(
        parent_task_id=parent_id, agent_id="impact-agent", task_type=ChildTaskType.IMPACT
    )
    task = task.model_copy(update={"task_id": new_child_task_id("impact")})

    with container.session() as session:
        first = asyncio.run(invoker.submit(task, session=session))
        session.commit()
    with container.session() as session:
        second = asyncio.run(invoker.submit(task, session=session))
        session.commit()
        children = A2ATaskStore(session).list_by_parent(parent_id)

    assert first.task_id == second.task_id
    assert len(children) == 1
    # 本地跟踪行与远端执行行通过 remote_task_id 关联：服务端分配自己的 Task ID
    assert children[0].remote_task_id and children[0].remote_task_id != children[0].id
    snapshot = A2AClient(stack.base_url).get_task(children[0].remote_task_id)
    assert snapshot["agent_id"] == "impact-agent"


def test_cancel_task_over_http(stack: A2AStack) -> None:
    container = stack.coordinator_container
    parent_id = create_review(container, key="http-idem-00000006")
    # 注入延迟，确保取消发起时任务仍在执行
    stack.set_faults(delay_seconds=4.0)
    submitted = submit_direct(stack, parent_id, idempotency_key="cancel-case-0000001")
    client = A2AClient(stack.base_url)
    canceled = client.cancel_task(submitted["task_id"])
    assert canceled["status"] == str(ChildTaskStatus.CANCELED)
    assert client.get_task(submitted["task_id"])["status"] == str(ChildTaskStatus.CANCELED)
    stack.clear_faults()


def test_sse_events_over_http(stack: A2AStack) -> None:
    container = stack.coordinator_container
    parent_id = create_review(container, key="http-idem-00000007")
    submitted = submit_direct(stack, parent_id, idempotency_key="sse-case-0000000001")
    client = A2AClient(stack.base_url)

    events = []
    for event in client.stream_events(submitted["task_id"], max_seconds=20):
        events.append(event)
        if event.event_type in {"child_task_completed", "child_task_failed"}:
            break
    assert any(event.event_type == "child_task_completed" for event in events), [
        item.event_type for item in events
    ]
    snapshot = client.get_task(submitted["task_id"])
    assert snapshot["status"] == str(ChildTaskStatus.COMPLETED)
    assert snapshot["artifacts"]


def test_sse_disconnect_falls_back_to_polling(stack: A2AStack) -> None:
    stack.set_faults(drop_events=True)
    task_id = create_review(stack.coordinator_container, key="http-idem-00000008")
    outcome = stack.coordinator_container.coordinator.run(task_id)
    assert outcome.status is ParentTaskStatus.REVIEWED, outcome.to_payload()
    with stack.coordinator_container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
    assert all(ChildTaskStatus(row.status) is ChildTaskStatus.COMPLETED for row in children)


# ---------------------------------------------------------------------------
# 故障矩阵
# ---------------------------------------------------------------------------


def test_timeout_retries_once_without_duplicate_side_effects(stack: A2AStack) -> None:
    """可重试错误 → 先对账、最多重试一次，且不得产生重复副作用。"""
    container = stack.coordinator_container
    stack.set_faults(fail_with=str(ErrorCode.TASK_TIMEOUT), agent_id="review-agent", fail_times=1)

    task_id = create_review(container, key="http-idem-00000009")
    outcome = container.coordinator.run(task_id)

    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
    review_rows = [row for row in children if row.agent_id == "review-agent"]
    assert len(review_rows) == 1, "重试不得创建第二个子任务记录"
    assert review_rows[0].attempt == 2, "受控重试应把 attempt 提升到 2"
    finding_artifacts = [row for row in artifacts if row.artifact_type == str(ArtifactType.FINDING)]
    assert len(finding_artifacts) == 1, "同一产物只允许入库一次（无重复副作用）"
    assert outcome.status is ParentTaskStatus.REVIEWED, outcome.to_payload()


def test_deadline_expiry_converges_to_needs_human(short_stack: A2AStack) -> None:
    """真正的超时（deadline 到点）必须收敛到 NEEDS_HUMAN，且不产生半成品产物。"""
    stack = short_stack
    stack.set_faults(delay_seconds=5.0)
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-00000012")
    outcome = container.coordinator.run(task_id)

    assert outcome.status is ParentTaskStatus.NEEDS_HUMAN, outcome.to_payload()
    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
    assert all(ChildTaskStatus(row.status) is ChildTaskStatus.FAILED for row in children)
    assert all(row.error_code == str(ErrorCode.TASK_TIMEOUT) for row in children)
    assert all(row.attempt <= 2 for row in children), "重试次数不得超过 1 次"
    assert not [row for row in artifacts if row.validated]


def test_tampered_artifact_is_rejected(stack: A2AStack) -> None:
    """篡改内容但不改 content_hash 的 Artifact 必须被拒绝，父任务不得前进。"""
    stack.set_faults(tamper_artifact=True, agent_id="impact-agent")
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-0000000a")
    outcome = container.coordinator.run(task_id)

    assert outcome.status is ParentTaskStatus.NEEDS_HUMAN, outcome.to_payload()
    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
        task = ReviewTaskStore(session).get(task_id)
        events = session.execute(select(AuditEvent)).scalars().all()

    failed = [row for row in children if ChildTaskStatus(row.status) is ChildTaskStatus.FAILED]
    assert failed, "被篡改的产物必须让子任务失败"
    assert failed[0].error_code in {
        str(ErrorCode.ARTIFACT_HASH_MISMATCH),
        str(ErrorCode.ARTIFACT_SCHEMA_INVALID),
    }
    # 被篡改的 ImpactReport 绝不允许以 validated 状态入库
    assert not [
        row
        for row in artifacts
        if row.artifact_type == str(ArtifactType.IMPACT_REPORT) and row.validated
    ]
    assert any(event.event_type == "child_task_failed" for event in events)
    assert task.status == str(ParentTaskStatus.NEEDS_HUMAN)


def test_unknown_status_parks_task(short_stack: A2AStack) -> None:
    stack = short_stack
    stack.set_faults(unknown_status=True)
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-0000000b")
    outcome = container.coordinator.run(task_id)
    assert outcome.status is ParentTaskStatus.NEEDS_HUMAN, outcome.to_payload()


def test_card_capability_missing_refuses_submission(stack: A2AStack) -> None:
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-0000000c")
    card = stack.agent_container.registry.get("impact-agent")
    weakened = card.model_copy(update={"capabilities": ["impact.symbols"], "card_version": "1.1"})
    stack.agent_container.registry._cards["impact-agent"] = weakened

    outcome = container.coordinator.run(task_id)
    assert outcome.status is ParentTaskStatus.NEEDS_HUMAN
    assert outcome.error_code == str(ErrorCode.CAPABILITY_NOT_AVAILABLE)


def test_expired_deadline_is_not_executed(stack: A2AStack) -> None:
    """超过 deadline 的子任务不得被 Agent 服务执行。"""
    container = stack.coordinator_container
    parent_id = create_review(container, key="http-idem-0000000d")
    submitted = submit_direct(
        stack, parent_id, idempotency_key="expired-case-0000001", deadline_delta=-5
    )
    client = A2AClient(stack.base_url)
    snapshot = _wait_terminal(client, submitted["task_id"], timeout=20)
    assert snapshot["status"] == str(ChildTaskStatus.FAILED)
    assert snapshot["error"]["code"] == str(ErrorCode.TASK_TIMEOUT)


def _wait_terminal(client: A2AClient, task_id: str, *, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    snapshot: dict = {}
    while time.time() < deadline:
        snapshot = client.get_task(task_id)
        if snapshot["status"] in {
            str(ChildTaskStatus.COMPLETED),
            str(ChildTaskStatus.FAILED),
            str(ChildTaskStatus.CANCELED),
        }:
            return snapshot
        time.sleep(0.1)
    return snapshot


def test_restart_recovery_with_http_transport_dedupes(stack: A2AStack) -> None:
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-0000000e")
    container.coordinator.run(task_id)
    with container.session() as session:
        before_artifacts = [
            row.id
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
        before_children = [row.id for row in A2ATaskStore(session).list_by_parent(task_id)]

    restarted = build_container(url=container.database_url, auto_create=False, config=container.config)
    restarted.content_store = container.content_store
    restarted.coordinator.content_store = container.content_store
    assert attach_http_invoker(restarted, base_url=stack.base_url) == "http"
    restarted.coordinator.recover()

    with restarted.session() as session:
        after_artifacts = [
            row.id
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
        after_children = [row.id for row in A2ATaskStore(session).list_by_parent(task_id)]
        task = ReviewTaskStore(session).get(task_id)
    assert after_artifacts == before_artifacts
    assert after_children == before_children
    assert task.status == str(ParentTaskStatus.REVIEWED)
    if isinstance(restarted.coordinator.invoker, A2AInvoker):
        restarted.coordinator.invoker.close()


def test_fault_endpoint_requires_flag(stack: A2AStack) -> None:
    stack.clear_faults()
    response = httpx.post(
        f"{stack.base_url}/internal/a2a/_faults",
        json={"delay_seconds": 1},
        headers=COORDINATOR_HEADERS,
        timeout=10,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_http_audit_records_negotiation_and_artifacts(stack: A2AStack) -> None:
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-0000000f")
    container.coordinator.run(task_id)
    with container.session() as session:
        events = session.execute(select(AuditEvent)).scalars().all()
    event_types = {event.event_type for event in events}
    assert "agent_card_resolved" in event_types
    assert "artifact_received" in event_types
    remote = [
        event
        for event in events
        if event.event_type == "agent_card_resolved" and event.after_state.get("source") == "remote"
    ]
    assert remote, "必须记录远程 Card 协商结果"


def test_trace_and_idempotency_propagate_over_http(stack: A2AStack) -> None:
    container = stack.coordinator_container
    task_id = create_review(container, key="http-idem-00000010")
    container.coordinator.run(task_id)
    with container.session() as session:
        parent = ReviewTaskStore(session).get(task_id)
        children = list(session.execute(select(A2ATaskRow)).scalars())
    assert {row.trace_id for row in children} == {parent.trace_id}
    assert all(row.idempotency_key and row.correlation_id for row in children)


def test_child_task_payload_satisfies_schema_on_submit(stack: A2AStack) -> None:
    """内部接口必须拒绝非法 Task 载荷（保留完整 Schema 校验）。"""
    container = stack.coordinator_container
    parent_id = create_review(container, key="http-idem-00000011")
    payload = A2ATask.model_validate(
        example_a2a_task(
            parent_task_id=parent_id, agent_id="review-agent", task_type=ChildTaskType.REVIEW
        ).model_dump(mode="json")
    ).model_dump(mode="json")
    payload["protocol_version"] = "0.9"

    response = httpx.post(
        f"{stack.base_url}/internal/a2a/agents/review-agent/tasks",
        json=payload,
        headers={
            **COORDINATOR_HEADERS,
            "X-A2A-Protocol-Version": "0.1",
            "X-Trace-Id": payload["trace_id"],
            "X-Correlation-Id": payload["correlation_id"],
            "Idempotency-Key": payload["idempotency_key"],
        },
        timeout=10,
    )
    assert response.status_code in {400, 422}
    assert response.json()["code"] in {"ARTIFACT_SCHEMA_INVALID", "PROTOCOL_VERSION_UNSUPPORTED"}
