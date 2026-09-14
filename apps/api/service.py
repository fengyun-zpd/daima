"""审查服务：创建、调度、查询与事件流（FR-001~FR-003、NFR-001）。"""

from __future__ import annotations

import asyncio
from typing import Any

from agents.coordinator.coordinator import ReviewRequest, RunOutcome
from apps.api.deps import Actor, AppContainer
from apps.api.idempotency import execute_idempotent
from apps.api.schemas import (
    ApprovalRequest,
    CreatedReviewResponse,
    CreateReviewRequest,
    EvalRunRequest,
    MergeRequest,
    PatchResponse,
)
from apps.api.serializers import patch_response, review_detail, task_response
from domain.enums import ActorRole, ParentTaskStatus
from domain.errors import CodePilotError, ErrorCode
from repositories.idempotency import (
    COMMAND_APPROVAL,
    COMMAND_EVAL_RUN,
    COMMAND_MERGE,
    COMMAND_TRIGGER_FIX,
)
from repositories.records import ApprovalStore, EvalStore, PatchStore
from repositories.store import ReviewTaskStore

MAX_CONCURRENT_TASKS = 5


class ReviewService:
    """把 API 请求翻译成 Coordinator 调用，并管理后台执行。"""

    def __init__(self, container: AppContainer) -> None:
        self.container = container
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._eval_runs: dict[str, asyncio.Task[Any]] = {}

    # ---- 创建与调度 --------------------------------------------------------------
    def create(
        self, request: CreateReviewRequest, actor: Actor, *, idempotency_key: str
    ) -> CreatedReviewResponse:
        mode = request.mode or self.container.config.mode
        review_request = ReviewRequest(
            input_type=request.input_type,
            content=request.content,
            base_commit=request.base_commit,
            actor_id=actor.actor_id,
            actor_role=str(actor.role),
            idempotency_key=idempotency_key,
            context_policy=request.context_policy,
            mode=mode,
        )
        task, created = self.container.coordinator.create_review(review_request)
        with self.container.session() as session:
            response = task_response(ReviewTaskStore(session).get(task.id))
        return CreatedReviewResponse(
            task=response,
            created=created,
            detail_url=f"/api/v1/reviews/{task.id}",
        )

    def schedule(self, task_id: str) -> None:
        """在后台线程执行编排；并发上限 5（NFR-001）。"""
        if task_id in self._running and not self._running[task_id].done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 非异步上下文（测试直接调用）
            self.run_sync(task_id)
            return
        runner = loop.create_task(self._run_bounded(task_id))
        self._running[task_id] = runner
        runner.add_done_callback(lambda _task: self._running.pop(task_id, None))

    async def _run_bounded(self, task_id: str) -> RunOutcome | None:
        async with self._semaphore:
            return await asyncio.to_thread(self.run_sync, task_id)

    def run_sync(self, task_id: str) -> RunOutcome:
        return self.container.coordinator.run(task_id)

    async def wait_until_settled(self, task_id: str, *, timeout: float = 60.0) -> None:
        """等待后台编排完成（供测试与运维使用）。"""
        runner = self._running.get(task_id)
        if runner is not None:
            await asyncio.wait_for(asyncio.shield(runner), timeout=timeout)

    # ---- Fix / Verify 触发（阶段六） ----------------------------------------------
    def trigger_fix(self, task_id: str, actor: Actor, *, idempotency_key: str) -> dict[str, Any]:
        """开发者选定可修复意见后触发 Fix/Verify 阶段（SRS §4.1）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原响应且不重复调度；
        不同键在补丁已存在时也复用同一个补丁版本（不会产生第二个补丁）。
        """

        def _work() -> dict[str, Any]:
            with self.container.session() as session:
                task = ReviewTaskStore(session).get(task_id)
                status = ParentTaskStatus(task.status)
                patch = PatchStore(session).latest(task_id)
            if status not in {
                ParentTaskStatus.REVIEWED,
                ParentTaskStatus.FIXING,
                ParentTaskStatus.TESTING,
            }:
                raise CodePilotError(
                    ErrorCode.ILLEGAL_STATE_TRANSITION,
                    f"当前状态 {status} 不允许触发修复（需要 REVIEWED/FIXING/TESTING）",
                    details={"status": str(status)},
                )
            self._schedule_fix(task_id)
            return {
                "task_id": task_id,
                "status": str(status),
                "patch_id": patch.id if patch else None,
                "detail_url": f"/api/v1/reviews/{task_id}",
            }

        return execute_idempotent(
            self.container.session_factory,
            actor_id=actor.actor_id,
            actor_role=str(actor.role),
            command_type=COMMAND_TRIGGER_FIX,
            aggregate_ref=task_id,
            idempotency_key=idempotency_key,
            request={"task_id": task_id},
            work=_work,
        )

    def _schedule_fix(self, task_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 非异步上下文
            self.container.coordinator.run(task_id, auto_fix=True)
            return
        runner = loop.create_task(self._run_fix_bounded(task_id))
        self._running[task_id] = runner
        runner.add_done_callback(lambda _task: self._running.pop(task_id, None))

    async def _run_fix_bounded(self, task_id: str) -> RunOutcome:
        async with self._semaphore:
            return await asyncio.to_thread(self.container.coordinator.run, task_id, auto_fix=True)

    def get_patch(self, patch_id: str) -> PatchResponse:
        with self.container.session() as session:
            patch = PatchStore(session).get(patch_id)
            approvals = ApprovalStore(session).list_by_task(patch.task_id)
        return patch_response(patch, approvals)

    def list_patches(self, task_id: str) -> list[PatchResponse]:
        with self.container.session() as session:
            ReviewTaskStore(session).get(task_id)
            patches = PatchStore(session).list_by_task(task_id)
            approvals = ApprovalStore(session).list_by_task(task_id)
        return [patch_response(item, approvals) for item in patches]

    # ---- 审批与合并（阶段七） ------------------------------------------------------
    def decide(
        self,
        patch_id: str,
        request: ApprovalRequest,
        actor: Actor,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """记录审批决定（仅 approver；版本必须匹配；拒绝必须填写原因）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原审批结果，不产生第二条审批记录。
        """
        if actor.role is not ActorRole.APPROVER:
            raise CodePilotError(
                ErrorCode.FORBIDDEN,
                f"仅 approver 可以审批，当前角色：{actor.role}（FR-071）",
                details={"role": str(actor.role)},
            )
        return execute_idempotent(
            self.container.session_factory,
            actor_id=actor.actor_id,
            actor_role=str(actor.role),
            command_type=COMMAND_APPROVAL,
            aggregate_ref=patch_id,
            idempotency_key=idempotency_key,
            request={
                "patch_id": patch_id,
                "decision": str(request.decision),
                "patch_version": request.patch_version,
                "reason": request.reason,
            },
            work=lambda: self.container.coordinator.record_approval(
                patch_id=patch_id,
                decision=request.decision,
                patch_version=request.patch_version,
                reason=request.reason,
                actor_id=actor.actor_id,
                actor_role=str(actor.role),
            ),
        )

    def merge(
        self,
        patch_id: str,
        request: MergeRequest,
        actor: Actor,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """审批通过后写入任务分支（未审批一律拒绝）。

        幂等：同一 ``Idempotency-Key`` 重复提交返回原合并结果（分支与 commit），
        既不重复写入分支，也不以"状态已变化"为由报错。
        """
        if actor.role not in {ActorRole.APPROVER, ActorRole.ADMIN}:
            raise CodePilotError(
                ErrorCode.FORBIDDEN,
                f"角色 {actor.role} 不允许合并（需要 approver/admin）",
                details={"role": str(actor.role)},
            )
        return execute_idempotent(
            self.container.session_factory,
            actor_id=actor.actor_id,
            actor_role=str(actor.role),
            command_type=COMMAND_MERGE,
            aggregate_ref=patch_id,
            idempotency_key=idempotency_key,
            request={"patch_id": patch_id, "patch_version": request.patch_version},
            work=lambda: self.container.coordinator.merge(
                patch_id=patch_id,
                patch_version=request.patch_version,
                actor_id=actor.actor_id,
                actor_role=str(actor.role),
            ),
        )

    # ---- 评测（阶段七） -----------------------------------------------------------
    def run_evals(
        self, request: EvalRunRequest, actor: Actor, *, idempotency_key: str
    ) -> dict[str, Any]:
        """启动黄金集评测（后台执行）；返回 run_id 供轮询。

        幂等：同一 ``Idempotency-Key`` 重复提交返回**同一个 run_id**，不重复启动评测。
        """
        from evals.golden_v1 import load_cases
        from evals.runner import EvalRunner

        cases = load_cases()
        if request.case_limit:
            cases = cases[: request.case_limit]
        runs_per_case = request.runs_per_case or self.container.config.evaluation.runs_per_case

        def _work() -> dict[str, Any]:
            runner = EvalRunner(self.container.coordinator, content_store=self.container.content_store)
            run_id = self.container.coordinator.new_eval_run_id()
            # 先落库再异步执行：调用方立刻可以查询 running 状态（避免竞态 404）。
            runner.create_run(run_id=run_id, modes=list(request.modes), runs_per_case=runs_per_case)
            self._schedule_eval(
                runner,
                run_id=run_id,
                modes=list(request.modes),
                runs_per_case=runs_per_case,
                case_limit=request.case_limit,
                with_fix=request.with_fix,
            )
            return {
                "run_id": run_id,
                "dataset": request.dataset,
                "modes": [str(mode) for mode in request.modes],
                "cases": len(cases),
                "with_fix": request.with_fix,
                "status": "running",
            }

        return execute_idempotent(
            self.container.session_factory,
            actor_id=actor.actor_id,
            actor_role=str(actor.role),
            command_type=COMMAND_EVAL_RUN,
            aggregate_ref=f"{request.dataset}:{','.join(str(mode) for mode in request.modes)}",
            idempotency_key=idempotency_key,
            request={
                "dataset": request.dataset,
                "modes": [str(mode) for mode in request.modes],
                "runs_per_case": runs_per_case,
                "case_limit": request.case_limit,
                "with_fix": request.with_fix,
            },
            work=_work,
        )

    def _schedule_eval(self, runner, *, run_id: str, modes, runs_per_case: int, case_limit, with_fix: bool) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 非异步上下文
            runner.run(
                modes=modes, runs_per_case=runs_per_case, case_limit=case_limit, with_fix=with_fix, run_id=run_id
            )
            return
        task = loop.create_task(
            asyncio.to_thread(
                runner.run,
                modes=modes,
                runs_per_case=runs_per_case,
                case_limit=case_limit,
                with_fix=with_fix,
                run_id=run_id,
            )
        )
        self._eval_runs[run_id] = task

    def get_eval_run(self, run_id: str) -> dict[str, Any]:
        with self.container.session() as session:
            row = EvalStore(session).get_run(run_id)
            if row is None:
                raise CodePilotError(
                    ErrorCode.RESOURCE_NOT_FOUND, f"评测运行不存在：{run_id}", details={"run_id": run_id}
                )
            results = EvalStore(session).list_results(run_id)
        report = None
        if row.report_ref and self.container.content_store.exists(row.report_ref):
            report = self.container.content_store.read_json(row.report_ref)
        return {
            "run_id": row.id,
            "dataset": row.dataset,
            "status": row.status,
            "modes": list(row.modes or []),
            "runs_per_case": row.runs_per_case,
            "summary": row.summary or {},
            "report_ref": row.report_ref,
            "report": report,
            "results": [
                {
                    "case_id": item.case_id,
                    "mode": item.mode,
                    "run_index": item.run_index,
                    "passed": item.passed,
                    "finding_recall": item.finding_recall,
                    "finding_precision": item.finding_precision,
                    "route_correct": item.route_correct,
                    "artifact_schema_ok": item.artifact_schema_ok,
                    "task_converged": item.task_converged,
                    "trace_complete": item.trace_complete,
                    "latency_ms": item.latency_ms,
                    "security_invariants": item.security_invariants,
                }
                for item in results
            ],
        }

    # ---- 查询 -------------------------------------------------------------------
    def get(self, task_id: str) -> dict[str, Any]:
        with self.container.session() as session:
            detail = review_detail(session, task_id)
        return detail.model_dump(mode="json")

    def get_task(self, task_id: str):
        with self.container.session() as session:
            return task_response(ReviewTaskStore(session).get(task_id))

    def require_open_task(self, task_id: str) -> None:
        with self.container.session() as session:
            task = ReviewTaskStore(session).get(task_id)
        if ParentTaskStatus(task.status) in {
            ParentTaskStatus.MERGED,
            ParentTaskStatus.REJECTED,
            ParentTaskStatus.FAILED,
        }:
            raise CodePilotError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                f"任务已处于终态 {task.status}",
                details={"status": task.status},
            )


__all__ = ["MAX_CONCURRENT_TASKS", "ReviewService"]
