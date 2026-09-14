"""阶段七测试：故障注入矩阵（11 个非沙箱场景，沙箱场景由 scripts/sandbox_check.py 覆盖）。"""

from __future__ import annotations

import pytest

from evals.fault_matrix import SCENARIOS, run_matrix

EXPECTED_SCENARIOS = {
    "重复提交",
    "子任务超时",
    "SSE 断线",
    "Coordinator 重启",
    "非法 Artifact",
    "Artifact 哈希错误",
    "Agent Card 能力缺失",
    "协议版本不兼容",
    "Review 越权调用写工具",
    "未审批合并",
    "非法状态迁移",
    "沙箱外网访问",
    "沙箱提权",
}


def test_matrix_covers_all_required_scenarios() -> None:
    names = {name for name, _scenario, _needs_sandbox in SCENARIOS}
    assert names == EXPECTED_SCENARIOS, "必须覆盖 SRS §11.4 与 docs/06 §2 的全部场景"


def test_non_sandbox_scenarios_all_pass(tmp_path, concurrent_database) -> None:
    outcomes = run_matrix(
        tmp_path=tmp_path, database_url=concurrent_database, include_sandbox=False
    )
    assert len(outcomes) == 11
    failures = [item for item in outcomes if not item.passed]
    assert not failures, [(item.name, item.detail) for item in failures]


@pytest.mark.parametrize(
    "scenario",
    [
        "协议版本不兼容",
        "未审批合并",
        "非法状态迁移",
    ],
)
def test_individual_security_scenarios(tmp_path, concurrent_database, scenario: str) -> None:
    outcomes = run_matrix(
        tmp_path=tmp_path,
        database_url=concurrent_database,
        include_sandbox=False,
        only=[scenario],
    )
    assert len(outcomes) == 1
    assert outcomes[0].passed, outcomes[0].detail
    assert outcomes[0].observed_code is not None
