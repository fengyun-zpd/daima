"""Coordinator 重启恢复（FR-097、NFR-011、docs/01 §6）。

恢复原则：查询优先、幂等重试、终态保护。
- 已完成子任务绝不重放（NFR-011 要求 100%）；
- 未知状态先对账（查询子任务与 Artifact），再决定是否重试一次；
- 无法收敛则转 ``NEEDS_HUMAN``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from domain.enums import ArtifactType, ChildTaskStatus, ParentTaskStatus
from domain.state_machine import can_retry_child
from repositories.models import A2AArtifact, ReviewTask
from repositories.models import A2ATask as A2ATaskRow

REVIEW_REQUIRED_ARTIFACTS: tuple[str, ...] = (
    str(ArtifactType.FINDING),
    str(ArtifactType.IMPACT_REPORT),
)

TASK_TYPE_REQUIRED: dict[str, str] = {
    "review": str(ArtifactType.FINDING),
    "impact": str(ArtifactType.IMPACT_REPORT),
    "fix": str(ArtifactType.PATCH_CANDIDATE),
    "verify": str(ArtifactType.VERIFY_EVIDENCE),
}

#: 阶段三的必需子任务流水线（Fix / Verify 在阶段六加入）。
PIPELINE_TASK_TYPES: tuple[str, ...] = ("review", "impact")


@dataclass(slots=True)
class RecoveryPlan:
    task_id: str
    trace_id: str
    status: str
    completed_children: list[str] = field(default_factory=list)
    pending_children: list[str] = field(default_factory=list)
    retryable_children: list[str] = field(default_factory=list)
    failed_children: list[str] = field(default_factory=list)
    missing_children: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)
    next_action: str = "noop"
    needs_human: bool = False
    reason: str = ""

    @property
    def will_replay_completed(self) -> bool:
        return False  # 结构性保证：计划中不存在已完成子任务

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "status": self.status,
            "completed_children": list(self.completed_children),
            "pending_children": list(self.pending_children),
            "retryable_children": list(self.retryable_children),
            "failed_children": list(self.failed_children),
            "missing_children": list(self.missing_children),
            "missing_artifacts": list(self.missing_artifacts),
            "next_action": self.next_action,
            "needs_human": self.needs_human,
            "reason": self.reason,
        }


def build_recovery_plan(session: Session, task_id: str) -> RecoveryPlan:
    """为单个父任务生成恢复计划。"""
    task = session.get(ReviewTask, task_id)
    if task is None:
        raise LookupError(f"任务不存在：{task_id}")

    children = list(
        session.execute(
            select(A2ATaskRow)
            .where(A2ATaskRow.parent_task_id == task_id, A2ATaskRow.side == "coordinator")
            .order_by(A2ATaskRow.created_at)
        ).scalars()
    )
    artifacts = list(
        session.execute(select(A2AArtifact).where(A2AArtifact.parent_task_id == task_id)).scalars()
    )
    validated_types = {
        row.artifact_type
        for row in artifacts
        if row.validated and row.validation_error is None
    }

    plan = RecoveryPlan(task_id=task.id, trace_id=task.trace_id, status=task.status)

    for child in children:
        row_status = ChildTaskStatus(child.status)
        required = TASK_TYPE_REQUIRED.get(child.task_type)
        if row_status is ChildTaskStatus.COMPLETED:
            if required and required not in validated_types:
                # completed 但没有合法 Artifact —— 不允许当成完成（FR-094）
                plan.pending_children.append(child.id)
                plan.missing_artifacts.append(required)
                plan.reason = f"子任务 {child.id} 标记完成但缺少合法 {required}"
            else:
                plan.completed_children.append(child.id)
        elif row_status in {ChildTaskStatus.SUBMITTED, ChildTaskStatus.WORKING, ChildTaskStatus.INPUT_REQUIRED}:
            plan.pending_children.append(child.id)
        elif row_status is ChildTaskStatus.FAILED:
            if can_retry_child(
                status=row_status, attempt=child.attempt, error_code=child.error_code,
                max_attempts=child.max_attempts,
            ):
                plan.retryable_children.append(child.id)
            else:
                plan.failed_children.append(child.id)
        elif row_status is ChildTaskStatus.CANCELED:
            plan.failed_children.append(child.id)

    missing = [item for item in REVIEW_REQUIRED_ARTIFACTS if item not in validated_types]
    present_types = {child.task_type for child in children}
    plan.missing_children = [task_type for task_type in PIPELINE_TASK_TYPES if task_type not in present_types]

    status = ParentTaskStatus(task.status)
    if status is ParentTaskStatus.DRAFT:
        plan.next_action = "start_review"
        plan.reason = plan.reason or "父任务尚未开始审查"
    elif status is ParentTaskStatus.REVIEWING:
        plan.missing_artifacts = list(missing)
        if plan.failed_children and not plan.pending_children and not plan.retryable_children:
            # 存在不可重试的硬失败：继续创建其它子任务也无法收敛。
            plan.next_action = "needs_human"
            plan.needs_human = True
            plan.reason = plan.reason or f"存在不可重试的失败子任务：{plan.failed_children}"
        elif plan.missing_children and not plan.completed_children:
            plan.next_action = "create_children"
            plan.reason = plan.reason or f"缺少子任务：{plan.missing_children}"
        elif not missing:
            plan.next_action = "finalize_review"
            plan.reason = plan.reason or "必需 Artifact 齐备，可推进到 REVIEWED"
        elif plan.pending_children or plan.missing_children:
            plan.next_action = "await_children"
            plan.reason = plan.reason or f"仍有未完成的子任务：{len(plan.pending_children) + len(plan.missing_children)}"
        elif plan.retryable_children:
            plan.next_action = "retry_children"
            plan.reason = plan.reason or f"子任务可受控重试：{len(plan.retryable_children)}"
        else:
            plan.next_action = "needs_human"
            plan.needs_human = True
            plan.reason = plan.reason or f"缺少必需 Artifact 且无可重试子任务：{missing}"
    else:
        plan.next_action = "noop"
        plan.reason = plan.reason or f"状态 {status} 的恢复由后续阶段处理"

    return plan


def list_recovery_plans(session: Session, *, limit: int = 200) -> list[RecoveryPlan]:
    terminal = {
        str(ParentTaskStatus.MERGED),
        str(ParentTaskStatus.REJECTED),
        str(ParentTaskStatus.FAILED),
        str(ParentTaskStatus.NEEDS_HUMAN),
    }
    rows = list(
        session.execute(
            select(ReviewTask)
            .where(ReviewTask.status.notin_(terminal))
            .order_by(ReviewTask.created_at)
            .limit(limit)
        ).scalars()
    )
    return [build_recovery_plan(session, row.id) for row in rows]


def recovery_summary(session: Session) -> dict[str, Any]:
    """恢复成功率的原始计数（NFR-011 报告口径）。"""
    plans = list_recovery_plans(session)
    replayed = sum(len(plan.completed_children) for plan in plans)
    plan_rows = session.execute(select(ReviewTask.status, func.count()).group_by(ReviewTask.status))
    status_counts: dict[str, int] = {}
    for status, count in plan_rows:
        status_counts[status] = count
    return {
        "recoverable_tasks": len(plans),
        "planned_actions": {
            action: sum(1 for plan in plans if plan.next_action == action)
            for action in sorted({plan.next_action for plan in plans})
        },
        "completed_children_available_for_reuse": replayed,
        "status_counts": status_counts,
        "needs_human": sum(1 for plan in plans if plan.needs_human),
    }


__all__ = [
    "PIPELINE_TASK_TYPES",
    "REVIEW_REQUIRED_ARTIFACTS",
    "TASK_TYPE_REQUIRED",
    "RecoveryPlan",
    "build_recovery_plan",
    "list_recovery_plans",
    "recovery_summary",
]
