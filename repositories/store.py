"""仓储层：乐观锁、幂等与追加式审计的唯一入口（FR-003/016/017、FR-096/097）。

设计约束：
- 所有状态写入都必须走 ``apply_intent`` / ``transition_*``，使用
  ``UPDATE ... WHERE id = ? AND state_version = ?`` 原子乐观锁（FR-016）；
- 幂等键命中历史记录时直接返回原结果，绝不产生第二个执行实例或副作用（宪法第七条）；
- 每个副作用都伴随一条追加式审计事件；
- Agent 不能写父任务：owner 校验失败返回 ``PERMISSION_DENIED``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from a2a.protocol import A2AMessage, A2ATask, AgentCard, ArtifactEnvelope
from domain.clock import ensure_utc, utcnow
from domain.enums import (
    ChildTaskStatus,
    ChildTaskType,
    ParentTaskStatus,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import (
    new_artifact_id,
    new_child_task_id,
    new_id,
    new_message_id,
)
from domain.sanitize import payload_hash
from domain.state_machine import assert_child_transition, assert_parent_transition
from repositories.audit import record_event
from repositories.models import (
    A2AAgent,
    A2AArtifact,
    AuditEvent,
    ReviewTask,
)
from repositories.models import (
    A2AMessage as A2AMessageRow,
)
from repositories.models import (
    A2ATask as A2ATaskRow,
)

COORDINATOR_OWNER = "coordinator"

_PARENT_MUTABLE_FIELDS = frozenset(
    {
        "status",
        "current_step",
        "needs_human_from",
        "owner",
        "budget_snapshot",
        "completed",
        "pending",
        "child_tasks",
        "artifacts",
        "fixed_comment_ids",
        "active_patch_id",
        "summary",
        "error_code",
        "error_message",
        "finished_at",
        "mode",
    }
)


@dataclass(slots=True)
class MutationResult:
    row: Any
    applied: bool
    replayed: bool = False


def _utc(value: datetime | None) -> datetime | None:
    return ensure_utc(value)


class ReviewTaskStore:
    """``review_task`` 仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- 创建 -------------------------------------------------------------------
    def find_by_idempotency(
        self, *, actor_id: str, command_type: str, idempotency_key: str
    ) -> ReviewTask | None:
        statement = select(ReviewTask).where(
            ReviewTask.actor_id == actor_id,
            ReviewTask.command_type == command_type,
            ReviewTask.idempotency_key == idempotency_key,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def create(
        self,
        *,
        task_id: str,
        trace_id: str,
        actor_id: str,
        actor_role: str,
        mode: str,
        input_type: str,
        input_hash: str,
        input_summary: dict[str, Any],
        base_commit: str,
        context_policy: str,
        idempotency_key: str,
        input_ref: str | None = None,
        command_type: str = "create_review",
        created_at: datetime | None = None,
    ) -> tuple[ReviewTask, bool]:
        """创建父任务；命中幂等键时返回历史任务（FR-002）。"""
        if len(idempotency_key) < 8:
            raise CodePilotError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "幂等键长度必须 >= 8")

        existing = self.find_by_idempotency(
            actor_id=actor_id, command_type=command_type, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.input_hash != input_hash:
                raise CodePilotError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "相同幂等键对应不同输入内容",
                    trace_id=existing.trace_id,
                    details={"existing_task_id": existing.id, "idempotency_key": idempotency_key},
                )
            return existing, False

        now = created_at or utcnow()
        row = ReviewTask(
            id=task_id,
            trace_id=trace_id,
            correlation_id=f"{task_id}:root",
            actor_id=actor_id,
            actor_role=actor_role,
            command_type=command_type,
            mode=mode,
            input_type=input_type,
            input_hash=input_hash,
            input_ref=input_ref,
            input_summary=input_summary,
            base_commit=base_commit,
            context_policy=context_policy,
            status=str(ParentTaskStatus.DRAFT),
            state_version=1,
            owner=COORDINATOR_OWNER,
            idempotency_key=idempotency_key,
            budget_snapshot={},
            completed=[],
            pending=[],
            child_tasks=[],
            artifacts=[],
            summary={},
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.find_by_idempotency(
                actor_id=actor_id, command_type=command_type, idempotency_key=idempotency_key
            )
            if existing is not None and existing.input_hash == input_hash:
                return existing, False
            raise CodePilotError(
                ErrorCode.IDEMPOTENCY_CONFLICT, "并发创建冲突", details={"idempotency_key": idempotency_key}
            ) from exc

        record_event(
            self.session,
            actor_id=actor_id,
            actor_role=actor_role,
            action="create_review_task",
            entity_type="review_task",
            entity_id=task_id,
            event_type="task_started",
            trace_id=trace_id,
            task_id=task_id,
            parent_task_id=task_id,
            state_version=1,
            after_state={"status": str(ParentTaskStatus.DRAFT), "mode": mode, "input_hash": input_hash},
            idempotency_key=idempotency_key,
            created_at=now,
        )
        return row, True

    # ---- 读取 -------------------------------------------------------------------
    def get(self, task_id: str, *, required: bool = True) -> ReviewTask | None:
        # populate_existing：状态可能由其它会话/进程（Agent 服务、恢复线程）更新，
        # 必须强制从数据库刷新，禁止使用身份映射中的过期对象（宪法第七条：先对账）。
        row = self.session.get(ReviewTask, task_id, populate_existing=True)
        if row is None and required:
            raise CodePilotError(
                ErrorCode.RESOURCE_NOT_FOUND, f"任务不存在：{task_id}", details={"task_id": task_id}
            )
        return row

    def list_tasks(
        self,
        *,
        status: ParentTaskStatus | None = None,
        trace_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReviewTask]:
        statement: Select = select(ReviewTask).order_by(ReviewTask.created_at.asc())
        if status is not None:
            statement = statement.where(ReviewTask.status == str(status))
        if trace_id is not None:
            statement = statement.where(ReviewTask.trace_id == trace_id)
        statement = statement.limit(limit).offset(offset)
        return list(self.session.execute(statement).scalars())

    def list_recoverable(self, *, limit: int = 500) -> list[ReviewTask]:
        """未进入终态的父任务（服务重启后需要恢复）。"""
        terminal = {
            str(ParentTaskStatus.MERGED),
            str(ParentTaskStatus.REJECTED),
            str(ParentTaskStatus.FAILED),
            str(ParentTaskStatus.NEEDS_HUMAN),
        }
        statement = (
            select(ReviewTask)
            .where(ReviewTask.status.notin_(terminal))
            .order_by(ReviewTask.created_at.asc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def count_by_status(self) -> dict[str, int]:
        statement = select(ReviewTask.status, func.count()).group_by(ReviewTask.status)
        counts: dict[str, int] = {}
        for status, count in self.session.execute(statement):
            counts[status] = count
        return counts

    # ---- 写入 -------------------------------------------------------------------
    def apply_intent(
        self,
        task_id: str,
        *,
        expected_version: int,
        owner: str,
        changes: dict[str, Any],
        reason: str,
        idempotency_key: str,
        actor_id: str = COORDINATOR_OWNER,
        actor_role: str = "coordinator",
        event_type: str = "state_intent_applied",
        require_owner: bool = True,
        allow_human_resume: bool = False,
    ) -> MutationResult:
        """状态变更意图协议（docs/02 §4）＋ 原子乐观锁。"""
        current = self.get(task_id)
        assert current is not None

        illegal_fields = set(changes) - _PARENT_MUTABLE_FIELDS
        if illegal_fields:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"不允许修改的父任务字段：{sorted(illegal_fields)}",
                details={"fields": sorted(illegal_fields)},
            )

        if require_owner and current.owner != owner:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"父任务 owner 为 {current.owner}，{owner} 无权提交状态变更",
                trace_id=current.trace_id,
                details={"owner": current.owner, "requested_by": owner},
            )

        replayed = self._find_replayed_intent(task_id, idempotency_key)
        if replayed is not None:
            return MutationResult(row=current, applied=False, replayed=True)

        if "status" in changes:
            target = ParentTaskStatus(changes["status"])
            if allow_human_resume:
                # 唯一允许离开 NEEDS_HUMAN 的路径：admin 显式恢复，且目标绝不含 MERGED。
                from domain.state_machine import assert_human_resume

                assert_human_resume(ParentTaskStatus(current.status), target)
            else:
                assert_parent_transition(ParentTaskStatus(current.status), target)

        values = dict(changes)
        if values.get("status") == str(ParentTaskStatus.NEEDS_HUMAN) and not values.get(
            "needs_human_from"
        ):
            values["needs_human_from"] = current.status
        if values.get("status") in {
            str(ParentTaskStatus.MERGED),
            str(ParentTaskStatus.REJECTED),
            str(ParentTaskStatus.FAILED),
        }:
            values.setdefault("finished_at", utcnow())

        result = self.session.execute(
            update(ReviewTask)
            .where(ReviewTask.id == task_id, ReviewTask.state_version == expected_version)
            .values(**values, state_version=expected_version + 1, updated_at=utcnow())
        )
        if result.rowcount != 1:
            fresh = self.session.get(ReviewTask, task_id)
            raise CodePilotError(
                ErrorCode.STATE_VERSION_CONFLICT,
                f"状态版本冲突：期望 {expected_version}，实际 {fresh.state_version if fresh else 'unknown'}",
                trace_id=current.trace_id,
                details={
                    "expected_version": expected_version,
                    "actual_version": fresh.state_version if fresh else None,
                    "task_id": task_id,
                },
            )

        record_event(
            self.session,
            actor_id=actor_id,
            actor_role=actor_role,
            action="apply_state_intent",
            entity_type="review_task",
            entity_id=task_id,
            event_type=event_type,
            trace_id=current.trace_id,
            task_id=task_id,
            parent_task_id=task_id,
            state_version=expected_version + 1,
            before_state={"status": current.status, "state_version": current.state_version},
            after_state={**changes, "reason": reason},
            idempotency_key=idempotency_key,
        )
        self.session.flush()
        refreshed = self.session.get(ReviewTask, task_id)
        self.session.refresh(refreshed)
        return MutationResult(row=refreshed, applied=True)

    def _find_replayed_intent(self, task_id: str, idempotency_key: str) -> AuditEvent | None:
        statement = select(AuditEvent).where(
            AuditEvent.entity_id == task_id,
            AuditEvent.idempotency_key == idempotency_key,
            AuditEvent.action == "apply_state_intent",
        )
        return self.session.execute(statement).scalars().first()

    def human_resume(
        self,
        task_id: str,
        *,
        target_status: ParentTaskStatus,
        expected_version: int,
        actor_id: str,
        reason: str,
        idempotency_key: str,
    ) -> MutationResult:
        """人工恢复：仅 admin，仅从 NEEDS_HUMAN，且绝不进入 MERGED。"""
        from domain.state_machine import assert_human_resume

        current = self.get(task_id)
        assert current is not None
        assert_human_resume(ParentTaskStatus(current.status), target_status)
        if not reason.strip():
            raise CodePilotError(ErrorCode.INVALID_INPUT, "人工恢复必须填写原因")
        return self.apply_intent(
            task_id,
            expected_version=expected_version,
            owner=current.owner,
            changes={"status": str(target_status), "error_code": None, "error_message": None},
            reason=reason,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            actor_role="admin",
            event_type="human_resume",
            allow_human_resume=True,
        )


class A2ATaskStore:
    """``a2a_task`` 仓储（子任务幂等 + 乐观锁 + 终态保护）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_idempotency(
        self,
        *,
        parent_task_id: str,
        agent_id: str,
        task_type: str,
        idempotency_key: str,
        side: str = "coordinator",
    ) -> A2ATaskRow | None:
        statement = select(A2ATaskRow).where(
            A2ATaskRow.side == side,
            A2ATaskRow.parent_task_id == parent_task_id,
            A2ATaskRow.agent_id == agent_id,
            A2ATaskRow.task_type == task_type,
            A2ATaskRow.idempotency_key == idempotency_key,
        )
        return self.session.execute(statement).scalar_one_or_none()

    def get(self, task_id: str, *, required: bool = True) -> A2ATaskRow | None:
        # 状态可能由远端 Agent 或后台线程更新，必须强制刷新（见 ReviewTaskStore.get）。
        row = self.session.get(A2ATaskRow, task_id, populate_existing=True)
        if row is None and required:
            raise CodePilotError(
                ErrorCode.RESOURCE_NOT_FOUND, f"子任务不存在：{task_id}", details={"task_id": task_id}
            )
        return row

    def submit(
        self,
        task: A2ATask,
        *,
        transport: str = "inprocess",
        owner: str | None = None,
        side: str = "coordinator",
    ) -> tuple[A2ATaskRow, bool]:
        """创建（或幂等返回）子任务。

        ``side`` 区分同一条子任务在 Coordinator（跟踪）与 Agent（执行）两侧的记录，
        两者共享数据库时互不冲突。
        """
        assert task.parent_task_id is not None, "子任务必须带 parent_task_id"
        existing = self.find_by_idempotency(
            parent_task_id=task.parent_task_id,
            agent_id=task.agent_id,
            task_type=str(task.task_type),
            idempotency_key=task.idempotency_key,
            side=side,
        )
        if existing is not None:
            return existing, False

        row = A2ATaskRow(
            id=task.task_id,
            parent_task_id=task.parent_task_id,
            trace_id=task.trace_id,
            correlation_id=task.correlation_id,
            agent_id=task.agent_id,
            task_type=str(task.task_type),
            side=side,
            status=str(task.status),
            protocol_version=task.protocol_version,
            idempotency_key=task.idempotency_key,
            attempt=task.attempt,
            deadline=ensure_utc(task.deadline),
            state_version=task.state_version,
            owner=owner or task.agent_id,
            input_artifacts=list(task.input_artifacts),
            required_output_types=list(task.required_output_types),
            transport=transport,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.find_by_idempotency(
                parent_task_id=task.parent_task_id,
                agent_id=task.agent_id,
                task_type=str(task.task_type),
                idempotency_key=task.idempotency_key,
                side=side,
            )
            if existing is not None:
                return existing, False
            raise CodePilotError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "子任务创建冲突",
                details={"idempotency_key": task.idempotency_key},
            ) from exc

        record_event(
            self.session,
            actor_id=COORDINATOR_OWNER,
            actor_role="coordinator",
            action="submit_child_task",
            entity_type="a2a_task",
            entity_id=task.task_id,
            event_type="child_task_created",
            trace_id=task.trace_id,
            task_id=task.task_id,
            parent_task_id=task.parent_task_id,
            agent_id=task.agent_id,
            state_version=task.state_version,
            after_state={
                "task_type": str(task.task_type),
                "agent_id": task.agent_id,
                "status": str(task.status),
                "idempotency_key": task.idempotency_key,
                "transport": transport,
            },
            idempotency_key=task.idempotency_key,
        )
        return row, True

    def transition(
        self,
        task_id: str,
        target: ChildTaskStatus,
        *,
        expected_version: int,
        idempotency_key: str,
        owner: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
        result_ref: str | None = None,
        attempt: int | None = None,
        actor_id: str | None = None,
    ) -> MutationResult:
        row = self.get(task_id)
        assert row is not None
        current = ChildTaskStatus(row.status)

        if owner is not None and row.owner != owner:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"子任务 owner 为 {row.owner}，{owner} 无权更新",
                trace_id=row.trace_id,
                details={"owner": row.owner, "requested_by": owner},
            )

        replayed = self.session.execute(
            select(AuditEvent).where(
                AuditEvent.entity_id == task_id,
                AuditEvent.idempotency_key == idempotency_key,
                AuditEvent.action == "transition_child_task",
            )
        ).scalars().first()
        if replayed is not None:
            # 重放保护：只有当行已经处于目标状态（或已进入终态）时，才允许当作"同命令重放"跳过。
            # 否则说明调用方把同一个幂等键用在了不同的状态迁移上——这会静默跳过迁移，
            # 让子任务停在 working 之类的非终态（曾经的真实缺陷），因此必须显式失败。
            if current is target or current in {
                ChildTaskStatus.COMPLETED,
                ChildTaskStatus.FAILED,
                ChildTaskStatus.CANCELED,
            }:
                return MutationResult(row=row, applied=False, replayed=True)
            raise CodePilotError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                f"幂等键 {idempotency_key} 已被用于另一次状态迁移：当前 {current} → 目标 {target}",
                trace_id=row.trace_id,
                details={
                    "task_id": task_id,
                    "current": str(current),
                    "target": str(target),
                    "idempotency_key": idempotency_key,
                },
            )

        assert_child_transition(current, target)

        if current is ChildTaskStatus.FAILED and target is ChildTaskStatus.WORKING:
            from domain.state_machine import can_retry_child

            if not can_retry_child(status=current, attempt=row.attempt, error_code=row.error_code):
                raise CodePilotError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    "该失败子任务不满足受控重试条件（attempt 上限或错误不可重试）",
                    trace_id=row.trace_id,
                    details={"attempt": row.attempt, "error_code": row.error_code},
                )

        values: dict[str, Any] = {
            "status": str(target),
            "error_code": error_code,
            "error_message": error_message,
        }
        if duration_ms is not None:
            values["duration_ms"] = duration_ms
        if result_ref is not None:
            values["result_ref"] = result_ref
        if attempt is not None:
            values["attempt"] = attempt
        if target in {ChildTaskStatus.COMPLETED, ChildTaskStatus.FAILED, ChildTaskStatus.CANCELED}:
            values["completed_at"] = utcnow()

        result = self.session.execute(
            update(A2ATaskRow)
            .where(A2ATaskRow.id == task_id, A2ATaskRow.state_version == expected_version)
            .values(**values, state_version=expected_version + 1, updated_at=utcnow())
        )
        if result.rowcount != 1:
            fresh = self.session.get(A2ATaskRow, task_id)
            raise CodePilotError(
                ErrorCode.STATE_VERSION_CONFLICT,
                f"子任务状态版本冲突：期望 {expected_version}，实际 {fresh.state_version if fresh else 'unknown'}",
                trace_id=row.trace_id,
                details={"expected_version": expected_version, "task_id": task_id},
            )

        event_type = {
            ChildTaskStatus.WORKING: "agent_started",
            ChildTaskStatus.COMPLETED: "child_task_completed",
            ChildTaskStatus.FAILED: "child_task_failed",
            ChildTaskStatus.CANCELED: "child_task_canceled",
            ChildTaskStatus.INPUT_REQUIRED: "child_task_input_required",
            ChildTaskStatus.SUBMITTED: "child_task_created",
        }[target]

        record_event(
            self.session,
            actor_id=actor_id or row.agent_id,
            actor_role="coordinator" if (actor_id or "") == COORDINATOR_OWNER else "agent",
            action="transition_child_task",
            entity_type="a2a_task",
            entity_id=task_id,
            event_type=event_type,
            trace_id=row.trace_id,
            task_id=task_id,
            parent_task_id=row.parent_task_id,
            agent_id=row.agent_id,
            state_version=expected_version + 1,
            before_state={"status": row.status},
            after_state={**values, "state_version": expected_version + 1},
            idempotency_key=idempotency_key,
            error_code=error_code,
            duration_ms=duration_ms,
        )
        self.session.flush()
        refreshed = self.session.get(A2ATaskRow, task_id)
        self.session.refresh(refreshed)
        return MutationResult(row=refreshed, applied=True)

    def list_by_parent(self, parent_task_id: str, *, side: str = "coordinator") -> list[A2ATaskRow]:
        statement = (
            select(A2ATaskRow)
            .where(A2ATaskRow.parent_task_id == parent_task_id, A2ATaskRow.side == side)
            .order_by(A2ATaskRow.created_at.asc())
            .execution_options(populate_existing=True)
        )
        return list(self.session.execute(statement).scalars())

    def list_unfinished(self, *, limit: int = 500, side: str = "coordinator") -> list[A2ATaskRow]:
        terminal = [
            str(status) for status in ChildTaskStatus if status.value in {"completed", "failed", "canceled"}
        ]
        statement = (
            select(A2ATaskRow)
            .where(A2ATaskRow.side == side, A2ATaskRow.status.notin_(terminal))
            .order_by(A2ATaskRow.created_at.asc())
            .limit(limit)
        )
        return list(self.session.execute(statement).scalars())

    def latest_for(
        self, parent_task_id: str, task_type: ChildTaskType, *, side: str = "coordinator"
    ) -> A2ATaskRow | None:
        statement = (
            select(A2ATaskRow)
            .where(
                A2ATaskRow.side == side,
                A2ATaskRow.parent_task_id == parent_task_id,
                A2ATaskRow.task_type == str(task_type),
            )
            .order_by(A2ATaskRow.attempt.desc(), A2ATaskRow.created_at.desc())
        )
        return self.session.execute(statement).scalars().first()


class ArtifactStore:
    """``a2a_artifact`` 仓储：入库前必须已通过 Schema 与哈希校验（FR-093）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def save(
        self,
        envelope: ArtifactEnvelope,
        *,
        parent_task_id: str,
        agent_id: str,
        validated: bool,
        validation_error: str | None = None,
        data_ref: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[A2AArtifact, bool]:
        existing = self.session.execute(
            select(A2AArtifact).where(
                A2AArtifact.task_id == envelope.task_id,
                A2AArtifact.artifact_type == str(envelope.artifact_type),
                A2AArtifact.content_hash == envelope.content_hash,
            )
        ).scalars().first()
        if existing is not None:
            return existing, False

        row = A2AArtifact(
            id=envelope.artifact_id,
            task_id=envelope.task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            artifact_type=str(envelope.artifact_type),
            schema_version=envelope.schema_version,
            content_hash=envelope.content_hash,
            size_bytes=envelope.size_bytes,
            data=envelope.data,
            data_ref=data_ref,
            validated=validated,
            validation_error=validation_error,
        )
        self.session.add(row)
        self.session.flush()

        record_event(
            self.session,
            actor_id=agent_id,
            actor_role="agent",
            action="store_artifact",
            entity_type="a2a_artifact",
            entity_id=envelope.artifact_id,
            event_type="artifact_received" if validated else "artifact_rejected",
            trace_id=trace_id,
            task_id=envelope.task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            after_state={
                "artifact_type": str(envelope.artifact_type),
                "schema_version": envelope.schema_version,
                "content_hash": envelope.content_hash,
                "size_bytes": envelope.size_bytes,
                "task_id": envelope.task_id,
                "validated": validated,
            },
            error_code=None if validated else "ARTIFACT_SCHEMA_INVALID",
        )
        if not validated:
            self.session.flush()
        return row, True

    def get(self, artifact_id: str) -> A2AArtifact | None:
        return self.session.get(A2AArtifact, artifact_id)

    def find_by_content(
        self, *, task_id: str, artifact_type: str, content_hash: str
    ) -> A2AArtifact | None:
        """按 (task_id, artifact_type, content_hash) 查找已有产物（幂等入库）。"""
        statement = select(A2AArtifact).where(
            A2AArtifact.task_id == task_id,
            A2AArtifact.artifact_type == artifact_type,
            A2AArtifact.content_hash == content_hash,
        )
        return self.session.execute(statement).scalars().first()

    def list_by_task(self, task_id: str) -> list[A2AArtifact]:
        return list(
            self.session.execute(
                select(A2AArtifact).where(A2AArtifact.task_id == task_id).order_by(A2AArtifact.created_at)
            ).scalars()
        )

    def list_by_parent(self, parent_task_id: str, *, artifact_type: str | None = None) -> list[A2AArtifact]:
        statement = select(A2AArtifact).where(A2AArtifact.parent_task_id == parent_task_id)
        if artifact_type is not None:
            statement = statement.where(A2AArtifact.artifact_type == artifact_type)
        return list(self.session.execute(statement.order_by(A2AArtifact.created_at)).scalars())

    def latest_by_type(self, parent_task_id: str, artifact_type: str) -> A2AArtifact | None:
        statement = (
            select(A2AArtifact)
            .where(A2AArtifact.parent_task_id == parent_task_id, A2AArtifact.artifact_type == artifact_type)
            .order_by(A2AArtifact.created_at.desc())
        )
        return self.session.execute(statement).scalars().first()

    def to_envelope(self, row: A2AArtifact) -> ArtifactEnvelope:
        return ArtifactEnvelope(
            artifact_id=row.id,
            task_id=row.task_id,
            artifact_type=row.artifact_type,
            schema_version=row.schema_version,
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            data=row.data,
        )


class MessageStore:
    """``a2a_message`` 仓储（只保存摘要与哈希，原文放到 Artifact）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        message: A2AMessage,
        *,
        parent_task_id: str | None = None,
        trace_id: str = "",
        payload_summary: dict[str, Any] | None = None,
    ) -> A2AMessageRow:
        row = A2AMessageRow(
            id=message.message_id,
            task_id=message.task_id,
            parent_task_id=parent_task_id,
            trace_id=trace_id or "trace-unknown",
            message_type=message.type,
            role=message.role,
            correlation_id=message.correlation_id,
            artifact_refs=list(message.artifact_refs),
            payload_hash=payload_hash(message.model_dump(mode="json")),
            payload_summary=payload_summary or {},
            error_code=message.error.code if message.error else None,
            error_message=message.error.message if message.error else None,
            created_at=message.created_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list_by_task(self, task_id: str) -> list[A2AMessageRow]:
        return list(
            self.session.execute(
                select(A2AMessageRow)
                .where(A2AMessageRow.task_id == task_id)
                .order_by(A2AMessageRow.created_at)
            ).scalars()
        )


class AgentCardStore:
    """``a2a_agent`` 仓储：Card 变更必须版本化（docs/05 §2）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def sync(self, card: AgentCard, *, active: bool = True) -> A2AAgent:
        payload = card.model_dump(mode="json")
        row_id = f"{card.agent_id}@{card.card_version}"
        existing = self.session.get(A2AAgent, row_id)
        if existing is None:
            existing = A2AAgent(
                id=row_id,
                agent_id=card.agent_id,
                card_version=card.card_version,
                protocol_versions=list(card.protocol_versions),
                capabilities=list(card.capabilities),
                input_schema=card.input_schema,
                output_schemas=list(card.output_schemas),
                endpoint=card.endpoint,
                auth_config=card.auth.model_dump(mode="json"),
                limits=card.limits.model_dump(mode="json"),
                status=str(card.status),
                card_hash=payload_hash(payload),
                card_snapshot=payload,
                active=active,
            )
            self.session.add(existing)
        else:
            existing.status = str(card.status)
            existing.card_hash = payload_hash(payload)
            existing.card_snapshot = payload
            existing.active = active
        self.session.flush()
        return existing

    def list_active(self) -> list[A2AAgent]:
        return list(
            self.session.execute(select(A2AAgent).where(A2AAgent.active.is_(True)).order_by(A2AAgent.agent_id)).scalars()
        )

    def get(self, agent_id: str, card_version: str | None = None) -> A2AAgent | None:
        if card_version:
            return self.session.get(A2AAgent, f"{agent_id}@{card_version}")
        statement = (
            select(A2AAgent)
            .where(A2AAgent.agent_id == agent_id)
            .order_by(A2AAgent.card_version.desc())
        )
        return self.session.execute(statement).scalars().first()


def new_event_message_id() -> str:
    return new_message_id()


def new_child_id(task_type: str) -> str:
    return new_child_task_id(task_type)


def new_artifact_identifier() -> str:
    return new_artifact_id()


def new_correlation(prefix: str) -> str:
    return new_id(prefix)


__all__ = [
    "COORDINATOR_OWNER",
    "A2ATaskStore",
    "AgentCardStore",
    "ArtifactStore",
    "MessageStore",
    "MutationResult",
    "ReviewTaskStore",
    "new_artifact_identifier",
    "new_child_id",
    "new_correlation",
    "new_event_message_id",
]
