"""协议模型与 schemas/ JSON Schema 的一致性测试（docs/00 §3 P0 冻结项）。"""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from a2a.examples import (
    example_a2a_message,
    example_a2a_message_with_error,
    example_a2a_task,
    example_agent_card,
    example_envelope,
    example_state_intent,
)
from a2a.protocol import (
    A2AMessage,
    A2ATask,
    AgentCard,
    ArtifactEnvelope,
    StateIntent,
)
from a2a.schema_registry import (
    A2A_MESSAGE_SCHEMA,
    A2A_TASK_SCHEMA,
    AGENT_CARD_SCHEMA,
    ARTIFACT_ENVELOPE_SCHEMA,
    ARTIFACT_PAYLOAD_SCHEMAS,
    STATE_INTENT_SCHEMA,
    available_schemas,
    schema_document,
    validate_artifact_envelope,
    validate_artifact_payload,
    validate_document,
)
from domain.enums import ArtifactType
from domain.errors import CodePilotError, ErrorCode

MODEL_SCHEMA_MAP = [
    (AgentCard, AGENT_CARD_SCHEMA),
    (A2ATask, A2A_TASK_SCHEMA),
    (A2AMessage, A2A_MESSAGE_SCHEMA),
    (ArtifactEnvelope, ARTIFACT_ENVELOPE_SCHEMA),
    (StateIntent, STATE_INTENT_SCHEMA),
]


def test_all_schemas_are_valid_draft_2020_12() -> None:
    names = available_schemas()
    assert AGENT_CARD_SCHEMA in names
    assert ARTIFACT_ENVELOPE_SCHEMA in names
    for name in names:
        Draft202012Validator.check_schema(schema_document(name))


def test_every_artifact_type_has_a_payload_schema() -> None:
    assert set(ARTIFACT_PAYLOAD_SCHEMAS) == set(ArtifactType)


@pytest.mark.parametrize(("model", "schema_name"), MODEL_SCHEMA_MAP)
def test_model_fields_match_schema_properties(model, schema_name: str) -> None:
    document = schema_document(schema_name)
    properties = set(document["properties"])
    fields = set(model.model_fields)
    assert fields == properties, f"{model.__name__} 与 {schema_name} 字段不一致"
    assert set(document["required"]) <= properties


@pytest.mark.parametrize(("model", "schema_name"), MODEL_SCHEMA_MAP)
def test_examples_validate_against_schema(model, schema_name: str) -> None:
    examples = {
        AgentCard: example_agent_card(),
        A2ATask: example_a2a_task(),
        A2AMessage: example_a2a_message(),
        ArtifactEnvelope: example_envelope(ArtifactType.IMPACT_REPORT),
        StateIntent: example_state_intent(),
    }
    validate_document(schema_name, examples[model].model_dump(mode="json"))


def test_message_with_error_validates() -> None:
    message = example_a2a_message_with_error()
    validate_document(A2A_MESSAGE_SCHEMA, message.model_dump(mode="json"))


@pytest.mark.parametrize("artifact_type", list(ArtifactType))
def test_artifact_payloads_validate(artifact_type: ArtifactType) -> None:
    envelope = example_envelope(artifact_type)
    validate_artifact_envelope(envelope.model_dump(mode="json"))
    validate_artifact_payload(artifact_type, envelope.data)
    envelope.verify_hash()


def test_artifact_payload_rejects_invalid_severity() -> None:
    envelope = example_envelope(ArtifactType.FINDING)
    data = dict(envelope.data)
    data["findings"] = [dict(data["findings"][0], severity="blocker")]
    with pytest.raises(CodePilotError) as excinfo:
        validate_artifact_payload(ArtifactType.FINDING, data)
    assert excinfo.value.code is ErrorCode.ARTIFACT_SCHEMA_INVALID


def test_artifact_payload_rejects_missing_required_field() -> None:
    envelope = example_envelope(ArtifactType.IMPACT_REPORT)
    data = dict(envelope.data)
    data.pop("risk_level")
    with pytest.raises(CodePilotError) as excinfo:
        validate_artifact_payload(ArtifactType.IMPACT_REPORT, data)
    assert excinfo.value.code is ErrorCode.ARTIFACT_SCHEMA_INVALID
    assert excinfo.value.details["path"] == "risk_level"


def test_unknown_artifact_type_rejected() -> None:
    with pytest.raises(CodePilotError) as excinfo:
        validate_artifact_payload("UnknownType", {})
    assert excinfo.value.code is ErrorCode.ARTIFACT_SCHEMA_INVALID


def test_artifact_hash_mismatch_detected() -> None:
    envelope = example_envelope(ArtifactType.IMPACT_REPORT)
    tampered = envelope.model_copy(update={"data": {**envelope.data, "risk_level": "high"}})
    with pytest.raises(CodePilotError) as excinfo:
        tampered.verify_hash()
    assert excinfo.value.code is ErrorCode.ARTIFACT_HASH_MISMATCH


def test_envelope_rejects_bad_hash_format() -> None:
    payload = example_envelope(ArtifactType.IMPACT_REPORT).model_dump(mode="json")
    payload["content_hash"] = "md5:deadbeef"
    with pytest.raises(CodePilotError) as excinfo:
        validate_artifact_envelope(payload)
    assert excinfo.value.code is ErrorCode.ARTIFACT_SCHEMA_INVALID


def test_envelope_rejects_extra_field() -> None:
    payload = example_envelope(ArtifactType.IMPACT_REPORT).model_dump(mode="json")
    payload["extra"] = 1
    with pytest.raises(CodePilotError):
        validate_artifact_envelope(payload)
