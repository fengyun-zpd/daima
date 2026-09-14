"""构建 CodePilot 沙箱镜像。

用法：
    python scripts/build_sandbox_image.py [--tag codepilot-sandbox:local]

Docker 不可用时返回非 0 并把原因写到 stderr：此时系统仍可运行规则与报告链路，
但不会降级为"在宿主机执行提交代码"（宪法第八条、docs/04 §4）。
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE_DIR = ROOT / "docker" / "sandbox"
DEFAULT_TAG = "codepilot-sandbox:local"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 CodePilot 沙箱镜像")
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    if not DOCKERFILE_DIR.joinpath("Dockerfile").exists():
        print(f"Dockerfile 不存在：{DOCKERFILE_DIR}", file=sys.stderr)
        return 2

    command = ["docker", "build", "-t", args.tag, str(DOCKERFILE_DIR)]
    if args.no_cache:
        command.insert(3, "--no-cache")
    print("执行：", " ".join(command))
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError:
        print("Docker CLI 不可用：沙箱无法启用，系统将运行安全降级路径", file=sys.stderr)
        return 3
    if completed.returncode != 0:
        print("镜像构建失败", file=sys.stderr)
        return completed.returncode

    verify = subprocess.run(
        ["docker", "run", "--rm", args.tag, "python", "-c", "import ruff, pytest; print('toolchain ok')"],
        check=False,
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        # ruff 没有可导入的模块入口，退化为命令行自检
        verify = subprocess.run(
            ["docker", "run", "--rm", args.tag, "sh", "-c", "python -m ruff --version && python -m pytest --version"],
            check=False,
            capture_output=True,
            text=True,
        )
    print(verify.stdout.strip() or verify.stderr.strip())
    print(f"镜像就绪：{args.tag}")
    return 0 if verify.returncode == 0 else verify.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
