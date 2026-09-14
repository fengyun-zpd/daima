"""合成审查工作区：把 Diff / ZIP 输入转换为只读内存工作区。

宪法第一条与 SRS §4.2：
- 审查输入是合成 Diff 或 ZIP，不读取真实仓库；
- Agent 只能通过 Tool Registry 读取本工作区，不能直接访问宿主机文件系统；
- ``offline`` 与 ``a2a`` 使用同一个工作区模型，保证 single/a2a 可比（FR-099）。
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from domain.diffparse import ParsedDiff, parse_unified_diff
from domain.enums import ContextPolicy, InputType
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_task_id
from domain.safepath import (
    ensure_allowed_path,
    is_python_path,
    normalize_path,
)

# 本地工作台允许拖入至多 100 MiB 的代码文件或 ZIP；解压后的 Python 工作区也受此限制。
# Diff 为单文件自动补齐 unified-diff 行前缀后可能显著变大，故单独允许 200 MiB。
MAX_WORKSPACE_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 500
MAX_FILE_BYTES = MAX_WORKSPACE_BYTES
MAX_DIFF_BYTES = 200 * 1024 * 1024


@dataclass(slots=True)
class Workspace:
    task_id: str
    base_commit: str
    input_type: InputType
    context_policy: ContextPolicy
    files: dict[str, str] = field(default_factory=dict)
    changed_lines: dict[str, set[int]] = field(default_factory=dict)
    diff_text: str = ""
    partial: bool = True
    warnings: list[str] = field(default_factory=list)

    # ---- 构造 -------------------------------------------------------------------
    @classmethod
    def from_diff(
        cls,
        *,
        task_id: str,
        diff_text: str,
        base_commit: str,
        context_policy: ContextPolicy = ContextPolicy.FUNCTION,
    ) -> tuple[Workspace, ParsedDiff]:
        parsed = parse_unified_diff(diff_text)
        files: dict[str, str] = {}
        changed: dict[str, set[int]] = {}
        warnings: list[str] = []

        for item in parsed.files:
            if item.is_deleted_file:
                continue
            post_image = item.post_image_lines()
            if post_image:
                max_line = max(post_image)
                buffer: list[str] = [""] * (max_line + 1)
                for lineno, content in post_image.items():
                    buffer[lineno] = content
                text = "\n".join(buffer[1:])
                # diff 重建的后像必须补齐结尾换行：否则 Fix Agent 依据该内容生成的补丁，
                # 在沙箱里会因为"最后一行缺少换行符"而 `git apply --check` 失败
                # （git 把整段 hunk 判为不匹配）。ZIP 输入的文件本身就带结尾换行，
                # 这里显式对齐，保证两种输入形态在沙箱中行为一致。
                if text and not text.endswith("\n"):
                    text += "\n"
                files[item.path] = text
            else:
                files[item.path] = ""
            changed[item.path] = item.changed_lines

        if any(len(item.hunks) == 0 for item in parsed.files if not item.is_deleted_file):
            warnings.append("存在没有 hunk 的文件变更段")
        warnings.append("工作区由 diff 重建，仅包含变更附近上下文（partial=True）")

        workspace = cls(
            task_id=task_id,
            base_commit=base_commit,
            input_type=InputType.DIFF,
            context_policy=context_policy,
            files=files,
            changed_lines=changed,
            diff_text=diff_text,
            partial=True,
            warnings=warnings,
        )
        workspace._assert_size()
        return workspace, parsed

    @classmethod
    def from_zip(
        cls,
        *,
        task_id: str,
        zip_bytes: bytes,
        base_commit: str,
        context_policy: ContextPolicy = ContextPolicy.FUNCTION,
    ) -> Workspace:
        files: dict[str, str] = {}
        changed: dict[str, set[int]] = {}
        total = 0

        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile as exc:
            raise CodePilotError(ErrorCode.INVALID_INPUT, f"ZIP 文件无法解析：{exc}") from exc

        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise CodePilotError(
                    ErrorCode.INVALID_INPUT, f"ZIP 条目数超过上限 {MAX_ZIP_ENTRIES}"
                )
            for info in infos:
                if info.is_dir():
                    continue
                if info.filename.startswith("/") or ".." in info.filename.replace("\\", "/").split("/"):
                    raise CodePilotError(
                        ErrorCode.INVALID_INPUT, f"ZIP 中包含非法路径：{info.filename}"
                    )
                path = normalize_path(info.filename)
                if not is_python_path(path):
                    continue
                ensure_allowed_path(path)
                if info.file_size > MAX_FILE_BYTES:
                    raise CodePilotError(
                        ErrorCode.INVALID_INPUT,
                        f"ZIP 内文件超过大小上限：{path} ({info.file_size} 字节)",
                    )
                raw = archive.read(info)
                total += len(raw)
                if total > MAX_WORKSPACE_BYTES:
                    raise CodePilotError(
                        ErrorCode.INVALID_INPUT, f"ZIP 解压后总大小超过上限 {MAX_WORKSPACE_BYTES} 字节"
                    )
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise CodePilotError(
                        ErrorCode.INVALID_INPUT, f"ZIP 内文件不是 UTF-8 文本：{path}"
                    ) from exc
                files[path] = content
                changed[path] = set(range(1, len(content.splitlines()) + 1))

        if not files:
            raise CodePilotError(ErrorCode.INVALID_INPUT, "ZIP 中没有 Python 文件")

        workspace = cls(
            task_id=task_id,
            base_commit=base_commit,
            input_type=InputType.ZIP,
            context_policy=context_policy,
            files=files,
            changed_lines=changed,
            diff_text="",
            partial=False,
            warnings=["ZIP 输入没有基线，全部文件视为变更文件"],
        )
        workspace._assert_size()
        return workspace

    def _assert_size(self) -> None:
        total = sum(len(content.encode("utf-8")) for content in self.files.values())
        if total > MAX_WORKSPACE_BYTES:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT, f"工作区超过大小上限 {MAX_WORKSPACE_BYTES} 字节"
            )

    # ---- 只读查询（供 Tool Registry 使用） ---------------------------------------
    @property
    def changed_files(self) -> list[str]:
        return sorted(self.changed_lines)

    def has_file(self, path: str) -> bool:
        return normalize_path(path) in self.files

    def read_file(self, path: str, *, offset: int = 1, limit: int | None = None) -> dict[str, Any]:
        normalized = normalize_path(path)
        if normalized not in self.files:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT,
                f"工作区中不存在文件：{normalized}",
                details={"path": normalized},
            )
        lines = self.files[normalized].splitlines()
        start = max(1, offset)
        end = len(lines) if limit is None else min(len(lines), start + limit - 1)
        selected = lines[start - 1 : end]
        return {
            "path": normalized,
            "offset": start,
            "limit": end - start + 1,
            "total_lines": len(lines),
            "content": "\n".join(selected),
            "truncated": end < len(lines),
            "changed": normalized in self.changed_lines,
        }

    def list_files(self, *, pattern: str | None = None, only_changed: bool = False) -> list[dict[str, Any]]:
        regex = re.compile(pattern) if pattern else None
        results: list[dict[str, Any]] = []
        for path in sorted(self.files):
            if regex is not None and not regex.search(path):
                continue
            if only_changed and path not in self.changed_lines:
                continue
            results.append(
                {
                    "path": path,
                    "lines": len(self.files[path].splitlines()),
                    "changed": path in self.changed_lines,
                }
            )
        return results

    def search_code(
        self,
        *,
        pattern: str,
        file_pattern: str | None = None,
        only_changed: bool = False,
        max_results: int = 200,
    ) -> dict[str, Any]:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise CodePilotError(ErrorCode.INVALID_INPUT, f"非法正则表达式：{exc}") from exc
        file_regex = re.compile(file_pattern) if file_pattern else None

        matches: list[dict[str, Any]] = []
        for path in sorted(self.files):
            if file_regex is not None and not file_regex.search(path):
                continue
            if only_changed and path not in self.changed_lines:
                continue
            for lineno, line in enumerate(self.files[path].splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "path": path,
                            "line": lineno,
                            "text": line.strip()[:300],
                            "in_changed_lines": lineno in self.changed_lines.get(path, set()),
                        }
                    )
                    if len(matches) >= max_results:
                        return {"matches": matches, "truncated": True, "count": len(matches)}
        return {"matches": matches, "truncated": False, "count": len(matches)}

    def get_diff(self, *, path: str | None = None) -> dict[str, Any]:
        if self.input_type is InputType.ZIP and not self.diff_text:
            return {
                "diff": "",
                "files": self.changed_files,
                "note": "ZIP 输入没有基线 diff，全部文件视为变更",
                "added_lines": sum(len(v) for v in self.changed_lines.values()),
                "removed_lines": 0,
            }
        if path is None:
            return {
                "diff": self.diff_text,
                "files": self.changed_files,
                "added_lines": sum(len(v) for v in self.changed_lines.values()),
                "removed_lines": 0,
            }
        normalized = normalize_path(path)
        segments = [seg for seg in self.diff_text.split("diff --git ") if seg.strip()]
        for segment in segments:
            if normalized in segment.splitlines()[0]:
                return {
                    "diff": f"diff --git {segment}".rstrip() + "\n",
                    "files": [normalized],
                    "added_lines": len(self.changed_lines.get(normalized, set())),
                    "removed_lines": 0,
                }
        return {"diff": "", "files": [], "added_lines": 0, "removed_lines": 0}

    def changed_line_set(self, path: str) -> set[int]:
        return self.changed_lines.get(normalize_path(path), set())

    def in_changed_lines(self, path: str, line: int) -> bool:
        return line in self.changed_line_set(path)

    @property
    def python_files(self) -> list[str]:
        return [path for path in sorted(self.files) if is_python_path(path)]

    # ---- 序列化 -----------------------------------------------------------------
    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "base_commit": self.base_commit,
            "input_type": str(self.input_type),
            "context_policy": str(self.context_policy),
            "files": self.files,
            "changed_lines": {path: sorted(lines) for path, lines in self.changed_lines.items()},
            "diff_text": self.diff_text,
            "partial": self.partial,
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Workspace:
        return cls(
            task_id=payload["task_id"],
            base_commit=payload["base_commit"],
            input_type=InputType(payload["input_type"]),
            context_policy=ContextPolicy(payload["context_policy"]),
            files=dict(payload.get("files", {})),
            changed_lines={
                path: set(lines) for path, lines in payload.get("changed_lines", {}).items()
            },
            diff_text=payload.get("diff_text", ""),
            partial=bool(payload.get("partial", True)),
            warnings=list(payload.get("warnings", [])),
        )


def decode_input_content(input_type: InputType, content: str) -> bytes | str:
    """API 请求中的 ``content`` 解码。

    - ``diff``：直接使用文本；
    - ``zip``：接受 base64 编码的 ZIP。
    """
    if input_type is InputType.DIFF:
        if len(content.encode("utf-8")) > MAX_DIFF_BYTES:
            raise CodePilotError(ErrorCode.INVALID_INPUT, "Diff 超过大小上限")
        return content
    try:
        decoded = base64.b64decode(content, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CodePilotError(ErrorCode.INVALID_INPUT, f"ZIP 内容必须是合法 base64：{exc}") from exc
    return decoded


def build_workspace(
    *,
    input_type: InputType,
    content: str,
    base_commit: str,
    context_policy: ContextPolicy = ContextPolicy.FUNCTION,
    task_id: str | None = None,
) -> tuple[Workspace, ParsedDiff | None]:
    identifier = task_id or new_task_id()
    if input_type is InputType.DIFF:
        text = decode_input_content(input_type, content)
        assert isinstance(text, str)
        workspace, parsed = Workspace.from_diff(
            task_id=identifier,
            diff_text=text,
            base_commit=base_commit,
            context_policy=context_policy,
        )
        return workspace, parsed

    blob = decode_input_content(input_type, content)
    assert isinstance(blob, bytes)
    workspace = Workspace.from_zip(
        task_id=identifier,
        zip_bytes=blob,
        base_commit=base_commit,
        context_policy=context_policy,
    )
    return workspace, None


__all__ = [
    "MAX_WORKSPACE_BYTES",
    "Workspace",
    "build_workspace",
    "decode_input_content",
]
