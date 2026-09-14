"""真实启动验收：用 README 记录的入口启动 uvicorn，而不是进程内 TestClient。

覆盖用户要求的三项"部署侧"验收：
1. `/healthz` 与 `/readyz` 在真实启动后可访问，且 `/readyz` 自证传输层（inprocess/http）；
2. 真实进程上写命令的幂等键约束与同键同请求返回原结果（P0-1 的部署侧证据）；
3. 真实重启（结束进程 + 重新启动，同一数据库）后重复同一幂等键**不产生重复副作用**。

不使用 TestClient、不伪造 ASGI 直连；数据库使用临时 SQLite 文件，端口为随机空闲端口。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.deps import build_container  # noqa: E402
from domain.enums import InputType, RunMode  # noqa: E402
from repositories.database import session_factory  # noqa: E402
from repositories.models import AuditEvent  # noqa: E402

DEVELOPER = {"X-Actor-Id": "dev-live", "X-Actor-Role": "developer"}
ADMIN = {"X-Actor-Id": "admin-live", "X-Actor-Role": "admin"}

SAMPLE_DIFF = "\n".join(
    [
        "diff --git a/app/config.py b/app/config.py",
        "--- a/app/config.py",
        "+++ b/app/config.py",
        "@@ -1,3 +1,5 @@",
        " import os",
        " ",
        '+API_KEY = "sk-live-0123456789abcdef"',
        '+os.system("rm -rf /tmp/x")',
        "",
    ]
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    """以真实 uvicorn 子进程运行 ``apps.api.main:app``。"""

    def __init__(self, database_url: str, data_root: Path) -> None:
        self.database_url = database_url
        self.data_root = data_root
        self.port = _free_port()
        self.process: subprocess.Popen[str] | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        environment = dict(os.environ)
        environment.update(
            {
                "CODEPILOT_DATABASE_URL": self.database_url,
                "CODEPILOT_DATA_ROOT": str(self.data_root),
                "CODEPILOT_CONFIG": "examples/.codepilot.yaml",
                "CODEPILOT_RECOVER_ON_START": "0",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.process = subprocess.Popen(  # noqa: S603 - 启动本仓库自身的服务入口
            [
                sys.executable,
                "-m",
                "uvicorn",
                "apps.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.process.poll() is not None:
                output = self.process.stdout.read() if self.process.stdout else ""
                raise RuntimeError(f"uvicorn 提前退出：\n{output[-4000:]}")
            try:
                response = httpx.get(f"{self.base_url}/healthz", timeout=2.0)
            except httpx.HTTPError:
                time.sleep(0.2)
                continue
            if response.status_code == 200:
                return
            time.sleep(0.2)
        raise RuntimeError("uvicorn 未能在 90 秒内就绪")

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover - 兜底
            self.process.kill()
            self.process.wait(timeout=10)
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.process = None

    def restart(self) -> None:
        self.stop()
        self.port = _free_port()
        self.start()


@pytest.fixture()
def live_server(tmp_path: Path):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'live.db').as_posix()}"
    # 先用仓储层建表并安装追加式护栏（生产形态由 alembic 完成同一件事）。
    build_container(url=database_url, auto_create=True)
    server = LiveServer(database_url, tmp_path / "var")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _count_audit(database_url: str, task_id: str, event_type: str | None = None) -> int:
    from sqlalchemy import func, select

    factory = session_factory(database_url)
    with factory() as session:
        statement = select(func.count()).select_from(AuditEvent).where(AuditEvent.task_id == task_id)
        if event_type:
            statement = statement.where(AuditEvent.event_type == event_type)
        return int(session.execute(statement).scalar_one())


def _create_review(base_url: str, *, key: str, content: str = SAMPLE_DIFF, mode: str = "offline") -> httpx.Response:
    return httpx.post(
        f"{base_url}/api/v1/reviews",
        json={
            "input_type": InputType.DIFF.value,
            "content": content,
            "base_commit": "live-base-001",
            "context_policy": "function",
            "mode": mode,
        },
        headers={**DEVELOPER, "Idempotency-Key": key},
        timeout=60.0,
    )


def _wait_terminal(base_url: str, task_id: str, timeout: float = 120.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    payload: dict[str, Any] = {}
    while time.time() < deadline:
        response = httpx.get(f"{base_url}/api/v1/reviews/{task_id}", headers=DEVELOPER, timeout=30.0)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["task"]["status"] in {"REVIEWED", "PENDING_APPROVAL", "NEEDS_HUMAN", "FAILED", "REJECTED", "MERGED"}:
            return payload
        time.sleep(0.5)
    raise AssertionError(f"任务未在 {timeout}s 内进入终态：{payload.get('task', {}).get('status')}")


def test_entrypoint_starts_and_reports_ready(live_server: LiveServer) -> None:
    health = httpx.get(f"{live_server.base_url}/healthz", timeout=10.0)
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    ready = httpx.get(f"{live_server.base_url}/readyz", timeout=30.0)
    assert ready.status_code == 200, ready.text
    body = ready.json()
    assert body["status"] == "ready"
    assert body["transport"] in {"inprocess", "http"}, "readyz 必须自证传输层"
    assert body["transport"] == "inprocess", "未配置 A2A 端点时必须安全降级为进程内调用"
    assert set(body["agents"]) == {"review-agent", "impact-agent", "fix-agent", "verify-agent"}
    assert "write_patch" in body["tools"]
    assert len(body["tools"]) == 8
    assert body["recoverable_tasks"] == 0


def test_live_openapi_exposes_no_external_a2a_task_routes(live_server: LiveServer) -> None:
    schema = httpx.get(f"{live_server.base_url}/openapi.json", timeout=30.0).json()
    paths = set(schema["paths"])
    assert paths, "OpenAPI 不应为空"
    assert not [path for path in paths if path.startswith("/api/v1/a2a")], (
        f"对外 API 不应暴露 A2A 子任务路径：{sorted(paths)}"
    )
    # 内部 A2A 路径在单进程形态下存在，且属于内部接口（docs/08 §3.12）。
    assert "/internal/a2a/tasks/{task_id}" in paths
    assert "/internal/a2a/tasks/{task_id}/cancel" in paths


def test_live_write_command_requires_idempotency_key(live_server: LiveServer) -> None:
    missing = httpx.post(
        f"{live_server.base_url}/api/v1/reviews",
        json={
            "input_type": "diff",
            "content": SAMPLE_DIFF,
            "base_commit": "live-base-001",
            "context_policy": "function",
        },
        headers=DEVELOPER,
        timeout=30.0,
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    short = _create_review(live_server.base_url, key="short")
    assert short.status_code == 400
    assert short.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_live_repeated_create_returns_original_task(live_server: LiveServer) -> None:
    key = "live-create-0001"
    first = _create_review(live_server.base_url, key=key)
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    task_id = first.json()["task"]["id"]

    second = _create_review(live_server.base_url, key=key)
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["task"]["id"] == task_id

    # 同键异请求必须冲突，而不是静默创建第二个任务。
    conflict = _create_review(live_server.base_url, key=key, content=SAMPLE_DIFF.replace(" import os", " import json"))
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_live_restart_does_not_duplicate_side_effects(live_server: LiveServer) -> None:
    key = "live-restart-0001"
    created = _create_review(live_server.base_url, key=key)
    assert created.status_code == 200, created.text
    task_id = created.json()["task"]["id"]
    payload = _wait_terminal(live_server.base_url, task_id)
    assert payload["task"]["status"] in {"REVIEWED", "PENDING_APPROVAL"}
    comments_before = len(payload["comments"])
    started_before = _count_audit(live_server.database_url, task_id, "task_started")
    assert started_before == 1

    # 真实重启：结束进程、换端口、以同一数据库重新启动。
    live_server.restart()

    ready = httpx.get(f"{live_server.base_url}/readyz", timeout=30.0).json()
    assert ready["status"] == "ready"

    replay = _create_review(live_server.base_url, key=key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["created"] is False
    assert replay.json()["task"]["id"] == task_id

    after = httpx.get(f"{live_server.base_url}/api/v1/reviews/{task_id}", headers=DEVELOPER, timeout=30.0).json()
    assert len(after["comments"]) == comments_before, "重启后重复同一幂等键不得产生第二份意见"
    assert _count_audit(live_server.database_url, task_id, "task_started") == 1
    child_rows = after["child_tasks"]
    assert len({row["id"] for row in child_rows}) == len(child_rows), "子任务不得被重复创建"
    for row in child_rows:
        assert row["attempt"] == 1, f"重启后不得重复执行子任务：{row}"


def test_live_audit_is_queryable_by_admin_and_hidden_from_developer(live_server: LiveServer) -> None:
    created = _create_review(live_server.base_url, key="live-audit-0001")
    task_id = created.json()["task"]["id"]
    _wait_terminal(live_server.base_url, task_id)

    forbidden = httpx.get(f"{live_server.base_url}/api/v1/audit", headers=DEVELOPER, timeout=30.0)
    assert forbidden.status_code == 403

    allowed = httpx.get(
        f"{live_server.base_url}/api/v1/audit",
        params={"task_id": task_id, "limit": 50},
        headers=ADMIN,
        timeout=30.0,
    )
    assert allowed.status_code == 200, allowed.text
    events = allowed.json()
    assert events and all(event["task_id"] == task_id for event in events)
    assert json.dumps(events, ensure_ascii=False)  # 可序列化，便于 Dashboard 展示


def test_live_mode_is_reflected_in_task_payload(live_server: LiveServer) -> None:
    for mode in (RunMode.SINGLE.value, RunMode.A2A.value, RunMode.OFFLINE.value):
        created = _create_review(live_server.base_url, key=f"live-mode-{mode}-1", mode=mode)
        assert created.status_code == 200, created.text
        assert created.json()["task"]["mode"] == mode
