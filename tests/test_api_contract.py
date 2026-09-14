"""API 契约测试：文档声明的路径必须与真实路由一致（防止文档与代码再次分叉）。

覆盖：
- `/api/v1/*` 的对外清单与代码一致；
- A2A Task 接口只存在于 `/internal/a2a/*`（对外 API 不暴露子任务视图）；
- 内部接口的角色限制、请求头要求与响应结构；
- 幂等键要求在生产路由上真的生效。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.api.deps import build_container
from apps.api.main import create_app
from domain.config import CodePilotConfig

CONFIG = CodePilotConfig.model_validate({"mode": "a2a"})
DEVELOPER = {"X-Actor-Id": "dev-1", "X-Actor-Role": "developer"}
COORDINATOR = {"X-Actor-Id": "coordinator", "X-Actor-Role": "coordinator"}

#: 对外 API 的冻结清单（与 SRS §8、docs/03 §2 一致）。
EXTERNAL_ROUTES: dict[str, set[str]] = {
    "/api/v1/reviews": {"POST"},
    "/api/v1/reviews/{task_id}": {"GET"},
    "/api/v1/reviews/{task_id}/events": {"GET"},
    "/api/v1/reviews/{task_id}/comments": {"GET"},
    "/api/v1/reviews/{task_id}/fixes": {"POST"},
    "/api/v1/reviews/{task_id}/patches": {"GET"},
    "/api/v1/reviews/{task_id}/resume": {"POST"},
    "/api/v1/fixes/{patch_id}": {"GET"},
    "/api/v1/fixes/{patch_id}/approval": {"POST"},
    "/api/v1/fixes/{patch_id}/merge": {"POST"},
    "/api/v1/audit": {"GET"},
    "/api/v1/agents": {"GET"},
    "/api/v1/agents/{agent_id}/card": {"GET"},
    "/api/v1/evals/run": {"POST"},
    "/api/v1/evals/{run_id}": {"GET"},
}

INTERNAL_ROUTES: dict[str, set[str]] = {
    "/internal/a2a/agents": {"GET"},
    "/internal/a2a/agents/{agent_id}/card": {"GET"},
    "/internal/a2a/agents/{agent_id}/tasks": {"POST"},
    "/internal/a2a/tasks/{task_id}": {"GET"},
    "/internal/a2a/tasks/{task_id}/events": {"GET"},
    "/internal/a2a/tasks/{task_id}/cancel": {"POST"},
}


def _collect_routes(app) -> dict[str, set[str]]:
    """从 OpenAPI 文档收集真实注册的路径与方法。

    FastAPI 会把 ``include_router`` 挂载的子路由展平进 ``openapi()['paths']``，
    因此这里是"代码真实暴露的 API 面"的权威来源。
    """
    collected: dict[str, set[str]] = {}
    for path, operations in app.openapi().get("paths", {}).items():
        collected[path] = {method.upper() for method in operations}
    return collected


@pytest.fixture()
def client(tmp_path, concurrent_database):
    container = build_container(url=concurrent_database, auto_create=True, config=CONFIG)
    app = create_app(
        container=container, recover_on_start=False, schedule_on_create=False, include_internal=True
    )
    with TestClient(app) as test_client:
        test_client.container = container  # type: ignore[attr-defined]
        yield test_client


def test_external_routes_match_documented_contract(client) -> None:
    routes = _collect_routes(client.app)
    for path, methods in EXTERNAL_ROUTES.items():
        assert path in routes, f"文档声明的对外路径缺失：{path}"
        assert methods <= routes[path], f"{path} 缺少方法 {methods - routes[path]}"


def test_internal_a2a_routes_match_documented_contract(client) -> None:
    routes = _collect_routes(client.app)
    for path, methods in INTERNAL_ROUTES.items():
        assert path in routes, f"内部 A2A 路径缺失：{path}"
        assert methods <= routes[path], f"{path} 缺少方法 {methods - routes[path]}"


def test_no_a2a_task_routes_under_public_api(client) -> None:
    """对外 API 不得暴露子任务视图（docs/08 §3.12 裁决）。"""
    routes = _collect_routes(client.app)
    leaked = sorted(path for path in routes if path.startswith("/api/v1/a2a"))
    assert leaked == [], f"/api/v1/a2a/* 不应存在，实际：{leaked}"


def test_internal_endpoints_require_coordinator_role(client) -> None:
    response = client.get("/internal/a2a/agents", headers=DEVELOPER)
    assert response.status_code == 403
    assert response.json()["code"] == "PERMISSION_DENIED"
    ok = client.get("/internal/a2a/agents", headers=COORDINATOR)
    assert ok.status_code == 200


def test_internal_endpoints_reject_wrong_protocol_version(client) -> None:
    response = client.get(
        "/internal/a2a/agents",
        headers={**COORDINATOR, "X-A2A-Protocol-Version": "9.9"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "PROTOCOL_VERSION_UNSUPPORTED"


def test_internal_submit_requires_idempotency_and_correlation_headers(client) -> None:
    payload = {
        "task_id": "review-000000000000000000000001",
        "parent_task_id": "task-00000000000000000000000001",
        "trace_id": "trace-000000000000000000000001",
        "agent_id": "review-agent",
        "task_type": "review",
        "protocol_version": "0.1",
        "status": "submitted",
        "required_output_types": ["Finding"],
        "deadline": "2030-01-01T00:00:00Z",
        "attempt": 1,
        "idempotency_key": "contract-key-0000001",
        "correlation_id": "task-00000000000000000000000001:review",
        "state_version": 1,
    }
    missing_key = client.post(
        "/internal/a2a/agents/review-agent/tasks", json=payload, headers=COORDINATOR
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    missing_correlation = client.post(
        "/internal/a2a/agents/review-agent/tasks",
        json=payload,
        headers={**COORDINATOR, "Idempotency-Key": "contract-key-0000001"},
    )
    assert missing_correlation.status_code == 400
    assert missing_correlation.json()["code"] == "INVALID_INPUT"


def test_write_endpoints_require_idempotency_key(client) -> None:
    """所有写端点在生产路由上都必须要求 Idempotency-Key。"""
    calls = [
        ("/api/v1/reviews/task-00000000000000000000000001/fixes", DEVELOPER, None),
        (
            "/api/v1/fixes/patch-00000000000000000000000001/approval",
            {"X-Actor-Id": "approver-1", "X-Actor-Role": "approver"},
            {"decision": "approve", "patch_version": 1, "reason": "x"},
        ),
        (
            "/api/v1/fixes/patch-00000000000000000000000001/merge",
            {"X-Actor-Id": "approver-1", "X-Actor-Role": "approver"},
            {"patch_version": 1},
        ),
        (
            "/api/v1/evals/run",
            {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"},
            {"modes": ["offline"], "case_limit": 1},
        ),
        (
            "/api/v1/reviews/task-00000000000000000000000001/resume",
            {"X-Actor-Id": "admin-1", "X-Actor-Role": "admin"},
            {"target_status": "REVIEWING", "expected_version": 2, "reason": "x"},
        ),
    ]
    for url, headers, body in calls:
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 400, url
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED", url


def test_openapi_lists_documented_paths_only(client) -> None:
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    assert not [path for path in paths if path.startswith("/api/v1/a2a")]
    assert "/api/v1/fixes/{patch_id}/approval" in paths
    assert "/internal/a2a/agents/{agent_id}/tasks" in paths
