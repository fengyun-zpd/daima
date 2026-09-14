"""配置加载与优先级测试（FR-022、examples/.codepilot.yaml）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from domain.config import CodePilotConfig, load_config, merge_layers
from domain.enums import ContextPolicy, FixCategory, RunMode, Severity

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "examples" / ".codepilot.yaml"


def test_example_config_is_valid() -> None:
    config = load_config(project_path=EXAMPLE_CONFIG)
    assert config.version == 1
    assert config.mode is RunMode.A2A
    assert config.project.context_policy is ContextPolicy.FUNCTION
    assert config.a2a.protocol_version == "0.1"
    assert config.a2a.child_task_timeout_seconds == 45
    assert config.a2a.fallback_mode is RunMode.SINGLE
    assert config.agent.max_steps == 15
    assert config.agent.budget.tokens == 12000
    assert config.agent.loop_detection_threshold == 3
    assert config.sandbox.network == "none"
    assert config.sandbox.memory_mb == 512
    assert config.evaluation.runs_per_case == 3
    assert set(config.evaluation.modes) == {RunMode.SINGLE, RunMode.A2A, RunMode.OFFLINE}


def test_cli_overrides_project_overrides_global(tmp_path) -> None:
    global_file = tmp_path / "global.yaml"
    global_file.write_text("mode: single\nagent:\n  max_steps: 5\n", encoding="utf-8")
    project_file = tmp_path / "project.yaml"
    project_file.write_text("mode: a2a\nagent:\n  max_steps: 9\n", encoding="utf-8")

    config = load_config(
        global_path=global_file,
        project_path=project_file,
        cli_overrides={"agent": {"max_steps": 12}},
    )
    assert config.mode is RunMode.A2A
    assert config.agent.max_steps == 12


def test_env_mode_overrides_everything() -> None:
    config = load_config(project_path=EXAMPLE_CONFIG, env={"CODEPILOT_MODE": "offline"})
    assert config.mode is RunMode.OFFLINE


def test_severity_overrides_merge_by_key() -> None:
    merged = merge_layers(
        global_={"rules": {"severity_overrides": {"R001_SQL_CONCAT": "warning", "R003_SHELL_TRUE": "info"}}},
        project={"rules": {"severity_overrides": {"R001_SQL_CONCAT": "critical"}}},
    )
    overrides = merged["rules"]["severity_overrides"]
    assert overrides["R001_SQL_CONCAT"] == "critical"
    assert overrides["R003_SHELL_TRUE"] == "info"


def test_enabled_rules_use_highest_precedence_layer() -> None:
    merged = merge_layers(
        global_={"rules": {"enabled": ["R001_SQL_CONCAT", "R002_HARDCODED_SECRET"]}},
        directory={"rules": {"enabled": ["R003_SHELL_TRUE"]}},
        project={"rules": {"enabled": ["R002_HARDCODED_SECRET"]}},
    )
    assert merged["rules"]["enabled"] == ["R002_HARDCODED_SECRET"]


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"unknown_section": {}})


def test_unsafe_sandbox_settings_rejected() -> None:
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"sandbox": {"network": "bridge"}})
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"sandbox": {"read_only_rootfs": False}})
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"sandbox": {"user": "root"}})
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"sandbox": {"drop_capabilities": "none"}})
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"sandbox": {"no_new_privileges": False}})


def test_protected_base_branch_rejected() -> None:
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"project": {"base_branch": "main"}})


def test_only_three_fix_categories_allowed() -> None:
    config = CodePilotConfig.model_validate({"fix": {"allowed_categories": ["hardcoded_secret"]}})
    assert config.fix.allowed_categories == [FixCategory.HARDCODED_SECRET]
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"fix": {"allowed_categories": ["rename_variable"]}})


def test_unsupported_protocol_version_rejected() -> None:
    with pytest.raises(ValidationError):
        CodePilotConfig.model_validate({"a2a": {"protocol_version": "0.2"}})


def test_severity_override_values_are_validated() -> None:
    config = CodePilotConfig.model_validate(
        {"rules": {"severity_overrides": {"R001_SQL_CONCAT": "critical"}}}
    )
    assert config.rules.severity_overrides["R001_SQL_CONCAT"] is Severity.CRITICAL
