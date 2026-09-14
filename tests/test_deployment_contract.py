"""部署契约测试：Compose 必须把 A2A 地址真正注入 API 容器，README 命令可真执行。

这些测试是静态检查（解析 YAML / Markdown），不启动容器，可在 CI 中快速运行；
真实启动验证由 `scripts/run_checks.py` 与手工 Compose 流程完成。
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_api_service_declares_a2a_transport_variables(compose) -> None:
    env = compose["services"]["api"]["environment"]
    assert "CODEPILOT_A2A_BASE_URL" in env, "API 容器必须声明 CODEPILOT_A2A_BASE_URL"
    assert "CODEPILOT_A2A_ROUTES" in env, "API 容器必须声明 CODEPILOT_A2A_ROUTES"
    # 默认必须为空：单进程形态下退回 InProcessInvoker
    assert env["CODEPILOT_A2A_BASE_URL"] == "${CODEPILOT_A2A_BASE_URL:-}"
    assert env["CODEPILOT_A2A_ROUTES"] == "${CODEPILOT_A2A_ROUTES:-}"


def test_agent_service_only_runs_under_split_profile(compose) -> None:
    agent = compose["services"]["agent"]
    assert "split" in agent["profiles"], "agent 服务必须只在 split profile 下启动"
    assert agent["ports"] == ["8100:8100"]
    assert "agent_main" in agent["command"], "agent 服务必须运行独立 Agent 服务应用"


def test_agent_service_exposes_only_internal_surface() -> None:
    """拆分服务只挂载 /internal/a2a/* 与 /healthz，不暴露对外 REST。"""
    source = (REPO_ROOT / "apps" / "api" / "agent_main.py").read_text(encoding="utf-8")
    assert "build_internal_router" in source
    assert "/api/v1/reviews" not in source
    assert "ReviewService" not in source


def test_env_example_documents_split_usage() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "CODEPILOT_A2A_BASE_URL=http://agent:8100" in text
    assert "不会自动转发" in text, "必须说明 Compose 不会自动转发未声明的宿主机变量"


def test_agent_service_registers_all_four_agents(compose) -> None:
    """拆分形态的 Agent 服务必须注册全部四类 handler。

    历史缺陷：`build_agent_service` 只注册 review/impact，导致拆分形态下 Fix/Verify 子任务
    报 `AGENT_UNAVAILABLE: Agent handler 未注册`（单进程形态正常，因此很难发现）。
    """
    from agents import default_handlers

    handlers = default_handlers()
    assert set(handlers) == {"review-agent", "impact-agent", "fix-agent", "verify-agent"}

    # 拆分服务与单进程 Coordinator 必须使用同一份注册来源
    agent_source = (REPO_ROOT / "apps" / "api" / "agent_service.py").read_text(encoding="utf-8")
    assert "default_handlers" in agent_source
    coordinator_source = (REPO_ROOT / "agents" / "coordinator" / "coordinator.py").read_text(encoding="utf-8")
    assert "default_handlers" in coordinator_source

    # 四个 agent_id 都必须有对应 Card，且服务只挂载内部接口
    registry_agents = {card.agent_id for card in _load_registry().list_cards()}
    assert registry_agents == set(handlers)


def _load_registry():
    from a2a.registry import AgentRegistry

    return AgentRegistry.load_static()


def test_readme_documents_split_mode_honestly() -> None:
    text = README.read_text(encoding="utf-8")
    assert "docker compose --profile split up -d" in text
    assert "不是一个" in text or "统一" in text, "必须说明拆分形态是统一 Agent 服务，而非四个容器"


def test_readme_commands_are_consistent_with_compose_services() -> None:
    text = README.read_text(encoding="utf-8")
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = set(compose["services"])
    for match in re.findall(r"docker compose(?: --profile \w+)? up -d ([a-z0-9 ]+)", text):
        for name in match.split():
            assert name in services, f"README 提到了不存在的服务：{name}"
