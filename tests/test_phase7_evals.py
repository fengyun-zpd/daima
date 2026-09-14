"""阶段七测试：黄金集评测、指标、对照实验与故障注入矩阵。

覆盖 SRS §11.2 指标口径、docs/06 §2 故障矩阵与宪法第八条安全不变量。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from a2a.schema_registry import validate_golden_case
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import ContextPolicy, InputType, RunMode
from evals import EvalRunner, load_cases
from evals.golden_v1 import build_cases, write_dataset
from evals.metrics import PASS_PRECISION_THRESHOLD, PASS_RECALL_THRESHOLD, aggregate, match_findings
from repositories.content_store import ContentStore
from repositories.records import CommentStore, EvalStore

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = CodePilotConfig.model_validate({"mode": "a2a", "a2a": {"child_task_timeout_seconds": 45}})
ADMIN = {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"}
DEVELOPER = {"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"}


@pytest.fixture()
def eval_container(tmp_path, concurrent_database):
    store = ContentStore(tmp_path / "var")
    container = build_container(url=concurrent_database, auto_create=True, config=CONFIG)
    container.content_store = store
    container.coordinator.content_store = store
    container.coordinator.repo_root = tmp_path / "var"
    return container


# ---------------------------------------------------------------------------
# 黄金集本身
# ---------------------------------------------------------------------------


def test_golden_dataset_has_twenty_valid_cases() -> None:
    cases = build_cases()
    assert len(cases) == 20, "SRS §11.1 要求 20 个合成 PR"
    for case in cases:
        validate_golden_case(case)
        assert case["expected_findings"], case["case_id"]


def test_golden_dataset_covers_ten_plus_rule_categories() -> None:
    rules = {item["rule_id"] for case in build_cases() for item in case["expected_findings"]}
    assert len(rules) >= 10
    assert len(rules) == 24, "应覆盖全部 24 条规则"


def test_golden_dataset_is_materialized_and_stable(tmp_path) -> None:
    path = write_dataset(tmp_path / "golden.json")
    first = path.read_text(encoding="utf-8")
    second = write_dataset(tmp_path / "golden.json").read_text(encoding="utf-8")
    assert first == second, "数据集生成必须可重复"
    payload = json.loads(first)
    assert payload["dataset"] == "golden-v1"
    assert len(payload["cases"]) == 20
    assert (REPO_ROOT / "evals" / "golden-v1.json").exists()


def test_expected_findings_match_real_review_output(eval_container) -> None:
    """期望标注必须与规则引擎的真实输出一致（否则评测没有意义）。

    覆盖**全部 20 个用例**：只抽样 4 个用例会让"其余用例的期望与实现漂移"长期隐形
    （历史缺陷：默认配置只启用 3 条规则，全量 recall 掉到 0.20 却无人发现）。
    """
    cases = load_cases()
    mismatches: list[str] = []
    for index, case in enumerate(cases):
        case_id = case["case_id"]
        task, _ = eval_container.coordinator.create_review(
            _review_request(case, idempotency_key=f"golden-{case_id}-{index:02d}")
        )
        eval_container.coordinator.run(task.id)
        with eval_container.session() as session:
            comments = CommentStore(session).list_by_task(task.id)
        found = [
            {"file": row.file, "line": row.line, "rule_id": row.rule_id}
            for row in comments
            if row.confidence_level in {"confirmed", "probable"}
        ]
        _matched, extra, missing = match_findings(case["expected_findings"], found)
        if missing or extra:
            mismatches.append(
                f"{case_id} missing={missing} extra={extra} found={found} expected={case['expected_findings']}"
            )
    assert not mismatches, "黄金集期望与真实输出不一致：\n" + "\n".join(mismatches)


def test_shipped_config_enables_all_rules() -> None:
    """出厂配置必须启用全部规则，否则黄金集与演示都会静默少检。"""
    from domain.config import load_config
    from rules import RULES

    config = load_config(project_path=str(REPO_ROOT / "examples" / ".codepilot.yaml"))
    enabled = config.rules.enabled
    assert enabled == [], "examples/.codepilot.yaml 的 rules.enabled 应为空（= 全部规则）"
    assert len(RULES) == 24
    # 空列表在 Agent 侧等价于"不过滤"，必须与规则总数一致地生效
    assert (enabled or None) is None


def test_fixable_cases_stay_lint_clean_after_patch() -> None:
    """可修复用例在**真实 recipe 改写**之后必须 lint 干净。

    Verify 的 lint 层只检查打过补丁的代码树；如果用例声明了修复后不再使用的 import，
    ruff 会报 F401 并让门禁失败——那是夹具的问题。这里直接跑 FixAgent 的真实 recipe，
    再把结果交给 ruff，避免"夹具与自动修复不匹配"这类问题再次出现。
    """
    import subprocess
    import sys
    import tempfile
    from pathlib import Path as _Path

    from a2a.protocol import FindingItem
    from agents.fix.agent import _plan_by_file
    from domain.config import load_config as _load_config

    fixable = {"PR-001", "PR-002", "PR-003", "PR-019"}
    cases = [case for case in load_cases() if case["case_id"] in fixable]
    assert {case["case_id"] for case in cases} == fixable
    config = _load_config(project_path=str(REPO_ROOT / "examples" / ".codepilot.yaml"))

    with tempfile.TemporaryDirectory() as tmp:
        for case in cases:
            workspace = _workspace_of(case)
            findings = [FindingItem.model_validate(item) for item in _findings_of(case, workspace)]
            plans = _plan_by_file(findings, workspace, placeholder=config.fix.sql_placeholder)
            assert plans, f"{case['case_id']} 应至少有一个可自动修复的计划"
            module = _Path(tmp) / f"{case['case_id'].replace('-', '_')}_main.py"
            module.write_text(next(iter(plans.values())).patched, encoding="utf-8")
            # 沙箱里没有本仓库的 pyproject.toml，ruff 使用默认规则集（E4/E7/E9 + F）；
            # 这里显式对齐该集合，避免用仓库配置（额外启用 I/UP/B/SIM）误判夹具。
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ruff",
                    "check",
                    "--no-cache",
                    "--isolated",
                    "--select",
                    "E4,E7,E9,F",
                    str(module),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, f"{case['case_id']} 修复后仍有 lint 问题：{result.stdout}{result.stderr}"


def _workspace_of(case: dict):
    from domain.workspace import build_workspace

    payload = case["input"]
    workspace, _ = build_workspace(
        input_type=InputType(payload.get("input_type", "zip")),
        content=payload.get("zip_base64") or payload.get("diff", ""),
        base_commit=payload["base_commit"],
        context_policy=ContextPolicy.FUNCTION,
        task_id=f"lint-{case['case_id']}",
    )
    return workspace


def _findings_of(case: dict, workspace) -> list[dict]:
    """用真实规则引擎产出该用例的 Finding（与 Review Agent 同一条链路）。"""
    from rules.base import FileContext
    from rules.engine import scan_files

    contexts = [
        FileContext.build(path, workspace.files.get(path, ""), workspace.changed_line_set(path))
        for path in workspace.changed_files
        if path.endswith(".py")
    ]
    findings, _modes = scan_files(contexts)
    return [
        {
            "rule_id": item.rule.rule_id,
            "title": item.rule.title,
            "cwe": item.rule.cwe,
            "severity": str(item.severity),
            "file": item.file,
            "line": item.hit.line,
            "evidence": item.hit.evidence,
            "message": item.hit.message,
            "confidence_base": item.rule.confidence_base,
            "confidence": item.confidence,
            "confidence_level": str(item.confidence_level),
            "auto_fixable": item.rule.auto_fixable,
            "fix_category": str(item.rule.fix_category) if item.rule.fix_category else None,
            "symbol": item.hit.symbol,
            "rule_version": "1.0",
        }
        for item in findings
        if item.rule.auto_fixable and item.rule.fix_category is not None
    ]


def _review_request(case: dict, *, idempotency_key: str):
    from agents.coordinator.coordinator import ReviewRequest

    payload = case["input"]
    return ReviewRequest(
        input_type=InputType(payload.get("input_type", "diff")),
        content=payload.get("zip_base64") or payload.get("diff", ""),
        base_commit=payload["base_commit"],
        actor_id="eval-test",
        actor_role="developer",
        idempotency_key=idempotency_key,
        context_policy=ContextPolicy.FUNCTION,
        mode=RunMode.A2A,
    )


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------


def test_match_findings_counts_missing_and_extra() -> None:
    expected = [{"file": "a.py", "line": 3, "rule_id": "R1"}]
    found = [
        {"file": "a.py", "line": 3, "rule_id": "R1"},
        {"file": "a.py", "line": 9, "rule_id": "R2"},
    ]
    matched, extra, missing = match_findings(expected, found)
    assert (matched, extra, missing) == (1, 1, 0)


def test_aggregate_computes_pass_at_3_and_security(tmp_path) -> None:
    from evals.metrics import CaseMetrics

    results = [
        CaseMetrics(case_id="PR-001", mode="single", run_index=index, passed=index != 3)
        for index in (1, 2, 3)
    ]
    results += [
        CaseMetrics(case_id="PR-001", mode="a2a", run_index=1, passed=True),
        CaseMetrics(case_id="PR-001", mode="a2a", run_index=2, passed=True),
        CaseMetrics(case_id="PR-001", mode="a2a", run_index=3, passed=True),
    ]
    summary = aggregate(results)
    assert summary["modes"]["single"]["pass_at_3"] == 1.0
    assert summary["modes"]["single"]["pass_pow_3"] == 0.0
    assert summary["modes"]["a2a"]["pass_pow_3"] == 1.0
    assert all(value == 0 for value in summary["security_invariants"].values())


def test_aggregate_flags_latency_regression() -> None:
    from evals.metrics import CaseMetrics

    results = [
        CaseMetrics(case_id="PR-001", mode="single", run_index=1, latency_ms=1000),
        CaseMetrics(case_id="PR-001", mode="a2a", run_index=1, latency_ms=2000),
    ]
    summary = aggregate(results)
    comparison = summary["comparison"]
    assert comparison["latency_regression_exceeds_30pct"] is True
    assert comparison["quality_not_worse"] is True


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------


def test_runner_produces_metrics_and_zero_security_violations(eval_container) -> None:
    runner = EvalRunner(eval_container.coordinator, content_store=eval_container.content_store)
    summary = runner.run(
        modes=[RunMode.SINGLE, RunMode.A2A, RunMode.OFFLINE],
        runs_per_case=1,
        case_limit=3,
    )
    payload = summary.to_payload()
    assert payload["run_id"].startswith("eval-")
    assert len(payload["results"]) == 9
    assert summary.report_ref and eval_container.content_store.exists(summary.report_ref)

    for mode in ("single", "a2a", "offline"):
        metrics = payload["aggregate"]["modes"][mode]
        assert metrics["finding_recall"] >= PASS_RECALL_THRESHOLD, metrics
        assert metrics["finding_precision"] >= PASS_PRECISION_THRESHOLD, metrics
        assert metrics["artifact_schema_pass_rate"] == 1.0
        assert metrics["task_convergence_rate"] == 1.0
        assert metrics["trace_complete_rate"] == 1.0
    assert all(value == 0 for value in payload["aggregate"]["security_invariants"].values())

    with eval_container.session() as session:
        run = EvalStore(session).get_run(payload["run_id"])
        results = EvalStore(session).list_results(payload["run_id"])
    assert run.status == "completed"
    assert len(results) == 9
    assert all(item.trace_complete for item in results)


def test_runner_modes_are_comparable(eval_container) -> None:
    """single 与 a2a 必须使用同一契约：命中集合应一致（FR-099）。"""
    runner = EvalRunner(eval_container.coordinator, content_store=eval_container.content_store)
    summary = runner.run(modes=[RunMode.SINGLE, RunMode.A2A], runs_per_case=1, case_limit=2)
    by_mode: dict[str, list] = {}
    for item in summary.results:
        by_mode.setdefault(item.mode, []).append(item)
    for mode, items in by_mode.items():
        assert all(item.matched == item.expected for item in items), mode


def test_eval_run_is_idempotent_by_run_id(eval_container) -> None:
    runner = EvalRunner(eval_container.coordinator, content_store=eval_container.content_store)
    first = runner.run(modes=[RunMode.OFFLINE], runs_per_case=1, case_limit=1)
    again = runner.run(modes=[RunMode.OFFLINE], runs_per_case=1, case_limit=1, run_id=first.run_id)
    assert again.run_id == first.run_id
    with eval_container.session() as session:
        results = EvalStore(session).list_results(first.run_id)
    assert len(results) == 1, "同一 run_id 的同一用例轮次不得重复入库"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_eval_api_requires_admin(eval_container) -> None:
    app = create_app(container=eval_container, recover_on_start=False, schedule_on_create=False, include_internal=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/evals/run",
            json={"modes": ["offline"], "case_limit": 1, "runs_per_case": 1},
            headers={**DEVELOPER, "Idempotency-Key": "p7-eval-0001"},
        )
        assert response.status_code == 403


def test_eval_api_runs_and_reports(eval_container) -> None:
    app = create_app(container=eval_container, recover_on_start=False, schedule_on_create=False, include_internal=False)
    with TestClient(app) as client:
        started = client.post(
            "/api/v1/evals/run",
            json={"modes": ["offline", "single"], "case_limit": 2, "runs_per_case": 1},
            headers={**ADMIN, "Idempotency-Key": "p7-eval-0002"},
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        deadline = time.time() + 120
        payload: dict = {}
        while time.time() < deadline:
            payload = client.get(f"/api/v1/evals/{run_id}", headers=ADMIN).json()
            if payload["status"] == "completed":
                break
            time.sleep(0.5)
        assert payload["status"] == "completed", payload
        assert payload["summary"]["security_invariants"]["unauthorized_write"] == 0
        assert payload["report"]["aggregate"]["modes"]["single"]["finding_recall"] >= 0.8
        assert len(payload["results"]) == 4


def test_readiness_endpoint_lists_tools_and_agents(eval_container) -> None:
    app = create_app(container=eval_container, recover_on_start=False, schedule_on_create=False, include_internal=False)
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 200
        body = ready.json()
        assert body["status"] == "ready"
        assert "write_patch" in body["tools"]
        assert set(body["agents"]) == {"review-agent", "impact-agent", "fix-agent", "verify-agent"}
