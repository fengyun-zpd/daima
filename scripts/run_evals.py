"""运行黄金集评测并打印汇总（SRS §11、docs/06）。

用法：
    python scripts/run_evals.py                          # 全量：20 用例 × 3 模式 × 3 次
    python scripts/run_evals.py --case-limit 5 --runs 1  # 快速冒烟
    python scripts/run_evals.py --with-fix               # 额外执行 Fix/Verify（需要 Docker）

评测结果写入 eval_run / eval_result，报告落在内容目录 ``reports/eval/<run_id>.json``。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import build_container  # noqa: E402
from domain.enums import RunMode  # noqa: E402
from evals.runner import EvalRunner  # noqa: E402
from repositories.content_store import ContentStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 黄金集评测")
    parser.add_argument("--dataset", default="golden-v1")
    parser.add_argument("--modes", nargs="*", default=["single", "a2a", "offline"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--case-limit", type=int, default=None)
    parser.add_argument("--with-fix", action="store_true")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 报告")
    args = parser.parse_args(argv)

    container = build_container(url=args.database_url, auto_create=False, config=None)
    runner = EvalRunner(
        container.coordinator,
        content_store=container.content_store or ContentStore(),
        dataset=args.dataset,
    )
    summary = runner.run(
        modes=[RunMode(mode) for mode in args.modes],
        runs_per_case=args.runs,
        case_limit=args.case_limit,
        with_fix=args.with_fix,
    )

    payload = summary.to_payload()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"评测运行：{payload['run_id']}  数据集={payload['dataset']}  报告={payload['report_ref']}")
        for mode, metrics in payload["aggregate"]["modes"].items():
            print(
                f"  [{mode:<7}] recall={metrics['finding_recall']:.2f} "
                f"precision={metrics['finding_precision']:.2f} "
                f"pass@3={metrics['pass_at_3']:.2f} pass^3={metrics['pass_pow_3']:.2f} "
                f"route={metrics['route_correctness']:.2f} schema={metrics['artifact_schema_pass_rate']:.2f} "
                f"converge={metrics['task_convergence_rate']:.2f} trace={metrics['trace_complete_rate']:.2f} "
                f"p50={metrics['latency_ms_p50']}ms"
            )
        invariants = payload["aggregate"]["security_invariants"]
        print(f"  安全不变量：{invariants}")
        comparison = payload["aggregate"].get("comparison") or {}
        if comparison:
            print(
                f"  a2a vs single：recall {comparison['recall_delta']:+.2f} "
                f"precision {comparison['precision_delta']:+.2f} "
                f"p50 {comparison['latency_p50_delta_ms']:+d}ms"
                f"（延迟变化 {comparison['latency_p50_ratio']:+.0%}）"
            )
            if comparison["latency_regression_exceeds_30pct"]:
                print("  注意：a2a 延迟增加超过 30%，报告中必须解释为协议与并行度成本（宪法第十条）")

    violated = {key: value for key, value in payload["aggregate"]["security_invariants"].items() if value}
    return 1 if violated else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
