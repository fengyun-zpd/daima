"""Review Agent（FR-020~FR-025、SRS §4.3 发现层）。

职责：对合成工作区的变更执行确定性规则，输出
- 必需产物 ``Finding``（Finding 集合）；
- 主列表意见对应的 ``ReviewComment`` 产物。

``ReviewComment`` 的生成方由 ``generated_by`` 标注：``rules`` 为确定性模板，
``rules+llm`` 表示解释文本由 LLM Gateway 生成且通过内容守卫。两种情况下
CWE 与严重级别都只来自规则（宪法第九条）。

Review Agent 不能写文件、不能调用写工具、不能改变规则结论。
"""

from __future__ import annotations

import time
from typing import Literal

from a2a.protocol import (
    A2AMessage,
    ArtifactEnvelope,
    FindingArtifact,
    FindingItem,
    FindingStats,
    ReviewComment,
)
from agents.base import AgentHandler, AgentRequest, AgentResult
from domain.confidence import dedup_findings
from domain.enums import ArtifactType, ConfidenceLevel
from domain.errors import CodePilotError
from domain.ids import new_artifact_id, new_message_id
from rules.base import RULE_SET_VERSION, FileContext
from rules.engine import scan_files
from rules.python_rules import RULES

MAX_COMMENT_ARTIFACTS = 20
SUGGESTIONS_BY_CATEGORY = {
    "hardcoded_secret": "改为从环境变量读取，并在部署环境中注入密钥。",
    "shell_true": "改用参数列表调用并设置 check=True，避免 shell 解析。",
    "sql_parameterization": "改用占位符参数化查询，不要把外部输入拼进 SQL。",
}


class ReviewAgent(AgentHandler):
    agent_id = "review-agent"

    def handle(self, request: AgentRequest) -> AgentResult:
        started = time.perf_counter()
        workspace = request.workspace

        contexts: list[FileContext] = []
        degraded: list[str] = []
        for path in workspace.changed_files:
            if not path.endswith(".py"):
                continue
            content = workspace.files.get(path, "")
            context = FileContext.build(path, content, workspace.changed_line_set(path))
            if context.parse_error:
                degraded.append(f"{path}: {context.parse_error}")
            contexts.append(context)

        config = request.config
        raw, _modes = scan_files(
            contexts,
            rules=RULES,
            enabled=config.rules.enabled or None,
            severity_overrides=dict(config.rules.severity_overrides),
        )

        deduped = dedup_findings(
            raw,
            key=lambda item: item.dedup_key,
            score=lambda item: item.confidence,
            severity=lambda item: item.severity,
        )
        deduped.sort(key=lambda item: (item.file, item.hit.line, item.rule.rule_id))

        findings = [
            FindingItem(
                rule_id=item.rule.rule_id,
                title=item.rule.title,
                cwe=item.rule.cwe,
                severity=item.severity,
                file=item.file,
                line=item.hit.line,
                evidence=item.hit.evidence,
                message=item.hit.message,
                confidence_base=item.rule.confidence_base,
                confidence=item.confidence,
                confidence_level=item.confidence_level,
                auto_fixable=item.rule.auto_fixable,
                fix_category=item.rule.fix_category,
                in_changed_lines=item.in_changed_lines,
                context_confirmed=item.hit.context_confirmed,
                symbol=item.hit.symbol,
                rule_version=RULE_SET_VERSION,
            )
            for item in deduped
        ]

        scan_mode = "text" if all(context.tree is None for context in contexts) and contexts else "ast"
        if degraded:
            scan_mode = "mixed" if scan_mode == "ast" else "text"

        stats = FindingStats(
            total=len(findings),
            by_severity=_count_by(findings, lambda item: str(item.severity)),
            by_confidence_level=_count_by(findings, lambda item: str(item.confidence_level)),
            suppressed=sum(
                1 for item in findings if item.confidence_level is ConfidenceLevel.SUPPRESSED
            ),
        )

        artifact_payload = FindingArtifact(
            rule_set_version=RULE_SET_VERSION,
            scan_mode=scan_mode,
            degraded_reason="；".join(degraded) if degraded else None,
            files_scanned=[context.path for context in contexts],
            findings=findings,
            stats=stats,
        )

        artifacts = [
            ArtifactEnvelope.build(
                artifact_id=new_artifact_id(),
                task_id=request.task_id,
                artifact_type=ArtifactType.FINDING,
                data=artifact_payload.model_dump(mode="json"),
            )
        ]

        main_list = [
            item
            for item in findings
            if item.confidence_level in {ConfidenceLevel.CONFIRMED, ConfidenceLevel.PROBABLE}
        ][:MAX_COMMENT_ARTIFACTS]
        llm_used = 0
        llm_degraded: str | None = None
        for item in main_list:
            suggestion = SUGGESTIONS_BY_CATEGORY.get(
                str(item.fix_category) if item.fix_category else "",
                "请人工确认后按建议修复。",
            )
            generated_by: Literal["rules", "llm", "rules+llm"] = "rules"
            message_text = item.message
            if request.llm is not None:
                try:
                    response = request.llm.explain_finding(
                        finding=item.model_dump(mode="json"),
                        impact_scope=f"{item.file} 第 {item.line} 行",
                    )
                    message_text = response.text
                    generated_by = "rules+llm"
                    llm_used += 1
                except CodePilotError as exc:
                    # 宪法第九条 + docs/04 §4：模型不可用时保留确定性结论，只降级解释文本。
                    llm_degraded = f"{exc.code}: {exc.message}"
            comment = ReviewComment(
                finding_ref=f"{item.rule_id}@{item.file}:{item.line}",
                message=message_text,
                suggestion=suggestion,
                confidence_level=item.confidence_level,
                impact_scope=f"{item.file} 第 {item.line} 行",
                citations=[f"{item.file}:{item.line}"],
                generated_by=generated_by,
            )
            artifacts.append(
                ArtifactEnvelope.build(
                    artifact_id=new_artifact_id(),
                    task_id=request.task_id,
                    artifact_type=ArtifactType.REVIEW_COMMENT,
                    data=comment.model_dump(mode="json"),
                )
            )

        duration_ms = int((time.perf_counter() - started) * 1000)
        message = A2AMessage(
            message_id=new_message_id(),
            task_id=request.task_id,
            type="task.completed",
            role="agent",
            correlation_id=request.task.correlation_id,
            artifact_refs=[envelope.artifact_id for envelope in artifacts],
            error=None,
            created_at=_utcnow(),
        )

        return AgentResult(
            artifacts=artifacts,
            messages=[message],
            stats={
                "findings": len(findings),
                "main_list": len(main_list),
                "files_scanned": len(contexts),
                "degraded": bool(degraded),
                "duration_ms": duration_ms,
                "tool_calls": len(request.tools.trace()),
                "scan_mode": scan_mode,
                "llm_comments": llm_used,
                "llm_degraded": llm_degraded,
                "budget": request.budget.snapshot(),
            },
        )


def _count_by(items: list[FindingItem], key) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        token = key(item)
        counts[token] = counts.get(token, 0) + 1
    return counts


def _utcnow():
    from domain.clock import utcnow

    return utcnow()


__all__ = ["ReviewAgent", "SUGGESTIONS_BY_CATEGORY"]
