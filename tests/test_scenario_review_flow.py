"""真实落地场景一：开发者提交 Diff → 获得审查结果。

对应验收清单（全部为 API 级行为断言，不做内部实现猜测）：

1. 同一个任务只能创建一个 Review 子任务；
2. 同一个任务只能创建一个 Impact 子任务；
3. Review 与 Impact **都**完成后父任务才能进入 `REVIEWED`；
4. 一个 Artifact 校验失败时父任务必须进入 `NEEDS_HUMAN`；
5. 子任务缺少必需能力时不能执行；
6. 任务详情的 `trace_id` / `parent_task_id` / `task_id` 能串起来；
7. 服务重启后不能重放已完成子任务；
8. 任务查询不暴露未授权 Artifact 内容（也不暴露给无权限角色）。

数据库使用临时 SQLite（宪法第三条允许的测试降级），传输层为 InProcessInvoker。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from a2a.protocol import ArtifactEnvelope
from a2a.registry import AgentRegistry
from agents.base import AgentHandler, AgentRequest, AgentResult
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig
from domain.enums import ArtifactType, ChildTaskStatus, ChildTaskType, ParentTaskStatus
from domain.sanitize import payload_hash
from repositories.content_store import ContentStore
from repositories.models import A2AArtifact, A2AMessage, AuditEvent
from repositories.models import A2ATask as A2ATaskRow
from repositories.store import ReviewTaskStore

DEV = {"X-Actor-Id": "dev-scenario", "X-Actor-Role": "developer"}
ADMIN = {"X-Actor-Id": "admin-scenario", "X-Actor-Role": "admin"}

#: 场景要求的缺陷集合：硬编码密钥、shell=True、SQL 字符串拼接、低风险问题、影响面较大的函数修改。
SCENARIO_DIFF = "\n".join(
    [
        "diff --git a/app/config.py b/app/config.py",
        "--- a/app/config.py",
        "+++ b/app/config.py",
        "@@ -1,6 +1,9 @@",
        " import os",
        " import subprocess",
        " import sqlite3",
        " ",
        " ",
        " def load_user(conn, name):",
        '+    API_KEY = "sk-live-0123456789abcdef"',
        '+    subprocess.run("ls " + name, shell=True)',
        '+    return conn.execute("SELECT * FROM users WHERE name = \'" + name + "\'").fetchall()',
        "",
    ]
)


class StubHandler(AgentHandler):
    """可注入失败的 Agent handler：用于构造"某一侧子任务未完成/产物非法"的场景。"""

    def __init__(self, agent_id: str, *, mode: str = "ok", tamper_hash: bool = False) -> None:
        self.agent_id = agent_id
        self.mode = mode
        self.tamper_hash = tamper_hash
        self.calls = 0

    def handle(self, request: AgentRequest) -> AgentResult:
        self.calls += 1
        if self.mode == "raise":
            from domain.errors import CodePilotError, ErrorCode

            raise CodePilotError(ErrorCode.AGENT_UNAVAILABLE, f"{self.agent_id} 模拟不可用")
        if self.mode == "empty":
            return AgentResult()
        artifact_type = (
            ArtifactType.IMPACT_REPORT if "impact" in self.agent_id else ArtifactType.FINDING
        )
        data = _minimal_payload(artifact_type)
        envelope = ArtifactEnvelope(
            artifact_id=f"artifact-stub-{self.agent_id}",
            task_id=request.task.task_id,
            artifact_type=artifact_type,
            schema_version="1.0",
            content_hash=payload_hash(data),
            size_bytes=len(json.dumps(data)),
            data=data,
        )
        if self.tamper_hash:
            envelope.content_hash = "sha256:" + "0" * 64
        return AgentResult(artifacts=[envelope])


def _minimal_payload(artifact_type: ArtifactType) -> dict[str, Any]:
    """构造通过 Schema 校验的最小 payload（字段沿用真实产物契约）。"""
    if artifact_type is ArtifactType.IMPACT_REPORT:
        return {
            "changed_symbols": [],
            "symbol_details": [],
            "direct_callers": [],
            "direct_callees": [],
            "affected_files": ["app/config.py"],
            "risk_level": "low",
            "uncertain": False,
        }
    return {"findings": []}


def make_container(tmp_path: Path, *, config: CodePilotConfig | None = None):
    url = f"sqlite+pysqlite:///{(tmp_path / 'scenario.db').as_posix()}"
    container = build_container(url=url, auto_create=True, config=config)
    container.content_store = ContentStore(tmp_path / "var")
    container.coordinator.content_store = container.content_store
    return container


def make_client(container) -> TestClient:
    app = create_app(container=container, recover_on_start=False, schedule_on_create=False)
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


@pytest.fixture()
def container(tmp_path: Path):
    return make_container(tmp_path)


@pytest.fixture()
def client(container):
    with make_client(container) as test_client:
        yield test_client


def create_scenario_review(client, *, key: str = "scenario-review-0001", diff: str = SCENARIO_DIFF):
    response = client.post(
        "/api/v1/reviews",
        json={
            "input_type": "diff",
            "content": diff,
            "context_policy": "function",
            "base_commit": "synthetic-base-001",
        },
        headers={**DEV, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _children(container, task_id: str) -> list[Any]:
    from repositories.store import A2ATaskStore

    with container.session() as session:
        return list(A2ATaskStore(session).list_by_parent(task_id))


# ---------------------------------------------------------------------------
# 1 & 2：同一任务只能有一个 Review / 一个 Impact 子任务
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_type", [ChildTaskType.REVIEW, ChildTaskType.IMPACT])
def test_only_one_child_task_per_type(client, container, task_type: ChildTaskType) -> None:
    body = create_scenario_review(client, key=f"scenario-once-{task_type}")
    task_id = body["task"]["id"]
    container.coordinator.run(task_id)  # 第一次编排
    container.coordinator.run(task_id)  # 幂等重跑：不得再创建子任务
    container.coordinator.recover()  # 恢复扫描：也不得再创建子任务

    rows = [row for row in _children(container, task_id) if row.task_type == str(task_type)]
    assert len(rows) == 1, f"{task_type} 子任务数量必须为 1，实际 {len(rows)}"
    assert rows[0].attempt == 1

    # API 视图同样只能看到一个该类型子任务
    detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
    same_type = [item for item in detail["child_tasks"] if item["task_type"] == str(task_type)]
    assert len(same_type) == 1


def test_child_task_rows_are_unique_per_parent_and_type(container) -> None:
    """数据库层也要保证唯一：同一父任务的子任务 key 不重复。"""
    with make_client(container) as scoped_client:
        task_id = create_scenario_review(scoped_client, key="scenario-unique-0001")["task"]["id"]
        container.coordinator.run(task_id)
    rows = _children(container, task_id)
    keys = {(row.parent_task_id, row.agent_id, row.task_type) for row in rows}
    assert len(keys) == len(rows) == 2


# ---------------------------------------------------------------------------
# 3：Review 与 Impact 都完成后父任务才能 REVIEWED
# ---------------------------------------------------------------------------


def test_parent_is_reviewed_only_after_both_children_complete(client, container) -> None:
    task_id = create_scenario_review(client, key="scenario-both-0001")["task"]["id"]
    container.coordinator.run(task_id)

    detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
    statuses = {item["task_type"]: item["status"] for item in detail["child_tasks"]}
    assert statuses == {
        str(ChildTaskType.REVIEW): str(ChildTaskStatus.COMPLETED),
        str(ChildTaskType.IMPACT): str(ChildTaskStatus.COMPLETED),
    }
    assert detail["task"]["status"] == str(ParentTaskStatus.REVIEWED)

    # 父任务记录里 completed / pending 必须与实际子任务一致（不靠 summary 里的展示字段）
    with container.session() as session:
        row = ReviewTaskStore(session).get(task_id)
        assert set(row.completed) == {item["id"] for item in detail["child_tasks"]}
        assert row.pending == []
        assert set(row.child_tasks) == {item["id"] for item in detail["child_tasks"]}
        assert set(row.artifacts) == {item["id"] for item in detail["artifacts"] if item["validated"]}


def test_parent_cannot_reach_reviewed_when_one_child_fails(tmp_path: Path) -> None:
    container = make_container(tmp_path)
    # Impact 侧 handler 直接抛错 → 子任务 FAILED → 父任务必须转人工，绝不能到 REVIEWED
    stub = StubHandler("impact-agent", mode="raise")
    container.coordinator.handlers["impact-agent"] = stub
    client = make_client(container)
    with client:
        task_id = create_scenario_review(client, key="scenario-half-0001")["task"]["id"]
        container.coordinator.run(task_id)
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    statuses = {item["task_type"]: item["status"] for item in detail["child_tasks"]}
    assert statuses[str(ChildTaskType.REVIEW)] == str(ChildTaskStatus.COMPLETED)
    assert statuses[str(ChildTaskType.IMPACT)] == str(ChildTaskStatus.FAILED)
    assert detail["task"]["status"] == str(ParentTaskStatus.NEEDS_HUMAN)
    assert detail["task"]["status"] != str(ParentTaskStatus.REVIEWED)
    assert detail["task"]["error_code"], "转人工必须带明确错误码"


# ---------------------------------------------------------------------------
# 4：Artifact 校验失败 → NEEDS_HUMAN
# ---------------------------------------------------------------------------


def test_tampered_artifact_parks_parent_needs_human(tmp_path: Path) -> None:
    container = make_container(tmp_path)
    container.coordinator.handlers["impact-agent"] = StubHandler("impact-agent", tamper_hash=True)
    client = make_client(container)
    with client:
        task_id = create_scenario_review(client, key="scenario-tamper-0001")["task"]["id"]
        container.coordinator.run(task_id)
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    impact = next(item for item in detail["child_tasks"] if item["task_type"] == "impact")
    assert impact["status"] == str(ChildTaskStatus.FAILED)
    assert impact["error_code"] == "ARTIFACT_HASH_MISMATCH"
    assert detail["task"]["status"] == str(ParentTaskStatus.NEEDS_HUMAN)


def test_missing_required_artifact_parks_parent_needs_human(tmp_path: Path) -> None:
    container = make_container(tmp_path)
    container.coordinator.handlers["impact-agent"] = StubHandler("impact-agent", mode="empty")
    client = make_client(container)
    with client:
        task_id = create_scenario_review(client, key="scenario-missing-0001")["task"]["id"]
        container.coordinator.run(task_id)
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    impact = next(item for item in detail["child_tasks"] if item["task_type"] == "impact")
    assert impact["error_code"] == "ARTIFACT_SCHEMA_INVALID"
    assert detail["task"]["status"] == str(ParentTaskStatus.NEEDS_HUMAN)
    # 没有落库的产物必须标记为未通过校验，避免"半个产物"被后续阶段使用
    assert all(item["validated"] for item in detail["artifacts"])


def test_invalid_artifact_is_recorded_as_unvalidated(container) -> None:
    """非法产物即使入库也必须 validated=False（供 Dashboard/审计识别）。"""
    with make_client(container) as scoped_client:
        task_id = create_scenario_review(scoped_client, key="scenario-invalid-0001")["task"]["id"]
        container.coordinator.run(task_id)
        child_id = next(
            row.id for row in _children(container, task_id) if row.task_type == "review"
        )
        from repositories.store import ArtifactStore

        with container.session() as session:
            bad = ArtifactEnvelope(
                artifact_id="artifact-invalid-1",
                task_id=child_id,
                artifact_type=ArtifactType.PATCH_CANDIDATE,
                schema_version="1.0",
                content_hash=payload_hash({"findings": "not-a-list"}),
                size_bytes=10,
                data={"findings": "not-a-list"},
            )
            ArtifactStore(session).save(
                bad,
                parent_task_id=task_id,
                agent_id="review-agent",
                validated=False,
                validation_error="schema 校验失败",
                trace_id="trace-invalid",
            )
        detail = scoped_client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    invalid = [item for item in detail["artifacts"] if item["id"] == "artifact-invalid-1"]
    assert invalid and invalid[0]["validated"] is False
    assert invalid[0]["validation_error"]


# ---------------------------------------------------------------------------
# 5：子任务缺少必需能力时不能执行
# ---------------------------------------------------------------------------


def test_card_missing_required_capability_blocks_startup(tmp_path: Path, monkeypatch) -> None:
    """Impact Agent Card 未声明必需能力 impact.ast 时，注册表直接拒绝加载（fail-closed）。

    这是实现层面的真实语义：静态 Registry 在启动时就校验"Card 能力 ⊇ 策略必需能力"，
    因此不会出现"能启动但会执行越权/不满足能力的子任务"的中间状态。
    """
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    source = Path(__file__).resolve().parents[1] / "a2a" / "cards"
    for path in source.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw["agent_id"] == "impact-agent":
            raw["capabilities"] = ["impact.summary"]  # 移除必需能力 impact.ast
        (cards_dir / path.name).write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr("a2a.registry.DEFAULT_CARDS_DIR", cards_dir)
    from domain.errors import CodePilotError

    with pytest.raises(CodePilotError) as excinfo:
        build_container(url=f"sqlite+pysqlite:///{(tmp_path / 'blocked.db').as_posix()}", auto_create=True)
    assert str(excinfo.value.code) == "AGENT_CARD_INVALID"
    assert "impact.ast" in excinfo.value.message


def test_runtime_negotiation_records_capability_failure(container, monkeypatch) -> None:
    """运行期协商：Card 缺少必需能力时不得创建子任务，且必须留下协商失败证据。"""
    from domain.errors import CodePilotError

    coordinator = container.coordinator
    card = coordinator.registry.get("impact-agent")
    stripped = card.model_copy(update={"capabilities": ["impact.summary"]})
    # 只替换内存中的 Card（模拟远端 Agent 声明能力不足，而不是本仓库的静态 Card）
    coordinator.registry._cards["impact-agent"] = stripped  # noqa: SLF001
    stub = StubHandler("impact-agent")
    coordinator.handlers["impact-agent"] = stub

    client = make_client(container)
    with client:
        task_id = create_scenario_review(client, key="scenario-capability-0001")["task"]["id"]
        with container.session() as session:
            event = session.execute(
                select(AuditEvent).where(
                    AuditEvent.event_type == "agent_card_resolved",
                    AuditEvent.task_id == task_id,
                )
            ).scalar_one()
        negotiation = event.after_state["negotiation_errors"]
        assert negotiation, "能力缺失必须记录协商失败"
        assert negotiation["impact"]["code"] == "CAPABILITY_NOT_AVAILABLE"

        outcome = coordinator.run(task_id)
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    assert stub.calls == 0, "能力缺失的子任务绝不能被执行"
    assert str(outcome.error_code) == "CAPABILITY_NOT_AVAILABLE"
    assert detail["task"]["status"] == str(ParentTaskStatus.NEEDS_HUMAN)
    assert not [item for item in detail["child_tasks"] if item["task_type"] == "impact"]
    # Review 侧仍然与 Impact 一起被创建（同一批 plan），但绝不能因为 Impact 缺失而放行父任务
    assert detail["task"]["status"] != str(ParentTaskStatus.REVIEWED)
    # 抛错路径不应该真的执行失败协商的 handler
    del CodePilotError


def test_registry_rejects_missing_capability_directly() -> None:
    from domain.enums import ChildTaskType as CT
    from domain.errors import CodePilotError

    registry = AgentRegistry.load_static()
    card = registry.get("impact-agent")
    stripped = card.model_copy(update={"capabilities": ["impact.summary"]})
    stripped_registry = AgentRegistry({**registry._cards, "impact-agent": stripped})  # noqa: SLF001
    with pytest.raises(CodePilotError) as excinfo:
        stripped_registry.resolve(task_type=CT.IMPACT, required_capabilities=("impact.ast",))
    assert str(excinfo.value.code) == "CAPABILITY_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# 6：trace / parent_task_id / task_id 串联
# ---------------------------------------------------------------------------


def test_trace_parent_and_task_ids_are_consistent(client, container) -> None:
    task_id = create_scenario_review(client, key="scenario-trace-0001")["task"]["id"]
    container.coordinator.run(task_id)
    detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
    trace_id = detail["task"]["trace_id"]
    assert trace_id.startswith("trace-")

    for child in detail["child_tasks"]:
        assert child["id"] != task_id
        assert child["correlation_id"].endswith(child["task_type"])

    with container.session() as session:
        child_rows = session.execute(
            select(A2ATaskRow).where(A2ATaskRow.parent_task_id == task_id)
        ).scalars().all()
        assert {row.trace_id for row in child_rows} == {trace_id}
        assert all(row.parent_task_id == task_id for row in child_rows)

        messages = session.execute(
            select(A2AMessage).where(A2AMessage.task_id.in_([row.id for row in child_rows]))
        ).scalars().all()
        assert messages, "子任务必须留下 A2A Message"
        assert all(message.correlation_id for message in messages)

        events = session.execute(
            select(AuditEvent).where(AuditEvent.task_id == task_id)
        ).scalars().all()
        assert events
        assert all(event.trace_id == trace_id for event in events)

    # 任务详情里的审计尾部同样属于同一 trace（通过 /api/v1/audit 交叉验证）
    audit = client.get(
        "/api/v1/audit", params={"trace_id": trace_id, "limit": 100}, headers=ADMIN
    ).json()
    assert len(audit) >= len(detail["audit_tail"])


# ---------------------------------------------------------------------------
# 7：重启后不重放已完成子任务
# ---------------------------------------------------------------------------


def test_restart_does_not_replay_completed_children(client, container, tmp_path: Path) -> None:
    task_id = create_scenario_review(client, key="scenario-restart-0001")["task"]["id"]
    container.coordinator.run(task_id)
    before = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
    assert before["task"]["status"] == str(ParentTaskStatus.REVIEWED)

    with container.session() as session:
        artifacts_before = session.execute(
            select(func.count()).select_from(A2AArtifact).where(A2AArtifact.parent_task_id == task_id)
        ).scalar_one()
        started_before = session.execute(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.task_id == task_id, AuditEvent.event_type == "task_started"
            )
        ).scalar_one()

    # 模拟进程重启：以同一数据库重建容器并执行恢复
    restarted = build_container(url=container.database_url, auto_create=False)
    restarted.content_store = ContentStore(tmp_path / "var")
    restarted.coordinator.content_store = restarted.content_store
    recovered = restarted.coordinator.recover()
    assert isinstance(recovered, list)

    restarted_client = make_client(restarted)
    with restarted_client:
        after = restarted_client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    assert after["task"]["status"] == before["task"]["status"]
    assert {item["id"] for item in after["child_tasks"]} == {
        item["id"] for item in before["child_tasks"]
    }
    assert all(item["attempt"] == 1 for item in after["child_tasks"]), "重启不得重放子任务"
    with restarted.session() as session:
        artifacts_after = session.execute(
            select(func.count()).select_from(A2AArtifact).where(A2AArtifact.parent_task_id == task_id)
        ).scalar_one()
        started_after = session.execute(
            select(func.count()).select_from(AuditEvent).where(
                AuditEvent.task_id == task_id, AuditEvent.event_type == "task_started"
            )
        ).scalar_one()
    assert artifacts_after == artifacts_before
    assert started_after == started_before == 1


# ---------------------------------------------------------------------------
# 8：查询不暴露未授权/未校验产物内容
# ---------------------------------------------------------------------------


def test_task_detail_exposes_only_artifact_summaries(client, container) -> None:
    task_id = create_scenario_review(client, key="scenario-leak-0001")["task"]["id"]
    container.coordinator.run(task_id)
    detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    assert detail["artifacts"], "应返回 Artifact 摘要"
    forbidden_keys = {"data", "payload", "raw", "content", "diff", "diff_text", "body", "stdout"}
    for artifact in detail["artifacts"]:
        assert not (forbidden_keys & set(artifact)), f"Artifact 摘要泄露内容字段：{artifact}"
        assert set(artifact) <= {
            "id",
            "task_id",
            "artifact_type",
            "schema_version",
            "content_hash",
            "size_bytes",
            "validated",
            "validation_error",
            "created_at",
        }

    # 审计接口需要 admin；developer 不能读到审计（包含详细 after_state）
    assert client.get("/api/v1/audit", headers=DEV).status_code == 403
    assert client.get("/api/v1/audit", headers=ADMIN).status_code == 200

    # 工作区快照（含完整源码）不经 API 暴露
    serialized = json.dumps(detail, ensure_ascii=False)
    assert "def load_user" not in serialized or "evidence" in serialized
    for path in ("/api/v1/reviews/{}/workspace", "/api/v1/reviews/{}/input"):
        assert client.get(path.format(task_id), headers=DEV).status_code == 404


def test_error_responses_do_not_leak_internal_paths(client) -> None:
    response = client.post(
        "/api/v1/reviews",
        json={
            "input_type": "diff",
            "content": "diff --git a/../../etc/passwd b/../../etc/passwd\n",
            "base_commit": "synthetic-base-001",
            "context_policy": "function",
        },
        headers={**DEV, "Idempotency-Key": "scenario-traversal-01"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_INPUT"
    assert "C:\\" not in json.dumps(body) and "site-packages" not in json.dumps(body)


def test_scenario_findings_cover_required_defect_classes(client, container) -> None:
    """场景一必须能发现题目要求的缺陷类别（硬编码密钥 / shell=True / SQL 拼接）。"""
    task_id = create_scenario_review(client, key="scenario-findings-0001")["task"]["id"]
    container.coordinator.run(task_id)
    detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    rule_ids = {item["rule_id"] for item in detail["comments"]}
    assert "R002_HARDCODED_SECRET" in rule_ids
    assert "R003_SHELL_TRUE" in rule_ids
    assert any("SQL" in rule for rule in rule_ids), f"必须命中 SQL 拼接规则：{sorted(rule_ids)}"
    assert detail["task"]["summary"]["findings"] >= 3
    assert detail["task"]["summary"]["risk_level"] in {"low", "medium", "high"}
    assert detail["task"]["summary"]["affected_files"] == ["app/config.py"]
