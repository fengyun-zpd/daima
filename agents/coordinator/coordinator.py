"""Coordinator：父任务唯一 owner（docs/01 §3、宪法第四条）。

职责：
1. 创建父任务并校验输入；
2. 解析 Agent Card、校验能力与协议版本；
3. 创建 Review / Impact 子任务并汇总 Artifact；
4. 校验必需 Artifact 后推进父任务状态；
5. 处理超时、重试、恢复与转人工。

Coordinator 不执行业务分析，也不直接读写文件；所有结果都来自子任务 Artifact。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from a2a.inprocess import InProcessInvoker
from a2a.protocol import A2ATask, ArtifactEnvelope, PatchCandidate, VerifyEvidence, make_deadline
from a2a.registry import AGENT_POLICIES, AgentRegistry
from agents.base import AgentHandler, ToolGateway
from domain.budget import Budget
from domain.clock import ensure_utc, utcnow
from domain.config import CodePilotConfig
from domain.enums import (
    ActorRole,
    ApprovalDecision,
    ArtifactType,
    ChildTaskStatus,
    ChildTaskType,
    ContextPolicy,
    InputType,
    ParentTaskStatus,
    PatchStatus,
    RunMode,
    SandboxRunStatus,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import (
    correlation_id_for,
    new_child_task_id,
    new_idempotency_key,
    new_task_id,
    new_trace_id,
)
from domain.impact_policy import ImpactPolicy, check_scope
from domain.llm import LLMGateway
from domain.sanitize import payload_hash
from domain.state_machine import can_retry_child
from domain.workspace import Workspace, build_workspace
from repositories.audit import record_event
from repositories.content_store import ContentStore
from repositories.records import ApprovalStore, CommentStore, PatchStore, SandboxRunStore
from repositories.recovery import RecoveryPlan, build_recovery_plan, list_recovery_plans
from repositories.store import (
    A2ATaskStore,
    ArtifactStore,
    ReviewTaskStore,
)
from sandbox.base import SandboxExecutor, SandboxLimits
from tools.registry import COORDINATOR_AGENT_ID, ToolCallContext, ToolRegistry, ToolSpec

CHILD_PIPELINE: tuple[ChildTaskType, ...] = (ChildTaskType.REVIEW, ChildTaskType.IMPACT)


@dataclass(slots=True)
class ReviewRequest:
    """创建审查父任务的输入（FR-001、FR-002）。"""

    input_type: InputType
    content: str
    base_commit: str
    actor_id: str
    actor_role: str
    idempotency_key: str
    context_policy: ContextPolicy = ContextPolicy.FUNCTION
    mode: RunMode = RunMode.A2A
    custom_task_id: str | None = None


@dataclass(slots=True)
class RunOutcome:
    """一次编排运行的结果摘要。"""

    task_id: str
    status: ParentTaskStatus
    next_action: str = "none"
    findings: int = 0
    affected_files: list[str] = field(default_factory=list)
    risk_level: str | None = None
    child_tasks: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    retried: list[str] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": str(self.status),
            "next_action": self.next_action,
            "findings": self.findings,
            "affected_files": list(self.affected_files),
            "risk_level": self.risk_level,
            "child_tasks": list(self.child_tasks),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retried": list(self.retried),
        }


class Coordinator:
    """父任务编排器（阶段三：Review + Impact 垂直切片）。"""

    def __init__(
        self,
        *,
        config: CodePilotConfig,
        session_factory: sessionmaker[Session],
        registry: AgentRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        handlers: dict[str, AgentHandler] | None = None,
        content_store: ContentStore | None = None,
        mode: RunMode | None = None,
        allow_parallel_children: bool | None = None,
        sandbox: SandboxExecutor | None = None,
        sandbox_limits: SandboxLimits | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        repo_root: str | None = None,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.registry = registry or AgentRegistry.load_static(allow_agents=config.a2a.allow_agents)
        self.tool_registry = tool_registry or _default_tool_registry()
        self.content_store = content_store or ContentStore()
        self.mode = mode or config.mode
        self.handlers = handlers or _default_handlers()
        self.sandbox = sandbox if sandbox is not None else _default_sandbox()
        self.sandbox_limits = sandbox_limits or SandboxLimits.from_config(config.sandbox)
        self.llm_provider = llm_provider
        self.llm_model = llm_model
        #: 合成仓库根目录：任务分支写入只发生在 <repo_root>/repos/<task_id>
        self.repo_root = Path(repo_root) if repo_root else self.content_store.root
        self.allow_parallel_children = (
            _supports_parallel(session_factory) if allow_parallel_children is None else allow_parallel_children
        )
        self.invoker = InProcessInvoker(
            registry=self.registry,
            handlers=self.handlers,
            request_builder=self._build_agent_request,
        )

    # -- 创建 ---------------------------------------------------------------------
    def create_review(self, request: ReviewRequest) -> tuple[Any, bool]:
        """校验输入、持久化工作区并创建（或幂等返回）父任务。"""
        task_id = new_task_id()
        workspace, parsed = build_workspace(
            input_type=request.input_type,
            content=request.content,
            base_commit=request.base_commit,
            context_policy=request.context_policy,
            task_id=task_id,
        )
        input_hash = payload_hash(
            {
                "input_type": str(request.input_type),
                "content": request.content,
                "base_commit": request.base_commit,
                "context_policy": str(request.context_policy),
                "custom_task_id": request.custom_task_id,
            }
        )
        summary = {
            "changed_files": workspace.changed_files,
            "partial": workspace.partial,
            "added_lines": sum(len(lines) for lines in workspace.changed_lines.values()),
            "removed_lines": parsed.removed_line_count if parsed else 0,
            "warnings": workspace.warnings,
        }

        session = self.session_factory()
        try:
            store = ReviewTaskStore(session)
            existing = store.find_by_idempotency(
                actor_id=request.actor_id,
                command_type="create_review",
                idempotency_key=request.idempotency_key,
            )
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise CodePilotError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "相同幂等键对应不同输入内容",
                        trace_id=existing.trace_id,
                        details={"existing_task_id": existing.id},
                    )
                return existing, False

            if request.custom_task_id and store.find_by_custom_id(
                actor_id=request.actor_id, custom_task_id=request.custom_task_id.strip()
            ) is not None:
                raise CodePilotError(ErrorCode.CONFLICT, f"任务编号 {request.custom_task_id} 已存在，请换一个。")

            self.content_store.store_workspace(task_id, workspace.to_payload())
            task, created = store.create(
                task_id=task_id,
                trace_id=new_trace_id(),
                actor_id=request.actor_id,
                actor_role=request.actor_role,
                mode=str(request.mode),
                input_type=str(request.input_type),
                input_hash=input_hash,
                input_summary=summary,
                base_commit=request.base_commit,
                context_policy=str(request.context_policy),
                idempotency_key=request.idempotency_key,
                input_ref=ContentStore.workspace_ref(task_id),
                created_at=utcnow(),
                custom_task_id=request.custom_task_id,
            )
            resolved_agents: dict[str, str] = {}
            negotiation_errors: dict[str, dict[str, str]] = {}
            for task_type in CHILD_PIPELINE:
                agent_id = self.registry.agent_for_task_type(task_type)
                try:
                    resolved_agents[str(task_type)] = self.registry.resolve(
                        task_type=task_type,
                        required_capabilities=AGENT_POLICIES[agent_id].required_capabilities,
                    ).agent_id
                except CodePilotError as exc:
                    # FR-091：Card 能力缺失/协议不兼容时记录协商结果，由审查阶段转人工，
                    # 而不是让父任务创建本身失败（审计必须留下证据）。
                    negotiation_errors[str(task_type)] = {
                        "code": str(exc.code),
                        "message": exc.message,
                    }
            record_event(
                session,
                actor_id="coordinator",
                actor_role="coordinator",
                action="resolve_agent_cards",
                entity_type="review_task",
                entity_id=task.id,
                event_type="agent_card_resolved",
                trace_id=task.trace_id,
                task_id=task.id,
                parent_task_id=task.id,
                after_state={
                    "agents": resolved_agents,
                    "negotiation_errors": negotiation_errors,
                    "protocol_version": self.config.a2a.protocol_version,
                },
            )
            session.commit()
            return task, created
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- 运行 ---------------------------------------------------------------------
    def run(self, task_id: str, *, auto_fix: bool | None = None) -> RunOutcome:
        """同步入口：供后台线程与测试调用。

        ``auto_fix`` 默认取 ``CODEPILOT_AUTO_FIX``（默认 0）：SRS §4.1 的主流程是
        开发者选定可修复意见后再触发 Fix，因此默认只跑到 ``REVIEWED``；
        显式调用 ``POST /reviews/{id}/fixes`` 或打开开关时继续 Fix/Verify 阶段。
        """
        if auto_fix is None:
            auto_fix = os.environ.get("CODEPILOT_AUTO_FIX", "0") == "1"
        return asyncio.run(self.run_async(task_id, auto_fix=auto_fix))

    async def run_async(self, task_id: str, *, auto_fix: bool = False) -> RunOutcome:
        session = self.session_factory()
        try:
            store = ReviewTaskStore(session)
            task = store.get(task_id)
            if task is None:
                raise CodePilotError(ErrorCode.RESOURCE_NOT_FOUND, f"任务不存在：{task_id}")

            status = ParentTaskStatus(task.status)
            if status in {
                ParentTaskStatus.MERGED,
                ParentTaskStatus.REJECTED,
                ParentTaskStatus.FAILED,
                ParentTaskStatus.NEEDS_HUMAN,
            }:
                return RunOutcome(task_id=task_id, status=status, next_action="terminal")

            if status is ParentTaskStatus.DRAFT:
                result = store.apply_intent(
                    task_id,
                    expected_version=task.state_version,
                    owner="coordinator",
                    changes={"status": str(ParentTaskStatus.REVIEWING), "current_step": "reviewing"},
                    reason="input_valid",
                    idempotency_key=f"{task_id}:enter_reviewing",
                )
                task = result.row
                session.commit()
                status = ParentTaskStatus(task.status)

            if status is ParentTaskStatus.REVIEWING:
                outcome = await self._run_review_phase(session, task_id)
                if auto_fix and outcome.status is ParentTaskStatus.REVIEWED:
                    return await self._run_fix_phase(session, task_id)
                return outcome

            if status in {ParentTaskStatus.REVIEWED, ParentTaskStatus.FIXING}:
                return await self._run_fix_phase(session, task_id)

            if status is ParentTaskStatus.TESTING:
                return await self._run_verify_phase(session, task_id)

            return RunOutcome(task_id=task_id, status=status, next_action="await_external")
        except CodePilotError as exc:
            session.rollback()
            return self._park(session, task_id, exc)
        finally:
            session.close()

    async def _run_review_phase(self, session: Session, task_id: str) -> RunOutcome:
        store = ReviewTaskStore(session)
        child_store = A2ATaskStore(session)
        artifact_store = ArtifactStore(session)

        task = store.get(task_id)
        assert task is not None
        # 工作区必须可加载，否则编排无法继续（恢复场景下会直接转人工）。
        self._load_workspace(task.id, session)

        plans = [self._plan_child(task, task_type) for task_type in CHILD_PIPELINE]
        rows = []
        for plan in plans:
            handle = await self.invoker.submit(plan, session=session)
            row = child_store.get(handle.task_id)
            assert row is not None
            rows.append(row)
        session.commit()

        outcomes = await self._execute_children(session, rows)

        # 子任务可能由其它会话/进程更新（HTTP A2A、后台线程），先对账再决定重试。
        session.expire_all()

        retried: list[str] = []
        for outcome, row in zip(outcomes, rows, strict=False):
            if outcome.status is ChildTaskStatus.FAILED:
                refreshed = child_store.get(row.id)
                assert refreshed is not None
                if can_retry_child(
                    status=ChildTaskStatus(refreshed.status),
                    attempt=refreshed.attempt,
                    error_code=refreshed.error_code,
                    max_attempts=refreshed.max_attempts,
                ):
                    # 宪法第七条：先对账（status 查询）再重试一次。
                    observed = await self.invoker.status(row.id, session=session)
                    if observed == str(ChildTaskStatus.FAILED):
                        child_store.transition(
                            row.id,
                            ChildTaskStatus.WORKING,
                            expected_version=refreshed.state_version,
                            idempotency_key=f"{row.id}:retry",
                            attempt=refreshed.attempt + 1,
                            actor_id="coordinator",
                        )
                        session.commit()
                        retry_outcome = await self.invoker.execute(row.id, session=session)
                        retried.append(row.id)
                        session.commit()
                        if retry_outcome.status is ChildTaskStatus.FAILED:
                            return self._park(
                                session,
                                task.id,
                                CodePilotError(
                                    ErrorCode.TASK_TIMEOUT
                                    if retry_outcome.error_code == str(ErrorCode.TASK_TIMEOUT)
                                    else ErrorCode.QUALITY_GATE_FAILED,
                                    f"子任务 {row.id} 重试后仍失败：{retry_outcome.error_message}",
                                ),
                            )

        # 重新读取子任务状态与 Artifact
        children = child_store.list_by_parent(task.id)
        artifacts = artifact_store.list_by_parent(task.id)
        validated = [row for row in artifacts if row.validated and row.validation_error is None]

        failed = [row for row in children if ChildTaskStatus(row.status) is ChildTaskStatus.FAILED]
        if failed:
            codes = ", ".join(f"{row.agent_id}:{row.error_code}" for row in failed)
            return self._park(
                session,
                task.id,
                CodePilotError(
                    ErrorCode.TASK_STATUS_UNKNOWN
                    if any(row.error_code == str(ErrorCode.TASK_STATUS_UNKNOWN) for row in failed)
                    else ErrorCode.QUALITY_GATE_FAILED,
                    f"存在失败的子任务，转人工：{codes}",
                ),
            )

        missing = [
            str(artifact_type)
            for artifact_type in (ArtifactType.FINDING, ArtifactType.IMPACT_REPORT)
            if not any(row.artifact_type == str(artifact_type) for row in validated)
        ]
        if missing:
            return self._park(
                session,
                task.id,
                CodePilotError(
                    ErrorCode.ARTIFACT_SCHEMA_INVALID,
                    f"缺少必需 Artifact，父任务不得前进：{missing}",
                    details={"missing": missing},
                ),
            )

        finding_row = next(row for row in validated if row.artifact_type == str(ArtifactType.FINDING))
        impact_row = next(row for row in validated if row.artifact_type == str(ArtifactType.IMPACT_REPORT))
        finding_envelope = artifact_store.to_envelope(finding_row)
        impact_envelope = artifact_store.to_envelope(impact_row)
        policy = ImpactPolicy(
            wide_impact_file_threshold=self.config.fix.wide_impact_file_threshold
        )

        comments = self._persist_comments(session, task, finding_row, impact_envelope)
        session.commit()

        summary = {
            "findings": len(finding_envelope.data.get("findings", [])),
            "auto_fixable": sum(
                1 for item in finding_envelope.data.get("findings", []) if item.get("auto_fixable")
            ),
            "risk_level": impact_envelope.data.get("risk_level"),
            "affected_files": impact_envelope.data.get("affected_files", []),
            "uncertain": impact_envelope.data.get("uncertain", False),
            "wide_impact": policy.is_wide_impact(
                affected_files=impact_envelope.data.get("affected_files", [])
            ),
            "human_review_required": policy.requires_human(
                affected_files=impact_envelope.data.get("affected_files", [])
            ),
            "comments": len(comments),
            "child_tasks": [
                {"id": row.id, "agent_id": row.agent_id, "status": row.status, "attempt": row.attempt}
                for row in children
            ],
            "artifacts": [
                {"id": row.id, "type": row.artifact_type, "hash": row.content_hash} for row in validated
            ],
            "mode": task.mode,
        }

        current = store.get(task.id)
        assert current is not None
        result = store.apply_intent(
            task.id,
            expected_version=current.state_version,
            owner="coordinator",
            changes={
                "status": str(ParentTaskStatus.REVIEWED),
                "current_step": "reviewed",
                "summary": summary,
                "completed": [row.id for row in children if row.status == str(ChildTaskStatus.COMPLETED)],
                "pending": [],
                "child_tasks": [row.id for row in children],
                "artifacts": [row.id for row in validated],
            },
            reason="review_and_impact_done",
            idempotency_key=f"{task.id}:reviewed",
        )
        session.commit()

        return RunOutcome(
            task_id=task.id,
            status=ParentTaskStatus(result.row.status),
            next_action="await_fix_or_finish",
            findings=summary["findings"],
            affected_files=list(summary["affected_files"]),
            risk_level=summary["risk_level"],
            child_tasks=summary["child_tasks"],
            retried=retried,
        )

    # ---- Fix / Verify（阶段六） ---------------------------------------------------
    async def _run_fix_phase(self, session: Session, task_id: str) -> RunOutcome:
        """REVIEWED → FIXING：生成候选补丁并执行格式/范围/scope 门禁。"""
        store = ReviewTaskStore(session)
        child_store = A2ATaskStore(session)
        artifact_store = ArtifactStore(session)
        comment_store = CommentStore(session)
        patch_store = PatchStore(session)

        task = store.get(task_id)
        assert task is not None

        if task.mode == str(RunMode.OFFLINE):
            # 宪法第二条：offline 只运行确定性规则与 AST，不生成未经验证的补丁。
            result = store.apply_intent(
                task_id,
                expected_version=task.state_version,
                owner="coordinator",
                changes={
                    "status": str(ParentTaskStatus.REJECTED),
                    "current_step": "offline_stop",
                    "summary": {**(task.summary or {}), "reason": "offline_mode_no_fix"},
                },
                reason="offline_mode_no_fix",
                idempotency_key=f"{task_id}:offline_stop",
                event_type="task_rejected",
            )
            session.commit()
            return RunOutcome(task_id=task_id, status=ParentTaskStatus(result.row.status), next_action="offline_stop")

        fixable = comment_store.list_auto_fixable(task_id, comment_ids=list(task.fixed_comment_ids or []))
        if not fixable:
            # docs/02：REVIEWED → REJECTED（无可修复项）
            result = store.apply_intent(
                task_id,
                expected_version=task.state_version,
                owner="coordinator",
                changes={
                    "status": str(ParentTaskStatus.REJECTED),
                    "current_step": "reviewed_no_fix",
                    "summary": {**(task.summary or {}), "reason": "no_auto_fixable_findings"},
                },
                reason="no_fix_or_blocked",
                idempotency_key=f"{task_id}:no_fix",
                event_type="task_rejected",
            )
            session.commit()
            return RunOutcome(task_id=task_id, status=ParentTaskStatus(result.row.status), next_action="no_fixable")

        if ParentTaskStatus(task.status) is ParentTaskStatus.REVIEWED:
            result = store.apply_intent(
                task_id,
                expected_version=task.state_version,
                owner="coordinator",
                changes={
                    "status": str(ParentTaskStatus.FIXING),
                    "current_step": "fixing",
                    "fixed_comment_ids": [row.id for row in fixable],
                },
                reason="fix_requested",
                idempotency_key=f"{task_id}:enter_fixing",
            )
            task = result.row
            session.commit()

        finding_row = artifact_store.latest_by_type(task_id, str(ArtifactType.FINDING))
        if finding_row is None:
            return self._park(
                session,
                task.id,
                CodePilotError(ErrorCode.ARTIFACT_SCHEMA_INVALID, "缺少 Finding Artifact，无法生成补丁"),
            )

        fix_task = self._plan_child(task, ChildTaskType.FIX, input_artifacts=[finding_row.id])
        handle = await self.invoker.submit(fix_task, session=session)
        fix_row = child_store.get(handle.task_id)
        assert fix_row is not None
        session.commit()

        outcome = await self._execute_child_in_session(fix_row.id) if self.allow_parallel_children else (
            await self.invoker.execute(fix_row.id, session=session)
        )
        session.expire_all()
        session.commit()

        if outcome.status is not ChildTaskStatus.COMPLETED:
            refreshed = child_store.get(fix_row.id)
            error_code = (refreshed.error_code if refreshed else None) or str(ErrorCode.PATCH_INVALID)
            return self._park(
                session,
                task.id,
                CodePilotError(
                    ErrorCode(error_code) if error_code in set(ErrorCode) else ErrorCode.PATCH_INVALID,
                    (refreshed.error_message if refreshed else None) or "候选补丁生成失败",
                ),
            )

        candidate_envelope = _artifact_envelope(
            artifact_store,
            fix_row.id,
            ArtifactType.PATCH_CANDIDATE,
            remote_task_id=fix_row.remote_task_id,
        )
        if candidate_envelope is None:
            return self._park(
                session,
                task.id,
                CodePilotError(ErrorCode.PATCH_INVALID, "Fix 子任务未返回 PatchCandidate"),
            )
        candidate = PatchCandidate.model_validate(candidate_envelope.data)

        # 确定性范围门禁（FR-027/FR-028）：scope drift、WIDE_IMPACT、高风险模块
        declared_files = sorted({row.file for row in fixable})
        impact_row = artifact_store.latest_by_type(task_id, str(ArtifactType.IMPACT_REPORT))
        affected = list(impact_row.data.get("affected_files", [])) if impact_row else []
        scope = check_scope(
            patch_files=list(candidate.changed_files),
            declared_files=declared_files,
            affected_files=affected or declared_files,
            policy=ImpactPolicy(wide_impact_file_threshold=self.config.fix.wide_impact_file_threshold),
        )

        patch, created = patch_store.create_candidate(
            task_id=task.id,
            candidate=candidate,
            artifact_id=candidate_envelope.artifact_id,
            idempotency_key=candidate.idempotency_key,
        )
        patch_store.record_evidence(
            patch.id,
            scope_drift=scope.scope_drift,
            scope_drift_files=scope.scope_drift_files,
            wide_impact=scope.wide_impact,
        )
        session.commit()

        if scope.scope_drift:
            return self._park(
                session,
                task.id,
                CodePilotError(
                    ErrorCode.SCOPE_DRIFT,
                    f"补丁超出声明范围：{scope.scope_drift_files}",
                    details=scope.to_payload(),
                ),
            )

        current = store.get(task.id)
        assert current is not None
        if ParentTaskStatus(current.status) is ParentTaskStatus.FIXING:
            current = store.apply_intent(
                task.id,
                expected_version=current.state_version,
                owner="coordinator",
                changes={
                    "status": str(ParentTaskStatus.TESTING),
                    "current_step": "testing",
                    "active_patch_id": patch.id,
                    "summary": {
                        **(current.summary or {}),
                        "patch_version": patch.patch_version,
                        "patch_hash": patch.patch_hash,
                        "patch_files": list(candidate.changed_files),
                        "scope": scope.to_payload(),
                    },
                },
                reason="patch_valid",
                idempotency_key=f"{task.id}:enter_testing:v{patch.patch_version}",
            ).row
            session.commit()

        if ParentTaskStatus(current.status) is ParentTaskStatus.TESTING:
            return await self._run_verify_phase(session, task.id)

        return RunOutcome(
            task_id=task.id, status=ParentTaskStatus(current.status), next_action="await_verify"
        )

    async def _run_verify_phase(self, session: Session, task_id: str) -> RunOutcome:
        """TESTING → PENDING_APPROVAL：在沙箱中执行质量门禁。"""
        store = ReviewTaskStore(session)
        child_store = A2ATaskStore(session)
        artifact_store = ArtifactStore(session)
        patch_store = PatchStore(session)

        task = store.get(task_id)
        assert task is not None
        patch = patch_store.get(task.active_patch_id) if task.active_patch_id else patch_store.latest(task.id)
        if patch is None:
            return self._park(
                session, task.id, CodePilotError(ErrorCode.PATCH_INVALID, "缺少候选补丁，无法验证")
            )

        candidate_row = (
            artifact_store.get(patch.artifact_id) if patch.artifact_id else None
        )
        if candidate_row is None:
            return self._park(
                session,
                task.id,
                CodePilotError(ErrorCode.ARTIFACT_SCHEMA_INVALID, "缺少 PatchCandidate Artifact，无法验证"),
            )

        verify_task = self._plan_child(task, ChildTaskType.VERIFY, input_artifacts=[candidate_row.id])
        handle = await self.invoker.submit(verify_task, session=session)
        verify_row = child_store.get(handle.task_id)
        assert verify_row is not None
        session.commit()

        outcome = (
            await self._execute_child_in_session(verify_row.id)
            if self.allow_parallel_children
            else await self.invoker.execute(verify_row.id, session=session)
        )
        session.expire_all()
        session.commit()

        evidence_envelope = _artifact_envelope(
            artifact_store,
            verify_row.id,
            ArtifactType.VERIFY_EVIDENCE,
            remote_task_id=verify_row.remote_task_id,
        )
        if outcome.status is not ChildTaskStatus.COMPLETED or evidence_envelope is None:
            refreshed = child_store.get(verify_row.id)
            error_code = (refreshed.error_code if refreshed else None) or str(ErrorCode.QUALITY_GATE_FAILED)
            return self._park(
                session,
                task.id,
                CodePilotError(
                    ErrorCode(error_code) if error_code in set(ErrorCode) else ErrorCode.QUALITY_GATE_FAILED,
                    (refreshed.error_message if refreshed else None) or "验证子任务失败",
                ),
            )

        evidence = VerifyEvidence.model_validate(evidence_envelope.data)
        sandbox_row = SandboxRunStore(session).record(
            sandbox_run_id=evidence.sandbox_run_id,
            task_id=task.id,
            patch_id=patch.id,
            image=self.sandbox_limits.image,
            status=SandboxRunStatus.PASSED if evidence.evidence_sufficient else SandboxRunStatus.FAILED,
            exit_code=evidence.exit_code,
            duration_ms=0,
            limits=self.sandbox_limits.to_payload(),
            stdout=evidence.sanitized_output,
            stderr="",
            test_summary=evidence.unit_tests.model_dump(mode="json"),
            coverage_before=evidence.coverage.before,
            coverage_after=evidence.coverage.after,
            coverage_delta=evidence.coverage.delta,
            degraded=evidence.sandbox_degraded,
            degraded_reason=evidence.degraded_reason,
        )
        patch_store.record_evidence(
            patch.id,
            scope_drift=evidence.scope_drift,
            scope_drift_files=patch.scope_drift_files,
            wide_impact=patch.wide_impact,
            coverage_before=evidence.coverage.before,
            coverage_after=evidence.coverage.after,
            coverage_delta=evidence.coverage.delta,
            test_gap=evidence.test_gap,
            sandbox_run_id=sandbox_row.id,
        )
        session.commit()

        if not evidence.evidence_sufficient:
            reason = _gate_failure_reason(evidence)
            return self._park(
                session,
                task.id,
                CodePilotError(
                    reason,
                    "质量门禁未通过，未产生任何分支副作用，转人工",
                    details={"tiers": [tier.model_dump(mode="json") for tier in evidence.tiers]},
                ),
                summary_extra={
                    "verify_evidence": evidence.model_dump(mode="json"),
                    "gate_failure": {
                        "error_code": str(reason),
                        "tiers": [
                            tier.model_dump(mode="json") for tier in evidence.tiers if not tier.ok
                        ],
                    },
                    "patch_version": patch.patch_version,
                    "patch_hash": patch.patch_hash,
                    "test_gap": evidence.test_gap,
                    "coverage_delta": evidence.coverage.delta,
                },
            )

        current = store.get(task.id)
        assert current is not None
        if ParentTaskStatus(current.status) is ParentTaskStatus.TESTING:
            result = store.apply_intent(
                task.id,
                expected_version=current.state_version,
                owner="coordinator",
                changes={
                    "status": str(ParentTaskStatus.PENDING_APPROVAL),
                    "current_step": "pending_approval",
                    "summary": {
                        **(current.summary or {}),
                        "wide_impact": patch.wide_impact,
                        "requires_extra_approval": patch.wide_impact or bool(patch.scope_drift),
                        "coverage_delta": patch.coverage_delta,
                        "test_gap": patch.test_gap,
                        "verify_evidence": evidence.model_dump(mode="json"),
                    },
                },
                reason="gate_pass",
                idempotency_key=f"{task.id}:enter_pending_approval:v{patch.patch_version}",
            )
            session.commit()
        else:
            result = current

        return RunOutcome(
            task_id=task.id,
            status=ParentTaskStatus(result.row.status),
            next_action="await_approval",
            risk_level=(current.summary or {}).get("risk_level"),
        )

    async def _execute_children(self, session: Session, rows: Sequence[Any]) -> list[Any]:
        if self.allow_parallel_children and len(rows) > 1:
            return await asyncio.gather(
                *[self._execute_child_in_session(row.id) for row in rows]
            )
        outcomes = []
        for row in rows:
            outcomes.append(await self.invoker.execute(row.id, session=session))
        return outcomes

    async def _execute_child_in_session(self, task_id: str):
        session = self.session_factory()
        try:
            outcome = await self.invoker.execute(task_id, session=session)
            session.commit()
            return outcome
        finally:
            session.close()

    # -- 恢复 ---------------------------------------------------------------------
    def recover(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """服务重启后的恢复入口（FR-097）。已完成子任务不会重放。"""
        session = self.session_factory()
        try:
            plans = list_recovery_plans(session, limit=limit)
            payloads = [plan.to_payload() for plan in plans]
            session.commit()
        finally:
            session.close()

        for plan in plans:
            if plan.next_action in {
                "start_review",
                "create_children",
                "finalize_review",
                "retry_children",
                "await_children",
            }:
                self.run(plan.task_id)
        return payloads

    # -- 评测（阶段七） --------------------------------------------------------------
    def new_eval_run_id(self) -> str:
        from domain.ids import new_eval_run_id

        return new_eval_run_id()

    def recovery_plan(self, task_id: str) -> RecoveryPlan:
        session = self.session_factory()
        try:
            return build_recovery_plan(session, task_id)
        finally:
            session.close()

    # -- 审批与合并（阶段七） --------------------------------------------------------
    def record_approval(
        self,
        *,
        patch_id: str,
        decision: ApprovalDecision,
        patch_version: int,
        reason: str,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """记录审批决定；批准绑定 patch_version，拒绝必须填写原因（FR-070~FR-074）。"""
        session = self.session_factory()
        try:
            store = ReviewTaskStore(session)
            patch = PatchStore(session).get(patch_id)
            task = store.get(patch.task_id)
            if ParentTaskStatus(task.status) is not ParentTaskStatus.PENDING_APPROVAL:
                raise CodePilotError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    f"任务状态 {task.status} 不允许审批（需要 PENDING_APPROVAL）",
                    trace_id=task.trace_id,
                    details={"status": task.status},
                )
            if decision is ApprovalDecision.APPROVE and patch.scope_drift:
                raise CodePilotError(
                    ErrorCode.SCOPE_DRIFT, "存在 scope drift 的补丁不允许批准", trace_id=task.trace_id
                )
            if patch_version != patch.patch_version:
                # FR-072：审批决定必须绑定当前补丁版本，旧版本审批新补丁一律拒绝。
                raise CodePilotError(
                    ErrorCode.VERSION_CONFLICT,
                    f"审批版本 {patch_version} 与当前补丁版本 {patch.patch_version} 不一致（FR-072）",
                    trace_id=task.trace_id,
                    details={"approved": patch_version, "current": patch.patch_version},
                )
            approval, created = ApprovalStore(session).record(
                task_id=task.id,
                patch_id=patch.id,
                decided_by=actor_id,
                decided_role=actor_role,
                decision=decision,
                reason=reason,
                patch_version=patch_version,
                patch_hash=patch.patch_hash,
            )
            if decision is ApprovalDecision.REJECT:
                store.apply_intent(
                    task.id,
                    expected_version=task.state_version,
                    owner="coordinator",
                    changes={
                        "status": str(ParentTaskStatus.REJECTED),
                        "current_step": "rejected",
                        "summary": {**(task.summary or {}), "reject_reason": reason},
                    },
                    reason="reject_with_reason",
                    idempotency_key=f"{task.id}:rejected:v{patch_version}",
                    actor_id="coordinator",
                    event_type="task_rejected",
                )
            session.commit()
            return {
                "approval_id": approval.id,
                "decision": approval.decision,
                "patch_id": patch.id,
                "patch_version": approval.patch_version,
                "patch_hash": approval.patch_hash,
                "created": created,
                "task_status": ParentTaskStatus(
                    store.get(task.id).status
                ).value,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def merge(
        self,
        *,
        patch_id: str,
        patch_version: int,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """审批通过后把补丁写入任务分支（FR-041~FR-043、FR-070~FR-072）。"""
        session = self.session_factory()
        try:
            store = ReviewTaskStore(session)
            patch_store = PatchStore(session)
            patch = patch_store.get(patch_id)
            task = store.get(patch.task_id)
            approval = ApprovalStore(session).find(patch.id, patch_version)

            if approval is None or approval.decision != str(ApprovalDecision.APPROVE):
                raise CodePilotError(
                    ErrorCode.FORBIDDEN,
                    "缺少绑定该 patch_version 的审批记录，禁止合并（FR-070）",
                    trace_id=task.trace_id,
                    details={"patch_id": patch.id, "patch_version": patch_version},
                )
            if patch.patch_version != patch_version or approval.patch_hash != patch.patch_hash:
                raise CodePilotError(
                    ErrorCode.VERSION_CONFLICT,
                    "审批绑定的补丁版本或哈希与当前补丁不一致（FR-072）",
                    trace_id=task.trace_id,
                    details={"patch_version": patch_version, "current": patch.patch_version},
                )
            if ParentTaskStatus(task.status) is not ParentTaskStatus.PENDING_APPROVAL:
                raise CodePilotError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    f"任务状态 {task.status} 不允许合并（需要 PENDING_APPROVAL）",
                    trace_id=task.trace_id,
                    details={"status": task.status},
                )
            if patch.status == str(PatchStatus.APPLIED):
                raise CodePilotError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    "该补丁版本已应用，禁止重复写入（幂等保护）",
                    trace_id=task.trace_id,
                )

            workspace = self._load_workspace(task.id, session)
            result = self._apply_to_branch(
                session=session,
                task=task,
                patch=patch,
                workspace=workspace,
                actor_id=actor_id,
                actor_role=actor_role,
            )
            patch_store.set_status(patch.id, PatchStatus.APPLIED)
            store.apply_intent(
                task.id,
                expected_version=task.state_version,
                owner="coordinator",
                changes={
                    "status": str(ParentTaskStatus.MERGED),
                    "current_step": "merged",
                    "summary": {
                        **(task.summary or {}),
                        "branch": result["branch"],
                        "commit": result["commit"],
                        "merged_by": actor_id,
                    },
                },
                reason="approve_and_version_match",
                idempotency_key=f"{task.id}:merged:v{patch_version}",
                actor_id="coordinator",
                event_type="task_merged",
            )
            session.commit()
            return {
                "task_id": task.id,
                "patch_id": patch.id,
                "patch_version": patch_version,
                "branch": result["branch"],
                "commit": result["commit"],
                "changed_files": result["changed_files"],
                "task_status": str(ParentTaskStatus.MERGED),
                "merged_by": actor_id,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _apply_to_branch(
        self,
        *,
        session: Session,
        task: Any,
        patch: Any,
        workspace: Workspace,
        actor_id: str,
        actor_role: str,
    ) -> dict[str, Any]:
        """通过 Tool Registry 调用写工具，保证审计、权限与分支白名单都生效。"""
        ctx = ToolCallContext(
            task_id=task.id,
            parent_task_id=task.id,
            agent_id=COORDINATOR_AGENT_ID,
            actor_role=str(ActorRole.COORDINATOR),
            trace_id=task.trace_id,
            workspace=workspace,
            session=session,
            budget=Budget.from_config(self.config.agent),
            mode=task.mode,
            sandbox=self.sandbox,
            sandbox_limits=self.sandbox_limits,
            extra={
                "approved": True,
                "approved_patch_version": patch.patch_version,
                "approved_patch_hash": patch.patch_hash,
                "approved_by": actor_id,
                "human_approval_role": actor_role,
                "repo_root": str(self.repo_root),
                "base_commit": task.base_commit,
            },
        )
        result = self.tool_registry.call(
            ctx,
            "write_patch",
            {
                "patch_id": patch.id,
                "patch_version": patch.patch_version,
                "patch_hash": patch.patch_hash,
                "diff": patch.diff_text,
                "target_branch": patch.target_branch,
            },
            idempotency_key=f"{task.id}:write_patch:v{patch.patch_version}",
        )
        return dict(result.data)

    # -- 内部构造 -----------------------------------------------------------------
    def _plan_child(
        self,
        task: Any,
        task_type: ChildTaskType,
        *,
        input_artifacts: list[str] | None = None,
        attempt: int = 1,
    ) -> A2ATask:
        agent_id = self.registry.agent_for_task_type(task_type)
        policy = AGENT_POLICIES[agent_id]
        card = self.registry.resolve(
            task_type=task_type, required_capabilities=policy.required_capabilities
        )
        timeout = min(card.limits.timeout_seconds, self.config.a2a.child_task_timeout_seconds)
        if task_type is ChildTaskType.VERIFY:
            timeout = max(timeout, self.config.sandbox.timeout_seconds * 2 + 30)
        deadline = make_deadline(timeout)
        return A2ATask(
            task_id=new_child_task_id(str(task_type)),
            parent_task_id=task.id,
            trace_id=task.trace_id,
            agent_id=agent_id,
            task_type=task_type,
            protocol_version=self.config.a2a.protocol_version,
            status=ChildTaskStatus.SUBMITTED,
            input_artifacts=list(input_artifacts or []),
            required_output_types=[str(item) for item in policy.required_output_types],
            deadline=deadline,
            attempt=attempt,
            idempotency_key=new_idempotency_key(task.id, task_type, f"v{attempt}"),
            correlation_id=correlation_id_for(task.id, str(task_type)),
            state_version=1,
        )

    def _build_agent_request(self, row: Any, session: Session):
        from agents.base import AgentRequest

        parent = ReviewTaskStore(session).get(row.parent_task_id)
        workspace = self._load_workspace(row.parent_task_id, session)
        artifact_store = ArtifactStore(session)
        inputs = [
            artifact_store.to_envelope(item)
            for artifact_id in (row.input_artifacts or [])
            if (item := artifact_store.get(artifact_id)) is not None
        ]
        budget = Budget.from_config(self.config.agent)
        mode = str(getattr(parent, "mode", self.mode))
        tool_context = ToolCallContext(
            task_id=row.id,
            parent_task_id=row.parent_task_id,
            agent_id=row.agent_id,
            actor_role="agent",
            trace_id=row.trace_id,
            workspace=workspace,
            session=session,
            budget=budget,
            mode=mode,
            sandbox=self.sandbox,
            sandbox_limits=self.sandbox_limits,
            extra={"base_commit": getattr(parent, "base_commit", "")},
        )
        task = A2ATask(
            task_id=row.id,
            parent_task_id=row.parent_task_id,
            trace_id=row.trace_id,
            agent_id=row.agent_id,
            task_type=ChildTaskType(row.task_type),
            protocol_version=row.protocol_version,
            status=ChildTaskStatus(row.status),
            input_artifacts=list(row.input_artifacts or []),
            required_output_types=list(row.required_output_types or []),
            deadline=ensure_utc(row.deadline),
            attempt=row.attempt,
            idempotency_key=row.idempotency_key,
            correlation_id=row.correlation_id,
            state_version=row.state_version,
        )
        context = {
            "base_commit": getattr(parent, "base_commit", ""),
            "target_branch": f"codepilot/{row.parent_task_id}",
            "mode": mode,
            "actor_id": getattr(parent, "actor_id", ""),
            "sandbox_available": bool(getattr(self.sandbox, "available", False)),
        }
        if ChildTaskType(row.task_type) is ChildTaskType.FIX:
            patch_version = PatchStore(session).next_version(row.parent_task_id)
            context["patch_version"] = patch_version
            context["patch_idempotency_key"] = (
                f"{row.parent_task_id}:fix:v{patch_version}"
            )
            comment_store = CommentStore(session)
            selected = comment_store.list_auto_fixable(
                row.parent_task_id, comment_ids=list(getattr(parent, "fixed_comment_ids", []) or [])
            )
            context["fixed_comment_refs"] = [
                f"{item.rule_id}@{item.file}:{item.line}" for item in selected
            ]
        return AgentRequest(
            task=task,
            workspace=workspace,
            tools=ToolGateway(self.tool_registry, tool_context),
            budget=budget,
            config=self.config,
            inputs=inputs,
            context=context,
            llm=self._llm_for(mode, budget),
            sandbox=self.sandbox,
            sandbox_limits=self.sandbox_limits,
        )

    def _llm_for(self, mode: str, budget: Budget) -> LLMGateway | None:
        """offline 模式不调用模型（宪法第二条）；其余模式使用受约束的生成层。"""
        if mode == str(RunMode.OFFLINE):
            return None
        gateway = LLMGateway(
            provider=self.llm_provider,
            model=self.llm_model,
            budget=budget,
        )
        if not gateway.available and not gateway.offline:
            return None
        return gateway

    def _load_workspace(self, parent_task_id: str, session: Session) -> Workspace:
        payload = self.content_store.load_workspace(parent_task_id)
        if payload is None:
            raise CodePilotError(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"工作区快照缺失：{parent_task_id}",
                details={"task_id": parent_task_id},
            )
        return Workspace.from_payload(payload)

    def _persist_comments(self, session: Session, task: Any, finding_row: Any, impact: ArtifactEnvelope) -> list[Any]:
        from a2a.protocol import FindingItem

        store = CommentStore(session)
        findings = [FindingItem.model_validate(item) for item in finding_row.data.get("findings", [])]
        scope_by_symbol: dict[str, str] = {}
        for symbol in impact.data.get("symbol_details", []):
            scope_by_symbol[symbol["name"]] = (
                f"{symbol['file']} 内 {symbol['kind']} {symbol['name']}（影响文件 "
                f"{len(impact.data.get('affected_files', []))} 个）"
            )
        citations = {
            path: [f"{path}:{line}" for line in sorted(task.input_summary.get("changed_files", []) or [])][:1]
            for path in {item.file for item in findings}
        }
        return store.add_findings(
            task_id=task.id,
            artifact_id=finding_row.id,
            findings=findings,
            impact_scope_by_symbol=scope_by_symbol,
            citations_by_file=citations,
        )

    def _park(
        self,
        session: Session,
        task_id: str,
        error: CodePilotError,
        *,
        summary_extra: dict[str, Any] | None = None,
    ) -> RunOutcome:
        target = error.default_parent_status or ParentTaskStatus.NEEDS_HUMAN
        if target is ParentTaskStatus.REJECTED:
            target = ParentTaskStatus.NEEDS_HUMAN
        store = ReviewTaskStore(session)
        task = store.get(task_id, required=False)
        if task is None:
            return RunOutcome(task_id=task_id, status=ParentTaskStatus.FAILED, error_code=str(error.code))

        try:
            result = store.apply_intent(
                task_id,
                expected_version=task.state_version,
                owner="coordinator",
                changes={
                    "status": str(target),
                    "error_code": str(error.code),
                    "error_message": error.message,
                    "current_step": "parked",
                    # 保留证据：质量门禁失败时也必须能看到 VerifyEvidence（FR-045/FR-046），
                    # 否则 Dashboard 与人工恢复都无法判断"为什么没通过"。
                    **({"summary": {**(task.summary or {}), **summary_extra}} if summary_extra else {}),
                },
                reason=f"error:{error.code}",
                idempotency_key=new_idempotency_key(task_id, "park", str(error.code)),
                event_type="task_failed" if target is ParentTaskStatus.FAILED else "task_needs_human",
            )
            session.commit()
            return RunOutcome(
                task_id=task_id,
                status=ParentTaskStatus(result.row.status),
                next_action="needs_human" if target is ParentTaskStatus.NEEDS_HUMAN else "failed",
                error_code=str(error.code),
                error_message=error.message,
            )
        except CodePilotError as nested:
            session.rollback()
            return RunOutcome(
                task_id=task_id,
                status=ParentTaskStatus(task.status),
                error_code=str(nested.code),
                error_message=nested.message,
            )


def _default_tool_registry() -> ToolRegistry:
    from tools import build_default_registry

    return build_default_registry()


def _default_handlers() -> dict[str, AgentHandler]:
    from agents import default_handlers

    return default_handlers()


def _artifact_envelope(
    artifact_store: ArtifactStore,
    task_id: str,
    artifact_type: ArtifactType,
    *,
    remote_task_id: str | None = None,
) -> ArtifactEnvelope | None:
    """从子任务产物中取指定类型（已验证）的信封。

    拆分形态（HTTP A2A）下，Agent 服务把产物写在**远端子任务 ID** 下，
    Coordinator 本地行只有 ``remote_task_id`` 指向它；因此两个 ID 都要查，
    否则 Fix/Verify 阶段会误判为"子任务未返回产物"。
    """
    candidates = [task_id] + ([remote_task_id] if remote_task_id and remote_task_id != task_id else [])
    for candidate in candidates:
        for row in artifact_store.list_by_task(candidate):
            if row.artifact_type == str(artifact_type) and row.validated:
                return artifact_store.to_envelope(row)
    return None


def _gate_failure_reason(evidence: VerifyEvidence) -> ErrorCode:
    """把质量门禁失败映射为统一错误码（FR-045/FR-046）。"""
    if not evidence.pre_scan.ok:
        return ErrorCode.SANDBOX_VIOLATION
    if evidence.test_gap:
        return ErrorCode.TEST_GAP
    if not evidence.apply_check.ok or not evidence.lint.ok:
        return ErrorCode.QUALITY_GATE_FAILED
    if not evidence.unit_tests.ok or not evidence.regression.ok:
        return ErrorCode.QUALITY_GATE_FAILED
    if evidence.coverage.delta is not None and evidence.coverage.delta <= -0.05:
        return ErrorCode.QUALITY_GATE_FAILED
    return ErrorCode.QUALITY_GATE_FAILED


def _default_sandbox() -> SandboxExecutor:
    from sandbox import default_executor

    return default_executor()


def _supports_parallel(session_factory: sessionmaker[Session]) -> bool:
    """并行子任务只在 PostgreSQL 上启用；SQLite 单写者会引入锁竞争。"""
    try:
        bind = session_factory.kw["bind"] if "bind" in session_factory.kw else session_factory().get_bind()
    except Exception:  # noqa: BLE001
        return False
    return bind.dialect.name == "postgresql"


def tool_specs() -> list[ToolSpec]:  # pragma: no cover - 便于测试与文档
    from tools import build_default_registry

    registry = build_default_registry()
    return [registry.get(name) for name in registry.names()]


def child_pipeline() -> tuple[ChildTaskType, ...]:
    """当前阶段的子任务流水线（阶段六加入 Fix / Verify）。"""
    return CHILD_PIPELINE


__all__ = [
    "CHILD_PIPELINE",
    "Coordinator",
    "ReviewRequest",
    "RunOutcome",
    "tool_specs",
]
