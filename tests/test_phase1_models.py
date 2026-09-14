"""协议模型、置信度与工作区解析测试（阶段一核心行为）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from a2a.examples import EXAMPLE_INPUT_DIFF, example_a2a_task, example_agent_card
from a2a.protocol import (
    A2ATask,
    AgentCard,
    ArtifactEnvelope,
    compute_content_hash,
)
from a2a.registry import (
    AGENT_POLICIES,
    TASK_TYPE_TO_AGENT,
    AgentRegistry,
)
from domain.confidence import (
    ConfidenceInputs,
    dedup_findings,
    dedup_key,
    is_high_risk_path,
    level_for,
    score_confidence,
)
from domain.diffparse import parse_unified_diff
from domain.enums import (
    ArtifactType,
    ChildTaskType,
    ConfidenceLevel,
    ContextPolicy,
    InputType,
    Severity,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_task_id
from domain.workspace import Workspace, build_workspace

# ---------------------------------------------------------------------------
# 协议模型
# ---------------------------------------------------------------------------


def test_agent_card_rejects_extra_fields() -> None:
    payload = example_agent_card().model_dump(mode="json")
    payload["secret_capability"] = "root"
    with pytest.raises(ValidationError):
        AgentCard.model_validate(payload)


def test_agent_card_rejects_bad_agent_id() -> None:
    payload = example_agent_card().model_dump(mode="json")
    payload["agent_id"] = "Review Agent"
    with pytest.raises(ValidationError):
        AgentCard.model_validate(payload)


def test_agent_card_rejects_unsupported_protocol_version() -> None:
    payload = example_agent_card().model_dump(mode="json")
    payload["protocol_versions"] = ["0.2"]
    with pytest.raises(ValidationError):
        AgentCard.model_validate(payload)


def test_task_requires_timezone_aware_deadline() -> None:
    task = example_a2a_task()
    payload = task.model_dump(mode="json")
    payload["deadline"] = "2026-09-13T12:00:45"
    with pytest.raises(ValidationError):
        A2ATask.model_validate(payload)


def test_task_attempt_is_capped_at_two() -> None:
    with pytest.raises(ValidationError):
        A2ATask.model_validate(example_a2a_task().model_dump(mode="json") | {"attempt": 3})


def test_task_deadline_expiry() -> None:
    from datetime import timedelta

    task = example_a2a_task()
    assert not task.is_expired()
    assert task.is_expired(task.deadline + timedelta(seconds=1))
    assert task.deadline - timedelta(seconds=45) <= task.deadline


def test_content_hash_is_deterministic_and_order_insensitive() -> None:
    assert compute_content_hash({"a": 1, "b": 2}) == compute_content_hash({"b": 2, "a": 1})
    assert compute_content_hash({"a": 1}) != compute_content_hash({"a": 2})
    assert compute_content_hash({"a": 1}).startswith("sha256:")


def test_envelope_build_verifies_hash() -> None:
    envelope = ArtifactEnvelope.build(
        artifact_id="artifact-0000000000000000000001",
        task_id="review-00000000000000000000000001",
        artifact_type=ArtifactType.IMPACT_REPORT,
        data={"changed_symbols": [], "risk_level": "low"},
    )
    assert envelope.size_bytes > 0
    envelope.verify_hash()


# ---------------------------------------------------------------------------
# Agent Registry
# ---------------------------------------------------------------------------


def test_static_registry_loads_all_four_agents() -> None:
    registry = AgentRegistry.load_static()
    assert registry.agent_ids() == ["fix-agent", "impact-agent", "review-agent", "verify-agent"]
    assert set(TASK_TYPE_TO_AGENT.values()) == set(registry.agent_ids())


def test_registry_resolves_task_type_to_agent() -> None:
    registry = AgentRegistry.load_static()
    assert registry.resolve(task_type=ChildTaskType.REVIEW).agent_id == "review-agent"
    assert registry.resolve(task_type=ChildTaskType.IMPACT).agent_id == "impact-agent"


def test_registry_rejects_missing_capability() -> None:
    registry = AgentRegistry.load_static()
    with pytest.raises(CodePilotError) as excinfo:
        registry.resolve(task_type=ChildTaskType.IMPACT, required_capabilities=["impact.rag"])
    assert excinfo.value.code is ErrorCode.CAPABILITY_NOT_AVAILABLE


def test_registry_rejects_incompatible_protocol_version() -> None:
    registry = AgentRegistry.load_static()
    with pytest.raises(CodePilotError) as excinfo:
        registry.resolve(task_type=ChildTaskType.REVIEW, protocol_version="0.2")
    assert excinfo.value.code is ErrorCode.PROTOCOL_VERSION_UNSUPPORTED


def test_registry_card_version_must_increase() -> None:
    registry = AgentRegistry.load_static()
    stale = example_agent_card().model_copy(update={"card_version": "0.9"})
    with pytest.raises(CodePilotError):
        registry.register(stale)


def test_review_and_impact_cannot_use_write_tools() -> None:
    for agent_id in ("review-agent", "impact-agent", "verify-agent"):
        policy = AGENT_POLICIES[agent_id]
        with pytest.raises(CodePilotError) as excinfo:
            policy.assert_tool_allowed("write_patch")
        assert excinfo.value.code is ErrorCode.PERMISSION_DENIED
        policy.assert_tool_allowed("read_file")


def test_fix_agent_cannot_merge() -> None:
    with pytest.raises(CodePilotError) as excinfo:
        AGENT_POLICIES["fix-agent"].assert_tool_allowed("merge_branch")
    assert excinfo.value.code is ErrorCode.PERMISSION_DENIED


def test_load_static_fails_fast_on_corrupt_card(tmp_path) -> None:
    (tmp_path / "broken-agent.json").write_text('{"agent_id": "broken-agent"}', encoding="utf-8")
    with pytest.raises(CodePilotError) as excinfo:
        AgentRegistry.load_static(tmp_path)
    assert excinfo.value.code is ErrorCode.AGENT_CARD_INVALID


# ---------------------------------------------------------------------------
# 置信度
# ---------------------------------------------------------------------------


def test_confidence_levels_follow_thresholds() -> None:
    assert level_for(0.90) is ConfidenceLevel.CONFIRMED
    assert level_for(0.70) is ConfidenceLevel.PROBABLE
    assert level_for(0.45) is ConfidenceLevel.SUSPICIOUS
    assert level_for(0.20) is ConfidenceLevel.SUPPRESSED


def test_confidence_modifiers_are_applied() -> None:
    baseline = score_confidence(ConfidenceInputs(confidence_base=0.50))
    assert baseline.score == pytest.approx(0.50)
    assert baseline.level is ConfidenceLevel.SUSPICIOUS
    boosted = score_confidence(
        ConfidenceInputs(confidence_base=0.50, context_confirmed=True, in_changed_lines=True)
    )
    assert boosted.score == pytest.approx(0.85)
    assert boosted.level is ConfidenceLevel.CONFIRMED

    reduced = score_confidence(ConfidenceInputs(confidence_base=0.80, historical_context_only=True))
    assert reduced.score == pytest.approx(0.60)
    assert "historical_only-0.20" in reduced.reasons


def test_confidence_is_clamped() -> None:
    assert score_confidence(
        ConfidenceInputs(confidence_base=0.95, context_confirmed=True, in_changed_lines=True)
    ).score == 1.0
    assert score_confidence(ConfidenceInputs(confidence_base=0.0)).score == 0.0


def test_dedup_keeps_higher_confidence() -> None:
    low = {"file": "a.py", "line": 1, "rule_id": "R001", "confidence": 0.5, "severity": Severity.INFO}
    high = {"file": "a.py", "line": 1, "rule_id": "R001", "confidence": 0.9, "severity": Severity.WARNING}
    other = {"file": "a.py", "line": 2, "rule_id": "R001", "confidence": 0.4, "severity": Severity.INFO}

    result = dedup_findings(
        [low, high, other],
        key=lambda item: dedup_key(item["file"], item["line"], item["rule_id"]),
        score=lambda item: item["confidence"],
        severity=lambda item: item["severity"],
    )
    assert len(result) == 2
    assert high in result


def test_high_risk_path_detection() -> None:
    assert is_high_risk_path("app/auth/login.py")
    assert is_high_risk_path("services/payment.py")
    assert not is_high_risk_path("app/utils/format.py")


# ---------------------------------------------------------------------------
# Diff 解析与工作区
# ---------------------------------------------------------------------------


def test_parse_diff_line_numbers() -> None:
    parsed = parse_unified_diff(EXAMPLE_INPUT_DIFF)
    assert parsed.changed_files == ["app/config.py"]
    file_diff = parsed.files[0]
    assert file_diff.changed_lines == {4, 5}
    assert file_diff.added_lines == [4, 5]


def test_parse_diff_rejects_empty_and_non_python() -> None:
    with pytest.raises(CodePilotError) as empty:
        parse_unified_diff("   ")
    assert empty.value.code is ErrorCode.INVALID_INPUT

    js_diff = (
        "diff --git a/app.js b/app.js\n--- a/app.js\n+++ b/app.js\n@@ -1,1 +1,1 @@\n-var a=1\n+var a=2\n"
    )
    with pytest.raises(CodePilotError) as non_python:
        parse_unified_diff(js_diff)
    assert non_python.value.code is ErrorCode.INVALID_INPUT


def test_parse_diff_rejects_path_traversal() -> None:
    traversal = (
        "diff --git a/../../etc/passwd b/../../etc/passwd\n"
        "--- a/../../etc/passwd\n+++ b/../../etc/passwd\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    with pytest.raises(CodePilotError) as excinfo:
        parse_unified_diff(traversal)
    assert excinfo.value.code is ErrorCode.INVALID_INPUT


def test_workspace_from_diff_and_readonly_queries() -> None:
    workspace, parsed = build_workspace(
        input_type=InputType.DIFF,
        content=EXAMPLE_INPUT_DIFF,
        base_commit="synthetic-base-001",
        context_policy=ContextPolicy.FUNCTION,
    )
    assert parsed is not None
    assert workspace.changed_files == ["app/config.py"]
    assert workspace.in_changed_lines("app/config.py", 4)
    assert not workspace.in_changed_lines("app/config.py", 1)

    read = workspace.read_file("app/config.py")
    assert "API_KEY" in read["content"]
    assert read["total_lines"] >= 5

    listing = workspace.list_files(only_changed=True)
    assert [item["path"] for item in listing] == ["app/config.py"]

    found = workspace.search_code(pattern=r"shell=True")
    assert found["count"] == 1
    assert found["matches"][0]["in_changed_lines"] is True

    diff = workspace.get_diff()
    assert diff["files"] == ["app/config.py"]

    round_trip = Workspace.from_payload(workspace.to_payload())
    assert round_trip.files == workspace.files
    assert round_trip.changed_lines == workspace.changed_lines


def test_workspace_rejects_unknown_file() -> None:
    workspace = Workspace(
        task_id=new_task_id(),
        base_commit="base",
        input_type=InputType.DIFF,
        context_policy=ContextPolicy.FUNCTION,
    )
    with pytest.raises(CodePilotError) as excinfo:
        workspace.read_file("nope.py")
    assert excinfo.value.code is ErrorCode.INVALID_INPUT


def test_workspace_rejects_invalid_regex() -> None:
    workspace = Workspace(
        task_id=new_task_id(),
        base_commit="base",
        input_type=InputType.DIFF,
        context_policy=ContextPolicy.FUNCTION,
        files={"a.py": "x = 1\n"},
    )
    with pytest.raises(CodePilotError) as excinfo:
        workspace.search_code(pattern="[unclosed")
    assert excinfo.value.code is ErrorCode.INVALID_INPUT


def test_workspace_from_zip() -> None:
    import base64
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("pkg/mod.py", "def f():\n    return 1\n")
        archive.writestr("README.md", "# demo\n")
        archive.writestr("pkg/other.txt", "ignored\n")

    content = base64.b64encode(buffer.getvalue()).decode("ascii")
    workspace, parsed = build_workspace(
        input_type=InputType.ZIP,
        content=content,
        base_commit="synthetic-base-001",
    )
    assert parsed is None
    assert workspace.python_files == ["pkg/mod.py"]
    assert workspace.input_type is InputType.ZIP
    assert workspace.partial is False


def test_workspace_zip_rejects_path_traversal() -> None:
    import base64
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.py", "x=1\n")
    content = base64.b64encode(buffer.getvalue()).decode("ascii")
    with pytest.raises(CodePilotError) as excinfo:
        build_workspace(input_type=InputType.ZIP, content=content, base_commit="base")
    assert excinfo.value.code is ErrorCode.INVALID_INPUT


def test_workspace_zip_rejects_non_base64() -> None:
    with pytest.raises(CodePilotError) as excinfo:
        build_workspace(input_type=InputType.ZIP, content="not base64!!", base_commit="base")
    assert excinfo.value.code is ErrorCode.INVALID_INPUT
