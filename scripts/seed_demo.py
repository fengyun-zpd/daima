"""最小演示数据：创建一个可跑通"审查 → 修复 → 沙箱验证 → 待审批"的演示任务。

用法：
    python scripts/seed_demo.py                     # 直接在本进程内跑完整链路
    python scripts/seed_demo.py --base-url http://127.0.0.1:8099   # 通过 API 触发（后台执行）

幂等键默认每次运行都不同（因此可以反复演示），并会打印出来：
用同一个键再跑一次即可演示"同键同请求返回原任务，不产生第二份副作用"。

演示项目包含两类可自动修复缺陷（硬编码密钥、shell=True）与可运行的测试，
因此 Verify 阶段能给出真实覆盖率证据。
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
import time
import uuid
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEMO_FILES = {
    "app/__init__.py": "",
    "app/config.py": '''"""演示项目的配置模块（含两类可自动修复缺陷）。"""
import os
import subprocess

API_KEY = "sk-live-demo-1234567890"


def list_dir(directory):
    return subprocess.run("ls " + directory, shell=True)


def load_text(path):
    with open(path) as handle:
        return handle.read()
''',
    "app/db.py": '''"""演示项目的查询模块（含 SQL 拼接缺陷）。"""


def find_user(cursor, name):
    return cursor.execute("SELECT * FROM users WHERE name='" + name + "'")
''',
    "tests/test_demo.py": '''import os

os.environ.setdefault("API_KEY", "demo-key")

from app import config, db


def test_list_dir_runs():
    assert config.list_dir("/tmp") is not None


def test_api_key_comes_from_env():
    assert config.API_KEY == "demo-key"


def test_find_user_passes_parameters():
    captured = {}

    class Cursor:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params
            return "ok"

    assert db.find_user(Cursor(), "alice") == "ok"
    assert captured["params"]
''',
}


def build_zip() -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in DEMO_FILES.items():
            archive.writestr(path, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def run_local() -> int:
    from agents.coordinator.coordinator import ReviewRequest
    from apps.api.deps import build_container
    from domain.enums import ContextPolicy, InputType, RunMode
    from repositories.records import ApprovalStore, CommentStore, PatchStore
    from repositories.store import ReviewTaskStore

    container = build_container(auto_create=False)
    task, created = container.coordinator.create_review(
        ReviewRequest(
            input_type=InputType.ZIP,
            content=build_zip(),
            base_commit="synthetic-base-001",
            actor_id="demo",
            actor_role="developer",
            idempotency_key="demo-seed-000001",
            context_policy=ContextPolicy.FUNCTION,
            mode=RunMode.A2A,
        )
    )
    print(f"创建任务：{task.id}（新建={created}）")

    reviewed = container.coordinator.run(task.id)
    print(f"审查完成：状态={reviewed.status} 意见={reviewed.findings} 影响文件={reviewed.affected_files}")

    fixed = container.coordinator.run(task.id, auto_fix=True)
    print(f"修复与验证：状态={fixed.status} 下一步={fixed.next_action}")
    if fixed.error_code:
        print(f"  （门禁未通过：{fixed.error_code} - {fixed.error_message}）")

    with container.session() as session:
        comments = CommentStore(session).list_by_task(task.id)
        patches = PatchStore(session).list_by_task(task.id)
        approvals = ApprovalStore(session).list_by_task(task.id)
        current = ReviewTaskStore(session).get(task.id)

    print("\n审查意见：")
    for row in comments:
        print(f"  [{row.severity:<8}] {row.rule_id:<24} {row.file}:{row.line} conf={row.confidence:.2f}")
    print("候选补丁：")
    for patch in patches:
        print(
            f"  v{patch.patch_version} 文件={patch.changed_files} 覆盖率变化={patch.coverage_delta} "
            f"TEST_GAP={patch.test_gap} 状态={patch.status}"
        )
    print(f"审批记录：{len(approvals)} 条；父任务状态：{current.status}")
    print("\n下一步可执行：")
    if patches:
        print(
            "  curl -X POST http://127.0.0.1:8099/api/v1/fixes/"
            f"{patches[-1].id}/approval -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver' "
            f"-H 'Idempotency-Key: demo-approve-01' -H 'Content-Type: application/json' "
            f"-d '{{\"decision\":\"approve\",\"patch_version\":{patches[-1].patch_version},\"reason\":\"演示\"}}'"
        )
    return 0 if current.status in {"PENDING_APPROVAL", "REVIEWED"} else 1


def run_via_api(base_url: str, *, timeout: float = 180.0, idempotency_key: str | None = None) -> int:
    headers = {"X-Actor-Id": "demo", "X-Actor-Role": "developer"}
    # 默认每次运行使用新的幂等键：演示脚本必须可以反复执行。
    # 显式传 --idempotency-key 时复用同一个键，用于演示幂等重放。
    create_key = idempotency_key or f"demo-seed-{uuid.uuid4().hex[:12]}"
    fix_key = f"{create_key}-fix1"
    print(f"幂等键：{create_key}（用同一个键再跑一次即可演示幂等重放）")
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        created = client.post(
            "/api/v1/reviews",
            json={
                "input_type": "zip",
                "content": build_zip(),
                "context_policy": "function",
                "base_commit": "synthetic-base-001",
            },
            headers={**headers, "Idempotency-Key": create_key},
        )
        if created.status_code != 200:
            print(f"创建失败：{created.status_code} {created.text}")
            if created.status_code == 409:
                print("提示：同键异请求会得到 409 IDEMPOTENCY_CONFLICT；换一个 --idempotency-key 或直接重跑即可。")
            return 1
        task_id = created.json()["task"]["id"]
        replayed = created.json()["created"] is False
        print(f"创建任务：{task_id}（created={created.json()['created']}）")

        deadline = time.time() + timeout
        detail: dict = {}
        while time.time() < deadline:
            detail = client.get(f"/api/v1/reviews/{task_id}", headers=headers).json()
            if detail["task"]["status"] not in {"DRAFT", "REVIEWING"}:
                break
            time.sleep(0.5)
        print(f"审查完成：状态={detail['task']['status']} 意见={len(detail['comments'])}")

        if replayed:
            # 幂等重放：不重复调度，直接展示已有结果
            patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=headers).json()
            print(f"幂等重放：未触发新的编排；已有补丁 {len(patches)} 个")
            return 0

        client.post(
            f"/api/v1/reviews/{task_id}/fixes",
            headers={**headers, "Idempotency-Key": fix_key},
        )
        while time.time() < deadline:
            detail = client.get(f"/api/v1/reviews/{task_id}", headers=headers).json()
            if detail["task"]["status"] in {"PENDING_APPROVAL", "NEEDS_HUMAN", "REJECTED", "MERGED"}:
                break
            time.sleep(0.5)
        patches = client.get(f"/api/v1/reviews/{task_id}/patches", headers=headers).json()
        print(f"修复与验证：状态={detail['task']['status']} 补丁数={len(patches)}")
        for patch in patches:
            print(
                f"  v{patch['patch_version']} 状态={patch['status']} 文件={patch['changed_files']} "
                f"覆盖率变化={patch['coverage_delta']}"
            )
        evidence = (detail["task"].get("summary") or {}).get("verify_evidence")
        if evidence:
            print(
                f"  VerifyEvidence: degraded={evidence['sandbox_degraded']} "
                f"apply_check={evidence['apply_check']['ok']} "
                f"tests={evidence['unit_tests']['passed']} passed / {evidence['unit_tests']['failed']} failed"
            )
        if detail["task"].get("error_code"):
            print(f"  （门禁结果：{detail['task']['error_code']} - {detail['task'].get('error_message')}）")
        print(f"\n任务 ID：{task_id}")
        if patches:
            print(
                "下一步（approver 审批后合并）：\n"
                f"  python -c \"import httpx;h={{'X-Actor-Id':'approver-1','X-Actor-Role':'approver','Idempotency-Key':'demo-approve-01'}};"
                f"print(httpx.post('{base_url}/api/v1/fixes/{patches[-1]['id']}/approval',"
                f"json={{'decision':'approve','patch_version':{patches[-1]['patch_version']},'reason':'demo'}},headers=h,timeout=30).text)\""
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 最小演示数据")
    parser.add_argument("--base-url", default=None, help="通过 API 触发（否则在本进程内执行）")
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="固定幂等键（用于演示幂等重放；默认每次运行随机）",
    )
    args = parser.parse_args(argv)
    if args.base_url:
        return run_via_api(args.base_url, idempotency_key=args.idempotency_key)
    return run_local()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
