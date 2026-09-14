"""垂直切片冒烟脚本：对真实运行的服务执行一次审查闭环。

用法：
    python scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8099

步骤：POST /api/v1/reviews → 轮询 GET /api/v1/reviews/{id} → 打印子任务、Artifact、
意见与审计尾部。退出码非 0 表示闭环未到达 REVIEWED。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a2a.examples import EXAMPLE_INPUT_DIFF  # noqa: E402

HEADERS = {"X-Actor-Id": "smoke-dev", "X-Actor-Role": "developer"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 垂直切片冒烟")
    parser.add_argument("--base-url", default="http://127.0.0.1:8099")
    parser.add_argument("--mode", default=None, choices=[None, "single", "a2a", "offline"])
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    idempotency_key = f"smoke-{uuid.uuid4().hex[:12]}"
    payload = {
        "input_type": "diff",
        "content": EXAMPLE_INPUT_DIFF,
        "context_policy": "function",
        "base_commit": "synthetic-base-001",
    }
    if args.mode:
        payload["mode"] = args.mode

    with httpx.Client(base_url=args.base_url, timeout=30.0) as client:
        health = client.get("/readyz")
        print(f"readyz: {health.status_code} {health.text[:200]}")
        if health.status_code != 200:
            print("服务未就绪")
            return 2

        created = client.post(
            "/api/v1/reviews", json=payload, headers={**HEADERS, "Idempotency-Key": idempotency_key}
        )
        print(f"POST /api/v1/reviews -> {created.status_code}")
        if created.status_code != 200:
            print(created.text)
            return 1
        body = created.json()
        task_id = body["task"]["id"]
        print(f"task_id={task_id} created={body['created']}")

        deadline = time.time() + args.timeout
        detail = None
        while time.time() < deadline:
            response = client.get(f"/api/v1/reviews/{task_id}", headers=HEADERS)
            response.raise_for_status()
            detail = response.json()
            status = detail["task"]["status"]
            print(f"  poll: status={status} state_version={detail['task']['state_version']}")
            if status in {"REVIEWED", "NEEDS_HUMAN", "FAILED", "REJECTED", "MERGED"}:
                break
            time.sleep(0.5)

        if detail is None:
            print("未获取到任务详情")
            return 1

        print("\n== 父任务 ==")
        print(
            json.dumps(
                {
                    "status": detail["task"]["status"],
                    "state_version": detail["task"]["state_version"],
                    "summary": detail["task"]["summary"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print("\n== 子任务 ==")
        for child in detail["child_tasks"]:
            print(
                f"  {child['task_type']:<7} {child['agent_id']:<13} {child['status']:<10} "
                f"attempt={child['attempt']} transport={child['transport']}"
            )
        print("\n== Artifact ==")
        for artifact in detail["artifacts"]:
            print(
                f"  {artifact['artifact_type']:<16} hash={artifact['content_hash'][:22]}… "
                f"validated={artifact['validated']} size={artifact['size_bytes']}"
            )
        print("\n== 意见 ==")
        for comment in detail["comments"]:
            print(
                f"  [{comment['severity']:<8}] {comment['rule_id']:<24} "
                f"{comment['file']}:{comment['line']} conf={comment['confidence']:.2f} "
                f"({comment['confidence_level']})"
            )
        print("\n== 审计尾部 ==")
        for event in detail["audit_tail"]:
            print(f"  {event['event_type']:<24} {event.get('agent_id') or '-'}")

        ok = detail["task"]["status"] == "REVIEWED" and detail["artifacts"]
        print("\n结论：", "通过" if ok else "未通过")
        return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
