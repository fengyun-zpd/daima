"""故障注入矩阵（docs/06 §2、SRS §11.4、宪法第七条与第八条）。

13 个必测场景，每个场景都返回可判定的结论与观测到的错误码：
重复提交、子任务超时、SSE 断线、Coordinator 重启、非法 Artifact、错误 content_hash、
Agent Card 能力缺失、协议版本不兼容、Review 越权调用写工具、未审批合并、
沙箱外网访问、沙箱提权、非法状态迁移。

除沙箱两项外都不需要 Docker，可用于 CI 与本地快速回归。
"""

from __future__ import annotations

import base64
import io
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from a2a.http_invoker import A2AInvoker
from apps.api.agent_service import FaultInjector
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import (
    ActorRole,
    ArtifactType,
    ChildTaskStatus,
    ContextPolicy,
    InputType,
    ParentTaskStatus,
    RunMode,
)
from domain.errors import CodePilotError, ErrorCode
from domain.ids import new_child_task_id  # noqa: F401  (故障场景构造子任务载荷)
from repositories.content_store import ContentStore
from repositories.models import A2AArtifact, Approval, AuditEvent
from repositories.records import PatchStore
from repositories.store import A2ATaskStore, ReviewTaskStore
from sandbox.base import SandboxResult

PROJECT = {
    "app/__init__.py": "",
    "app/main.py": (
        '"""Synthetic module."""\n'
        "import os\n"
        "import subprocess\n"
        "\n"
        'API_KEY = "sk-live-abcdef123456"\n'
        "\n"
        "\n"
        "def run(directory):\n"
        '    return subprocess.run("ls " + directory, shell=True)\n'
    ),
    "tests/test_main.py": (
        "import os\n\nos.environ.setdefault('API_KEY', 'test-key')\n\n\n"
        "def test_imports():\n    import app.main  # noqa: F401\n"
    ),
}

GOOD_SANDBOX_OUTPUT = (
    "CODEPILOT_APPLY_CHECK_OK\nCODEPILOT_APPLY_OK\nCODEPILOT_LINT_OK\n"
    "TOTAL 12 2 83%\n2 passed in 0.05s\n"
)


class _StubSandbox:
    available = True
    unavailable_reason = ""

    def run(self, *, files, argv, name, limits, patch=None) -> SandboxResult:
        return SandboxResult(status="passed", exit_code=0, stdout=GOOD_SANDBOX_OUTPUT)


class UvicornThread:
    """在后台线程运行真实 HTTP 服务（Agent 侧故障注入需要真实传输）。"""

    def __init__(self, app: FastAPI) -> None:
        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> str:
        self.thread.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if getattr(self.server, "started", False):
                for server in getattr(self.server, "servers", []):
                    for sock in server.sockets:
                        host, port = sock.getsockname()[:2]
                        return f"http://{host}:{port}"
            time.sleep(0.05)
        raise RuntimeError("uvicorn 未能在 30 秒内启动")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)


@dataclass
class FaultOutcome:
    name: str
    passed: bool
    detail: str = ""
    observed_code: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "observed_code": self.observed_code,
        }


@dataclass
class FaultContext:
    """故障矩阵的运行上下文（每个场景独立使用一个容器，避免相互污染）。"""

    tmp_path: Path
    database_url: str
    container: Any = None
    app: FastAPI | None = None
    client: TestClient | None = None
    server: Any = None
    include_sandbox: bool = False

    def new_container(
        self, *, config: CodePilotConfig | None = None, use_http: bool = False
    ) -> Any:
        store = ContentStore(self.tmp_path / "var")
        container = build_container(
            url=self.database_url,
            auto_create=True,
            config=config or CodePilotConfig.model_validate({"mode": "a2a"}),
        )
        container.content_store = store
        container.coordinator.content_store = store
        container.coordinator.repo_root = store.root
        if not self.include_sandbox:
            container.coordinator.sandbox = _StubSandbox()
        self.container = container
        self.app = create_app(
            container=container,
            recover_on_start=False,
            schedule_on_create=False,
            include_internal=True,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        if use_http:
            # 故障注入针对 Agent 侧服务，必须走真实 HTTP 传输（FR-092）。
            self.server = UvicornThread(self.app)
            base_url = self.server.start()
            container.coordinator.invoker = A2AInvoker(registry=container.registry, base_url=base_url)
        return container

    def close(self) -> None:
        invoker = getattr(self.container.coordinator, "invoker", None) if self.container else None
        if isinstance(invoker, A2AInvoker):
            invoker.close()
        if self.server is not None:
            self.server.stop()
            self.server = None
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None


#: 本次运行批次标识：故障矩阵允许对同一数据库重复执行，幂等键必须按批次唯一。
RUN_TOKEN = uuid.uuid4().hex[:8]

FIXED_ZIP_TIME = (2026, 9, 13, 0, 0, 0)


def fault_key(name: str) -> str:
    """生成本次运行唯一的幂等键（同一批次内保持稳定）。"""
    return f"fault-{name}-{RUN_TOKEN}"


def build_zip(project: dict[str, str] | None = None) -> str:
    """确定性 ZIP：固定时间戳，避免同一输入产生不同哈希（NFR-005）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in (project or PROJECT).items():
            info = zipfile.ZipInfo(path, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def create_review(container, *, key: str, mode: RunMode = RunMode.A2A) -> str:
    from agents.coordinator.coordinator import ReviewRequest

    task, _ = container.coordinator.create_review(
        ReviewRequest(
            input_type=InputType.ZIP,
            content=build_zip(),
            base_commit="synthetic-base-001",
            actor_id="fault-runner",
            actor_role=str(ActorRole.DEVELOPER),
            idempotency_key=key,
            context_policy=ContextPolicy.FUNCTION,
            mode=mode,
        )
    )
    return task.id


def set_faults(context: FaultContext, **kwargs) -> None:
    context.app.state.agent_service.faults = FaultInjector(enabled=True, **kwargs)


# ---------------------------------------------------------------------------
# 场景实现
# ---------------------------------------------------------------------------


def scenario_duplicate_submission(context: FaultContext) -> FaultOutcome:
    container = context.new_container()
    first = create_review(container, key=fault_key("dup"))
    second = create_review(container, key=fault_key("dup"))
    container.coordinator.run(first)
    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(first)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == first
        ]
    same_task = first == second
    no_duplicates = len(children) == len({(row.agent_id, row.task_type) for row in children})
    unique_artifacts = len(artifacts) == len({(row.artifact_type, row.content_hash) for row in artifacts})
    passed = same_task and no_duplicates and unique_artifacts
    return FaultOutcome(
        "重复提交",
        passed,
        f"task_id 一致={same_task} 子任务无重复={no_duplicates} 产物无重复={unique_artifacts}",
    )


def scenario_child_timeout(context: FaultContext) -> FaultOutcome:
    # 超时本身由 ``fail_times=1`` 确定性注入（review-agent 第一次执行返回 TASK_TIMEOUT），
    # 子任务 deadline 只是兜底。历史上这里设成 1 秒，遇到机器负载高时第二次尝试也会
    # 真的超时，导致场景偶发失败；因此放宽到 5 秒，让"重试一次后收敛"成为确定性结论。
    container = context.new_container(
        config=CodePilotConfig.model_validate({"mode": "a2a", "a2a": {"child_task_timeout_seconds": 5}}),
        use_http=True,
    )
    set_faults(context, fail_with=str(ErrorCode.TASK_TIMEOUT), agent_id="review-agent", fail_times=1)
    task_id = create_review(container, key=fault_key("timeout"))
    outcome = container.coordinator.run(task_id)
    with container.session() as session:
        review_rows = [row for row in A2ATaskStore(session).list_by_parent(task_id) if row.task_type == "review"]
    attempts = [f"{row.id.split('-')[0]}:a{row.attempt}:{row.status}:{row.error_code or '-'}" for row in review_rows]
    retried = len(review_rows) == 1 and review_rows[0].attempt == 2
    converged = outcome.status in {
        ParentTaskStatus.REVIEWED,
        ParentTaskStatus.PENDING_APPROVAL,
        ParentTaskStatus.NEEDS_HUMAN,
    }
    return FaultOutcome(
        "子任务超时",
        retried and converged,
        f"attempts={attempts} 最终状态={outcome.status} 错误码={outcome.error_code}",
    )


def scenario_sse_disconnect(context: FaultContext) -> FaultOutcome:
    container = context.new_container(use_http=True)
    set_faults(context, drop_events=True)
    task_id = create_review(container, key=fault_key("sse"))
    outcome = container.coordinator.run(task_id)
    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
    completed = all(ChildTaskStatus(row.status) is ChildTaskStatus.COMPLETED for row in children)
    return FaultOutcome(
        "SSE 断线",
        outcome.status is ParentTaskStatus.REVIEWED and completed,
        f"最终状态={outcome.status} 子任务={[row.status for row in children]}",
    )


def scenario_coordinator_restart(context: FaultContext) -> FaultOutcome:
    container = context.new_container()
    task_id = create_review(container, key=fault_key("restart"))
    container.coordinator.run(task_id)
    with container.session() as session:
        before_artifacts = sorted(
            row.id
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        )
        before_children = sorted(row.id for row in A2ATaskStore(session).list_by_parent(task_id))

    restarted = build_container(url=container.database_url, auto_create=False, config=container.config)
    restarted.content_store = container.content_store
    restarted.coordinator.content_store = container.content_store
    restarted.coordinator.sandbox = _StubSandbox()
    restarted.coordinator.recover()
    with restarted.session() as session:
        after_artifacts = sorted(
            row.id
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        )
        after_children = sorted(row.id for row in A2ATaskStore(session).list_by_parent(task_id))
        task = ReviewTaskStore(session).get(task_id)
    passed = (
        before_artifacts == after_artifacts
        and before_children == after_children
        and task.status == str(ParentTaskStatus.REVIEWED)
    )
    return FaultOutcome(
        "Coordinator 重启",
        passed,
        f"产物 {len(after_artifacts)} 个、子任务 {len(after_children)} 个未重放，状态={task.status}",
    )


def scenario_invalid_artifact(context: FaultContext) -> FaultOutcome:
    container = context.new_container(use_http=True)
    set_faults(context, agent_id="impact-agent", fail_with=str(ErrorCode.ARTIFACT_SCHEMA_INVALID), fail_times=2)
    task_id = create_review(container, key=fault_key("artifact"))
    outcome = container.coordinator.run(task_id)
    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
        artifacts = [
            row
            for row in session.execute(select(A2AArtifact)).scalars()
            if row.parent_task_id == task_id
        ]
    failed = [row for row in children if ChildTaskStatus(row.status) is ChildTaskStatus.FAILED]
    passed = (
        outcome.status is ParentTaskStatus.NEEDS_HUMAN
        and bool(failed)
        and failed[0].error_code == str(ErrorCode.ARTIFACT_SCHEMA_INVALID)
        and not [row for row in artifacts if row.artifact_type == str(ArtifactType.IMPACT_REPORT) and row.validated]
    )
    return FaultOutcome(
        "非法 Artifact",
        passed,
        f"最终状态={outcome.status} 子任务错误={failed[0].error_code if failed else 'n/a'}",
        failed[0].error_code if failed else None,
    )


def scenario_hash_mismatch(context: FaultContext) -> FaultOutcome:
    """篡改内容但不改 content_hash：必须命中 ARTIFACT_HASH_MISMATCH。"""
    container = context.new_container(use_http=True)
    set_faults(context, agent_id="impact-agent", tamper_artifact=True)
    task_id = create_review(container, key=fault_key("hash"))
    outcome = container.coordinator.run(task_id)
    with container.session() as session:
        children = A2ATaskStore(session).list_by_parent(task_id)
    failed = [row for row in children if ChildTaskStatus(row.status) is ChildTaskStatus.FAILED]
    codes = {row.error_code for row in failed}
    passed = outcome.status is ParentTaskStatus.NEEDS_HUMAN and codes <= {
        str(ErrorCode.ARTIFACT_HASH_MISMATCH),
        str(ErrorCode.ARTIFACT_SCHEMA_INVALID),
    } and bool(codes)
    return FaultOutcome("Artifact 哈希错误", passed, f"错误码={sorted(codes)}", ",".join(sorted(codes)))


def scenario_capability_missing(context: FaultContext) -> FaultOutcome:
    container = context.new_container()
    card = context.app.state.agent_service.registry.get("impact-agent")
    weakened = card.model_copy(update={"capabilities": ["impact.symbols"], "card_version": "1.1"})
    context.app.state.agent_service.registry._cards["impact-agent"] = weakened
    task_id = create_review(container, key=fault_key("card"))
    outcome = container.coordinator.run(task_id)
    with container.session() as session:
        events = session.execute(select(AuditEvent)).scalars().all()
    recorded = any(
        event.event_type == "agent_card_resolved"
        and (event.after_state or {}).get("negotiation_errors")
        for event in events
    )
    passed = (
        outcome.status is ParentTaskStatus.NEEDS_HUMAN
        and outcome.error_code
        in {str(ErrorCode.CAPABILITY_NOT_AVAILABLE), str(ErrorCode.AGENT_CARD_INVALID)}
        and recorded
    )
    return FaultOutcome(
        "Agent Card 能力缺失",
        passed,
        f"最终状态={outcome.status} 错误码={outcome.error_code} 协商记录={recorded}",
        outcome.error_code,
    )


def scenario_protocol_version(context: FaultContext) -> FaultOutcome:
    context.new_container()
    response = context.client.get(
        "/internal/a2a/agents",
        headers={
            "X-Actor-Id": "coordinator",
            "X-Actor-Role": "coordinator",
            "X-A2A-Protocol-Version": "0.9",
        },
    )
    body = response.json()
    passed = response.status_code == 400 and body.get("code") == str(ErrorCode.PROTOCOL_VERSION_UNSUPPORTED)
    return FaultOutcome("协议版本不兼容", passed, f"HTTP {response.status_code} code={body.get('code')}", body.get("code"))


def scenario_unauthorized_tool(context: FaultContext) -> FaultOutcome:
    container = context.new_container()
    task_id = create_review(container, key=fault_key("tool"))
    container.coordinator.run(task_id)
    from tools.registry import ToolCallContext

    with container.session() as session:
        row = A2ATaskStore(session).list_by_parent(task_id)[0]
        workspace = container.coordinator._load_workspace(task_id, session)
        ctx = ToolCallContext(
            task_id=row.id,
            parent_task_id=task_id,
            agent_id="review-agent",
            actor_role="agent",
            trace_id=row.trace_id,
            workspace=workspace,
            session=session,
            budget=__import__("domain.budget", fromlist=["Budget"]).Budget(),
            sandbox=_StubSandbox(),
            sandbox_limits=__import__("sandbox.base", fromlist=["SandboxLimits"]).SandboxLimits(),
        )
        try:
            container.tool_registry.call(
                ctx,
                "write_patch",
                {
                    "patch_id": "patch-00000000001",
                    "patch_version": 1,
                    "patch_hash": "sha256:" + "a" * 64,
                    "diff": "--- a/x\n+++ b/x\n",
                    "target_branch": f"codepilot/{task_id}",
                },
                idempotency_key="fault-tool-call-01",
            )
            passed, code = False, None
        except CodePilotError as exc:
            passed, code = exc.code in {ErrorCode.PERMISSION_DENIED, ErrorCode.FORBIDDEN}, str(exc.code)
    return FaultOutcome("Review 越权调用写工具", passed, f"错误码={code}", code)


def scenario_unapproved_merge(context: FaultContext) -> FaultOutcome:
    container = context.new_container()
    task_id = create_review(container, key=fault_key("merge"))
    container.coordinator.run(task_id)
    container.coordinator.run(task_id, auto_fix=True)
    with container.session() as session:
        patch = PatchStore(session).latest(task_id)
        task = ReviewTaskStore(session).get(task_id)
    if patch is None or task.status != str(ParentTaskStatus.PENDING_APPROVAL):
        return FaultOutcome("未审批合并", False, f"前置条件不满足：patch={patch} status={task.status}")
    try:
        container.coordinator.merge(
            patch_id=patch.id,
            patch_version=patch.patch_version,
            actor_id="approver-1",
            actor_role=str(ActorRole.APPROVER),
        )
        code, passed = None, False
    except CodePilotError as exc:
        code, passed = str(exc.code), exc.code is ErrorCode.FORBIDDEN
    with container.session() as session:
        after = PatchStore(session).get(patch.id)
        approvals = session.execute(select(Approval)).scalars().all()
    passed = passed and after.status != "applied" and not approvals
    return FaultOutcome("未审批合并", passed, f"错误码={code} 补丁状态={after.status}", code)


def scenario_illegal_transition(context: FaultContext) -> FaultOutcome:
    container = context.new_container()
    task_id = create_review(container, key=fault_key("state"))
    with container.session() as session:
        store = ReviewTaskStore(session)
        task = store.get(task_id)
        try:
            store.apply_intent(
                task_id,
                expected_version=task.state_version,
                owner="coordinator",
                changes={"status": str(ParentTaskStatus.MERGED)},
                reason="skip_approval",
                idempotency_key="fault-state-intent-01",
            )
            code, passed = None, False
        except CodePilotError as exc:
            code, passed = str(exc.code), exc.code is ErrorCode.ILLEGAL_STATE_TRANSITION
        session.rollback()
    return FaultOutcome("非法状态迁移", passed, f"错误码={code}", code)


def scenario_sandbox_network(context: FaultContext) -> FaultOutcome:
    """沙箱必须无网络（FR-051）。"""
    from sandbox.base import SandboxLimits
    from sandbox.docker import DockerSandbox

    sandbox = DockerSandbox()
    if not sandbox.available:
        return FaultOutcome("沙箱外网访问", False, f"沙箱不可用：{sandbox.unavailable_reason}")
    result = sandbox.run(
        files={"app/__init__.py": ""},
        argv=[
            "/bin/sh",
            "-c",
            "python -c \"import socket;socket.setdefaulttimeout(3);"
            "socket.create_connection(('1.1.1.1',80));print('NETWORK_OPEN')\" 2>&1 || echo NETWORK_BLOCKED",
        ],
        name="fault-network",
        limits=SandboxLimits(timeout_seconds=30),
    )
    passed = "NETWORK_OPEN" not in result.stdout
    return FaultOutcome("沙箱外网访问", passed, result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "")


def scenario_sandbox_privilege(context: FaultContext) -> FaultOutcome:
    """沙箱必须非 root、丢弃 capabilities、只读根文件系统（FR-056~FR-058）。"""
    from sandbox.base import SandboxLimits
    from sandbox.docker import DockerSandbox

    sandbox = DockerSandbox()
    if not sandbox.available:
        return FaultOutcome("沙箱提权", False, f"沙箱不可用：{sandbox.unavailable_reason}")
    limits = SandboxLimits(timeout_seconds=30)
    probe = sandbox.run(
        files={"app/__init__.py": ""},
        argv=[
            "/bin/sh",
            "-c",
            "id -u; (mount -t tmpfs none /mnt 2>/dev/null && echo MOUNT_OK) || echo MOUNT_DENIED; "
            "(touch /etc/probe && echo ROOTFS_WRITABLE) || echo ROOTFS_READONLY",
        ],
        name="fault-privilege",
        limits=limits,
    )
    uid = probe.stdout.splitlines()[0].strip() if probe.stdout.strip() else ""
    passed = uid.isdigit() and uid != "0" and "MOUNT_OK" not in probe.stdout and "ROOTFS_WRITABLE" not in probe.stdout
    return FaultOutcome(
        "沙箱提权", passed, f"uid={uid} mount={'OK' if 'MOUNT_OK' in probe.stdout else 'DENIED'}"
    )


SCENARIOS: list[tuple[str, Callable[[FaultContext], FaultOutcome], bool]] = [
    ("重复提交", scenario_duplicate_submission, False),
    ("子任务超时", scenario_child_timeout, False),
    ("SSE 断线", scenario_sse_disconnect, False),
    ("Coordinator 重启", scenario_coordinator_restart, False),
    ("非法 Artifact", scenario_invalid_artifact, False),
    ("Artifact 哈希错误", scenario_hash_mismatch, False),
    ("Agent Card 能力缺失", scenario_capability_missing, False),
    ("协议版本不兼容", scenario_protocol_version, False),
    ("Review 越权调用写工具", scenario_unauthorized_tool, False),
    ("未审批合并", scenario_unapproved_merge, False),
    ("非法状态迁移", scenario_illegal_transition, False),
    ("沙箱外网访问", scenario_sandbox_network, True),
    ("沙箱提权", scenario_sandbox_privilege, True),
]


def run_matrix(
    *,
    tmp_path: Path,
    database_url: str,
    include_sandbox: bool = True,
    only: list[str] | None = None,
) -> list[FaultOutcome]:
    """运行故障矩阵；每个场景使用独立容器，互不影响。"""
    outcomes: list[FaultOutcome] = []
    for name, scenario, needs_sandbox in SCENARIOS:
        if only and name not in only:
            continue
        if needs_sandbox and not include_sandbox:
            continue
        context = FaultContext(
            tmp_path=tmp_path / name.replace(" ", "_"),
            database_url=database_url,
            include_sandbox=include_sandbox,
        )
        context.tmp_path.mkdir(parents=True, exist_ok=True)
        try:
            outcomes.append(scenario(context))
        except Exception as exc:  # noqa: BLE001 - 矩阵必须完整报告，而不是中断
            outcomes.append(FaultOutcome(name, False, f"场景异常：{type(exc).__name__}: {exc}"))
        finally:
            context.close()
    return outcomes


__all__ = ["SCENARIOS", "FaultContext", "FaultOutcome", "build_zip", "run_matrix"]
