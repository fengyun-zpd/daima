"""生成层：LLM Gateway 与内容守卫（宪法第九条、SRS §5.1 生成层）。

约束：
- LLM 只能生成说明、建议和引用，不能改变规则结论（CWE / 严重级别）；
- LLM 不能生成审批指令、工具执行指令或未经输入提供的外部链接；
- 结构化输出必须通过校验，失败即拒绝或转人工（``MODEL_OUTPUT_INVALID``）；
- Token 消耗计入 Agent 预算，超限即 ``BUDGET_EXCEEDED``。

默认 provider 为 ``offline``：确定性模板生成，不访问网络，便于 single/a2a/offline 对照。
设置 ``CODEPILOT_LLM_PROVIDER=openai`` 且提供 ``CODEPILOT_LLM_BASE_URL`` 时启用
OpenAI 兼容接口（默认 DeepSeek），失败按 docs/01 §6 重试一次后转人工。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from domain.budget import Budget
from domain.errors import CodePilotError, ErrorCode
from domain.sanitize import sanitize_text

DEFAULT_PROVIDER = os.environ.get("CODEPILOT_LLM_PROVIDER", "offline")
DEFAULT_MODEL = os.environ.get("CODEPILOT_LLM_MODEL", "deepseek-chat")

#: 禁止 LLM 输出的指令性内容（不得绕过审批门或工具策略）。
FORBIDDEN_INSTRUCTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("approval_instruction", r"(批准|审批通过|同意合并|approve this|auto[-\s]?merge|直接合并)"),
    ("tool_instruction", r"(write_patch|run_tests|apply_patch|merge_branch|os\.system|执行命令|运行命令)"),
    ("gate_bypass", r"(跳过(审批|测试|门禁)|bypass (approval|gate|test)|忽略质量门禁)"),
)
SEVERITY_OVERRIDE_PATTERN = re.compile(
    r"(severity|严重级别|风险等级)\s*(应|需)?\s*(改为|调整为|设置为|=|:)\s*(critical|high|严重|warning|info)",
    re.IGNORECASE,
)
EXTERNAL_LINK_PATTERN = re.compile(r"https?://[^\s)\]\"'，。；]+")


@dataclass(slots=True)
class GuardResult:
    ok: bool
    violations: list[str] = field(default_factory=list)
    cleaned: str = ""


class ContentGuard:
    """LLM 输出守卫：确定性拒绝越权、指令性内容与外部链接。"""

    def __init__(self, *, allowed_links: tuple[str, ...] = ()) -> None:
        self.allowed_links = tuple(allowed_links)

    def inspect(self, text: str) -> GuardResult:
        violations: list[str] = []
        lowered = text
        for name, pattern in FORBIDDEN_INSTRUCTION_PATTERNS:
            if re.search(pattern, lowered, re.IGNORECASE):
                violations.append(name)
        if SEVERITY_OVERRIDE_PATTERN.search(text):
            violations.append("severity_override")
        for link in EXTERNAL_LINK_PATTERN.findall(text):
            if not any(link.startswith(allowed) for allowed in self.allowed_links):
                violations.append("external_link")
                break
        cleaned = sanitize_text(text, max_length=2000)
        return GuardResult(ok=not violations, violations=sorted(set(violations)), cleaned=cleaned)

    def enforce(self, text: str) -> str:
        result = self.inspect(text)
        if not result.ok:
            raise CodePilotError(
                ErrorCode.MODEL_OUTPUT_INVALID,
                f"LLM 输出被内容守卫拒绝：{result.violations}",
                details={"violations": result.violations},
            )
        return result.cleaned


@dataclass(slots=True)
class LLMRequest:
    purpose: str
    prompt: str
    payload: dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 800


@dataclass(slots=True)
class LLMResponse:
    text: str
    tokens: int
    provider: str
    model: str
    latency_ms: int = 0
    cached: bool = False


class LLMGateway:
    """统一模型入口：provider 选择、超时、Token 预算、结构化校验与内容守卫。"""

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        budget: Budget | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 10.0,
        max_retries: int = 1,
        base_url: str | None = None,
        api_key: str | None = None,
        guard: ContentGuard | None = None,
        allow_network: bool = True,
    ) -> None:
        self.provider = (provider or DEFAULT_PROVIDER).lower()
        self.model = model or DEFAULT_MODEL
        self.budget = budget
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.base_url = base_url or os.environ.get("CODEPILOT_LLM_BASE_URL", "")
        self.api_key = api_key or os.environ.get("CODEPILOT_LLM_API_KEY", "")
        self.guard = guard or ContentGuard()
        self.allow_network = allow_network

    # ---- 状态 -------------------------------------------------------------------
    @property
    def offline(self) -> bool:
        return self.provider in {"offline", "none", "rule-only"}

    @property
    def available(self) -> bool:
        if self.offline:
            return True
        return bool(self.base_url) and self.allow_network

    @property
    def model_info(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "offline": self.offline,
            "available": self.available,
        }

    # ---- 调用 -------------------------------------------------------------------
    def complete(self, request: LLMRequest) -> LLMResponse:
        attempts = 0
        last_error: CodePilotError | None = None
        while attempts <= self.max_retries:
            attempts += 1
            try:
                response = self._dispatch(request)
            except CodePilotError as exc:
                last_error = exc
                if not exc.retryable or attempts > self.max_retries:
                    raise
                continue
            text = self.guard.enforce(response.text)
            response = LLMResponse(
                text=text,
                tokens=response.tokens,
                provider=response.provider,
                model=response.model,
                latency_ms=response.latency_ms,
                cached=response.cached,
            )
            if self.budget is not None:
                self.budget.charge_tokens(response.tokens)
            return response
        raise last_error or CodePilotError(ErrorCode.MODEL_UNAVAILABLE, "模型调用失败")

    def _dispatch(self, request: LLMRequest) -> LLMResponse:
        if self.offline:
            return self._offline(request)
        if not self.available:
            raise CodePilotError(
                ErrorCode.MODEL_UNAVAILABLE,
                "未配置可用的模型端点，降级为确定性结果（docs/04 §4）",
                details=self.model_info,
            )
        return self._openai_compatible(request)

    def _offline(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        text = _offline_text(request)
        return LLMResponse(
            text=text,
            tokens=max(1, len(text) // 4),
            provider="offline",
            model="deterministic-template",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _openai_compatible(self, request: LLMRequest) -> LLMResponse:
        import httpx

        started = time.perf_counter()
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": request.max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是代码审查助手。只能输出中文说明、修复建议和引用，"
                        "不得修改规则给出的 CWE 与严重级别，不得输出审批或工具执行指令，"
                        "不得引入输入之外的外部链接。"
                    ),
                },
                {"role": "user", "content": request.prompt},
            ],
        }
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=self.timeout_seconds)
        except httpx.TimeoutException as exc:
            raise CodePilotError(ErrorCode.MODEL_TIMEOUT, f"模型调用超时：{exc}") from exc
        except httpx.HTTPError as exc:
            raise CodePilotError(ErrorCode.MODEL_UNAVAILABLE, f"模型调用失败：{exc}") from exc

        if response.status_code >= 500:
            raise CodePilotError(ErrorCode.MODEL_UNAVAILABLE, f"模型服务返回 {response.status_code}")
        if response.status_code >= 400:
            raise CodePilotError(ErrorCode.MODEL_OUTPUT_INVALID, f"模型请求被拒绝：{response.text[:200]}")

        payload = response.json()
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CodePilotError(ErrorCode.MODEL_OUTPUT_INVALID, "模型响应缺少 choices[0].message.content") from exc
        usage = payload.get("usage") or {}
        tokens = int(usage.get("total_tokens") or max(1, len(text) // 4))
        return LLMResponse(
            text=text,
            tokens=tokens,
            provider=self.provider,
            model=self.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    # ---- 便捷方法 ---------------------------------------------------------------
    def explain_finding(self, *, finding: dict[str, Any], impact_scope: str = "") -> LLMResponse:
        """把结构化 Finding 归纳为说明与建议（不改变规则结论）。"""
        prompt = (
            "请用中文解释下面的规则命中，并给出可执行的修复建议。\n"
            f"规则：{finding.get('rule_id')}（{finding.get('cwe')}，严重级别 {finding.get('severity')}）\n"
            f"位置：{finding.get('file')}:{finding.get('line')}\n"
            f"证据：{finding.get('evidence')}\n"
            f"影响范围：{impact_scope or '未提供'}\n"
        )
        return self.complete(LLMRequest(purpose="explain_finding", prompt=prompt, payload=finding))


def _offline_text(request: LLMRequest) -> str:
    """确定性模板：与规则结论一致，不引入外部信息。"""
    if request.purpose == "explain_finding":
        payload = request.payload
        severity = payload.get("severity", "warning")
        return (
            f"规则 {payload.get('rule_id')} 在 {payload.get('file')}:{payload.get('line')} 命中"
            f"（严重级别 {severity}，{payload.get('cwe')}）。"
            f"证据：{str(payload.get('evidence', ''))[:160]}。"
            "建议按该规则的修复指引处理，并在修改后补充覆盖该路径的测试。"
        )
    return "已按确定性规则生成结论。"


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "FORBIDDEN_INSTRUCTION_PATTERNS",
    "ContentGuard",
    "GuardResult",
    "LLMGateway",
    "LLMRequest",
    "LLMResponse",
]
