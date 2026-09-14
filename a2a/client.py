"""A2A HTTP/SSE 客户端（docs/05 §5）。

只实现 MVP 需要的受控子集：Agent Card 查询、Task 提交/查询/取消、SSE 事件订阅。
所有请求都带调用方身份、`trace_id`、`correlation_id`、协议版本与幂等键。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from domain.enums import PROTOCOL_VERSION
from domain.errors import CodePilotError, ErrorCode

DEFAULT_TIMEOUT_SECONDS = 10.0
COORDINATOR_ACTOR = "coordinator"


@dataclass(slots=True)
class SSEEvent:
    event_id: str
    event_type: str
    data: dict[str, Any]


def _error_from_response(response: httpx.Response) -> CodePilotError:
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 错误体
        payload = {}
    code_token = str(payload.get("code", ""))
    try:
        code = ErrorCode(code_token)
    except ValueError:
        code = ErrorCode.AGENT_UNAVAILABLE if response.status_code >= 500 else ErrorCode.INVALID_INPUT
    return CodePilotError(
        code,
        str(payload.get("message") or f"Agent 返回 HTTP {response.status_code}"),
        trace_id=payload.get("trace_id"),
        details={"status_code": response.status_code},
    )


class A2AClient:
    """同步 HTTP 客户端；由 ``A2AInvoker`` 在独立线程中调用。"""

    def __init__(
        self,
        base_url: str,
        *,
        actor_id: str = COORDINATOR_ACTOR,
        actor_role: str = "coordinator",
        protocol_version: str = PROTOCOL_VERSION,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.actor_id = actor_id
        self.actor_role = actor_role
        self.protocol_version = protocol_version
        self.timeout_seconds = timeout_seconds
        self._client = client

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- 头部 -------------------------------------------------------------------
    def headers(
        self,
        *,
        trace_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "X-Actor-Id": self.actor_id,
            "X-Actor-Role": self.actor_role,
            "X-A2A-Protocol-Version": self.protocol_version,
            "Accept": "application/json",
        }
        if trace_id:
            headers["X-Trace-Id"] = trace_id
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    # ---- 接口 -------------------------------------------------------------------
    def list_cards(self) -> list[dict[str, Any]]:
        response = self.client.get("/internal/a2a/agents", headers=self.headers())
        if response.status_code != 200:
            raise _error_from_response(response)
        return list(response.json())

    def get_card(self, agent_id: str) -> dict[str, Any]:
        response = self.client.get(f"/internal/a2a/agents/{agent_id}/card", headers=self.headers())
        if response.status_code != 200:
            raise _error_from_response(response)
        return dict(response.json())

    def submit_task(self, task: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(
            f"/internal/a2a/agents/{task['agent_id']}/tasks",
            json=task,
            headers=self.headers(
                trace_id=task.get("trace_id"),
                correlation_id=task.get("correlation_id"),
                idempotency_key=task.get("idempotency_key"),
            ),
        )
        if response.status_code not in {200, 201, 202}:
            raise _error_from_response(response)
        return dict(response.json())

    def get_task(self, task_id: str) -> dict[str, Any]:
        response = self.client.get(f"/internal/a2a/tasks/{task_id}", headers=self.headers())
        if response.status_code != 200:
            raise _error_from_response(response)
        return dict(response.json())

    def cancel_task(self, task_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        """请求取消子任务。

        取消是写命令，必须带幂等键（docs/08 §3.14）。未显式提供时使用由任务 ID 派生的
        确定性键，保证"同一任务的重复取消返回原结果"，而不是第二次报冲突或产生第二条账本。
        """
        key = idempotency_key or f"cancel:{task_id}:v1"
        response = self.client.post(
            f"/internal/a2a/tasks/{task_id}/cancel", headers=self.headers(idempotency_key=key)
        )
        if response.status_code != 200:
            raise _error_from_response(response)
        return dict(response.json())

    def stream_events(
        self,
        task_id: str,
        *,
        last_event_id: str | None = None,
        max_seconds: float = 60.0,
    ) -> Iterator[SSEEvent]:
        """订阅任务事件；SSE 断线由调用方退化为轮询补偿。"""
        headers = self.headers()
        headers["Accept"] = "text/event-stream"
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        with self.client.stream(
            "GET",
            f"/internal/a2a/tasks/{task_id}/events",
            headers=headers,
            timeout=httpx.Timeout(self.timeout_seconds, read=max_seconds),
        ) as response:
            if response.status_code != 200:
                raise _error_from_response(response)
            event_id = ""
            event_type = "message"
            data_lines: list[str] = []
            for raw in response.iter_lines():
                line = raw.rstrip("\r")
                if not line:
                    if data_lines:
                        payload = "\n".join(data_lines)
                        try:
                            parsed = json.loads(payload)
                        except json.JSONDecodeError:
                            parsed = {"raw": payload}
                        yield SSEEvent(event_id=event_id, event_type=event_type, data=parsed)
                    event_id, event_type, data_lines = "", "message", []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                value = value.lstrip()
                if field == "id":
                    event_id = value
                elif field == "event":
                    event_type = value
                elif field == "data":
                    data_lines.append(value)


def client_from_env(agent_id: str | None = None) -> A2AClient | None:
    """按 ``CODEPILOT_A2A_BASE_URL`` 构造客户端；未配置时返回 ``None``。

    支持 ``CODEPILOT_A2A_ROUTES=impact-agent=http://host:port`` 形式的按 Agent 路由。
    """
    base = os.environ.get("CODEPILOT_A2A_BASE_URL", "").strip()
    routes = _parse_routes(os.environ.get("CODEPILOT_A2A_ROUTES", ""))
    if agent_id and agent_id in routes:
        base = routes[agent_id]
    if not base:
        return None
    return A2AClient(base)


def _parse_routes(raw: str) -> dict[str, str]:
    routes: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        agent, _, url = item.partition("=")
        routes[agent.strip()] = url.strip().rstrip("/")
    return routes


__all__ = [
    "COORDINATOR_ACTOR",
    "DEFAULT_TIMEOUT_SECONDS",
    "A2AClient",
    "SSEEvent",
    "client_from_env",
]
