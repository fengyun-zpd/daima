"""Fix Agent（FR-040~FR-044：只生成候选补丁）。

约束：
- 只支持 3 类确定性修复（硬编码密钥、``shell=True``、SQL 拼接）；
- 只返回 ``PatchCandidate`` 与 ``PatchEvidence``，不写文件、不写分支、不合并；
- 补丁必须带 ``patch_version`` 与 `patch_hash``，且同样输入幂等产出同样内容。
"""

from __future__ import annotations

import time
from typing import Any

from a2a.protocol import (
    A2AMessage,
    ArtifactEnvelope,
    FindingItem,
    PatchCandidate,
    PatchEvidence,
)
from agents.base import AgentHandler, AgentRequest, AgentResult
from agents.fix.recipes import PatchPlan, finding_ref, plan_fix
from domain.clock import utcnow
from domain.diffparse import build_unified_diff
from domain.enums import ArtifactType, FixCategory
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_artifact_id, new_message_id
from domain.workspace import Workspace

MAX_FILES_PER_PATCH = 5
MAX_FINDINGS_PER_PATCH = 10


class FixAgent(AgentHandler):
    agent_id = "fix-agent"

    def __init__(self, *, placeholder: str | None = None) -> None:
        self.placeholder = placeholder

    def handle(self, request: AgentRequest) -> AgentResult:
        started = time.perf_counter()
        findings = _select_findings(request)
        if not findings:
            raise CodePilotError(
                ErrorCode.PATCH_INVALID,
                "没有可自动修复的意见（仅支持硬编码密钥、shell=True、SQL 参数化三类）",
            )

        placeholder = self.placeholder or request.config.fix.sql_placeholder
        plans = _plan_by_file(findings, request.workspace, placeholder=placeholder)
        if not plans:
            raise CodePilotError(
                ErrorCode.PATCH_INVALID,
                "候选修复无法安全改写为目标补丁（形态未覆盖或文件不在工作区）",
            )
        if len(plans) > MAX_FILES_PER_PATCH:
            raise CodePilotError(
                ErrorCode.WIDE_IMPACT,
                f"单次修复涉及 {len(plans)} 个文件，超过阈值 {MAX_FILES_PER_PATCH}",
                details={"files": sorted(plans)},
            )

        patch_version = int(request.context.get("patch_version", 1))
        diff_parts: list[str] = []
        changed_files: list[str] = []
        changed_functions: set[str] = set()
        finding_refs: list[str] = []
        notes: list[str] = []
        added = removed = 0

        for path in sorted(plans):
            plan = plans[path]
            diff = build_unified_diff(path, plan.original, plan.patched)
            diff_parts.append(diff if diff.endswith("\n") else diff + "\n")
            changed_files.append(path)
            changed_functions.update(plan.changed_functions)
            finding_refs.extend(plan.finding_refs)
            notes.extend(f"{path}: {note}" for note in plan.notes)
            added += sum(
                1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
            )
            removed += sum(
                1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
            )

        patch_text = "".join(diff_parts)
        patch_hash = _hash_patch(patch_text)
        idempotency_key = str(
            request.context.get("patch_idempotency_key")
            or f"{request.task.parent_task_id}:fix:v{patch_version}"
        )
        target_branch = str(
            request.context.get("target_branch") or f"codepilot/{request.task.parent_task_id}"
        )

        candidate = PatchCandidate(
            patch_version=patch_version,
            patch_hash=patch_hash,
            base_commit=str(request.context.get("base_commit", "synthetic-base-001")),
            target_branch=target_branch,
            fix_category=_dominant_category(plans),
            finding_refs=sorted(set(finding_refs)),
            changed_files=sorted(changed_files),
            changed_functions=sorted(changed_functions),
            added_lines=added,
            removed_lines=removed,
            diff=patch_text,
            idempotency_key=idempotency_key,
        )
        evidence = PatchEvidence(
            patch_version=patch_version,
            patch_hash=patch_hash,
            changed_files=sorted(changed_files),
            changed_functions=sorted(changed_functions),
            scope_drift=False,
            scope_drift_files=[],
            wide_impact=len(changed_files) > request.config.fix.wide_impact_file_threshold,
            test_gap=False,
            sandbox_run_id=None,
            reasons=notes,
        )

        artifacts = [
            ArtifactEnvelope.build(
                artifact_id=new_artifact_id(),
                task_id=request.task_id,
                artifact_type=ArtifactType.PATCH_CANDIDATE,
                data=candidate.model_dump(mode="json"),
            ),
            ArtifactEnvelope.build(
                artifact_id=new_artifact_id(),
                task_id=request.task_id,
                artifact_type=ArtifactType.PATCH_EVIDENCE,
                data=evidence.model_dump(mode="json"),
            ),
        ]
        message = A2AMessage(
            message_id=new_message_id(),
            task_id=request.task_id,
            type="task.completed",
            role="agent",
            correlation_id=request.task.correlation_id,
            artifact_refs=[item.artifact_id for item in artifacts],
            error=None,
            created_at=utcnow(),
        )
        return AgentResult(
            artifacts=artifacts,
            messages=[message],
            stats={
                "patch_version": patch_version,
                "patch_hash": patch_hash,
                "changed_files": sorted(changed_files),
                "added_lines": added,
                "removed_lines": removed,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "tool_calls": len(request.tools.trace()),
            },
        )


def _hash_patch(patch_text: str) -> str:
    from a2a.protocol import compute_text_hash

    return compute_text_hash(patch_text)


def _select_findings(request: AgentRequest) -> list[FindingItem]:
    """从输入 Artifact 中取出可修复的 Finding（按选中意见过滤）。"""
    envelope = request.input_of_type(str(ArtifactType.FINDING))
    if envelope is None:
        raise CodePilotError(ErrorCode.PATCH_INVALID, "Fix 子任务缺少 Finding 输入 Artifact")
    findings = [FindingItem.model_validate(item) for item in envelope.data.get("findings", [])]
    selected = {str(item) for item in request.context.get("fixed_comment_refs", []) or []}
    fixable = [
        item
        for item in findings
        if item.auto_fixable and item.fix_category is not None and item.confidence_level != "suppressed"
    ]
    if selected:
        fixable = [item for item in fixable if finding_ref(item) in selected]
    return fixable[:MAX_FINDINGS_PER_PATCH]


def _plan_by_file(
    findings: list[FindingItem], workspace: Workspace, *, placeholder: str
) -> dict[str, PatchPlan]:
    """按文件分组规划；同一文件内自上而下定位、自下而上应用，保证行号稳定。"""
    grouped: dict[str, list[FindingItem]] = {}
    for finding in findings:
        grouped.setdefault(finding.file, []).append(finding)

    plans: dict[str, PatchPlan] = {}
    for path, items in grouped.items():
        if not workspace.has_file(path):
            continue
        content = workspace.files[path]
        merged: PatchPlan | None = None
        for finding in sorted(items, key=lambda item: item.line, reverse=True):
            scope = Workspace(
                task_id=workspace.task_id,
                base_commit=workspace.base_commit,
                input_type=workspace.input_type,
                context_policy=workspace.context_policy,
                files={path: content},
                changed_lines={path: workspace.changed_line_set(path)},
            )
            plan = plan_fix(finding, scope, placeholder=placeholder)
            if plan is None or not plan.changed:
                continue
            if merged is None:
                merged = plan
            else:
                merged = PatchPlan(
                    category=merged.category,
                    path=path,
                    original=merged.original,
                    patched=plan.patched,
                    finding_refs=[*merged.finding_refs, *plan.finding_refs],
                    changed_functions=sorted(set(merged.changed_functions) | set(plan.changed_functions)),
                    notes=[*merged.notes, *plan.notes],
                )
            content = plan.patched
        if merged is not None:
            plans[path] = merged
    return plans


def _dominant_category(plans: dict[str, PatchPlan]) -> FixCategory:
    """补丁类别：以出现次数最多的修复类别为准，保证确定性。"""
    counts: dict[FixCategory, int] = {}
    for plan in plans.values():
        counts[plan.category] = counts.get(plan.category, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[0][0]


def patch_summary(candidate: PatchCandidate) -> dict[str, Any]:
    return {
        "patch_version": candidate.patch_version,
        "patch_hash": candidate.patch_hash,
        "changed_files": list(candidate.changed_files),
        "fix_category": str(candidate.fix_category),
    }


__all__ = ["MAX_FILES_PER_PATCH", "FixAgent", "patch_summary"]
