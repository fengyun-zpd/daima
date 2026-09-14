"""阶段二：数据库、幂等、乐观锁、追加式审计与恢复测试（FR-003/016/017/073/096/097）。"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from a2a.examples import example_a2a_task, example_envelope, example_finding_artifact, example_patch_candidate
from a2a.protocol import A2AMessage
from domain.clock import utcnow
from domain.enums import (
    ActorRole,
    ApprovalDecision,
    ArtifactType,
    ChildTaskStatus,
    ChildTaskType,
    ParentTaskStatus,
    PatchStatus,
    SandboxRunStatus,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_artifact_id, new_message_id, new_task_id, new_trace_id
from domain.sanitize import sanitize_text
from repositories.audit import AuditImmutabilityError, list_events
from repositories.models import (
    A2AArtifact,
    Approval,
    ReviewTask,
)
from repositories.models import (
    A2AMessage as A2AMessageRow,
)
from repositories.records import (
    ApprovalStore,
    CommentStore,
    EvalStore,
    PatchStore,
    SandboxRunStore,
    ToolExecutionStore,
)
from repositories.recovery import build_recovery_plan, list_recovery_plans, recovery_summary
from repositories.store import A2ATaskStore, ArtifactStore, MessageStore, ReviewTaskStore

ACTOR = "dev-1"
TRACE = "trace-0000000000000000000000000a"
IDEM = "req-0000000000000001"


def create_task(session, *, idempotency_key: str = IDEM, input_hash: str = "sha256:aa", task_id: str | None = None):
    store = ReviewTaskStore(session)
    return store.create(
        task_id=task_id or new_task_id(),
        trace_id=TRACE,
        actor_id=ACTOR,
        actor_role=str(ActorRole.DEVELOPER),
        mode="a2a",
        input_type="diff",
        input_hash=input_hash,
        input_summary={"files": ["app/config.py"]},
        base_commit="synthetic-base-001",
        context_policy="function",
        idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------------
# 幂等创建
# ---------------------------------------------------------------------------


def test_repeated_create_returns_same_task(db_session) -> None:
    first, created_first = create_task(db_session)
    second, created_second = create_task(db_session)
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert len(db_session.execute(select(ReviewTask)).scalars().all()) == 1


def test_same_key_different_input_is_conflict(db_session) -> None:
    create_task(db_session)
    with pytest.raises(CodePilotError) as excinfo:
        create_task(db_session, input_hash="sha256:bb")
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_same_input_different_key_creates_second_task(db_session) -> None:
    """同一输入不同幂等键允许创建多个任务（评测需要同输入多次运行）。"""
    first, _ = create_task(db_session, idempotency_key="req-0000000000000001")
    second, created = create_task(db_session, idempotency_key="req-0000000000000002")
    assert created is True
    assert first.id != second.id


def test_short_idempotency_key_rejected(db_session) -> None:
    with pytest.raises(CodePilotError) as excinfo:
        create_task(db_session, idempotency_key="short")
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_KEY_REQUIRED


# ---------------------------------------------------------------------------
# 乐观锁与状态意图
# ---------------------------------------------------------------------------


def test_optimistic_lock_conflict(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key="intent-000000000001",
    )
    with pytest.raises(CodePilotError) as excinfo:
        store.apply_intent(
            task.id,
            expected_version=1,
            owner="coordinator",
            changes={"status": str(ParentTaskStatus.REVIEWED)},
            reason="stale_write",
            idempotency_key="intent-000000000002",
        )
    assert excinfo.value.code is ErrorCode.STATE_VERSION_CONFLICT
    assert excinfo.value.retryable is True


def test_state_version_increments(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    result = store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key="intent-000000000003",
    )
    assert result.applied is True
    assert result.row.state_version == 2
    assert result.row.status == str(ParentTaskStatus.REVIEWING)


def test_illegal_transition_is_rejected(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    with pytest.raises(CodePilotError) as excinfo:
        store.apply_intent(
            task.id,
            expected_version=1,
            owner="coordinator",
            changes={"status": str(ParentTaskStatus.MERGED)},
            reason="skip_approval",
            idempotency_key="intent-000000000004",
        )
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


def test_non_owner_cannot_write_parent(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    with pytest.raises(CodePilotError) as excinfo:
        store.apply_intent(
            task.id,
            expected_version=1,
            owner="review-agent",
            changes={"status": str(ParentTaskStatus.REVIEWING)},
            reason="agent_write_attempt",
            idempotency_key="intent-000000000005",
        )
    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED


def test_unknown_field_write_blocked(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    with pytest.raises(CodePilotError) as excinfo:
        store.apply_intent(
            task.id,
            expected_version=1,
            owner="coordinator",
            changes={"state_version": 99},
            reason="tamper",
            idempotency_key="intent-000000000006",
        )
    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED


def test_intent_replay_returns_original_result(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    key = "intent-000000000007"
    first = store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key=key,
    )
    second = store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key=key,
    )
    assert first.applied is True
    assert second.applied is False
    assert second.replayed is True
    assert second.row.state_version == 2
    events = list_events(db_session, task_id=task.id, event_type="state_intent_applied")
    assert len(events) == 1


def test_needs_human_records_resume_target(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key="intent-000000000008",
    )
    result = store.apply_intent(
        task.id,
        expected_version=2,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.NEEDS_HUMAN)},
        reason="budget_exceeded",
        idempotency_key="intent-000000000009",
    )
    assert result.row.needs_human_from == str(ParentTaskStatus.REVIEWING)


def test_human_resume_requires_admin_and_target(db_session) -> None:
    task, _ = create_task(db_session)
    store = ReviewTaskStore(db_session)
    store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.NEEDS_HUMAN)},
        reason="needs_human",
        idempotency_key="intent-00000000000a",
    )
    with pytest.raises(CodePilotError) as excinfo:
        store.human_resume(
            task.id,
            target_status=ParentTaskStatus.MERGED,
            expected_version=2,
            actor_id="admin-1",
            reason="尝试直接合并",
            idempotency_key="resume-000000000001",
        )
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION

    result = store.human_resume(
        task.id,
        target_status=ParentTaskStatus.REVIEWING,
        expected_version=2,
        actor_id="admin-1",
        reason="人工补充上下文后继续",
        idempotency_key="resume-000000000002",
    )
    assert result.row.status == str(ParentTaskStatus.REVIEWING)


# ---------------------------------------------------------------------------
# 追加式审计
# ---------------------------------------------------------------------------


def test_audit_events_are_written_for_creation(db_session) -> None:
    task, _ = create_task(db_session)
    events = list_events(db_session, trace_id=TRACE)
    assert events
    assert events[0].event_type == "task_started"
    assert events[0].entity_id == task.id


def test_audit_update_is_blocked_by_orm(db_session) -> None:
    task, _ = create_task(db_session)
    event = list_events(db_session, task_id=task.id)[0]
    event.action = "tampered"
    with pytest.raises(AuditImmutabilityError):
        db_session.flush()
    db_session.rollback()


def test_audit_update_is_blocked_in_database(db_session) -> None:
    task, _ = create_task(db_session)
    db_session.commit()
    with pytest.raises(DatabaseError):
        db_session.execute(text("UPDATE audit_event SET action='tampered' WHERE task_id=:t"), {"t": task.id})
        db_session.commit()
    db_session.rollback()


def test_audit_delete_is_blocked_in_database(db_session) -> None:
    create_task(db_session)
    db_session.commit()
    with pytest.raises(DatabaseError):
        db_session.execute(text("DELETE FROM audit_event"))
        db_session.commit()
    db_session.rollback()


def test_audit_sanitizes_secrets(db_session) -> None:
    from repositories.audit import record_event

    record_event(
        db_session,
        actor_id="admin-1",
        actor_role="admin",
        action="test",
        entity_type="tool",
        entity_id="read_file",
        event_type="tool_called",
        after_state={"api_key": "sk-live-abcdef123456", "path": "C:\\secret\\repo\\app.py"},
    )
    db_session.flush()
    event = list_events(db_session)[-1]
    assert "sk-live" not in str(event.after_state)
    assert "C:\\secret" not in str(event.after_state)


# ---------------------------------------------------------------------------
# 子任务幂等与终态
# ---------------------------------------------------------------------------


def test_child_task_idempotency(db_session) -> None:
    task, _ = create_task(db_session)
    store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, task_id=None, agent_id="impact-agent")
    child = child.model_copy(update={"idempotency_key": f"{task.id}:impact:v1", "task_id": new_task_id()})
    first, created_first = store.submit(child)
    second, created_second = store.submit(child)
    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_child_task_version_conflict(db_session) -> None:
    task, _ = create_task(db_session)
    store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, agent_id="review-agent", task_type=ChildTaskType.REVIEW)
    row, _ = store.submit(child)
    store.transition(
        row.id,
        ChildTaskStatus.WORKING,
        expected_version=1,
        idempotency_key="child-intent-0001",
    )
    with pytest.raises(CodePilotError) as excinfo:
        store.transition(
            row.id,
            ChildTaskStatus.COMPLETED,
            expected_version=1,
            idempotency_key="child-intent-0002",
        )
    assert excinfo.value.code is ErrorCode.STATE_VERSION_CONFLICT


def test_child_task_terminal_protection(db_session) -> None:
    task, _ = create_task(db_session)
    store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, agent_id="review-agent", task_type=ChildTaskType.REVIEW)
    row, _ = store.submit(child)
    store.transition(row.id, ChildTaskStatus.WORKING, expected_version=1, idempotency_key="child-intent-0003")
    store.transition(row.id, ChildTaskStatus.COMPLETED, expected_version=2, idempotency_key="child-intent-0004")
    with pytest.raises(CodePilotError) as excinfo:
        store.transition(row.id, ChildTaskStatus.WORKING, expected_version=3, idempotency_key="child-intent-0005")
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


def test_child_retry_only_when_error_retryable(db_session) -> None:
    task, _ = create_task(db_session)
    store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, agent_id="review-agent", task_type=ChildTaskType.REVIEW)
    row, _ = store.submit(child)
    store.transition(row.id, ChildTaskStatus.WORKING, expected_version=1, idempotency_key="child-intent-0006")
    store.transition(
        row.id,
        ChildTaskStatus.FAILED,
        expected_version=2,
        idempotency_key="child-intent-0007",
        error_code=str(ErrorCode.ARTIFACT_SCHEMA_INVALID),
    )
    with pytest.raises(CodePilotError) as excinfo:
        store.transition(row.id, ChildTaskStatus.WORKING, expected_version=3, idempotency_key="child-intent-0008")
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION


def test_child_retry_allowed_for_timeout(db_session) -> None:
    task, _ = create_task(db_session)
    store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, agent_id="review-agent", task_type=ChildTaskType.REVIEW)
    row, _ = store.submit(child)
    store.transition(row.id, ChildTaskStatus.WORKING, expected_version=1, idempotency_key="child-intent-0009")
    store.transition(
        row.id,
        ChildTaskStatus.FAILED,
        expected_version=2,
        idempotency_key="child-intent-000a",
        error_code=str(ErrorCode.TASK_TIMEOUT),
    )
    result = store.transition(
        row.id, ChildTaskStatus.WORKING, expected_version=3, idempotency_key="child-intent-000b", attempt=2
    )
    assert result.row.attempt == 2


# ---------------------------------------------------------------------------
# Artifact / Message
# ---------------------------------------------------------------------------


def test_artifact_dedup_and_validation_flag(db_session) -> None:
    task, _ = create_task(db_session)
    child_store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, agent_id="impact-agent")
    row, _ = child_store.submit(child)

    artifact_store = ArtifactStore(db_session)
    envelope = example_envelope(ArtifactType.IMPACT_REPORT, task_id=row.id)
    first, created_first = artifact_store.save(envelope, parent_task_id=task.id, agent_id="impact-agent", validated=True)
    second, created_second = artifact_store.save(envelope, parent_task_id=task.id, agent_id="impact-agent", validated=True)
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert first.validated is True


def test_artifact_rejected_event_written(db_session) -> None:
    task, _ = create_task(db_session)
    child_store = A2ATaskStore(db_session)
    row, _ = child_store.submit(example_a2a_task(parent_task_id=task.id, agent_id="impact-agent"))
    envelope = example_envelope(ArtifactType.IMPACT_REPORT, task_id=row.id)
    ArtifactStore(db_session).save(
        envelope, parent_task_id=task.id, agent_id="impact-agent", validated=False, validation_error="risk_level 缺失"
    )
    events = list_events(db_session, task_id=row.id)
    assert any(event.event_type == "artifact_rejected" for event in events)


def test_message_store_persists_summary_only(db_session) -> None:
    task, _ = create_task(db_session)
    row, _ = A2ATaskStore(db_session).submit(example_a2a_task(parent_task_id=task.id, agent_id="impact-agent"))
    message = A2AMessage(
        message_id=new_message_id(),
        task_id=row.id,
        type="task.completed",
        role="agent",
        correlation_id=row.correlation_id,
        artifact_refs=["artifact-1"],
        error=None,
        created_at=utcnow(),
    )
    MessageStore(db_session).append(message, parent_task_id=task.id, trace_id=TRACE)
    stored = db_session.execute(select(A2AMessageRow)).scalars().all()
    assert len(stored) == 1
    assert stored[0].payload_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# 意见 / 补丁 / 沙箱 / 审批 / 工具
# ---------------------------------------------------------------------------


def test_comment_dedup(db_session) -> None:
    task, _ = create_task(db_session)
    payload = example_finding_artifact()
    store = CommentStore(db_session)
    store.add_findings(task_id=task.id, artifact_id=None, findings=payload.findings)
    store.add_findings(task_id=task.id, artifact_id=None, findings=payload.findings)
    assert len(store.created) == 1
    assert len(store.duplicates) == 1
    assert len(store.list_by_task(task.id)) == 1


def test_patch_protected_branch_rejected(db_session) -> None:
    task, _ = create_task(db_session)
    store = PatchStore(db_session)
    candidate = example_patch_candidate()
    with pytest.raises(CodePilotError) as excinfo:
        store.create_candidate(
            task_id=task.id,
            candidate=candidate,
            artifact_id=None,
            idempotency_key="patch-idem-0001",
            target_branch="main",
        )
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_patch_version_and_idempotency(db_session) -> None:
    task, _ = create_task(db_session)
    store = PatchStore(db_session)
    candidate = example_patch_candidate()
    row, created = store.create_candidate(
        task_id=task.id, candidate=candidate, artifact_id=None, idempotency_key="patch-idem-0002"
    )
    again, created_again = store.create_candidate(
        task_id=task.id, candidate=candidate, artifact_id=None, idempotency_key="patch-idem-0002"
    )
    assert created is True
    assert created_again is False
    assert row.id == again.id
    assert store.next_version(task.id) == 2


def test_approval_requires_approver_role(db_session) -> None:
    task, _ = create_task(db_session)
    patch, _ = PatchStore(db_session).create_candidate(
        task_id=task.id,
        candidate=example_patch_candidate(),
        artifact_id=None,
        idempotency_key="patch-idem-0003",
    )
    with pytest.raises(CodePilotError) as excinfo:
        ApprovalStore(db_session).record(
            task_id=task.id,
            patch_id=patch.id,
            decided_by="dev-1",
            decided_role=str(ActorRole.DEVELOPER),
            decision=ApprovalDecision.APPROVE,
            reason="自批",
            patch_version=patch.patch_version,
            patch_hash=patch.patch_hash,
        )
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_approval_reject_requires_reason(db_session) -> None:
    task, _ = create_task(db_session)
    patch, _ = PatchStore(db_session).create_candidate(
        task_id=task.id,
        candidate=example_patch_candidate(),
        artifact_id=None,
        idempotency_key="patch-idem-0004",
    )
    with pytest.raises(CodePilotError) as excinfo:
        ApprovalStore(db_session).record(
            task_id=task.id,
            patch_id=patch.id,
            decided_by="approver-1",
            decided_role=str(ActorRole.APPROVER),
            decision=ApprovalDecision.REJECT,
            reason="   ",
            patch_version=patch.patch_version,
            patch_hash=patch.patch_hash,
        )
    assert excinfo.value.code is ErrorCode.INVALID_INPUT


def test_approval_is_idempotent_and_append_only(db_session) -> None:
    task, _ = create_task(db_session)
    patch, _ = PatchStore(db_session).create_candidate(
        task_id=task.id,
        candidate=example_patch_candidate(),
        artifact_id=None,
        idempotency_key="patch-idem-0005",
    )
    store = ApprovalStore(db_session)
    first, created_first = store.record(
        task_id=task.id,
        patch_id=patch.id,
        decided_by="approver-1",
        decided_role=str(ActorRole.APPROVER),
        decision=ApprovalDecision.APPROVE,
        reason="证据充分",
        patch_version=patch.patch_version,
        patch_hash=patch.patch_hash,
    )
    second, created_second = store.record(
        task_id=task.id,
        patch_id=patch.id,
        decided_by="approver-1",
        decided_role=str(ActorRole.APPROVER),
        decision=ApprovalDecision.APPROVE,
        reason="证据充分",
        patch_version=patch.patch_version,
        patch_hash=patch.patch_hash,
    )
    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert store.has_approval(patch.id, patch.patch_version)

    db_session.commit()
    with pytest.raises(DatabaseError):
        db_session.execute(text("DELETE FROM approval"))
        db_session.commit()
    db_session.rollback()


def test_approval_conflicting_decision_rejected(db_session) -> None:
    task, _ = create_task(db_session)
    patch, _ = PatchStore(db_session).create_candidate(
        task_id=task.id,
        candidate=example_patch_candidate(),
        artifact_id=None,
        idempotency_key="patch-idem-0006",
    )
    store = ApprovalStore(db_session)
    store.record(
        task_id=task.id,
        patch_id=patch.id,
        decided_by="approver-1",
        decided_role=str(ActorRole.APPROVER),
        decision=ApprovalDecision.APPROVE,
        reason="ok",
        patch_version=patch.patch_version,
        patch_hash=patch.patch_hash,
    )
    with pytest.raises(CodePilotError) as excinfo:
        store.record(
            task_id=task.id,
            patch_id=patch.id,
            decided_by="approver-2",
            decided_role=str(ActorRole.APPROVER),
            decision=ApprovalDecision.REJECT,
            reason="反悔",
            patch_version=patch.patch_version,
            patch_hash=patch.patch_hash,
        )
    assert excinfo.value.code is ErrorCode.CONFLICT


def test_sandbox_run_records_sanitized_output(db_session) -> None:
    task, _ = create_task(db_session)
    store = SandboxRunStore(db_session)
    row = store.record(
        sandbox_run_id="sandbox-00000000000000000001",
        task_id=task.id,
        patch_id=None,
        image="codepilot-sandbox:local",
        status=SandboxRunStatus.PASSED,
        exit_code=0,
        duration_ms=1234,
        limits={"cpu": 1, "memory_mb": 512, "disk_mb": 100, "timeout_seconds": 60, "network": "none"},
        stdout="token=sk-live-abcdef123456 at C:\\Users\\x\\repo\\app.py",
        stderr="",
    )
    assert "sk-live" not in row.stdout_sanitized
    assert "C:\\Users" not in row.stdout_sanitized
    assert row.network == "none"


def test_tool_execution_idempotency_lookup(db_session) -> None:
    store = ToolExecutionStore(db_session)
    store.record(
        task_id="task-1",
        parent_task_id=None,
        agent_id="review-agent",
        actor_role="agent",
        tool_name="read_file",
        access="read",
        allowed=True,
        params_hash="sha256:aa",
        result_hash="sha256:bb",
        result_summary={"lines": 10},
        idempotency_key="tool-idem-0001",
        duration_ms=5,
    )
    found = store.find(
        task_id="task-1", agent_id="review-agent", tool_name="read_file", idempotency_key="tool-idem-0001"
    )
    assert found is not None
    assert found.result_hash == "sha256:bb"


def test_tool_execution_unique_constraint(db_session) -> None:
    store = ToolExecutionStore(db_session)
    payload = {
        "task_id": "task-1",
        "parent_task_id": None,
        "agent_id": "review-agent",
        "actor_role": "agent",
        "tool_name": "read_file",
        "access": "read",
        "allowed": True,
        "params_hash": "sha256:aa",
        "result_hash": "sha256:bb",
        "result_summary": {},
        "idempotency_key": "tool-idem-0002",
        "duration_ms": 1,
    }
    store.record(**payload)
    with pytest.raises(IntegrityError):
        store.record(**payload)
    db_session.rollback()


def test_patch_status_transitions_persist(db_session) -> None:
    task, _ = create_task(db_session)
    store = PatchStore(db_session)
    patch, _ = store.create_candidate(
        task_id=task.id,
        candidate=example_patch_candidate(),
        artifact_id=None,
        idempotency_key="patch-idem-0007",
    )
    store.record_evidence(
        patch.id,
        scope_drift=False,
        scope_drift_files=[],
        wide_impact=False,
        coverage_before=0.8,
        coverage_after=0.81,
        coverage_delta=0.01,
        sandbox_run_id="sandbox-1",
    )
    store.set_status(patch.id, PatchStatus.APPLIED)
    refreshed = store.get(patch.id)
    assert refreshed.status == str(PatchStatus.APPLIED)
    assert refreshed.applied_at is not None


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def test_recovery_plan_does_not_replay_completed_children(db_session) -> None:
    task, _ = create_task(db_session)
    parent_store = ReviewTaskStore(db_session)
    child_store = A2ATaskStore(db_session)
    artifact_store = ArtifactStore(db_session)

    parent_store.apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key="intent-recover-0001",
    )

    review_child = example_a2a_task(
        parent_task_id=task.id, agent_id="review-agent", task_type=ChildTaskType.REVIEW, required_output_types=["Finding"]
    )
    review_row, _ = child_store.submit(review_child)
    child_store.transition(
        review_row.id, ChildTaskStatus.WORKING, expected_version=1, idempotency_key="child-rec-0001"
    )
    child_store.transition(
        review_row.id, ChildTaskStatus.COMPLETED, expected_version=2, idempotency_key="child-rec-0002"
    )
    finding_envelope = example_envelope(ArtifactType.FINDING, task_id=review_row.id)
    artifact_store.save(finding_envelope, parent_task_id=task.id, agent_id="review-agent", validated=True)

    impact_child = example_a2a_task(parent_task_id=task.id, agent_id="impact-agent")
    impact_row, _ = child_store.submit(impact_child)
    child_store.transition(
        impact_row.id, ChildTaskStatus.WORKING, expected_version=1, idempotency_key="child-rec-0003"
    )

    plan = build_recovery_plan(db_session, task.id)
    assert review_row.id in plan.completed_children
    assert impact_row.id in plan.pending_children
    assert review_row.id not in plan.pending_children
    assert plan.next_action == "await_children"
    assert plan.missing_artifacts == ["ImpactReport"]
    assert plan.will_replay_completed is False


def test_recovery_plan_finalizes_when_artifacts_present(db_session) -> None:
    task, _ = create_task(db_session)
    ReviewTaskStore(db_session).apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key="intent-recover-0002",
    )
    child_store = A2ATaskStore(db_session)
    artifact_store = ArtifactStore(db_session)
    for task_type, agent, artifact_type in (
        (ChildTaskType.REVIEW, "review-agent", ArtifactType.FINDING),
        (ChildTaskType.IMPACT, "impact-agent", ArtifactType.IMPACT_REPORT),
    ):
        child = example_a2a_task(parent_task_id=task.id, agent_id=agent, task_type=task_type)
        row, _ = child_store.submit(child)
        artifact_store.save(
            example_envelope(artifact_type, task_id=row.id),
            parent_task_id=task.id,
            agent_id=agent,
            validated=True,
        )
    plan = build_recovery_plan(db_session, task.id)
    assert plan.next_action == "finalize_review"
    assert plan.missing_artifacts == []


def test_recovery_plan_needs_human_without_retryable_child(db_session) -> None:
    task, _ = create_task(db_session)
    ReviewTaskStore(db_session).apply_intent(
        task.id,
        expected_version=1,
        owner="coordinator",
        changes={"status": str(ParentTaskStatus.REVIEWING)},
        reason="input_valid",
        idempotency_key="intent-recover-0003",
    )
    child_store = A2ATaskStore(db_session)
    child = example_a2a_task(parent_task_id=task.id, agent_id="review-agent", task_type=ChildTaskType.REVIEW)
    row, _ = child_store.submit(child)
    child_store.transition(row.id, ChildTaskStatus.WORKING, expected_version=1, idempotency_key="child-rec-0004")
    child_store.transition(
        row.id,
        ChildTaskStatus.FAILED,
        expected_version=2,
        idempotency_key="child-rec-0005",
        error_code=str(ErrorCode.PROTOCOL_VERSION_UNSUPPORTED),
    )
    plan = build_recovery_plan(db_session, task.id)
    assert plan.next_action == "needs_human"
    assert plan.needs_human is True


def test_recovery_summary_and_unfinished_listing(db_session) -> None:
    create_task(db_session)
    summary = recovery_summary(db_session)
    assert summary["recoverable_tasks"] == 1
    assert summary["planned_actions"]["start_review"] == 1
    plans = list_recovery_plans(db_session)
    assert plans[0].next_action == "start_review"


# ---------------------------------------------------------------------------
# 评测仓储
# ---------------------------------------------------------------------------


def test_eval_store_roundtrip(db_session) -> None:
    store = EvalStore(db_session)
    store.upsert_case(dataset="golden-v1", case_id="PR-001", payload={"input": {}}, tags=["security"])
    run = store.create_run(
        run_id="eval-000000000000000000000001",
        dataset="golden-v1",
        modes=["single", "a2a", "offline"],
        runs_per_case=3,
        idempotency_key="eval-idem-0001",
    )
    store.add_result(
        run_id=run.id,
        case_id="PR-001",
        mode="a2a",
        run_index=1,
        payload={
            "passed": True,
            "finding_recall": 1.0,
            "finding_precision": 0.9,
            "latency_ms": 1200,
            "tokens": 800,
            "security_invariants": {"unauthorized_write": 0},
        },
    )
    store.finish_run(run.id, summary={"pass_rate": 1.0}, report_ref="reports/eval/x.json")
    refreshed = store.get_run(run.id)
    assert refreshed.status == "completed"
    assert refreshed.finished_at is not None
    results = store.list_results(run.id)
    assert len(results) == 1
    assert results[0].passed is True
    assert results[0].security_invariants == {"unauthorized_write": 0}


def test_sanitize_text_removes_secrets_and_paths() -> None:
    raw = "key=sk-live-abcdef123456 path=/home/user/repo/app.py ip=10.0.0.5 mail=a@b.com"
    cleaned = sanitize_text(raw)
    assert "sk-live" not in cleaned
    assert "/home/user" not in cleaned
    assert "10.0.0.5" not in cleaned
    assert "a@b.com" not in cleaned


def test_artifact_type_enum_rows_persist(db_session) -> None:
    """Artifact 表必须能保存六种产物类型。"""
    task, _ = create_task(db_session)
    child_row, _ = A2ATaskStore(db_session).submit(example_a2a_task(parent_task_id=task.id, agent_id="impact-agent"))
    store = ArtifactStore(db_session)
    for artifact_type in ArtifactType:
        envelope = example_envelope(artifact_type, task_id=child_row.id)
        store.save(envelope, parent_task_id=task.id, agent_id="impact-agent", validated=True)
    rows = db_session.execute(select(A2AArtifact)).scalars().all()
    assert {row.artifact_type for row in rows} == {str(item) for item in ArtifactType}


def test_artifact_identifier_helper_is_unique() -> None:
    assert len({new_artifact_id() for _ in range(50)}) == 50
    assert new_trace_id().startswith("trace-")


def test_approval_table_has_no_update_path(db_session) -> None:
    row = Approval.__table__
    assert {"created_at", "decision", "patch_version"} <= set(row.columns.keys())
