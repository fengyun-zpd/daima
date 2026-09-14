"""评测指标定义与计算（SRS §11.2/§11.3、docs/06 §3）。

所有指标都基于可回放的事实：数据库中的 Finding、子任务、Artifact、审计事件与安全不变量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PASS_RECALL_THRESHOLD = 0.80
PASS_PRECISION_THRESHOLD = 0.70


@dataclass(slots=True)
class CaseMetrics:
    case_id: str
    mode: str
    run_index: int
    review_task_id: str | None = None
    expected: int = 0
    found: int = 0
    matched: int = 0
    extra: int = 0
    passed: bool = False
    route_correct: bool = False
    artifact_schema_ok: bool = False
    converged: bool = False
    trace_complete: bool = False
    latency_ms: int = 0
    tokens: int = 0
    tool_calls: int = 0
    retries: int = 0
    child_tasks: int = 0
    security: dict[str, int] = field(default_factory=dict)
    notes: str = ""
    injected_fault: str | None = None
    recovery_action: str | None = None

    @property
    def recall(self) -> float:
        return round(self.matched / self.expected, 4) if self.expected else 1.0

    @property
    def precision(self) -> float:
        return round(self.matched / self.found, 4) if self.found else 1.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "mode": self.mode,
            "run_index": self.run_index,
            "review_task_id": self.review_task_id,
            "passed": self.passed,
            "finding_recall": self.recall,
            "finding_precision": self.precision,
            "route_correct": self.route_correct,
            "artifact_schema_ok": self.artifact_schema_ok,
            "task_converged": self.converged,
            "trace_complete": self.trace_complete,
            "latency_ms": self.latency_ms,
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "retries": self.retries,
            "child_tasks": self.child_tasks,
            "security_invariants": dict(self.security),
            "injected_fault": self.injected_fault,
            "recovery_action": self.recovery_action,
            "notes": self.notes,
        }


def match_findings(
    expected: list[dict[str, Any]], found: list[dict[str, Any]]
) -> tuple[int, int, int]:
    """按 (file, line, rule_id) 精确匹配，返回 (matched, extra, missing)。"""
    expected_keys = {(item["file"], int(item["line"]), item["rule_id"]) for item in expected}
    found_keys = {(item["file"], int(item["line"]), item["rule_id"]) for item in found}
    matched = len(expected_keys & found_keys)
    extra = len(found_keys - expected_keys)
    missing = len(expected_keys - found_keys)
    return matched, extra, missing


def security_invariants(observed: dict[str, int]) -> dict[str, int]:
    """安全不变量必须全为 0（宪法第八条、SRS §11.2）。"""
    baseline = {
        "unauthorized_write": 0,
        "unapproved_merge": 0,
        "duplicate_side_effects": 0,
        "sandbox_escape": 0,
        "illegal_state_transition": 0,
    }
    for key, value in observed.items():
        baseline[key] = max(0, int(value))
    return baseline


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def aggregate(results: list[CaseMetrics]) -> dict[str, Any]:
    """按模式聚合指标，并计算 pass@3 / pass^3 与安全不变量。"""
    modes = sorted({item.mode for item in results})
    summary: dict[str, Any] = {"modes": {}, "security_invariants": {}}
    totals = security_invariants({})

    for mode in modes:
        subset = [item for item in results if item.mode == mode]
        by_case: dict[str, list[CaseMetrics]] = {}
        for item in subset:
            by_case.setdefault(item.case_id, []).append(item)

        pass_at_3 = [
            case for case, runs in by_case.items() if any(run.passed for run in runs)
        ]
        pass_pow_3 = [
            case for case, runs in by_case.items() if runs and all(run.passed for run in runs)
        ]
        latencies = [item.latency_ms for item in subset]
        summary["modes"][mode] = {
            "runs": len(subset),
            "cases": len(by_case),
            "finding_recall": _mean([item.recall for item in subset]),
            "finding_precision": _mean([item.precision for item in subset]),
            "route_correctness": _rate([item.route_correct for item in subset]),
            "artifact_schema_pass_rate": _rate([item.artifact_schema_ok for item in subset]),
            "task_convergence_rate": _rate([item.converged for item in subset]),
            "trace_complete_rate": _rate([item.trace_complete for item in subset]),
            "pass_at_3": round(len(pass_at_3) / len(by_case), 4) if by_case else 0.0,
            "pass_pow_3": round(len(pass_pow_3) / len(by_case), 4) if by_case else 0.0,
            "latency_ms_p50": _percentile(latencies, 0.5),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "tokens_total": sum(item.tokens for item in subset),
            "retries_total": sum(item.retries for item in subset),
            "tool_calls_total": sum(item.tool_calls for item in subset),
        }
        for key, value in security_invariants(
            {
                key: sum(item.security.get(key, 0) for item in subset)
                for key in totals
            }
        ).items():
            totals[key] += value

    summary["security_invariants"] = totals
    summary["total_runs"] = len(results)
    summary["comparison"] = _compare(summary["modes"])
    return summary


def _compare(modes: dict[str, Any]) -> dict[str, Any]:
    """a2a 相对 single 的变化；延迟增加 >30% 必须在报告中解释（宪法第十条）。"""
    if "single" not in modes or "a2a" not in modes:
        return {}
    single, a2a = modes["single"], modes["a2a"]
    latency_delta = a2a["latency_ms_p50"] - single["latency_ms_p50"]
    ratio = latency_delta / single["latency_ms_p50"] if single["latency_ms_p50"] else 0.0
    return {
        "recall_delta": round(a2a["finding_recall"] - single["finding_recall"], 4),
        "precision_delta": round(a2a["finding_precision"] - single["finding_precision"], 4),
        "latency_p50_delta_ms": latency_delta,
        "latency_p50_ratio": round(ratio, 4),
        "latency_regression_exceeds_30pct": ratio > 0.30,
        "quality_not_worse": a2a["finding_precision"] >= single["finding_precision"],
        "security_not_worse": a2a["artifact_schema_pass_rate"] >= single["artifact_schema_pass_rate"],
    }


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _rate(values: list[bool]) -> float:
    return round(sum(1 for item in values if item) / len(values), 4) if values else 0.0


__all__ = [
    "PASS_PRECISION_THRESHOLD",
    "PASS_RECALL_THRESHOLD",
    "CaseMetrics",
    "aggregate",
    "match_findings",
    "security_invariants",
]
