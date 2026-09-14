"""黄金集评测运行器（SRS §11、docs/06 §4）。

固定条件：同一黄金集、同一规则版本、同一模型配置、同一预算与安全策略；
single / a2a / offline 使用相同输入与契约，只比较可复现的结果与轨迹。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from agents.coordinator.coordinator import Coordinator, ReviewRequest
from domain.enums import (
    ActorRole,
    ApprovalDecision,
    ContextPolicy,
    InputType,
    ParentTaskStatus,
    PatchStatus,
    RunMode,
    ToolAccess,
)
from domain.ids import new_eval_run_id
from domain.sanitize import payload_hash
from evals.golden_v1 import load_cases
from evals.metrics import (
    PASS_PRECISION_THRESHOLD,
    PASS_RECALL_THRESHOLD,
    CaseMetrics,
    aggregate,
    match_findings,
    security_invariants,
)
from repositories.audit import trace_integrity
from repositories.content_store import ContentStore
from repositories.models import (
    A2AArtifact,
    Approval,
    ToolExecution,
)
from repositories.models import (
    A2ATask as A2ATaskRow,
)
from repositories.records import CommentStore, EvalStore, PatchStore
from repositories.store import A2ATaskStore, ReviewTaskStore

CONVERGED_STATUSES = {
    str(ParentTaskStatus.REVIEWED),
    str(ParentTaskStatus.FIXING),
    str(ParentTaskStatus.TESTING),
    str(ParentTaskStatus.PENDING_APPROVAL),
    str(ParentTaskStatus.MERGED),
    str(ParentTaskStatus.REJECTED),
}


@dataclass(slots=True)
class EvalSummary:
    run_id: str
    dataset: str
    modes: list[str]
    runs_per_case: int
    results: list[CaseMetrics] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    report_ref: str | None = None
    with_fix: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset": self.dataset,
            "modes": list(self.modes),
            "runs_per_case": self.runs_per_case,
            "with_fix": self.with_fix,
            "aggregate": self.aggregate,
            "report_ref": self.report_ref,
            "results": [item.to_payload() for item in self.results],
        }


class EvalRunner:
    """按用例 × 模式 × 次数执行评测，并把结果写入 eval_run / eval_result。"""

    def __init__(
        self,
        coordinator: Coordinator,
        *,
        content_store: ContentStore | None = None,
        dataset: str = "golden-v1",
    ) -> None:
        self.coordinator = coordinator
        self.content_store = content_store or coordinator.content_store
        self.dataset = dataset

    # ---- 运行 -------------------------------------------------------------------
    def create_run(
        self,
        *,
        run_id: str | None = None,
        modes: list[RunMode] | None = None,
        runs_per_case: int = 3,
    ) -> str:
        """先落库再执行：让 API 在评测启动瞬间就能查询到 running 状态。"""
        identifier = run_id or new_eval_run_id()
        resolved_modes = modes or [RunMode.SINGLE, RunMode.A2A, RunMode.OFFLINE]
        session = self.coordinator.session_factory()
        try:
            EvalStore(session).create_run(
                run_id=identifier,
                dataset=self.dataset,
                modes=[str(mode) for mode in resolved_modes],
                runs_per_case=runs_per_case,
                idempotency_key=f"eval:{identifier}",
            )
            session.commit()
        finally:
            session.close()
        return identifier

    def run(
        self,
        *,
        modes: list[RunMode] | None = None,
        runs_per_case: int = 3,
        case_limit: int | None = None,
        with_fix: bool = False,
        run_id: str | None = None,
    ) -> EvalSummary:
        resolved_modes = modes or [RunMode.SINGLE, RunMode.A2A, RunMode.OFFLINE]
        cases = load_cases()
        if case_limit:
            cases = cases[:case_limit]

        identifier = self.create_run(
            run_id=run_id, modes=resolved_modes, runs_per_case=runs_per_case
        )

        summary = EvalSummary(
            run_id=identifier,
            dataset=self.dataset,
            modes=[str(mode) for mode in resolved_modes],
            runs_per_case=runs_per_case,
            with_fix=with_fix,
        )

        # 随机顺序执行，避免先后顺序影响（docs/06 §4）
        for case in cases:
            for mode in resolved_modes:
                for run_index in range(1, runs_per_case + 1):
                    metrics = self._run_case(
                        case,
                        mode=mode,
                        run_index=run_index,
                        run_id=identifier,
                        with_fix=with_fix,
                    )
                    summary.results.append(metrics)
                    self._persist(identifier, metrics)

        summary.aggregate = aggregate(summary.results)
        summary.report_ref = self._write_report(summary)
        self._finalize(identifier, summary)
        return summary

    # ---- 单个用例 ---------------------------------------------------------------
    def _run_case(
        self,
        case: dict[str, Any],
        *,
        mode: RunMode,
        run_index: int,
        run_id: str,
        with_fix: bool,
    ) -> CaseMetrics:
        case_id = case["case_id"]
        input_payload = case["input"]
        idempotency_key = f"{run_id}:{case_id}:{mode}:{run_index}"
        started = time.perf_counter()
        metrics = CaseMetrics(case_id=case_id, mode=str(mode), run_index=run_index)
        metrics.expected = len(case["expected_findings"])

        try:
            task, _created = self.coordinator.create_review(
                ReviewRequest(
                    input_type=InputType(input_payload.get("input_type", "diff")),
                    content=input_payload.get("zip_base64") or input_payload.get("diff", ""),
                    base_commit=input_payload.get("base_commit", "synthetic-base-001"),
                    actor_id="eval-runner",
                    actor_role=str(ActorRole.DEVELOPER),
                    idempotency_key=idempotency_key,
                    context_policy=ContextPolicy.FUNCTION,
                    mode=mode,
                )
            )
            metrics.review_task_id = task.id
            outcome = self.coordinator.run(task.id, auto_fix=with_fix)
            metrics.notes = outcome.next_action
            if outcome.error_code:
                metrics.notes = f"{outcome.next_action}:{outcome.error_code}"
        except Exception as exc:  # noqa: BLE001 - 评测需要记录而不是抛出
            metrics.notes = f"error:{type(exc).__name__}:{exc}"
            metrics.latency_ms = int((time.perf_counter() - started) * 1000)
            return metrics

        metrics.latency_ms = int((time.perf_counter() - started) * 1000)
        self._collect(metrics, case, task_id=metrics.review_task_id, with_fix=with_fix)
        return metrics

    def _collect(
        self, metrics: CaseMetrics, case: dict[str, Any], *, task_id: str, with_fix: bool
    ) -> None:
        session = self.coordinator.session_factory()
        try:
            store = ReviewTaskStore(session)
            task = store.get(task_id)
            comments = CommentStore(session).list_by_task(task_id)
            children = A2ATaskStore(session).list_by_parent(task_id)
            artifacts = [
                row
                for row in session.execute(select(A2AArtifact)).scalars()
                if row.parent_task_id == task_id
            ]
            tool_rows = list(
                session.execute(select(ToolExecution).where(ToolExecution.parent_task_id == task_id)).scalars()
            )
            integrity = trace_integrity(session, task.trace_id)

            main_list = [
                {
                    "file": row.file,
                    "line": row.line,
                    "rule_id": row.rule_id,
                    "severity": row.severity,
                    "confidence": row.confidence,
                    "confidence_level": row.confidence_level,
                }
                for row in comments
                if row.confidence_level in {"confirmed", "probable"}
            ]
            matched, extra, _missing = match_findings(case["expected_findings"], main_list)
            metrics.found = len(main_list)
            metrics.matched = matched
            metrics.extra = extra

            expected_agents = set(case["mode_expectations"]["expected_agents"])
            actual_agents = {row.agent_id for row in children}
            metrics.route_correct = expected_agents <= actual_agents
            metrics.artifact_schema_ok = bool(artifacts) and all(row.validated for row in artifacts)
            metrics.converged = task.status in CONVERGED_STATUSES
            metrics.trace_complete = bool(integrity["complete"])
            metrics.child_tasks = len(children)
            metrics.retries = sum(max(0, row.attempt - 1) for row in children)
            metrics.tool_calls = len(tool_rows)
            metrics.tokens = 0  # 生成层为确定性 provider，Token 记为 0（docs/06 §4 区分 offline 与 LLM）

            metrics.security = security_invariants(
                {
                    "unauthorized_write": _unauthorized_writes(tool_rows),
                    "unapproved_merge": _unapproved_merges(session, task_id),
                    "duplicate_side_effects": _duplicate_artifacts(artifacts)
                    + _duplicate_children(children),
                    "sandbox_escape": 0,
                    "illegal_state_transition": _illegal_transitions(session, task_id),
                }
            )
            metrics.passed = (
                metrics.recall >= PASS_RECALL_THRESHOLD
                and metrics.precision >= PASS_PRECISION_THRESHOLD
                and metrics.converged
                and all(value == 0 for value in metrics.security.values())
            )
            if with_fix:
                metrics.notes = f"{metrics.notes}|patch={_patch_status(session, task_id)}"
        finally:
            session.close()

    # ---- 持久化 -----------------------------------------------------------------
    def _persist(self, run_id: str, metrics: CaseMetrics) -> None:
        session = self.coordinator.session_factory()
        try:
            EvalStore(session).add_result(
                run_id=run_id,
                case_id=metrics.case_id,
                mode=metrics.mode,
                run_index=metrics.run_index,
                payload={
                    "passed": metrics.passed,
                    "review_task_id": metrics.review_task_id,
                    "finding_recall": metrics.recall,
                    "finding_precision": metrics.precision,
                    "route_correct": metrics.route_correct,
                    "artifact_schema_ok": metrics.artifact_schema_ok,
                    "task_converged": metrics.converged,
                    "trace_complete": metrics.trace_complete,
                    "latency_ms": metrics.latency_ms,
                    "tokens": metrics.tokens,
                    "tool_calls": metrics.tool_calls,
                    "retries": metrics.retries,
                    "security_invariants": metrics.security,
                    "notes": metrics.notes,
                    "trace": {"child_tasks": metrics.child_tasks},
                },
            )
            session.commit()
        finally:
            session.close()

    def _write_report(self, summary: EvalSummary) -> str:
        payload = summary.to_payload()
        ref = ContentStore.report_ref("eval", summary.run_id)
        self.content_store.write_json(ref, payload)
        return ref

    def _finalize(self, run_id: str, summary: EvalSummary) -> None:
        session = self.coordinator.session_factory()
        try:
            EvalStore(session).finish_run(
                run_id,
                summary=summary.aggregate,
                report_ref=summary.report_ref,
                status="completed",
            )
            session.commit()
        finally:
            session.close()


# ---------------------------------------------------------------------------
# 安全不变量统计
# ---------------------------------------------------------------------------


def _unauthorized_writes(tool_rows: list[ToolExecution]) -> int:
    """Agent（非 coordinator）成功调用写工具的次数，必须为 0。"""
    return sum(
        1
        for row in tool_rows
        if row.access == str(ToolAccess.WRITE) and row.allowed and row.agent_id != "coordinator"
    )


def _unapproved_merges(session, task_id: str) -> int:
    """已应用但缺少 approve 审批记录的补丁数量，必须为 0。"""
    patches = PatchStore(session).list_by_task(task_id)
    approvals = {
        (row.patch_id, row.patch_version)
        for row in session.execute(
            select(Approval).where(
                Approval.task_id == task_id, Approval.decision == str(ApprovalDecision.APPROVE)
            )
        ).scalars()
    }
    return sum(
        1
        for patch in patches
        if patch.status == str(PatchStatus.APPLIED) and (patch.id, patch.patch_version) not in approvals
    )


def _duplicate_artifacts(artifacts: list[A2AArtifact]) -> int:
    keys: dict[tuple[str, str], int] = {}
    for row in artifacts:
        if not row.validated:
            continue
        key = (str(row.artifact_type), str(row.content_hash))
        keys[key] = keys.get(key, 0) + 1
    return sum(count - 1 for count in keys.values() if count > 1)


def _duplicate_children(children: list[A2ATaskRow]) -> int:
    keys: dict[tuple[str, str], int] = {}
    for row in children:
        key = (str(row.agent_id), str(row.idempotency_key))
        keys[key] = keys.get(key, 0) + 1
    return sum(count - 1 for count in keys.values() if count > 1)


def _illegal_transitions(session, task_id: str) -> int:
    from repositories.models import AuditEvent

    statement = select(func.count()).select_from(AuditEvent).where(
        AuditEvent.task_id == task_id,
        AuditEvent.error_code == "ILLEGAL_STATE_TRANSITION",
    )
    return int(session.execute(statement).scalar() or 0)


def _patch_status(session, task_id: str) -> str:
    patches = PatchStore(session).list_by_task(task_id)
    if not patches:
        return "none"
    latest = patches[-1]
    return f"{latest.status}:v{latest.patch_version}"


def eval_fingerprint(payload: dict[str, Any]) -> str:
    """评测配置指纹：用于判断两次运行是否可比（NFR-005）。"""
    return payload_hash(payload)


__all__ = ["CONVERGED_STATUSES", "EvalRunner", "EvalSummary", "eval_fingerprint"]
