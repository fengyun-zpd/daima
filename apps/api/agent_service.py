"""Agent 服务端：在独立服务中承载四类 Agent 的 A2A 接口（docs/05 §5）。

职责边界：
- 只接受带 ``X-Actor-Role: coordinator`` / ``admin`` 的内部调用；
- 提交子任务时校验 Card、协议版本、能力与父任务关联；
- 执行在后台线程完成，Artifact 入库前必须通过 Schema 与内容哈希校验；
- 事件走追加式审计表，SSE 可续接。

故障注入（``CODEPILOT_FAULT_INJECTION=1`` 时启用）用于阶段七的故障矩阵：
延迟、丢弃 SSE、篡改 Artifact、返回未知状态等。生产环境默认关闭。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from a2a.invoker import ChildTaskOutcome
from a2a.protocol import A2AError, A2AMessage, A2ATask, ArtifactEnvelope
from a2a.registry import AgentRegistry
from a2a.schema_registry import validate_a2a_task, validate_agent_card
from agents.base import AgentHandler, AgentRequest, ToolGateway
from domain.budget import Budget
from domain.clock import ensure_utc, utcnow
from domain.config import CodePilotConfig
from domain.enums import ChildTaskStatus
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_child_task_id, new_message_id
from domain.llm import LLMGateway
from domain.sanitize import payload_hash
from repositories.content_store import ContentStore
from repositories.store import A2ATaskStore, ArtifactStore, MessageStore, ReviewTaskStore
from sandbox.base import SandboxExecutor, SandboxLimits
from tools.registry import ToolCallContext, ToolRegistry

FAULT_INJECTION_ENABLED = os.environ.get("CODEPILOT_FAULT_INJECTION", "0") == "1"


@dataclass(slots=True)
class FaultInjector:
    """故障注入开关（仅测试/评测使用）。"""

    delay_seconds: float = 0.0
    drop_events: bool = False
    tamper_artifact: bool = False
    unknown_status: bool = False
    fail_with: str | None = None
    fail_times: int = 0
    agent_id: str | None = None
    enabled: bool = FAULT_INJECTION_ENABLED

    def applies_to(self, agent_id: str) -> bool:
        return self.enabled and (self.agent_id is None or self.agent_id == agent_id)

    def apply_delay(self, agent_id: str) -> None:
        if self.applies_to(agent_id) and self.delay_seconds > 0:
            time.sleep(self.delay_seconds)

    def maybe_fail(self, agent_id: str) -> None:
        if not self.applies_to(agent_id) or not self.fail_with:
            return
        if self.fail_times <= 0:
            return
        self.fail_times -= 1
        try:
            code = ErrorCode(self.fail_with)
        except ValueError:
            code = ErrorCode.INTERNAL_ERROR
        raise CodePilotError(code, f"故障注入：{self.fail_with}")

    def tamper(self, agent_id: str, envelopes: list[ArtifactEnvelope]) -> list[ArtifactEnvelope]:
        if not (self.applies_to(agent_id) and self.tamper_artifact) or not envelopes:
            return envelopes
        first = envelopes[0]
        broken = first.model_copy(update={"data": {**first.data, "tampered": True}})
        return [broken, *envelopes[1:]]

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "delay_seconds": self.delay_seconds,
            "drop_events": self.drop_events,
            "tamper_artifact": self.tamper_artifact,
            "unknown_status": self.unknown_status,
            "fail_with": self.fail_with,
            "fail_times": self.fail_times,
            "agent_id": self.agent_id,
        }


@dataclass(slots=True)
class AgentService:
    """Agent 侧的服务实现：与 Coordinator 共用同一套协议与校验。"""

    config: CodePilotConfig
    session_factory: sessionmaker[Session]
    registry: AgentRegistry
    tool_registry: ToolRegistry
    handlers: dict[str, AgentHandler]
    content_store: ContentStore
    sandbox: SandboxExecutor | None = None
    sandbox_limits: SandboxLimits | None = None
    transport: str = "http"
    faults: FaultInjector = field(default_factory=FaultInjector)
    _locks: dict[str, threading.Lock] = field(default_factory=dict)

    # ---- 卡片 -------------------------------------------------------------------
    def healthy_cards(self) -> list[dict[str, Any]]:
        cards = []
        for card in self.registry.list_cards():
            payload = card.model_dump(mode="json")
            validate_agent_card(payload)
            cards.append(payload)
        return cards

    def card(self, agent_id: str) -> dict[str, Any]:
        return self.registry.get(agent_id).model_dump(mode="json")

    # ---- 任务 -------------------------------------------------------------------
    def submit(self, task: A2ATask) -> dict[str, Any]:
        # A2A 语义：Task ID 由服务端分配，客户端本地行通过 remote_task_id 关联，
        # 这样在同一数据库部署（Coordinator 与 Agent 共库）时也不会与本地行冲突。
        assigned_id = new_child_task_id(str(task.task_type))
        task = task.model_copy(update={"task_id": assigned_id})
        payload = task.model_dump(mode="json")
        validate_a2a_task(payload)

        card = self.registry.get(task.agent_id)
        if task.protocol_version not in card.protocol_versions:
            raise CodePilotError(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                f"Agent {task.agent_id} 不支持协议版本 {task.protocol_version}",
                details={"supported": list(card.protocol_versions)},
            )
        if not card.has_capability(self._capability_for(task.agent_id)):
            raise CodePilotError(
                ErrorCode.CAPABILITY_NOT_AVAILABLE,
                f"Agent {task.agent_id} 缺少处理 {task.task_type} 的必需能力",
            )

        session = self.session_factory()
        try:
            parent = ReviewTaskStore(session).get(task.parent_task_id, required=False)
            if parent is None:
                raise CodePilotError(
                    ErrorCode.PARENT_TASK_MISMATCH,
                    f"父任务不存在：{task.parent_task_id}",
                    details={"parent_task_id": task.parent_task_id},
                )
            row, created = A2ATaskStore(session).submit(
                task, transport=self.transport, owner=task.agent_id, side="agent"
            )
            if created:
                MessageStore(session).append(
                    A2AMessage(
                        message_id=new_message_id(),
                        task_id=row.id,
                        type="task.accepted",
                        role="agent",
                        correlation_id=row.correlation_id,
                        artifact_refs=[],
                        error=None,
                        created_at=utcnow(),
                    ),
                    parent_task_id=row.parent_task_id,
                    trace_id=row.trace_id,
                )
            session.commit()
            return {
                "task_id": row.id,
                "status": row.status,
                "created": created,
                "agent_id": row.agent_id,
                "protocol_version": row.protocol_version,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def snapshot(self, task_id: str) -> dict[str, Any]:
        if self.faults.unknown_status and self.faults.enabled:
            raise CodePilotError(ErrorCode.TASK_STATUS_UNKNOWN, "故障注入：状态不可查询")
        session = self.session_factory()
        try:
            store = A2ATaskStore(session)
            row = store.get(task_id)
            artifacts = ArtifactStore(session).list_by_task(task_id)
            return {
                "task_id": row.id,
                "parent_task_id": row.parent_task_id,
                "agent_id": row.agent_id,
                "task_type": row.task_type,
                "status": row.status,
                "attempt": row.attempt,
                "state_version": row.state_version,
                "protocol_version": row.protocol_version,
                "correlation_id": row.correlation_id,
                "deadline": ensure_utc(row.deadline).isoformat(),
                "error": (
                    {"code": row.error_code, "message": row.error_message} if row.error_code else None
                ),
                "artifacts": [
                    {
                        "artifact_id": item.id,
                        "task_id": item.task_id,
                        "artifact_type": item.artifact_type,
                        "schema_version": item.schema_version,
                        "content_hash": item.content_hash,
                        "size_bytes": item.size_bytes,
                        "data": item.data,
                        "validated": item.validated,
                    }
                    for item in artifacts
                    if item.validated
                ],
            }
        finally:
            session.close()

    def cancel(self, task_id: str) -> dict[str, Any]:
        session = self.session_factory()
        try:
            store = A2ATaskStore(session)
            row = store.get(task_id)
            if ChildTaskStatus(row.status) in {
                ChildTaskStatus.COMPLETED,
                ChildTaskStatus.FAILED,
                ChildTaskStatus.CANCELED,
            }:
                return {"task_id": row.id, "status": row.status}
            store.transition(
                task_id,
                ChildTaskStatus.CANCELED,
                expected_version=row.state_version,
                idempotency_key=f"{task_id}:canceled",
                actor_id=row.agent_id,
            )
            session.commit()
            return {"task_id": task_id, "status": str(ChildTaskStatus.CANCELED)}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- 执行 -------------------------------------------------------------------
    def execute(self, task_id: str) -> ChildTaskOutcome:
        """在调用线程内执行子任务；由后台调度器负责并发控制。"""
        lock = self._locks.setdefault(task_id, threading.Lock())
        if not lock.acquire(blocking=False):
            # 同一子任务不允许并发执行（避免重复副作用）。
            session = self.session_factory()
            try:
                row = A2ATaskStore(session).get(task_id)
                return ChildTaskOutcome(
                    task_id=task_id,
                    status=ChildTaskStatus(row.status),
                    transport=self.transport,
                    replayed=True,
                )
            finally:
                session.close()
        try:
            return self._execute_locked(task_id)
        finally:
            lock.release()
            self._locks.pop(task_id, None)

    def _execute_locked(self, task_id: str) -> ChildTaskOutcome:
        session = self.session_factory()
        started = time.monotonic()
        try:
            store = A2ATaskStore(session)
            row = store.get(task_id)
            status = ChildTaskStatus(row.status)
            artifact_store = ArtifactStore(session)
            if status in {
                ChildTaskStatus.COMPLETED,
                ChildTaskStatus.FAILED,
                ChildTaskStatus.CANCELED,
            }:
                return ChildTaskOutcome(
                    task_id=task_id,
                    status=status,
                    artifacts=[
                        artifact_store.to_envelope(item) for item in artifact_store.list_by_task(task_id)
                    ],
                    transport=self.transport,
                    replayed=True,
                )

            if status is ChildTaskStatus.SUBMITTED:
                store.transition(
                    task_id,
                    ChildTaskStatus.WORKING,
                    expected_version=row.state_version,
                    # 按 state_version 区分，避免重试时被当成重放而跳过迁移（见 inprocess.py 同处注释）。
                    idempotency_key=f"{task_id}:working:v{row.state_version}",
                    owner=row.agent_id,
                )
                session.commit()
                row = store.get(task_id)

            handler = self.handlers.get(row.agent_id)
            if handler is None:
                return self._fail(session, task_id, ErrorCode.AGENT_UNAVAILABLE, "Agent handler 未注册")

            deadline = ensure_utc(row.deadline)
            if deadline is not None and utcnow() >= deadline:
                return self._fail(
                    session, task_id, ErrorCode.TASK_TIMEOUT, "子任务在开始执行前已超过 deadline"
                )

            request = self._build_request(session, row)
            try:
                self.faults.apply_delay(row.agent_id)
                self.faults.maybe_fail(row.agent_id)
                result = handler.handle(request)
            except CodePilotError as exc:
                return self._fail(session, task_id, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - Agent 内部异常
                return self._fail(session, task_id, ErrorCode.INTERNAL_ERROR, f"Agent 执行异常：{exc}")

            envelopes = self.faults.tamper(row.agent_id, list(result.artifacts))
            duration_ms = int((time.monotonic() - started) * 1000)
            persisted: list[ArtifactEnvelope] = []
            for envelope in envelopes:
                try:
                    from a2a.schema_registry import (
                        validate_artifact_envelope,
                        validate_artifact_payload,
                    )

                    validate_artifact_envelope(envelope.model_dump(mode="json"))
                    validate_artifact_payload(envelope.artifact_type, envelope.data)
                    envelope.verify_hash()
                except CodePilotError as exc:
                    return self._fail(session, task_id, exc.code, exc.message)
                artifact_store.save(
                    envelope,
                    parent_task_id=row.parent_task_id,
                    agent_id=row.agent_id,
                    validated=True,
                    trace_id=row.trace_id,
                )
                persisted.append(envelope)

            message_store = MessageStore(session)
            for message in result.messages:
                message_store.append(
                    message, parent_task_id=row.parent_task_id, trace_id=row.trace_id
                )

            current = store.get(task_id)
            store.transition(
                task_id,
                ChildTaskStatus.COMPLETED,
                expected_version=current.state_version,
                idempotency_key=f"{task_id}:completed:v{current.state_version}",
                owner=row.agent_id,
                duration_ms=duration_ms,
                result_ref=",".join(item.artifact_id for item in persisted) or None,
                actor_id=row.agent_id,
            )
            session.commit()
            return ChildTaskOutcome(
                task_id=task_id,
                status=ChildTaskStatus.COMPLETED,
                artifacts=persisted,
                duration_ms=duration_ms,
                transport=self.transport,
            )
        except CodePilotError as exc:
            session.rollback()
            return self._fail(session, task_id, exc.code, exc.message)
        finally:
            session.close()

    # ---- 内部 -------------------------------------------------------------------
    def _capability_for(self, agent_id: str) -> str:
        from a2a.registry import AGENT_POLICIES

        policy = AGENT_POLICIES.get(agent_id)
        return policy.required_capabilities[0] if policy else ""

    def _task_payload(self, row: Any) -> dict[str, Any]:
        return {
            "task_id": row.id,
            "parent_task_id": row.parent_task_id,
            "trace_id": row.trace_id,
            "agent_id": row.agent_id,
            "task_type": row.task_type,
            "protocol_version": row.protocol_version,
            "status": row.status,
            "input_artifacts": list(row.input_artifacts or []),
            "required_output_types": list(row.required_output_types or []),
            "deadline": ensure_utc(row.deadline).isoformat(),
            "attempt": row.attempt,
            "idempotency_key": row.idempotency_key,
            "correlation_id": row.correlation_id,
            "state_version": row.state_version,
        }

    def _build_request(self, session: Session, row: Any) -> AgentRequest:
        from domain.workspace import Workspace

        payload = self.content_store.load_workspace(row.parent_task_id)
        if payload is None:
            raise CodePilotError(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"工作区快照缺失：{row.parent_task_id}",
                details={"task_id": row.parent_task_id},
            )
        workspace = Workspace.from_payload(payload)
        budget = Budget.from_config(self.config.agent)
        tool_context = ToolCallContext(
            task_id=row.id,
            parent_task_id=row.parent_task_id,
            agent_id=row.agent_id,
            actor_role="agent",
            trace_id=row.trace_id,
            workspace=workspace,
            session=session,
            budget=budget,
            mode=str(self.config.mode),
            sandbox=self.sandbox,
            sandbox_limits=self.sandbox_limits,
        )
        task = A2ATask.model_validate(self._task_payload(row))
        return AgentRequest(
            task=task,
            workspace=workspace,
            tools=ToolGateway(self.tool_registry, tool_context),
            budget=budget,
            config=self.config,
            inputs=[
                artifact
                for artifact_id in (row.input_artifacts or [])
                if (artifact := _load_input_artifact(session, artifact_id)) is not None
            ],
            context={
                "transport": self.transport,
                "base_commit": getattr(
                    ReviewTaskStore(session).get(row.parent_task_id, required=False), "base_commit", ""
                ),
                "target_branch": f"codepilot/{row.parent_task_id}",
            },
            llm=LLMGateway(budget=budget),
            sandbox=self.sandbox,
            sandbox_limits=self.sandbox_limits,
        )

    def mark_execution_failed(self, task_id: str, code: ErrorCode, message: str) -> ChildTaskOutcome:
        """把后台执行异常显式落库（禁止任务静默悬挂在 working）。"""
        session = self.session_factory()
        try:
            return self._fail(session, task_id, code, message)
        finally:
            session.close()

    def _fail(
        self, session: Session, task_id: str, code: ErrorCode, message: str
    ) -> ChildTaskOutcome:
        store = A2ATaskStore(session)
        row = store.get(task_id, required=False)
        if row is None:
            return ChildTaskOutcome(task_id=task_id, status=ChildTaskStatus.FAILED, error_code=str(code))
        current = ChildTaskStatus(row.status)
        if current not in {
            ChildTaskStatus.COMPLETED,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }:
            store.transition(
                task_id,
                ChildTaskStatus.FAILED,
                expected_version=row.state_version,
                idempotency_key=f"{task_id}:failed:{code}:{row.state_version}",
                owner=row.agent_id,
                error_code=str(code),
                error_message=message,
                actor_id=row.agent_id,
            )
            MessageStore(session).append(
                A2AMessage(
                    message_id=new_message_id(),
                    task_id=task_id,
                    type="task.failed",
                    role="agent",
                    correlation_id=row.correlation_id,
                    artifact_refs=[],
                    error=A2AError(code=str(code), message=message),
                    created_at=utcnow(),
                ),
                parent_task_id=row.parent_task_id,
                trace_id=row.trace_id,
            )
            session.commit()
        return ChildTaskOutcome(
            task_id=task_id,
            status=ChildTaskStatus.FAILED,
            error_code=str(code),
            error_message=message,
            transport=self.transport,
        )


class TaskScheduler:
    """后台执行器：限制并发，POST 立即返回 202（NFR-001）。"""

    def __init__(self, service: AgentService, *, max_concurrency: int = 4) -> None:
        self.service = service
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._running: dict[str, asyncio.Task[Any]] = {}

    def schedule(self, task_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 非异步上下文
            self.service.execute(task_id)
            return
        if task_id in self._running and not self._running[task_id].done():
            return
        task = loop.create_task(self._run(task_id))
        self._running[task_id] = task
        task.add_done_callback(lambda _item: self._running.pop(task_id, None))

    async def _run(self, task_id: str) -> None:
        async with self._semaphore:
            try:
                await asyncio.to_thread(self.service.execute, task_id)
            except Exception as exc:  # noqa: BLE001 - 后台执行异常必须落库，不能静默悬挂
                self.service.mark_execution_failed(
                    task_id, ErrorCode.INTERNAL_ERROR, f"后台执行异常：{type(exc).__name__}: {exc}"
                )

    async def wait(self, task_id: str, *, timeout: float = 30.0) -> None:
        task = self._running.get(task_id)
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


def build_agent_service(
    *,
    config: CodePilotConfig,
    session_factory: sessionmaker[Session],
    registry: AgentRegistry,
    tool_registry: ToolRegistry,
    handlers: dict[str, AgentHandler] | None = None,
    content_store: ContentStore | None = None,
    sandbox: SandboxExecutor | None = None,
    sandbox_limits: SandboxLimits | None = None,
    faults: FaultInjector | None = None,
    transport: str = "http",
) -> AgentService:
    # 与单进程 Coordinator 共用同一份注册表：四类 Agent 一个都不能少，
    # 否则拆分形态下 Fix/Verify 子任务会被判定为 AGENT_UNAVAILABLE。
    from agents import default_handlers

    resolved_handlers = handlers or default_handlers()
    return AgentService(
        config=config,
        session_factory=session_factory,
        registry=registry,
        tool_registry=tool_registry,
        handlers=resolved_handlers,
        content_store=content_store or ContentStore(),
        sandbox=sandbox,
        sandbox_limits=sandbox_limits,
        transport=transport,
        faults=faults or FaultInjector(),
    )


__all__ = [
    "FAULT_INJECTION_ENABLED",
    "AgentService",
    "FaultInjector",
    "TaskScheduler",
    "build_agent_service",
]


def payload_digest(payload: dict[str, Any]) -> str:
    """工具函数：用于测试与审计的稳定摘要。"""
    return payload_hash(payload)


def _load_input_artifact(session: Session, artifact_id: str) -> ArtifactEnvelope | None:
    row = ArtifactStore(session).get(artifact_id)
    if row is None or not row.validated:
        return None
    return ArtifactEnvelope.model_validate(
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


HandlerFactory = Callable[[], dict[str, AgentHandler]]
