"""输出脱敏（FR-060、NFR-006、NFR-009）。

进入日志、审计、SSE 或 API 之前，路径、IP 和密钥必须被替换。
本模块是纯函数式的确定性边界（宪法第九条）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Any

from domain.clock import isoformat

PATH_PLACEHOLDER = "<PATH>"
IP_PLACEHOLDER = "<IP>"
SECRET_PLACEHOLDER = "<REDACTED>"
EMAIL_PLACEHOLDER = "<EMAIL>"

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), SECRET_PLACEHOLDER),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{10,}\b"), SECRET_PLACEHOLDER),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), SECRET_PLACEHOLDER),
    (re.compile(r"(?i)\b(authorization|bearer)\s*[:=]?\s*[A-Za-z0-9._\-]{8,}"), SECRET_PLACEHOLDER),
    (
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?key|secret|password|passwd|token|key)\b\s*[:=]\s*"
            r"['\"]?[^\s'\"]{4,}['\"]?"
        ),
        SECRET_PLACEHOLDER,
    ),
)

_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s:*?\"<>|]+\\)*[^\\\s:*?\"<>|]*")
_POSIX_PATH = re.compile(r"(?<![\w.])/(?:home|Users|root|var|opt|srv|tmp|etc|usr)(?:/[\w.\-]+)+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")


def sanitize_text(text: str | None, *, max_length: int = 20_000) -> str:
    """对文本做确定性脱敏，并截断到安全长度。"""
    if not text:
        return ""
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    result = _WINDOWS_PATH.sub(PATH_PLACEHOLDER, result)
    result = _POSIX_PATH.sub(PATH_PLACEHOLDER, result)
    result = _EMAIL.sub(EMAIL_PLACEHOLDER, result)
    result = _IPV4.sub(IP_PLACEHOLDER, result)
    if len(result) > max_length:
        result = result[:max_length] + f"\n...[truncated {len(result) - max_length} chars]"
    return result


def sanitize_structured(value: Any, *, max_string: int = 500) -> Any:
    """递归脱敏结构化数据，并保证结果可 JSON 序列化（审计负载要求）。"""
    if isinstance(value, str):
        return sanitize_text(value, max_length=max_string)
    if isinstance(value, dict):
        return {
            str(key): sanitize_structured(item, max_string=max_string) for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_structured(item, max_string=max_string) for item in value]
    if isinstance(value, datetime):
        return isoformat(value)
    if isinstance(value, Enum):
        return str(value.value)
    return value


def payload_hash(payload: Any) -> str:
    """参数/结果哈希：审计中不保存完整参数原文（FR-015）。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def summarize_payload(payload: Any, *, max_keys: int = 20, max_string: int = 200) -> dict[str, Any]:
    """只保留可审计的摘要：键名、类型和脱敏后的短值。"""
    if not isinstance(payload, dict):
        return {"value": sanitize_text(str(payload), max_length=max_string)}
    summary: dict[str, Any] = {}
    for index, (key, value) in enumerate(payload.items()):
        if index >= max_keys:
            summary["__truncated__"] = True
            break
        if isinstance(value, (dict, list)):
            summary[str(key)] = {
                "type": type(value).__name__,
                "size": len(value),
                "hash": payload_hash(value),
            }
        elif isinstance(value, str):
            summary[str(key)] = sanitize_text(value, max_length=max_string)
        else:
            summary[str(key)] = value
    return summary


__all__ = [
    "EMAIL_PLACEHOLDER",
    "IP_PLACEHOLDER",
    "PATH_PLACEHOLDER",
    "SECRET_PLACEHOLDER",
    "payload_hash",
    "sanitize_structured",
    "sanitize_text",
    "summarize_payload",
]
