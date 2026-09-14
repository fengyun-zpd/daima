"""运行 A2A 故障注入矩阵（docs/06 §2）。

用法：
    python scripts/run_fault_matrix.py                 # 全部场景（含 Docker 沙箱场景）
    python scripts/run_fault_matrix.py --no-sandbox    # 跳过需要 Docker 的场景
    python scripts/run_fault_matrix.py --only "重复提交" "未审批合并"

数据库使用 ``CODEPILOT_DATABASE_URL``；未设置时使用临时 SQLite 文件。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.fault_matrix import SCENARIOS, run_matrix  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 故障注入矩阵")
    parser.add_argument("--no-sandbox", action="store_true", help="跳过需要 Docker 的沙箱场景")
    parser.add_argument("--only", nargs="*", default=None, help="只运行指定场景")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="codepilot-faults-"))
    url = args.database_url or os.environ.get("CODEPILOT_DATABASE_URL") or (
        f"sqlite+pysqlite:///{(tmp / 'faults.db').as_posix()}"
    )
    print(f"数据库：{url}")

    # 明确列出被跳过的场景：跳过 ≠ 通过（与 run_checks.py 的口径一致）。
    skipped = [name for name, _scenario, needs_sandbox in SCENARIOS if needs_sandbox and args.no_sandbox]
    if args.only:
        skipped = [name for name in skipped if name in args.only]
    if skipped:
        print(f"跳过 {len(skipped)} 个沙箱场景（--no-sandbox）：{'、'.join(skipped)}")

    outcomes = run_matrix(
        tmp_path=tmp, database_url=url, include_sandbox=not args.no_sandbox, only=args.only
    )

    print("\n故障注入矩阵")
    for outcome in outcomes:
        status = "OK  " if outcome.passed else "FAIL"
        print(f"  {status} {outcome.name}: {outcome.detail}")

    failed = [item for item in outcomes if not item.passed]
    suffix = f"（已跳过 {len(skipped)} 个沙箱场景）" if skipped else ""
    print(f"\n共 {len(outcomes)} 个场景{suffix}，失败 {len(failed)} 个")
    if failed:
        print("结论： FAIL ——", "、".join(item.name for item in failed))
        return 1
    print("结论： " + ("fast pass（沙箱场景未执行）" if skipped else "full pass"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
