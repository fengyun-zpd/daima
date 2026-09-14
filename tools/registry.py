"""工具注册中心（FR-012、FR-015、FR-017、宪法第六条）。

执行顺序（deny-by-default）：
1. 工具必须存在；
2. Agent capability allowlist 必须允许该工具，且禁止列表为空命中；
3. 读写类别与角色必须匹配（写工具只允许 Coordinator 在审批后调用）；
4. 参数必须通过 Pydantic/JSON Schema 校验；
5. 预算与循环检测；
6. 幂等键命中历史结果时直接返回原结果，不重复执行；
7. 记录参数哈希、结果哈希、耗时与审计事件（不保存参数原文）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from a2a.registry import AGENT_POLICIES, AgentPolicy
from domain.budget import Budget
from domain.enums import ActorRole, ToolAccess
from domain.errors import CodePilotError, ErrorCode
from domain.sanitize import payload_hash, sanitize_structured, summarize_payload
from domain.workspace import Workspace
from repositories.audit import record_event
from repositories.records import ToolExecutionStore

TOOL_VERSION = "1.0"

#: Coordinator 的调用方标识（写工具白名单、审计归属）。
COORDINATOR_AGENT_ID = "coordinator"

#: 允许调用工具的 Agent 角色（MVP 中 Agent 以 agent 角色调用只读工具）。
READ_TOOL_ROLES: frozenset[str] = frozenset({str(ActorRole.COORDINATOR), "agent"})
WRITE_TOOL_ROLES: frozenset[str] = frozenset({str(ActorRole.COORDINATOR)})


@dataclass(slots=True)
class ToolCallContext:
    """一次工具调用的上下文，由编排器构造，Agent 无法伪造。"""

    task_id: str
    parent_task_id: str | None
    agent_id: str
    actor_role: str
    trace_id: str
    workspace: Workspace
    session: Session
    budget: Budget
    mode: str = "a2a"
    sandbox: Any = None
    sandbox_limits: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCallResult:
    ok: bool
    tool_name: str
    data: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    replayed: bool = False
    duration_ms: int = 0
    result_hash: str | None = None


ToolHandler = Callable[[ToolCallContext, BaseModel], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    access: ToolAccess
    params_model: type[BaseModel]
    handler: ToolHandler
    version: str = TOOL_VERSION

    def schema(self) -> dict[str, Any]:
        return self.params_model.model_json_schema()


class ToolRegistry:
    """工具注册表：校验、策略、幂等与审计的唯一入口。"""

    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise CodePilotError(ErrorCode.INTERNAL_ERROR, f"工具重复注册：{spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"未注册的工具：{name}",
                details={"tool": name, "available": sorted(self._specs)},
            )
        return spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "version": spec.version,
                "access": str(spec.access),
                "description": spec.description,
                "params_schema": spec.schema(),
            }
            for spec in (self._specs[key] for key in sorted(self._specs))
        ]

    # ---- 策略 -------------------------------------------------------------------
    def policy_for(self, agent_id: str) -> AgentPolicy:
        policy = AGENT_POLICIES.get(agent_id)
        if policy is None:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"Agent {agent_id} 没有能力策略，默认拒绝（宪法第六条）",
                details={"agent_id": agent_id},
            )
        return policy

    def authorize(self, ctx: ToolCallContext, spec: ToolSpec) -> None:
        if ctx.agent_id == COORDINATOR_AGENT_ID:
            # Coordinator 是唯一编排者，不参与 Agent 能力 allowlist；
            # 但写工具仍必须携带审批上下文，且分支白名单在工具内部再次校验（宪法第六条）。
            if spec.access is ToolAccess.WRITE and ctx.extra.get("approved") is not True:
                raise CodePilotError(
                    ErrorCode.FORBIDDEN,
                    f"写工具 {spec.name} 必须携带已审批上下文",
                    details={"tool": spec.name},
                )
            if (
                spec.access is ToolAccess.WRITE
                and ctx.actor_role not in WRITE_TOOL_ROLES
                and ctx.extra.get("human_approval_role") is None
            ):
                raise CodePilotError(
                    ErrorCode.PERMISSION_DENIED,
                    f"角色 {ctx.actor_role} 不允许调用写工具 {spec.name}",
                    details={"role": ctx.actor_role, "tool": spec.name},
                )
            return

        policy = self.policy_for(ctx.agent_id)
        policy.assert_tool_allowed(spec.name)

        allowed_roles = WRITE_TOOL_ROLES if spec.access is ToolAccess.WRITE else READ_TOOL_ROLES
        if ctx.actor_role not in allowed_roles:
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                f"角色 {ctx.actor_role} 不允许调用 {spec.access} 工具 {spec.name}",
                details={"role": ctx.actor_role, "tool": spec.name, "access": str(spec.access)},
            )
        if spec.access is ToolAccess.WRITE:
            # 宪法第六条：只有 Coordinator 在审批通过后才能写任务分支。
            raise CodePilotError(
                ErrorCode.PERMISSION_DENIED,
                "Agent 不允许直接调用写工具",
                details={"agent_id": ctx.agent_id, "tool": spec.name},
            )

    # ---- 调用 -------------------------------------------------------------------
    def call(
        self,
        ctx: ToolCallContext,
        tool_name: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str,
    ) -> ToolCallResult:
        import time

        started = time.perf_counter()
        payload = dict(params or {})

        spec = self.get(tool_name)
        store = ToolExecutionStore(ctx.session)
        params_hash = payload_hash({"tool": tool_name, "params": payload})

        # 幂等命中：先于任何校验返回历史结果（FR-017）。
        existing = store.find(
            task_id=ctx.task_id, agent_id=ctx.agent_id, tool_name=tool_name, idempotency_key=idempotency_key
        )
        if existing is not None:
            if existing.params_hash != params_hash:
                raise CodePilotError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "相同幂等键对应不同工具参数",
                    details={"tool": tool_name, "idempotency_key": idempotency_key},
                )
            return ToolCallResult(
                ok=existing.allowed,
                tool_name=tool_name,
                data=dict(existing.result_summary.get("data", {})),
                error_code=existing.error_code,
                replayed=True,
                result_hash=existing.result_hash,
                duration_ms=existing.duration_ms,
            )

        try:
            self.authorize(ctx, spec)
        except CodePilotError as exc:
            self._record_denied(ctx, spec, params_hash, idempotency_key, exc)
            raise

        try:
            validated = spec.params_model.model_validate(payload)
        except ValidationError as exc:
            error = CodePilotError(
                ErrorCode.INVALID_INPUT,
                f"工具 {tool_name} 参数校验失败：{exc.errors()[0]['msg']}",
                details={"tool": tool_name, "errors": exc.errors()[:5]},
            )
            self._record_denied(ctx, spec, params_hash, idempotency_key, error)
            raise error from exc

        ctx.budget.charge_tool(f"{tool_name}:{params_hash}")

        try:
            result = spec.handler(ctx, validated)
        except CodePilotError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            store.record(
                task_id=ctx.task_id,
                parent_task_id=ctx.parent_task_id,
                agent_id=ctx.agent_id,
                actor_role=ctx.actor_role,
                tool_name=tool_name,
                access=str(spec.access),
                allowed=True,
                params_hash=params_hash,
                result_hash=None,
                result_summary={},
                idempotency_key=idempotency_key,
                duration_ms=duration_ms,
                error_code=str(exc.code),
                tool_version=spec.version,
            )
            raise

        duration_ms = int((time.perf_counter() - started) * 1000)
        result_hash = payload_hash(result)
        store.record(
            task_id=ctx.task_id,
            parent_task_id=ctx.parent_task_id,
            agent_id=ctx.agent_id,
            actor_role=ctx.actor_role,
            tool_name=tool_name,
            access=str(spec.access),
            allowed=True,
            params_hash=params_hash,
            result_hash=result_hash,
            result_summary={"data": sanitize_structured(result, max_string=500)},
            idempotency_key=idempotency_key,
            duration_ms=duration_ms,
            tool_version=spec.version,
        )
        record_event(
            ctx.session,
            actor_id=ctx.agent_id,
            actor_role=ctx.actor_role,
            action="tool_call",
            entity_type="tool",
            entity_id=tool_name,
            event_type="tool_called",
            trace_id=ctx.trace_id,
            task_id=ctx.task_id,
            parent_task_id=ctx.parent_task_id,
            agent_id=ctx.agent_id,
            idempotency_key=idempotency_key,
            duration_ms=duration_ms,
            after_state={
                "params_hash": params_hash,
                "params_summary": summarize_payload(payload),
                "result_hash": result_hash,
                "access": str(spec.access),
            },
        )
        return ToolCallResult(
            ok=True,
            tool_name=tool_name,
            data=result,
            duration_ms=duration_ms,
            result_hash=result_hash,
        )

    def _record_denied(
        self,
        ctx: ToolCallContext,
        spec: ToolSpec,
        params_hash: str,
        idempotency_key: str,
        error: CodePilotError,
    ) -> None:
        store = ToolExecutionStore(ctx.session)
        store.record(
            task_id=ctx.task_id,
            parent_task_id=ctx.parent_task_id,
            agent_id=ctx.agent_id,
            actor_role=ctx.actor_role,
            tool_name=spec.name,
            access=str(spec.access),
            allowed=False,
            params_hash=params_hash,
            result_hash=None,
            result_summary={},
            idempotency_key=idempotency_key,
            duration_ms=0,
            denied_reason=error.message,
            error_code=str(error.code),
            tool_version=spec.version,
        )
        record_event(
            ctx.session,
            actor_id=ctx.agent_id,
            actor_role=ctx.actor_role,
            action="tool_call_denied",
            entity_type="tool",
            entity_id=spec.name,
            event_type="tool_denied",
            trace_id=ctx.trace_id,
            task_id=ctx.task_id,
            parent_task_id=ctx.parent_task_id,
            agent_id=ctx.agent_id,
            idempotency_key=idempotency_key,
            error_code=str(error.code),
            after_state={"params_hash": params_hash, "reason": error.message},
        )


__all__ = [
    "COORDINATOR_AGENT_ID",
    "READ_TOOL_ROLES",
    "TOOL_VERSION",
    "WRITE_TOOL_ROLES",
    "ToolCallContext",
    "ToolCallResult",
    "ToolHandler",
    "ToolRegistry",
    "ToolSpec",
]
