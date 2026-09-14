"""标识符生成：Task、Trace、Artifact、Message。

约束（宪法第四条、docs/05 §3）：
- 每个 Agent 调用必须有 ``task_id``、``parent_task_id``、``trace_id``、``correlation_id``；
- Schema 要求 ``task_id`` / ``trace_id`` / ``idempotency_key`` / ``correlation_id`` 长度 >= 8。
"""

from __future__ import annotations

import os
import secrets
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


def new_ulid() -> str:
    """生成 26 字符 ULID（时间有序，便于审计排序）。"""
    timestamp_ms = int(time.time() * 1000)
    randomness = secrets.randbits(80)
    return _encode(timestamp_ms, 10) + _encode(randomness, 16)


def new_id(prefix: str) -> str:
    """生成形如 ``review-01J...`` 的标识符。"""
    return f"{prefix}-{new_ulid()}"


def new_task_id() -> str:
    return new_id("task")


def new_child_task_id(task_type: str) -> str:
    return new_id(task_type)


def new_trace_id() -> str:
    return new_id("trace")


def new_artifact_id() -> str:
    return new_id("artifact")


def new_message_id() -> str:
    return new_id("msg")


def new_event_id() -> str:
    return new_id("evt")


def new_audit_id() -> str:
    return new_id("audit")


def new_sandbox_run_id() -> str:
    return new_id("sandbox")


def new_eval_run_id() -> str:
    return new_id("eval")


def new_idempotency_key(*parts: object) -> str:
    """按 docs/03 §6 的建议格式拼接幂等键：``task_id:command:resource:version``。"""
    joined = ":".join(str(part) for part in parts if part not in (None, ""))
    return joined or new_id("idem")


def correlation_id_for(parent_task_id: str, task_type: str) -> str:
    """docs/05 §3 的 correlation_id 形如 ``review-01J:impact``。"""
    return f"{parent_task_id}:{task_type}"


def worker_identity() -> str:
    """进程/工作节点标识，用于审计与恢复归属。"""
    return os.environ.get("CODEPILOT_WORKER_ID", f"{os.getpid()}@{os.environ.get('COMPUTERNAME', 'local')}")


__all__ = [
    "correlation_id_for",
    "new_artifact_id",
    "new_audit_id",
    "new_child_task_id",
    "new_eval_run_id",
    "new_event_id",
    "new_id",
    "new_idempotency_key",
    "new_message_id",
    "new_sandbox_run_id",
    "new_task_id",
    "new_trace_id",
    "new_ulid",
    "worker_identity",
]
