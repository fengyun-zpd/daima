"""路径规范化与安全检查（FR-001、SRS §4.2）。

规则：
- 拒绝绝对路径、Windows 盘符、``..`` 上溯、空路径、``\\x00``；
- 统一使用 POSIX 分隔符；
- 只接受允许的源码后缀（MVP 仅 Python，SRS §2.2）。
"""

from __future__ import annotations

import posixpath
import re

from domain.errors import CodePilotError, ErrorCode

ALLOWED_SUFFIXES: tuple[str, ...] = (".py",)
ALWAYS_ALLOWED_NAMES: frozenset[str] = frozenset(
    {"requirements.txt", "pyproject.toml", "README.md", "pytest.ini", "ruff.toml", "setup.cfg"}
)
_INVALID_CHARS = re.compile(r"[\x00-\x1f]")
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:")


def normalize_path(raw: str) -> str:
    """规范化并校验仓库内相对路径，失败抛 ``INVALID_INPUT``。"""
    if raw is None or not isinstance(raw, str):
        raise CodePilotError(ErrorCode.INVALID_INPUT, "路径必须是非空字符串")

    candidate = raw.strip().strip('"').replace("\\", "/")
    if not candidate:
        raise CodePilotError(ErrorCode.INVALID_INPUT, "路径为空")
    if _INVALID_CHARS.search(candidate):
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"路径包含非法控制字符：{raw!r}")
    if candidate.startswith("/") or _WINDOWS_DRIVE.match(candidate):
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"不允许绝对路径：{raw}")
    if candidate.startswith("//"):
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"不允许 UNC 路径：{raw}")

    normalized = posixpath.normpath(candidate)
    if normalized in {".", ""}:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"路径无效：{raw}")
    if normalized.startswith("../") or normalized == "..":
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"检测到路径穿越：{raw}")
    if "/../" in f"/{normalized}/":
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"检测到路径穿越：{raw}")
    return normalized


def strip_diff_prefix(path: str) -> str:
    """去除 unified diff 的 ``a/`` ``b/`` 前缀。"""
    value = path.strip()
    if value in {"/dev/null", "dev/null"}:
        return ""
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def is_python_path(path: str) -> bool:
    return path.endswith(".py")


def is_allowed_path(path: str) -> bool:
    if path in ALWAYS_ALLOWED_NAMES:
        return True
    return path.endswith(ALLOWED_SUFFIXES)


def ensure_allowed_path(path: str) -> str:
    normalized = normalize_path(path)
    if not is_allowed_path(normalized):
        raise CodePilotError(
            ErrorCode.INVALID_INPUT,
            f"MVP 仅支持 Python 源码与标准工程文件：{normalized}",
            details={"path": normalized},
        )
    return normalized


__all__ = [
    "ALLOWED_SUFFIXES",
    "ALWAYS_ALLOWED_NAMES",
    "ensure_allowed_path",
    "is_allowed_path",
    "is_python_path",
    "normalize_path",
    "strip_diff_prefix",
]
