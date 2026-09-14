"""协议与产物的规范示例（最小演示数据）。

用途：
1. 作为 JSON Schema 一致性测试的黄金样例（tests/test_schema_conformance.py）；
2. 作为 API 文档示例与演示数据来源（阶段八）；
3. 作为 Agent Definition of Ready 要求的"成功样例 / 非法输入样例"底座（docs/00 §5）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from a2a.protocol import (
    A2AError,
    A2AMessage,
    A2ATask,
    AgentAuth,
    AgentCard,
    AgentLimits,
    ApplyCheck,
    ArtifactEnvelope,
    CallEdge,
    ChangedSymbol,
    CoverageResult,
    FindingArtifact,
    FindingItem,
    FindingStats,
    ImpactReport,
    ImpactStats,
    LintResult,
    PatchCandidate,
    PatchEvidence,
    PreScanResult,
    RegressionResult,
    ReviewComment,
    StateIntent,
    TestResult,
    TierResult,
    VerifyEvidence,
    compute_text_hash,
    make_deadline,
)
from domain.enums import (
    AgentStatus,
    ArtifactType,
    ChildTaskStatus,
    ChildTaskType,
    ConfidenceLevel,
    ContextPolicy,
    FixCategory,
    InputType,
    RiskLevel,
    Severity,
)
from domain.ids import new_artifact_id, new_message_id, new_task_id

FIXED_NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def example_agent_card(agent_id: str = "review-agent") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        card_version="1.0",
        protocol_versions=["0.1"],
        capabilities=["review.rules", "review.explain"],
        input_schema="ReviewTaskInput@1.0",
        output_schemas=["FindingArtifact@1.0", "ReviewCommentArtifact@1.0"],
        endpoint=f"/internal/a2a/agents/{agent_id}/tasks",
        auth=AgentAuth(audience="codepilot-coordinator", scopes=["task:receive"]),
        limits=AgentLimits(max_steps=15, timeout_seconds=45),
        status=AgentStatus.HEALTHY,
    )


def example_a2a_task(
    *,
    task_id: str | None = None,
    parent_task_id: str | None = "review-00000000000000000000000001",
    agent_id: str = "impact-agent",
    task_type: ChildTaskType = ChildTaskType.IMPACT,
    required_output_types: list[str] | None = None,
) -> A2ATask:
    identifier = task_id or new_task_id()
    return A2ATask(
        task_id=identifier,
        parent_task_id=parent_task_id,
        trace_id="trace-00000000000000000000000001",
        agent_id=agent_id,
        task_type=task_type,
        protocol_version="0.1",
        status=ChildTaskStatus.WORKING,
        input_artifacts=["artifact-findings-1"],
        required_output_types=required_output_types or ["ImpactReport"],
        # deadline 必须是"相对现在"的实时时间：协议字段语义是绝对截止时间，
        # 固定时间戳会让示例在真实时间流逝后立即过期。
        deadline=make_deadline(45),
        attempt=1,
        idempotency_key=f"{parent_task_id}:{task_type}:v1",
        correlation_id=f"{parent_task_id}:{task_type}",
        state_version=1,
    )


def example_a2a_message(task_id: str = "impact-0000000000000000000000001") -> A2AMessage:
    return A2AMessage(
        message_id=new_message_id(),
        task_id=task_id,
        type="task.completed",
        role="agent",
        correlation_id="review-01J:impact",
        artifact_refs=["artifact-impact-1"],
        error=None,
        created_at=FIXED_NOW,
    )


def example_a2a_message_with_error(task_id: str = "impact-0000000000000000000000001") -> A2AMessage:
    return A2AMessage(
        message_id=new_message_id(),
        task_id=task_id,
        type="task.failed",
        role="agent",
        correlation_id="review-01J:impact",
        artifact_refs=[],
        error=A2AError(code="ARTIFACT_SCHEMA_INVALID", message="缺少必填字段 findings"),
        created_at=FIXED_NOW,
    )


def example_state_intent() -> StateIntent:
    return StateIntent(
        expected_version=7,
        owner="FixAgent",
        changes={"candidate_patch": "--- a/app.py", "next_action": "TESTING"},
        reason="patch_generated",
        idempotency_key="task-123:fix:comment-9:v1",
    )


def example_finding_item() -> FindingItem:
    return FindingItem(
        rule_id="R002_HARDCODED_SECRET",
        title="硬编码密钥",
        cwe="CWE-798",
        severity=Severity.CRITICAL,
        file="app/config.py",
        line=12,
        evidence='API_KEY = "sk-live-1234567890"',
        message="检测到硬编码密钥，应改为从环境变量读取。",
        confidence_base=0.80,
        confidence=0.95,
        confidence_level=ConfidenceLevel.CONFIRMED,
        auto_fixable=True,
        fix_category=FixCategory.HARDCODED_SECRET,
        in_changed_lines=True,
        context_confirmed=True,
        symbol="app.config.API_KEY",
        rule_version="rules-v1",
    )


def example_finding_artifact() -> FindingArtifact:
    item = example_finding_item()
    return FindingArtifact(
        rule_set_version="rules-v1",
        scan_mode="ast",
        degraded_reason=None,
        files_scanned=["app/config.py"],
        findings=[item],
        stats=FindingStats(
            total=1,
            by_severity={"critical": 1},
            by_confidence_level={"confirmed": 1},
            suppressed=0,
        ),
    )


def example_impact_report() -> ImpactReport:
    return ImpactReport(
        changed_symbols=["app.auth.login"],
        direct_callers=["api.login"],
        direct_callees=["db.query"],
        affected_files=["app/auth.py", "api.py"],
        risk_level=RiskLevel.MEDIUM,
        uncertain=False,
        dynamic_calls=[],
        symbol_details=[
            ChangedSymbol(
                name="app.auth.login",
                kind="function",
                file="app/auth.py",
                line=10,
                changed_lines=[12, 13],
            )
        ],
        call_graph=[
            CallEdge(caller="api.login", callee="app.auth.login", file="api.py", line=22),
            CallEdge(caller="app.auth.login", callee="db.query", file="app/auth.py", line=18),
        ],
        stats=ImpactStats(
            changed_symbol_count=1,
            affected_file_count=2,
            caller_count=1,
            callee_count=1,
        ),
        analyzers=["ast", "callgraph"],
    )


def example_review_comment() -> ReviewComment:
    return ReviewComment(
        finding_ref="R002_HARDCODED_SECRET@app/config.py:12",
        message="该处直接写入了长期有效的密钥，一旦进入版本库即视为泄露。",
        suggestion="改为 os.environ['CODEPILOT_API_KEY'] 并在部署环境中注入。",
        confidence_level=ConfidenceLevel.CONFIRMED,
        impact_scope="仅影响 app/config.py 的模块级常量",
        citations=["app/config.py:12"],
        generated_by="rules",
    )


PATCH_DIFF = (
    "diff --git a/app/config.py b/app/config.py\n"
    "--- a/app/config.py\n"
    "+++ b/app/config.py\n"
    "@@ -9,3 +9,5 @@\n"
    " import os\n"
    " \n"
    '-API_KEY = "sk-live-1234567890"\n'
    "+API_KEY = os.environ.get(\"CODEPILOT_API_KEY\")\n"
    "+if not API_KEY:\n"
    '+    raise RuntimeError("CODEPILOT_API_KEY is required")\n'
)


def example_patch_candidate(patch_version: int = 1) -> PatchCandidate:
    return PatchCandidate(
        patch_version=patch_version,
        patch_hash=compute_text_hash(PATCH_DIFF),
        base_commit="synthetic-base-001",
        target_branch="codepilot/task-123",
        fix_category=FixCategory.HARDCODED_SECRET,
        finding_refs=["R002_HARDCODED_SECRET@app/config.py:12"],
        changed_files=["app/config.py"],
        changed_functions=["app.config.<module>"],
        added_lines=3,
        removed_lines=1,
        diff=PATCH_DIFF,
        idempotency_key="task-123:fix:comment-9:v1",
    )


def example_patch_evidence(patch_version: int = 1) -> PatchEvidence:
    candidate = example_patch_candidate(patch_version)
    return PatchEvidence(
        patch_version=patch_version,
        patch_hash=candidate.patch_hash,
        changed_files=candidate.changed_files,
        changed_functions=candidate.changed_functions,
        scope_drift=False,
        scope_drift_files=[],
        wide_impact=False,
        coverage_before=0.82,
        coverage_after=0.83,
        coverage_delta=0.01,
        test_gap=False,
        sandbox_run_id="sandbox-000000000000000000000001",
        reasons=["scope 与审查意见一致"],
    )


def example_verify_evidence(patch_version: int = 1, *, ok: bool = True) -> VerifyEvidence:
    candidate = example_patch_candidate(patch_version)
    return VerifyEvidence(
        patch_version=patch_version,
        patch_hash=candidate.patch_hash,
        sandbox_run_id="sandbox-000000000000000000000001",
        sandbox_degraded=False,
        degraded_reason=None,
        exit_code=0 if ok else 1,
        apply_check=ApplyCheck(
            ok=True,
            command="git apply --check --whitespace=nowarn patch.diff",
            message="patch applies cleanly",
        ),
        pre_scan=PreScanResult(ok=True, blocked_patterns=[]),
        lint=LintResult(ok=ok, tool="ruff", exit_code=0 if ok else 1, issue_count=0 if ok else 2, summary="2 issues"),
        unit_tests=TestResult(
            ok=ok,
            passed=12 if ok else 10,
            failed=0 if ok else 2,
            errors=0,
            total=12,
            duration_seconds=1.42,
        ),
        regression=RegressionResult(
            ok=ok,
            baseline_failed_total=2,
            baseline_failed_fixed=2 if ok else 0,
            pass_rate=1.0 if ok else 0.0,
        ),
        coverage=CoverageResult(before=0.82, after=0.83, delta=0.01, source="coverage.py"),
        test_gap=False,
        scope_drift=False,
        evidence_sufficient=ok,
        tiers=[
            TierResult(tier="prescan", ok=True, detail="pass"),
            TierResult(tier="apply_check", ok=True, detail="pass"),
            TierResult(tier="lint", ok=ok, detail="pass" if ok else "ruff issues"),
            TierResult(tier="unit_test", ok=ok, detail="pass" if ok else "failed"),
            TierResult(tier="regression", ok=ok, detail="pass" if ok else "baseline regression failed"),
            TierResult(tier="coverage", ok=True, detail="delta=+0.01"),
        ],
        sanitized_output="redacted sandbox output",
    )


PAYLOAD_EXAMPLES = {
    ArtifactType.FINDING: example_finding_artifact,
    ArtifactType.IMPACT_REPORT: example_impact_report,
    ArtifactType.REVIEW_COMMENT: example_review_comment,
    ArtifactType.PATCH_CANDIDATE: example_patch_candidate,
    ArtifactType.PATCH_EVIDENCE: example_patch_evidence,
    ArtifactType.VERIFY_EVIDENCE: example_verify_evidence,
}


def example_envelope(artifact_type: ArtifactType, *, task_id: str | None = None) -> ArtifactEnvelope:
    builder = PAYLOAD_EXAMPLES[artifact_type]
    payload = builder()
    return ArtifactEnvelope.build(
        artifact_id=new_artifact_id(),
        task_id=task_id or "review-00000000000000000000000001",
        artifact_type=artifact_type,
        data=payload.model_dump(mode="json"),
    )


EXAMPLE_INPUT_DIFF = (
    "diff --git a/app/config.py b/app/config.py\n"
    "--- a/app/config.py\n"
    "+++ b/app/config.py\n"
    "@@ -1,4 +1,5 @@\n"
    " import os\n"
    " import subprocess\n"
    " \n"
    '-API_KEY = "sk-live-1234567890"\n'
    '+API_KEY = "sk-live-0987654321"\n'
    "+subprocess.run('ls ' + os.environ['DIR'], shell=True)\n"
)

EXAMPLE_INPUT = {
    "input_type": str(InputType.DIFF),
    "content": EXAMPLE_INPUT_DIFF,
    "context_policy": str(ContextPolicy.FUNCTION),
    "base_commit": "synthetic-base-001",
}


__all__ = [
    "EXAMPLE_INPUT",
    "EXAMPLE_INPUT_DIFF",
    "FIXED_NOW",
    "PATCH_DIFF",
    "PAYLOAD_EXAMPLES",
    "example_a2a_message",
    "example_a2a_message_with_error",
    "example_a2a_task",
    "example_agent_card",
    "example_envelope",
    "example_finding_artifact",
    "example_finding_item",
    "example_impact_report",
    "example_patch_candidate",
    "example_patch_evidence",
    "example_review_comment",
    "example_state_intent",
    "example_verify_evidence",
]
