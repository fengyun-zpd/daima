# API 与数据模型

## 1. API 约定

- Base URL：`/api/v1`
- 鉴权：请求头 `X-Actor-Id`、`X-Actor-Role`（MVP 使用演示身份，生产应替换为 JWT/OIDC）
- 写请求必须带 `Idempotency-Key`。
- 错误格式：`{"code":"...","message":"...","trace_id":"..."}`。

## 2. 接口清单

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/reviews` | developer | 上传 Diff/ZIP 创建任务 |
| GET | `/reviews/{id}` | developer/approver | 查询任务和状态 |
| GET | `/reviews/{id}/events` | developer/approver | SSE 事件流 |
| GET | `/reviews/{id}/comments` | developer/approver | 意见、置信度、调用图和证据 |
| POST | `/reviews/{id}/fixes` | developer | 生成候选补丁 |
| GET | `/reviews/{id}/patches` | developer/approver | 该任务的候选补丁列表 |
| GET | `/fixes/{id}` | developer/approver | 补丁、范围、覆盖率和测试结果 |
| POST | `/fixes/{id}/approval` | approver | 批准或拒绝 |
| POST | `/fixes/{id}/merge` | approver | 应用到任务分支 |
| POST | `/reviews/{id}/resume` | admin | 人工恢复 NEEDS_HUMAN 任务 |
| GET | `/audit` | admin | 查询追加式审计事件 |
| GET | `/agents` | admin/coordinator | 获取 Agent Card 列表 |
| GET | `/agents/{agent_id}/card` | admin/coordinator | 获取指定 Agent 能力声明 |
| POST | `/evals/run` | admin | 运行黄金集 |
| GET | `/evals/{id}` | admin | 获取评测报告 |

运维端点（不在 `/api/v1` 下，无鉴权，仅返回无敏感信息）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 进程存活：`{"status":"ok"}` |
| GET | `/readyz` | 就绪：数据库、Agent Card 列表、工具列表、待恢复任务数，以及 **`transport`**（`inprocess` / `http`），用于验收拆分形态是否真的走 HTTP A2A |

### 2.1 A2A Task 接口（内部，不在 `/api/v1` 下）

对外 API **只暴露父任务视图**：`/api/v1/a2a/*` 不提供子任务创建/查询/SSE/取消（裁决见 docs/08 §3.12）。
A2A Task 的完整生命周期在内部接口上实现，且只允许 coordinator/admin 调用：

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| GET | `/internal/a2a/agents` | coordinator/admin | 健康 Agent Card 列表 |
| GET | `/internal/a2a/agents/{agent_id}/card` | coordinator/admin | 指定 Agent Card |
| POST | `/internal/a2a/agents/{agent_id}/tasks` | coordinator/admin | 创建子任务（必须带 `Idempotency-Key`） |
| GET | `/internal/a2a/tasks/{task_id}` | coordinator/admin | 子任务状态与产物摘要 |
| GET | `/internal/a2a/tasks/{task_id}/events` | coordinator/admin | 子任务 SSE 事件（支持 `Last-Event-ID`） |
| POST | `/internal/a2a/tasks/{task_id}/cancel` | coordinator/admin | 取消未完成子任务（必须带 `Idempotency-Key`） |

## 3. 关键请求示例

### 创建审查任务

```json
{
  "input_type": "diff",
  "content": "diff --git a/app.py ...",
  "context_policy": "function",
  "base_commit": "synthetic-base-001"
}
```

### 审批补丁

```json
{
  "decision": "approve",
  "patch_version": 2,
  "reason": "测试证据充分，影响范围符合预期"
}
```

### 创建 A2A 子任务

```json
{
  "parent_task_id": "review-123",
  "agent_id": "impact-agent",
  "task_type": "impact",
  "protocol_version": "0.1",
  "input_artifacts": ["artifact-findings-1"],
  "deadline_seconds": 45,
  "idempotency_key": "review-123:impact:v1"
}
```

## 4. 实体字段

| 实体 | 必填字段 | 约束 |
|---|---|---|
| review_task | id、actor、input_hash、status、state_version | 幂等唯一键为 `(actor_id, command_type, idempotency_key)`；`input_hash` 建索引便于查找（见 docs/08 §3.1） |
| review_comment | task_id、file、line、rule_id、severity、confidence | `(task_id,file,line,rule_id)` 去重 |
| fix_patch | id、task_id、patch_version、patch_hash、status | 版本递增 |
| sandbox_run | patch_id、status、exit_code、coverage_delta | 输出必须脱敏 |
| approval | patch_id、decided_by、decision、patch_version | approver 且版本匹配 |
| audit_event | actor、action、entity、before、after、created_at | 只 INSERT |
| eval_result | run_id、case_id、passed、trace、tokens、latency_ms | 轨迹可导出 JSON |
| a2a_agent | agent_id、card_version、capabilities、schemas、endpoint、status | 能力变更需版本化 |
| a2a_task | id、parent_task_id、agent_id、status、protocol_version、idempotency_key、attempt | 父子关系不可变 |
| a2a_artifact | id、task_id、artifact_type、schema_version、content_hash、data_ref | Schema 和哈希必须校验 |
| a2a_message | id、task_id、message_type、correlation_id、payload_hash、created_at | 负载原文按策略脱敏 |

## 5. 统一错误码

`INVALID_INPUT`、`PERMISSION_DENIED`、`FORBIDDEN`、`STATE_VERSION_CONFLICT`、`STEP_LIMIT_EXCEEDED`、`TOOL_LOOP_DETECTED`、`PATCH_INVALID`、`SCOPE_DRIFT`、`WIDE_IMPACT`、`CONFLICT`、`TEST_GAP`、`SANDBOX_TIMEOUT`、`QUALITY_GATE_FAILED`、`VERSION_CONFLICT`、`PROTOCOL_VERSION_UNSUPPORTED`、`ARTIFACT_SCHEMA_INVALID`、`TASK_TIMEOUT`、`TASK_STATUS_UNKNOWN`。

完整冻结清单（含新增的 `ILLEGAL_STATE_TRANSITION`、`BUDGET_EXCEEDED`、`SANDBOX_UNAVAILABLE`、
`IDEMPOTENCY_KEY_REQUIRED`、`RESOURCE_NOT_FOUND` 等）及其 HTTP 状态、是否重试、是否转人工，
见 `docs/08-错误码与协议冻结.md` §1；实现位于 `domain/errors.py::ERROR_SPECS`。

## 6. 幂等与并发

幂等键格式建议为 `task_id:command:resource:version`（长度必须 ≥ 8，否则 `IDEMPOTENCY_KEY_REQUIRED`）。

幂等账本：所有写命令（创建任务、触发修复、审批、合并、评测、人工恢复、内部 A2A 提交与取消）
统一写入 `idempotency_record`，唯一键为 `(actor_id, command_type, aggregate_ref, idempotency_key)`：

| 命中情况 | 行为 |
|---|---|
| 同键 + 同请求（`request_hash` 相同），`status=completed` | 返回**原响应负载**，附 `idempotent_replay: true`；不产生第二次副作用 |
| 同键 + 异请求，或同键跨命令/跨聚合复用 | `409 IDEMPOTENCY_CONFLICT` |
| 同键 + `status=in_progress` | `409 CONFLICT`（不并发放行） |
| 同键 + `status=failed` | 允许用同一键重试 |

A2A 子任务另有 `(parent_task_id, agent_id, task_type, idempotency_key)` 唯一约束（拆分形态下为
`(side, parent_task_id, agent_id, task_type, idempotency_key)`，见 docs/08 §3.7）；工具结果写入
`tool_execution` 表后重复请求直接返回原结果。补丁写入使用任务和文件级锁，审批使用乐观版本校验。

**不使用"当前状态已变化"代替幂等**：例如同一幂等键重复合并，必须返回第一次合并的分支与 commit，
而不是报状态冲突。未知状态不得盲目重试（宪法第七条）。实现见 `repositories/idempotency.py`、
`apps/api/idempotency.py`；验收见 `tests/test_p0_idempotency.py`。

### 6.1 数据表：`idempotency_record`

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `actor_id` / `actor_role` | 调用者身份 |
| `command_type` | `review.create` / `fix.trigger` / `approval.decide` / `merge.apply` / `eval.run` / `review.resume` / `a2a.submit` / `a2a.cancel` |
| `aggregate_ref` | 聚合根引用（任务 ID、补丁 ID 或评测范围），参与唯一键 |
| `idempotency_key` | 客户端幂等键 |
| `request_hash` | 请求摘要，用于判定"同键异请求" |
| `status` | `in_progress` / `completed` / `failed`（CHECK 约束） |
| `response` | 原样保存的响应负载（JSON） |
| `created_at` / `updated_at` | 时间戳 |
