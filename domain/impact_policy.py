"""影响范围与修复范围策略（FR-027、FR-028、SRS §6.4）。

确定性边界：影响文件数、高风险模块、scope drift 与 WIDE_IMPACT 的判定
只由本模块给出，LLM 与 Agent 不得改变（宪法第九条）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from domain.confidence import is_high_risk_path
from domain.enums import RiskLevel

LOW_MAX_FILES = 2
MEDIUM_MAX_FILES = 5

#: 认证、权限、支付模块无论影响范围大小都必须转人工（SRS §6.4）。
ALWAYS_HUMAN_TOKENS: tuple[str, ...] = ("auth", "login", "permission", "acl", "payment", "billing")


@dataclass(slots=True)
class ImpactPolicy:
    wide_impact_file_threshold: int = MEDIUM_MAX_FILES

    def classify(self, *, affected_files: list[str]) -> RiskLevel:
        count = len(set(affected_files))
        if count > self.wide_impact_file_threshold:
            return RiskLevel.HIGH
        if any(is_high_risk_path(path) for path in affected_files):
            return RiskLevel.MEDIUM
        if count > LOW_MAX_FILES:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def is_wide_impact(self, *, affected_files: list[str]) -> bool:
        return len(set(affected_files)) > self.wide_impact_file_threshold

    def requires_human(self, *, affected_files: list[str]) -> bool:
        """高风险模块改动一律转人工，不论影响文件数。"""
        return any(
            any(token in path.lower() for token in ALWAYS_HUMAN_TOKENS) for path in affected_files
        )


@dataclass(slots=True)
class ScopeCheck:
    """补丁范围校验结果（FR-028）。"""

    scope_drift: bool
    scope_drift_files: list[str] = field(default_factory=list)
    wide_impact: bool = False
    requires_extra_approval: bool = False
    reasons: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, object]:
        return {
            "scope_drift": self.scope_drift,
            "scope_drift_files": list(self.scope_drift_files),
            "wide_impact": self.wide_impact,
            "requires_extra_approval": self.requires_extra_approval,
            "reasons": list(self.reasons),
        }


def check_scope(
    *,
    patch_files: list[str],
    declared_files: list[str],
    affected_files: list[str],
    policy: ImpactPolicy | None = None,
) -> ScopeCheck:
    """校验补丁是否超出审查意见声明的文件范围。"""
    resolved = policy or ImpactPolicy()
    declared = set(declared_files)
    patch_set = set(patch_files)
    drift_files = sorted(patch_set - declared)
    reasons: list[str] = []

    if drift_files:
        reasons.append(f"补丁修改了未声明的文件：{drift_files}")

    wide = resolved.is_wide_impact(affected_files=affected_files) or resolved.is_wide_impact(
        affected_files=patch_files
    )
    if wide:
        reasons.append(
            f"影响文件数超过阈值 {resolved.wide_impact_file_threshold}，标记 WIDE_IMPACT"
        )
    if resolved.requires_human(affected_files=patch_files):
        reasons.append("涉及认证/权限/支付等高风险模块，必须人工确认")

    return ScopeCheck(
        scope_drift=bool(drift_files),
        scope_drift_files=drift_files,
        wide_impact=wide,
        requires_extra_approval=wide or resolved.requires_human(affected_files=patch_files),
        reasons=reasons,
    )


__all__ = [
    "ALWAYS_HUMAN_TOKENS",
    "LOW_MAX_FILES",
    "MEDIUM_MAX_FILES",
    "ImpactPolicy",
    "ScopeCheck",
    "check_scope",
]
