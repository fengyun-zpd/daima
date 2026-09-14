"""统一错误码测试（docs/00 §3：每个错误都有 HTTP 状态和是否重试）。"""

from __future__ import annotations

import pytest

from domain.enums import ParentTaskStatus
from domain.errors import (
    ERROR_SPECS,
    NON_RETRYABLE_CODES,
    CodePilotError,
    ErrorCode,
    error_payload,
)


def test_every_error_code_has_a_spec() -> None:
    assert set(ERROR_SPECS) == set(ErrorCode)


def test_specs_are_complete() -> None:
    for code, spec in ERROR_SPECS.items():
        assert 400 <= spec.http_status <= 599, code
        assert isinstance(spec.retryable, bool)
        assert isinstance(spec.human_action, bool)
        assert spec.description, code


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.FORBIDDEN,
        ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
        ErrorCode.ARTIFACT_SCHEMA_INVALID,
        ErrorCode.ARTIFACT_HASH_MISMATCH,
        ErrorCode.ILLEGAL_STATE_TRANSITION,
        ErrorCode.SCOPE_DRIFT,
        ErrorCode.VERSION_CONFLICT,
        ErrorCode.TASK_STATUS_UNKNOWN,
        ErrorCode.IDEMPOTENCY_CONFLICT,
    ],
)
def test_security_relevant_errors_are_not_retryable(code: ErrorCode) -> None:
    """越权、Schema、协议、状态冲突类错误禁止重试（宪法第七条）。"""
    assert code in NON_RETRYABLE_CODES
    assert not ERROR_SPECS[code].retryable


@pytest.mark.parametrize(
    "code",
    [ErrorCode.TASK_TIMEOUT, ErrorCode.SANDBOX_TIMEOUT, ErrorCode.STATE_VERSION_CONFLICT],
)
def test_transient_errors_are_retryable(code: ErrorCode) -> None:
    assert ERROR_SPECS[code].retryable


def test_error_payload_shape() -> None:
    payload = error_payload(ErrorCode.INVALID_INPUT, "diff 为空", trace_id="trace-1")
    assert payload == {"code": "INVALID_INPUT", "message": "diff 为空", "trace_id": "trace-1"}


def test_error_payload_includes_details_when_present() -> None:
    payload = error_payload(
        ErrorCode.STATE_VERSION_CONFLICT, trace_id="trace-2", details={"expected": 3, "actual": 4}
    )
    assert payload["details"] == {"expected": 3, "actual": 4}


def test_error_carries_spec_metadata() -> None:
    error = CodePilotError(ErrorCode.PERMISSION_DENIED, trace_id="trace-3")
    assert error.http_status == 403
    assert error.retryable is False
    assert error.human_action is True
    assert error.default_parent_status is ParentTaskStatus.NEEDS_HUMAN


def test_quality_gate_errors_park_task_for_human() -> None:
    for code in (
        ErrorCode.QUALITY_GATE_FAILED,
        ErrorCode.TEST_GAP,
        ErrorCode.SCOPE_DRIFT,
        ErrorCode.PATCH_INVALID,
    ):
        assert ERROR_SPECS[code].default_parent_status is ParentTaskStatus.NEEDS_HUMAN


def test_error_message_defaults_to_spec_description() -> None:
    error = CodePilotError(ErrorCode.TASK_CANCELED)
    assert error.message == ERROR_SPECS[ErrorCode.TASK_CANCELED].description
    assert str(error).startswith("TASK_CANCELED:")
