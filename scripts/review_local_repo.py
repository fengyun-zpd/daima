"""从本地 Git 工作目录启动一次 CodePilot 审查。

这个适配器只负责把 ``base..head`` 转成 unified diff，再调用现有的 HTTP API。
CodePilot 服务仍以 Diff 作为输入；服务端不会读取调用方的本地路径，也不会把补丁写回
调用方当前工作目录。审批后的结果写入服务自己的 ``var/repos/<task_id>`` 任务分支。

示例（PowerShell）：
    python scripts/review_local_repo.py --repo . --base HEAD~1 --trigger-fix
    python scripts/review_local_repo.py --repo D:/src/demo --base main --patch-output var/demo.patch
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMINAL_REVIEW_STATUSES = {"REVIEWED", "PENDING_APPROVAL", "NEEDS_HUMAN", "FAILED", "REJECTED", "MERGED"}
DEFAULT_HEADERS = {"X-Actor-Id": "local-dev", "X-Actor-Role": "developer"}


class LocalRepoError(RuntimeError):
    """本地仓库输入或 Git 命令错误。"""


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise LocalRepoError(f"git {' '.join(args)} 失败：{message}")
    return completed.stdout


def resolve_repo(path: str | Path) -> Path:
    """解析并验证 Git 工作目录，拒绝文件路径和不存在路径。"""
    candidate = Path(path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_dir():
        raise LocalRepoError(f"仓库目录不存在：{candidate}")
    top = Path(_git(candidate, "rev-parse", "--show-toplevel").strip()).resolve()
    return top


def build_review_input(repo: Path, *, base: str, head: str) -> tuple[str, str]:
    """生成审查 diff 与基线 commit SHA。

    ``--binary`` 保留 Git diff 的标准格式；CodePilot 的工作区会只解析 Python 文本文件。
    空 diff 被拒绝，避免创建没有可审查内容的任务。
    """
    base_commit = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").strip()
    _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    diff = _git(repo, "diff", "--binary", "--no-ext-diff", f"{base}..{head}", "--")
    if not diff.strip():
        raise LocalRepoError(f"{base}..{head} 没有文件变更")
    return diff, base_commit


def _headers(actor_id: str, role: str, idempotency_key: str | None = None) -> dict[str, str]:
    result = {"X-Actor-Id": actor_id, "X-Actor-Role": role}
    if idempotency_key:
        result["Idempotency-Key"] = idempotency_key
    return result


def _raise_http(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    raise LocalRepoError(f"HTTP {response.status_code}: {payload}")


def _poll_review(client: httpx.Client, task_id: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/reviews/{task_id}", headers=headers)
        _raise_http(response)
        detail = response.json()
        status = detail["task"]["status"]
        if status in TERMINAL_REVIEW_STATUSES:
            return detail
        time.sleep(0.5)
    raise LocalRepoError(f"等待任务 {task_id} 超时（{timeout:.0f}s）")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    repo = resolve_repo(args.repo)
    diff, base_commit = build_review_input(repo, base=args.base, head=args.head)
    if args.diff_output:
        output = Path(args.diff_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(diff, encoding="utf-8", newline="\n")
        print(f"diff 已保存：{output.resolve()}")
    if args.dry_run:
        print(f"仓库：{repo}")
        print(f"基线：{base_commit}（{args.base}）")
        print(f"diff 字节数：{len(diff.encode('utf-8'))}")
        return 0

    actor_id = args.actor_id
    developer_headers = _headers(actor_id, "developer")
    key_prefix = args.idempotency_prefix or f"local-{uuid.uuid4().hex[:12]}"
    payload: dict[str, Any] = {
        "input_type": "diff",
        "content": diff,
        "base_commit": base_commit,
        "context_policy": args.context_policy,
    }
    if args.mode:
        payload["mode"] = args.mode

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=args.http_timeout) as client:
        ready = client.get("/readyz")
        _raise_http(ready)
        created = client.post(
            "/api/v1/reviews",
            json=payload,
            headers=_headers(actor_id, "developer", f"{key_prefix}-review"),
        )
        _raise_http(created)
        task = created.json()["task"]
        task_id = task["id"]
        detail = _poll_review(client, task_id, developer_headers, args.timeout)
        output_dir = Path(args.output_dir)
        report_path = Path(args.report) if args.report else output_dir / f"{task_id}.json"
        _write_json(report_path, detail)
        print(f"task_id={task_id} status={detail['task']['status']}")
        print(f"审查报告：{report_path.resolve()}")

        if not args.trigger_fix:
            return 0 if detail["task"]["status"] == "REVIEWED" else 1

        fix = client.post(
            f"/api/v1/reviews/{task_id}/fixes",
            headers=_headers(actor_id, "developer", f"{key_prefix}-fix"),
        )
        _raise_http(fix)
        detail = _poll_review(client, task_id, developer_headers, args.timeout)
        patches_response = client.get(f"/api/v1/reviews/{task_id}/patches", headers=developer_headers)
        _raise_http(patches_response)
        patches = patches_response.json()
        if patches:
            patch = patches[-1]
            patch_path = Path(args.patch_output) if args.patch_output else output_dir / f"{task_id}.patch"
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(patch["diff"], encoding="utf-8", newline="\n")
            print(f"候选补丁：{patch_path.resolve()} status={patch['status']}")
        _write_json(report_path, detail)

        if args.approve:
            if detail["task"]["status"] != "PENDING_APPROVAL" or not patches:
                raise LocalRepoError("任务未进入 PENDING_APPROVAL，不能审批")
            approver_id = args.approver_id
            approval = client.post(
                f"/api/v1/fixes/{patch['id']}/approval",
                json={"decision": "approve", "patch_version": patch["patch_version"], "reason": args.approval_reason},
                headers=_headers(approver_id, "approver", f"{key_prefix}-approval"),
            )
            _raise_http(approval)
            print(f"审批完成：patch={patch['id']} version={patch['patch_version']}")

        if args.merge:
            if not args.approve:
                raise LocalRepoError("--merge 必须与 --approve 一起使用")
            merged = client.post(
                f"/api/v1/fixes/{patch['id']}/merge",
                json={"patch_version": patch["patch_version"]},
                headers=_headers(args.approver_id, "approver", f"{key_prefix}-merge"),
            )
            _raise_http(merged)
            print(json.dumps(merged.json(), ensure_ascii=False, indent=2))

    return 0 if detail["task"]["status"] in {"PENDING_APPROVAL", "MERGED", "REVIEWED"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从本地 Git 目录启动 CodePilot 审查")
    parser.add_argument("--repo", default=".", help="Git 工作目录，默认当前目录")
    parser.add_argument("--base", default="HEAD~1", help="基线 ref，默认 HEAD~1")
    parser.add_argument("--head", default="HEAD", help="待审查 ref，默认 HEAD")
    parser.add_argument("--base-url", default=os.environ.get("CODEPILOT_BASE_URL", "http://127.0.0.1:8099"))
    parser.add_argument("--mode", choices=["single", "a2a", "offline"], default=None)
    parser.add_argument("--context-policy", choices=["minimal", "function", "module"], default="function")
    parser.add_argument("--timeout", type=float, default=120.0, help="等待任务终态的秒数")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--actor-id", default="local-dev")
    parser.add_argument("--approver-id", default="local-approver")
    parser.add_argument("--approval-reason", default="本地演示：验证证据与影响范围符合预期")
    parser.add_argument("--idempotency-prefix", default=None)
    parser.add_argument("--output-dir", default="var/local-review")
    parser.add_argument("--report", default=None, help="审查报告 JSON 路径")
    parser.add_argument("--diff-output", default=None, help="同时保存提交给 API 的 diff")
    parser.add_argument("--patch-output", default=None, help="候选补丁保存路径")
    parser.add_argument("--dry-run", action="store_true", help="只生成 diff，不调用服务")
    parser.add_argument("--trigger-fix", action="store_true", help="审查完成后触发 Fix/Verify")
    parser.add_argument("--approve", action="store_true", help="Fix/Verify 通过后记录审批")
    parser.add_argument("--merge", action="store_true", help="审批后写入 CodePilot 任务分支")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (LocalRepoError, httpx.HTTPError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
