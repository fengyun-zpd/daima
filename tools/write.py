"""写工具（FR-014、FR-041~FR-043、宪法第六条）。

写工具是系统中唯一能改变代码的动作，必须同时满足：
1. 调用方是 Coordinator（Agent 一律拒绝）；
2. 目标分支是本任务的任务分支，`main/develop` 硬拒绝；
3. 已存在**绑定当前 patch_version** 的 approve 审批记录；
4. 补丁通过 `git apply --check`，失败即回滚。

所有调用都经 Tool Registry 记录参数哈希、结果哈希与审计事件。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import ToolAccess
from domain.errors import CodePilotError, ErrorCode
from domain.sanitize import sanitize_text
from repositories.repo_store import SyntheticRepo, assert_writable_branch
from tools.registry import ToolCallContext, ToolSpec


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WritePatchParams(_Params):
    patch_id: str = Field(min_length=8, description="fix_patch 主键")
    patch_version: int = Field(ge=1, description="审批绑定的补丁版本")
    patch_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    diff: str = Field(min_length=1, description="unified diff 内容")
    target_branch: str = Field(min_length=1, description="必须等于 codepilot/<task_id>")


class RunTestsParams(_Params):
    patch_id: str | None = Field(default=None)
    argv: list[str] = Field(default_factory=list, description="沙箱内执行的测试命令")


def write_patch(ctx: ToolCallContext, params: WritePatchParams) -> dict[str, Any]:
    """把已审批的补丁写入任务分支。"""
    task_id = ctx.parent_task_id or ctx.task_id
    if not ctx.extra.get("approved"):
        raise CodePilotError(
            ErrorCode.FORBIDDEN,
            "未审批的补丁禁止写入任务分支（FR-070）",
            details={"patch_id": params.patch_id},
        )
    approved_version = ctx.extra.get("approved_patch_version")
    if approved_version != params.patch_version:
        raise CodePilotError(
            ErrorCode.VERSION_CONFLICT,
            f"审批版本 {approved_version} 与补丁版本 {params.patch_version} 不一致（FR-072）",
            details={"approved": approved_version, "patch_version": params.patch_version},
        )
    if ctx.extra.get("approved_patch_hash") != params.patch_hash:
        raise CodePilotError(
            ErrorCode.VERSION_CONFLICT,
            "审批绑定的补丁哈希与当前补丁不一致",
            details={"patch_hash": params.patch_hash},
        )

    branch = assert_writable_branch(params.target_branch, task_id=task_id)
    repo = SyntheticRepo(ctx.extra.get("repo_root", "./var"), task_id)
    files = ctx.workspace.files
    repo.ensure(files, base_commit=str(ctx.extra.get("base_commit", "synthetic-base-001")))
    result = repo.apply_patch(params.diff, expected_branch=branch)
    return {
        "branch": result.branch,
        "commit": result.commit,
        "changed_files": result.changed_files,
        "checksums": result.checksums,
        "output": sanitize_text(result.output, max_length=1000),
        "patch_id": params.patch_id,
        "patch_version": params.patch_version,
    }


def run_tests(ctx: ToolCallContext, params: RunTestsParams) -> dict[str, Any]:
    """在沙箱内运行测试（写工具语义：会启动容器，但只读工作区内容）。"""
    if ctx.sandbox is None or ctx.sandbox_limits is None or not getattr(ctx.sandbox, "available", False):
        raise CodePilotError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "沙箱不可用：禁止在宿主机执行提交代码（宪法第八条）",
        )
    argv = params.argv or ["python", "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
    files = {path: content for path, content in ctx.workspace.files.items() if path.endswith(".py")}
    result = ctx.sandbox.run(
        files=files,
        argv=argv,
        name=f"run-tests-{ctx.task_id}",
        limits=ctx.sandbox_limits,
    )
    return {
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
    }


WRITE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="write_patch",
        description="把已审批的补丁写入任务分支（仅 Coordinator，需审批记录）",
        access=ToolAccess.WRITE,
        params_model=WritePatchParams,
        handler=write_patch,
    ),
    ToolSpec(
        name="run_tests",
        description="在沙箱内运行测试命令（仅 Coordinator）",
        access=ToolAccess.WRITE,
        params_model=RunTestsParams,
        handler=run_tests,
    ),
)

__all__ = [
    "WRITE_TOOL_SPECS",
    "RunTestsParams",
    "WritePatchParams",
    "run_tests",
    "write_patch",
]
