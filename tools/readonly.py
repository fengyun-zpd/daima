"""只读工具实现（FR-013）。

全部工具只读取编排器提供的合成工作区，绝不触碰宿主机文件系统，
也不会执行任何提交代码。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import ToolAccess
from tools.registry import ToolCallContext, ToolSpec


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFileParams(_Params):
    path: str = Field(description="工作区内的相对路径")
    offset: int = Field(default=1, ge=1, description="起始行号（1 起）")
    limit: int | None = Field(default=None, ge=1, le=2000, description="最大返回行数")


class SearchCodeParams(_Params):
    pattern: str = Field(min_length=1, max_length=200, description="正则表达式")
    file_pattern: str | None = Field(default=None, max_length=200, description="文件名正则过滤")
    only_changed: bool = Field(default=False, description="只搜索变更文件")
    max_results: int = Field(default=100, ge=1, le=500)


class GetDiffParams(_Params):
    path: str | None = Field(default=None, description="仅返回指定文件的 diff")


class ListFilesParams(_Params):
    pattern: str | None = Field(default=None, max_length=200)
    only_changed: bool = Field(default=False)


def read_file(ctx: ToolCallContext, params: ReadFileParams) -> dict[str, Any]:
    return ctx.workspace.read_file(params.path, offset=params.offset, limit=params.limit)


def search_code(ctx: ToolCallContext, params: SearchCodeParams) -> dict[str, Any]:
    return ctx.workspace.search_code(
        pattern=params.pattern,
        file_pattern=params.file_pattern,
        only_changed=params.only_changed,
        max_results=params.max_results,
    )


def get_diff(ctx: ToolCallContext, params: GetDiffParams) -> dict[str, Any]:
    return ctx.workspace.get_diff(path=params.path)


def list_files(ctx: ToolCallContext, params: ListFilesParams) -> dict[str, Any]:
    items = ctx.workspace.list_files(pattern=params.pattern, only_changed=params.only_changed)
    return {"files": items, "count": len(items)}


READONLY_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_file",
        description="读取工作区内文件内容（只读）",
        access=ToolAccess.READ,
        params_model=ReadFileParams,
        handler=read_file,
    ),
    ToolSpec(
        name="search_code",
        description="在工作区内按正则搜索代码（只读）",
        access=ToolAccess.READ,
        params_model=SearchCodeParams,
        handler=search_code,
    ),
    ToolSpec(
        name="get_diff",
        description="获取合成 diff 内容（只读）",
        access=ToolAccess.READ,
        params_model=GetDiffParams,
        handler=get_diff,
    ),
    ToolSpec(
        name="list_files",
        description="列出工作区文件（只读）",
        access=ToolAccess.READ,
        params_model=ListFilesParams,
        handler=list_files,
    ),
)

__all__ = [
    "READONLY_TOOL_SPECS",
    "GetDiffParams",
    "ListFilesParams",
    "ReadFileParams",
    "SearchCodeParams",
    "get_diff",
    "list_files",
    "read_file",
    "search_code",
]
