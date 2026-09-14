"""CodePilot 业务 Agent 实现。

``default_handlers()`` 是四类 Agent 的**唯一注册来源**：单进程 Coordinator 与拆分形态的
Agent 服务都必须用它，避免"某一形态漏注册某一类 Agent"（曾导致拆分形态下 Fix/Verify
子任务报 `AGENT_UNAVAILABLE: Agent handler 未注册`）。
"""

from __future__ import annotations

from agents.base import AgentHandler, AgentRequest, AgentResult, ToolGateway


def default_handlers() -> dict[str, AgentHandler]:
    """构造四类业务 Agent 的 handler 映射（按 agent_id 索引）。"""
    from agents.fix import FixAgent
    from agents.impact import ImpactAgent
    from agents.review import ReviewAgent
    from agents.verify import VerifyAgent

    return {
        "review-agent": ReviewAgent(),
        "impact-agent": ImpactAgent(),
        "fix-agent": FixAgent(),
        "verify-agent": VerifyAgent(),
    }


__all__ = [
    "AgentHandler",
    "AgentRequest",
    "AgentResult",
    "ToolGateway",
    "default_handlers",
]
