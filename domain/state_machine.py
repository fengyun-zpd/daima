"""父任务与子任务状态机（宪法第四条、docs/02 §2 与 §5）。

设计要点：
1. 迁移表是显式白名单，deny-by-default；表外迁移一律返回 ``ILLEGAL_STATE_TRANSITION``。
2. 终态不可回退；任何跳过 ``PENDING_APPROVAL`` 直达 ``MERGED`` 的迁移都不在表中。
3. ``NEEDS_HUMAN`` 没有自动出边，只能由人工通过受审计的 ``resume`` 操作恢复
   （见 ``can_human_resume``），且永远不能恢复到 ``MERGED``。
4. 子任务 ``failed`` 的 retry 边属于"文档冲突已裁决"项：docs/02 允许 ``failed → working``，
   docs/05 §6 声明 failed 是终态。裁决为"同一 task 记录内允许受控重试一次"，
   必须同时满足 attempt < 2、错误码可重试、且已完成对账（宪法第七条）。
"""

from __future__ import annotations

from domain.enums import (
    CHILD_TERMINAL_STATUSES,
    ChildTaskStatus,
    ParentTaskStatus,
)
from domain.errors import CodePilotError, ErrorCode

PARENT_TRANSITIONS: dict[ParentTaskStatus, frozenset[ParentTaskStatus]] = {
    ParentTaskStatus.DRAFT: frozenset(
        {ParentTaskStatus.REVIEWING, ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.FAILED}
    ),
    ParentTaskStatus.REVIEWING: frozenset(
        {ParentTaskStatus.REVIEWED, ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.FAILED}
    ),
    ParentTaskStatus.REVIEWED: frozenset(
        {
            ParentTaskStatus.FIXING,
            ParentTaskStatus.REJECTED,
            ParentTaskStatus.NEEDS_HUMAN,
            ParentTaskStatus.FAILED,
        }
    ),
    ParentTaskStatus.FIXING: frozenset(
        {ParentTaskStatus.TESTING, ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.FAILED}
    ),
    ParentTaskStatus.TESTING: frozenset(
        {ParentTaskStatus.PENDING_APPROVAL, ParentTaskStatus.NEEDS_HUMAN, ParentTaskStatus.FAILED}
    ),
    ParentTaskStatus.PENDING_APPROVAL: frozenset(
        {ParentTaskStatus.MERGED, ParentTaskStatus.REJECTED, ParentTaskStatus.NEEDS_HUMAN}
    ),
    ParentTaskStatus.NEEDS_HUMAN: frozenset(),
    ParentTaskStatus.MERGED: frozenset(),
    ParentTaskStatus.REJECTED: frozenset(),
    ParentTaskStatus.FAILED: frozenset(),
}

PARENT_TERMINAL_STATUSES: frozenset[ParentTaskStatus] = frozenset(
    {
        ParentTaskStatus.MERGED,
        ParentTaskStatus.REJECTED,
        ParentTaskStatus.FAILED,
        ParentTaskStatus.NEEDS_HUMAN,
    }
)

#: 主路径顺序，用于恢复时判断"是否回退"。
PARENT_MAIN_PATH: tuple[ParentTaskStatus, ...] = (
    ParentTaskStatus.DRAFT,
    ParentTaskStatus.REVIEWING,
    ParentTaskStatus.REVIEWED,
    ParentTaskStatus.FIXING,
    ParentTaskStatus.TESTING,
    ParentTaskStatus.PENDING_APPROVAL,
    ParentTaskStatus.MERGED,
)

CHILD_TRANSITIONS: dict[ChildTaskStatus, frozenset[ChildTaskStatus]] = {
    ChildTaskStatus.SUBMITTED: frozenset(
        {
            ChildTaskStatus.WORKING,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }
    ),
    ChildTaskStatus.WORKING: frozenset(
        {
            ChildTaskStatus.INPUT_REQUIRED,
            ChildTaskStatus.COMPLETED,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }
    ),
    ChildTaskStatus.INPUT_REQUIRED: frozenset(
        {
            ChildTaskStatus.WORKING,
            ChildTaskStatus.FAILED,
            ChildTaskStatus.CANCELED,
        }
    ),
    # failed 的 retry 边：仅允许通过 can_retry_child 守卫后使用。
    ChildTaskStatus.FAILED: frozenset({ChildTaskStatus.WORKING}),
    ChildTaskStatus.COMPLETED: frozenset(),
    ChildTaskStatus.CANCELED: frozenset(),
}

MAX_CHILD_ATTEMPTS = 2
"""docs/05 §6：默认子任务最多重试 1 次；Schema 中 attempt 上限为 2。"""

#: 人工从 NEEDS_HUMAN 恢复时允许进入的状态（永远不含 MERGED）。
HUMAN_RESUME_TARGETS: frozenset[ParentTaskStatus] = frozenset(
    {
        ParentTaskStatus.DRAFT,
        ParentTaskStatus.REVIEWING,
        ParentTaskStatus.REVIEWED,
        ParentTaskStatus.FIXING,
        ParentTaskStatus.TESTING,
        ParentTaskStatus.PENDING_APPROVAL,
        ParentTaskStatus.REJECTED,
        ParentTaskStatus.FAILED,
    }
)


def can_transition_parent(current: ParentTaskStatus, target: ParentTaskStatus) -> bool:
    if current == target:
        # 幂等重放：允许把状态写回自身（例如恢复时重放同一事件）。
        return True
    return target in PARENT_TRANSITIONS[current]


def assert_parent_transition(current: ParentTaskStatus, target: ParentTaskStatus) -> None:
    """校验父任务迁移，非法迁移抛出 ``ILLEGAL_STATE_TRANSITION``。"""
    if current == target:
        # 幂等重放：允许把状态写回自身（例如恢复时重放同一事件），不产生新版本。
        return
    if not can_transition_parent(current, target):
        raise CodePilotError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            f"父任务不允许从 {current} 迁移到 {target}",
            details={
                "current": str(current),
                "target": str(target),
                "allowed": sorted(str(s) for s in PARENT_TRANSITIONS[current]),
            },
        )


def is_parent_terminal(status: ParentTaskStatus) -> bool:
    return status in PARENT_TERMINAL_STATUSES


def can_human_resume(current: ParentTaskStatus, target: ParentTaskStatus) -> bool:
    """人工恢复：仅允许从 NEEDS_HUMAN 出发，且目标不得是 MERGED。"""
    return current is ParentTaskStatus.NEEDS_HUMAN and target in HUMAN_RESUME_TARGETS


def assert_human_resume(current: ParentTaskStatus, target: ParentTaskStatus) -> None:
    if not can_human_resume(current, target):
        raise CodePilotError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            f"人工恢复不允许从 {current} 迁移到 {target}",
            details={"current": str(current), "target": str(target)},
        )


def can_transition_child(current: ChildTaskStatus, target: ChildTaskStatus) -> bool:
    if current == target:
        return True
    return target in CHILD_TRANSITIONS[current]


def assert_child_transition(current: ChildTaskStatus, target: ChildTaskStatus) -> None:
    if current == target:
        return
    if not can_transition_child(current, target):
        raise CodePilotError(
            ErrorCode.ILLEGAL_STATE_TRANSITION,
            f"子任务不允许从 {current} 迁移到 {target}",
            details={
                "current": str(current),
                "target": str(target),
                "allowed": sorted(str(s) for s in CHILD_TRANSITIONS[current]),
            },
        )


def is_child_terminal(status: ChildTaskStatus) -> bool:
    return status in CHILD_TERMINAL_STATUSES


def can_retry_child(
    *,
    status: ChildTaskStatus,
    attempt: int,
    error_code: str | None,
    max_attempts: int = MAX_CHILD_ATTEMPTS,
) -> bool:
    """受控重试守卫（宪法第七条 + SRS FR-095）。

    必须同时满足：任务处于 ``failed``、未超过最大尝试次数、失败错误码被标记为可重试。
    调用方还必须先完成状态对账（查询 Agent 当前状态或命中幂等记录）。
    """
    from domain.errors import ERROR_SPECS

    if status is not ChildTaskStatus.FAILED:
        return False
    if attempt >= max_attempts:
        return False
    if not error_code:
        return False
    try:
        code = ErrorCode(error_code)
    except ValueError:
        return False
    return ERROR_SPECS[code].retryable


__all__ = [
    "CHILD_TRANSITIONS",
    "HUMAN_RESUME_TARGETS",
    "MAX_CHILD_ATTEMPTS",
    "PARENT_MAIN_PATH",
    "PARENT_TERMINAL_STATUSES",
    "PARENT_TRANSITIONS",
    "assert_child_transition",
    "assert_human_resume",
    "assert_parent_transition",
    "can_human_resume",
    "can_retry_child",
    "can_transition_child",
    "can_transition_parent",
    "is_child_terminal",
    "is_parent_terminal",
]
