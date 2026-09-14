"""沙箱安全自检（FR-050~FR-058）。

用法：
    python scripts/sandbox_check.py [--image codepilot-sandbox:local]

检查项：
1. 非 root 运行；
2. 无网络（连接外网失败）；
3. 只读根文件系统（写 /etc 失败）；
4. 可写的 /work 与 /tmp（受 tmpfs 大小限制）；
5. capabilities 已丢弃（尝试 mount 失败）；
6. 执行结束后容器与临时目录无残留；
7. 超时会被 kill 且状态为 timeout。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sandbox.base import SandboxLimits  # noqa: E402
from sandbox.docker import DockerSandbox  # noqa: E402

SAMPLE_FILES = {"app/main.py": "print('hello')\n", "tests/test_main.py": "def test_ok():\n    assert True\n"}


def _run(sandbox: DockerSandbox, argv: list[str], limits: SandboxLimits, *, name: str, patch=None):
    return sandbox.run(files=SAMPLE_FILES, argv=argv, name=name, limits=limits, patch=patch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 沙箱安全自检")
    parser.add_argument("--image", default="codepilot-sandbox:local")
    args = parser.parse_args(argv)

    sandbox = DockerSandbox(args.image)
    if not sandbox.available:
        print(f"沙箱不可用：{sandbox.unavailable_reason}", file=sys.stderr)
        return 2

    limits = SandboxLimits(image=args.image, timeout_seconds=30)
    results: list[tuple[str, bool, str]] = []

    whoami = _run(sandbox, ["/bin/sh", "-c", "id -u"], limits, name="chk-user")
    uid = whoami.stdout.strip().splitlines()[0].strip() if whoami.stdout.strip() else ""
    results.append(
        (
            "非 root 用户",
            uid.isdigit() and uid != "0",
            f"uid={uid or '<empty>'} exit={whoami.exit_code}",
        )
    )

    network = _run(
        sandbox,
        [
            "/bin/sh",
            "-c",
            "python -c \"import socket;socket.setdefaulttimeout(3);"
            "socket.create_connection(('1.1.1.1',80));print('NETWORK_OPEN')\" 2>&1 || echo NETWORK_BLOCKED",
        ],
        limits,
        name="chk-network",
    )
    results.append(
        (
            "无网络（network=none）",
            "NETWORK_OPEN" not in network.stdout,
            network.stdout.strip().splitlines()[-1] if network.stdout.strip() else "no output",
        )
    )

    readonly = _run(
        sandbox,
        ["/bin/sh", "-c", "(touch /etc/codepilot_probe && echo ROOTFS_WRITABLE) || echo ROOTFS_READONLY"],
        limits,
        name="chk-readonly",
    )
    results.append(
        ("只读根文件系统", "ROOTFS_WRITABLE" not in readonly.stdout, readonly.stdout.strip())
    )

    workdir = _run(
        sandbox,
        ["/bin/sh", "-c", "ls /work/app /work/tests && touch /work/probe && echo WORKDIR_WRITABLE"],
        limits,
        name="chk-workdir",
    )
    results.append(
        ("/work 可写且注入源码", "WORKDIR_WRITABLE" in workdir.stdout and "main.py" in workdir.stdout,
         workdir.stdout.strip().replace("\n", " | "))
    )

    caps = _run(
        sandbox,
        ["/bin/sh", "-c", "(mount -t tmpfs none /mnt 2>/dev/null && echo MOUNT_OK) || echo MOUNT_DENIED"],
        limits,
        name="chk-caps",
    )
    results.append(("capabilities 已丢弃", "MOUNT_OK" not in caps.stdout, caps.stdout.strip()))

    timeout_limits = SandboxLimits(image=args.image, timeout_seconds=2)
    timed = _run(sandbox, ["/bin/sh", "-c", "sleep 30"], timeout_limits, name="chk-timeout")
    results.append(
        (
            "超时被终止",
            timed.timed_out and timed.status == "timeout",
            f"status={timed.status} exit={timed.exit_code}",
        )
    )

    leftovers = subprocess.run(
        ["docker", "ps", "-a", "--filter", "label=codepilot.task", "--format", "{{.Names}}"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    volumes = subprocess.run(
        ["docker", "volume", "ls", "--filter", "label=codepilot.role", "--format", "{{.Name}}"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    results.append(
        (
            "容器与临时卷无残留",
            leftovers == "" and volumes == "",
            f"containers={leftovers or '无'} volumes={volumes or '无'}",
        )
    )

    print("沙箱安全自检")
    for name, ok, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}: {detail}")
    ok_all = all(item[1] for item in results)
    print("结论：", "通过" if ok_all else "存在问题")
    return 0 if ok_all else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
