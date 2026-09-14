"""一键运行交付验收检查（区分 PASS / FAIL / SKIP，不把跳过写成通过）。

用法：
    python scripts/run_checks.py                    # 全量（Docker / PostgreSQL 可用时真实执行）
    python scripts/run_checks.py --fast             # 跳过 Docker 与 PostgreSQL 相关检查
    python scripts/run_checks.py --static           # 只跑静态检查（ruff/compileall/schema/web）
    python scripts/run_checks.py --skip-docker      # 只跳过 Docker 相关检查
    python scripts/run_checks.py --skip-postgres    # 只跳过真实 PostgreSQL 检查
    python scripts/run_checks.py --skip-tests       # 只跳过 pytest
    python scripts/run_checks.py --with-fix         # 评测冒烟额外执行 Fix/Verify（需要 Docker）

检查项与依赖：

| 检查 | 依赖 | 跳过条件 |
|---|---|---|
| ruff / compileall / json-schema / web-check | 无 | 仅 `--static` 之外不会被跳过 |
| pytest（全量） | 无（PG/Docker 相关用例自行 skip） | `--skip-tests` / `--fast` / `--static` |
| db-check（真实 PostgreSQL 结构自检） | PostgreSQL | `--skip-postgres` / `--fast` / `--static`，或 PostgreSQL 不可达 |
| sandbox-check（真实 Docker 沙箱） | Docker | `--skip-docker` / `--fast` / `--static`，或 Docker 不可达 |
| fault-matrix | Docker（沙箱场景） | Docker 不可达时自动退化为 `--no-sandbox` 并标注 |
| evals-smoke | 无 | 不会被跳过 |

结论会明确区分：`full pass` / `fast pass` / `static pass`，并单独打印
`Docker：未执行` 与 `PostgreSQL：未执行`，避免把"跳过"当成"通过"。
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


@dataclasses.dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    elapsed: float = 0.0
    detail: str = ""


def _run(name: str, command: list[str], *, skip_reason: str | None = None, detail: str = "") -> CheckResult:
    if skip_reason is not None:
        print(f"[skip] {name}（{skip_reason}）")
        return CheckResult(name=name, status=SKIP, detail=skip_reason)
    started = time.perf_counter()
    print(f"[run ] {name}: {' '.join(command)}")
    completed = subprocess.run(command, cwd=ROOT, check=False)
    elapsed = time.perf_counter() - started
    ok = completed.returncode == 0
    print(f"[{'ok  ' if ok else 'FAIL'}] {name} （{elapsed:.1f}s）")
    if not ok and not detail:
        detail = f"退出码 {completed.returncode}"
    return CheckResult(name=name, status=PASS if ok else FAIL, elapsed=elapsed, detail=detail)


def docker_available() -> bool:
    """Docker CLI 存在且 daemon 可达。"""
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def postgres_available() -> tuple[bool, str]:
    """真实 PostgreSQL 是否可达（用于 db-check 与真实库用例）。"""
    import os

    url = os.environ.get(
        "CODEPILOT_TEST_POSTGRES_URL",
        "postgresql+psycopg://codepilot:codepilot@localhost:55432/codepilot",
    )
    try:
        import psycopg
    except ImportError:  # pragma: no cover - 依赖缺失时视为不可用
        return False, "psycopg 未安装"
    dsn = url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都视为不可用
        return False, f"连接失败：{type(exc).__name__}"
    return True, url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 交付验收检查")
    parser.add_argument("--fast", action="store_true", help="跳过 Docker 与 PostgreSQL 相关检查（等价于 --skip-docker --skip-postgres）")
    parser.add_argument("--static", action="store_true", help="只跑静态检查")
    parser.add_argument("--skip-docker", action="store_true", help="跳过 Docker 相关检查")
    parser.add_argument("--skip-postgres", action="store_true", help="跳过真实 PostgreSQL 检查")
    parser.add_argument("--skip-tests", action="store_true", help="跳过全量 pytest")
    parser.add_argument("--with-fix", action="store_true", help="评测冒烟额外执行 Fix/Verify（需要 Docker）")
    args = parser.parse_args(argv)

    python = sys.executable
    skip_docker = args.fast or args.static or args.skip_docker
    skip_postgres = args.fast or args.static or args.skip_postgres
    skip_tests = args.fast or args.static or args.skip_tests

    docker_ok = False if skip_docker else docker_available()
    if skip_docker:
        docker_reason = "命令行要求跳过"
    elif docker_ok:
        docker_reason = ""
    else:
        docker_reason = "Docker 不可用"
    postgres_ok, postgres_detail = (False, "命令行要求跳过") if skip_postgres else postgres_available()
    if skip_postgres:
        postgres_reason = "命令行要求跳过"
    elif postgres_ok:
        postgres_reason = ""
    else:
        postgres_reason = f"PostgreSQL 不可用（{postgres_detail}）"

    web_build = (ROOT / "web" / "node_modules").exists()
    web_command = [python, "scripts/web_check.py"] + (["--build"] if web_build else [])

    results: list[CheckResult] = []
    results.append(_run("ruff", [python, "-m", "ruff", "check", "."]))
    results.append(
        _run(
            "compileall",
            [python, "-m", "compileall", "-q", "apps", "domain", "agents", "a2a", "tools", "rules",
             "repositories", "sandbox", "evals", "scripts", "tests"],
        )
    )
    results.append(_run("json-schema", [python, "-m", "a2a.schema_registry"]))
    results.append(
        _run(
            "web-check",
            web_command,
            detail="" if web_build else "未安装 web/node_modules，仅做静态契约检查",
        )
    )
    results.append(
        _run(
            "targeted-tests",
            [python, "-m", "pytest", "-q", "tests/test_p0_idempotency.py", "tests/test_api_contract.py",
             "tests/test_dashboard_contract.py", "tests/test_deployment_contract.py",
             "tests/test_deployment_runtime.py", "tests/test_scenario_review_flow.py",
             "tests/test_scenario_fix_flow.py"],
            skip_reason="--static 只跑静态检查" if args.static else None,
        )
    )
    results.append(
        _run(
            "pytest",
            [python, "-m", "pytest", "-q"],
            skip_reason="命令行要求跳过" if skip_tests else None,
        )
    )
    results.append(
        _run("db-check", [python, "scripts/db_check.py"], skip_reason=postgres_reason or None)
    )
    results.append(
        _run("sandbox-check", [python, "scripts/sandbox_check.py"], skip_reason=docker_reason or None)
    )
    if docker_ok or skip_docker:
        fault_command = [python, "scripts/run_fault_matrix.py"]
        fault_detail = ""
    else:
        fault_command = [python, "scripts/run_fault_matrix.py", "--no-sandbox"]
        fault_detail = "Docker 不可用，已跳过 3 个沙箱场景（--no-sandbox）"
    results.append(_run("fault-matrix", fault_command, detail=fault_detail))
    eval_command = [python, "scripts/run_evals.py", "--case-limit", "3", "--runs", "1"]
    eval_detail = ""
    if args.with_fix:
        if docker_ok:
            eval_command.append("--with-fix")
        else:
            eval_detail = "Docker 不可用，未执行 --with-fix"
    results.append(_run("evals-smoke", eval_command, detail=eval_detail))

    print("\n验收结果")
    for item in results:
        timing = f" ({item.elapsed:.1f}s)" if item.status != SKIP else ""
        suffix = f"（{item.detail}）" if item.detail else ""
        print(f"  {item.status:<4} {item.name}{timing}{suffix}")

    failures = [item.name for item in results if item.status == FAIL]
    skipped = [item.name for item in results if item.status == SKIP]

    print("\n执行范围")
    print(f"  Docker：{'已执行' if docker_ok else '未执行'}" + (f"（{docker_reason}）" if docker_reason else ""))
    print(f"  PostgreSQL：{'已执行' if postgres_ok else '未执行'}" + (f"（{postgres_reason}）" if postgres_reason else ""))
    print(f"  全量 pytest：{'已执行' if not skip_tests else '未执行'}")
    if skipped:
        print(f"  跳过项：{skipped}")

    if failures:
        print("结论： FAIL ——", f"失败项：{failures}")
        return 1

    docker_note = "" if docker_ok else " + Docker not executed"
    postgres_note = "" if postgres_ok else " + PostgreSQL not executed"
    if args.static:
        kind = "static pass"
    elif skip_tests or skip_docker or skip_postgres:
        kind = "fast pass"
    else:
        kind = "full pass"
    print(f"结论： {kind}{docker_note}{postgres_note}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
