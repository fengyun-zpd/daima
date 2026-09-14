"""意见、补丁、沙箱、审批、工具执行与评测仓储。

关键约束：
- 意见按 ``(task_id, file, line, rule_id)`` 去重入库（FR-024）；
- 补丁只有 Coordinator 能写，且必须带 patch_version 与幂等键（FR-042）；
- 审批只允许 approver，决定必须绑定 patch_version，拒绝必须填写原因（FR-070~074）；
- 工具执行相同幂等键直接返回原结果（FR-017）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from a2a.protocol import FindingItem, PatchCandidate
from domain.clock import utcnow
from domain.confidence import dedup_key
from domain.enums import (
    ActorRole,
    ApprovalDecision,
    PatchStatus,
    SandboxRunStatus,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_id
from repositories.audit import record_event
from repositories.models import (
    Approval,
    EvalCase,
    EvalResult,
    EvalRun,
    FixPatch,
    ReviewComment,
    SandboxRun,
    ToolExecution,
)

PROTECTED_BRANCHES = frozenset({"main", "master", "develop"})


class CommentStore:
    """``review_comment`` 仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.created: list[ReviewComment] = []
        self.duplicates: list[str] = []

    def add_findings(
        self,
        *,
        task_id: str,
        artifact_id: str | None,
        findings: list[FindingItem],
        impact_scope_by_symbol: dict[str, str] | None = None,
        citations_by_file: dict[str, list[str]] | None = None,
    ) -> list[ReviewComment]:
        existing_keys = set(
            self.session.execute(
                select(ReviewComment.file, ReviewComment.line, ReviewComment.rule_id).where(
                    ReviewComment.task_id == task_id
                )
            ).all()
        )
        impact_scope_by_symbol = impact_scope_by_symbol or {}
        citations_by_file = citations_by_file or {}

        for item in findings:
            key = (item.file, item.line, item.rule_id)
            if key in existing_keys:
                self.duplicates.append(dedup_key(item.file, item.line, item.rule_id))
                continue
            row = ReviewComment(
                id=new_id("comment"),
                task_id=task_id,
                artifact_id=artifact_id,
                finding_id=dedup_key(item.file, item.line, item.rule_id),
                rule_id=item.rule_id,
                rule_version=item.rule_version,
                cwe=item.cwe,
                severity=str(item.severity),
                file=item.file,
                line=item.line,
                evidence=item.evidence,
                message=item.message,
                suggestion="",
                confidence=item.confidence,
                confidence_level=str(item.confidence_level),
                auto_fixable=item.auto_fixable,
                fix_category=str(item.fix_category) if item.fix_category else None,
                impact_scope=impact_scope_by_symbol.get(item.symbol or "", ""),
                call_graph_summary={"symbol": item.symbol} if item.symbol else {},
                citations=citations_by_file.get(item.file, []),
                suppressed=item.confidence_level.value == "suppressed",
            )
            self.session.add(row)
            existing_keys.add(key)
            self.created.append(row)
        self.session.flush()
        return self.created

    def list_by_task(self, task_id: str, *, include_suppressed: bool = False) -> list[ReviewComment]:
        statement = select(ReviewComment).where(ReviewComment.task_id == task_id)
        if not include_suppressed:
            statement = statement.where(ReviewComment.suppressed.is_(False))
        statement = statement.order_by(ReviewComment.severity, ReviewComment.file, ReviewComment.line)
        return list(self.session.execute(statement).scalars())

    def list_auto_fixable(self, task_id: str, *, comment_ids: list[str] | None = None) -> list[ReviewComment]:
        statement = select(ReviewComment).where(
            ReviewComment.task_id == task_id,
            ReviewComment.auto_fixable.is_(True),
            ReviewComment.suppressed.is_(False),
        )
        if comment_ids:
            statement = statement.where(ReviewComment.id.in_(comment_ids))
        return list(self.session.execute(statement.order_by(ReviewComment.file, ReviewComment.line)).scalars())

    def get(self, comment_id: str) -> ReviewComment | None:
        return self.session.get(ReviewComment, comment_id)


class PatchStore:
    """``fix_patch`` 仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find_by_idempotency(self, task_id: str, idempotency_key: str) -> FixPatch | None:
        return self.session.execute(
            select(FixPatch).where(
                FixPatch.task_id == task_id, FixPatch.idempotency_key == idempotency_key
            )
        ).scalars().first()

    def next_version(self, task_id: str) -> int:
        current = self.session.execute(
            select(func.max(FixPatch.patch_version)).where(FixPatch.task_id == task_id)
        ).scalar()
        return int(current or 0) + 1

    def create_candidate(
        self,
        *,
        task_id: str,
        candidate: PatchCandidate,
        artifact_id: str | None,
        idempotency_key: str,
        target_branch: str | None = None,
    ) -> tuple[FixPatch, bool]:
        existing = self.find_by_idempotency(task_id, idempotency_key)
        if existing is not None:
            return existing, False

        branch = target_branch or candidate.target_branch
        if branch in PROTECTED_BRANCHES:
            raise CodePilotError(
                ErrorCode.FORBIDDEN,
                f"补丁只能写入任务分支，禁止写入 {branch}（FR-041）",
                details={"target_branch": branch},
            )

        row = FixPatch(
            id=new_id("patch"),
            task_id=task_id,
            artifact_id=artifact_id,
            patch_version=candidate.patch_version,
            patch_hash=candidate.patch_hash,
            base_commit=candidate.base_commit,
            target_branch=branch,
            status=str(PatchStatus.CANDIDATE),
            fix_category=str(candidate.fix_category),
            finding_refs=list(candidate.finding_refs),
            changed_files=list(candidate.changed_files),
            changed_functions=list(candidate.changed_functions),
            diff_text=candidate.diff,
            added_lines=candidate.added_lines,
            removed_lines=candidate.removed_lines,
            idempotency_key=idempotency_key,
        )
        self.session.add(row)
        self.session.flush()
        record_event(
            self.session,
            actor_id="coordinator",
            actor_role="coordinator",
            action="create_patch_candidate",
            entity_type="fix_patch",
            entity_id=row.id,
            event_type="patch_created",
            task_id=task_id,
            after_state={
                "patch_version": row.patch_version,
                "patch_hash": row.patch_hash,
                "changed_files": row.changed_files,
                "fix_category": row.fix_category,
            },
            idempotency_key=idempotency_key,
        )
        return row, True

    def get(self, patch_id: str, *, required: bool = True) -> FixPatch | None:
        row = self.session.get(FixPatch, patch_id)
        if row is None and required:
            raise CodePilotError(ErrorCode.RESOURCE_NOT_FOUND, f"补丁不存在：{patch_id}")
        return row

    def latest(self, task_id: str) -> FixPatch | None:
        return self.session.execute(
            select(FixPatch)
            .where(FixPatch.task_id == task_id)
            .order_by(FixPatch.patch_version.desc())
        ).scalars().first()

    def record_evidence(
        self,
        patch_id: str,
        *,
        scope_drift: bool,
        scope_drift_files: list[str],
        wide_impact: bool,
        coverage_before: float | None = None,
        coverage_after: float | None = None,
        coverage_delta: float | None = None,
        test_gap: bool = False,
        sandbox_run_id: str | None = None,
    ) -> FixPatch:
        row = self.get(patch_id)
        assert row is not None
        row.scope_drift = scope_drift
        row.scope_drift_files = list(scope_drift_files)
        row.wide_impact = wide_impact
        row.coverage_before = coverage_before
        row.coverage_after = coverage_after
        row.coverage_delta = coverage_delta
        row.test_gap = test_gap
        row.sandbox_run_id = sandbox_run_id
        self.session.flush()
        return row

    def set_status(self, patch_id: str, status: PatchStatus) -> FixPatch:
        row = self.get(patch_id)
        assert row is not None
        row.status = str(status)
        if status is PatchStatus.APPLIED:
            row.applied_at = utcnow()
        if status is PatchStatus.ROLLED_BACK:
            row.rolled_back_at = utcnow()
        self.session.flush()
        return row

    def list_by_task(self, task_id: str) -> list[FixPatch]:
        return list(
            self.session.execute(
                select(FixPatch).where(FixPatch.task_id == task_id).order_by(FixPatch.patch_version)
            ).scalars()
        )


class SandboxRunStore:
    """``sandbox_run`` 仓储（输出必须已脱敏）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        sandbox_run_id: str,
        task_id: str | None,
        patch_id: str | None,
        image: str,
        status: SandboxRunStatus,
        exit_code: int | None,
        duration_ms: int,
        limits: dict[str, Any],
        stdout: str,
        stderr: str,
        resource_usage: dict[str, Any] | None = None,
        test_summary: dict[str, Any] | None = None,
        container_id: str | None = None,
        coverage_before: float | None = None,
        coverage_after: float | None = None,
        coverage_delta: float | None = None,
        degraded: bool = False,
        degraded_reason: str | None = None,
    ) -> SandboxRun:
        from domain.sanitize import sanitize_text

        row = SandboxRun(
            id=sandbox_run_id,
            task_id=task_id,
            patch_id=patch_id,
            container_id=container_id,
            image=image,
            status=str(status),
            exit_code=exit_code,
            degraded=degraded,
            degraded_reason=degraded_reason,
            duration_ms=duration_ms,
            cpu_limit=float(limits.get("cpu", 1.0)),
            memory_limit_mb=int(limits.get("memory_mb", 512)),
            disk_limit_mb=int(limits.get("disk_mb", 100)),
            timeout_seconds=int(limits.get("timeout_seconds", 60)),
            network=str(limits.get("network", "none")),
            coverage_before=coverage_before,
            coverage_after=coverage_after,
            coverage_delta=coverage_delta,
            resource_usage=resource_usage or {},
            stdout_sanitized=sanitize_text(stdout),
            stderr_sanitized=sanitize_text(stderr),
            test_summary=test_summary or {},
        )
        self.session.add(row)
        self.session.flush()
        record_event(
            self.session,
            actor_id="verify-agent",
            actor_role="agent",
            action="run_sandbox",
            entity_type="sandbox_run",
            entity_id=sandbox_run_id,
            event_type="sandbox_finished",
            task_id=task_id,
            after_state={
                "status": str(status),
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "network": row.network,
                "degraded": degraded,
            },
            error_code=None if status is SandboxRunStatus.PASSED else str(ErrorCode.SANDBOX_TIMEOUT)
            if status is SandboxRunStatus.TIMEOUT
            else None,
        )
        return row

    def get(self, sandbox_run_id: str) -> SandboxRun | None:
        return self.session.get(SandboxRun, sandbox_run_id)

    def list_by_task(self, task_id: str) -> list[SandboxRun]:
        return list(
            self.session.execute(
                select(SandboxRun).where(SandboxRun.task_id == task_id).order_by(SandboxRun.created_at)
            ).scalars()
        )


class ApprovalStore:
    """``approval`` 仓储：只追加；决定绑定 patch_version（FR-070~074）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find(self, patch_id: str, patch_version: int) -> Approval | None:
        return self.session.execute(
            select(Approval).where(Approval.patch_id == patch_id, Approval.patch_version == patch_version)
        ).scalars().first()

    def record(
        self,
        *,
        task_id: str,
        patch_id: str,
        decided_by: str,
        decided_role: str,
        decision: ApprovalDecision,
        reason: str,
        patch_version: int,
        patch_hash: str,
    ) -> tuple[Approval, bool]:
        if decided_role != ActorRole.APPROVER:
            raise CodePilotError(
                ErrorCode.FORBIDDEN,
                f"仅 approver 可以审批，当前角色：{decided_role}（FR-071）",
                details={"decided_role": decided_role},
            )
        if decision is ApprovalDecision.REJECT and not reason.strip():
            raise CodePilotError(ErrorCode.INVALID_INPUT, "拒绝审批必须填写原因（FR-074）")

        existing = self.find(patch_id, patch_version)
        if existing is not None:
            if existing.decision == str(decision) and existing.decided_by == decided_by:
                return existing, False
            raise CodePilotError(
                ErrorCode.CONFLICT,
                f"补丁版本 {patch_version} 已有审批记录",
                details={"patch_id": patch_id, "patch_version": patch_version},
            )

        row = Approval(
            id=new_id("approval"),
            task_id=task_id,
            patch_id=patch_id,
            decided_by=decided_by,
            decided_role=decided_role,
            decision=str(decision),
            reason=reason.strip(),
            patch_version=patch_version,
            patch_hash=patch_hash,
        )
        self.session.add(row)
        self.session.flush()
        record_event(
            self.session,
            actor_id=decided_by,
            actor_role=decided_role,
            action="decide_patch",
            entity_type="approval",
            entity_id=row.id,
            event_type="approval_recorded",
            task_id=task_id,
            after_state={
                "patch_id": patch_id,
                "patch_version": patch_version,
                "patch_hash": patch_hash,
                "decision": str(decision),
                "reason": reason,
            },
        )
        return row, True

    def list_by_task(self, task_id: str) -> list[Approval]:
        return list(
            self.session.execute(
                select(Approval).where(Approval.task_id == task_id).order_by(Approval.created_at)
            ).scalars()
        )

    def has_approval(self, patch_id: str, patch_version: int) -> bool:
        found = self.find(patch_id, patch_version)
        return found is not None and found.decision == str(ApprovalDecision.APPROVE)


class ToolExecutionStore:
    """``tool_execution`` 仓储：幂等返回原结果（FR-017）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def find(
        self, *, task_id: str, agent_id: str, tool_name: str, idempotency_key: str
    ) -> ToolExecution | None:
        return self.session.execute(
            select(ToolExecution).where(
                ToolExecution.task_id == task_id,
                ToolExecution.agent_id == agent_id,
                ToolExecution.tool_name == tool_name,
                ToolExecution.idempotency_key == idempotency_key,
            )
        ).scalars().first()

    def record(
        self,
        *,
        task_id: str,
        parent_task_id: str | None,
        agent_id: str,
        actor_role: str,
        tool_name: str,
        access: str,
        allowed: bool,
        params_hash: str,
        result_hash: str | None,
        result_summary: dict[str, Any],
        idempotency_key: str,
        duration_ms: int,
        denied_reason: str | None = None,
        error_code: str | None = None,
        result_ref: str | None = None,
        tool_version: str = "1.0",
    ) -> ToolExecution:
        row = ToolExecution(
            id=new_id("tool"),
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            actor_role=actor_role,
            tool_name=tool_name,
            tool_version=tool_version,
            access=access,
            allowed=allowed,
            denied_reason=denied_reason,
            error_code=error_code,
            params_hash=params_hash,
            result_hash=result_hash,
            result_summary=result_summary,
            result_ref=result_ref,
            idempotency_key=idempotency_key,
            duration_ms=duration_ms,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list_by_task(self, task_id: str) -> list[ToolExecution]:
        return list(
            self.session.execute(
                select(ToolExecution)
                .where(ToolExecution.task_id == task_id)
                .order_by(ToolExecution.created_at)
            ).scalars()
        )


class EvalStore:
    """``eval_case`` / ``eval_run`` / ``eval_result`` 仓储。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_case(self, *, dataset: str, case_id: str, payload: dict[str, Any], tags: list[str]) -> EvalCase:
        row_id = f"{dataset}:{case_id}"
        row = self.session.get(EvalCase, row_id)
        if row is None:
            row = EvalCase(id=row_id, dataset=dataset, case_id=case_id, payload=payload, tags=tags)
            self.session.add(row)
        else:
            row.payload = payload
            row.tags = tags
        self.session.flush()
        return row

    def list_cases(self, dataset: str) -> list[EvalCase]:
        return list(
            self.session.execute(
                select(EvalCase).where(EvalCase.dataset == dataset).order_by(EvalCase.case_id)
            ).scalars()
        )

    def create_run(
        self, *, run_id: str, dataset: str, modes: list[str], runs_per_case: int, idempotency_key: str
    ) -> EvalRun:
        """创建或复用评测运行：同一 run_id 重复调用是幂等的。"""
        existing = self.session.get(EvalRun, run_id)
        if existing is not None:
            existing.modes = list(modes)
            existing.runs_per_case = runs_per_case
            existing.status = "running"
            self.session.flush()
            return existing
        row = EvalRun(
            id=run_id,
            dataset=dataset,
            modes=modes,
            runs_per_case=runs_per_case,
            idempotency_key=idempotency_key,
            status="running",
        )
        self.session.add(row)
        self.session.flush()
        return row

    def add_result(
        self,
        *,
        run_id: str,
        case_id: str,
        mode: str,
        run_index: int,
        payload: dict[str, Any],
    ) -> EvalResult:
        row_id = f"{run_id}:{case_id}:{mode}:{run_index}"
        row = self.session.get(EvalResult, row_id) or EvalResult(
            id=row_id, run_id=run_id, case_id=case_id, mode=mode, run_index=run_index
        )
        row.passed = bool(payload.get("passed", False))
        row.review_task_id = payload.get("review_task_id")
        row.finding_recall = payload.get("finding_recall")
        row.finding_precision = payload.get("finding_precision")
        row.patch_valid = payload.get("patch_valid")
        row.verify_passed = payload.get("verify_passed")
        row.route_correct = payload.get("route_correct")
        row.artifact_schema_ok = payload.get("artifact_schema_ok")
        row.task_converged = payload.get("task_converged")
        row.trace_complete = payload.get("trace_complete")
        row.recovery_stable = payload.get("recovery_stable")
        row.latency_ms = int(payload.get("latency_ms", 0))
        row.tokens = int(payload.get("tokens", 0))
        row.tool_calls = int(payload.get("tool_calls", 0))
        row.retries = int(payload.get("retries", 0))
        row.injected_fault = payload.get("injected_fault")
        row.recovery_action = payload.get("recovery_action")
        row.security_invariants = payload.get("security_invariants", {})
        row.trace = payload.get("trace", {})
        row.notes = payload.get("notes", "")
        self.session.add(row)
        self.session.flush()
        return row

    def finish_run(self, run_id: str, *, summary: dict[str, Any], report_ref: str | None, status: str = "completed") -> EvalRun:
        row = self.session.get(EvalRun, run_id)
        if row is None:
            raise CodePilotError(ErrorCode.RESOURCE_NOT_FOUND, f"评测运行不存在：{run_id}")
        row.status = status
        row.summary = summary
        row.report_ref = report_ref
        row.finished_at = utcnow()
        self.session.flush()
        return row

    def get_run(self, run_id: str) -> EvalRun | None:
        return self.session.get(EvalRun, run_id)

    def list_results(self, run_id: str) -> list[EvalResult]:
        return list(
            self.session.execute(
                select(EvalResult)
                .where(EvalResult.run_id == run_id)
                .order_by(EvalResult.case_id, EvalResult.mode, EvalResult.run_index)
            ).scalars()
        )


def ensure_aware(value: datetime | None) -> datetime | None:
    from domain.clock import ensure_utc

    return ensure_utc(value)


__all__ = [
    "PROTECTED_BRANCHES",
    "ApprovalStore",
    "CommentStore",
    "EvalStore",
    "PatchStore",
    "SandboxRunStore",
    "ToolExecutionStore",
]
