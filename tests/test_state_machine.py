"""父/子状态机与非法迁移测试（宪法第四条、docs/02 §2、docs/04 §3）。"""

from __future__ import annotations

import pytest

from domain.enums import ChildTaskStatus, ParentTaskStatus
from domain.errors import CodePilotError, ErrorCode
from domain.state_machine import (
    HUMAN_RESUME_TARGETS,
    PARENT_MAIN_PATH,
    PARENT_TERMINAL_STATUSES,
    assert_child_transition,
    assert_human_resume,
    assert_parent_transition,
    can_human_resume,
    can_retry_child,
    can_transition_child,
    can_transition_parent,
    is_child_terminal,
    is_parent_terminal,
)


def test_main_path_is_legal() -> None:
    for current, target in zip(PARENT_MAIN_PATH, PARENT_MAIN_PATH[1:], strict=False):
        assert can_transition_parent(current, target), f"{current} → {target} 应合法"
        assert_parent_transition(current, target)


def test_pending_approval_requires_approval_to_merge() -> None:
    assert can_transition_parent(ParentTaskStatus.TESTING, ParentTaskStatus.PENDING_APPROVAL)
    assert not can_transition_parent(ParentTaskStatus.TESTING, ParentTaskStatus.MERGED)
    assert not can_transition_parent(ParentTaskStatus.REVIEWED, ParentTaskStatus.MERGED)
    assert not can_transition_parent(ParentTaskStatus.FIXING, ParentTaskStatus.MERGED)
    assert not can_transition_parent(ParentTaskStatus.DRAFT, ParentTaskStatus.MERGED)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ParentTaskStatus.DRAFT, ParentTaskStatus.REVIEWED),
        (ParentTaskStatus.REVIEWING, ParentTaskStatus.FIXING),
        (ParentTaskStatus.FIXING, ParentTaskStatus.REVIEWED),
        (ParentTaskStatus.PENDING_APPROVAL, ParentTaskStatus.FIXING),
        (ParentTaskStatus.MERGED, ParentTaskStatus.REVIEWING),
        (ParentTaskStatus.REJECTED, ParentTaskStatus.FIXING),
        (ParentTaskStatus.FAILED, ParentTaskStatus.REVIEWING),
        (ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.REVIEWED),
    ],
)
def test_illegal_parent_transitions_rejected(current, target) -> None:
    with pytest.raises(CodePilotError) as excinfo:
        assert_parent_transition(current, target)
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    assert excinfo.value.details["current"] == str(current)


def test_terminal_states_have_no_outgoing_edges() -> None:
    for status in PARENT_TERMINAL_STATUSES:
        assert is_parent_terminal(status)
        for target in ParentTaskStatus:
            if target is status:
                continue
            assert not can_transition_parent(status, target), f"{status} → {target} 必须被拒绝"


def test_same_state_write_is_idempotent() -> None:
    # 恢复时可能重放同一状态事件，不视为非法迁移。
    assert_parent_transition(ParentTaskStatus.REVIEWING, ParentTaskStatus.REVIEWING)


def test_self_transition_is_allowed_only_for_same_state() -> None:
    assert can_transition_child(ChildTaskStatus.WORKING, ChildTaskStatus.WORKING)
    assert_child_transition(ChildTaskStatus.WORKING, ChildTaskStatus.WORKING)


def test_human_resume_never_reaches_merged() -> None:
    assert ParentTaskStatus.MERGED not in HUMAN_RESUME_TARGETS
    assert not can_human_resume(ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.MERGED)
    with pytest.raises(CodePilotError):
        assert_human_resume(ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.MERGED)


def test_human_resume_requires_needs_human_state() -> None:
    assert not can_human_resume(ParentTaskStatus.REVIEWING, ParentTaskStatus.REVIEWED)
    assert can_human_resume(ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.TESTING)


def test_child_lifecycle() -> None:
    assert can_transition_child(ChildTaskStatus.SUBMITTED, ChildTaskStatus.WORKING)
    assert can_transition_child(ChildTaskStatus.WORKING, ChildTaskStatus.INPUT_REQUIRED)
    assert can_transition_child(ChildTaskStatus.INPUT_REQUIRED, ChildTaskStatus.WORKING)
    assert can_transition_child(ChildTaskStatus.WORKING, ChildTaskStatus.COMPLETED)
    assert not can_transition_child(ChildTaskStatus.COMPLETED, ChildTaskStatus.WORKING)
    assert is_child_terminal(ChildTaskStatus.COMPLETED)
    assert not is_child_terminal(ChildTaskStatus.WORKING)


def test_child_illegal_transitions() -> None:
    with pytest.raises(CodePilotError) as excinfo:
        assert_child_transition(ChildTaskStatus.SUBMITTED, ChildTaskStatus.COMPLETED)
    assert excinfo.value.code is ErrorCode.ILLEGAL_STATE_TRANSITION
    with pytest.raises(CodePilotError):
        assert_child_transition(ChildTaskStatus.CANCELED, ChildTaskStatus.WORKING)


def test_retry_guard_requires_retryable_error_and_remaining_attempts() -> None:
    assert can_retry_child(
        status=ChildTaskStatus.FAILED, attempt=1, error_code=str(ErrorCode.TASK_TIMEOUT)
    )
    # 已用尽重试次数（Schema attempt 上限 2）
    assert not can_retry_child(
        status=ChildTaskStatus.FAILED, attempt=2, error_code=str(ErrorCode.TASK_TIMEOUT)
    )
    # 不可重试错误码
    assert not can_retry_child(
        status=ChildTaskStatus.FAILED,
        attempt=1,
        error_code=str(ErrorCode.ARTIFACT_SCHEMA_INVALID),
    )
    assert not can_retry_child(
        status=ChildTaskStatus.FAILED,
        attempt=1,
        error_code=str(ErrorCode.PROTOCOL_VERSION_UNSUPPORTED),
    )
    # 只有 failed 状态允许重试
    assert not can_retry_child(
        status=ChildTaskStatus.WORKING, attempt=1, error_code=str(ErrorCode.TASK_TIMEOUT)
    )
    assert not can_retry_child(status=ChildTaskStatus.FAILED, attempt=1, error_code=None)
    assert not can_retry_child(
        status=ChildTaskStatus.FAILED, attempt=1, error_code="NOT_A_REAL_CODE"
    )
