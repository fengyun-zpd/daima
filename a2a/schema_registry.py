"""JSON Schema 注册与校验（FR-093 / FR-100；docs/00 §3 协议 Schema 冻结项）。

- 启动时加载 ``schemas/`` 下全部 JSON Schema，并自校验 Schema 本身合法；
- 按 ``(artifact_type, schema_version)`` 解析负载 Schema；
- 所有跨 Agent 数据入库前必须通过这里校验，否则抛 ``ARTIFACT_SCHEMA_INVALID``。
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from domain.enums import ArtifactType
from domain.errors import CodePilotError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]

AGENT_CARD_SCHEMA = "agent-card-v1.schema.json"
A2A_TASK_SCHEMA = "a2a-task-v1.schema.json"
A2A_MESSAGE_SCHEMA = "a2a-message-v1.schema.json"
ARTIFACT_ENVELOPE_SCHEMA = "artifact-envelope-v1.schema.json"
STATE_INTENT_SCHEMA = "state-intent-v1.schema.json"
GOLDEN_CASE_SCHEMA = "golden-v1.schema.json"

ARTIFACT_PAYLOAD_SCHEMAS: dict[ArtifactType, str] = {
    ArtifactType.FINDING: "artifact-finding-v1.schema.json",
    ArtifactType.IMPACT_REPORT: "artifact-impact-report-v1.schema.json",
    ArtifactType.REVIEW_COMMENT: "artifact-review-comment-v1.schema.json",
    ArtifactType.PATCH_CANDIDATE: "artifact-patch-candidate-v1.schema.json",
    ArtifactType.PATCH_EVIDENCE: "artifact-patch-evidence-v1.schema.json",
    ArtifactType.VERIFY_EVIDENCE: "artifact-verify-evidence-v1.schema.json",
}


def schema_dir() -> Path:
    override = os.environ.get("CODEPILOT_SCHEMA_DIR")
    return Path(override) if override else REPO_ROOT / "schemas"


def golden_schema_paths() -> list[Path]:
    return [REPO_ROOT / "evals" / GOLDEN_CASE_SCHEMA]


@lru_cache(maxsize=1)
def _load_documents() -> dict[str, dict[str, Any]]:
    documents: dict[str, dict[str, Any]] = {}
    for path in sorted(schema_dir().glob("*.schema.json")) + golden_schema_paths():
        if not path.exists():
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as exc:  # pragma: no cover - 防御性分支
            raise CodePilotError(
                ErrorCode.INTERNAL_ERROR,
                f"JSON Schema 自身非法：{path.name}: {exc.message}",
            ) from exc
        documents[path.name] = document
    if not documents:
        raise CodePilotError(ErrorCode.INTERNAL_ERROR, f"未在 {schema_dir()} 找到任何 JSON Schema")
    return documents


@lru_cache(maxsize=64)
def _validator(schema_name: str) -> Draft202012Validator:
    documents = _load_documents()
    if schema_name not in documents:
        raise CodePilotError(ErrorCode.INTERNAL_ERROR, f"未知 Schema：{schema_name}")
    return Draft202012Validator(documents[schema_name], format_checker=FormatChecker())


def schema_document(schema_name: str) -> dict[str, Any]:
    return _load_documents()[schema_name]


def available_schemas() -> list[str]:
    return sorted(_load_documents())


def validate_document(schema_name: str, document: Any, *, context: str = "") -> None:
    """按 Schema 校验文档，失败抛 ``ARTIFACT_SCHEMA_INVALID``。"""
    errors = sorted(_validator(schema_name).iter_errors(document), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "<root>"
        details: dict[str, Any] = {
            "schema": schema_name,
            "path": location,
            "reason": first.message,
            "error_count": len(errors),
        }
        if first.validator == "required":
            missing = list(re.findall(r"'([^']+)'", first.message))
            if missing:
                details["missing_property"] = missing[0]
                details["path"] = f"{location}/{missing[0]}".replace("<root>/", "")
        raise CodePilotError(
            ErrorCode.ARTIFACT_SCHEMA_INVALID,
            f"{context or schema_name} Schema 校验失败：{details['path']}: {first.message}",
            details=details,
        )


def validate_agent_card(document: Any) -> None:
    validate_document(AGENT_CARD_SCHEMA, document, context="AgentCard")


def validate_a2a_task(document: Any) -> None:
    validate_document(A2A_TASK_SCHEMA, document, context="A2ATask")


def validate_a2a_message(document: Any) -> None:
    validate_document(A2A_MESSAGE_SCHEMA, document, context="A2AMessage")


def validate_artifact_envelope(document: Any) -> None:
    validate_document(ARTIFACT_ENVELOPE_SCHEMA, document, context="ArtifactEnvelope")


def validate_state_intent(document: Any) -> None:
    validate_document(STATE_INTENT_SCHEMA, document, context="StateIntent")


def validate_golden_case(document: Any) -> None:
    validate_document(GOLDEN_CASE_SCHEMA, document, context="GoldenCase")


def validate_artifact_payload(artifact_type: ArtifactType | str, data: Any) -> None:
    """按 artifact_type 校验负载（在 envelope 校验之外的第二层校验）。"""
    try:
        artifact = ArtifactType(artifact_type)
    except ValueError as exc:
        raise CodePilotError(
            ErrorCode.ARTIFACT_SCHEMA_INVALID,
            f"未知 artifact_type：{artifact_type}",
            details={"artifact_type": str(artifact_type)},
        ) from exc
    schema_name = ARTIFACT_PAYLOAD_SCHEMAS.get(artifact)
    if schema_name is None:
        raise CodePilotError(
            ErrorCode.ARTIFACT_SCHEMA_INVALID,
            f"artifact_type {artifact} 没有对应的负载 Schema",
            details={"artifact_type": str(artifact)},
        )
    validate_document(schema_name, data, context=f"{artifact}")


def main() -> int:
    """``python -m a2a.schema_registry``：自校验全部 Schema 并打印清单。"""
    documents = _load_documents()
    for name in sorted(documents):
        Draft202012Validator.check_schema(documents[name])
        print(f"OK  {name}")
    print(f"共 {len(documents)} 个 Schema，全部通过 Draft 2020-12 自校验")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "A2A_MESSAGE_SCHEMA",
    "A2A_TASK_SCHEMA",
    "AGENT_CARD_SCHEMA",
    "ARTIFACT_ENVELOPE_SCHEMA",
    "ARTIFACT_PAYLOAD_SCHEMAS",
    "GOLDEN_CASE_SCHEMA",
    "REPO_ROOT",
    "STATE_INTENT_SCHEMA",
    "available_schemas",
    "schema_dir",
    "schema_document",
    "validate_a2a_message",
    "validate_a2a_task",
    "validate_agent_card",
    "validate_artifact_envelope",
    "validate_artifact_payload",
    "validate_document",
    "validate_golden_case",
    "validate_state_intent",
]
