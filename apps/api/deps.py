"""API 依赖：鉴权、会话、应用容器与错误映射（docs/03 §1）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session, sessionmaker

from a2a.registry import AgentRegistry
from apps.api.auth import AuthService
from agents.coordinator.coordinator import Coordinator
from domain.config import CodePilotConfig, load_config
from domain.enums import ActorRole
from domain.errors import CodePilotError, ErrorCode
from repositories.content_store import ContentStore
from repositories.database import database_url, session_factory
from repositories.ddl import install_append_only_guards
from repositories.models import Base
from repositories.store import AgentCardStore
from sandbox import default_executor
from sandbox.base import SandboxExecutor, SandboxLimits
from tools import build_default_registry
from tools.registry import ToolRegistry

CONFIG_FILE = "examples/.codepilot.yaml"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    role: ActorRole
    username: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role is ActorRole.ADMIN


def get_actor(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
    x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
) -> Actor:
    """优先使用登录令牌；保留旧身份头以兼容内部 Agent 与自动化测试。"""
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise CodePilotError(ErrorCode.UNAUTHORIZED, "登录令牌格式不正确，请重新登录。")
        user = AuthService(get_container(request).session_factory).authenticate(token.strip())
        try:
            role = ActorRole(user.role)
        except ValueError as exc:
            raise CodePilotError(ErrorCode.PERMISSION_DENIED, "账户角色配置无效。") from exc
        return Actor(actor_id=user.employee_id, role=role, username=user.username)
    if not x_actor_id or not x_actor_role:
        raise CodePilotError(
            ErrorCode.INVALID_INPUT,
            "请先登录后再使用审查工作台。",
            details={"required_headers": ["X-Actor-Id", "X-Actor-Role"]},
        )
    try:
        role = ActorRole(x_actor_role.strip().lower())
    except ValueError as exc:
        raise CodePilotError(
            ErrorCode.PERMISSION_DENIED,
            f"未知角色：{x_actor_role}",
            details={"allowed": [str(item) for item in ActorRole]},
        ) from exc
    return Actor(actor_id=x_actor_id.strip(), role=role)


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """写请求必须带幂等键（docs/03 §1）。"""
    if not idempotency_key or len(idempotency_key) < 8:
        raise CodePilotError(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            "写请求必须携带长度 >= 8 的 Idempotency-Key 请求头",
        )
    return idempotency_key


def require_roles(*roles: ActorRole):
    allowed = set(roles)

    def dependency(actor: Annotated[Actor, Depends(get_actor)]) -> Actor:
        if actor.role not in allowed:
            raise CodePilotError(
                ErrorCode.FORBIDDEN,
                f"角色 {actor.role} 无权访问该接口",
                details={"allowed_roles": sorted(str(item) for item in allowed)},
            )
        return actor

    return dependency


@dataclass
class AppContainer:
    """进程内共享对象：配置、会话工厂、Agent Registry、沙箱与 Coordinator。"""

    config: CodePilotConfig
    session_factory: sessionmaker[Session]
    database_url: str
    registry: AgentRegistry
    tool_registry: ToolRegistry
    content_store: ContentStore
    coordinator: Coordinator
    sandbox: SandboxExecutor | None = None

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def sync_agent_cards(self) -> None:
        with self.session() as session:
            store = AgentCardStore(session)
            for card in self.registry.list_cards():
                store.sync(card, active=True)


def build_container(
    *,
    config: CodePilotConfig | None = None,
    url: str | None = None,
    auto_create: bool = False,
    sandbox: SandboxExecutor | None = None,
) -> AppContainer:
    target_url = url or database_url()
    resolved = config or load_config(project_path=CONFIG_FILE)
    factory = session_factory(target_url)

    if auto_create:
        engine = factory.kw["bind"]
        Base.metadata.create_all(engine)
        install_append_only_guards(engine)

    registry = AgentRegistry.load_static(allow_agents=resolved.a2a.allow_agents)
    tool_registry = build_default_registry()
    content_store = ContentStore()
    sandbox_executor = sandbox if sandbox is not None else default_executor()
    coordinator = Coordinator(
        config=resolved,
        session_factory=factory,
        registry=registry,
        tool_registry=tool_registry,
        content_store=content_store,
        sandbox=sandbox_executor,
        sandbox_limits=SandboxLimits.from_config(resolved.sandbox),
    )
    container = AppContainer(
        config=resolved,
        session_factory=factory,
        database_url=target_url,
        registry=registry,
        tool_registry=tool_registry,
        content_store=content_store,
        sandbox=sandbox_executor,
        coordinator=coordinator,
    )
    container.sync_agent_cards()
    return container


def attach_http_invoker(
    container: AppContainer,
    *,
    base_url: str | None = None,
    routes: dict[str, str] | None = None,
) -> str:
    """按配置把 Coordinator 的 Invoker 切换为 HTTP A2A（阶段五）。

    返回实际使用的传输名：``http`` 或 ``inprocess``（未配置端点时安全降级）。
    """
    from a2a.http_invoker import build_invoker

    invoker = build_invoker(
        registry=container.registry,
        mode=str(container.config.mode),
        base_url=base_url,
        routes=routes,
    )
    if invoker is None:
        return container.coordinator.invoker.mode
    container.coordinator.invoker = invoker
    return invoker.mode


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - 仅在未初始化时触发
        raise CodePilotError(ErrorCode.INTERNAL_ERROR, "应用容器尚未初始化")
    return container


__all__ = [
    "CONFIG_FILE",
    "Actor",
    "AppContainer",
    "attach_http_invoker",
    "build_agent_service_for",
    "build_container",
    "get_actor",
    "get_container",
    "get_idempotency_key",
    "require_roles",
]


def build_agent_service_for(container: AppContainer):
    """在 Coordinator 进程内同时挂载 Agent 侧服务（MVP 单进程部署形态）。

    生产/compose 形态可以只启动 ``create_agent_app`` 的独立进程；
    两种形态共用同一套协议、校验与审计。
    """
    from apps.api.agent_service import build_agent_service

    return build_agent_service(
        config=container.config,
        session_factory=container.session_factory,
        registry=container.registry,
        tool_registry=container.tool_registry,
        content_store=container.content_store,
        sandbox=container.sandbox,
        sandbox_limits=SandboxLimits.from_config(container.config.sandbox),
        transport="http",
    )
