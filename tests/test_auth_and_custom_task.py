from __future__ import annotations

from fastapi.testclient import TestClient

from a2a.examples import EXAMPLE_INPUT_DIFF
from apps.api.main import create_app
from tests.test_phase3_vertical_slice import make_container


def _register(client: TestClient, *, employee_id: str = "BJ-001", username: str = "xiaoming") -> dict:
    response = client.post(
        "/api/v1/auth/register",
        json={"employee_id": employee_id, "username": username, "password": "password-123"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_register_token_and_logout_lifecycle(tmp_path) -> None:
    app = create_app(
        container=make_container(tmp_path), recover_on_start=False, schedule_on_create=False
    )
    with TestClient(app) as client:
        registered = _register(client)
        headers = {"Authorization": f"Bearer {registered['token']}"}

        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["employee_id"] == "BJ-001"
        assert me.json()["username"] == "xiaoming"
        assert me.json()["role"] == "developer"

        login = client.post(
            "/api/v1/auth/login", json={"account": "xiaoming", "password": "password-123"}
        )
        assert login.status_code == 200

        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_custom_task_id_is_visible_and_unique_per_account(tmp_path) -> None:
    app = create_app(
        container=make_container(tmp_path), recover_on_start=False, schedule_on_create=False
    )
    with TestClient(app) as client:
        registered = _register(client)
        headers = {
            "Authorization": f"Bearer {registered['token']}",
            "Idempotency-Key": "custom-task-create-001",
        }
        payload = {
            "input_type": "diff",
            "content": EXAMPLE_INPUT_DIFF,
            "base_commit": "本地上传",
            "custom_task_id": "2026-001",
        }

        created = client.post("/api/v1/reviews", json=payload, headers=headers)
        assert created.status_code == 200, created.text
        assert created.json()["task"]["custom_task_id"] == "2026-001"

        history = client.get(
            "/api/v1/reviews", headers={"Authorization": headers["Authorization"]}
        )
        assert history.status_code == 200
        assert history.json()[0]["custom_task_id"] == "2026-001"

        duplicate_headers = {**headers, "Idempotency-Key": "custom-task-create-002"}
        duplicate = client.post("/api/v1/reviews", json=payload, headers=duplicate_headers)
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "CONFLICT"
