"""统一时钟与时间归一化。

PostgreSQL 保留时区，SQLite 不保留；所有领域时间统一为 UTC aware（宪法第三条：
缓存/存储差异不得改变业务结论）。
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def isoformat(value: datetime | None) -> str | None:
    moment = ensure_utc(value)
    return moment.isoformat().replace("+00:00", "Z") if moment else None


__all__ = ["ensure_utc", "isoformat", "utcnow"]
