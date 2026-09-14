"""统一错误码与异常（宪法第七条、SRS §7、docs/03 §5、docs/05 §7）。

每个错误码都显式声明：
- ``http_status``：对外 API 返回的 HTTP 状态；
- ``retryable``：是否允许"先对账、再重试一次"（宪法第七条，未知状态禁止盲目重放）；
- ``human_action``：是否必须转人工（NEEDS_HUMAN）；
- ``default_parent_status``：Coordinator 捕获该错误时父任务应进入的状态。

docs/00 §3 要求"每个错误都有 HTTP 状态和是否重试"，本文件是该冻结项的落地实现。
新增错误码必须在 ``docs/08-错误码与协议冻结.md`` 中登记。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from domain.enums import ParentTaskStatus


class ErrorCode(StrEnum):
    # --- 输入与校验 ---
    INVALID_INPUT = "INVALID_INPUT"
    PATCH_INVALID = "PATCH_INVALID"
    ARTIFACT_SCHEMA_INVALID = "ARTIFACT_SCHEMA_INVALID"
    ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"

    # --- 权限与越权 ---
    PERMISSION_DENIED = "PERMISSION_DENIED"
    FORBIDDEN = "FORBIDDEN"

    # --- Agent 预算与循环 ---
    STEP_LIMIT_EXCEEDED = "STEP_LIMIT_EXCEEDED"
    TOOL_LOOP_DETECTED = "TOOL_LOOP_DETECTED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

    # --- 变更范围与质量门禁 ---
    CONFLICT = "CONFLICT"
    SCOPE_DRIFT = "SCOPE_DRIFT"
    WIDE_IMPACT = "WIDE_IMPACT"
    TEST_GAP = "TEST_GAP"
    QUALITY_GATE_FAILED = "QUALITY_GATE_FAILED"

    # --- 沙箱 ---
    SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"

    # --- 状态与并发 ---
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"
    STATE_VERSION_CONFLICT = "STATE_VERSION_CONFLICT"
    VERSION_CONFLICT = "VERSION_CONFLICT"

    # --- A2A 协议 ---
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    TASK_STATUS_UNKNOWN = "TASK_STATUS_UNKNOWN"
    TASK_CANCELED = "TASK_CANCELED"
    PARENT_TASK_MISMATCH = "PARENT_TASK_MISMATCH"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    AGENT_CARD_INVALID = "AGENT_CARD_INVALID"
    CAPABILITY_NOT_AVAILABLE = "CAPABILITY_NOT_AVAILABLE"

    # --- 幂等 ---
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"

    # --- 模型 ---
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"

    # --- 资源 ---
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http_status: int
    retryable: bool
    human_action: bool
    default_parent_status: ParentTaskStatus | None
    description: str


_NEEDS_HUMAN = ParentTaskStatus.NEEDS_HUMAN
_FAILED = ParentTaskStatus.FAILED
_REJECTED = ParentTaskStatus.REJECTED

ERROR_SPECS: dict[ErrorCode, ErrorSpec] = {
    # 输入类：不重试，直接 FAILED
    ErrorCode.INVALID_INPUT: ErrorSpec(400, False, False, _FAILED, "输入格式、路径或内容校验失败"),
    ErrorCode.PATCH_INVALID: ErrorSpec(422, False, True, _NEEDS_HUMAN, "补丁无法被 git apply 解析或格式非法"),
    ErrorCode.ARTIFACT_SCHEMA_INVALID: ErrorSpec(422, False, True, _NEEDS_HUMAN, "Artifact 未通过类型/版本/字段校验"),
    ErrorCode.ARTIFACT_HASH_MISMATCH: ErrorSpec(422, False, True, _NEEDS_HUMAN, "Artifact content_hash 与内容不一致"),
    ErrorCode.MODEL_OUTPUT_INVALID: ErrorSpec(422, False, True, _NEEDS_HUMAN, "LLM 结构化输出未通过 Schema 校验"),
    # 权限类：永不重试
    ErrorCode.PERMISSION_DENIED: ErrorSpec(403, False, True, _NEEDS_HUMAN, "角色或 Agent 能力 allowlist 拒绝该调用"),
    ErrorCode.FORBIDDEN: ErrorSpec(403, False, False, None, "角色无权执行该操作（例如写入 main/develop）"),
    # 预算与循环
    ErrorCode.STEP_LIMIT_EXCEEDED: ErrorSpec(409, False, True, _NEEDS_HUMAN, "Agent 超过单次循环最大步数"),
    ErrorCode.TOOL_LOOP_DETECTED: ErrorSpec(409, False, True, _NEEDS_HUMAN, "同工具同参数连续调用超过阈值"),
    ErrorCode.BUDGET_EXCEEDED: ErrorSpec(409, False, True, _NEEDS_HUMAN, "Token 或工具调用预算超限"),
    # 质量门禁
    ErrorCode.CONFLICT: ErrorSpec(409, False, True, _NEEDS_HUMAN, "同文件重叠修改冲突"),
    ErrorCode.SCOPE_DRIFT: ErrorSpec(422, False, True, _NEEDS_HUMAN, "补丁超出审查意见声明的文件和逻辑范围"),
    ErrorCode.WIDE_IMPACT: ErrorSpec(409, False, False, None, "影响文件数超过阈值，需要额外 approver 确认"),
    ErrorCode.TEST_GAP: ErrorSpec(422, False, True, _NEEDS_HUMAN, "被修复代码路径未被测试覆盖"),
    ErrorCode.QUALITY_GATE_FAILED: ErrorSpec(409, False, True, _NEEDS_HUMAN, "质量门禁失败"),
    # 沙箱
    ErrorCode.SANDBOX_TIMEOUT: ErrorSpec(504, True, True, _NEEDS_HUMAN, "沙箱执行超时，已销毁容器并清理临时目录"),
    ErrorCode.SANDBOX_VIOLATION: ErrorSpec(403, False, True, _NEEDS_HUMAN, "沙箱安全策略被触发（网络/提权/只读根）"),
    ErrorCode.SANDBOX_UNAVAILABLE: ErrorSpec(503, False, True, _NEEDS_HUMAN, "Docker 不可用，禁止在宿主机执行提交代码"),
    # 状态
    ErrorCode.ILLEGAL_STATE_TRANSITION: ErrorSpec(409, False, True, _NEEDS_HUMAN, "非法状态迁移，已拒绝"),
    ErrorCode.STATE_VERSION_CONFLICT: ErrorSpec(409, True, False, None, "乐观锁版本不匹配，需重新读取后再提交"),
    ErrorCode.VERSION_CONFLICT: ErrorSpec(409, False, True, _NEEDS_HUMAN, "审批版本与当前 patch_version 不一致"),
    # A2A
    ErrorCode.PROTOCOL_VERSION_UNSUPPORTED: ErrorSpec(400, False, True, _NEEDS_HUMAN, "协议版本不兼容"),
    ErrorCode.TASK_TIMEOUT: ErrorSpec(504, True, True, _NEEDS_HUMAN, "子任务超过 deadline"),
    ErrorCode.TASK_STATUS_UNKNOWN: ErrorSpec(409, False, True, _NEEDS_HUMAN, "无法确认子任务当前状态，禁止盲目重放"),
    ErrorCode.TASK_CANCELED: ErrorSpec(409, False, True, _NEEDS_HUMAN, "子任务已被取消"),
    ErrorCode.PARENT_TASK_MISMATCH: ErrorSpec(409, False, True, _NEEDS_HUMAN, "Artifact/Task 的父任务关联不一致"),
    ErrorCode.AGENT_UNAVAILABLE: ErrorSpec(503, True, True, _NEEDS_HUMAN, "目标 Agent 不可用或健康检查失败"),
    ErrorCode.AGENT_CARD_INVALID: ErrorSpec(422, False, False, None, "Agent Card 未通过 Schema 或能力校验"),
    ErrorCode.CAPABILITY_NOT_AVAILABLE: ErrorSpec(409, False, True, _NEEDS_HUMAN, "Agent Card 未声明所需能力"),
    # 幂等
    ErrorCode.IDEMPOTENCY_CONFLICT: ErrorSpec(409, False, False, None, "相同幂等键对应不同请求内容"),
    ErrorCode.IDEMPOTENCY_KEY_REQUIRED: ErrorSpec(400, False, False, None, "写请求缺少 Idempotency-Key"),
    # 模型
    ErrorCode.MODEL_TIMEOUT: ErrorSpec(504, True, True, _NEEDS_HUMAN, "模型调用超时"),
    ErrorCode.MODEL_UNAVAILABLE: ErrorSpec(503, True, True, _NEEDS_HUMAN, "模型服务不可用，降级为确定性结果"),
    # 资源
    ErrorCode.RESOURCE_NOT_FOUND: ErrorSpec(404, False, False, None, "资源不存在"),
    ErrorCode.INTERNAL_ERROR: ErrorSpec(500, False, True, _NEEDS_HUMAN, "未预期内部错误"),
}

#: 不能被任何 Agent 或重试逻辑绕过的不可重试错误（宪法第六条、第七条）。
NON_RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    code for code, spec in ERROR_SPECS.items() if not spec.retryable
)


class CodePilotError(Exception):
    """统一业务异常。所有对外错误响应都由该异常生成。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str | None = None,
        *,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.spec = ERROR_SPECS[code]
        self.message = message or self.spec.description
        self.trace_id = trace_id
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(f"{self.code}: {self.message}")

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    @property
    def human_action(self) -> bool:
        return self.spec.human_action

    @property
    def default_parent_status(self) -> ParentTaskStatus | None:
        return self.spec.default_parent_status

    def to_payload(self) -> dict[str, Any]:
        """docs/03 §1 规定的错误响应格式。"""
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "trace_id": self.trace_id,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def error_payload(
    code: ErrorCode,
    message: str | None = None,
    *,
    trace_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return CodePilotError(code, message, trace_id=trace_id, details=details).to_payload()


__all__ = [
    "ERROR_SPECS",
    "NON_RETRYABLE_CODES",
    "CodePilotError",
    "ErrorCode",
    "ErrorSpec",
    "error_payload",
]
