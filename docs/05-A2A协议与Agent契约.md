# A2A 协议与 Agent 契约

## 1. 范围

CodePilot 实现一个受控的 A2A 协议子集，用于 Coordinator 与四类业务 Agent 的任务协作。MVP 使用 HTTP 请求、SSE 事件和 PostgreSQL 持久化，不依赖公网发现、跨组织身份或生产级服务治理。

A2A 负责 Agent 间的任务、消息和产物传递；代码读取、规则分析、补丁写入和 Docker 执行仍由 Tool Registry 与内部工具负责。工具调用不能通过 A2A 消息绕过权限策略。

## 2. Agent Card

每个 Agent 启动时注册 Agent Card，Coordinator 创建任务前必须读取并校验 Card。Card 变更必须递增 `card_version`，不兼容变更不能覆盖旧版本。

```json
{
  "agent_id": "review-agent",
  "card_version": "1.0",
  "protocol_versions": ["0.1"],
  "capabilities": ["review.rules", "review.explain"],
  "input_schema": "ReviewTaskInput@1.0",
  "output_schemas": ["FindingArtifact@1.0", "ReviewCommentArtifact@1.0"],
  "endpoint": "/internal/a2a/agents/review-agent/tasks",
  "auth": {"audience": "codepilot-coordinator", "scopes": ["task:receive"]},
  "limits": {"max_steps": 15, "timeout_seconds": 45},
  "status": "healthy"
}
```

MVP Agent 能力边界如下：

| Agent | 允许能力 | 禁止能力 |
|---|---|---|
| Review | 读取 Diff、规则、有限上下文、LLM 归纳 | 写文件、合并、审批 |
| Impact | AST、符号索引、调用图、只读检索 | 写文件、合并、审批 |
| Fix | 读取 Finding/Impact、生成候选补丁 | 直接写分支、合并、审批 |
| Verify | 运行预审查、lint、pytest、覆盖率、沙箱 | 修改候选补丁、合并、审批 |

## 3. Task 生命周期

```text
submitted → working → input_required → working
                    ├→ completed
                    ├→ failed
                    └→ canceled
```

Task 核心字段：

```json
{
  "task_id": "impact-01J...",
  "parent_task_id": "review-01J...",
  "trace_id": "trace-01J...",
  "agent_id": "impact-agent",
  "task_type": "impact",
  "protocol_version": "0.1",
  "status": "working",
  "input_artifacts": ["artifact-findings-1"],
  "required_output_types": ["ImpactReport"],
  "deadline": "2026-09-13T12:00:45Z",
  "attempt": 1,
  "idempotency_key": "review-01J:impact:v1",
  "correlation_id": "review-01J:impact",
  "state_version": 2
}
```

Coordinator 是父任务的唯一 owner。Agent 可以更新自己的 Task 状态，但不能直接修改父任务、其他子任务或审批状态。

## 4. Message 与 Artifact

Message 只传递控制信息、摘要和引用；大内容放入 Artifact 存储。

```json
{
  "message_id": "msg-01J...",
  "task_id": "impact-01J...",
  "type": "task.completed",
  "role": "agent",
  "correlation_id": "review-01J:impact",
  "artifact_refs": ["artifact-impact-1"],
  "error": null,
  "created_at": "2026-09-13T12:00:10Z"
}
```

ArtifactEnvelope 必须包含类型、版本和内容哈希：

```json
{
  "artifact_id": "artifact-impact-1",
  "task_id": "impact-01J...",
  "artifact_type": "ImpactReport",
  "schema_version": "1.0",
  "content_hash": "sha256:...",
  "size_bytes": 2840,
  "data": {
    "changed_symbols": ["app.auth.login"],
    "direct_callers": ["api.login"],
    "direct_callees": ["db.query"],
    "affected_files": ["app/auth.py", "api.py"],
    "risk_level": "medium",
    "uncertain": false
  }
}
```

必需产物：Review 返回 `Finding`，Impact 返回 `ImpactReport`，Fix 返回 `PatchCandidate` 和 `PatchEvidence`，Verify 返回 `VerifyEvidence`。Coordinator 在入库前校验 JSON Schema、父子关系、内容哈希和允许的 artifact_type。

## 5. 内部接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/internal/a2a/agents` | 返回健康 Agent Card |
| GET | `/internal/a2a/agents/{agent_id}/card` | 返回指定 Card |
| POST | `/internal/a2a/agents/{agent_id}/tasks` | 提交子任务 |
| GET | `/internal/a2a/tasks/{task_id}` | 查询 Task 和产物引用 |
| GET | `/internal/a2a/tasks/{task_id}/events` | 订阅 SSE 事件 |
| POST | `/internal/a2a/tasks/{task_id}/cancel` | 请求取消 |

请求必须带调用方身份、`trace_id`、`correlation_id`、`protocol_version` 和 `Idempotency-Key`。对外 API 只暴露父任务视图，内部接口不得直接暴露给 developer。

## 6. 超时、重试、取消与恢复

- 默认子任务超时 45 秒，Coordinator 先查询状态，再决定最多一次重试。
- `completed`、`failed`、`canceled` 是终态；终态 Task 不接受新的业务命令。
  例外（已裁决，见 docs/08 §3.2）：`failed` 允许一次**受控重试**——必须同时满足
  `attempt < 2`、失败错误码被标记为可重试、且重试前已完成状态对账。
- Schema 错误、版本不兼容、权限错误不重试。
- SSE 断线按 `Last-Event-ID` 续接；续接失败时轮询 Task 和 Artifact。
- Coordinator 重启从数据库恢复父子任务、checkpoint、事件游标和幂等记录。
- 状态未知时禁止重放写工具；确认未执行或命中幂等结果后才能重试。

## 7. 幂等键与错误

子任务幂等唯一键为 `(parent_task_id, agent_id, task_type, idempotency_key)`。重复提交返回原 Task 和已有 Artifact，不创建第二个执行实例。

统一错误码：`PROTOCOL_VERSION_UNSUPPORTED`、`AGENT_UNAVAILABLE`、`TASK_TIMEOUT`、`TASK_STATUS_UNKNOWN`、`TASK_CANCELED`、`ARTIFACT_SCHEMA_INVALID`、`ARTIFACT_HASH_MISMATCH`、`PARENT_TASK_MISMATCH`、`PERMISSION_DENIED`、`IDEMPOTENCY_CONFLICT`。

## 8. single 与 a2a 适配

两种模式实现同一个接口：

```python
class AgentInvoker(Protocol):
    async def submit(self, task: A2ATask) -> TaskHandle: ...
    async def wait(self, handle: TaskHandle) -> list[ArtifactEnvelope]: ...
    async def execute(self, task_id: str) -> ChildTaskOutcome: ...
    async def cancel(self, task_id: str) -> None: ...
    async def status(self, task_id: str) -> str: ...
```

`A2AInvoker` 使用 HTTP/SSE；`InProcessInvoker` 直接调用 Agent 函数，但仍创建 Task、校验 Card、记录 Message 和 Artifact。这样可以比较传输开销与协作收益，而不改变业务逻辑和安全门禁。

### 8.1 服务端分配 Task ID 与共享数据库部署

- **Task ID 由 Agent 服务端分配**：客户端提交的 `task_id` 只作为请求标识，服务端生成自己的任务 ID 并返回；
  Coordinator 通过 `a2a_task.remote_task_id` 关联远端任务（见 docs/08 §3.7）。
- **共享数据库部署**（docs/01 §5）：`a2a_task.side` 区分 `coordinator` / `agent` 两侧的生命周期记录，
  唯一约束为 `(side, parent_task_id, agent_id, task_type, idempotency_key)`。
  Coordinator 默认只读取 `side='coordinator'` 的行。
- **受控重试**：重试时客户端使用新的幂等键（`<key>:r<attempt>`），远端因此产生新的 Task，
  而本地跟踪行的 `attempt` 提升到 2，且不得产生重复 Artifact。
- **跨进程事务**：每次远程调用前必须提交本地事务，并在读取状态时强制刷新
  （`populate_existing`），避免基于过期快照做出重试判断。
