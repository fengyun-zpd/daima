"""SSE 事件流（FR-092、docs/01 §8）。

实现要点：
- 事件来源是追加式审计表，天然满足"只追加、可回放"；
- 支持 ``Last-Event-ID`` 续接，游标即审计事件主键；
- 客户端断线后可用 ``GET /reviews/{id}`` 轮询补偿（docs/02 §6）；
- 连接在父任务进入终态且事件发完后自动结束，避免连接泄漏。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

from sqlalchemy.orm import Session

from domain.clock import isoformat
from domain.enums import ParentTaskStatus
from repositories.models import AuditEvent, ReviewTask

SSE_EVENT_FIELDS = (
    "event_id",
    "trace_id",
    "parent_task_id",
    "task_id",
    "agent_id",
    "event_type",
    "state_version",
    "artifact_id",
    "idempotency_key",
    "duration_ms",
    "error_code",
)

TERMINAL_STATUSES = {
    str(ParentTaskStatus.MERGED),
    str(ParentTaskStatus.REJECTED),
    str(ParentTaskStatus.FAILED),
}


def event_payload(event: AuditEvent) -> dict[str, object]:
    after = event.after_state or {}
    return {
        "event_id": event.id,
        "trace_id": event.trace_id,
        "parent_task_id": event.parent_task_id,
        "task_id": event.task_id,
        "agent_id": event.agent_id,
        "event_type": event.event_type,
        "state_version": event.state_version,
        "artifact_id": after.get("artifact_id"),
        "idempotency_key": event.idempotency_key,
        "duration_ms": event.duration_ms,
        "error_code": event.error_code,
        "created_at": isoformat(event.created_at),
        "summary": {
            key: value
            for key, value in after.items()
            if key in {"status", "artifact_type", "content_hash", "task_type", "agent_id", "reason", "decision"}
        },
    }


def format_sse(event: AuditEvent) -> str:
    payload = event_payload(event)
    body = json.dumps(payload, ensure_ascii=False)
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {body}\n\n"


def drain(session: Session, *, task_id: str, after_event_id: str | None, limit: int = 500) -> Iterator[AuditEvent]:
    """按排序读取与 ``task_id`` 相关的新事件（父任务自身的及全部子任务事件）。"""
    from sqlalchemy import or_, select

    statement = (
        select(AuditEvent)
        .where(or_(AuditEvent.task_id == task_id, AuditEvent.parent_task_id == task_id))
        .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
        .limit(limit)
    )
    events = list(session.execute(statement).scalars())
    if after_event_id is None:
        yield from events
        return
    seen = False
    for event in events:
        if seen:
            yield event
            continue
        if event.id == after_event_id:
            seen = True
    if not seen:
        # 游标已失效（例如事件被清理）：退化为全量返回，由客户端按 id 去重。
        yield from events


async def stream_events(
    *,
    session_factory,
    task_id: str,
    last_event_id: str | None = None,
    poll_interval: float = 0.25,
    max_seconds: float = 120.0,
    stop_when_terminal: bool = True,
) -> AsyncIterator[str]:
    """生成 SSE 事件流。"""
    cursor = last_event_id
    elapsed = 0.0
    terminal = False

    yield ": connected\n\n"
    while True:
        session = session_factory()
        try:
            for event in drain(session, task_id=task_id, after_event_id=cursor):
                cursor = event.id
                yield format_sse(event)
            task = session.get(ReviewTask, task_id)
            if task is not None:
                terminal = task.status in TERMINAL_STATUSES
        finally:
            session.close()

        if stop_when_terminal and terminal:
            yield "event: stream_closed\ndata: {\"reason\": \"task_terminal\"}\n\n"
            return
        if elapsed >= max_seconds:
            yield "event: stream_closed\ndata: {\"reason\": \"max_duration\"}\n\n"
            return

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        yield ": keep-alive\n\n"


__all__ = [
    "SSE_EVENT_FIELDS",
    "TERMINAL_STATUSES",
    "drain",
    "event_payload",
    "format_sse",
    "stream_events",
]
