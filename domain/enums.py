"""CodePilot 运行模式与状态枚举。

单一事实来源：
- 宪法第二条：single / a2a / offline 三种模式必须同时存在。
- 宪法第四条：父任务主路径 DRAFT → REVIEWING → REVIEWED → FIXING → TESTING → PENDING_APPROVAL → MERGED。
- docs/02 §5 与 docs/05 §3：子任务生命周期。
"""

from __future__ import annotations

from enum import StrEnum


class RunMode(StrEnum):
    """运行模式（宪法第二条）。"""

    SINGLE = "single"
    A2A = "a2a"
    OFFLINE = "offline"

    @property
    def uses_llm(self) -> bool:
        """offline 模式只运行确定性规则和 AST，不调用模型（宪法第二条）。"""
        return self is not RunMode.OFFLINE

    @property
    def uses_remote_agents(self) -> bool:
        return self is RunMode.A2A


class ParentTaskStatus(StrEnum):
    """父任务状态（宪法第四条 / SRS §7）。"""

    DRAFT = "DRAFT"
    REVIEWING = "REVIEWING"
    REVIEWED = "REVIEWED"
    FIXING = "FIXING"
    TESTING = "TESTING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    MERGED = "MERGED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class ChildTaskStatus(StrEnum):
    """A2A 子任务状态（docs/02 §5，docs/05 §3）。"""

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


CHILD_TERMINAL_STATUSES: frozenset[ChildTaskStatus] = frozenset(
    {
        ChildTaskStatus.COMPLETED,
        ChildTaskStatus.FAILED,
        ChildTaskStatus.CANCELED,
    }
)


class ChildTaskType(StrEnum):
    """四类业务子任务（docs/02 §0）。"""

    REVIEW = "review"
    IMPACT = "impact"
    FIX = "fix"
    VERIFY = "verify"


class ArtifactType(StrEnum):
    """`schemas/artifact-envelope-v1.schema.json` 允许的 artifact_type。"""

    FINDING = "Finding"
    IMPACT_REPORT = "ImpactReport"
    REVIEW_COMMENT = "ReviewComment"
    PATCH_CANDIDATE = "PatchCandidate"
    PATCH_EVIDENCE = "PatchEvidence"
    VERIFY_EVIDENCE = "VerifyEvidence"


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class ConfidenceLevel(StrEnum):
    """SRS §6.3.1 置信度分级。"""

    CONFIRMED = "confirmed"
    PROBABLE = "probable"
    SUSPICIOUS = "suspicious"
    SUPPRESSED = "suppressed"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ContextPolicy(StrEnum):
    """SRS §4.2 上下文策略。"""

    MINIMAL = "minimal"
    FUNCTION = "function"
    MODULE = "module"


class InputType(StrEnum):
    DIFF = "diff"
    ZIP = "zip"


class ActorRole(StrEnum):
    """SRS §3 角色。coordinator 是内部调用方角色（docs/03 §2）。"""

    DEVELOPER = "developer"
    APPROVER = "approver"
    ADMIN = "admin"
    COORDINATOR = "coordinator"


class AgentStatus(StrEnum):
    """Agent Card 健康状态（schemas/agent-card-v1.schema.json）。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DRAINING = "draining"
    OFFLINE = "offline"


class FixCategory(StrEnum):
    """MVP 只承诺 3 类自动修复（SRS §6.4）。"""

    HARDCODED_SECRET = "hardcoded_secret"
    SHELL_TRUE = "shell_true"
    SQL_PARAMETERIZATION = "sql_parameterization"


class PatchStatus(StrEnum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    VERIFIED = "verified"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class SandboxRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    RESOURCE_EXCEEDED = "resource_exceeded"
    ERROR = "error"


class ToolAccess(StrEnum):
    """工具读写类别，用于角色 allowlist（宪法第六条）。"""

    READ = "read"
    WRITE = "write"


class EvalStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


PROTOCOL_VERSION = "0.1"
"""MVP 唯一支持的 A2A 协议版本（schemas/a2a-task-v1.schema.json）。"""

ARTIFACT_SCHEMA_VERSION = "1.0"
"""MVP 唯一支持的 ArtifactEnvelope schema_version。"""
