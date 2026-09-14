"""SQLAlchemy 2.0 数据模型（SRS §9、docs/03 §4）。

约束落地要点：
1. ``review_task.state_version`` / ``a2a_task.state_version``：乐观锁（FR-016）；
2. ``(parent_task_id, agent_id, task_type, idempotency_key)`` 唯一：子任务幂等（FR-096）；
3. ``(task_id, file, line, rule_id)`` 唯一：意见去重（FR-024）；
4. ``audit_event`` / ``approval``：只允许 INSERT（FR-073，见 repositories/audit.py + 迁移触发器）；
5. 状态列带 CHECK 约束：非法状态无法落库（宪法第八条：非法状态迁移必须为 0）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from domain.clock import utcnow
from domain.enums import (
    CHILD_TERMINAL_STATUSES,
    ApprovalDecision,
    ChildTaskStatus,
    ParentTaskStatus,
    PatchStatus,
    SandboxRunStatus,
)

JSONType = JSON().with_variant(JSONB, "postgresql")

PARENT_STATUS_VALUES = tuple(str(status) for status in ParentTaskStatus)
CHILD_STATUS_VALUES = tuple(str(status) for status in ChildTaskStatus)
PATCH_STATUS_VALUES = tuple(str(status) for status in PatchStatus)
SANDBOX_STATUS_VALUES = tuple(str(status) for status in SandboxRunStatus)
APPROVAL_DECISION_VALUES = tuple(str(decision) for decision in ApprovalDecision)
CHILD_TERMINAL_VALUES = tuple(str(status) for status in CHILD_TERMINAL_STATUSES)


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[str]: JSONType}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class UserAccount(Base, TimestampMixin):
    """工作台账户。密码只保存 PBKDF2 派生值，不保存明文。"""

    __tablename__ = "user_account"
    __table_args__ = (
        UniqueConstraint("employee_id", name="uq_user_account_employee_id"),
        UniqueConstraint("username", name="uq_user_account_username"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="developer")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("user_account.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# 父任务
# ---------------------------------------------------------------------------


class ReviewTask(Base, TimestampMixin):
    __tablename__ = "review_task"
    __table_args__ = (
        CheckConstraint(_sql_in("status", PARENT_STATUS_VALUES), name="ck_review_task_status"),
        CheckConstraint("state_version >= 1", name="ck_review_task_state_version"),
        # 幂等：同一 actor 的同一幂等键只允许创建一次（FR-002）。
        # 说明：docs/03 §4 曾要求 input_hash 全局唯一，但那会与"同一输入多次评测"
        # （pass@3，NFR-005/§11.3）冲突；宪法优先级更高，故唯一性落在
        # (actor_id, command_type, idempotency_key)，input_hash 只建索引。
        UniqueConstraint("actor_id", "command_type", "idempotency_key", name="uq_review_task_idempotency"),
        Index("ix_review_task_input_hash", "input_hash"),
        Index("ix_review_task_parent_state", "status", "state_version"),
        UniqueConstraint("actor_id", "custom_task_id", name="uq_review_task_actor_custom_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    custom_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    command_type: Mapped[str] = mapped_column(String(32), nullable=False, default="create_review")
    mode: Mapped[str] = mapped_column(String(16), nullable=False)

    input_type: Mapped[str] = mapped_column(String(16), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    input_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    context_policy: Mapped[str] = mapped_column(String(16), nullable=False)

    status: Mapped[str] = mapped_column(String(24), nullable=False, default=str(ParentTaskStatus.DRAFT))
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="coordinator")
    current_step: Mapped[str | None] = mapped_column(String(64), nullable=True)
    needs_human_from: Mapped[str | None] = mapped_column(String(24), nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    budget_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    completed: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    pending: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    child_tasks: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    artifacts: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)

    fixed_comment_ids: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    active_patch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# 意见与补丁
# ---------------------------------------------------------------------------


class ReviewComment(Base, TimestampMixin):
    __tablename__ = "review_comment"
    __table_args__ = (
        UniqueConstraint("task_id", "file", "line", "rule_id", name="uq_review_comment_dedup"),
        CheckConstraint(_sql_in("severity", ("critical", "warning", "info")), name="ck_review_comment_severity"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_review_comment_confidence"),
        Index("ix_review_comment_task", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("review_task.id"), nullable=False)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finding_id: Mapped[str] = mapped_column(String(256), nullable=False)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    cwe: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    file: Mapped[str] = mapped_column(String(512), nullable=False)
    line: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(16), nullable=False)
    auto_fixable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fix_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impact_scope: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    call_graph_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    citations: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class FixPatch(Base, TimestampMixin):
    __tablename__ = "fix_patch"
    __table_args__ = (
        UniqueConstraint("task_id", "patch_version", name="uq_fix_patch_version"),
        UniqueConstraint("task_id", "idempotency_key", name="uq_fix_patch_idempotency"),
        CheckConstraint(_sql_in("status", PATCH_STATUS_VALUES), name="ck_fix_patch_status"),
        CheckConstraint("patch_version >= 1", name="ck_fix_patch_version_positive"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("review_task.id"), nullable=False, index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    base_commit: Mapped[str] = mapped_column(String(128), nullable=False)
    target_branch: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default=str(PatchStatus.CANDIDATE))
    fix_category: Mapped[str] = mapped_column(String(32), nullable=False)
    finding_refs: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    changed_files: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    changed_functions: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    diff_text: Mapped[str] = mapped_column(Text, nullable=False)
    added_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    removed_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    scope_drift: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scope_drift_files: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    wide_impact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    coverage_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    test_gap: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sandbox_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SandboxRun(Base, TimestampMixin):
    __tablename__ = "sandbox_run"
    __table_args__ = (
        CheckConstraint(_sql_in("status", SANDBOX_STATUS_VALUES), name="ck_sandbox_run_status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("review_task.id"), nullable=True)
    patch_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("fix_patch.id"), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    image: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    degraded_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cpu_limit: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    memory_limit_mb: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    disk_limit_mb: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    network: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    coverage_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
    resource_usage: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    stdout_sanitized: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stderr_sanitized: Mapped[str] = mapped_column(Text, default="", nullable=False)
    test_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class Approval(Base):
    """审批记录：只允许 INSERT（FR-073）。"""

    __tablename__ = "approval"
    __table_args__ = (
        CheckConstraint(_sql_in("decision", APPROVAL_DECISION_VALUES), name="ck_approval_decision"),
        UniqueConstraint("patch_id", "patch_version", name="uq_approval_patch_version"),
        Index("ix_approval_task", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("review_task.id"), nullable=False)
    patch_id: Mapped[str] = mapped_column(String(64), ForeignKey("fix_patch.id"), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_role: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    patch_version: Mapped[int] = mapped_column(Integer, nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# A2A
# ---------------------------------------------------------------------------


class A2AAgent(Base, TimestampMixin):
    __tablename__ = "a2a_agent"
    __table_args__ = (
        UniqueConstraint("agent_id", "card_version", name="uq_a2a_agent_card_version"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    card_version: Mapped[str] = mapped_column(String(16), nullable=False)
    protocol_versions: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    input_schema: Mapped[str] = mapped_column(String(128), nullable=False)
    output_schemas: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    auth_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="healthy")
    card_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    card_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class A2ATask(Base, TimestampMixin):
    __tablename__ = "a2a_task"
    __table_args__ = (
        CheckConstraint(_sql_in("status", CHILD_STATUS_VALUES), name="ck_a2a_task_status"),
        CheckConstraint(_sql_in("side", ("coordinator", "agent")), name="ck_a2a_task_side"),
        # FR-096：父子任务幂等唯一约束。
        # 说明：MVP 允许 Coordinator 与 Agent 服务共享同一个 PostgreSQL（docs/01 §5），
        # 两侧各自拥有同一子任务的生命周期记录，因此唯一键必须包含 side，
        # 否则远端任务行会与协调方跟踪行冲突。
        UniqueConstraint(
            "side",
            "parent_task_id",
            "agent_id",
            "task_type",
            "idempotency_key",
            name="uq_a2a_task_idempotency",
        ),
        CheckConstraint("attempt >= 1 AND attempt <= 2", name="ck_a2a_task_attempt"),
        CheckConstraint("state_version >= 1", name="ck_a2a_task_state_version"),
        Index("ix_a2a_task_parent", "parent_task_id"),
        Index("ix_a2a_task_status_deadline", "status", "deadline"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("review_task.id"), nullable=False
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(
        String(16), nullable=False, default="coordinator", server_default="coordinator"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="submitted")
    protocol_version: Mapped[str] = mapped_column(String(8), nullable=False, default="0.1")
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    input_artifacts: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    required_output_types: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    transport: Mapped[str] = mapped_column(String(16), nullable=False, default="inprocess")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    remote_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class A2AMessage(Base):
    __tablename__ = "a2a_message"
    __table_args__ = (Index("ix_a2a_message_task", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("a2a_task.id"), nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_refs: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class A2AArtifact(Base):
    __tablename__ = "a2a_artifact"
    __table_args__ = (
        UniqueConstraint("task_id", "artifact_type", "content_hash", name="uq_a2a_artifact_content"),
        Index("ix_a2a_artifact_task", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), ForeignKey("a2a_task.id"), nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    data_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    validated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# 审计与工具执行
# ---------------------------------------------------------------------------


class AuditEvent(Base):
    """追加式审计（FR-073、NFR-007）。禁止 UPDATE/DELETE。"""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_event_trace", "trace_id", "created_at"),
        Index("ix_audit_event_task", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    state_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_state: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    after_state: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class ToolExecution(Base):
    """工具调用审计（FR-015 / FR-017）：只保存参数哈希与结果哈希。"""

    __tablename__ = "tool_execution"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "agent_id", "tool_name", "idempotency_key", name="uq_tool_execution_idempotency"
        ),
        Index("ix_tool_execution_task", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    access: Mapped[str] = mapped_column(String(8), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    denied_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    result_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    result_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------


class EvalCase(Base):
    __tablename__ = "eval_case"
    __table_args__ = (UniqueConstraint("dataset", "case_id", name="uq_eval_case"),)

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class EvalRun(Base):
    __tablename__ = "eval_run"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    modes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    runs_per_case: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    report_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalResult(Base):
    __tablename__ = "eval_result"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", "mode", "run_index", name="uq_eval_result"),
        Index("ix_eval_result_run", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("eval_run.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    run_index: Mapped[int] = mapped_column(Integer, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finding_recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    finding_precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    patch_valid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    verify_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    route_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    artifact_schema_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    task_converged: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    trace_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recovery_stable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    injected_fault: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovery_action: Mapped[str | None] = mapped_column(String(128), nullable=True)
    security_invariants: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    trace: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class IdempotencyRecord(Base, TimestampMixin):
    """命令级幂等台账（docs/03 §6：``(actor_id, command_type, aggregate_ref, idempotency_key)`` 唯一）。

    用途：同一幂等键重复提交必须返回**原结果**（补丁、审批、合并、评测、子任务），
    而不是依赖"当前状态已变化"来拒绝；同一键携带不同请求内容必须返回
    ``IDEMPOTENCY_CONFLICT``。响应体持久化后即使进程重启也能重放原结果（宪法第七条）。
    """

    __tablename__ = "idempotency_record"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "command_type",
            "aggregate_ref",
            "idempotency_key",
            name="uq_idempotency_record_key",
        ),
        CheckConstraint(
            _sql_in("status", ("in_progress", "completed", "failed")),
            name="ck_idempotency_record_status",
        ),
        Index("ix_idempotency_record_aggregate", "command_type", "aggregate_ref"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    command_type: Mapped[str] = mapped_column(String(48), nullable=False)
    aggregate_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    request_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_progress")
    response: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


ALL_TABLES = (
    UserAccount.__tablename__,
    AuthSession.__tablename__,
    ReviewTask.__tablename__,
    ReviewComment.__tablename__,
    FixPatch.__tablename__,
    SandboxRun.__tablename__,
    Approval.__tablename__,
    A2AAgent.__tablename__,
    A2ATask.__tablename__,
    A2AMessage.__tablename__,
    A2AArtifact.__tablename__,
    AuditEvent.__tablename__,
    ToolExecution.__tablename__,
    IdempotencyRecord.__tablename__,
    EvalCase.__tablename__,
    EvalRun.__tablename__,
    EvalResult.__tablename__,
)

APPEND_ONLY_TABLES = (AuditEvent.__tablename__, Approval.__tablename__)


__all__ = [
    "ALL_TABLES",
    "APPEND_ONLY_TABLES",
    "A2AAgent",
    "A2AArtifact",
    "A2AMessage",
    "A2ATask",
    "AuthSession",
    "Approval",
    "AuditEvent",
    "Base",
    "EvalCase",
    "EvalResult",
    "EvalRun",
    "FixPatch",
    "IdempotencyRecord",
    "JSONType",
    "ReviewComment",
    "ReviewTask",
    "SandboxRun",
    "TimestampMixin",
    "ToolExecution",
    "UserAccount",
]
