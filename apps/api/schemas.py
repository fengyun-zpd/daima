"""API 请求与响应模型（docs/03 §3）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import (
    ApprovalDecision,
    ContextPolicy,
    InputType,
    RunMode,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateReviewRequest(StrictModel):
    input_type: InputType = InputType.DIFF
    content: str = Field(min_length=1, description="unified diff 文本，或 base64 编码的 ZIP")
    context_policy: ContextPolicy = ContextPolicy.FUNCTION
    base_commit: str = Field(min_length=1, max_length=128)
    mode: RunMode | None = Field(default=None, description="覆盖服务默认运行模式")
    custom_task_id: str | None = Field(default=None, max_length=128, description="用户可读的任务编号")


class RegisterRequest(StrictModel):
    employee_id: str = Field(min_length=2, max_length=64)
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(StrictModel):
    account: str = Field(min_length=2, max_length=64, description="用户名或工号")
    password: str = Field(min_length=1, max_length=128)


class UserResponse(StrictModel):
    id: str
    employee_id: str
    username: str
    role: str


class AuthResponse(StrictModel):
    token: str
    expires_at: datetime
    user: UserResponse


class ApprovalRequest(StrictModel):
    decision: ApprovalDecision
    patch_version: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class CreateFixRequest(StrictModel):
    comment_ids: list[str] = Field(default_factory=list, description="要修复的意见；留空表示全部可修复意见")


class MergeRequest(StrictModel):
    patch_version: int = Field(ge=1)


class ResumeRequest(StrictModel):
    target_status: str
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class CreateA2ATaskRequest(StrictModel):
    parent_task_id: str = Field(min_length=8)
    agent_id: str = Field(min_length=3)
    task_type: str
    protocol_version: str = "0.1"
    input_artifacts: list[str] = Field(default_factory=list)
    deadline_seconds: int = Field(default=45, ge=1, le=300)
    idempotency_key: str = Field(min_length=8)


class EvalRunRequest(StrictModel):
    dataset: str = "golden-v1"
    modes: list[RunMode] = Field(default_factory=lambda: [RunMode.SINGLE, RunMode.A2A, RunMode.OFFLINE])
    runs_per_case: int | None = Field(default=None, ge=1, le=5)
    case_limit: int | None = Field(default=None, ge=1, le=50)
    with_fix: bool = Field(default=False, description="是否执行 Fix/Verify（需要 Docker 沙箱）")


class ErrorResponse(StrictModel):
    code: str
    message: str
    trace_id: str | None = None
    details: dict[str, Any] | None = None


class ChildTaskResponse(StrictModel):
    id: str
    agent_id: str
    task_type: str
    status: str
    attempt: int
    max_attempts: int
    deadline: datetime
    state_version: int
    correlation_id: str
    transport: str
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ArtifactSummary(StrictModel):
    id: str
    task_id: str
    artifact_type: str
    schema_version: str
    content_hash: str
    size_bytes: int
    validated: bool
    validation_error: str | None = None
    created_at: datetime


class CommentResponse(StrictModel):
    id: str
    file: str
    line: int
    rule_id: str
    cwe: str
    severity: str
    confidence: float
    confidence_level: str
    message: str
    evidence: str
    suggestion: str
    auto_fixable: bool
    fix_category: str | None = None
    impact_scope: str
    citations: list[str] = Field(default_factory=list)


class ReviewTaskResponse(StrictModel):
    id: str
    trace_id: str
    mode: str
    status: str
    state_version: int
    owner: str
    actor_id: str
    custom_task_id: str | None = None
    input_type: str
    base_commit: str
    context_policy: str
    input_summary: dict[str, Any]
    summary: dict[str, Any]
    current_step: str | None = None
    needs_human_from: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class ReviewDetailResponse(StrictModel):
    task: ReviewTaskResponse
    child_tasks: list[ChildTaskResponse] = Field(default_factory=list)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    comments: list[CommentResponse] = Field(default_factory=list)
    audit_tail: list[dict[str, Any]] = Field(default_factory=list)


class CreatedReviewResponse(StrictModel):
    task: ReviewTaskResponse
    created: bool
    detail_url: str


class PatchResponse(StrictModel):
    id: str
    task_id: str
    patch_version: int
    patch_hash: str
    status: str
    fix_category: str
    target_branch: str
    changed_files: list[str]
    changed_functions: list[str]
    scope_drift: bool
    scope_drift_files: list[str]
    wide_impact: bool
    coverage_before: float | None = None
    coverage_after: float | None = None
    coverage_delta: float | None = None
    test_gap: bool
    sandbox_run_id: str | None = None
    diff: str
    approvals: list[dict[str, Any]] = Field(default_factory=list)


class AgentCardResponse(StrictModel):
    agent_id: str
    card_version: str
    protocol_versions: list[str]
    capabilities: list[str]
    input_schema: str
    output_schemas: list[str]
    endpoint: str
    limits: dict[str, Any]
    status: str
    registered_at: datetime | None = None


class AuditEventResponse(StrictModel):
    id: str
    trace_id: str | None = None
    task_id: str | None = None
    parent_task_id: str | None = None
    agent_id: str | None = None
    actor_id: str
    actor_role: str
    action: str
    entity_type: str
    entity_id: str
    event_type: str
    state_version: int | None = None
    before_state: dict[str, Any] = Field(default_factory=dict)
    after_state: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    duration_ms: int | None = None
    created_at: datetime


class EvalRunResponse(StrictModel):
    run_id: str
    status: str
    dataset: str
    modes: list[str]
    summary: dict[str, Any] = Field(default_factory=dict)
    report_ref: str | None = None


__all__ = [
    "AgentCardResponse",
    "AuthResponse",
    "ApprovalRequest",
    "ArtifactSummary",
    "AuditEventResponse",
    "ChildTaskResponse",
    "CommentResponse",
    "CreateA2ATaskRequest",
    "CreateFixRequest",
    "CreateReviewRequest",
    "LoginRequest",
    "RegisterRequest",
    "CreatedReviewResponse",
    "ErrorResponse",
    "EvalRunRequest",
    "EvalRunResponse",
    "MergeRequest",
    "PatchResponse",
    "ResumeRequest",
    "ReviewDetailResponse",
    "ReviewTaskResponse",
    "UserResponse",
    "StrictModel",
]
