# CodePilot API 示例

所有示例都假定服务运行在 `http://127.0.0.1:8099`（`docker compose up -d postgres api` 后可用）。

- 鉴权：请求头 `X-Actor-Id` 与 `X-Actor-Role`（`developer` / `approver` / `admin` / `coordinator`）
- 写请求：必须带 `Idempotency-Key`（长度 ≥ 8）；**同键同请求返回原结果**（附 `idempotent_replay: true`），
  同键异请求返回 `409 IDEMPOTENCY_CONFLICT`（语义与覆盖命令见 `docs/08` §3.14、`docs/03` §6）
- 错误：`{"code": "...", "message": "...", "trace_id": "..."}`；完整错误码见 `docs/08-错误码与协议冻结.md`
- Dashboard 也是本 API 的调用方，且只使用相对路径，因此示例中的路径同样适用于
  `http://127.0.0.1:8080`（容器形态，见 README §1.3）

## 0. 健康检查与能力发现

```bash
curl http://127.0.0.1:8099/healthz
curl http://127.0.0.1:8099/readyz
# {"status":"ready","database":"...","agents":["fix-agent",...],"tools":["get_diff",...],
#  "recoverable_tasks":0,"transport":"inprocess"}
# transport: inprocess = 单进程/未配置 A2A 端点；http = Coordinator 走 HTTP/SSE 调用拆分出的 Agent 服务

curl http://127.0.0.1:8099/api/v1/agents -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'
curl http://127.0.0.1:8099/api/v1/agents/impact-agent/card -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'
```

## 1. 创建审查任务（Diff）

```bash
curl -X POST http://127.0.0.1:8099/api/v1/reviews \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer' \
  -H 'Idempotency-Key: demo-00000001' \
  -d '{
        "input_type": "diff",
        "base_commit": "synthetic-base-001",
        "context_policy": "function",
        "content": "diff --git a/app/config.py b/app/config.py\n--- a/app/config.py\n+++ b/app/config.py\n@@ -1,2 +1,3 @@\n import os\n import subprocess\n+API_KEY = \"sk-live-abcdef123456\"\n"
      }'
```

响应（`200`）：

```json
{
  "task": {"id": "task-01J...", "status": "DRAFT", "state_version": 1, "mode": "a2a", "trace_id": "trace-01J..."},
  "created": true,
  "detail_url": "/api/v1/reviews/task-01J..."
}
```

- 重复提交（同 `Idempotency-Key` + 同内容）返回同一任务且 `created=false`；
- 同 `Idempotency-Key` 但内容不同 → `409 IDEMPOTENCY_CONFLICT`；
- 缺 `Idempotency-Key` → `400 IDEMPOTENCY_KEY_REQUIRED`；
- 非法 diff（空、路径穿越、非 Python）→ `400 INVALID_INPUT`。

## 2. 创建审查任务（ZIP，可用于 Fix/Verify 链路）

ZIP 需要 base64 编码（`zip` 内为 UTF-8 文本的 `.py` 文件）：

```bash
python - <<'PY'
import base64, io, zipfile, json, pathlib
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("app/__init__.py", "")
    z.writestr("app/config.py", 'import os\nAPI_KEY = "sk-live-abcdef123456"\n')
    z.writestr("tests/test_config.py", "import os\nos.environ.setdefault('API_KEY','k')\ndef test_import():\n    import app.config  # noqa\n")
pathlib.Path("payload.json").write_text(json.dumps({
    "input_type": "zip",
    "content": base64.b64encode(buf.getvalue()).decode(),
    "context_policy": "function",
    "base_commit": "synthetic-base-001",
}), encoding="utf-8")
PY

curl -X POST http://127.0.0.1:8099/api/v1/reviews \
  -H 'Content-Type: application/json' -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer' \
  -H 'Idempotency-Key: demo-zip-000001' --data-binary @payload.json
```

## 3. 查询任务、子任务、Artifact 与意见

```bash
TASK=<task_id>
curl http://127.0.0.1:8099/api/v1/reviews/$TASK -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'
```

```json
{
  "task": {"id": "task-01J...", "status": "REVIEWED", "state_version": 3,
           "summary": {"findings": 2, "risk_level": "low", "affected_files": ["app/config.py"],
                       "wide_impact": false, "human_review_required": false}},
  "child_tasks": [{"id": "review-01J...", "agent_id": "review-agent", "status": "completed", "attempt": 1,
                   "transport": "inprocess", "deadline": "2026-09-13T12:00:45Z"}],
  "artifacts": [{"id": "artifact-01J...", "artifact_type": "Finding", "schema_version": "1.0",
                 "content_hash": "sha256:...", "validated": true}],
  "comments": [{"file": "app/config.py", "line": 3, "rule_id": "R002_HARDCODED_SECRET",
                "severity": "critical", "confidence": 1.0, "confidence_level": "confirmed",
                "auto_fixable": true, "fix_category": "hardcoded_secret"}],
  "audit_tail": [{"event_type": "task_started", "created_at": "..."}]
}
```

只取意见：

```bash
curl "http://127.0.0.1:8099/api/v1/reviews/$TASK/comments?include_suppressed=false" \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'
```

## 4. SSE 事件流（支持断线续接）

```bash
curl -N http://127.0.0.1:8099/api/v1/reviews/$TASK/events \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'

# 断线后从游标续接（事件 id 即审计事件主键）
curl -N http://127.0.0.1:8099/api/v1/reviews/$TASK/events \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer' -H 'Last-Event-ID: audit-01J...'
```

```text
id: audit-01J...
event: artifact_received
data: {"event_id":"audit-01J...","trace_id":"trace-01J...","task_id":"review-01J...","event_type":"artifact_received","artifact_id":"artifact-01J...","summary":{"artifact_type":"Finding","content_hash":"sha256:..."}}
```

## 5. 触发修复并读取补丁

```bash
curl -X POST http://127.0.0.1:8099/api/v1/reviews/$TASK/fixes \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer' -H 'Idempotency-Key: demo-fix-0001'
# {"task_id":"task-01J...","status":"REVIEWED","patch_id":null,"detail_url":"/api/v1/reviews/task-01J..."}

curl http://127.0.0.1:8099/api/v1/reviews/$TASK/patches -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'
curl http://127.0.0.1:8099/api/v1/fixes/<patch_id> -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver'
```

```json
[{
  "id": "patch-01J...",
  "patch_version": 1,
  "patch_hash": "sha256:...",
  "status": "candidate",
  "target_branch": "codepilot/task-01J...",
  "changed_files": ["app/config.py"],
  "scope_drift": false,
  "wide_impact": false,
  "test_gap": false,
  "coverage_before": 0.83, "coverage_after": 0.83, "coverage_delta": 0.0,
  "sandbox_run_id": "verify-review-01J...",
  "diff": "--- a/app/config.py\n+++ b/app/config.py\n@@ ...\n-API_KEY = \"sk-live-abcdef123456\"\n+API_KEY = os.environ[\"API_KEY\"]\n",
  "approvals": []
}]
```

## 6. 审批与合并

```bash
# 批准（仅 approver；patch_version 必须等于当前补丁版本）
curl -X POST http://127.0.0.1:8099/api/v1/fixes/<patch_id>/approval \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver' -H 'Idempotency-Key: demo-approve-01' \
  -d '{"decision":"approve","patch_version":1,"reason":"测试证据充分，影响范围符合预期"}'

# 拒绝（必须填写原因；父任务进入 REJECTED）
curl -X POST http://127.0.0.1:8099/api/v1/fixes/<patch_id>/approval \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver' -H 'Idempotency-Key: demo-reject-01' \
  -d '{"decision":"reject","patch_version":1,"reason":"缺少回归测试证据"}'

# 合并（审批通过后写入任务分支 codepilot/<task_id>）
curl -X POST http://127.0.0.1:8099/api/v1/fixes/<patch_id>/merge \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver' -H 'Idempotency-Key: demo-merge-0001' \
  -d '{"patch_version":1}'
# {"task_id":"task-01J...","branch":"codepilot/task-01J...","commit":"<sha>","task_status":"MERGED","merged_by":"approver-1"}
```

必测拒绝路径：

| 场景 | 结果 |
|---|---|
| developer 审批 | `403 FORBIDDEN` |
| 旧版本审批（`patch_version` 不匹配） | `409 VERSION_CONFLICT` |
| 拒绝未填写原因 | `400 INVALID_INPUT` |
| 未审批直接合并 | `403 FORBIDDEN` |
| 重复合并同一版本（同键同请求） | `200`，返回首次合并的分支与 commit，附 `idempotent_replay: true`（**不是**状态冲突） |
| 同键但请求体不同（如 `patch_version` 不同） | `409 IDEMPOTENCY_CONFLICT` |

## 7. 审计与人工恢复

```bash
curl "http://127.0.0.1:8099/api/v1/audit?task_id=$TASK&limit=50" -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'
curl "http://127.0.0.1:8099/api/v1/audit?trace_id=<trace_id>" -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'

# 人工恢复：仅 admin，仅从 NEEDS_HUMAN 出发，且永远不能进入 MERGED
curl -X POST http://127.0.0.1:8099/api/v1/reviews/$TASK/resume \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin' -H 'Idempotency-Key: demo-resume-0001' \
  -d '{"target_status":"REVIEWING","expected_version":5,"reason":"补充上下文后继续"}'
```

## 8. 内部 A2A 接口（仅 coordinator/admin）

```bash
curl http://127.0.0.1:8099/internal/a2a/agents \
  -H 'X-Actor-Id: coordinator' -H 'X-Actor-Role: coordinator' -H 'X-A2A-Protocol-Version: 0.1'
curl http://127.0.0.1:8099/internal/a2a/agents/review-agent/card \
  -H 'X-Actor-Id: coordinator' -H 'X-Actor-Role: coordinator'

curl http://127.0.0.1:8099/internal/a2a/tasks/<child_task_id> \
  -H 'X-Actor-Id: coordinator' -H 'X-Actor-Role: coordinator'

curl -X POST http://127.0.0.1:8099/internal/a2a/tasks/<child_task_id>/cancel \
  -H 'X-Actor-Id: coordinator' -H 'X-Actor-Role: coordinator' \
  -H 'Idempotency-Key: <child_task_id>:cancel'
# developer 角色访问 → 403 PERMISSION_DENIED；协议版本不匹配 → 400 PROTOCOL_VERSION_UNSUPPORTED
# 取消是写命令：缺 Idempotency-Key → 400 IDEMPOTENCY_KEY_REQUIRED；同键重复取消返回原结果
```

## 9. 评测

```bash
curl -X POST http://127.0.0.1:8099/api/v1/evals/run \
  -H 'Content-Type: application/json' -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin' \
  -H 'Idempotency-Key: demo-eval-0001' \
  -d '{"modes":["single","a2a","offline"],"case_limit":3,"runs_per_case":1,"with_fix":false}'

curl http://127.0.0.1:8099/api/v1/evals/<run_id> -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'
```

```json
{
  "run_id": "eval-01J...",
  "status": "completed",
  "summary": {
    "modes": {
      "single": {"finding_recall": 1.0, "finding_precision": 1.0, "pass_at_3": 1.0, "pass_pow_3": 1.0,
                 "route_correctness": 1.0, "artifact_schema_pass_rate": 1.0,
                 "task_convergence_rate": 1.0, "trace_complete_rate": 1.0, "latency_ms_p50": 850},
      "a2a": {"...": "..."},
      "offline": {"...": "..."}
    },
    "security_invariants": {"unauthorized_write": 0, "unapproved_merge": 0,
                            "duplicate_side_effects": 0, "sandbox_escape": 0,
                            "illegal_state_transition": 0},
    "comparison": {"recall_delta": 0.0, "precision_delta": 0.0, "latency_p50_delta_ms": 120,
                   "latency_regression_exceeds_30pct": false}
  }
}
```

## 10. 故障注入（仅测试环境）

`CODEPILOT_FAULT_INJECTION=1` 时可用：

```bash
curl -X POST http://127.0.0.1:8099/internal/a2a/_faults \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: coordinator' -H 'X-Actor-Role: coordinator' \
  -d '{"agent_id":"impact-agent","tamper_artifact":true}'
curl http://127.0.0.1:8099/internal/a2a/_faults \
  -H 'X-Actor-Id: coordinator' -H 'X-Actor-Role: coordinator'
# 未开启开关时返回 403 FORBIDDEN
```

也可直接用脚本运行全部 13 个故障场景：

```bash
python scripts/run_fault_matrix.py            # 含 Docker 沙箱场景
python scripts/run_fault_matrix.py --no-sandbox
```
