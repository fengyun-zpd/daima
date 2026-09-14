"""内部 A2A 接口（docs/05 §5）。

只对 coordinator/admin 开放，绝不暴露给 developer：
- ``GET  /internal/a2a/agents``：健康 Agent Card 列表
- ``GET  /internal/a2a/agents/{agent_id}/card``：指定 Card
- ``POST /internal/a2a/agents/{agent_id}/tasks``：提交子任务
- ``GET  /internal/a2a/tasks/{task_id}``：查询状态与产物
- ``GET  /internal/a2a/tasks/{task_id}/events``：SSE 事件
- ``POST /internal/a2a/tasks/{task_id}/cancel``：请求取消

身份校验（docs/05 §5）：请求必须带调用方身份、trace_id、correlation_id、
协议版本与幂等键；缺失即拒绝。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

from a2a.protocol import A2ATask
from a2a.schema_registry import validate_a2a_task
from apps.api.agent_service import AgentService, FaultInjector, TaskScheduler
from apps.api.events import stream_events
from apps.api.idempotency import execute_idempotent
from apps.api.schemas import ErrorResponse
from domain.enums import PROTOCOL_VERSION, ActorRole
from domain.errors import CodePilotError, ErrorCode
from repositories.idempotency import COMMAND_A2A_CANCEL, COMMAND_A2A_SUBMIT

INTERNAL_ROLES = {str(ActorRole.COORDINATOR), str(ActorRole.ADMIN)}


def _require_internal_caller(
    x_actor_id: str | None, x_actor_role: str | None, protocol_version: str | None
) -> str:
    if not x_actor_id or not x_actor_role:
        raise CodePilotError(
            ErrorCode.PERMISSION_DENIED,
            "内部 A2A 接口必须携带 X-Actor-Id 与 X-Actor-Role",
        )
    if x_actor_role.strip().lower() not in INTERNAL_ROLES:
        raise CodePilotError(
            ErrorCode.PERMISSION_DENIED,
            f"角色 {x_actor_role} 无权访问内部 A2A 接口",
            details={"allowed_roles": sorted(INTERNAL_ROLES)},
        )
    if protocol_version and protocol_version != PROTOCOL_VERSION:
        raise CodePilotError(
            ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
            f"不支持的 A2A 协议版本：{protocol_version}",
            details={"supported": [PROTOCOL_VERSION], "requested": protocol_version},
        )
    return x_actor_id


def build_internal_router() -> APIRouter:
    """构造内部 A2A 路由；服务实例与调度器在请求时从 ``app.state`` 解析。"""
    router = APIRouter(prefix="/internal/a2a", tags=["a2a-internal"])

    def _service(request: Request) -> AgentService:
        return request.app.state.agent_service

    def _scheduler(request: Request) -> TaskScheduler:
        return request.app.state.agent_scheduler

    @router.get("/agents", responses={403: {"model": ErrorResponse}})
    async def list_agents(
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
    ) -> list[dict[str, Any]]:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        return _service(request).healthy_cards()

    @router.get("/agents/{agent_id}/card", responses={404: {"model": ErrorResponse}})
    async def get_card(
        agent_id: str,
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
    ) -> dict[str, Any]:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        return _service(request).card(agent_id)

    @router.post(
        "/agents/{agent_id}/tasks",
        status_code=202,
        responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def submit_task(
        agent_id: str,
        payload: dict[str, Any],
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
        x_trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
        x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-Id")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        if not idempotency_key:
            raise CodePilotError(ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "内部子任务必须带 Idempotency-Key")
        if not x_trace_id or not x_correlation_id:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT, "内部子任务必须带 X-Trace-Id 与 X-Correlation-Id"
            )
        payload = {**payload, "agent_id": agent_id}
        if payload.get("task_type") not in {"review", "impact", "fix", "verify"}:
            raise CodePilotError(ErrorCode.INVALID_INPUT, f"非法 task_type：{payload.get('task_type')}")
        validate_a2a_task(payload)
        task = A2ATask.model_validate(payload)
        if task.trace_id != x_trace_id or task.correlation_id != x_correlation_id:
            raise CodePilotError(
                ErrorCode.PARENT_TASK_MISMATCH,
                "请求头与任务体内的 trace_id/correlation_id 不一致",
            )
        if task.idempotency_key != idempotency_key:
            raise CodePilotError(ErrorCode.IDEMPOTENCY_CONFLICT, "请求头与任务体的幂等键不一致")

        response = execute_idempotent(
            _service(request).session_factory,
            actor_id=x_actor_id or "coordinator",
            actor_role=x_actor_role or "coordinator",
            command_type=COMMAND_A2A_SUBMIT,
            aggregate_ref=f"{task.parent_task_id}:{task.agent_id}:{task.task_type}",
            idempotency_key=idempotency_key,
            request=payload,
            work=lambda: _service(request).submit(task),
        )
        if response.get("created"):
            _scheduler(request).schedule(response["task_id"])
        return response

    @router.get("/tasks/{task_id}", responses={404: {"model": ErrorResponse}})
    async def get_task(
        task_id: str,
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
    ) -> dict[str, Any]:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        return _service(request).snapshot(task_id)

    @router.get("/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        resolved: AgentService = _service(request)
        # 校验任务存在（不存在时返回 RESOURCE_NOT_FOUND），再决定事件流形态。
        resolved.snapshot(task_id)
        faults: FaultInjector = resolved.faults
        if faults.enabled and faults.drop_events:
            # 故障注入：立刻关闭连接，模拟 SSE 断线（客户端应退化为轮询）。
            async def empty_stream():
                yield ": dropped\n\n"

            return StreamingResponse(empty_stream(), media_type="text/event-stream")
        return StreamingResponse(
            stream_events(
                session_factory=resolved.session_factory,
                task_id=task_id,
                last_event_id=last_event_id,
                max_seconds=30.0,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/tasks/{task_id}/cancel")
    async def cancel_task(
        task_id: str,
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        if not idempotency_key:
            raise CodePilotError(
                ErrorCode.IDEMPOTENCY_KEY_REQUIRED, "取消子任务必须携带 Idempotency-Key"
            )
        resolved = _service(request)
        return execute_idempotent(
            resolved.session_factory,
            actor_id=x_actor_id or "coordinator",
            actor_role=x_actor_role or "coordinator",
            command_type=COMMAND_A2A_CANCEL,
            aggregate_ref=task_id,
            idempotency_key=idempotency_key,
            request={"task_id": task_id},
            work=lambda: resolved.cancel(task_id),
        )

    @router.post("/_faults")
    async def set_faults(
        payload: dict[str, Any],
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
    ) -> dict[str, Any]:
        """故障注入开关（仅当 ``CODEPILOT_FAULT_INJECTION=1`` 时可用）。"""
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        service: AgentService = _service(request)
        if not service.faults.enabled:
            raise CodePilotError(
                ErrorCode.FORBIDDEN,
                "故障注入未启用（设置 CODEPILOT_FAULT_INJECTION=1 后重试）",
            )
        injector = FaultInjector(
            delay_seconds=float(payload.get("delay_seconds", 0.0)),
            drop_events=bool(payload.get("drop_events", False)),
            tamper_artifact=bool(payload.get("tamper_artifact", False)),
            unknown_status=bool(payload.get("unknown_status", False)),
            fail_with=payload.get("fail_with"),
            agent_id=payload.get("agent_id"),
            enabled=True,
        )
        service.faults = injector
        return injector.to_payload()

    @router.get("/_faults")
    async def get_faults(
        request: Request,
        x_actor_id: Annotated[str | None, Header(alias="X-Actor-Id")] = None,
        x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
        x_protocol: Annotated[str | None, Header(alias="X-A2A-Protocol-Version")] = None,
    ) -> dict[str, Any]:
        _require_internal_caller(x_actor_id, x_actor_role, x_protocol)
        return _service(request).faults.to_payload()

    return router


__all__ = ["INTERNAL_ROLES", "build_internal_router"]
