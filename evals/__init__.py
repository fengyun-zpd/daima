"""CodePilot 评测模块：黄金集、指标计算与运行器。"""

from __future__ import annotations

from evals.golden_v1 import build_cases, load_cases, write_dataset
from evals.metrics import CaseMetrics, aggregate, security_invariants
from evals.runner import EvalRunner, EvalSummary

__all__ = [
    "CaseMetrics",
    "EvalRunner",
    "EvalSummary",
    "aggregate",
    "build_cases",
    "load_cases",
    "security_invariants",
    "write_dataset",
]
