"""CodePilot domain layer: enums, error codes, state machines and contracts.

宪法第四条/第七条：任务与状态、错误语义、幂等与恢复的确定性边界都在本层实现，
LLM 不得改变本层结论（宪法第九条）。
"""

from __future__ import annotations

__all__ = [
    "confidence",
    "config",
    "enums",
    "errors",
    "ids",
    "state_machine",
]
