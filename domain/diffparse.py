"""Unified Diff 解析与校验（FR-001、FR-002）。

只做确定性解析：
- 拒绝空 diff、非法 hunk 头、路径穿越与非 Python 变更；
- 输出每个文件的新行号视图，供规则引擎定位 ``file + line``；
- 依据 diff 重建"后像"内容（context + added 行），供 AST 分析；
  当后像不足以构成合法 AST 时由上层降级为文本扫描并告警（FR-021）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from domain.errors import CodePilotError, ErrorCode
from domain.safepath import ensure_allowed_path, is_python_path, normalize_path, strip_diff_prefix

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)


@dataclass(slots=True)
class HunkLine:
    origin: str  # ' ', '+', '-'
    content: str
    old_lineno: int | None
    new_lineno: int | None


@dataclass(slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    lines: list[HunkLine] = field(default_factory=list)

    @property
    def added_lines(self) -> list[int]:
        return [line.new_lineno for line in self.lines if line.origin == "+" and line.new_lineno is not None]

    @property
    def removed_lines(self) -> list[int]:
        return [line.old_lineno for line in self.lines if line.origin == "-" and line.old_lineno is not None]

    def post_image(self) -> list[tuple[int, str]]:
        """返回 (新行号, 内容) 列表，包含 context 与 added 行。"""
        return [
            (line.new_lineno, line.content)
            for line in self.lines
            if line.origin in {" ", "+"} and line.new_lineno is not None
        ]


@dataclass(slots=True)
class FileDiff:
    path: str
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)
    is_new_file: bool = False
    is_deleted_file: bool = False

    @property
    def added_lines(self) -> list[int]:
        return [line for hunk in self.hunks for line in hunk.added_lines]

    @property
    def removed_lines(self) -> list[int]:
        return [line for hunk in self.hunks for line in hunk.removed_lines]

    @property
    def changed_lines(self) -> set[int]:
        return set(self.added_lines)

    def post_image_lines(self) -> dict[int, str]:
        merged: dict[int, str] = {}
        for hunk in self.hunks:
            for lineno, content in hunk.post_image():
                merged[lineno] = content
        return merged

    def added_text(self) -> str:
        return "\n".join(
            line.content for hunk in self.hunks for line in hunk.lines if line.origin == "+"
        )


@dataclass(slots=True)
class ParsedDiff:
    files: list[FileDiff]
    raw: str

    @property
    def changed_files(self) -> list[str]:
        return [item.path for item in self.files]

    @property
    def python_files(self) -> list[FileDiff]:
        return [item for item in self.files if is_python_path(item.path)]

    @property
    def added_line_count(self) -> int:
        return sum(len(item.added_lines) for item in self.files)

    @property
    def removed_line_count(self) -> int:
        return sum(len(item.removed_lines) for item in self.files)

    def changed_lines_map(self) -> dict[str, set[int]]:
        return {item.path: item.changed_lines for item in self.files}


def parse_unified_diff(text: str, *, max_files: int = 200, max_bytes: int = 2_000_000) -> ParsedDiff:
    """解析 unified diff，任何非法结构都抛 ``INVALID_INPUT``。"""
    if not isinstance(text, str) or not text.strip():
        raise CodePilotError(ErrorCode.INVALID_INPUT, "Diff 内容为空")
    if len(text.encode("utf-8")) > max_bytes:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"Diff 超过大小上限 {max_bytes} 字节")

    lines = text.splitlines()
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: Hunk | None = None
    old_lineno = new_lineno = 0
    seen_paths: set[str] = set()

    for raw_line in lines:
        if raw_line.startswith("diff --git "):
            current = _start_file(raw_line, seen_paths, max_files)
            files.append(current)
            current_hunk = None
            continue
        if raw_line.startswith("--- ") and current is not None:
            current.old_path = _clean_header_path(raw_line[4:])
            continue
        if raw_line.startswith("+++ ") and current is not None:
            new_path = _clean_header_path(raw_line[4:])
            current.new_path = new_path
            if new_path:
                current.path = ensure_allowed_path(new_path)
            current.is_new_file = current.old_path in {"", "/dev/null", "dev/null"}
            current.is_deleted_file = new_path in {"", "/dev/null", "dev/null"}
            continue
        if raw_line.startswith("@@"):
            if current is None:
                raise CodePilotError(ErrorCode.INVALID_INPUT, f"hunk 出现在文件头之前：{raw_line}")
            current_hunk = _parse_hunk_header(raw_line)
            old_lineno = current_hunk.old_start
            new_lineno = current_hunk.new_start
            current.hunks.append(current_hunk)
            continue
        if current is None or current_hunk is None:
            # diff 头部元信息（index/new file mode 等）允许出现。
            if raw_line.startswith(("index ", "new file mode", "deleted file mode", "similarity index", "rename ")):
                continue
            if raw_line.strip() == "":
                continue
            raise CodePilotError(ErrorCode.INVALID_INPUT, f"无法解析的 diff 行：{raw_line[:80]}")
        if raw_line.startswith("\\"):
            continue
        if raw_line.startswith("+"):
            current_hunk.lines.append(HunkLine("+", raw_line[1:], None, new_lineno))
            new_lineno += 1
        elif raw_line.startswith("-"):
            current_hunk.lines.append(HunkLine("-", raw_line[1:], old_lineno, None))
            old_lineno += 1
        elif raw_line.startswith(" ") or raw_line == "":
            content = raw_line[1:] if raw_line.startswith(" ") else ""
            current_hunk.lines.append(HunkLine(" ", content, old_lineno, new_lineno))
            old_lineno += 1
            new_lineno += 1
        else:
            raise CodePilotError(ErrorCode.INVALID_INPUT, f"无法解析的 diff 行：{raw_line[:80]}")

    if not files:
        raise CodePilotError(ErrorCode.INVALID_INPUT, "Diff 中没有发现文件变更")

    for item in files:
        if not item.hunks and not item.is_deleted_file:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT,
                f"文件 {item.path or item.new_path} 没有 hunk",
            )

    if not any(is_python_path(item.path) for item in files):
        raise CodePilotError(ErrorCode.INVALID_INPUT, "变更集中没有 Python 文件")

    return ParsedDiff(files=files, raw=text)


def _start_file(header: str, seen: set[str], max_files: int) -> FileDiff:
    parts = header.split()
    raw_old = parts[2] if len(parts) > 2 else ""
    raw_new = parts[3] if len(parts) > 3 else ""
    old_path = strip_diff_prefix(raw_old)
    new_path = strip_diff_prefix(raw_new)
    candidate = new_path or old_path
    if not candidate:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"无法从 diff 头部解析路径：{header}")
    normalized = normalize_path(candidate)
    if normalized in seen:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"同一文件出现多个 diff 段：{normalized}")
    if len(seen) >= max_files:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"变更文件数超过上限 {max_files}")
    seen.add(normalized)
    return FileDiff(path=normalized, old_path=old_path, new_path=new_path)


def _clean_header_path(value: str) -> str:
    token = value.strip().split("\t")[0]
    if token in {"/dev/null", ""}:
        return token
    return strip_diff_prefix(token)


def _parse_hunk_header(line: str) -> Hunk:
    match = _HUNK_HEADER.match(line)
    if not match:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"非法 hunk 头：{line}")
    old_count = match.group("old_count")
    new_count = match.group("new_count")
    return Hunk(
        old_start=int(match.group("old_start")),
        old_count=int(old_count) if old_count is not None else 1,
        new_start=int(match.group("new_start")),
        new_count=int(new_count) if new_count is not None else 1,
        section=(match.group("section") or "").strip(),
    )


def build_unified_diff(path: str, old_text: str, new_text: str) -> str:
    """生成单文件 unified diff（Fix Agent 阶段使用）。"""
    import difflib

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    body = list(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}", n=3)
    )
    if not body:
        raise CodePilotError(ErrorCode.PATCH_INVALID, f"补丁为空：{path} 内容未发生变化")
    return "".join(body)


__all__ = [
    "FileDiff",
    "Hunk",
    "HunkLine",
    "ParsedDiff",
    "build_unified_diff",
    "parse_unified_diff",
]
