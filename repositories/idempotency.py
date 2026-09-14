"""命令级幂等台账（docs/03 §6、宪法第七条）。

语义：
1. **同一幂等键 + 同一请求内容** → 返回**原结果**（进程重启后依然可重放）；
2. **同一幂等键 + 不同请求内容** → ``IDEMPOTENCY_CONFLICT``；
3. 首次执行会先落 ``in_progress`` 占位，执行成功后写入响应；
   执行期间重复提交（并发）→ ``CONFLICT``，避免重复副作用；
4. 执行失败会把状态置为 ``failed``，允许调用方用同一键重试（失败无副作用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_id
from domain.sanitize import payload_hash, summarize_payload
from repositories.models import IdempotencyRecord

COMMAND_CREATE_REVIEW = "create_review"
COMMAND_TRIGGER_FIX = "trigger_fix"
COMMAND_APPROVAL = "patch_approval"
COMMAND_MERGE = "patch_merge"
COMMAND_EVAL_RUN = "eval_run"
COMMAND_A2A_SUBMIT = "a2a_submit"
COMMAND_A2A_CANCEL = "a2a_cancel"
COMMAND_HUMAN_RESUME = "human_resume"


@dataclass(slots=True)
class Reservation:
    """一次幂等预留的结果。"""

    record: IdempotencyRecord
    created: bool

    @property
    def replayed(self) -> bool:
        return not self.created

    @property
    def completed(self) -> bool:
        return self.record.status == "completed"

    @property
    def response(self) -> dict[str, Any]:
        return dict(self.record.response or {})


class IdempotencyStore:
    """``idempotency_record`` 仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- 查询 -------------------------------------------------------------------
    def find(
        self,
        *,
        actor_id: str,
        command_type: str,
        aggregate_ref: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.command_type == command_type,
            IdempotencyRecord.aggregate_ref == aggregate_ref,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        return self.session.execute(statement).scalars().first()

    def find_by_key(
        self, *, actor_id: str, command_type: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        """按 (actor, command_type, key) 查找：用于识别"同键不同请求"（不改作用域）。"""
        statement = select(IdempotencyRecord).where(
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.command_type == command_type,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        return self.session.execute(statement).scalars().first()

    def list_for_aggregate(self, *, command_type: str, aggregate_ref: str) -> list[IdempotencyRecord]:
        statement = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.command_type == command_type,
                IdempotencyRecord.aggregate_ref == aggregate_ref,
            )
            .order_by(IdempotencyRecord.created_at)
        )
        return list(self.session.execute(statement).scalars())

    # ---- 预留 -------------------------------------------------------------------
    def reserve(
        self,
        *,
        actor_id: str,
        actor_role: str,
        command_type: str,
        aggregate_ref: str,
        idempotency_key: str,
        request: dict[str, Any] | None = None,
    ) -> Reservation:
        """预留幂等键；命中历史记录时返回原记录（含原响应）。"""
        if len(idempotency_key) < 8:
            raise CodePilotError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "幂等键长度必须 >= 8")

        request_hash = payload_hash(
            {"command_type": command_type, "aggregate_ref": aggregate_ref, "request": request or {}}
        )
        # 先按 (actor, command_type, key) 查：同一幂等键复用给不同请求/不同对象都属于客户端错误。
        by_key = self.find_by_key(
            actor_id=actor_id, command_type=command_type, idempotency_key=idempotency_key
        )
        if by_key is not None and (
            by_key.aggregate_ref != aggregate_ref or by_key.request_hash != request_hash
        ):
            raise CodePilotError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应不同的请求内容",
                details={
                    "command_type": command_type,
                    "aggregate_ref": aggregate_ref,
                    "existing_aggregate_ref": by_key.aggregate_ref,
                    "idempotency_key": idempotency_key,
                },
            )

        existing = by_key or self.find(
            actor_id=actor_id,
            command_type=command_type,
            aggregate_ref=aggregate_ref,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise CodePilotError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "相同幂等键对应不同的请求内容",
                    details={
                        "command_type": command_type,
                        "aggregate_ref": aggregate_ref,
                        "idempotency_key": idempotency_key,
                    },
                )
            if existing.status == "in_progress":
                raise CodePilotError(
                    ErrorCode.CONFLICT,
                    "同一幂等键的请求正在处理中，请稍后重试",
                    details={"command_type": command_type, "idempotency_key": idempotency_key},
                )
            return Reservation(record=existing, created=False)

        record = IdempotencyRecord(
            id=new_id("idem"),
            actor_id=actor_id,
            actor_role=actor_role,
            command_type=command_type,
            aggregate_ref=aggregate_ref,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            request_summary=summarize_payload(request or {}),
            status="in_progress",
            response={},
        )
        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError as exc:  # 并发预留
            self.session.rollback()
            concurrent = self.find(
                actor_id=actor_id,
                command_type=command_type,
                aggregate_ref=aggregate_ref,
                idempotency_key=idempotency_key,
            )
            if concurrent is not None and concurrent.request_hash == request_hash:
                raise CodePilotError(
                    ErrorCode.CONFLICT,
                    "同一幂等键的请求正在处理中，请稍后重试",
                    details={"command_type": command_type, "idempotency_key": idempotency_key},
                ) from exc
            raise CodePilotError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同幂等键对应不同的请求内容",
                details={"command_type": command_type, "idempotency_key": idempotency_key},
            ) from exc
        return Reservation(record=record, created=True)

    # ---- 收尾 -------------------------------------------------------------------
    def complete(self, record: IdempotencyRecord, response: dict[str, Any]) -> IdempotencyRecord:
        record.status = "completed"
        record.response = _jsonable(response)
        record.error_code = None
        self.session.flush()
        return record

    def fail(self, record: IdempotencyRecord, error_code: str) -> IdempotencyRecord:
        record.status = "failed"
        record.error_code = error_code
        self.session.flush()
        return record


def _jsonable(payload: dict[str, Any]) -> dict[str, Any]:
    """把响应转换为可持久化 JSON（datetime/Enum 等转字符串）。"""
    from domain.sanitize import sanitize_structured

    return sanitize_structured(payload, max_string=4000)


__all__ = [
    "COMMAND_A2A_CANCEL",
    "COMMAND_A2A_SUBMIT",
    "COMMAND_APPROVAL",
    "COMMAND_CREATE_REVIEW",
    "COMMAND_EVAL_RUN",
    "COMMAND_HUMAN_RESUME",
    "COMMAND_MERGE",
    "COMMAND_TRIGGER_FIX",
    "IdempotencyStore",
    "Reservation",
]
