"""Agent 公共契约（docs/00 §5 Definition of Ready）。

Agent 只能：读取工作区（经 Tool Registry）、返回版本化 Artifact。
Agent 不能：写父任务、写分支、调用写工具、改变审批或状态机结论。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from a2a.protocol import A2AMessage, A2ATask, ArtifactEnvelope
from domain.budget import Budget
from domain.config import CodePilotConfig
from domain.llm import LLMGateway
from domain.workspace import Workspace
from tools.registry import ToolCallContext, ToolCallResult, ToolRegistry


class ToolGateway:
    """绑定到具体子任务的工具调用入口，自动生成确定性幂等键。"""

    def __init__(
        self,
        registry: ToolRegistry,
        context: ToolCallContext,
    ) -> None:
        self.registry = registry
        self.context = context
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def call(self, tool_name: str, params: dict[str, Any] | None = None) -> ToolCallResult:
        self._counter += 1
        key = f"{self.context.task_id}:{self.context.agent_id}:{tool_name}:{self._counter:03d}"
        result = self.registry.call(self.context, tool_name, params, idempotency_key=key)
        self.calls.append(
            {
                "tool": tool_name,
                "idempotency_key": key,
                "ok": result.ok,
                "replayed": result.replayed,
                "duration_ms": result.duration_ms,
                "result_hash": result.result_hash,
            }
        )
        return result

    def trace(self) -> list[dict[str, Any]]:
        return list(self.calls)


@dataclass(slots=True)
class AgentRequest:
    """子任务输入：任务本身、只读工作区、上游 Artifact 与预算。"""

    task: A2ATask
    workspace: Workspace
    tools: ToolGateway
    budget: Budget
    config: CodePilotConfig
    inputs: list[ArtifactEnvelope] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    llm: LLMGateway | None = None
    sandbox: Any = None
    sandbox_limits: Any = None

    @property
    def task_id(self) -> str:
        return self.task.task_id

    def input_of_type(self, artifact_type: str) -> ArtifactEnvelope | None:
        for envelope in self.inputs:
            if str(envelope.artifact_type) == artifact_type:
                return envelope
        return None


@dataclass(slots=True)
class AgentResult:
    artifacts: list[ArtifactEnvelope] = field(default_factory=list)
    messages: list[A2AMessage] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def artifact_of_type(self, artifact_type: str) -> ArtifactEnvelope | None:
        for envelope in self.artifacts:
            if str(envelope.artifact_type) == artifact_type:
                return envelope
        return None


class AgentHandler(ABC):
    """四类业务 Agent 的统一接口。"""

    agent_id: str = "agent"

    @abstractmethod
    def handle(self, request: AgentRequest) -> AgentResult:
        """处理子任务并返回 Artifact。禁止产生任何直接副作用。"""


__all__ = ["AgentHandler", "AgentRequest", "AgentResult", "ToolGateway"]
