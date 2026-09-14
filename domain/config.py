"""项目配置加载与校验（SRS FR-022、examples/.codepilot.yaml）。

优先级：CLI > 项目配置 > 目录配置 > 全局配置（examples/.codepilot.yaml 中声明的顺序）。
实现语义：
- 标量与嵌套对象逐层深合并，高优先级覆盖低优先级；
- ``rules.enabled`` 为列表语义，最高优先级中出现的该键整体生效（避免"低优先级偷偷开规则"）；
- ``rules.severity_overrides`` 按规则 ID 逐键合并，高优先级覆盖。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import (
    ARTIFACT_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    ContextPolicy,
    FixCategory,
    RunMode,
    Severity,
)

CONFIG_PRECEDENCE: tuple[str, ...] = ("cli", "project", "directory", "global")
DEFAULT_PROJECT_CONFIG_NAME = ".codepilot.yaml"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectSection(_Base):
    language: str = "python"
    base_branch: str = "synthetic-main"
    context_policy: ContextPolicy = ContextPolicy.FUNCTION

    @field_validator("language")
    @classmethod
    def _python_only(cls, value: str) -> str:
        if value != "python":
            raise ValueError("MVP 只支持 python（SRS §2.2 不做多语言）")
        return value

    @field_validator("base_branch")
    @classmethod
    def _never_protected_branch(cls, value: str) -> str:
        if value in {"main", "master", "develop"}:
            raise ValueError("base_branch 不得直接指向受保护分支（宪法第八条）")
        return value


class A2ASection(_Base):
    protocol_version: str = PROTOCOL_VERSION
    coordinator_id: str = "coordinator"
    child_task_timeout_seconds: int = Field(default=45, ge=1, le=300)
    max_parallel_children: int = Field(default=2, ge=1, le=8)
    retry_limit: int = Field(default=1, ge=0, le=1)
    allow_agents: list[str] = Field(
        default_factory=lambda: ["review-agent", "impact-agent", "fix-agent", "verify-agent"]
    )
    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION
    fallback_mode: RunMode = RunMode.SINGLE
    evaluation_compare_modes: bool = True

    @field_validator("protocol_version")
    @classmethod
    def _supported_protocol(cls, value: str) -> str:
        if value != PROTOCOL_VERSION:
            raise ValueError(f"MVP 只支持协议版本 {PROTOCOL_VERSION}（FR-100）")
        return value

    @field_validator("artifact_schema_version")
    @classmethod
    def _supported_schema(cls, value: str) -> str:
        if value != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"MVP 只支持 Artifact schema_version {ARTIFACT_SCHEMA_VERSION}")
        return value


class RulesSection(_Base):
    config_precedence: list[str] = Field(default_factory=lambda: list(CONFIG_PRECEDENCE))
    enabled: list[str] = Field(default_factory=list)
    severity_overrides: dict[str, Severity] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_precedence(self) -> RulesSection:
        unknown = [item for item in self.config_precedence if item not in CONFIG_PRECEDENCE]
        if unknown:
            raise ValueError(f"未知的 config_precedence 层级：{unknown}")
        return self


class BudgetSection(_Base):
    tokens: int = Field(default=12_000, ge=0)
    tool_calls: int = Field(default=30, ge=0)


class AgentSection(_Base):
    max_steps: int = Field(default=15, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=5)
    budget: BudgetSection = Field(default_factory=BudgetSection)
    loop_detection_threshold: int = Field(default=3, ge=2, le=10)


class FixSection(_Base):
    allowed_categories: list[FixCategory] = Field(
        default_factory=lambda: [
            FixCategory.HARDCODED_SECRET,
            FixCategory.SHELL_TRUE,
            FixCategory.SQL_PARAMETERIZATION,
        ]
    )
    wide_impact_file_threshold: int = Field(default=5, ge=1)
    #: SQL 参数化占位符：只改写声明支持该占位符的 DB API（SRS §6.4）。
    sql_placeholder: str = Field(default="%s", min_length=1, max_length=8)

    @field_validator("allowed_categories")
    @classmethod
    def _only_three(cls, value: list[FixCategory]) -> list[FixCategory]:
        allowed = {
            FixCategory.HARDCODED_SECRET,
            FixCategory.SHELL_TRUE,
            FixCategory.SQL_PARAMETERIZATION,
        }
        extra = set(value) - allowed
        if extra:
            raise ValueError(f"MVP 只支持 3 类自动修复，非法类别：{sorted(str(x) for x in extra)}")
        return value


class SandboxSection(_Base):
    """SRS FR-051~FR-058：沙箱安全参数。这些值不允许放宽到不安全取值。"""

    network: str = "none"
    user: str = "sandbox"
    read_only_rootfs: bool = True
    tmpfs_mb: int = Field(default=64, ge=8, le=512)
    cpu: float = Field(default=1.0, gt=0, le=4)
    memory_mb: int = Field(default=512, ge=128, le=4096)
    disk_mb: int = Field(default=100, ge=16, le=2048)
    timeout_seconds: int = Field(default=60, ge=1, le=300)
    drop_capabilities: str = "all"
    no_new_privileges: bool = True
    image: str = "codepilot-sandbox:local"
    pull_policy: str = "never"

    @field_validator("network")
    @classmethod
    def _no_network(cls, value: str) -> str:
        if value != "none":
            raise ValueError("沙箱必须 network=none（宪法第八条 / FR-051）")
        return value

    @field_validator("read_only_rootfs")
    @classmethod
    def _read_only(cls, value: bool) -> bool:
        if not value:
            raise ValueError("沙箱根文件系统必须只读（FR-056）")
        return value

    @field_validator("drop_capabilities")
    @classmethod
    def _drop_all(cls, value: str) -> str:
        if value != "all":
            raise ValueError("沙箱必须丢弃全部 capabilities（FR-058）")
        return value

    @field_validator("no_new_privileges")
    @classmethod
    def _no_new_privileges(cls, value: bool) -> bool:
        if not value:
            raise ValueError("沙箱必须启用 no-new-privileges（FR-057）")
        return value

    @field_validator("user")
    @classmethod
    def _non_root(cls, value: str) -> str:
        if value in {"root", "0", "0:0"}:
            raise ValueError("沙箱必须非 root 运行（FR-057）")
        return value


class EvaluationSection(_Base):
    dataset: str = "golden-v1"
    runs_per_case: int = Field(default=3, ge=1, le=5)
    modes: list[RunMode] = Field(
        default_factory=lambda: [RunMode.SINGLE, RunMode.A2A, RunMode.OFFLINE]
    )


class CodePilotConfig(_Base):
    version: int = 1
    project: ProjectSection = Field(default_factory=ProjectSection)
    mode: RunMode = RunMode.A2A
    a2a: A2ASection = Field(default_factory=A2ASection)
    rules: RulesSection = Field(default_factory=RulesSection)
    agent: AgentSection = Field(default_factory=AgentSection)
    fix: FixSection = Field(default_factory=FixSection)
    sandbox: SandboxSection = Field(default_factory=SandboxSection)
    evaluation: EvaluationSection = Field(default_factory=EvaluationSection)

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("未知配置版本")
        return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def merge_layers(*, cli: dict[str, Any] | None = None, project: dict[str, Any] | None = None,
                 directory: dict[str, Any] | None = None, global_: dict[str, Any] | None = None) -> dict[str, Any]:
    """按 cli > project > directory > global 合并配置字典。"""
    layers = {
        "global": global_ or {},
        "directory": directory or {},
        "project": project or {},
        "cli": cli or {},
    }
    merged: dict[str, Any] = {}
    for name in reversed(CONFIG_PRECEDENCE):
        merged = _deep_merge(merged, layers[name])

    # severity_overrides 逐键合并，高优先级覆盖低优先级。
    overrides: dict[str, Any] = {}
    for name in reversed(CONFIG_PRECEDENCE):
        layer_rules = layers[name].get("rules") or {}
        if isinstance(layer_rules.get("severity_overrides"), dict):
            overrides.update(layer_rules["severity_overrides"])
    if overrides:
        merged.setdefault("rules", {})["severity_overrides"] = overrides
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 YAML 映射：{path}")
    return data


def load_config(
    *,
    cli_overrides: dict[str, Any] | None = None,
    project_path: str | Path | None = None,
    directory_path: str | Path | None = None,
    global_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> CodePilotConfig:
    """加载并校验 CodePilot 配置。

    ``CODEPILOT_MODE`` 环境变量作为 CLI 层最高优先级覆盖 ``mode``。
    """
    env = env if env is not None else dict(os.environ)
    cli = dict(cli_overrides or {})
    if env.get("CODEPILOT_MODE"):
        cli["mode"] = env["CODEPILOT_MODE"]

    merged = merge_layers(
        cli=cli,
        project=_read_yaml(Path(project_path)) if project_path else {},
        directory=_read_yaml(Path(directory_path)) if directory_path else {},
        global_=_read_yaml(Path(global_path)) if global_path else {},
    )
    return CodePilotConfig.model_validate(merged)


def default_config_paths(root: str | Path) -> dict[str, Path]:
    root_path = Path(root)
    return {
        "project": root_path / DEFAULT_PROJECT_CONFIG_NAME,
        "directory": root_path / "examples" / DEFAULT_PROJECT_CONFIG_NAME,
        "global": Path.home() / DEFAULT_PROJECT_CONFIG_NAME,
    }


__all__ = [
    "CONFIG_PRECEDENCE",
    "A2ASection",
    "AgentSection",
    "BudgetSection",
    "CodePilotConfig",
    "EvaluationSection",
    "FixSection",
    "ProjectSection",
    "RulesSection",
    "SandboxSection",
    "default_config_paths",
    "load_config",
    "merge_layers",
]
