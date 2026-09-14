"""独立的 Agent 服务应用（compose 拆分形态：coordinator 与 agent 分开部署）。

与 ``apps.api.main`` 共用同一套协议、校验、审计与沙箱实现，只是不挂载对外 REST 接口、
不创建父任务、不持有 Coordinator。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from a2a.registry import AgentRegistry
from apps.api.agent_app import build_internal_router
from apps.api.agent_service import TaskScheduler, build_agent_service
from domain.config import load_config
from domain.errors import CodePilotError, ErrorCode
from repositories.content_store import ContentStore
from repositories.database import database_url, session_factory
from repositories.ddl import install_append_only_guards
from repositories.models import Base
from repositories.store import AgentCardStore
from sandbox import default_executor
from sandbox.base import SandboxLimits
from tools import build_default_registry

CONFIG_FILE = os.environ.get("CODEPILOT_CONFIG", "examples/.codepilot.yaml")


def create_agent_app(
    *,
    database_url_override: str | None = None,
    auto_create: bool = False,
) -> FastAPI:
    """构造 Agent 侧服务：只暴露 ``/internal/a2a/*`` 与健康检查。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = load_config(project_path=CONFIG_FILE, env=dict(os.environ))
        url = database_url_override or database_url()
        factory = session_factory(url)
        if auto_create:
            engine = factory.kw["bind"]
            Base.metadata.create_all(engine)
            install_append_only_guards(engine)

        registry = AgentRegistry.load_static(allow_agents=config.a2a.allow_agents)
        tool_registry = build_default_registry()
        content_store = ContentStore()
        sandbox = default_executor()

        service = build_agent_service(
            config=config,
            session_factory=factory,
            registry=registry,
            tool_registry=tool_registry,
            content_store=content_store,
            sandbox=sandbox,
            sandbox_limits=SandboxLimits.from_config(config.sandbox),
            transport="http",
        )
        with factory() as session:
            store = AgentCardStore(session)
            for card in registry.list_cards():
                store.sync(card, active=True)
            session.commit()

        app.state.agent_service = service
        app.state.agent_scheduler = TaskScheduler(service)
        app.state.config = config
        yield

    app = FastAPI(
        title="CodePilot A2A Agent Service",
        version="0.1.0",
        description="CodePilot 内部 A2A Agent 服务（仅接受 coordinator/admin 调用）",
        lifespan=lifespan,
    )

    @app.exception_handler(CodePilotError)
    async def handle_error(_request, exc: CodePilotError) -> JSONResponse:  # noqa: ANN001
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def handle_unexpected(_request, exc: Exception) -> JSONResponse:  # noqa: ANN001
        error = CodePilotError(ErrorCode.INTERNAL_ERROR, str(exc))
        return JSONResponse(status_code=error.http_status, content=error.to_payload())

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(build_internal_router())
    return app


app = create_agent_app()


__all__ = ["app", "create_agent_app"]
