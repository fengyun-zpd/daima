"""规则引擎：按文件执行确定性规则并输出可追溯命中（FR-020~FR-022）。

输出顺序、去重结果和严重级别都是确定的（NFR-005：同输入同规则版本结果一致）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from domain.confidence import ConfidenceInputs, score_confidence
from domain.enums import ConfidenceLevel, Severity
from rules.base import AST_CHECKS, RULE_SET_VERSION, FileContext, Rule, RuleHit, RuleKind
from rules.python_rules import RULES, _hardcoded_secret  # noqa: F401  (触发检查注册)


@dataclass(slots=True, frozen=True)
class RawFinding:
    rule: Rule
    hit: RuleHit
    file: str
    confidence: float
    confidence_level: ConfidenceLevel
    in_changed_lines: bool
    reasons: tuple[str, ...]

    @property
    def severity(self) -> Severity:
        return self.hit.severity or self.rule.severity

    @property
    def dedup_key(self) -> str:
        return f"{self.file}:{self.hit.line}:{self.rule.rule_id}"


def _enabled_rules(
    rules: Sequence[Rule],
    *,
    enabled: Sequence[str] | None,
    severity_overrides: dict[str, Severity] | None,
) -> list[Rule]:
    selected = []
    for rule in rules:
        if enabled and rule.rule_id not in enabled:
            continue
        override = (severity_overrides or {}).get(rule.rule_id)
        selected.append(rule if override is None else _with_severity(rule, override))
    return selected


def _with_severity(rule: Rule, severity: Severity) -> Rule:
    return Rule(
        rule_id=rule.rule_id,
        title=rule.title,
        cwe=rule.cwe,
        severity=severity,
        confidence_base=rule.confidence_base,
        kind=rule.kind,
        pattern=rule.pattern,
        check=rule.check,
        auto_fixable=rule.auto_fixable,
        fix_category=rule.fix_category,
        description=rule.description,
        references=rule.references,
    )


def run_rule(context: FileContext, rule: Rule) -> list[RuleHit]:
    """在单个文件上执行一条规则。"""
    if rule.check:
        check = AST_CHECKS.get(rule.check)
        if check is None:
            return []
        if rule.kind in {RuleKind.AST, RuleKind.AST_DATAFLOW} and context.tree is None:
            # 降级：AST 不可用时该规则跳过，由调用方记录 degraded_reason（FR-021）。
            return []
        return list(check(context, rule))
    if rule.pattern:
        pattern = rule.compiled()
        hits: list[RuleHit] = []
        for lineno, text in enumerate(context.lines, start=1):
            if pattern.search(text):
                hits.append(
                    RuleHit(
                        line=lineno,
                        evidence=text.strip()[:300],
                        message=rule.description or rule.title,
                        symbol=context.symbol_for(lineno),
                        context_confirmed=True,
                    )
                )
        return hits
    return []


def scan_file(
    context: FileContext,
    *,
    rules: Sequence[Rule] = RULES,
    enabled: Sequence[str] | None = None,
    severity_overrides: dict[str, Severity] | None = None,
) -> list[RawFinding]:
    """扫描单个文件，返回评分后的命中列表。"""
    findings: list[RawFinding] = []
    for rule in _enabled_rules(rules, enabled=enabled, severity_overrides=severity_overrides):
        for hit in run_rule(context, rule):
            in_changed = context.in_changed_lines(hit.line)
            result = score_confidence(
                ConfidenceInputs(
                    confidence_base=rule.confidence_base,
                    context_confirmed=hit.context_confirmed,
                    in_changed_lines=in_changed,
                    historical_context_only=hit.historical_only or not in_changed,
                    dynamic_uncertain=hit.dynamic_uncertain,
                )
            )
            findings.append(
                RawFinding(
                    rule=rule,
                    hit=hit,
                    file=context.path,
                    confidence=result.score,
                    confidence_level=result.level,
                    in_changed_lines=in_changed,
                    reasons=result.reasons,
                )
            )
    return findings


def scan_files(
    contexts: Iterable[FileContext],
    *,
    rules: Sequence[Rule] = RULES,
    enabled: Sequence[str] | None = None,
    severity_overrides: dict[str, Severity] | None = None,
) -> tuple[list[RawFinding], list[str]]:
    """扫描多个文件，返回 (命中列表, 扫描模式标记)。"""
    findings: list[RawFinding] = []
    modes: set[str] = set()
    for context in contexts:
        modes.add("text" if context.tree is None else "ast")
        findings.extend(
            scan_file(
                context,
                rules=rules,
                enabled=enabled,
                severity_overrides=severity_overrides,
            )
        )
    return findings, sorted(modes)


__all__ = [
    "RULE_SET_VERSION",
    "RawFinding",
    "run_rule",
    "scan_file",
    "scan_files",
]
