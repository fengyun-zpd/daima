"""CodePilot 沙箱层：执行器抽象、危险模式预审查与 Docker 实现。"""

from __future__ import annotations

from sandbox.base import (
    SandboxExecutor,
    SandboxLimits,
    SandboxResult,
    UnavailableSandbox,
)
from sandbox.prescan import PreScanOutcome, prescan_files, prescan_patch, prescan_text


def default_executor() -> SandboxExecutor:
    """返回可用的沙箱执行器；Docker 不可用时返回安全降级实现。"""
    from sandbox.docker import DockerSandbox

    executor = DockerSandbox()
    if executor.available:
        return executor
    return UnavailableSandbox(executor.unavailable_reason)


__all__ = [
    "PreScanOutcome",
    "SandboxExecutor",
    "SandboxLimits",
    "SandboxResult",
    "UnavailableSandbox",
    "default_executor",
    "prescan_files",
    "prescan_patch",
    "prescan_text",
]
