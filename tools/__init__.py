"""CodePilot 工具注册中心与工具实现。"""

from __future__ import annotations

from sandbox.base import SandboxExecutor, SandboxLimits
from tools.lint import GET_TEST_RESULT_SPEC, RUN_LINT_SPEC, SANDBOX_TOOL_SPECS
from tools.readonly import READONLY_TOOL_SPECS
from tools.registry import ToolCallContext, ToolCallResult, ToolRegistry, ToolSpec
from tools.write import WRITE_TOOL_SPECS

READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "read_file",
    "search_code",
    "get_diff",
    "list_files",
    "run_lint",
    "get_test_result",
)

WRITE_TOOL_NAMES: tuple[str, ...] = ("write_patch", "run_tests")


def build_default_registry() -> ToolRegistry:
    """构造默认工具集：6 个只读工具（FR-013）+ 2 个写工具（FR-014）。

    写工具只允许 Coordinator 在**已审批**的前提下调用（宪法第六条）：
    ``write_patch`` 写入任务分支，``run_tests`` 在沙箱内执行测试。
    """
    registry = ToolRegistry(list(READONLY_TOOL_SPECS) + list(SANDBOX_TOOL_SPECS) + list(WRITE_TOOL_SPECS))
    assert tuple(registry.names()) == tuple(sorted(READ_ONLY_TOOL_NAMES + WRITE_TOOL_NAMES))
    return registry


def build_tool_context(
    *,
    task_id: str,
    parent_task_id: str | None,
    agent_id: str,
    trace_id: str,
    workspace,
    session,
    budget,
    sandbox: SandboxExecutor | None = None,
    sandbox_limits: SandboxLimits | None = None,
    mode: str = "a2a",
    extra: dict | None = None,
) -> ToolCallContext:
    """构造工具调用上下文；沙箱与限制只能由编排器注入。"""
    return ToolCallContext(
        task_id=task_id,
        parent_task_id=parent_task_id,
        agent_id=agent_id,
        actor_role="agent",
        trace_id=trace_id,
        workspace=workspace,
        session=session,
        budget=budget,
        mode=mode,
        sandbox=sandbox,
        sandbox_limits=sandbox_limits,
        extra=dict(extra or {}),
    )


__all__ = [
    "GET_TEST_RESULT_SPEC",
    "READONLY_TOOL_SPECS",
    "READ_ONLY_TOOL_NAMES",
    "RUN_LINT_SPEC",
    "SANDBOX_TOOL_SPECS",
    "WRITE_TOOL_NAMES",
    "WRITE_TOOL_SPECS",
    "ToolCallContext",
    "ToolCallResult",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "build_tool_context",
]
