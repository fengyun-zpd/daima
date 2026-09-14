"""Docker 沙箱执行器（宪法第八条、FR-050~FR-058）。

安全参数在创建容器时强制注入，调用方无法覆盖：
- ``network_mode="none"``（FR-051）；
- 非 root 用户 + ``no-new-privileges``（FR-057）；
- 只读根文件系统 + tmpfs 的 ``/tmp`` 与 ``/work``（FR-056）；
- 磁盘上限通过 ``/work`` tmpfs 大小强制（FR-052）；
- ``cap_drop=["ALL"]``（FR-058）；
- CPU / 内存 / 进程数上限（FR-052）；
- 超时后 kill 容器，最后一律删除容器与临时目录（FR-053）。

提交代码只通过容器 API（``put_archive``）注入，不使用宿主机可写挂载，
所有产物在容器销毁后清理，宿主机永不经由本模块执行提交代码。
"""

from __future__ import annotations

import contextlib
import io
import os
import tarfile
import threading
import time
from typing import Any

from domain.errors import CodePilotError, ErrorCode
from domain.sanitize import sanitize_text
from sandbox.base import SandboxLimits, SandboxResult

DEFAULT_IMAGE = os.environ.get("CODEPILOT_SANDBOX_IMAGE", "codepilot-sandbox:local")
WORK_DIR = "/work"
SRC_DIR = "/src"
#: 与 docker/sandbox/Dockerfile 中的 sandbox 用户保持一致。
SANDBOX_UID = 10001
SANDBOX_GID = 10001
#: 补丁在沙箱工作目录内的相对路径（tmpfs，随容器销毁）。
PATCH_RELATIVE_PATH = ".codepilot/patch.diff"


class DockerSandbox:
    """基于 Docker Engine 的沙箱执行器。"""

    def __init__(self, image: str | None = None, *, client: Any | None = None) -> None:
        self.image = image or DEFAULT_IMAGE
        self._client = client
        self._probe: bool | None = None
        self._reason = ""

    # ---- 可用性 -----------------------------------------------------------------
    @property
    def client(self) -> Any | None:
        if self._client is None:
            try:
                import docker

                self._client = docker.from_env()
            except Exception:  # noqa: BLE001 - 依赖缺失或环境不可用
                self._reason = "docker SDK 不可用或无法连接 Docker Engine"
                return None
        return self._client

    @property
    def available(self) -> bool:
        if self._probe is None:
            self._probe = self._probe_available()
        return self._probe

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        return self._reason or "Docker 不可用"

    def _probe_available(self) -> bool:
        connection = self.client
        if connection is None:
            return False
        try:
            connection.ping()
        except Exception as exc:  # noqa: BLE001
            self._reason = f"Docker daemon 无响应：{exc}"
            return False
        try:
            connection.images.get(self.image)
        except Exception:  # noqa: BLE001
            self._reason = f"沙箱镜像不存在：{self.image}（请先执行 scripts/build_sandbox_image.py）"
            return False
        return True

    # ---- 执行 -------------------------------------------------------------------
    def run(
        self,
        *,
        files: dict[str, str],
        argv: list[str],
        name: str,
        limits: SandboxLimits,
        patch: str | None = None,
    ) -> SandboxResult:
        limits.assert_safe()
        if not self.available:
            raise CodePilotError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"{self.unavailable_reason}：禁止在宿主机执行提交代码（宪法第八条）",
                details={"argv": argv[:4], "file_count": len(files)},
            )
        if not argv:
            raise CodePilotError(ErrorCode.INVALID_INPUT, "沙箱命令为空")

        connection = self.client
        assert connection is not None

        # 提交代码只通过容器 API 注入：宿主机不落盘任何提交代码，也无需临时目录。
        # FR-053 的清理责任因此只剩"销毁容器"这一步（见 finally）。
        # 提交代码只经 Docker 卷注入：宿主机不落盘任何提交代码，也无需临时目录。
        archive = _build_archive(files, patch, limits)
        container = None
        volume = None
        helper = None
        started = time.perf_counter()
        volume_name = f"codepilot-src-{name}-{os.getpid()}"[:63]
        try:
            volume, helper = _populate_volume(connection, volume_name, archive, self.image)
            container = connection.containers.create(
                image=self.image,
                # 只读根文件系统下无法向根层写入，因此先以长驻命令启动，
                # 再把只读卷中的源码复制到可写 tmpfs 的 /work，最后 exec 目标命令。
                command=["sleep", "infinity"],
                name=f"codepilot-{name}"[:63],
                working_dir=WORK_DIR,
                user=limits.user,
                network_mode=limits.network,
                read_only=limits.read_only_rootfs,
                # /tmp 与 /work 均为 tmpfs：/work 可写且受磁盘上限约束（FR-052/FR-056）。
                tmpfs={
                    "/tmp": f"size={limits.tmpfs_mb}m,mode=1777",
                    WORK_DIR: f"size={limits.disk_mb}m,mode=1777",
                },
                volumes={volume_name: {"bind": SRC_DIR, "mode": "ro"}},
                mem_limit=f"{limits.memory_mb}m",
                memswap_limit=f"{limits.memory_mb}m",
                nano_cpus=int(limits.cpu * 1_000_000_000),
                pids_limit=256,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                environment={
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "HOME": "/tmp",
                    "TMPDIR": "/tmp",
                    # /work 是 tmpfs（root 属主），非 root 的 git 需要显式信任该目录，
                    # 否则会以 "dubious ownership" 拒绝操作。
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": WORK_DIR,
                    "GIT_CONFIG_KEY_1": "init.defaultBranch",
                    "GIT_CONFIG_VALUE_1": "main",
                },
                labels={"codepilot.task": name},
                detach=True,
            )
            container.start()

            exit_code, timed_out, stdout, stderr = _exec_with_timeout(
                container,
                _bootstrap_command(argv),
                timeout_seconds=limits.timeout_seconds,
                workdir=WORK_DIR,
            )
            stats = _stats(container) if not timed_out else {}
            duration_ms = int((time.perf_counter() - started) * 1000)

            return SandboxResult(
                status="timeout" if timed_out else ("passed" if exit_code == 0 else "failed"),
                exit_code=exit_code,
                stdout=sanitize_text(stdout),
                stderr=sanitize_text(stderr),
                duration_ms=duration_ms,
                container_id=getattr(container, "id", None),
                timed_out=timed_out,
                resource_usage={
                    **stats,
                    "cpu_limit": limits.cpu,
                    "memory_limit_mb": limits.memory_mb,
                    "disk_limit_mb": limits.disk_mb,
                    "tmpfs_mb": limits.tmpfs_mb,
                    "network": limits.network,
                    "timeout_seconds": limits.timeout_seconds,
                },
            )
        finally:
            # FR-053：无论成功失败都必须销毁容器、辅助容器与临时卷，宿主机无残留。
            for resource, remover in (
                (helper, lambda item: item.remove(force=True, v=True)),
                (container, lambda item: item.remove(force=True, v=True)),
            ):
                if resource is None:
                    continue
                with contextlib.suppress(Exception):
                    if resource is container:
                        resource.kill()
                with contextlib.suppress(Exception):
                    remover(resource)
            if volume is not None:
                with contextlib.suppress(Exception):
                    volume.remove(force=True)


def _populate_volume(
    connection: Any, volume_name: str, archive: bytes, image: str
) -> tuple[Any, Any]:
    """用一次性辅助容器把源码写入命名卷，再以只读方式挂载给沙箱容器。

    这样提交代码既不落宿主机磁盘，也不违反沙箱的只读根文件系统约束（FR-056）。
    """
    volume = connection.volumes.create(name=volume_name, labels={"codepilot.role": "source"})
    helper = connection.containers.create(
        image=image,
        command=["sleep", "infinity"],
        volumes={volume_name: {"bind": "/data", "mode": "rw"}},
        labels={"codepilot.role": "source-loader"},
        detach=True,
    )
    helper.start()
    helper.put_archive("/data", archive)
    helper.remove(force=True, v=True)
    return volume, None


def _bootstrap_command(argv: list[str]) -> list[str]:
    """把只读源码卷复制到可写工作目录后执行目标命令。

    使用 ``cp -R``（不保留属主）而不是 ``cp -a``：沙箱以非 root 运行且已丢弃 capabilities，
    无法保留 root 属主；tar 成员已归属沙箱用户，复制后目录仍可写。
    """
    import shlex

    inner = " ".join(shlex.quote(item) for item in argv)
    return ["/bin/sh", "-c", f"cp -R {SRC_DIR}/. {WORK_DIR}/ && cd {WORK_DIR} && exec {inner}"]


def _exec_with_timeout(
    container: Any,
    argv: list[str],
    *,
    timeout_seconds: int,
    workdir: str,
) -> tuple[int | None, bool, str, str]:
    """在容器内执行命令，超时则终止并返回 ``timed_out=True``。

    双重超时保护：容器内 ``timeout`` 命令负责正常终止（退出码 124），
    外层线程 join 超时则直接 kill 容器，保证 1 秒级停止时延（NFR-008）。
    """
    outcome: dict[str, Any] = {}

    def _run() -> None:
        try:
            result = container.exec_run(
                ["timeout", "-k", "5", str(timeout_seconds), *argv],
                workdir=workdir,
                demux=True,
            )
            outcome["exit_code"] = int(result.exit_code)
            stdout, stderr = result.output if isinstance(result.output, tuple) else (result.output, b"")
            outcome["stdout"] = _decode(stdout or b"")
            outcome["stderr"] = _decode(stderr or b"")
        except Exception as exc:  # noqa: BLE001 - 执行通道异常统一记录
            outcome["exit_code"] = None
            outcome["stdout"] = ""
            outcome["stderr"] = f"exec failed: {type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds + 10)
    if thread.is_alive():
        with contextlib.suppress(Exception):
            container.kill()
        thread.join(timeout=5)
        return None, True, outcome.get("stdout", ""), outcome.get("stderr", "执行超时，容器已终止")

    exit_code = outcome.get("exit_code")
    timed_out = exit_code == 124
    return exit_code, timed_out, outcome.get("stdout", ""), outcome.get("stderr", "")


def _build_archive(files: dict[str, str], patch: str | None, limits: SandboxLimits) -> bytes:
    """构造注入 /work 的 tar：路径相对工作目录，杜绝写出工作区（FR-056）。"""
    from domain.safepath import ensure_allowed_path

    total = 0
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path, content in sorted(files.items()):
            safe = ensure_allowed_path(path)
            payload = content.encode("utf-8")
            total += len(payload)
            if total > limits.disk_mb * 1024 * 1024:
                raise CodePilotError(
                    ErrorCode.INVALID_INPUT,
                    f"注入沙箱的内容超过磁盘上限 {limits.disk_mb}MB",
                    details={"total_bytes": total},
                )
            _add(archive, safe, payload)
        if patch:
            _add(archive, PATCH_RELATIVE_PATH, patch.encode("utf-8"))
    return buffer.getvalue()


def _add(archive: tarfile.TarFile, name: str, payload: bytes, *, mode: int = 0o644) -> None:
    """写入 tar 成员：统一归属沙箱用户，保证复制到 /work 后仍可写。"""
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = mode
    info.mtime = 0
    info.uid = SANDBOX_UID
    info.gid = SANDBOX_GID
    info.uname = "sandbox"
    info.gname = "sandbox"
    archive.addfile(info, io.BytesIO(payload))


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _stats(container: Any) -> dict[str, Any]:
    try:
        stats = container.stats(stream=False)
    except Exception:  # noqa: BLE001
        return {}
    memory = stats.get("memory_stats", {}) or {}
    cpu = stats.get("cpu_stats", {}) or {}
    return {
        "memory_usage_bytes": memory.get("usage"),
        "memory_limit_bytes": memory.get("limit"),
        "cpu_total_usage": (cpu.get("cpu_usage") or {}).get("total_usage"),
    }


__all__ = ["DEFAULT_IMAGE", "PATCH_RELATIVE_PATH", "SRC_DIR", "WORK_DIR", "DockerSandbox"]
