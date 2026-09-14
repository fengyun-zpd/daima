"""CodePilot A2A 协议模型（宪法第五条；docs/05；schemas/*.json）。

本模块是协议层唯一事实来源：Agent Card、Task、Message、ArtifactEnvelope 全部为
冻结的 Pydantic 模型，``extra="forbid"``，字段与 ``schemas/`` 下 JSON Schema 一一映射
（``tests/test_schema_conformance.py`` 强制校验字段集合与 required 集合完全一致）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.enums import (
    ARTIFACT_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    AgentStatus,
    ArtifactType,
    ChildTaskStatus,
    ChildTaskType,
    ConfidenceLevel,
    FixCategory,
    RiskLevel,
    Severity,
)
from domain.errors import CodePilotError, ErrorCode

MAX_ARTIFACT_BYTES = 10_485_760


class ProtocolModel(BaseModel):
    """所有协议模型的基类：禁止额外字段，保证跨 Agent 传输可控。

    注意：**不能**开启 ``str_strip_whitespace``。``PatchCandidate.diff`` 与
    ``VerifyEvidence.sanitized_output`` 是逐字节敏感的文本，去掉结尾换行会让
    ``git apply`` 报 "corrupt patch"。标识符类字段改用 pattern / min_length 约束校验。
    """

    model_config = ConfigDict(extra="forbid", frozen=False, str_strip_whitespace=False)


# --------------------------------------------------------------------------------------
# Agent Card
# --------------------------------------------------------------------------------------


class AgentAuth(ProtocolModel):
    audience: str
    scopes: list[str] = Field(min_length=1)


class AgentLimits(ProtocolModel):
    max_steps: int = Field(ge=1, le=100)
    timeout_seconds: int = Field(ge=1, le=300)


class AgentCard(ProtocolModel):
    """版本化 Agent Card（FR-091；schemas/agent-card-v1.schema.json）。"""

    agent_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,63}$")
    card_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    protocol_versions: list[Literal["0.1"]] = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)
    input_schema: str
    output_schemas: list[str] = Field(min_length=1)
    endpoint: str = Field(pattern=r"^/internal/a2a/agents/")
    auth: AgentAuth
    limits: AgentLimits
    status: AgentStatus

    @field_validator("protocol_versions")
    @classmethod
    def _unique_protocols(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("protocol_versions 不允许重复")
        return value

    @field_validator("capabilities")
    @classmethod
    def _valid_capabilities(cls, value: list[str]) -> list[str]:
        import re

        pattern = re.compile(r"^[a-z]+\.[a-z0-9_.-]+$")
        for item in value:
            if not pattern.match(item):
                raise ValueError(f"非法能力名：{item}")
        if len(set(value)) != len(value):
            raise ValueError("capabilities 不允许重复")
        return value

    @field_validator("output_schemas")
    @classmethod
    def _unique_schemas(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("output_schemas 不允许重复")
        return value

    def supports_protocol(self, version: str = PROTOCOL_VERSION) -> bool:
        return version in self.protocol_versions

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


# --------------------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------------------


class A2ATask(ProtocolModel):
    """A2A 子任务（FR-092；docs/05 §3；schemas/a2a-task-v1.schema.json）。"""

    task_id: str = Field(min_length=8)
    parent_task_id: str | None = None
    trace_id: str = Field(min_length=8)
    agent_id: str
    task_type: ChildTaskType
    protocol_version: Literal["0.1"] = PROTOCOL_VERSION
    status: ChildTaskStatus = ChildTaskStatus.SUBMITTED
    input_artifacts: list[str] = Field(default_factory=list)
    required_output_types: list[str] = Field(min_length=1)
    deadline: datetime
    attempt: int = Field(default=1, ge=1, le=2)
    idempotency_key: str = Field(min_length=8)
    correlation_id: str = Field(min_length=8)
    state_version: int = Field(default=1, ge=1)

    @field_validator("deadline")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline 必须带时区（UTC）")
        return value.astimezone(UTC)

    @property
    def is_terminal(self) -> bool:
        from domain.state_machine import is_child_terminal

        return is_child_terminal(self.status)

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        return moment >= self.deadline


def make_deadline(timeout_seconds: int, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment + timedelta(seconds=timeout_seconds)


# --------------------------------------------------------------------------------------
# Message
# --------------------------------------------------------------------------------------


class A2AError(ProtocolModel):
    code: str
    message: str


class A2AMessage(ProtocolModel):
    """控制信息与引用（docs/05 §4）。"""

    message_id: str = Field(min_length=8)
    task_id: str = Field(min_length=8)
    type: str
    role: Literal["coordinator", "agent", "system"]
    correlation_id: str = Field(min_length=8)
    artifact_refs: list[str] = Field(default_factory=list)
    error: A2AError | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at 必须带时区（UTC）")
        return value.astimezone(UTC)


# --------------------------------------------------------------------------------------
# Artifact
# --------------------------------------------------------------------------------------


def canonical_json(data: Any) -> str:
    """确定性 JSON 序列化，用于内容哈希（NFR-005 可重复性）。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_content_hash(data: Any) -> str:
    """docs/05 §4：``sha256:<hex>``。"""
    digest = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def compute_text_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class ArtifactEnvelope(ProtocolModel):
    """跨 Agent 产物信封（FR-093；schemas/artifact-envelope-v1.schema.json）。"""

    artifact_id: str = Field(min_length=8)
    task_id: str = Field(min_length=8)
    artifact_type: ArtifactType
    schema_version: Literal["1.0"] = ARTIFACT_SCHEMA_VERSION
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    data: dict[str, Any]

    @classmethod
    def build(cls, *, artifact_id: str, task_id: str, artifact_type: ArtifactType, data: dict[str, Any]) -> ArtifactEnvelope:
        payload = json.loads(canonical_json(data))
        size = len(canonical_json(payload).encode("utf-8"))
        if size > MAX_ARTIFACT_BYTES:
            raise CodePilotError(
                ErrorCode.ARTIFACT_SCHEMA_INVALID,
                f"Artifact 超过最大尺寸 {MAX_ARTIFACT_BYTES} 字节",
                details={"size_bytes": size},
            )
        return cls(
            artifact_id=artifact_id,
            task_id=task_id,
            artifact_type=artifact_type,
            schema_version=ARTIFACT_SCHEMA_VERSION,
            content_hash=compute_content_hash(payload),
            size_bytes=size,
            data=payload,
        )

    def verify_hash(self) -> None:
        expected = compute_content_hash(self.data)
        if expected != self.content_hash:
            raise CodePilotError(
                ErrorCode.ARTIFACT_HASH_MISMATCH,
                "Artifact content_hash 校验失败",
                details={
                    "artifact_id": self.artifact_id,
                    "expected": expected,
                    "actual": self.content_hash,
                },
            )


class StateIntent(ProtocolModel):
    """状态变更意图（SRS §5.4、docs/02 §4）：Agent 只能提交意图，由编排器校验后落库。"""

    expected_version: int = Field(ge=1)
    owner: str
    changes: dict[str, Any]
    reason: str
    idempotency_key: str = Field(min_length=8)


class TaskHandle(ProtocolModel):
    """``AgentInvoker.submit`` 的返回句柄（docs/05 §8）。"""

    task_id: str
    agent_id: str
    status: ChildTaskStatus
    submitted_at: datetime


# --------------------------------------------------------------------------------------
# Artifact 负载契约
# --------------------------------------------------------------------------------------


class FindingItem(ProtocolModel):
    """单条确定性规则命中（SRS §6.3.1）。"""

    rule_id: str
    title: str
    cwe: str
    severity: Severity
    file: str
    line: int = Field(ge=1)
    evidence: str
    message: str
    confidence_base: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: ConfidenceLevel
    auto_fixable: bool = False
    fix_category: FixCategory | None = None
    in_changed_lines: bool = False
    context_confirmed: bool = False
    symbol: str | None = None
    rule_version: str


class FindingStats(ProtocolModel):
    total: int = Field(ge=0)
    by_severity: dict[str, int] = Field(default_factory=dict)
    by_confidence_level: dict[str, int] = Field(default_factory=dict)
    suppressed: int = Field(default=0, ge=0)


class FindingArtifact(ProtocolModel):
    """Review Agent 的 ``Finding`` 产物：一个包含多条 FindingItem 的集合。"""

    rule_set_version: str
    scan_mode: Literal["ast", "text", "mixed"]
    degraded_reason: str | None = None
    files_scanned: list[str] = Field(default_factory=list)
    findings: list[FindingItem] = Field(default_factory=list)
    stats: FindingStats


class ChangedSymbol(ProtocolModel):
    name: str
    kind: Literal["function", "method", "class", "module"]
    file: str
    line: int = Field(ge=1)
    changed_lines: list[int] = Field(default_factory=list)


class CallEdge(ProtocolModel):
    caller: str
    callee: str
    file: str
    line: int = Field(ge=1)


class ImpactStats(ProtocolModel):
    changed_symbol_count: int = Field(ge=0)
    affected_file_count: int = Field(ge=0)
    caller_count: int = Field(ge=0)
    callee_count: int = Field(ge=0)


class ImpactReport(ProtocolModel):
    """Impact Agent 产物（SRS §5.4、§6.3）。"""

    changed_symbols: list[str] = Field(default_factory=list)
    direct_callers: list[str] = Field(default_factory=list)
    direct_callees: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    risk_level: RiskLevel
    uncertain: bool
    dynamic_calls: list[str] = Field(default_factory=list)
    symbol_details: list[ChangedSymbol] = Field(default_factory=list)
    call_graph: list[CallEdge] = Field(default_factory=list)
    stats: ImpactStats
    analyzers: list[str] = Field(default_factory=list)


class ReviewComment(ProtocolModel):
    finding_ref: str
    message: str
    suggestion: str
    confidence_level: ConfidenceLevel
    impact_scope: str
    citations: list[str] = Field(default_factory=list)
    generated_by: Literal["rules", "llm", "rules+llm"]


class PatchCandidate(ProtocolModel):
    """Fix Agent 产物：只返回候选补丁，不写分支（宪法第六条）。"""

    patch_version: int = Field(ge=1)
    patch_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    base_commit: str
    target_branch: str
    fix_category: FixCategory
    finding_refs: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(min_length=1)
    changed_functions: list[str] = Field(default_factory=list)
    added_lines: int = Field(ge=0)
    removed_lines: int = Field(ge=0)
    diff: str
    idempotency_key: str = Field(min_length=8)

    @field_validator("target_branch")
    @classmethod
    def _never_protected(cls, value: str) -> str:
        if value in {"main", "master", "develop"}:
            raise ValueError("补丁目标分支不得是受保护分支（FR-041）")
        return value


class PatchEvidence(ProtocolModel):
    """补丁范围与覆盖证据（SRS §5.4）。"""

    patch_version: int = Field(ge=1)
    patch_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    changed_files: list[str] = Field(default_factory=list)
    changed_functions: list[str] = Field(default_factory=list)
    scope_drift: bool
    scope_drift_files: list[str] = Field(default_factory=list)
    wide_impact: bool
    coverage_before: float | None = None
    coverage_after: float | None = None
    coverage_delta: float | None = None
    test_gap: bool
    sandbox_run_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ApplyCheck(ProtocolModel):
    ok: bool
    command: str
    message: str


class PreScanResult(ProtocolModel):
    ok: bool
    blocked_patterns: list[str] = Field(default_factory=list)


class LintResult(ProtocolModel):
    ok: bool
    tool: str
    exit_code: int
    issue_count: int = Field(ge=0)
    summary: str


class TestResult(ProtocolModel):
    ok: bool
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    errors: int = Field(ge=0)
    total: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)


class RegressionResult(ProtocolModel):
    ok: bool
    baseline_failed_total: int = Field(ge=0)
    baseline_failed_fixed: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)


class CoverageResult(ProtocolModel):
    before: float | None = None
    after: float | None = None
    delta: float | None = None
    source: Literal["coverage.py", "unavailable"]


class TierResult(ProtocolModel):
    tier: Literal["prescan", "apply_check", "lint", "unit_test", "regression", "coverage"]
    ok: bool
    detail: str


class VerifyEvidence(ProtocolModel):
    """Verify Agent 产物（SRS §6.5、§4.4 质量门禁）。"""

    patch_version: int = Field(ge=1)
    patch_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sandbox_run_id: str
    sandbox_degraded: bool
    degraded_reason: str | None = None
    exit_code: int
    apply_check: ApplyCheck
    pre_scan: PreScanResult
    lint: LintResult
    unit_tests: TestResult
    regression: RegressionResult
    coverage: CoverageResult
    test_gap: bool
    scope_drift: bool
    evidence_sufficient: bool
    tiers: list[TierResult] = Field(default_factory=list)
    sanitized_output: str


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "A2AError",
    "A2AMessage",
    "A2ATask",
    "AgentAuth",
    "AgentCard",
    "AgentLimits",
    "ApplyCheck",
    "ArtifactEnvelope",
    "CallEdge",
    "ChangedSymbol",
    "CoverageResult",
    "FindingArtifact",
    "FindingItem",
    "FindingStats",
    "ImpactReport",
    "ImpactStats",
    "LintResult",
    "PatchCandidate",
    "PatchEvidence",
    "PreScanResult",
    "ProtocolModel",
    "RegressionResult",
    "ReviewComment",
    "StateIntent",
    "TaskHandle",
    "TestResult",
    "TierResult",
    "VerifyEvidence",
    "canonical_json",
    "compute_content_hash",
    "compute_text_hash",
    "make_deadline",
]
