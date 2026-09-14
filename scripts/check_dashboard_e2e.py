"""Dashboard 端到端验收：经反向代理（nginx 或 vite dev）走一遍工作台主流程。

用法：
    # 容器形态（nginx 代理 8080 → api:8099）
    python scripts/check_dashboard_e2e.py --base-url http://127.0.0.1:8080
    # 本地开发形态（vite dev 代理 5173 → 127.0.0.1:8099）
    python scripts/check_dashboard_e2e.py --base-url http://127.0.0.1:5173

该脚本验证的是 **Dashboard 实际使用的调用路径**（相对路径 + nginx/vite 代理），
而不是直连 API：
1. `/readyz` 经代理可用，并读取 `transport`（inprocess / http）；
2. 首页可访问（不是 404）；
3. 创建任务 → 轮询任务状态 → 意见 / 子任务 / Artifact / 审计尾部均可取到；
4. 补丁列表可取；
5. 审计：developer 403、admin 200（页面上的权限行为）；
6. 同幂等键重复提交返回原任务（`created=false`，`idempotent_replay`）；
7. 同幂等键不同请求体 → `409 IDEMPOTENCY_CONFLICT`。

退出码非 0 表示 Dashboard 主流程在代理链路上不可用。
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a.examples import EXAMPLE_INPUT_DIFF  # noqa: E402

DEVELOPER = {"X-Actor-Id": "ui-dev", "X-Actor-Role": "developer"}
ADMIN = {"X-Actor-Id": "ui-admin", "X-Actor-Role": "admin"}
TERMINAL = {"REVIEWED", "PENDING_APPROVAL", "NEEDS_HUMAN", "REJECTED", "MERGED", "FAILED"}


def _check(step: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {step}{(' — ' + detail) if detail else ''}")
    if not ok:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot Dashboard 端到端验收")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="Dashboard 代理地址")
    parser.add_argument("--mode", default="a2a", choices=["single", "a2a", "offline"])
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    base = args.base_url.rstrip("/")
    payload = {
        "input_type": "diff",
        "content": EXAMPLE_INPUT_DIFF,
        "base_commit": "ui-base-001",
        "context_policy": "function",
        "mode": args.mode,
    }
    key = f"dashboard-{uuid.uuid4().hex[:12]}"
    headers = {**DEVELOPER, "Idempotency-Key": key}

    with httpx.Client(base_url=base, timeout=60.0) as client:
        index = client.get("/")
        _check("首页可访问（Dashboard 静态产物）", index.status_code == 200, f"HTTP {index.status_code}")

        ready = client.get("/readyz")
        _check("代理 /readyz", ready.status_code == 200, f"HTTP {ready.status_code}")
        transport = ready.json().get("transport")
        _check("readyz 自证传输层", transport in {"inprocess", "http"}, f"transport={transport}")

        created = client.post("/api/v1/reviews", json=payload, headers=headers)
        _check("代理创建任务", created.status_code == 200, f"HTTP {created.status_code}")
        body = created.json()
        _check("首次提交 created=true", body["created"] is True)
        task_id = body["task"]["id"]

        detail: dict = {}
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            response = client.get(f"/api/v1/reviews/{task_id}", headers=DEVELOPER)
            _check("代理查询任务", response.status_code == 200, f"HTTP {response.status_code}")
            detail = response.json()
            if detail["task"]["status"] in TERMINAL:
                break
            time.sleep(0.5)
        _check(
            "任务到达终态",
            detail["task"]["status"] in TERMINAL,
            f"status={detail['task']['status']} mode={detail['task']['mode']}",
        )
        _check("意见可展示", bool(detail["comments"]), f"comments={len(detail['comments'])}")
        _check("子任务时间线可展示", bool(detail["child_tasks"]), f"child_tasks={len(detail['child_tasks'])}")
        _check("Artifact 可展示", bool(detail["artifacts"]), f"artifacts={len(detail['artifacts'])}")
        _check("审计尾部可展示", bool(detail["audit_tail"]), f"audit_tail={len(detail['audit_tail'])}")

        patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=DEVELOPER)
        _check("代理查询补丁列表", patches.status_code == 200, f"HTTP {patches.status_code}")

        forbidden = client.get("/api/v1/audit", headers=DEVELOPER)
        _check("审计对 developer 不可见", forbidden.status_code == 403, f"HTTP {forbidden.status_code}")
        allowed = client.get("/api/v1/audit", params={"task_id": task_id, "limit": 50}, headers=ADMIN)
        _check("审计对 admin 可见", allowed.status_code == 200, f"HTTP {allowed.status_code}")
        _check("审计事件非空", bool(allowed.json()), f"events={len(allowed.json())}")

        replay = client.post("/api/v1/reviews", json=payload, headers=headers)
        replay_body = replay.json()
        _check(
            "同键同请求返回原任务",
            replay.status_code == 200 and replay_body["created"] is False and replay_body["task"]["id"] == task_id,
            f"HTTP {replay.status_code} created={replay_body['created']}",
        )

        conflict = client.post(
            "/api/v1/reviews", json={**payload, "base_commit": "ui-base-002"}, headers=headers
        )
        _check(
            "同键异请求返回 IDEMPOTENCY_CONFLICT",
            conflict.status_code == 409 and conflict.json().get("code") == "IDEMPOTENCY_CONFLICT",
            f"HTTP {conflict.status_code} code={conflict.json().get('code')}",
        )

    print(f"\n结论： 通过（task_id={task_id}，传输层 transport={transport}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
