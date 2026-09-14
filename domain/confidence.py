"""可解释的置信度计算与去重（SRS §6.3.1、docs/02 §3）。

确定性边界：LLM 不得改变本模块结论（宪法第九条）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from domain.enums import ConfidenceLevel, Severity

CONFIRMED_THRESHOLD = 0.85
PROBABLE_THRESHOLD = 0.60
SUSPICIOUS_THRESHOLD = 0.40

CONTEXT_CONFIRMED_BONUS = 0.20
IN_CHANGED_LINES_BONUS = 0.15
TEST_COVERED_PENALTY = -0.10
HISTORICAL_ONLY_PENALTY = -0.20
DYNAMIC_UNCERTAINTY_PENALTY = -0.15

#: 认证、权限、支付等高风险模块，无论影响范围都必须转人工（SRS §6.4）。
HIGH_RISK_PATH_TOKENS: tuple[str, ...] = (
    "auth",
    "login",
    "permission",
    "acl",
    "payment",
    "billing",
    "token",
    "crypto",
    "secret",
)


@dataclass(frozen=True, slots=True)
class ConfidenceInputs:
    confidence_base: float
    context_confirmed: bool = False
    in_changed_lines: bool = False
    test_covered: bool = False
    historical_context_only: bool = False
    dynamic_uncertain: bool = False


@dataclass(frozen=True, slots=True)
class ConfidenceResult:
    score: float
    level: ConfidenceLevel
    reasons: tuple[str, ...]

    @property
    def blocks_pipeline(self) -> bool:
        """critical + confirmed 才阻断（FR-025 由 severity 与 level 共同决定）。"""
        return self.level is ConfidenceLevel.CONFIRMED


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_confidence(inputs: ConfidenceInputs) -> ConfidenceResult:
    score = clamp(inputs.confidence_base)
    reasons: list[str] = [f"rule_base={inputs.confidence_base:.2f}"]

    if inputs.context_confirmed:
        score += CONTEXT_CONFIRMED_BONUS
        reasons.append(f"context_confirmed+{CONTEXT_CONFIRMED_BONUS:.2f}")
    if inputs.in_changed_lines:
        score += IN_CHANGED_LINES_BONUS
        reasons.append(f"in_changed_lines+{IN_CHANGED_LINES_BONUS:.2f}")
    if inputs.test_covered:
        score += TEST_COVERED_PENALTY
        reasons.append(f"test_covered{TEST_COVERED_PENALTY:.2f}")
    if inputs.historical_context_only:
        score += HISTORICAL_ONLY_PENALTY
        reasons.append(f"historical_only{HISTORICAL_ONLY_PENALTY:.2f}")
    if inputs.dynamic_uncertain:
        score += DYNAMIC_UNCERTAINTY_PENALTY
        reasons.append(f"dynamic_uncertain{DYNAMIC_UNCERTAINTY_PENALTY:.2f}")

    score = clamp(score)
    return ConfidenceResult(score=round(score, 4), level=level_for(score), reasons=tuple(reasons))


def level_for(score: float) -> ConfidenceLevel:
    if score >= CONFIRMED_THRESHOLD:
        return ConfidenceLevel.CONFIRMED
    if score >= PROBABLE_THRESHOLD:
        return ConfidenceLevel.PROBABLE
    if score >= SUSPICIOUS_THRESHOLD:
        return ConfidenceLevel.SUSPICIOUS
    return ConfidenceLevel.SUPPRESSED


def is_high_risk_path(path: str) -> bool:
    lowered = path.lower()
    return any(token in lowered for token in HIGH_RISK_PATH_TOKENS)


def dedup_key(file: str, line: int, rule_id: str) -> str:
    """去重键 ``(file, line, rule_id)``（FR-024、docs/02 §3）。"""
    return f"{file}:{line}:{rule_id}"


def dedup_findings[T](findings: Sequence[T], *, key, score, severity) -> list[T]:
    """同键保留置信度更高的记录；同分时保留更严重的记录。"""
    severity_rank = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
    best: dict[str, T] = {}
    for item in findings:
        item_key = key(item)
        current = best.get(item_key)
        if current is None:
            best[item_key] = item
            continue
        if (score(item), severity_rank[severity(item)]) > (
            score(current),
            severity_rank[severity(current)],
        ):
            best[item_key] = item
    return list(best.values())


def main_list[T](findings: Iterable[T], *, level) -> list[T]:
    """``confirmed`` 与 ``probable`` 进入主列表（SRS §4.3）。"""
    return [
        item
        for item in findings
        if level(item) in {ConfidenceLevel.CONFIRMED, ConfidenceLevel.PROBABLE}
    ]


__all__ = [
    "CONFIRMED_THRESHOLD",
    "CONTEXT_CONFIRMED_BONUS",
    "DYNAMIC_UNCERTAINTY_PENALTY",
    "HISTORICAL_ONLY_PENALTY",
    "HIGH_RISK_PATH_TOKENS",
    "IN_CHANGED_LINES_BONUS",
    "PROBABLE_THRESHOLD",
    "SUSPICIOUS_THRESHOLD",
    "TEST_COVERED_PENALTY",
    "ConfidenceInputs",
    "ConfidenceResult",
    "clamp",
    "dedup_findings",
    "dedup_key",
    "is_high_risk_path",
    "level_for",
    "main_list",
    "score_confidence",
]
