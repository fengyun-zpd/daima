"""需要沙箱或历史证据的工具（FR-013、FR-050~FR-054）。

- ``run_lint``：把工作区内容注入 Docker 沙箱运行 ruff，宿主机永不执行提交代码；
- ``get_test_result``：只读取已记录的 ``sandbox_run`` 证据，不触发任何执行。

两个工具都是只读语义：不写文件、不写分支、不改状态。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from domain.enums import ToolAccess
from domain.errors import CodePilotError, ErrorCode
from repositories.models import SandboxRun
from tools.registry import ToolCallContext, ToolSpec

RUFF_LINE = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<code>[A-Z]+\d+)\s*(?P<message>.*)$")
MAX_REPORTED_ISSUES = 50


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunLintParams(_Params):
    paths: list[str] = Field(default_factory=list, description="限定检查的文件；留空表示全部变更文件")
    max_issues: int = Field(default=MAX_REPORTED_ISSUES, ge=1, le=500)


class GetTestResultParams(_Params):
    sandbox_run_id: str | None = Field(default=None, description="指定沙箱运行 ID；留空表示最近一次")


def _require_sandbox(ctx: ToolCallContext) -> Any:
    if ctx.sandbox is None or ctx.sandbox_limits is None:
        raise CodePilotError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            "当前执行上下文未配置沙箱执行器",
            details={"tool": "run_lint"},
        )
    if not getattr(ctx.sandbox, "available", False):
        raise CodePilotError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"{getattr(ctx.sandbox, 'unavailable_reason', 'Docker 不可用')}：禁止在宿主机执行提交代码",
        )
    return ctx.sandbox


def run_lint(ctx: ToolCallContext, params: RunLintParams) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    workspace = ctx.workspace
    if params.paths:
        selected = {path: workspace.files[path] for path in params.paths if path in workspace.files}
        missing = [path for path in params.paths if path not in workspace.files]
        if missing:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT, f"工作区中不存在文件：{missing}", details={"missing": missing}
            )
    else:
        selected = {path: workspace.files[path] for path in workspace.changed_files}

    result = sandbox.run(
        files=selected,
        argv=["python", "-m", "ruff", "check", "--output-format", "concise", "--no-cache", "."],
        name=f"lint-{ctx.task_id}",
        limits=ctx.sandbox_limits,
    )

    issues: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        match = RUFF_LINE.match(line.strip())
        if not match:
            continue
        issues.append(
            {
                "file": match.group("file"),
                "line": int(match.group("line")),
                "code": match.group("code"),
                "message": match.group("message")[:200],
            }
        )
        if len(issues) >= params.max_issues:
            break

    return {
        "ok": result.exit_code == 0 and not result.timed_out,
        "tool": "ruff",
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "issue_count": len(issues),
        "issues": issues,
        "summary": _summarize(result, len(issues)),
        "sandbox": {
            "duration_ms": result.duration_ms,
            "network": ctx.sandbox_limits.network,
            "memory_limit_mb": ctx.sandbox_limits.memory_mb,
            "resource_usage": result.resource_usage,
        },
    }


def _summarize(result: Any, issue_count: int) -> str:
    if result.timed_out:
        return f"沙箱在 {result.resource_usage.get('timeout_seconds')} 秒内未完成，已终止"
    if result.exit_code == 0:
        return "ruff 通过"
    return f"ruff 报告 {issue_count} 个问题（exit_code={result.exit_code}）"


def get_test_result(ctx: ToolCallContext, params: GetTestResultParams) -> dict[str, Any]:
    """读取已记录在案的测试证据（只读，不触发执行）。"""
    statement = select(SandboxRun).order_by(SandboxRun.created_at.desc())
    if params.sandbox_run_id:
        statement = statement.where(SandboxRun.id == params.sandbox_run_id)
    elif ctx.parent_task_id:
        statement = statement.where(SandboxRun.task_id == ctx.parent_task_id)
    else:
        statement = statement.where(SandboxRun.task_id == ctx.task_id)
    rows = list(ctx.session.execute(statement.limit(10)).scalars())

    if not rows:
        return {
            "available": False,
            "reason": "尚无沙箱测试证据（Verify 子任务尚未运行）",
            "runs": [],
        }

    return {
        "available": True,
        "runs": [
            {
                "sandbox_run_id": row.id,
                "status": row.status,
                "exit_code": row.exit_code,
                "duration_ms": row.duration_ms,
                "coverage_before": row.coverage_before,
                "coverage_after": row.coverage_after,
                "coverage_delta": row.coverage_delta,
                "test_summary": row.test_summary or {},
                "resource_usage": row.resource_usage or {},
                "degraded": row.degraded,
            }
            for row in rows
        ],
        "latest": rows[0].id,
    }


RUN_LINT_SPEC = ToolSpec(
    name="run_lint",
    description="在 Docker 沙箱内运行 ruff（只读：不修改工作区）",
    access=ToolAccess.READ,
    params_model=RunLintParams,
    handler=run_lint,
)

GET_TEST_RESULT_SPEC = ToolSpec(
    name="get_test_result",
    description="读取已记录的沙箱测试与覆盖率证据（只读）",
    access=ToolAccess.READ,
    params_model=GetTestResultParams,
    handler=get_test_result,
)

SANDBOX_TOOL_SPECS: tuple[ToolSpec, ...] = (RUN_LINT_SPEC, GET_TEST_RESULT_SPEC)

__all__ = [
    "GET_TEST_RESULT_SPEC",
    "RUN_LINT_SPEC",
    "SANDBOX_TOOL_SPECS",
    "GetTestResultParams",
    "RunLintParams",
    "get_test_result",
    "run_lint",
]
