"""沙箱执行器抽象（宪法第八条、FR-050~FR-058）。

约定：
- 提交代码**只能**在 Docker 沙箱中运行，宿主机永不执行提交代码；
- Docker 不可用时返回 ``SANDBOX_UNAVAILABLE``，只允许继续规则与报告链路（docs/04 §4）；
- 沙箱参数（网络、用户、只读根、资源上限）由配置强制注入，不接受调用方覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from domain.errors import CodePilotError, ErrorCode


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """沙箱资源与安全参数（默认值来自 examples/.codepilot.yaml）。"""

    network: str = "none"
    user: str = "sandbox"
    read_only_rootfs: bool = True
    tmpfs_mb: int = 64
    cpu: float = 1.0
    memory_mb: int = 512
    disk_mb: int = 100
    timeout_seconds: int = 60
    drop_capabilities: str = "all"
    no_new_privileges: bool = True
    image: str = "codepilot-sandbox:local"
    pull_policy: str = "never"

    @classmethod
    def from_config(cls, section: Any) -> SandboxLimits:
        return cls(
            network=str(section.network),
            user=str(section.user),
            read_only_rootfs=bool(section.read_only_rootfs),
            tmpfs_mb=int(section.tmpfs_mb),
            cpu=float(section.cpu),
            memory_mb=int(section.memory_mb),
            disk_mb=int(section.disk_mb),
            timeout_seconds=int(section.timeout_seconds),
            drop_capabilities=str(section.drop_capabilities),
            no_new_privileges=bool(section.no_new_privileges),
            image=str(section.image),
            pull_policy=str(section.pull_policy),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "user": self.user,
            "read_only_rootfs": self.read_only_rootfs,
            "tmpfs_mb": self.tmpfs_mb,
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "disk_mb": self.disk_mb,
            "timeout_seconds": self.timeout_seconds,
            "drop_capabilities": self.drop_capabilities,
            "no_new_privileges": self.no_new_privileges,
            "image": self.image,
        }

    def assert_safe(self) -> None:
        """任何不满足安全底线的参数都必须在校验期被拒绝。"""
        if self.network != "none":
            raise CodePilotError(ErrorCode.SANDBOX_VIOLATION, "沙箱必须 network=none")
        if not self.read_only_rootfs:
            raise CodePilotError(ErrorCode.SANDBOX_VIOLATION, "沙箱根文件系统必须只读")
        if self.user in {"root", "0", "0:0"}:
            raise CodePilotError(ErrorCode.SANDBOX_VIOLATION, "沙箱必须非 root 运行")
        if self.drop_capabilities != "all":
            raise CodePilotError(ErrorCode.SANDBOX_VIOLATION, "沙箱必须丢弃全部 capabilities")
        if not self.no_new_privileges:
            raise CodePilotError(ErrorCode.SANDBOX_VIOLATION, "沙箱必须启用 no-new-privileges")


@dataclass(slots=True)
class SandboxResult:
    """一次沙箱执行的结果（输出必须已脱敏）。"""

    status: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    container_id: str | None = None
    timed_out: bool = False
    degraded: bool = False
    degraded_reason: str | None = None
    resource_usage: dict[str, Any] = field(default_factory=dict)
    files_written: dict[str, str] = field(default_factory=dict)
    patch_applied: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "resource_usage": dict(self.resource_usage),
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class SandboxExecutor(Protocol):
    """沙箱执行器协议；Docker 实现见 ``sandbox/docker.py``。"""

    @property
    def available(self) -> bool: ...

    def run(
        self,
        *,
        files: dict[str, str],
        argv: list[str],
        name: str,
        limits: SandboxLimits,
        patch: str | None = None,
    ) -> SandboxResult: ...


class UnavailableSandbox:
    """Docker 不可用时的安全降级实现：拒绝一切执行，绝不在宿主机执行提交代码。"""

    available = False

    def __init__(self, reason: str = "Docker 不可用") -> None:
        self.reason = reason

    def run(
        self,
        *,
        files: dict[str, str],
        argv: list[str],
        name: str,
        limits: SandboxLimits,
        patch: str | None = None,
    ) -> SandboxResult:
        raise CodePilotError(
            ErrorCode.SANDBOX_UNAVAILABLE,
            f"{self.reason}：禁止在宿主机执行提交代码（宪法第八条）",
            details={"argv": argv[:4], "file_count": len(files)},
        )


__all__ = [
    "SandboxExecutor",
    "SandboxLimits",
    "SandboxResult",
    "UnavailableSandbox",
]
