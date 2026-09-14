"""InProcessInvoker：进程内 Agent 调用适配器（docs/05 §8、FR-099）。

与 ``A2AInvoker`` 使用完全相同的接口、Task 生命周期、Artifact 校验和安全门禁，
只是把传输替换为进程内函数调用。这样 single 与 a2a 的评测才可比。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from a2a.invoker import AgentInvoker, ChildTaskOutcome
from a2a.protocol import A2AError, A2AMessage, A2ATask, ArtifactEnvelope, TaskHandle
from a2a.registry import AgentRegistry
from a2a.schema_registry import validate_artifact_envelope, validate_artifact_payload
from agents.base import AgentHandler, AgentRequest
from domain.clock import ensure_utc, utcnow
from domain.enums import ChildTaskStatus
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_message_id
from repositories.store import A2ATaskStore, ArtifactStore
from repositories.store import MessageStore as MessageStoreImpl

RequestBuilder = Callable[[Any, Session], AgentRequest]


#: 兼容别名：进程内执行结果与 HTTP A2A 传输结果共用同一结构。
ExecutionOutcome = ChildTaskOutcome


class InProcessInvoker(AgentInvoker):
    """进程内调用器：仍然创建 Task、校验 Card、记录 Message 与 Artifact。"""

    mode = "inprocess"

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        handlers: dict[str, AgentHandler],
        request_builder: RequestBuilder,
    ) -> None:
        self.registry = registry
        self.handlers = handlers
        self.request_builder = request_builder

    # ---- AgentInvoker 接口 -------------------------------------------------------
    async def submit(self, task: A2ATask, *, session: Session) -> TaskHandle:
        card = self.registry.resolve(
            task_type=task.task_type,
            protocol_version=task.protocol_version,
        )
        if card.agent_id != task.agent_id:
            raise CodePilotError(
                ErrorCode.PARENT_TASK_MISMATCH,
                f"任务声明的 agent_id={task.agent_id} 与 Card 路由结果 {card.agent_id} 不一致",
                details={"declared": task.agent_id, "routed": card.agent_id},
            )
        row, _created = A2ATaskStore(session).submit(task, transport=self.mode, owner=task.agent_id)
        MessageStoreImpl(session).append(
            A2AMessage(
                message_id=new_message_id(),
                task_id=row.id,
                type="task.submitted",
                role="coordinator",
                correlation_id=row.correlation_id,
                artifact_refs=list(row.input_artifacts),
                error=None,
                created_at=utcnow(),
            ),
            parent_task_id=row.parent_task_id,
            trace_id=row.trace_id,
            payload_summary={"agent_id": row.agent_id, "task_type": row.task_type},
        )
        session.flush()
        return TaskHandle(
            task_id=row.id,
            agent_id=row.agent_id,
            status=ChildTaskStatus(row.status),
            submitted_at=ensure_utc(row.created_at) or utcnow(),
        )

    async def wait(self, handle: TaskHandle, *, session: Session) -> list[ArtifactEnvelope]:
        outcome = await self.execute(handle.task_id, session=session)
        return outcome.artifacts

    async def cancel(self, task_id: str, *, session: Session) -> None:
        store = A2ATaskStore(session)
        row = store.get(task_id)
        assert row is not None
        status = ChildTaskStatus(row.status)
        if status.value in {"completed", "failed", "canceled"}:
            return
        store.transition(
            task_id,
            ChildTaskStatus.CANCELED,
            expected_version=row.state_version,
            idempotency_key=f"{task_id}:cancel",
            actor_id="coordinator",
        )

    async def status(self, task_id: str, *, session: Session) -> str:
        row = A2ATaskStore(session).get(task_id)
        return row.status if row is not None else "unknown"

    # ---- 执行 -------------------------------------------------------------------
    async def execute(self, task_id: str, *, session: Session) -> ExecutionOutcome:
        """执行（或幂等返回）子任务，并完成 Artifact 入库与状态迁移。"""
        store = A2ATaskStore(session)
        row = store.get(task_id)
        if row is None:
            raise CodePilotError(ErrorCode.RESOURCE_NOT_FOUND, f"子任务不存在：{task_id}")

        status = ChildTaskStatus(row.status)
        artifact_store = ArtifactStore(session)

        if status in {ChildTaskStatus.COMPLETED, ChildTaskStatus.FAILED, ChildTaskStatus.CANCELED}:
            artifacts = [
                artifact_store.to_envelope(item) for item in artifact_store.list_by_task(task_id)
            ]
            return ChildTaskOutcome(
                task_id=task_id,
                status=status,
                artifacts=artifacts,
                error_code=row.error_code,
                error_message=row.error_message,
                duration_ms=row.duration_ms or 0,
                replayed=True,
                transport=self.mode,
            )

        started = utcnow()
        if status is ChildTaskStatus.SUBMITTED:
            store.transition(
                task_id,
                ChildTaskStatus.WORKING,
                expected_version=row.state_version,
                # 迁移键必须带上迁移前的 state_version：重试（attempt=2）时状态版本已经变化，
                # 否则会被当成"同一命令的重放"而跳过迁移（曾经导致子任务卡在 working）。
                idempotency_key=f"{task_id}:working:v{row.state_version}",
                owner=row.owner,
            )
            row = store.get(task_id)
            assert row is not None

        deadline = ensure_utc(row.deadline)
        if deadline is not None and utcnow() >= deadline:
            return self._fail(
                session, task_id, ErrorCode.TASK_TIMEOUT, "子任务在开始执行前已超过 deadline"
            )

        handler = self.handlers.get(row.agent_id)
        if handler is None:
            return self._fail(
                session, task_id, ErrorCode.AGENT_UNAVAILABLE, f"没有注册 Agent handler：{row.agent_id}"
            )

        try:
            request = self.request_builder(row, session)
        except CodePilotError as exc:
            return self._fail(session, task_id, exc.code, exc.message)

        try:
            result = handler.handle(request)
        except CodePilotError as exc:
            return self._fail(session, task_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - Agent 内部异常统一转 INTERNAL_ERROR
            return self._fail(session, task_id, ErrorCode.INTERNAL_ERROR, f"Agent 执行异常：{exc}")

        duration_ms = int((utcnow() - started).total_seconds() * 1000)
        if deadline is not None and utcnow() >= deadline:
            return self._fail(
                session,
                task_id,
                ErrorCode.TASK_TIMEOUT,
                "子任务执行超过 deadline",
                duration_ms=duration_ms,
            )

        envelopes, error = self._validate_artifacts(row, result)
        if error is not None:
            return self._fail(session, task_id, error.code, error.message, duration_ms=duration_ms)

        for envelope in envelopes:
            artifact_store.save(
                envelope,
                parent_task_id=row.parent_task_id,
                agent_id=row.agent_id,
                validated=True,
                trace_id=row.trace_id,
            )
        message_store = MessageStoreImpl(session)
        for message in result.messages:
            message_store.append(
                message, parent_task_id=row.parent_task_id, trace_id=row.trace_id
            )

        store.transition(
            task_id,
            ChildTaskStatus.COMPLETED,
            expected_version=row.state_version,
            # 同样按 state_version 区分：重试后成功的完成事件不得被当成首次完成的重放。
            idempotency_key=f"{task_id}:completed:v{row.state_version}",
            owner=row.agent_id,
            duration_ms=duration_ms,
            result_ref=",".join(envelope.artifact_id for envelope in envelopes) or None,
            actor_id=row.agent_id,
        )
        return ChildTaskOutcome(
            task_id=task_id,
            status=ChildTaskStatus.COMPLETED,
            artifacts=envelopes,
            duration_ms=duration_ms,
            transport=self.mode,
        )

    def _validate_artifacts(self, row, result) -> tuple[list[ArtifactEnvelope], CodePilotError | None]:
        """校验必需产物类型、父子关系、Schema 与内容哈希（FR-093 / FR-094）。"""
        policy = self.registry.policy(row.agent_id)
        envelopes: list[ArtifactEnvelope] = []
        produced_types = {str(envelope.artifact_type) for envelope in result.artifacts}

        for required in policy.required_output_types:
            if str(required) not in produced_types:
                return [], CodePilotError(
                    ErrorCode.ARTIFACT_SCHEMA_INVALID,
                    f"Agent {row.agent_id} 未产出必需产物 {required}",
                    details={"required": str(required), "produced": sorted(produced_types)},
                )

        for envelope in result.artifacts:
            if envelope.task_id != row.id:
                return [], CodePilotError(
                    ErrorCode.PARENT_TASK_MISMATCH,
                    "Artifact 的 task_id 与子任务不一致",
                    details={"artifact_task_id": envelope.task_id, "task_id": row.id},
                )
            if policy.output_types and envelope.artifact_type not in policy.output_types:
                return [], CodePilotError(
                    ErrorCode.PERMISSION_DENIED,
                    f"Agent {row.agent_id} 不允许产出 {envelope.artifact_type}",
                    details={"artifact_type": str(envelope.artifact_type)},
                )
            try:
                validate_artifact_envelope(envelope.model_dump(mode="json"))
                envelope.verify_hash()
                validate_artifact_payload(envelope.artifact_type, envelope.data)
            except CodePilotError as exc:
                return [], exc
            envelopes.append(envelope)
        return envelopes, None

    def _fail(
        self,
        session: Session,
        task_id: str,
        code: ErrorCode,
        message: str,
        *,
        duration_ms: int = 0,
    ) -> ExecutionOutcome:
        store = A2ATaskStore(session)
        row = store.get(task_id)
        assert row is not None
        status = ChildTaskStatus(row.status)
        if status not in {ChildTaskStatus.SUBMITTED, ChildTaskStatus.WORKING, ChildTaskStatus.INPUT_REQUIRED}:
            return ExecutionOutcome(task_id=task_id, status=status, artifacts=[], error_code=str(code))

        store.transition(
            task_id,
            ChildTaskStatus.FAILED,
            expected_version=row.state_version,
            # 关键修复：失败事件按 state_version 区分。若沿用固定键
            # ``{task_id}:failed:{code}``，受控重试后的第二次失败会被当成首次失败的重放，
            # 迁移被跳过，子任务永久停留在 working，而父任务已转人工（状态不一致）。
            idempotency_key=f"{task_id}:failed:{code}:v{row.state_version}",
            owner=row.owner,
            error_code=str(code),
            error_message=message,
            duration_ms=duration_ms,
            actor_id=row.agent_id,
        )
        MessageStoreImpl(session).append(
            A2AMessage(
                message_id=new_message_id(),
                task_id=task_id,
                type="task.failed",
                role="agent",
                correlation_id=row.correlation_id,
                artifact_refs=[],
                error=A2AError(code=str(code), message=message),
                created_at=datetime.now(UTC),
            ),
            parent_task_id=row.parent_task_id,
            trace_id=row.trace_id,
        )
        session.flush()
        return ChildTaskOutcome(
            task_id=task_id,
            status=ChildTaskStatus.FAILED,
            artifacts=[],
            error_code=str(code),
            error_message=message,
            duration_ms=duration_ms,
            transport=self.mode,
        )


__all__ = [
    "ChildTaskOutcome",
    "ExecutionOutcome",
    "InProcessInvoker",
    "RequestBuilder",
]
