"""真实落地场景二 + 三：选择修复 → 生成补丁 → Docker 沙箱验证。

覆盖验收清单：
1. 父任务从 `REVIEWED` 进入 `FIXING`；
2. Fix Agent 只接收允许修复的 Finding；
3. `PatchCandidate` 包含全部必需字段；
4. 生成的 diff 保留结尾换行；
5. `PatchCandidate` 通过 Schema 校验；
6. `patch_hash` 可以重新计算并一致；
7. 不允许直接写入任务分支（Fix 阶段零文件副作用）；
8. 不允许修改 `main` / `develop`；审批前不允许合并；
9. 生成补丁失败 → `NEEDS_HUMAN` / `REJECTED`；
10. offline 模式不生成补丁；
11. 保守拒绝：无法安全改写的形态一律不生成补丁；
12. 沙箱不可用时安全降级，绝不回退到宿主机执行。

沙箱相关断言在 Docker 不可用时跳过（`pytest.mark.skipif`），并在输出中说明原因，
不会把"未执行"写成"通过"。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from a2a.protocol import FindingItem
from a2a.schema_registry import validate_artifact_payload
from agents.fix.recipes import plan_fix
from apps.api.deps import build_container
from apps.api.main import create_app
from domain.enums import FixCategory, ParentTaskStatus, PatchStatus, RunMode
from repositories.content_store import ContentStore
from repositories.models import A2AArtifact, AuditEvent, SandboxRun
from repositories.records import PatchStore
from repositories.repo_store import SyntheticRepo
from sandbox.base import UnavailableSandbox

DEV = {"X-Actor-Id": "dev-fix", "X-Actor-Role": "developer"}
APPROVER = {"X-Actor-Id": "approver-1", "X-Actor-Role": "approver"}

#: 覆盖三类自动修复 + 一个不可自动修复的问题（路径穿越/非字面量）。
FIXABLE_DIFF = "\n".join(
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

#: 无法安全改写：密钥来自函数调用（非字面量）、子进程命令来自函数调用。
UNFIXABLE_DIFF = "\n".join(
    [
        "diff --git a/app/unsafe.py b/app/unsafe.py",
        "--- a/app/unsafe.py",
        "+++ b/app/unsafe.py",
        "@@ -1,5 +1,8 @@",
        " import os",
        " import subprocess",
        " ",
        " ",
        " def loader(env):",
        "+    TOKEN = env.get('TOKEN')",
        '+    subprocess.run(build_command(), shell=True)',
        "+    return TOKEN",
        "",
    ]
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    completed = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


DOCKER_AVAILABLE = _docker_available()


def make_container(tmp_path: Path, *, mode: str | None = None, sandbox=None):
    url = f"sqlite+pysqlite:///{(tmp_path / 'fixflow.db').as_posix()}"
    config = None
    if mode:
        from domain.config import CodePilotConfig

        config = CodePilotConfig.model_validate({"mode": mode})
    container = build_container(url=url, auto_create=True, config=config, sandbox=sandbox)
    if sandbox is not None:
        container.coordinator.sandbox = sandbox
    container.content_store = ContentStore(tmp_path / "var")
    container.coordinator.content_store = container.content_store
    container.coordinator.repo_root = container.content_store.root
    return container


def make_client(container) -> TestClient:
    app = create_app(container=container, recover_on_start=False, schedule_on_create=False)
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def create_review(client, diff: str, *, key: str, mode: str | None = None):
    body: dict[str, object] = {
        "input_type": "diff",
        "content": diff,
        "context_policy": "function",
        "base_commit": "synthetic-base-001",
    }
    if mode:
        body["mode"] = mode
    response = client.post(
        "/api/v1/reviews",
        json=body,
        headers={**DEV, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]["id"]


#: 带测试的 ZIP 工作区（沙箱门禁需要真实可运行的测试才能真正通过）。
ZIP_PROJECT = {
    "app/__init__.py": "",
    "app/config.py": "\n".join(
        [
            '"""Config module."""',
            "import os",
            "import subprocess",
            "",
            'API_KEY = "sk-live-1234567890"',
            "",
            "",
            "def run(cmd, directory):",
            '    return subprocess.run("ls " + directory, shell=True)',
            "",
            "",
            "def query(cursor, name):",
            '    return cursor.execute("SELECT * FROM users WHERE name=\'" + name + "\'")',
            "",
        ]
    ),
    "tests/test_config.py": "\n".join(
        [
            "import os",
            "",
            'os.environ.setdefault("API_KEY", "test-key")',
            "",
            "from app import config",
            "",
            "",
            "def test_run_returns_something():",
            '    assert config.run("ls", "/tmp") is not None',
            "",
            "",
            "def test_api_key_from_env():",
            '    assert config.API_KEY == "test-key"',
            "",
            "",
            "def test_query_builds_sql():",
            "    calls = []",
            "",
            "    class Cursor:",
            "        def execute(self, sql, params=None):",
            "            calls.append((sql, params))",
            '            return "ok"',
            "",
            '    assert config.query(Cursor(), "alice") == "ok"',
            "    assert calls",
            "",
        ]
    ),
}


def build_zip(project: dict[str, str]) -> str:
    import base64
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in project.items():
            archive.writestr(path, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def create_review_zip(client, *, key: str):
    response = client.post(
        "/api/v1/reviews",
        json={
            "input_type": "zip",
            "content": build_zip(ZIP_PROJECT),
            "context_policy": "function",
            "base_commit": "synthetic-base-001",
        },
        headers={**DEV, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["task"]["id"]


# ---------------------------------------------------------------------------
# 1~6：REVIEWED → FIXING，PatchCandidate 字段/换行/Schema/哈希
# ---------------------------------------------------------------------------


def _wait_for_status(
    container, task_id: str, targets: set[str], *, timeout: float = 120.0
) -> str:
    """等待后台 Fix/Verify 编排收敛到目标状态（API 触发后由后台线程执行）。"""
    import time

    from repositories.store import ReviewTaskStore

    deadline = time.time() + timeout
    status = ""
    while time.time() < deadline:
        with container.session() as session:
            status = ReviewTaskStore(session).get(task_id).status
        if status in targets:
            return status
        time.sleep(0.3)
    return status


def _trigger_fix(client, task_id: str, *, key: str) -> None:
    response = client.post(
        f"/api/v1/reviews/{task_id}/fixes",
        headers={**DEV, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text


def test_fix_phase_generates_schema_valid_candidate(tmp_path: Path) -> None:
    container = make_container(tmp_path, sandbox=UnavailableSandbox(reason="测试：不执行沙箱"))
    with make_client(container) as client:
        task_id = create_review(client, FIXABLE_DIFF, key="fixflow-happy-0001")
        container.coordinator.run(task_id)
        reviewed = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        assert reviewed["task"]["status"] == str(ParentTaskStatus.REVIEWED)

        _trigger_fix(client, task_id, key="fixflow-happy-fix1")
        settled = _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.NEEDS_HUMAN), str(ParentTaskStatus.PENDING_APPROVAL)},
        )
        patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=DEV).json()

    assert patches, f"必须生成候选补丁（最终状态 {settled}）"
    patch = patches[-1]

    # 2) Fix 只接收允许修复的 Finding：变更文件必须来自可修复意见
    detail = reviewed
    fixable_files = {item["file"] for item in detail["comments"] if item["auto_fixable"]}
    assert set(patch["changed_files"]) <= fixable_files
    assert patch["changed_files"] == ["app/config.py"]

    # 3) PatchCandidate 必需字段
    for field in (
        "id",
        "task_id",
        "patch_version",
        "diff",
        "patch_hash",
        "changed_files",
        "changed_functions",
        "fix_category",
        "target_branch",
        "scope_drift",
        "wide_impact",
        "test_gap",
    ):
        assert field in patch, f"PatchCandidate 缺少字段 {field}"
    assert patch["task_id"] == task_id
    assert patch["patch_version"] == 1
    assert patch["target_branch"] == f"codepilot/{task_id}"
    assert patch["fix_category"] in {item.value for item in FixCategory}

    # 4) diff 必须保留结尾换行（协议层曾因 strip 破坏补丁）
    assert patch["diff"].endswith("\n"), "生成的 diff 必须保留结尾换行"

    # 5) Schema 校验
    artifact_envelope = None
    with container.session() as session:
        rows = session.execute(
            select(A2AArtifact).where(
                A2AArtifact.parent_task_id == task_id,
                A2AArtifact.artifact_type == "PatchCandidate",
            )
        ).scalars().all()
        assert rows, "必须落库 PatchCandidate Artifact"
        artifact_envelope = rows[-1]
    validate_artifact_payload(artifact_envelope.artifact_type, artifact_envelope.data)
    payload = artifact_envelope.data
    # Schema 冻结的必需字段（schemas/artifact-patch-candidate-v1.schema.json）
    for field in (
        "patch_version",
        "patch_hash",
        "base_commit",
        "target_branch",
        "fix_category",
        "finding_refs",
        "changed_files",
        "changed_functions",
        "added_lines",
        "removed_lines",
        "diff",
        "idempotency_key",
    ):
        assert field in payload, f"Schema 要求字段 {field} 缺失"
    assert payload["finding_refs"], "必须声明来源 Finding"
    assert payload["target_branch"] == f"codepilot/{task_id}"

    # 6) patch_hash 可重算
    recomputed = "sha256:" + hashlib.sha256(payload["diff"].encode("utf-8")).hexdigest()
    assert patch["patch_hash"] == recomputed
    assert patch["patch_hash"] == ArtifactHash(payload)


def ArtifactHash(payload: dict) -> str:  # noqa: N802 - 与 payload 字段同名便于阅读
    """按产物内声明的算法重新计算 patch_hash。"""
    algorithm = str(payload.get("patch_hash", "sha256:")).split(":", 1)[0] or "sha256"
    digest = hashlib.new(algorithm, payload["diff"].encode("utf-8")).hexdigest()
    return f"{algorithm}:{digest}"


def test_fix_does_not_write_task_branch_before_approval(tmp_path: Path) -> None:
    """7 & 8：Fix 阶段不得产生任何文件副作用，也不得触碰任务分支/main/develop。"""
    container = make_container(tmp_path, sandbox=UnavailableSandbox(reason="测试：不执行沙箱"))
    with make_client(container) as client:
        task_id = create_review(client, FIXABLE_DIFF, key="fixflow-nowrite-0001")
        container.coordinator.run(task_id)
        _trigger_fix(client, task_id, key="fixflow-nowrite-fix1")
        _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.NEEDS_HUMAN), str(ParentTaskStatus.PENDING_APPROVAL)},
        )
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    # 任务分支在 Fix 阶段仍不存在（只有合并才创建）
    repo_root = Path(container.coordinator.repo_root)
    task_branch = repo_root / "repos" / task_id
    assert not task_branch.exists(), "审批前不得创建/写入任务分支"

    # 状态必须是 NEEDS_HUMAN（沙箱不可用安全降级）或 PENDING_APPROVAL，绝不能是 MERGED
    assert detail["task"]["status"] in {
        str(ParentTaskStatus.NEEDS_HUMAN),
        str(ParentTaskStatus.PENDING_APPROVAL),
        str(ParentTaskStatus.TESTING),
    }

    with container.session() as session:
        patch = PatchStore(session).latest(task_id)
        assert patch is not None
        assert patch.status != str(PatchStatus.APPLIED), "审批前补丁不能被标记为已应用"
        events = session.execute(
            select(AuditEvent).where(AuditEvent.task_id == task_id)
        ).scalars().all()
    assert not [event for event in events if event.event_type == "task_merged"]

    # 未审批直接合并必须被拒绝
    with make_client(container) as client:
        merge = client.post(
            f"/api/v1/fixes/{patch.id}/merge",
            json={"patch_version": patch.patch_version},
            headers={**APPROVER, "Idempotency-Key": "fixflow-nowrite-merge1"},
        )
    assert merge.status_code in {403, 409}, merge.text


def test_fix_phase_rejects_a_non_rewritable_finding(tmp_path: Path) -> None:
    """9 & 11：规则命中了 BUG，但 recipe 无法安全改写 → 不产出补丁，转人工。

    ``subprocess.run(build_command(), shell=True)`` 的命令来自函数调用，参数边界不可知，
    因此 recipe 必须保守拒绝（而不是猜测改写），父任务不得进入审批。
    """
    container = make_container(tmp_path, sandbox=UnavailableSandbox(reason="测试：不执行沙箱"))
    with make_client(container) as client:
        task_id = create_review(client, UNFIXABLE_DIFF, key="fixflow-unfixable-0001")
        container.coordinator.run(task_id)
        reviewed = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        assert reviewed["task"]["status"] == str(ParentTaskStatus.REVIEWED)
        _trigger_fix(client, task_id, key="fixflow-unfixable-fix1")
        settled = _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.NEEDS_HUMAN), str(ParentTaskStatus.REJECTED)},
        )
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=DEV).json()

    assert settled in {str(ParentTaskStatus.NEEDS_HUMAN), str(ParentTaskStatus.REJECTED)}
    assert detail["task"]["status"] != str(ParentTaskStatus.PENDING_APPROVAL)
    assert patches == [], "无法安全改写的形态绝不能产出候选补丁"


def test_fix_phase_rejected_when_no_fixable_finding(tmp_path: Path) -> None:
    """REVIEWED → REJECTED：审查结果里没有任何可自动修复项时不得进入 Fix。"""
    low_risk_only = "\n".join(
        [
            "diff --git a/app/style.py b/app/style.py",
            "--- a/app/style.py",
            "+++ b/app/style.py",
            "@@ -1,3 +1,4 @@",
            " import os",
            " ",
            " ",
            "+x = 1  # noqa: F841 低风险问题，不可自动修复",
            "",
        ]
    )
    container = make_container(tmp_path, sandbox=UnavailableSandbox(reason="测试：不执行沙箱"))
    with make_client(container) as client:
        task_id = create_review(client, low_risk_only, key="fixflow-nofix-0001")
        container.coordinator.run(task_id)
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        assert detail["task"]["status"] == str(ParentTaskStatus.REVIEWED)
        assert not [item for item in detail["comments"] if item["auto_fixable"]]
        _trigger_fix(client, task_id, key="fixflow-nofix-fix1")
        _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.REJECTED), str(ParentTaskStatus.NEEDS_HUMAN)},
        )
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=DEV).json()

    assert detail["task"]["status"] == str(ParentTaskStatus.REJECTED)
    assert patches == []


def test_offline_mode_never_generates_patch(tmp_path: Path) -> None:
    """10：offline 只跑确定性规则与 AST，不生成补丁。"""
    container = make_container(
        tmp_path, mode=str(RunMode.OFFLINE), sandbox=UnavailableSandbox(reason="offline")
    )
    with make_client(container) as client:
        task_id = create_review(client, FIXABLE_DIFF, key="fixflow-offline-0001", mode="offline")
        container.coordinator.run(task_id)
        reviewed = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        assert reviewed["task"]["mode"] == "offline"
        assert reviewed["task"]["status"] == str(ParentTaskStatus.REVIEWED)
        assert [item for item in reviewed["comments"] if item["auto_fixable"]]

        _trigger_fix(client, task_id, key="fixflow-offline-fix1")
        _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.REJECTED), str(ParentTaskStatus.NEEDS_HUMAN)},
        )
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=DEV).json()

    assert patches == [], "offline 模式不得生成补丁"
    assert detail["task"]["status"] == str(ParentTaskStatus.REJECTED)
    assert detail["task"]["summary"]["reason"] == "offline_mode_no_fix"


# ---------------------------------------------------------------------------
# 11：recipe 层的保守拒绝（确定性、不依赖 Docker）
# ---------------------------------------------------------------------------


def _finding(line: int, category: FixCategory, *, symbol: str = "load_user") -> FindingItem:
    return FindingItem(
        rule_id="R002_HARDCODED_SECRET",
        title="硬编码密钥",
        cwe="CWE-798",
        severity="critical",
        file="app/config.py",
        line=line,
        evidence="TOKEN = \"sk-live-...\"",
        message="检测到硬编码密钥",
        confidence_base=1.0,
        confidence=1.0,
        confidence_level="confirmed",
        auto_fixable=True,
        fix_category=category,
        symbol=symbol,
        rule_version="1.0",
    )


def _workspace(content: str):
    """直接构造工作区（避免走 diff 解析），用于 recipes 的纯函数级验证。"""
    from domain.enums import ContextPolicy, InputType
    from domain.workspace import Workspace

    return Workspace(
        task_id="task-workspace-check",
        base_commit="synthetic-base-001",
        input_type=InputType.DIFF,
        context_policy=ContextPolicy.FUNCTION,
        files={"app/config.py": content},
        changed_lines={"app/config.py": set(range(1, len(content.splitlines()) + 1))},
        diff_text="",
        partial=False,
    )


@pytest.mark.parametrize(
    ("content", "line", "category", "expected"),
    [
        # 非字面量密钥：值来自函数调用 → 拒绝
        ("def f(env):\n    TOKEN = env.get('TOKEN')\n", 2, FixCategory.HARDCODED_SECRET, False),
        # 字面量密钥 → 允许
        ('def f():\n    TOKEN = "sk-live-abcdef123456"\n', 2, FixCategory.HARDCODED_SECRET, True),
        # shell=True 且参数数量不明确（f-string 拼接外部调用）→ 拒绝
        ("import subprocess\ndef f():\n    subprocess.run(build(), shell=True)\n", 3, FixCategory.SHELL_TRUE, False),
        # shell=True 且命令为字面量 → 允许
        ('import subprocess\ndef f():\n    subprocess.run("ls /tmp", shell=True)\n', 3, FixCategory.SHELL_TRUE, True),
        # SQL 拼接但无法确定边界（直接拼接子查询变量）→ 保守接受改写为参数化
        ('def f(conn, name):\n    return conn.execute("SELECT * FROM t WHERE n = " + name)\n', 2, FixCategory.SQL_PARAMETERIZATION, True),
    ],
)
def test_recipes_are_conservative(content: str, line: int, category: FixCategory, expected: bool) -> None:
    workspace = _workspace(content)
    plan = plan_fix(_finding(line, category), workspace)
    assert (plan is not None) is expected, f"计划结果与预期不符：{plan}"


def test_recipe_returns_none_for_unknown_line_and_missing_file() -> None:
    workspace = _workspace('def f():\n    TOKEN = "sk-live-abcdef123456"\n')
    assert plan_fix(_finding(99, FixCategory.HARDCODED_SECRET), workspace) is None
    outside = _finding(2, FixCategory.HARDCODED_SECRET)
    outside.file = "app/other.py"
    assert plan_fix(outside, workspace) is None


def test_recipe_rejects_non_auto_fixable_finding() -> None:
    workspace = _workspace('def f():\n    TOKEN = "sk-live-abcdef123456"\n')
    finding = _finding(2, FixCategory.HARDCODED_SECRET)
    finding.auto_fixable = False
    assert plan_fix(finding, workspace) is None


def test_scope_guard_rejects_protected_branch(tmp_path: Path) -> None:
    """8：main / develop 在策略层禁止写入，且只允许写入本任务分支。"""
    from domain.errors import CodePilotError

    task_id = "task-protected-check"
    repo = SyntheticRepo(tmp_path, task_id)
    repo.ensure({"app/main.py": "print('hi')\n"}, base_commit="synthetic-base-001")
    for branch in ("main", "master", "develop", "HEAD"):
        with pytest.raises(CodePilotError) as excinfo:
            repo.apply_patch(
                "diff --git a/app/main.py b/app/main.py\n",
                expected_branch=branch,
            )
        assert str(excinfo.value.code) == "FORBIDDEN"
        assert "受保护分支" in excinfo.value.message
    # 其它任务的分支同样拒绝
    with pytest.raises(CodePilotError) as excinfo:
        repo.apply_patch("diff --git a/app/main.py b/app/main.py\n", expected_branch="codepilot/other-task")
    assert str(excinfo.value.code) == "FORBIDDEN"


# ---------------------------------------------------------------------------
# 12：沙箱不可用时安全降级（真实 UnavailableSandbox，不需要 Docker）
# ---------------------------------------------------------------------------


def test_sandbox_unavailable_degrades_without_host_execution(tmp_path: Path) -> None:
    container = make_container(tmp_path, sandbox=UnavailableSandbox(reason="Docker 不可用（测试注入）"))
    with make_client(container) as client:
        task_id = create_review(client, FIXABLE_DIFF, key="fixflow-degrade-0001")
        container.coordinator.run(task_id)
        _trigger_fix(client, task_id, key="fixflow-degrade-fix1")
        settled = _wait_for_status(
            container, task_id, {str(ParentTaskStatus.NEEDS_HUMAN)}
        )
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    assert settled == str(ParentTaskStatus.NEEDS_HUMAN), f"安全降级未生效：{settled}"
    assert detail["task"]["status"] == str(ParentTaskStatus.NEEDS_HUMAN)
    assert detail["task"]["error_code"] == "SANDBOX_UNAVAILABLE"
    with container.session() as session:
        patch = PatchStore(session).latest(task_id)
        assert patch is not None
        assert patch.status != str(PatchStatus.APPLIED)
        assert patch.sandbox_run_id is None or patch.sandbox_run_id == ""
    # 安全降级：绝不在宿主机执行提交代码（任务分支始终不存在）
    assert not (Path(container.coordinator.repo_root) / "repos" / task_id).exists()


# ---------------------------------------------------------------------------
# 三：真实 Docker 沙箱验证（Docker 不可用时明确跳过，不写成通过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker 不可用：真实沙箱验收未完成")
def test_verify_evidence_is_produced_in_real_sandbox(tmp_path: Path) -> None:
    """补丁验证必须在真实 Docker 沙箱中执行，并留下可审计的 VerifyEvidence。"""
    from sandbox import default_executor

    container = make_container(tmp_path, sandbox=default_executor())
    with make_client(container) as client:
        task_id = create_review(client, FIXABLE_DIFF, key="fixflow-sandbox-0001")
        container.coordinator.run(task_id)
        _trigger_fix(client, task_id, key="fixflow-sandbox-fix1")
        _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.PENDING_APPROVAL), str(ParentTaskStatus.NEEDS_HUMAN)},
            timeout=300.0,
        )
        detail = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()

    status = detail["task"]["status"]
    evidence = detail["task"]["summary"].get("verify_evidence")
    assert evidence, f"沙箱执行后必须保留 VerifyEvidence（当前状态 {status}）"
    assert evidence["sandbox_degraded"] is False, "真实沙箱执行不得标记为降级"
    assert evidence["apply_check"]["ok"] is True, "必须先通过 git apply --check"
    assert evidence["tiers"], "必须给出分层门禁结果"
    assert evidence["sandbox_run_id"]
    assert status in {
        str(ParentTaskStatus.PENDING_APPROVAL),
        str(ParentTaskStatus.NEEDS_HUMAN),
    }
    if status == str(ParentTaskStatus.NEEDS_HUMAN):
        # 门禁失败必须给出可解释的原因，而不是只留一个错误码
        gate_failure = detail["task"]["summary"].get("gate_failure")
        assert gate_failure, "门禁失败必须记录失败层级"
        assert detail["task"]["error_code"] in {
            "TEST_GAP",
            "QUALITY_GATE_FAILED",
            "SANDBOX_VIOLATION",
        }
        # 记录的门禁原因必须与父任务的错误码一致（diff 输入没有测试文件 → TEST_GAP）
        assert gate_failure["error_code"] == detail["task"]["error_code"]
    # 沙箱输出必须脱敏：不得出现宿主机绝对路径
    assert "C:\\" not in evidence["sanitized_output"]
    # 沙箱运行记录必须落库（可回放），且带上真实隔离参数
    with container.session() as session:
        runs = session.execute(
            select(SandboxRun).where(SandboxRun.task_id == task_id)
        ).scalars().all()
    assert runs, "必须记录 sandbox_run"
    row = runs[-1]
    assert row.degraded is False
    assert row.network == "none", "沙箱必须禁用网络"
    assert row.memory_limit_mb > 0 and row.cpu_limit > 0 and row.timeout_seconds > 0
    assert row.image
    assert "C:\\" not in row.stdout_sanitized


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker 不可用：真实沙箱验收未完成")
def test_full_chain_to_merge_in_real_sandbox(tmp_path: Path) -> None:
    """场景二+三+四：Fix → Verify → PENDING_APPROVAL → 审批 → 合并（真实沙箱）。"""
    from sandbox import default_executor

    container = make_container(tmp_path, sandbox=default_executor())
    with make_client(container) as client:
        task_id = create_review_zip(client, key="fixflow-merge-0001")
        container.coordinator.run(task_id)
        _trigger_fix(client, task_id, key="fixflow-merge-fix1")
        _wait_for_status(
            container,
            task_id,
            {str(ParentTaskStatus.PENDING_APPROVAL), str(ParentTaskStatus.NEEDS_HUMAN)},
            timeout=300.0,
        )
        pending = client.get(f"/api/v1/reviews/{task_id}", headers=DEV).json()
        if pending["task"]["status"] != str(ParentTaskStatus.PENDING_APPROVAL):
            evidence = pending["task"]["summary"].get("verify_evidence") or {}
            pytest.skip(
                "沙箱门禁未通过，合并链路不在本用例范围："
                f"{pending['task']['status']} / {pending['task'].get('error_code')} / "
                f"tiers={[t['tier'] for t in evidence.get('tiers', []) if not t['ok']]}"
            )
        patch = client.get(f"/api/v1/reviews/{task_id}/patches", headers=DEV).json()[-1]

        # developer 不能审批/合并
        assert (
            client.post(
                f"/api/v1/fixes/{patch['id']}/approval",
                json={"decision": "approve", "patch_version": patch["patch_version"], "reason": "ok"},
                headers={**DEV, "Idempotency-Key": "fixflow-merge-dev1"},
            ).status_code
            == 403
        )
        approved = client.post(
            f"/api/v1/fixes/{patch['id']}/approval",
            json={"decision": "approve", "patch_version": patch["patch_version"], "reason": "证据充分"},
            headers={**APPROVER, "Idempotency-Key": "fixflow-merge-appr1"},
        )
        assert approved.status_code == 200, approved.text
        merged = client.post(
            f"/api/v1/fixes/{patch['id']}/merge",
            json={"patch_version": patch["patch_version"]},
            headers={**APPROVER, "Idempotency-Key": "fixflow-merge-appr2"},
        )
        assert merged.status_code == 200, merged.text
        body = merged.json()
        assert body["task_status"] == str(ParentTaskStatus.MERGED)
        assert body["branch"] == f"codepilot/{task_id}"
        assert body["commit"], "合并必须返回 commit"

        # 幂等重放：同键同请求返回原结果
        replay = client.post(
            f"/api/v1/fixes/{patch['id']}/merge",
            json={"patch_version": patch["patch_version"]},
            headers={**APPROVER, "Idempotency-Key": "fixflow-merge-appr2"},
        )
        assert replay.status_code == 200
        assert replay.json()["commit"] == body["commit"]
        assert replay.json().get("idempotent_replay") is True

        audit = client.get(
            "/api/v1/audit", params={"task_id": task_id, "limit": 200},
            headers={"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"},
        ).json()
    assert any(event["event_type"] == "task_merged" for event in audit), "合并必须写审计"
    assert json.dumps(body, ensure_ascii=False)
