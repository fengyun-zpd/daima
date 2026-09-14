"""FastAPI 应用（SRS §8、docs/03 §2）。

阶段三暴露父任务闭环所需接口：
- ``POST /api/v1/reviews``：创建审查任务并后台编排；
- ``GET /api/v1/reviews/{id}``：父任务、子任务、Artifact 与意见；
- ``GET /api/v1/reviews/{id}/events``：SSE 事件流；
- ``GET /api/v1/reviews/{id}/comments``：审查意见；
- ``GET /api/v1/audit``：admin 查询追加式审计；
- ``GET /api/v1/agents``、``/agents/{agent_id}/card``：Agent Card；
- ``GET /healthz``、``GET /readyz``：健康检查。
Fix / Verify / 审批 / 评测接口在阶段六、七接入。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from apps.api.agent_service import TaskScheduler
from apps.api.auth import AuthService
from apps.api.deps import (
    Actor,
    AppContainer,
    attach_http_invoker,
    build_agent_service_for,
    build_container,
    get_container,
    get_actor,
    get_idempotency_key,
    require_roles,
)
from apps.api.events import stream_events
from apps.api.idempotency import execute_idempotent
from apps.api.schemas import (
    AgentCardResponse,
    ApprovalRequest,
    AuditEventResponse,
    CreatedReviewResponse,
    CreateReviewRequest,
    ErrorResponse,
    EvalRunRequest,
    MergeRequest,
    PatchResponse,
    ReviewDetailResponse,
    ReviewTaskResponse,
    AuthResponse,
    LoginRequest,
    RegisterRequest,
    UserResponse,
)
from apps.api.serializers import audit_response, comment_response, review_detail, task_response
from apps.api.service import ReviewService
from domain.enums import ActorRole
from domain.errors import CodePilotError, ErrorCode
from repositories.audit import list_events
from repositories.models import A2AAgent
from repositories.records import CommentStore
from repositories.recovery import recovery_summary
from repositories.store import ReviewTaskStore

logger = logging.getLogger("codepilot.api")


def _configure_logging() -> None:
    """确保应用自身的 INFO 级启动日志可见（uvicorn 只配置自己的 logger）。"""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=os.environ.get("CODEPILOT_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s %(message)s",
    )


def create_app(
    *,
    container: AppContainer | None = None,
    database_url: str | None = None,
    auto_create: bool = False,
    recover_on_start: bool | None = None,
    schedule_on_create: bool = True,
    include_internal: bool = True,
    a2a_base_url: str | None = None,
    a2a_routes: dict[str, str] | None = None,
) -> FastAPI:
    """构造 FastAPI 应用；测试可注入自定义容器。

    ``schedule_on_create=False`` 时创建接口只落库不调度，由调用方显式执行编排
    （确定性测试与故障注入需要该开关）。
    ``include_internal=True`` 时同时挂载 ``/internal/a2a/*``（MVP 单进程部署形态），
    独立 Agent 服务见 ``create_agent_app``。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _configure_logging()
        resolved = container or build_container(url=database_url, auto_create=auto_create)
        app.state.container = resolved
        app.state.service = ReviewService(resolved)
        app.state.auth_service = AuthService(resolved.session_factory)
        app.state.schedule_on_create = schedule_on_create
        if include_internal:
            agent_service = build_agent_service_for(resolved)
            app.state.agent_service = agent_service
            app.state.agent_scheduler = TaskScheduler(agent_service)
        app.state.transport = attach_http_invoker(resolved, base_url=a2a_base_url, routes=a2a_routes)
        # 启动即声明实际传输层：拆分形态验收（README §1.1）依赖该日志与 /readyz.transport。
        logger.info(
            "A2A transport=%s mode=%s database=%s",
            app.state.transport,
            resolved.config.mode,
            resolved.database_url.split("@")[-1],
        )
        should_recover = (
            os.environ.get("CODEPILOT_RECOVER_ON_START", "1") == "1"
            if recover_on_start is None
            else recover_on_start
        )
        if should_recover:
            import asyncio

            # 恢复不阻塞启动；已完成子任务不会重放（FR-097）。
            app.state.recovery_task = asyncio.get_running_loop().create_task(
                asyncio.to_thread(resolved.coordinator.recover)
            )
        yield
        task = getattr(app.state, "recovery_task", None)
        if task is not None and not task.done():
            task.cancel()

    app = FastAPI(
        title="CodePilot A2A",
        version="0.1.0",
        description="受控的多 Agent Python 代码审查闭环（项目三 MVP）",
        lifespan=lifespan,
    )

    if include_internal:
        # 内部 A2A 接口在请求时从 app.state 取 AgentService / 调度器，
        # 因此可以在 lifespan 之前注册路由（测试与生产共用同一份实现）。
        from apps.api.agent_app import build_internal_router

        app.include_router(build_internal_router())

    # ---- 错误处理 ---------------------------------------------------------------
    @app.exception_handler(CodePilotError)
    async def handle_error(_request: Request, exc: CodePilotError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        error = CodePilotError(ErrorCode.INTERNAL_ERROR, str(exc))
        return JSONResponse(status_code=error.http_status, content=error.to_payload())

    def service(request: Request) -> ReviewService:
        return request.app.state.service

    # ---- 账户登录 ---------------------------------------------------------------
    @app.post("/api/v1/auth/register", response_model=AuthResponse, tags=["auth"])
    async def register_account(payload: RegisterRequest, request: Request) -> AuthResponse:
        result = request.app.state.auth_service.register(
            employee_id=payload.employee_id,
            username=payload.username,
            password=payload.password,
        )
        return AuthResponse.model_validate(result)

    @app.post("/api/v1/auth/login", response_model=AuthResponse, tags=["auth"])
    async def login_account(payload: LoginRequest, request: Request) -> AuthResponse:
        result = request.app.state.auth_service.login(account=payload.account, password=payload.password)
        return AuthResponse.model_validate(result)

    @app.get("/api/v1/auth/me", response_model=UserResponse, tags=["auth"])
    async def current_account(actor: Annotated[Actor, Depends(get_actor)]) -> UserResponse:
        return UserResponse(id="", employee_id=actor.actor_id, username=actor.username or actor.actor_id, role=str(actor.role))

    @app.post("/api/v1/auth/logout", status_code=204, tags=["auth"])
    async def logout_account(request: Request) -> None:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token.strip():
            request.app.state.auth_service.logout(token.strip())

    # ---- 健康检查 ---------------------------------------------------------------
    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> dict[str, Any]:
        resolved = get_container(request)
        with resolved.session() as session:
            summary = recovery_summary(session)
        return {
            "status": "ready",
            "database": resolved.database_url.split("@")[-1],
            "agents": resolved.registry.agent_ids(),
            "tools": resolved.tool_registry.names(),
            "recoverable_tasks": summary["recoverable_tasks"],
            # 传输层自证：single/docker 单进程为 inprocess，拆分形态为 http。
            # 拆分形态验收依赖该字段（见 README §1.1 与 docs/03 §2.1）。
            "transport": getattr(request.app.state, "transport", "inprocess"),
        }

    # ---- 审查任务 ---------------------------------------------------------------
    @app.post(
        "/api/v1/reviews",
        response_model=CreatedReviewResponse,
        responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
        tags=["reviews"],
    )
    async def create_review(
        payload: CreateReviewRequest,
        request: Request,
        actor: Annotated[Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.ADMIN))],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> CreatedReviewResponse:
        review_service: ReviewService = request.app.state.service
        response = review_service.create(payload, actor, idempotency_key=idempotency_key)
        if request.app.state.schedule_on_create:
            review_service.schedule(response.task.id)
        return response

    @app.get("/api/v1/reviews", response_model=list[ReviewTaskResponse], tags=["reviews"])
    async def list_reviews(
        request: Request,
        actor: Annotated[
            Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.APPROVER, ActorRole.ADMIN))
        ],
        limit: int = Query(default=100, ge=1, le=200),
    ) -> list[ReviewTaskResponse]:
        """返回可回看的审查记录；普通提交者只看到自己创建的任务。"""
        with get_container(request).session() as session:
            rows = ReviewTaskStore(session).list_tasks(
                actor_id=actor.actor_id if actor.role is ActorRole.DEVELOPER else None,
                limit=limit,
            )
            return [task_response(row) for row in rows]

    @app.get("/api/v1/reviews/{task_id}", response_model=ReviewDetailResponse, tags=["reviews"])
    async def get_review(
        task_id: str,
        request: Request,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.APPROVER, ActorRole.ADMIN))
        ],
    ) -> ReviewDetailResponse:
        with get_container(request).session() as session:
            return review_detail(session, task_id)

    @app.get("/api/v1/reviews/{task_id}/comments", tags=["reviews"])
    async def get_comments(
        task_id: str,
        request: Request,
        include_suppressed: bool = False,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.APPROVER, ActorRole.ADMIN))
        ] = None,
    ) -> list[dict[str, Any]]:
        resolved = get_container(request)
        with resolved.session() as session:
            ReviewTaskStore(session).get(task_id)
            rows = CommentStore(session).list_by_task(task_id, include_suppressed=include_suppressed)
            return [comment_response(row).model_dump(mode="json") for row in rows]

    @app.get("/api/v1/reviews/{task_id}/events", tags=["reviews"])
    async def review_events(
        task_id: str,
        request: Request,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.APPROVER, ActorRole.ADMIN))
        ] = None,
    ) -> StreamingResponse:
        resolved = get_container(request)
        with resolved.session() as session:
            ReviewTaskStore(session).get(task_id)
        return StreamingResponse(
            stream_events(
                session_factory=resolved.session_factory,
                task_id=task_id,
                last_event_id=last_event_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/reviews/{task_id}/resume", response_model=ReviewTaskResponse, tags=["reviews"])
    async def resume_review(
        task_id: str,
        payload: dict[str, Any],
        request: Request,
        actor: Annotated[Actor, Depends(require_roles(ActorRole.ADMIN))],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> ReviewTaskResponse:
        """人工恢复：仅 admin，仅从 NEEDS_HUMAN 出发，绝不进入 MERGED（docs/02 更新项）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原结果；同键不同请求 → ``IDEMPOTENCY_CONFLICT``。
        """
        from domain.enums import ParentTaskStatus

        target = str(payload.get("target_status", ""))
        reason = str(payload.get("reason", "")).strip()
        expected_version = int(payload.get("expected_version", 0))
        if not reason or expected_version < 1:
            raise CodePilotError(ErrorCode.INVALID_INPUT, "必须提供 reason 与 expected_version")
        try:
            target_status = ParentTaskStatus(target)
        except ValueError as exc:
            raise CodePilotError(ErrorCode.INVALID_INPUT, f"未知目标状态：{target}") from exc

        resolved = get_container(request)

        def _work() -> dict[str, Any]:
            with resolved.session() as session:
                result = ReviewTaskStore(session).human_resume(
                    task_id,
                    target_status=target_status,
                    expected_version=expected_version,
                    actor_id=actor.actor_id,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
                return task_response(result.row).model_dump(mode="json")

        stored = execute_idempotent(
            resolved.session_factory,
            actor_id=actor.actor_id,
            actor_role=str(actor.role),
            command_type="human_resume",
            aggregate_ref=task_id,
            idempotency_key=idempotency_key,
            request={
                "task_id": task_id,
                "target_status": str(target_status),
                "expected_version": expected_version,
                "reason": reason,
            },
            work=_work,
        )
        stored.pop("idempotent_replay", None)
        return ReviewTaskResponse.model_validate(stored)

    # ---- Fix / Verify（阶段六） ---------------------------------------------------
    @app.post("/api/v1/reviews/{task_id}/fixes", tags=["fixes"])
    async def create_fix(
        task_id: str,
        request: Request,
        actor: Annotated[Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.ADMIN))],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> dict[str, Any]:
        """触发 Fix/Verify 阶段（开发者选定可修复意见后调用）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原响应，不重复调度，也不产生第二个补丁。
        """
        review_service: ReviewService = request.app.state.service
        return review_service.trigger_fix(task_id, actor, idempotency_key=idempotency_key)

    @app.get("/api/v1/fixes/{patch_id}", response_model=PatchResponse, tags=["fixes"])
    async def get_patch(
        patch_id: str,
        request: Request,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.APPROVER, ActorRole.ADMIN))
        ],
    ) -> PatchResponse:
        return request.app.state.service.get_patch(patch_id)

    @app.get("/api/v1/reviews/{task_id}/patches", response_model=list[PatchResponse], tags=["fixes"])
    async def list_patches(
        task_id: str,
        request: Request,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.DEVELOPER, ActorRole.APPROVER, ActorRole.ADMIN))
        ],
    ) -> list[PatchResponse]:
        return request.app.state.service.list_patches(task_id)

    # ---- 审批与合并（阶段七） ------------------------------------------------------
    @app.post("/api/v1/fixes/{patch_id}/approval", tags=["approval"])
    async def decide_patch(
        patch_id: str,
        payload: ApprovalRequest,
        request: Request,
        actor: Annotated[Actor, Depends(require_roles(ActorRole.APPROVER))],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> dict[str, Any]:
        """approver 批准或拒绝补丁（决定绑定 patch_version，拒绝必须填写原因）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原审批结果，不产生第二条审批记录。
        """
        return request.app.state.service.decide(
            patch_id, payload, actor, idempotency_key=idempotency_key
        )

    @app.post("/api/v1/fixes/{patch_id}/merge", tags=["approval"])
    async def merge_patch(
        patch_id: str,
        payload: MergeRequest,
        request: Request,
        actor: Annotated[Actor, Depends(require_roles(ActorRole.APPROVER, ActorRole.ADMIN))],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> dict[str, Any]:
        """审批通过后把补丁写入任务分支；无审批记录一律拒绝（FR-070）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原分支与 commit，不重复写入。
        """
        return request.app.state.service.merge(
            patch_id, payload, actor, idempotency_key=idempotency_key
        )

    # ---- A2A 子任务（内部接口，见 docs/05 §5） -------------------------------------
    # 对外 API 只暴露父任务视图；A2A Task 的创建/查询/SSE/取消都在 /internal/a2a/*，
    # 且只允许 coordinator/admin 调用。

    # ---- Agent Card -------------------------------------------------------------
    @app.get("/api/v1/agents", response_model=list[AgentCardResponse], tags=["agents"])
    async def list_agents(
        request: Request,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.COORDINATOR, ActorRole.ADMIN))
        ] = None,
    ) -> list[AgentCardResponse]:
        resolved = get_container(request)
        with resolved.session() as session:
            rows = session.execute(select(A2AAgent).order_by(A2AAgent.agent_id)).scalars().all()
            return [
                AgentCardResponse(
                    agent_id=row.agent_id,
                    card_version=row.card_version,
                    protocol_versions=list(row.protocol_versions or []),
                    capabilities=list(row.capabilities or []),
                    input_schema=row.input_schema,
                    output_schemas=list(row.output_schemas or []),
                    endpoint=row.endpoint,
                    limits=dict(row.limits or {}),
                    status=row.status,
                )
                for row in rows
            ]

    @app.get("/api/v1/agents/{agent_id}/card", response_model=AgentCardResponse, tags=["agents"])
    async def get_agent_card(
        agent_id: str,
        request: Request,
        _actor: Annotated[
            Actor, Depends(require_roles(ActorRole.COORDINATOR, ActorRole.ADMIN))
        ] = None,
    ) -> AgentCardResponse:
        resolved = get_container(request)
        card = resolved.registry.get(agent_id)
        with resolved.session() as session:
            rows = session.execute(
                select(A2AAgent).where(A2AAgent.agent_id == agent_id).order_by(A2AAgent.card_version)
            ).scalars().all()
        return AgentCardResponse(
            agent_id=card.agent_id,
            card_version=card.card_version,
            protocol_versions=list(card.protocol_versions),
            capabilities=list(card.capabilities),
            input_schema=card.input_schema,
            output_schemas=list(card.output_schemas),
            endpoint=card.endpoint,
            limits=card.limits.model_dump(mode="json"),
            status=str(card.status),
            registered_at=rows[-1].created_at if rows else None,
        )

    # ---- 评测（阶段七） -----------------------------------------------------------
    @app.post("/api/v1/evals/run", tags=["evals"])
    async def run_evals(
        payload: EvalRunRequest,
        request: Request,
        actor: Annotated[Actor, Depends(require_roles(ActorRole.ADMIN))],
        idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    ) -> dict[str, Any]:
        """运行黄金集评测（single / a2a / offline 对照，后台执行）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回同一个 run_id，不重复启动评测。
        """
        return request.app.state.service.run_evals(
            payload, actor, idempotency_key=idempotency_key
        )

    @app.get("/api/v1/evals/{run_id}", tags=["evals"])
    async def get_eval_run(
        run_id: str,
        request: Request,
        _actor: Annotated[Actor, Depends(require_roles(ActorRole.ADMIN))],
    ) -> dict[str, Any]:
        return request.app.state.service.get_eval_run(run_id)

    # ---- 审计 -------------------------------------------------------------------
    @app.get("/api/v1/audit", response_model=list[AuditEventResponse], tags=["audit"])
    async def get_audit(
        request: Request,
        trace_id: str | None = None,
        task_id: str | None = None,
        event_type: str | None = None,
        limit: int = 200,
        offset: int = 0,
        _actor: Annotated[Actor, Depends(require_roles(ActorRole.ADMIN))] = None,
    ) -> list[AuditEventResponse]:
        resolved = get_container(request)
        with resolved.session() as session:
            events = list_events(
                session,
                trace_id=trace_id,
                task_id=task_id,
                event_type=event_type,
                limit=min(limit, 1000),
                offset=offset,
            )
            return [audit_response(event) for event in events]

    return app


app = create_app()


__all__ = ["app", "create_app"]
