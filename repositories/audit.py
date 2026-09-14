"""追加式审计（FR-073、FR-015、NFR-007）。

三重保障：
1. ORM 层：``before_update`` / ``before_delete`` 事件直接抛错，任何代码路径都无法改写；
2. 数据库层：Alembic 迁移为 ``audit_event`` 与 ``approval`` 安装
   ``BEFORE UPDATE OR DELETE`` 触发器（PostgreSQL 函数 / SQLite 触发器）；
3. API 层：只提供查询接口，没有更新与删除端点。

审计内容只保存摘要、哈希与脱敏值（NFR-006）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from domain.clock import utcnow
from domain.ids import new_audit_id
from domain.sanitize import payload_hash, sanitize_structured, summarize_payload
from repositories.models import Approval, AuditEvent


class AuditImmutabilityError(RuntimeError):
    """审计与审批记录禁止修改或删除。"""


def _block_mutation(_mapper, _connection, target) -> None:  # noqa: ANN001 - SQLAlchemy 回调签名
    raise AuditImmutabilityError(
        f"{type(target).__name__} 是追加式记录，禁止 UPDATE/DELETE（FR-073）"
    )


for _model in (AuditEvent, Approval):
    event.listen(_model, "before_update", _block_mutation)
    event.listen(_model, "before_delete", _block_mutation)


def record_event(
    session: Session,
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    entity_type: str,
    entity_id: str,
    event_type: str,
    trace_id: str | None = None,
    parent_task_id: str | None = None,
    task_id: str | None = None,
    agent_id: str | None = None,
    state_version: int | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
    created_at: datetime | None = None,
) -> AuditEvent:
    """追加一条审计事件。调用方必须在事务内使用。"""
    record = AuditEvent(
        id=new_audit_id(),
        trace_id=trace_id,
        parent_task_id=parent_task_id,
        task_id=task_id,
        agent_id=agent_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        state_version=state_version,
        before_state=sanitize_structured(before_state or {}),
        after_state=sanitize_structured(after_state or {}),
        idempotency_key=idempotency_key,
        error_code=error_code,
        duration_ms=duration_ms,
        created_at=created_at or utcnow(),
    )
    session.add(record)
    return record


def record_tool_execution_event(
    session: Session,
    *,
    actor_id: str,
    actor_role: str,
    tool_name: str,
    allowed: bool,
    params: dict[str, Any],
    result: Any,
    trace_id: str | None,
    task_id: str,
    idempotency_key: str,
) -> AuditEvent:
    """工具调用审计：只写参数哈希与结果哈希（FR-015）。"""
    return record_event(
        session,
        actor_id=actor_id,
        actor_role=actor_role,
        action="tool_call",
        entity_type="tool",
        entity_id=tool_name,
        event_type="tool_called" if allowed else "tool_denied",
        trace_id=trace_id,
        task_id=task_id,
        idempotency_key=idempotency_key,
        after_state={
            "params_hash": payload_hash(params),
            "params_summary": summarize_payload(params),
            "result_hash": payload_hash(result) if allowed else None,
        },
    )


def list_events(
    session: Session,
    *,
    trace_id: str | None = None,
    parent_task_id: str | None = None,
    task_id: str | None = None,
    event_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[AuditEvent]:
    statement = select(AuditEvent).order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
    if trace_id:
        statement = statement.where(AuditEvent.trace_id == trace_id)
    if parent_task_id:
        statement = statement.where(AuditEvent.parent_task_id == parent_task_id)
    if task_id:
        statement = statement.where(AuditEvent.task_id == task_id)
    if event_type:
        statement = statement.where(AuditEvent.event_type == event_type)
    statement = statement.limit(limit).offset(offset)
    return list(session.execute(statement).scalars())


def trace_integrity(session: Session, trace_id: str) -> dict[str, Any]:
    """Trace 完整率统计（NFR-013）：是否有父任务、子任务、Artifact 与工具事件。"""
    events = list_events(session, trace_id=trace_id, limit=1000)
    has_task = any(item.event_type == "task_started" for item in events)
    has_child = any(item.event_type.startswith("child_task") for item in events)
    has_artifact = any(item.event_type == "artifact_received" for item in events)
    has_tool = any(item.event_type == "tool_called" for item in events)
    linked = all(
        item.task_id is not None or item.parent_task_id is not None
        for item in events
        if item.entity_type in {"a2a_task", "review_task"}
    )
    return {
        "trace_id": trace_id,
        "event_count": len(events),
        "has_task_started": has_task,
        "has_child_event": has_child,
        "has_artifact_event": has_artifact,
        "has_tool_event": has_tool,
        "links_complete": linked,
        "complete": has_task and has_child and has_artifact and linked,
    }


__all__ = [
    "AuditImmutabilityError",
    "list_events",
    "record_event",
    "record_tool_execution_event",
    "trace_integrity",
]
