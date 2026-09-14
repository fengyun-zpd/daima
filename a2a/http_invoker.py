"""A2AInvoker：通过 HTTP/SSE 调用远程 Agent（FR-092/095/096/100、docs/05 §6）。

与 ``InProcessInvoker`` 的差异只在传输：
- 提交、状态、取消、SSE 全部走 ``/internal/a2a/*``；
- 仍复用同一套 Task 生命周期、Artifact Schema/哈希校验与安全门禁（FR-099）。

恢复语义：
- SSE 断线 → 退化为轮询补偿（不丢事件、不重复副作用）；
- 超时 → 先查询远程状态对账，确认未执行或可安全重放时才由 Coordinator 重试一次；
- 未知状态 → ``TASK_STATUS_UNKNOWN``，禁止盲目重放写操作（宪法第七条）。
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from a2a.client import A2AClient, client_from_env
from a2a.invoker import AgentInvoker, ChildTaskOutcome
from a2a.protocol import A2AError, A2AMessage, A2ATask, ArtifactEnvelope, TaskHandle
from a2a.registry import AGENT_POLICIES, AgentRegistry
from a2a.schema_registry import validate_agent_card, validate_artifact_envelope, validate_artifact_payload
from domain.clock import ensure_utc, utcnow
from domain.enums import ArtifactType, ChildTaskStatus
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_message_id
from repositories.audit import record_event
from repositories.store import A2ATaskStore, ArtifactStore, MessageStore

TERMINAL_EVENT_TYPES = frozenset({"child_task_completed", "child_task_failed", "child_task_canceled"})
DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_SSE_WINDOW_SECONDS = 2.0

ClientFactory = Callable[[str], A2AClient]


class A2AInvoker(AgentInvoker):
    """HTTP/SSE 传输实现。"""

    mode = "http"

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        base_url: str | None = None,
        routes: dict[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        sse_window_seconds: float = DEFAULT_SSE_WINDOW_SECONDS,
        use_sse: bool = True,
    ) -> None:
        self.registry = registry
        self.base_url = (base_url or "").rstrip("/")
        self.routes = dict(routes or {})
        self.client_factory = client_factory
        self.poll_interval = poll_interval
        self.sse_window_seconds = sse_window_seconds
        self.use_sse = use_sse
        self._clients: dict[str, A2AClient] = {}
        self._last_degraded: str | None = None

    # ---- 客户端 -----------------------------------------------------------------
    def client_for(self, agent_id: str) -> A2AClient:
        if agent_id in self._clients:
            return self._clients[agent_id]
        if self.client_factory is not None:
            client = self.client_factory(agent_id)
        else:
            url = self.routes.get(agent_id) or self.base_url
            if not url:
                raise CodePilotError(
                    ErrorCode.AGENT_UNAVAILABLE,
                    f"未配置 Agent {agent_id} 的 A2A 端点",
                    details={"agent_id": agent_id, "hint": "设置 CODEPILOT_A2A_BASE_URL 或 CODEPILOT_A2A_ROUTES"},
                )
            client = A2AClient(url)
        self._clients[agent_id] = client
        return client

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ---- AgentInvoker 接口 -------------------------------------------------------
    async def submit(
        self, task: A2ATask, *, session: Session, force_new_remote: bool = False
    ) -> TaskHandle:
        store = A2ATaskStore(session)
        card = self.registry.resolve(task_type=task.task_type, protocol_version=task.protocol_version)
        if card.agent_id != task.agent_id:
            raise CodePilotError(
                ErrorCode.PARENT_TASK_MISMATCH,
                f"任务声明的 agent_id={task.agent_id} 与 Card 路由结果 {card.agent_id} 不一致",
                details={"declared": task.agent_id, "routed": card.agent_id},
            )

        row, created = store.submit(task, transport=self.mode, owner=task.agent_id)
        if ChildTaskStatus(row.status) in {
            ChildTaskStatus.COMPLETED,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }:
            # 幂等：终态子任务直接返回，不重复调用远程。
            return self._handle(row)

        if row.remote_task_id and not force_new_remote:
            # 同一 attempt 已提交过：复用远程任务，避免重复执行。
            return self._handle(row)

        self._negotiate(row, session)
        client = self.client_for(row.agent_id)
        payload = self._task_payload(row)
        # 远程调用前先落地本地意图并提交：跨进程 HTTP 期间不能持有数据库写事务，
        # 否则远端写入需要等待本地事务，形成长阻塞（SQLite 单写者会直接超时）。
        session.commit()
        try:
            response = client.submit_task(payload)
        except CodePilotError as exc:
            if created:
                self._mark_failed(session, row, exc.code, exc.message)
                session.commit()
            raise

        remote_status = str(response.get("status", "submitted"))
        current = store.get(row.id)
        assert current is not None
        current.remote_task_id = str(response.get("task_id") or row.id)
        self._apply_remote_status(session, row, remote_status)
        self._append_message(
            session,
            row,
            message_type="task.submitted",
            role="coordinator",
            error=None,
        )
        session.commit()
        refreshed = store.get(row.id)
        assert refreshed is not None
        return self._handle(refreshed)

    def _handle(self, row: Any) -> TaskHandle:
        return TaskHandle(
            task_id=row.id,
            agent_id=row.agent_id,
            status=ChildTaskStatus(row.status),
            submitted_at=ensure_utc(row.created_at) or utcnow(),
        )

    @staticmethod
    def _remote_id(row: Any) -> str:
        return str(getattr(row, "remote_task_id", None) or row.id)

    async def wait(self, handle: TaskHandle, *, session: Session) -> list[ArtifactEnvelope]:
        outcome = await self.execute(handle.task_id, session=session)
        return outcome.artifacts

    async def execute(self, task_id: str, *, session: Session) -> ChildTaskOutcome:
        store = A2ATaskStore(session)
        row = store.get(task_id)
        if row is None:
            raise CodePilotError(ErrorCode.RESOURCE_NOT_FOUND, f"子任务不存在：{task_id}")

        status = ChildTaskStatus(row.status)
        artifact_store = ArtifactStore(session)
        if status in {ChildTaskStatus.COMPLETED, ChildTaskStatus.FAILED, ChildTaskStatus.CANCELED}:
            artifacts = [artifact_store.to_envelope(item) for item in artifact_store.list_by_task(task_id)]
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

        if status is ChildTaskStatus.SUBMITTED:
            # 提交（幂等键命中时返回历史 Task）。
            handle = await self.submit(self._row_to_task(row), session=session)
            row = store.get(handle.task_id)
            assert row is not None
        elif row.attempt > 1:
            # 受控重试：远程任务已终态失败，用新的幂等键重新提交（宪法第七条）。
            client = self.client_for(row.agent_id)
            snapshot = self._safe_snapshot(client, self._remote_id(row))
            remote_status = str((snapshot or {}).get("status", ""))
            if snapshot is None or remote_status in {
                str(ChildTaskStatus.FAILED),
                str(ChildTaskStatus.CANCELED),
                str(ChildTaskStatus.COMPLETED),
            }:
                await self.submit(
                    self._row_to_task(row), session=session, force_new_remote=True
                )
                refreshed = store.get(row.id)
                assert refreshed is not None
                row = refreshed

        return await self._await_terminal(session, row)

    async def cancel(self, task_id: str, *, session: Session) -> None:
        store = A2ATaskStore(session)
        row = store.get(task_id)
        assert row is not None
        if ChildTaskStatus(row.status) in {
            ChildTaskStatus.COMPLETED,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }:
            return
        client = self.client_for(row.agent_id)
        try:
            # 取消的幂等键由子任务自身幂等键派生：同一子任务的重复取消返回原结果。
            client.cancel_task(
                self._remote_id(row), idempotency_key=_cancel_key(row.idempotency_key, row.id)
            )
        except CodePilotError as exc:
            # 取消失败不改变本地终态判断：记录后仍需靠状态对账收敛。
            record_event(
                session,
                actor_id="coordinator",
                actor_role="coordinator",
                action="cancel_child_task",
                entity_type="a2a_task",
                entity_id=task_id,
                event_type="child_task_cancel_failed",
                trace_id=row.trace_id,
                task_id=task_id,
                parent_task_id=row.parent_task_id,
                agent_id=row.agent_id,
                error_code=str(exc.code),
                after_state={"message": exc.message},
            )
            raise
        store.transition(
            task_id,
            ChildTaskStatus.CANCELED,
            expected_version=row.state_version,
            idempotency_key=f"{task_id}:cancel",
            actor_id="coordinator",
        )

    async def status(self, task_id: str, *, session: Session) -> str:
        store = A2ATaskStore(session)
        row = store.get(task_id, required=False)
        if row is None:
            return "unknown"
        client = self.client_for(row.agent_id)
        try:
            snapshot = client.get_task(self._remote_id(row))
        except CodePilotError:
            return "unknown"
        remote_status = str(snapshot.get("status", ""))
        if remote_status and remote_status != row.status:
            self._apply_remote_status(session, row, remote_status)
        return remote_status or "unknown"

    # ---- 等待与收敛 ---------------------------------------------------------------
    async def _await_terminal(self, session: Session, row: Any) -> ChildTaskOutcome:
        client = self.client_for(row.agent_id)
        deadline = ensure_utc(row.deadline) or utcnow()
        last_event_id: str | None = None
        started = time.monotonic()
        degraded: str | None = None

        while True:
            remaining = (deadline - utcnow()).total_seconds()
            if remaining <= 0:
                return self._on_deadline(session, row, client, started, degraded, last_event_id)

            if self.use_sse:
                try:
                    window = min(self.sse_window_seconds, max(remaining, 0.05))
                    for event in client.stream_events(
                        self._remote_id(row), last_event_id=last_event_id, max_seconds=window
                    ):
                        last_event_id = event.event_id or last_event_id
                        if event.event_type in TERMINAL_EVENT_TYPES:
                            break
                except CodePilotError as exc:
                    degraded = f"sse_failed:{exc.code}"
                except Exception as exc:  # noqa: BLE001 - 传输层异常统一降级为轮询
                    degraded = f"sse_failed:{type(exc).__name__}"

            snapshot = self._safe_snapshot(client, self._remote_id(row))
            if snapshot is None:
                # 状态未知：等待下一轮，绝不盲目重放（宪法第七条）。
                if (utcnow() - deadline).total_seconds() > 0:
                    return self._fail(
                        session,
                        row,
                        ErrorCode.TASK_STATUS_UNKNOWN,
                        "无法查询远程子任务状态",
                        started=started,
                        degraded=degraded,
                    )
                self._sleep()
                continue

            status = str(snapshot.get("status", ""))
            if status in {str(item) for item in ChildTaskStatus} and status in {
                str(ChildTaskStatus.COMPLETED),
                str(ChildTaskStatus.FAILED),
                str(ChildTaskStatus.CANCELED),
            }:
                return self._finalize(session, row, snapshot, started=started, degraded=degraded)
            if status and status != row.status:
                self._apply_remote_status(session, row, status)
            self._sleep()

    def _on_deadline(
        self,
        session: Session,
        row: Any,
        client: A2AClient,
        started: float,
        degraded: str | None,
        last_event_id: str | None,
    ) -> ChildTaskOutcome:
        """超时先对账：确认未执行 → TASK_TIMEOUT（可重试）；无法确认 → TASK_STATUS_UNKNOWN。"""
        snapshot = self._safe_snapshot(client, self._remote_id(row))
        if snapshot is None:
            return self._fail(
                session,
                row,
                ErrorCode.TASK_STATUS_UNKNOWN,
                "子任务超时且无法查询远程状态，转人工",
                started=started,
                degraded=degraded,
            )
        status = str(snapshot.get("status", ""))
        if status in {
            str(ChildTaskStatus.COMPLETED),
            str(ChildTaskStatus.FAILED),
            str(ChildTaskStatus.CANCELED),
        }:
            return self._finalize(session, row, snapshot, started=started, degraded=degraded)
        return self._fail(
            session,
            row,
            ErrorCode.TASK_TIMEOUT,
            f"子任务超过 deadline（远程状态 {status or 'unknown'}）",
            started=started,
            degraded=degraded,
        )

    def _ensure_status(
        self,
        session: Session,
        row: Any,
        target: ChildTaskStatus,
        *,
        key_suffix: str,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
        result_ref: str | None = None,
    ) -> Any:
        """把本地跟踪行推进到目标状态，必要时补走 submitted → working 的合法边。

        远程 Agent 可能在一次轮询窗口内直接从 submitted 进入终态，
        这里必须沿合法迁移链推进，而不是跳态（宪法第四条）。
        """
        from domain.state_machine import can_transition_child

        store = A2ATaskStore(session)
        current = store.get(row.id)
        status = ChildTaskStatus(current.status)
        if status is target:
            return current
        if not can_transition_child(status, target):
            if status is ChildTaskStatus.SUBMITTED and can_transition_child(
                ChildTaskStatus.WORKING, target
            ):
                mid = store.transition(
                    current.id,
                    ChildTaskStatus.WORKING,
                    expected_version=current.state_version,
                    idempotency_key=f"{row.id}:remote:working:{current.state_version}",
                    owner=row.agent_id,
                    actor_id=row.agent_id,
                )
                session.commit()
                current = mid.row
            else:
                raise CodePilotError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    f"本地子任务不允许从 {status} 迁移到 {target}",
                    details={"current": str(status), "target": str(target)},
                )
        return store.transition(
            current.id,
            target,
            expected_version=current.state_version,
            idempotency_key=f"{row.id}:{key_suffix}",
            owner=row.agent_id,
            error_code=error_code,
            error_message=error_message,
            duration_ms=duration_ms,
            result_ref=result_ref,
            actor_id=row.agent_id,
        )

    def _finalize(
        self,
        session: Session,
        row: Any,
        snapshot: dict[str, Any],
        *,
        started: float,
        degraded: str | None,
    ) -> ChildTaskOutcome:
        store = A2ATaskStore(session)
        duration_ms = int((time.monotonic() - started) * 1000)
        status = ChildTaskStatus(str(snapshot["status"]))
        current = store.get(row.id)
        assert current is not None

        if status is ChildTaskStatus.FAILED:
            error = snapshot.get("error") or {}
            return self._fail(
                session,
                row,
                ErrorCode(error.get("code")) if error.get("code") in set(ErrorCode) else ErrorCode.INTERNAL_ERROR,
                str(error.get("message") or "远程 Agent 报告失败"),
                started=started,
                degraded=degraded,
            )
        if status is ChildTaskStatus.CANCELED:
            self._ensure_status(
                session,
                row,
                ChildTaskStatus.CANCELED,
                key_suffix=f"canceled:{current.state_version}",
                duration_ms=duration_ms,
            )
            session.commit()
            return ChildTaskOutcome(
                task_id=row.id,
                status=ChildTaskStatus.CANCELED,
                transport=self.mode,
                duration_ms=duration_ms,
                error_code=str(ErrorCode.TASK_CANCELED),
                error_message="子任务被取消",
                degraded_reason=degraded,
            )

        envelopes, error = self._ingest_artifacts(session, row, snapshot)
        if error is not None:
            return self._fail(
                session,
                row,
                error.code,
                error.message,
                started=started,
                degraded=degraded,
            )

        self._ensure_status(
            session,
            row,
            ChildTaskStatus.COMPLETED,
            key_suffix="completed",
            duration_ms=duration_ms,
            result_ref=",".join(item.artifact_id for item in envelopes) or None,
        )
        self._append_message(session, row, message_type="task.completed", role="agent", error=None)
        session.commit()
        return ChildTaskOutcome(
            task_id=row.id,
            status=ChildTaskStatus.COMPLETED,
            artifacts=envelopes,
            duration_ms=duration_ms,
            transport=self.mode,
            degraded_reason=degraded,
        )

    def _ingest_artifacts(
        self, session: Session, row: Any, snapshot: dict[str, Any]
    ) -> tuple[list[ArtifactEnvelope], CodePilotError | None]:
        """校验远程 Artifact：Schema、内容哈希、父子关系与允许类型（FR-093）。"""
        policy = self.registry.policy(row.agent_id)
        store = ArtifactStore(session)
        envelopes: list[ArtifactEnvelope] = []
        produced = {str(item.get("artifact_type")) for item in snapshot.get("artifacts", [])}

        for required in policy.required_output_types:
            if str(required) not in produced:
                return [], CodePilotError(
                    ErrorCode.ARTIFACT_SCHEMA_INVALID,
                    f"远程 Agent 未返回必需产物 {required}",
                    details={"required": str(required), "produced": sorted(produced)},
                )

        for item in snapshot.get("artifacts", []):
            payload = (
                {key: value for key, value in item.items() if key in ArtifactEnvelope.model_fields}
                if isinstance(item, dict)
                else item
            )
            try:
                envelope = ArtifactEnvelope.model_validate(payload)
                validate_artifact_envelope(envelope.model_dump(mode="json"))
                envelope.verify_hash()
                validate_artifact_payload(envelope.artifact_type, envelope.data)
            except CodePilotError as exc:
                with contextlib.suppress(Exception):
                    store.save(
                        _placeholder_envelope(row, item, policy),
                        parent_task_id=row.parent_task_id,
                        agent_id=row.agent_id,
                        validated=False,
                        validation_error=exc.message,
                        trace_id=row.trace_id,
                    )
                return [], exc
            except ValidationError as exc:
                message = f"Artifact 结构非法：{exc.errors()[0]['msg'] if exc.errors() else exc}"
                with contextlib.suppress(Exception):
                    store.save(
                        _placeholder_envelope(row, item if isinstance(item, dict) else {}, policy),
                        parent_task_id=row.parent_task_id,
                        agent_id=row.agent_id,
                        validated=False,
                        validation_error=message,
                        trace_id=row.trace_id,
                    )
                return [], CodePilotError(ErrorCode.ARTIFACT_SCHEMA_INVALID, message)
            if envelope.task_id not in {row.id, self._remote_id(row)}:
                return [], CodePilotError(
                    ErrorCode.PARENT_TASK_MISMATCH,
                    "远程 Artifact 的 task_id 与子任务不一致",
                    details={
                        "artifact_task_id": envelope.task_id,
                        "task_id": row.id,
                        "remote_task_id": self._remote_id(row),
                    },
                )
            if policy.output_types and envelope.artifact_type not in policy.output_types:
                return [], CodePilotError(
                    ErrorCode.PERMISSION_DENIED,
                    f"Agent {row.agent_id} 不允许产出 {envelope.artifact_type}",
                    details={"artifact_type": str(envelope.artifact_type)},
                )
            # 共享数据库部署时产物已由 Agent 侧落库（task_id 为远程 ID），此处只做校验；
            # 独立部署时本地补写一份，task_id 改写为本地区任务 ID，保证父任务视图唯一。
            existing = store.find_by_content(
                task_id=envelope.task_id,
                artifact_type=str(envelope.artifact_type),
                content_hash=envelope.content_hash,
            )
            if existing is None:
                store.save(
                    envelope.model_copy(update={"task_id": row.id}),
                    parent_task_id=row.parent_task_id,
                    agent_id=row.agent_id,
                    validated=True,
                    trace_id=row.trace_id,
                )
            envelopes.append(envelope)
        return envelopes, None

    # ---- 辅助 -------------------------------------------------------------------
    def _negotiate(self, row: Any, session: Session) -> dict[str, Any]:
        """远程 Agent Card 协商：Schema、协议版本与能力校验（FR-091/FR-100）。"""
        client = self.client_for(row.agent_id)
        policy = AGENT_POLICIES[row.agent_id]
        try:
            raw_card = client.get_card(row.agent_id)
        except CodePilotError as exc:
            raise CodePilotError(
                ErrorCode.AGENT_UNAVAILABLE,
                f"无法获取 Agent {row.agent_id} 的 Card：{exc.message}",
                details={"agent_id": row.agent_id},
            ) from exc

        validate_agent_card(raw_card)
        versions = list(raw_card.get("protocol_versions", []))
        if row.protocol_version not in versions:
            raise CodePilotError(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                f"远程 Agent {row.agent_id} 不支持协议版本 {row.protocol_version}",
                details={"supported": versions, "requested": row.protocol_version},
            )
        declared = set(raw_card.get("capabilities", []))
        missing = [item for item in policy.required_capabilities if item not in declared]
        if missing:
            raise CodePilotError(
                ErrorCode.CAPABILITY_NOT_AVAILABLE,
                f"远程 Agent {row.agent_id} 缺少必需能力：{missing}",
                details={"missing": missing, "declared": sorted(declared)},
            )

        record_event(
            session,
            actor_id="coordinator",
            actor_role="coordinator",
            action="negotiate_agent_card",
            entity_type="a2a_task",
            entity_id=row.id,
            event_type="agent_card_resolved",
            trace_id=row.trace_id,
            task_id=row.id,
            parent_task_id=row.parent_task_id,
            agent_id=row.agent_id,
            after_state={
                "source": "remote",
                "card_version": raw_card.get("card_version"),
                "protocol_versions": versions,
                "capabilities": sorted(declared),
                "endpoint": raw_card.get("endpoint"),
                "status": raw_card.get("status"),
            },
        )
        return raw_card

    def _task_payload(self, row: Any) -> dict[str, Any]:
        task = self._row_to_task(row)
        payload = task.model_dump(mode="json")
        if row.attempt > 1:
            # 重试使用新的幂等键，避免命中远程的历史失败结果（宪法第七条）。
            payload["idempotency_key"] = f"{row.idempotency_key}:r{row.attempt}"
        return payload

    @staticmethod
    def _row_to_task(row: Any) -> A2ATask:
        return A2ATask(
            task_id=row.id,
            parent_task_id=row.parent_task_id,
            trace_id=row.trace_id,
            agent_id=row.agent_id,
            task_type=row.task_type,
            protocol_version=row.protocol_version,
            status=ChildTaskStatus(row.status),
            input_artifacts=list(row.input_artifacts or []),
            required_output_types=list(row.required_output_types or []),
            deadline=ensure_utc(row.deadline) or utcnow(),
            attempt=row.attempt,
            idempotency_key=row.idempotency_key,
            correlation_id=row.correlation_id,
            state_version=row.state_version,
        )

    def _apply_remote_status(self, session: Session, row: Any, remote_status: str) -> None:
        try:
            target = ChildTaskStatus(remote_status)
        except ValueError:
            return
        store = A2ATaskStore(session)
        current = store.get(row.id)
        if current is None or ChildTaskStatus(current.status) in {
            ChildTaskStatus.COMPLETED,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }:
            return
        if target is ChildTaskStatus.SUBMITTED or ChildTaskStatus(current.status) is target:
            return
        from domain.state_machine import can_transition_child

        if not can_transition_child(ChildTaskStatus(current.status), target):
            return
        store.transition(
            row.id,
            target,
            expected_version=current.state_version,
            idempotency_key=f"{row.id}:remote:{target}:{current.state_version}",
            owner=row.agent_id,
            actor_id=row.agent_id,
        )
        # 立即提交：轮询间隔内不得持有写事务（否则远端写入会阻塞）。
        session.commit()

    def _mark_failed(
        self, session: Session, row: Any, code: ErrorCode, message: str
    ) -> ChildTaskOutcome:
        return self._fail(session, row, code, message, started=time.monotonic(), degraded=None)

    def _fail(
        self,
        session: Session,
        row: Any,
        code: ErrorCode,
        message: str,
        *,
        started: float,
        degraded: str | None,
        duration_ms: int | None = None,
    ) -> ChildTaskOutcome:
        store = A2ATaskStore(session)
        current = store.get(row.id)
        elapsed = duration_ms if duration_ms is not None else int((time.monotonic() - started) * 1000)
        if current is not None and ChildTaskStatus(current.status) not in {
            ChildTaskStatus.COMPLETED,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }:
            self._ensure_status(
                session,
                row,
                ChildTaskStatus.FAILED,
                key_suffix=f"failed:{code}:{current.state_version}",
                error_code=str(code),
                error_message=message,
                duration_ms=elapsed,
            )
            self._append_message(
                session,
                row,
                message_type="task.failed",
                role="agent",
                error=A2AError(code=str(code), message=message),
            )
            session.commit()
        return ChildTaskOutcome(
            task_id=row.id,
            status=ChildTaskStatus.FAILED,
            error_code=str(code),
            error_message=message,
            duration_ms=elapsed,
            transport=self.mode,
            degraded_reason=degraded,
        )

    def _append_message(
        self,
        session: Session,
        row: Any,
        *,
        message_type: str,
        role: str,
        error: A2AError | None,
    ) -> None:
        MessageStore(session).append(
            A2AMessage(
                message_id=new_message_id(),
                task_id=row.id,
                type=message_type,
                role=role,
                correlation_id=row.correlation_id,
                artifact_refs=[],
                error=error,
                created_at=utcnow(),
            ),
            parent_task_id=row.parent_task_id,
            trace_id=row.trace_id,
        )

    def _safe_snapshot(self, client: A2AClient, task_id: str) -> dict[str, Any] | None:
        try:
            return client.get_task(task_id)
        except CodePilotError:
            return None
        except Exception:  # noqa: BLE001 - 网络异常统一视为状态未知
            return None

    def _sleep(self) -> None:
        time.sleep(self.poll_interval)


def _cancel_key(child_idempotency_key: str | None, task_id: str) -> str:
    """取消命令的确定性幂等键：同一子任务的重复取消返回原结果（docs/08 §3.14）。"""
    base = (child_idempotency_key or task_id).strip() or task_id
    return f"{base}:cancel"


def _placeholder_envelope(row: Any, item: dict[str, Any], policy: Any) -> ArtifactEnvelope:
    """非法 Artifact 的占位记录：保留原始摘要以便审计，但标记为未通过校验。

    使用新的 artifact_id，避免与远端已存在的同一产物主键冲突。
    """
    from domain.ids import new_artifact_id
    from domain.sanitize import payload_hash

    expected = policy.required_output_types[0] if policy.required_output_types else ArtifactType.FINDING
    return ArtifactEnvelope(
        artifact_id=new_artifact_id(),
        task_id=row.id,
        artifact_type=expected,
        schema_version="1.0",
        content_hash=payload_hash(item),
        size_bytes=0,
        data={
            "invalid_payload_fields": sorted(str(key) for key in item)[:20],
            "remote_artifact_id": str(item.get("artifact_id") or ""),
        },
    )


def build_invoker(
    *,
    registry: AgentRegistry,
    mode: str,
    base_url: str | None = None,
    routes: dict[str, str] | None = None,
) -> A2AInvoker | None:
    """按运行模式选择 HTTP Invoker；a2a 且未配置端点时返回 ``None``（由调用方降级）。

    降级路径遵循 docs/04 §4：Docker/远程 Agent 不可用时仍可运行规则与报告链路，
    但绝不因此放松任何安全门禁。
    """
    from_env = client_from_env()
    resolved_base = base_url or (from_env.base_url if from_env is not None else "")
    if mode == "a2a" and (resolved_base or routes):
        return A2AInvoker(registry=registry, base_url=resolved_base, routes=routes)
    return None


__all__ = [
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_SSE_WINDOW_SECONDS",
    "TERMINAL_EVENT_TYPES",
    "A2AInvoker",
    "build_invoker",
]
