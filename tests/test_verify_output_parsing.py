"""Verify Agent 输出解析的回归测试。

历史缺陷：ruff 与 pytest 的输出被拼在同一条 stdout 中，`_parse_pytest` 会把 ruff 的
``Found 2 errors.`` 当成 "2 errors"，于是"测试全过 + lint 有问题"被误判为"测试有错误"，
导致质量门禁把可以修复的补丁一律拒绝（在 `--with-fix` 评测里表现为 QUALITY_GATE_FAILED）。
"""

from __future__ import annotations

import pytest

from agents.verify.agent import _failed_tests, _parse_apply_check, _parse_lint, _parse_pytest, _pytest_section
from sandbox.base import SandboxResult

# 真实沙箱输出的结构：apply → ruff（可能报 N errors）→ pytest
REAL_OUTPUT_WITH_LINT_ERRORS = "\n".join(
    [
        "CODEPILOT_APPLY_CHECK_OK",
        "CODEPILOT_APPLY_OK",
        "app/main.py:2:8: F401 [*] `os` imported but unused",
        "app/main.py:3:8: F401 [*] `subprocess` imported but unused",
        "Found 2 errors.",
        "[*] 2 fixable with the `--fix` option.",
        "CODEPILOT_LINT_FAIL",
        ".",
        "1 passed in 0.02s",
        "",
    ]
)

CLEAN_OUTPUT = "\n".join(
    [
        "CODEPILOT_APPLY_CHECK_OK",
        "CODEPILOT_APPLY_OK",
        "CODEPILOT_LINT_OK",
        "3 passed in 0.11s",
        "",
    ]
)

FAILING_OUTPUT = "\n".join(
    [
        "CODEPILOT_APPLY_CHECK_OK",
        "CODEPILOT_APPLY_OK",
        "Found 1 error.",
        "CODEPILOT_LINT_FAIL",
        "FAILED tests/test_main.py::test_api_key - KeyError",
        "1 failed, 2 passed in 0.20s",
        "",
    ]
)


def test_ruff_error_count_is_not_read_as_pytest_errors() -> None:
    result = _parse_pytest(REAL_OUTPUT_WITH_LINT_ERRORS)
    assert result.errors == 0, "ruff 的 'Found 2 errors.' 不能被当成 pytest 错误"
    assert result.passed == 1
    assert result.failed == 0
    assert result.ok is True


def test_clean_output_is_parsed_correctly() -> None:
    result = _parse_pytest(CLEAN_OUTPUT)
    assert (result.ok, result.passed, result.failed, result.errors) == (True, 3, 0, 0)


def test_real_test_failure_is_detected() -> None:
    result = _parse_pytest(FAILING_OUTPUT)
    assert result.failed == 1
    assert result.passed == 2
    assert result.ok is False
    assert _failed_tests(_pytest_section(FAILING_OUTPUT)) == ["tests/test_main.py::test_api_key"]


def test_pytest_section_starts_after_lint_marker() -> None:
    section = _pytest_section(REAL_OUTPUT_WITH_LINT_ERRORS)
    assert "Found 2 errors." not in section
    assert "1 passed" in section
    # 没有 lint 标记时退化为整段输出（兼容旧格式）
    assert _pytest_section("1 passed in 0.01s") == "1 passed in 0.01s"


@pytest.mark.parametrize(
    ("output", "expected_ok"),
    [
        ("CODEPILOT_APPLY_CHECK_OK", True),
        ("CODEPILOT_APPLY_CHECK_OK_ZERO", True),
        ("CODEPILOT_APPLY_CHECK_FAIL", False),
        ("", False),
    ],
)
def test_apply_check_parsing(output: str, expected_ok: bool) -> None:
    result = _parse_apply_check(SandboxResult(status="passed", exit_code=0, stdout=output), output)
    assert result.ok is expected_ok


def test_lint_parsing_counts_issues_only_from_diff_lines() -> None:
    lint = _parse_lint(REAL_OUTPUT_WITH_LINT_ERRORS)
    assert lint.ok is False
    assert lint.issue_count == 2
    assert "2" in lint.summary
    assert _parse_lint(CLEAN_OUTPUT).ok is True
