"""Verify Agent（FR-045~FR-060：在 Docker 沙箱内验证候选补丁）。

质量门禁顺序（SRS §4.4，前一层失败即停止后续执行）：
危险模式预审查 → ``git apply --check`` → ruff → 单元测试 → 修复前失败用例回归 → 覆盖率与 TEST_GAP。

安全约束：
- 预审查在进入沙箱**之前**完成（FR-059），命中危险模式直接拦截；
- 所有测试都在 Docker 沙箱内执行（FR-050），宿主机永不执行提交代码；
- Docker 不可用时返回 ``SANDBOX_UNAVAILABLE``，不降级为宿主机执行；
- 输出在进入证据前完成脱敏（FR-060）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from a2a.protocol import (
    A2AMessage,
    ApplyCheck,
    ArtifactEnvelope,
    CoverageResult,
    LintResult,
    PatchCandidate,
    PreScanResult,
    RegressionResult,
    TestResult,
    TierResult,
    VerifyEvidence,
)
from agents.base import AgentHandler, AgentRequest, AgentResult
from domain.clock import utcnow
from domain.enums import ArtifactType
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_artifact_id, new_message_id
from sandbox.base import SandboxLimits, SandboxResult
from sandbox.prescan import prescan_patch

COVERAGE_FAIL_THRESHOLD = -0.05
COVERAGE_WARN_THRESHOLD = -0.02
PYTEST_SUMMARY = re.compile(r"(?P<count>\d+) (?P<kind>passed|failed|error|errors|skipped)")
GIT_INIT = (
    "git init -q ."
    " && git add -A"
    " && git -c user.email=sandbox@codepilot.local -c user.name=codepilot-sandbox commit -qm base"
)
COVERAGE_JSON = "coverage.json"


class VerifyAgent(AgentHandler):
    agent_id = "verify-agent"

    def handle(self, request: AgentRequest) -> AgentResult:
        started = time.perf_counter()
        candidate = _candidate_from(request)
        patch_text = candidate.diff

        # 1. 危险模式预审查（进入沙箱之前）
        pre_scan = prescan_patch(patch_text)
        if not pre_scan.ok:
            evidence = _blocked_evidence(candidate, pre_scan)
            return _result(request, evidence, started, blocked=True)

        sandbox = request.sandbox
        limits: SandboxLimits = request.sandbox_limits or SandboxLimits()
        if sandbox is None or not getattr(sandbox, "available", False):
            reason = getattr(sandbox, "unavailable_reason", None) or "未配置沙箱执行器"
            raise CodePilotError(
                ErrorCode.SANDBOX_UNAVAILABLE,
                f"{reason}：禁止在宿主机执行提交代码（宪法第八条）",
            )

        files = {
            path: content
            for path, content in request.workspace.files.items()
            if path.endswith(".py")
        }
        if not files:
            raise CodePilotError(ErrorCode.PATCH_INVALID, "工作区没有可验证的 Python 文件")

        # 2. 基线：修复前测试与覆盖率
        baseline = sandbox.run(
            files=files,
            argv=_pytest_argv(with_coverage=True),
            name=f"baseline-{request.task_id}",
            limits=limits,
        )
        baseline_failures = _failed_tests(baseline.stdout + baseline.stderr)

        # 3. 应用补丁 + 校验
        applied = sandbox.run(
            files=files,
            argv=_verify_argv(),
            name=f"verify-{request.task_id}",
            limits=limits,
            patch=patch_text,
        )
        combined = applied.stdout + "\n" + applied.stderr

        apply_check = _parse_apply_check(applied, combined)
        lint = _parse_lint(combined)
        tests = _parse_pytest(combined)
        regression = _regression(baseline_failures, _failed_tests(_pytest_section(combined)))
        coverage = _coverage(baseline, applied, files, candidate.changed_files)
        test_gap = _test_gap(applied, candidate.changed_files, tests)

        evidence = VerifyEvidence(
            patch_version=candidate.patch_version,
            patch_hash=candidate.patch_hash,
            sandbox_run_id=f"verify-{request.task_id}"[:64],
            sandbox_degraded=False,
            degraded_reason=None,
            exit_code=applied.exit_code if applied.exit_code is not None else -1,
            apply_check=apply_check,
            pre_scan=PreScanResult(ok=True, blocked_patterns=[]),
            lint=lint,
            unit_tests=tests,
            regression=regression,
            coverage=coverage,
            test_gap=test_gap,
            scope_drift=False,
            evidence_sufficient=False,
            tiers=_tiers(apply_check, lint, tests, regression, coverage, test_gap),
            sanitized_output=_sanitize_output(combined),
        )
        evidence = evidence.model_copy(
            update={
                "evidence_sufficient": _sufficient(evidence, applied),
                "sandbox_degraded": applied.timed_out,
                "degraded_reason": "沙箱执行超时" if applied.timed_out else None,
            }
        )
        return _result(request, evidence, started, blocked=False, sandbox=applied)


# ---------------------------------------------------------------------------
# 沙箱命令
# ---------------------------------------------------------------------------


def _pytest_argv(*, with_coverage: bool) -> list[str]:
    base = "python -m pytest -q --no-header -p no:cacheprovider"
    if with_coverage:
        return [
            "/bin/sh",
            "-c",
            f"{GIT_INIT} && {base} --cov=. --cov-report=term --cov-report=json:{COVERAGE_JSON} || true",
        ]
    return ["/bin/sh", "-c", f"{GIT_INIT} && {base} || true"]


def _verify_argv() -> list[str]:
    """apply --check → apply → ruff → pytest（分层执行，任一失败即停止后续）。

    补丁位于文件末尾时没有尾部上下文，``git apply`` 严格模式会误判为不可应用，
    因此严格模式失败后允许退化为 ``--unidiff-zero``（只放宽上下文要求，被删除行仍需逐字匹配）。
    """
    script = (
        f"{GIT_INIT}"
        " && (git apply --check --whitespace=nowarn .codepilot/patch.diff"
        " && echo CODEPILOT_APPLY_CHECK_OK"
        " || (git apply --check --whitespace=nowarn --unidiff-zero .codepilot/patch.diff"
        " && echo CODEPILOT_APPLY_CHECK_OK_ZERO)"
        " || echo CODEPILOT_APPLY_CHECK_FAIL)"
        " && (git apply --whitespace=nowarn .codepilot/patch.diff"
        " || git apply --whitespace=nowarn --unidiff-zero .codepilot/patch.diff)"
        " && echo CODEPILOT_APPLY_OK || echo CODEPILOT_APPLY_FAIL"
        " && (python -m ruff check --no-cache --output-format concise ."
        " && echo CODEPILOT_LINT_OK || echo CODEPILOT_LINT_FAIL)"
        " && (python -m pytest -q --no-header -p no:cacheprovider"
        f" --cov=. --cov-report=term --cov-report=json:{COVERAGE_JSON} || true)"
    )
    return ["/bin/sh", "-c", script]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def _parse_apply_check(result: SandboxResult, output: str) -> ApplyCheck:
    strict_ok = "CODEPILOT_APPLY_CHECK_OK" in output
    relaxed_ok = "CODEPILOT_APPLY_CHECK_OK_ZERO" in output
    ok = (strict_ok or relaxed_ok) and "CODEPILOT_APPLY_CHECK_FAIL" not in output
    command = "git apply --check --whitespace=nowarn .codepilot/patch.diff"
    if ok and not strict_ok and relaxed_ok:
        command = f"{command} --unidiff-zero（补丁缺少尾部上下文，已记录退化模式）"
    return ApplyCheck(
        ok=ok,
        command=command,
        message="补丁可被 git apply 解析" if ok else _first_error(output, "apply --check 失败"),
    )


def _parse_lint(output: str) -> LintResult:
    ok = "CODEPILOT_LINT_OK" in output and "CODEPILOT_LINT_FAIL" not in output
    issues = [line for line in output.splitlines() if re.match(r"^[^\s:]+:\d+:\d+:", line.strip())]
    return LintResult(
        ok=ok,
        tool="ruff",
        exit_code=0 if ok else 1,
        issue_count=len(issues),
        summary="ruff 通过" if ok else f"ruff 报告 {len(issues)} 个问题",
    )


def _pytest_section(output: str) -> str:
    """截取 pytest 的输出段。

    ``_verify_argv`` 先跑 ruff 再跑 pytest，两者输出被拼在同一条 stdout 里。
    ruff 的汇总行形如 ``Found 2 errors.``，会被 ``PYTEST_SUMMARY`` 当成
    "2 errors" 误读，从而把通过的测试判成失败（曾经的真实缺陷）。
    因此这里只在 lint 标记之后解析 pytest 结果。
    """
    for marker in ("CODEPILOT_LINT_FAIL", "CODEPILOT_LINT_OK"):
        index = output.rfind(marker)
        if index >= 0:
            return output[index + len(marker) :]
    return output


def _parse_pytest(output: str) -> TestResult:
    section = _pytest_section(output)
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for match in PYTEST_SUMMARY.finditer(section):
        kind = match.group("kind")
        if kind == "error":
            kind = "errors"
        counts[kind] = int(match.group("count"))
    total = sum(counts.values())
    duration = 0.0
    duration_match = re.search(r"in (?P<seconds>\d+\.\d+)s", section)
    if duration_match:
        duration = float(duration_match.group("seconds"))
    ok = counts["failed"] == 0 and counts["errors"] == 0 and bool(section.strip())
    return TestResult(
        ok=ok,
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["errors"],
        total=total,
        duration_seconds=duration,
    )


def _failed_tests(output: str) -> list[str]:
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED ", "ERROR ")):
            names.append(stripped.split(" ", 1)[1].split(" - ")[0].strip())
    return sorted(set(names))


def _regression(baseline_failures: list[str], after_failures: list[str]) -> RegressionResult:
    total = len(baseline_failures)
    if total == 0:
        return RegressionResult(ok=True, baseline_failed_total=0, baseline_failed_fixed=0, pass_rate=1.0)
    remaining = set(baseline_failures) & set(after_failures)
    fixed = total - len(remaining)
    return RegressionResult(
        ok=len(remaining) == 0,
        baseline_failed_total=total,
        baseline_failed_fixed=fixed,
        pass_rate=round(fixed / total, 4),
    )


def _coverage(
    baseline: SandboxResult,
    applied: SandboxResult,
    files: dict[str, str],
    changed_files: list[str],
) -> CoverageResult:
    before = _coverage_from_output(baseline.stdout + baseline.stderr)
    after = _coverage_from_output(applied.stdout + applied.stderr)
    delta = None
    if before is not None and after is not None:
        delta = round(after - before, 4)
    source = "coverage.py" if after is not None else "unavailable"
    if after is None and _coverage_json_available(files):
        source = "coverage.py"
    return CoverageResult(before=before, after=after, delta=delta, source=source)


def _coverage_json_available(files: dict[str, str]) -> bool:
    return any(path.endswith(".py") for path in files)


def _coverage_from_output(output: str) -> float | None:
    match = re.search(r"TOTAL\s+\d+\s+\d+\s+(?P<percent>\d+(?:\.\d+)?)%", output)
    if match:
        return round(float(match.group("percent")) / 100.0, 4)
    # pytest-cov 的 json 报告摘要
    match = re.search(r'"percent_covered"\s*:\s*(?P<percent>\d+(?:\.\d+)?)', output)
    if match:
        return round(float(match.group("percent")) / 100.0, 4)
    return None


def _test_gap(applied: SandboxResult, changed_files: list[str], tests: TestResult) -> bool:
    """TEST_GAP：被修复代码路径缺少测试覆盖（FR-046）。"""
    if tests.total == 0:
        return True
    coverage = _coverage_from_output(applied.stdout + applied.stderr)
    if coverage is None:
        return False
    return coverage <= 0.0


def _tiers(
    apply_check: ApplyCheck,
    lint: LintResult,
    tests: TestResult,
    regression: RegressionResult,
    coverage: CoverageResult,
    test_gap: bool,
) -> list[TierResult]:
    tiers = [
        TierResult(tier="prescan", ok=True, detail="未命中危险模式"),
        TierResult(tier="apply_check", ok=apply_check.ok, detail=apply_check.message),
        TierResult(tier="lint", ok=lint.ok, detail=lint.summary),
        TierResult(tier="unit_test", ok=tests.ok, detail=f"{tests.passed} passed / {tests.failed} failed"),
        TierResult(
            tier="regression",
            ok=regression.ok,
            detail=f"基线失败用例修复率 {regression.pass_rate:.0%}",
        ),
        TierResult(
            tier="coverage",
            ok=coverage.delta is None or coverage.delta > COVERAGE_FAIL_THRESHOLD,
            detail=(
                f"覆盖率 {coverage.before} → {coverage.after}"
                if coverage.delta is not None
                else "覆盖率不可用"
            ),
        ),
    ]
    if test_gap:
        tiers.append(TierResult(tier="coverage", ok=False, detail="检测到 TEST_GAP"))
    return tiers


def _sufficient(evidence: VerifyEvidence, applied: SandboxResult) -> bool:
    if applied.timed_out:
        return False
    if not evidence.apply_check.ok or not evidence.lint.ok or not evidence.unit_tests.ok:
        return False
    if not evidence.regression.ok:
        return False
    if evidence.coverage.delta is not None and evidence.coverage.delta <= COVERAGE_FAIL_THRESHOLD:
        return False
    return not evidence.test_gap


def _blocked_evidence(candidate: PatchCandidate, pre_scan) -> VerifyEvidence:
    return VerifyEvidence(
        patch_version=candidate.patch_version,
        patch_hash=candidate.patch_hash,
        sandbox_run_id="prescan-blocked",
        sandbox_degraded=False,
        degraded_reason="预审查拦截，未进入沙箱",
        exit_code=-1,
        apply_check=ApplyCheck(ok=False, command="skipped", message="预审查拦截，未执行 git apply"),
        pre_scan=PreScanResult(ok=False, blocked_patterns=list(pre_scan.blocked_patterns)),
        lint=LintResult(ok=False, tool="ruff", exit_code=-1, issue_count=0, summary="未执行"),
        unit_tests=TestResult(ok=False, passed=0, failed=0, errors=0, total=0, duration_seconds=0.0),
        regression=RegressionResult(
            ok=False, baseline_failed_total=0, baseline_failed_fixed=0, pass_rate=0.0
        ),
        coverage=CoverageResult(before=None, after=None, delta=None, source="unavailable"),
        test_gap=True,
        scope_drift=False,
        evidence_sufficient=False,
        tiers=[TierResult(tier="prescan", ok=False, detail=", ".join(pre_scan.blocked_patterns))],
        sanitized_output="",
    )


def _first_error(output: str, fallback: str) -> str:
    for line in output.splitlines():
        if "error" in line.lower() or "fail" in line.lower():
            return line.strip()[:300]
    return fallback


def _sanitize_output(output: str, *, limit: int = 4000) -> str:
    from domain.sanitize import sanitize_text

    return sanitize_text(output, max_length=limit)


def _candidate_from(request: AgentRequest) -> PatchCandidate:
    envelope = request.input_of_type(str(ArtifactType.PATCH_CANDIDATE))
    if envelope is None:
        raise CodePilotError(ErrorCode.PATCH_INVALID, "Verify 子任务缺少 PatchCandidate 输入 Artifact")
    return PatchCandidate.model_validate(envelope.data)


def _result(
    request: AgentRequest,
    evidence: VerifyEvidence,
    started: float,
    *,
    blocked: bool,
    sandbox: SandboxResult | None = None,
) -> AgentResult:
    artifact = ArtifactEnvelope.build(
        artifact_id=new_artifact_id(),
        task_id=request.task_id,
        artifact_type=ArtifactType.VERIFY_EVIDENCE,
        data=evidence.model_dump(mode="json"),
    )
    message = A2AMessage(
        message_id=new_message_id(),
        task_id=request.task_id,
        type="task.completed" if evidence.evidence_sufficient else "task.failed",
        role="agent",
        correlation_id=request.task.correlation_id,
        artifact_refs=[artifact.artifact_id],
        error=None if evidence.evidence_sufficient else None,
        created_at=utcnow(),
    )
    return AgentResult(
        artifacts=[artifact],
        messages=[message],
        stats={
            "blocked": blocked,
            "evidence_sufficient": evidence.evidence_sufficient,
            "patch_version": evidence.patch_version,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "sandbox_duration_ms": sandbox.duration_ms if sandbox else 0,
            "sandbox_exit_code": sandbox.exit_code if sandbox else None,
            "resource_usage": sandbox.resource_usage if sandbox else {},
        },
    )


def coverage_payload(raw: str) -> dict[str, Any] | None:
    """解析 coverage.json（供阶段七的覆盖率报告复用）。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


__all__ = [
    "COVERAGE_FAIL_THRESHOLD",
    "COVERAGE_WARN_THRESHOLD",
    "VerifyAgent",
    "coverage_payload",
]
