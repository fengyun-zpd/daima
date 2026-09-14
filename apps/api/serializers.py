"""API 序列化：数据库行 → 响应模型。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from apps.api.schemas import (
    ArtifactSummary,
    AuditEventResponse,
    ChildTaskResponse,
    CommentResponse,
    PatchResponse,
    ReviewDetailResponse,
    ReviewTaskResponse,
)
from domain.clock import ensure_utc
from repositories.models import (
    A2AArtifact,
    Approval,
    AuditEvent,
    FixPatch,
    ReviewComment,
    ReviewTask,
)
from repositories.models import (
    A2ATask as A2ATaskRow,
)
from repositories.store import A2ATaskStore, ArtifactStore, ReviewTaskStore


def task_response(row: ReviewTask) -> ReviewTaskResponse:
    return ReviewTaskResponse(
        id=row.id,
        trace_id=row.trace_id,
        mode=row.mode,
        status=row.status,
        state_version=row.state_version,
        owner=row.owner,
        actor_id=row.actor_id,
        custom_task_id=row.custom_task_id,
        input_type=row.input_type,
        base_commit=row.base_commit,
        context_policy=row.context_policy,
        input_summary=row.input_summary or {},
        summary=row.summary or {},
        current_step=row.current_step,
        needs_human_from=row.needs_human_from,
        error_code=row.error_code,
        error_message=row.error_message,
        idempotency_key=row.idempotency_key,
        created_at=ensure_utc(row.created_at),
        updated_at=ensure_utc(row.updated_at),
        finished_at=ensure_utc(row.finished_at),
    )


def child_response(row: A2ATaskRow) -> ChildTaskResponse:
    return ChildTaskResponse(
        id=row.id,
        agent_id=row.agent_id,
        task_type=row.task_type,
        status=row.status,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        deadline=ensure_utc(row.deadline),
        state_version=row.state_version,
        correlation_id=row.correlation_id,
        transport=row.transport,
        error_code=row.error_code,
        error_message=row.error_message,
        duration_ms=row.duration_ms,
        created_at=ensure_utc(row.created_at),
        completed_at=ensure_utc(row.completed_at),
    )


def artifact_response(row: A2AArtifact) -> ArtifactSummary:
    return ArtifactSummary(
        id=row.id,
        task_id=row.task_id,
        artifact_type=row.artifact_type,
        schema_version=row.schema_version,
        content_hash=row.content_hash,
        size_bytes=row.size_bytes,
        validated=row.validated,
        validation_error=row.validation_error,
        created_at=ensure_utc(row.created_at),
    )


def comment_response(row: ReviewComment) -> CommentResponse:
    return CommentResponse(
        id=row.id,
        file=row.file,
        line=row.line,
        rule_id=row.rule_id,
        cwe=row.cwe,
        severity=row.severity,
        confidence=row.confidence,
        confidence_level=row.confidence_level,
        message=row.message,
        evidence=row.evidence,
        suggestion=row.suggestion,
        auto_fixable=row.auto_fixable,
        fix_category=row.fix_category,
        impact_scope=row.impact_scope,
        citations=list(row.citations or []),
    )


def patch_response(row: FixPatch, approvals: list[Approval] | None = None) -> PatchResponse:
    return PatchResponse(
        id=row.id,
        task_id=row.task_id,
        patch_version=row.patch_version,
        patch_hash=row.patch_hash,
        status=row.status,
        fix_category=row.fix_category,
        target_branch=row.target_branch,
        changed_files=list(row.changed_files or []),
        changed_functions=list(row.changed_functions or []),
        scope_drift=row.scope_drift,
        scope_drift_files=list(row.scope_drift_files or []),
        wide_impact=row.wide_impact,
        coverage_before=row.coverage_before,
        coverage_after=row.coverage_after,
        coverage_delta=row.coverage_delta,
        test_gap=row.test_gap,
        sandbox_run_id=row.sandbox_run_id,
        diff=row.diff_text,
        approvals=[
            {
                "id": item.id,
                "decided_by": item.decided_by,
                "decided_role": item.decided_role,
                "decision": item.decision,
                "reason": item.reason,
                "patch_version": item.patch_version,
                "created_at": ensure_utc(item.created_at).isoformat(),
            }
            for item in (approvals or [])
        ],
    )


def audit_response(row: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        id=row.id,
        trace_id=row.trace_id,
        task_id=row.task_id,
        parent_task_id=row.parent_task_id,
        agent_id=row.agent_id,
        actor_id=row.actor_id,
        actor_role=row.actor_role,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        event_type=row.event_type,
        state_version=row.state_version,
        before_state=row.before_state or {},
        after_state=row.after_state or {},
        error_code=row.error_code,
        duration_ms=row.duration_ms,
        created_at=ensure_utc(row.created_at),
    )


def audit_tail(session: Session, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    from repositories.audit import list_events

    events = list_events(session, task_id=task_id, limit=limit)
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "agent_id": event.agent_id,
            "state_version": event.state_version,
            "error_code": event.error_code,
            "created_at": ensure_utc(event.created_at).isoformat(),
        }
        for event in events
    ]


def review_detail(session: Session, task_id: str, *, include_suppressed: bool = False) -> ReviewDetailResponse:
    task = ReviewTaskStore(session).get(task_id)
    assert task is not None
    children = A2ATaskStore(session).list_by_parent(task_id)
    artifacts = ArtifactStore(session).list_by_parent(task_id)
    from repositories.records import CommentStore

    comments = CommentStore(session).list_by_task(task_id, include_suppressed=include_suppressed)
    return ReviewDetailResponse(
        task=task_response(task),
        child_tasks=[child_response(row) for row in children],
        artifacts=[artifact_response(row) for row in artifacts],
        comments=[comment_response(row) for row in comments],
        audit_tail=audit_tail(session, task_id),
    )


__all__ = [
    "artifact_response",
    "audit_response",
    "audit_tail",
    "child_response",
    "comment_response",
    "patch_response",
    "review_detail",
    "task_response",
]
