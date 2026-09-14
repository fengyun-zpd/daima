"""Agent 预算与循环检测（FR-018、SRS §5.3、NFR-008）。

确定性边界：Token、工具调用、步数和重复调用阈值都在这里判定，
超出即抛错并转 ``NEEDS_HUMAN``，不受 LLM 影响。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from domain.errors import CodePilotError, ErrorCode


@dataclass(slots=True)
class Budget:
    """单个 Agent 调用的资源预算。"""

    tokens_limit: int = 12_000
    tool_calls_limit: int = 30
    max_steps: int = 15
    loop_detection_threshold: int = 3

    tokens_used: int = 0
    tool_calls_used: int = 0
    steps: int = 0
    signatures: Counter[str] = field(default_factory=Counter)

    # ---- 计费 -------------------------------------------------------------------
    def step(self) -> None:
        self.steps += 1
        if self.steps > self.max_steps:
            raise CodePilotError(
                ErrorCode.STEP_LIMIT_EXCEEDED,
                f"Agent 超过最大步数 {self.max_steps}",
                details={"max_steps": self.max_steps, "steps": self.steps},
            )

    def charge_tool(self, signature: str) -> None:
        self.tool_calls_used += 1
        if self.tool_calls_used > self.tool_calls_limit:
            raise CodePilotError(
                ErrorCode.BUDGET_EXCEEDED,
                f"工具调用超过预算 {self.tool_calls_limit}",
                details={"limit": self.tool_calls_limit, "used": self.tool_calls_used},
            )
        self.signatures[signature] += 1
        if self.signatures[signature] >= self.loop_detection_threshold:
            raise CodePilotError(
                ErrorCode.TOOL_LOOP_DETECTED,
                f"同一工具与参数连续调用达到 {self.loop_detection_threshold} 次，判定为循环",
                details={
                    "signature": signature,
                    "count": self.signatures[signature],
                    "threshold": self.loop_detection_threshold,
                },
            )

    def charge_tokens(self, tokens: int) -> None:
        self.tokens_used += max(0, tokens)
        if self.tokens_used > self.tokens_limit:
            raise CodePilotError(
                ErrorCode.BUDGET_EXCEEDED,
                f"Token 使用超过预算 {self.tokens_limit}",
                details={"limit": self.tokens_limit, "used": self.tokens_used},
            )

    # ---- 视图 -------------------------------------------------------------------
    @property
    def exhausted(self) -> bool:
        return (
            self.tool_calls_used >= self.tool_calls_limit
            or self.tokens_used >= self.tokens_limit
            or self.steps >= self.max_steps
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "tokens_limit": self.tokens_limit,
            "tokens_used": self.tokens_used,
            "tool_calls_limit": self.tool_calls_limit,
            "tool_calls_used": self.tool_calls_used,
            "max_steps": self.max_steps,
            "steps": self.steps,
            "loop_detection_threshold": self.loop_detection_threshold,
        }

    @classmethod
    def from_config(cls, agent_section: Any) -> Budget:
        return cls(
            tokens_limit=int(getattr(agent_section.budget, "tokens", 12_000)),
            tool_calls_limit=int(getattr(agent_section.budget, "tool_calls", 30)),
            max_steps=int(getattr(agent_section, "max_steps", 15)),
            loop_detection_threshold=int(getattr(agent_section, "loop_detection_threshold", 3)),
        )


__all__ = ["Budget"]
