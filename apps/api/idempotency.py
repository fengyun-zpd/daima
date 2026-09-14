"""命令级幂等的服务层封装：预留 → 执行 → 记录响应。

API 层只负责读取 ``Idempotency-Key`` 并透传；语义判定与持久化都在这里完成，
保证"同一键返回原结果、不同内容返回 IDEMPOTENCY_CONFLICT、不产生重复副作用"。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from domain.errors import CodePilotError, ErrorCode
from repositories.idempotency import IdempotencyStore
from repositories.models import IdempotencyRecord

SessionFactory = sessionmaker[Session]


def execute_idempotent(
    session_factory: SessionFactory,
    *,
    actor_id: str,
    actor_role: str,
    command_type: str,
    aggregate_ref: str,
    idempotency_key: str,
    request: dict[str, Any] | None,
    work: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """执行一个带幂等语义的写命令。

    - 命中历史 ``completed`` 记录 → 直接返回原响应（附加 ``idempotent_replay=True``）；
    - 命中 ``failed`` 记录 → 允许用同一键重试（失败不产生副作用）；
    - 同一键但请求内容不同 → ``IDEMPOTENCY_CONFLICT``；
    - 预留成功 → 执行 ``work``，成功后把响应写入台账。
    """
    session = session_factory()
    try:
        reservation = IdempotencyStore(session).reserve(
            actor_id=actor_id,
            actor_role=actor_role,
            command_type=command_type,
            aggregate_ref=aggregate_ref,
            idempotency_key=idempotency_key,
            request=request,
        )
        if reservation.replayed and reservation.completed:
            return {**reservation.response, "idempotent_replay": True}
        record_id = reservation.record.id
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    try:
        result = work()
    except CodePilotError as exc:
        _update(session_factory, record_id, status="failed", error_code=str(exc.code))
        raise
    except Exception:
        _update(session_factory, record_id, status="failed", error_code=str(ErrorCode.INTERNAL_ERROR))
        raise

    _update(session_factory, record_id, status="completed", response=result)
    return result


def _update(
    session_factory: SessionFactory,
    record_id: str,
    *,
    status: str,
    response: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    session = session_factory()
    try:
        record = session.get(IdempotencyRecord, record_id)
        if record is None:  # pragma: no cover - 记录被外部删除
            return
        store = IdempotencyStore(session)
        if status == "completed" and response is not None:
            store.complete(record, response)
        elif status == "failed":
            store.fail(record, error_code or str(ErrorCode.INTERNAL_ERROR))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["execute_idempotent"]
