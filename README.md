# CodePilot A2A

**受控的多 Agent Python 代码审查闭环（项目三 MVP）**

CodePilot 接收合成 Git Diff / ZIP 变更集，自动完成：确定性规则审查 → AST/调用图影响分析 →
候选补丁生成 → Docker 沙箱验证 → 人工审批 → 任务分支写入。核心成果是**受控的 A2A Agent 协作**：
Agent Card 契约、父子任务状态机、幂等与恢复、工具权限、沙箱隔离、审批门与可回放审计。

- 最高工程约束：[`CodePilot项目宪法.md`](CodePilot项目宪法.md)
- 需求规格：[`CodePilot-MVP-需求规格说明书.md`](CodePilot-MVP-需求规格说明书.md)
- 实施记录与文档冲突裁决：[`docs/00-落地就绪评审.md`](docs/00-落地就绪评审.md)、[`docs/08-错误码与协议冻结.md`](docs/08-错误码与协议冻结.md)

---

## 1. 快速开始

> 本文档命令以 **Windows PowerShell** 为主（本项目的主要开发环境），Linux/macOS 等价命令在每节末尾给出。
> 最短的一次完整演示（含 Fix/Verify/审批/合并）见
> [`docs/10-个人开发者验收与演示剧本.md`](docs/10-个人开发者验收与演示剧本.md)。

### 1.1 Docker Compose（推荐）

单进程形态（PostgreSQL + API + Dashboard；沙箱容器按需启动）：

```powershell
Copy-Item .env.example .env              # Linux/macOS: cp .env.example .env
docker compose config --quiet            # 先校验 Compose 文件
docker compose up -d postgres api web    # 数据库 + API + Dashboard（自动执行 alembic 迁移）
docker compose run --rm sandbox-image    # 构建沙箱镜像（Fix/Verify 需要，约 1~2 分钟）
docker compose ps
docker compose logs api                  # 看到 "A2A transport=inprocess" 即启动成功
Invoke-RestMethod http://127.0.0.1:8099/healthz
Invoke-RestMethod http://127.0.0.1:8099/readyz
# 打开 http://127.0.0.1:8080 使用审查工作台（Dashboard）
```

拆分形态（Coordinator 与 Agent 分进程，Dashboard 不变）：

```powershell
# 1) 把 A2A 地址写进 .env（Compose 不会自动转发未声明的宿主机变量）
#    CODEPILOT_A2A_BASE_URL=http://agent:8100     ← 不能写 127.0.0.1，容器内的 127.0.0.1 是 API 容器自己
docker compose --profile split up -d postgres api agent web
# 2) 确认真的走 HTTP：/readyz 的 transport 字段 + api 启动日志
(Invoke-RestMethod http://127.0.0.1:8099/readyz).transport          # 期望 http
docker compose logs api | Select-String "A2A transport="            # Linux/macOS: ... | grep "A2A transport="
#    → inprocess = 进程内调用（未配置 CODEPILOT_A2A_BASE_URL）
#    → http      = Coordinator 通过 HTTP/SSE 调用拆分出来的 Agent 服务
python scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8099 --mode a2a
python scripts/check_dashboard_e2e.py --base-url http://127.0.0.1:8080 --mode a2a
```

停止与清理：

```powershell
docker compose down                 # 停止并删除容器/网络（保留数据卷）
docker compose down -v              # 连数据卷一起删除（会清空数据库）
docker compose --profile split down # 拆分形态
docker image rm codepilot-web:local codepilot-api:latest codepilot-sandbox:local
```

> **MVP 的拆分形态是"一个统一的 Agent 服务进程"**（`apps/api/agent_main.py`）托管四类 Agent Card，
> **不是一个 Agent 一个容器**。四类 Agent 的边界由 Agent Card、capability allowlist 和独立 handler
> 保证，部署粒度由 compose profile 决定；四容器拓扑（review/impact/fix/verify 各自独立服务）
> 明确不在范围内（宪法第十一条）。详见 `docs/01-架构设计.md` §3/§5。
> 两种形态共用同一 PostgreSQL 与同一套协议/校验，切换只影响传输层。

### 1.2 本地直接运行（不用 Compose）

```powershell
python -m pip install -e ".[dev]"
Copy-Item .env.example .env               # 按需修改 CODEPILOT_DATABASE_URL

docker run -d --name codepilot-pg -p 55432:5432 `
  -e POSTGRES_USER=codepilot -e POSTGRES_PASSWORD=codepilot -e POSTGRES_DB=codepilot postgres:16-alpine

python -m alembic upgrade head            # 建表 + 追加式审计护栏
python scripts/build_sandbox_image.py     # 构建沙箱镜像
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8099
```

Windows：`./scripts/dev.ps1`；Linux/macOS：`./scripts/dev.sh`（自动完成上述步骤）。

### 1.3 Dashboard（审查工作台）

Dashboard 是 Frontend 交付物，**直接进入工作台，没有营销页**：创建任务（diff/ZIP、`single`/`a2a`/`offline`）、
查看父子任务时间线、Finding、风险等级、影响文件、Artifact、补丁 diff、验证证据、审批/合并、
`NEEDS_HUMAN` 原因与 admin 恢复入口、审计事件，并显示 API ready 状态与实际 `transport`。

两种启动方式（前端只使用相对路径 `/api`、`/internal`、`/healthz`、`/readyz`，因此无需配置后端地址）：

```powershell
# 方式 A：容器（nginx 托管静态产物 + 反向代理到 api 服务）
docker compose up -d postgres api web
#   打开 http://127.0.0.1:8080

# 方式 B：本地开发（vite dev server 代理到 127.0.0.1:8099）
cd web
npm install
npm run dev
#   打开 http://127.0.0.1:5173
```

自检（静态契约检查 + 真实构建 + 真实浏览器链路代理检查）：

```powershell
python scripts/web_check.py            # 前端调用的每个路径都必须在后端 OpenAPI 中存在 + 23 项功能契约
python scripts/web_check.py --build    # 追加 npm run build（tsc --noEmit + vite build）
python scripts/check_dashboard_e2e.py --base-url http://127.0.0.1:8080   # 需要先启动 Web 与 API
python -m pytest tests/test_dashboard_contract.py -q
```

技术选型说明：React 18 + TypeScript + Vite，样式使用原生 CSS 而非 Ant Design
（避免为一个工作台引入重量级组件库与额外构建体积；属**有意偏离**，见 `docs/08-错误码与协议冻结.md` §3.13）。
进度刷新使用 2 秒轮询（进入终态或等待人工动作后自动停止），SSE 端点 `/api/v1/reviews/{task_id}/events` 亦可用于后续接入。
写请求统一携带 `X-Actor-Id` / `X-Actor-Role` 与 `Idempotency-Key`；**同一次用户操作的重试复用同一个键**。

### 1.4 最小演示闭环

```powershell
python scripts/seed_demo.py --base-url http://127.0.0.1:8099   # 创建最小演示任务并跑到待审批
python scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8099
python scripts/smoke_vertical_slice.py --base-url http://127.0.0.1:8099 --mode a2a
```

### 1.5 本机环境注意事项（实测）

| 现象 | 原因与处理 |
|---|---|
| `docker compose build` 报 `header key "x-docker-expose-session-sharedkey" contains value with non-printable ASCII characters` | Docker Desktop 的 Buildx/Bake 会话问题，与仓库无关。`$env:COMPOSE_BAKE="0"; docker compose build ...` 可绕开（或直接 `docker build -f docker/web/Dockerfile -t codepilot-web:local .`）。 |
| 重建 api 容器后 Dashboard 报 `502 Bad Gateway` | nginx 只在启动时解析一次上游主机名。`docker/web/nginx.conf` 已改用 `resolver 127.0.0.11` + 变量式 `proxy_pass`，每次请求重新解析。 |
| `docker compose up` 报 `Bind for 0.0.0.0:55432 failed` | 本机已有别的 PostgreSQL 占了 55432。停掉它，或用 override 文件改端口（`ports: !override [...]`；Compose 对 `ports` 列表默认是**追加**合并）。 |
| **Docker Socket 挂载的安全边界** | `docker-compose.yml` 把 `/var/run/docker.sock` 挂进 api 容器，这是为了让 api 能按需创建**沙箱容器**。挂载 Docker Socket 等于把宿主机 Docker 控制权交给该容器，**只适合个人本地开发/演示**；生产部署必须换成独立的沙箱执行服务或受限的远程 Docker API 代理。 |
| **不适合直接部署公网** | 当前鉴权是请求头身份（`X-Actor-Id` / `X-Actor-Role`），没有 OAuth/JWT、没有多租户、没有速率限制、没有 TLS 终止。**这是个人项目演示形态，不要直接对公网暴露。** |

**尚未实现的企业级能力（明确不做，宪法第十一条）**：Kubernetes、Redis、Kafka、Celery、服务网格、
服务注册中心、OAuth/OIDC/SSO、多租户、公网 Agent 发现、向量数据库/RAG、GitHub/GitLab 生产级 OAuth、
模型微调训练、LangGraph 重写 Coordinator、云厂商专属服务。

---

## 2. 运行模式（宪法第二条）

| 模式 | 用途 | 远程调用 | 生成层 |
|---|---|---|---|
| `single` | 基线与故障降级 | 进程内 `InProcessInvoker` | 受约束生成层 |
| `a2a` | 项目三主模式 | HTTP/SSE `A2AInvoker` | 受约束生成层 |
| `offline` | 只运行确定性规则与 AST | 进程内 | 不调用模型，**不生成补丁** |

三种模式复用同一套 Finding / ImpactReport / PatchCandidate / VerifyEvidence、状态机、权限策略与
安全门禁（FR-099）。

### 2.1 用自己的本地 Git 项目演示

服务端仍接收 Diff/ZIP；本地仓库场景使用客户端适配器生成 Diff：

```powershell
python scripts/review_local_repo.py --repo D:/src/demo --base HEAD~1 --mode a2a
python scripts/review_local_repo.py --repo D:/src/demo --base HEAD~1 --trigger-fix --patch-output var/local-review/demo.patch
```

适配器只读 `base..HEAD`，不会读取未提交改动，也不会修改当前目录。审批后的 `--merge` 结果写入服务端
`var/repos/<task_id>` 的 `codepilot/<task_id>` 分支。完整参数和边界见 [`docs/11-本地Git工作流.md`](docs/11-本地Git工作流.md)。

---

## 3. 端到端流程

```text
POST /api/v1/reviews                 → DRAFT → REVIEWING
  Coordinator 解析 Agent Card → Review 子任务 + Impact 子任务（可并行）
  Review  → Finding Artifact（24 条确定性规则）
  Impact  → ImpactReport Artifact（AST、符号索引、调用图）
  Coordinator 校验 Schema / 内容哈希 / 父子关系 → REVIEWED

POST /api/v1/reviews/{id}/fixes       → FIXING（默认显式触发；CODEPILOT_AUTO_FIX=1 可自动）
  Fix    → PatchCandidate（仅 3 类修复：硬编码密钥、shell=True、SQL 拼接）
  Coordinator 校验补丁格式、影响范围、scope drift → TESTING
  Verify → VerifyEvidence（沙箱内 git apply --check → ruff → pytest → 回归 → 覆盖率/TEST_GAP）
  质量门禁通过 → PENDING_APPROVAL；失败 → NEEDS_HUMAN（无任何分支副作用）

POST /api/v1/fixes/{id}/approval      → approver 批准/拒绝（决定绑定 patch_version）
POST /api/v1/fixes/{id}/merge         → 写入任务分支 codepilot/<task_id> → MERGED
```

父任务状态机：`DRAFT → REVIEWING → REVIEWED → FIXING → TESTING → PENDING_APPROVAL → MERGED`；
异常态 `NEEDS_HUMAN / REJECTED / FAILED`。非法迁移、跳过审批、终态回退一律拒绝
（`ILLEGAL_STATE_TRANSITION`）。

---

## 4. 安全不变量（必须全为 0）

| 不变量 | 保障机制 |
|---|---|
| 越权写入 | Agent 能力 allowlist + 角色校验 + 默认拒绝；Agent 调用写工具返回 `PERMISSION_DENIED` |
| 未审批合并 | 合并前必须存在绑定当前 `patch_version` 的 approve 记录 |
| 重复副作用 | 幂等键唯一约束 + 状态意图重放检测 + Artifact 内容去重 |
| 沙箱逃逸 | `network=none`、非 root、只读根文件系统、`cap_drop=ALL`、`no-new-privileges`、资源与超时上限 |
| 非法状态迁移 | 显式迁移表白名单 + 乐观锁 + 数据库 CHECK 约束 |
| 审计篡改 | `audit_event` / `approval` 只允许 INSERT（ORM 事件 + 数据库触发器双重拦截） |

---

## 5. 常用命令

```powershell
# 交付验收（ruff / compileall / Schema / web / 契约测试 / pytest / db / 沙箱 / 故障矩阵 / 评测冒烟）
python scripts/run_checks.py             # 全量（含 Docker 与 PostgreSQL）
python scripts/run_checks.py --fast      # 跳过 Docker 与 PostgreSQL（结论写 "Docker not executed"）
python scripts/run_checks.py --static    # 只跑静态检查（ruff/compileall/schema/web）

# 测试（全量 429 项）
python -m pytest -q
python -m pytest tests/test_scenario_review_flow.py -q   # 场景一：审查闭环（唯一子任务/转人工/trace/重启）
python -m pytest tests/test_scenario_fix_flow.py -q      # 场景二/三：Fix + Verify（含真实沙箱）
python -m pytest tests/test_p0_idempotency.py -q         # 幂等：8 类写命令
python -m pytest tests/test_phase5_http_a2a.py -q        # HTTP A2A（真实 uvicorn）
python -m pytest tests/test_phase6_fix_verify.py -q      # Fix/Verify（真实 Docker 沙箱）

# Dashboard（前端契约自检 + 真实构建）
python scripts/web_check.py
python scripts/web_check.py --build
python -m pytest tests/test_dashboard_contract.py -q

# Dashboard 端到端（经 nginx/vite 代理走完工作台主流程，需要先启动 Web 与 API）
python scripts/check_dashboard_e2e.py --base-url http://127.0.0.1:8080 --mode a2a

# 数据库
python -m alembic upgrade head
python scripts/db_check.py

# 沙箱安全自检（非 root / 无网络 / 只读根 / 超时 / 清理）
python scripts/sandbox_check.py

# 故障注入矩阵（13 场景；--no-sandbox 只跑 11 个非沙箱场景，并明确标注跳过）
python scripts/run_fault_matrix.py
python scripts/run_fault_matrix.py --no-sandbox

# 黄金集评测（20 PR 覆盖全部 24 条规则 × single/a2a/offline；pass@3、pass^3、路由、Schema、收敛、Trace）
python scripts/run_evals.py --modes single a2a offline --case-limit 3 --runs 1     # 冒烟
python scripts/run_evals.py --modes single a2a offline --case-limit 20 --runs 3    # 全量
python scripts/run_evals.py --modes single a2a --case-limit 20 --runs 1 --with-fix # 含真实沙箱 Fix/Verify
```

> 规则集共 24 条；`examples/.codepilot.yaml` 的 `rules.enabled: []` 表示**全部启用**。
> 如果在那里写了子集，Review 只会产出该子集的 Finding，评测 recall 会随之下降——
> 请保持为空，或用 `python scripts/run_evals.py` 复测。

---

## 6. API 示例

鉴权：请求头 `X-Actor-Id` / `X-Actor-Role`（`developer` / `approver` / `admin` / `coordinator`）；
写请求必须带 `Idempotency-Key`。更完整的示例见 [`docs/09-API示例.md`](docs/09-API示例.md)。

```bash
# 1) 创建审查任务（unified diff 或 base64(ZIP)）
curl -X POST http://127.0.0.1:8099/api/v1/reviews \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer' -H 'Idempotency-Key: demo-00000001' \
  -d '{"input_type":"diff","base_commit":"synthetic-base-001","context_policy":"function",
       "content":"diff --git a/app/config.py b/app/config.py\n--- a/app/config.py\n+++ b/app/config.py\n@@ -1,1 +1,2 @@\n import os\n+API_KEY = \"sk-live-abcdef123456\"\n"}'

# 2) 查询父任务、子任务、Artifact 与意见
curl http://127.0.0.1:8099/api/v1/reviews/<task_id> -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'
curl http://127.0.0.1:8099/api/v1/reviews/<task_id>/comments -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'

# 3) SSE 事件流（支持 Last-Event-ID 续接）
curl -N http://127.0.0.1:8099/api/v1/reviews/<task_id>/events -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer'

# 4) 触发修复并读取补丁
curl -X POST http://127.0.0.1:8099/api/v1/reviews/<task_id>/fixes \
  -H 'X-Actor-Id: dev-1' -H 'X-Actor-Role: developer' -H 'Idempotency-Key: demo-fix-0001'
curl http://127.0.0.1:8099/api/v1/fixes/<patch_id> -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver'

# 5) 审批与合并（仅 approver）
curl -X POST http://127.0.0.1:8099/api/v1/fixes/<patch_id>/approval \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver' -H 'Idempotency-Key: demo-approve-01' \
  -d '{"decision":"approve","patch_version":1,"reason":"测试证据充分"}'
curl -X POST http://127.0.0.1:8099/api/v1/fixes/<patch_id>/merge \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: approver-1' -H 'X-Actor-Role: approver' -H 'Idempotency-Key: demo-merge-0001' \
  -d '{"patch_version":1}'

# 6) 审计（仅 admin）与 Agent Card
curl 'http://127.0.0.1:8099/api/v1/audit?task_id=<task_id>' -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'
curl http://127.0.0.1:8099/api/v1/agents -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'

# 7) 黄金集评测（仅 admin）
curl -X POST http://127.0.0.1:8099/api/v1/evals/run \
  -H 'Content-Type: application/json' -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin' \
  -H 'Idempotency-Key: demo-eval-0001' \
  -d '{"modes":["single","a2a","offline"],"case_limit":3,"runs_per_case":1}'
curl http://127.0.0.1:8099/api/v1/evals/<run_id> -H 'X-Actor-Id: admin-1' -H 'X-Actor-Role: admin'
```

错误响应统一为 `{"code": "...", "message": "...", "trace_id": "..."}`；
完整错误码、HTTP 状态与是否重试见 [`docs/08-错误码与协议冻结.md`](docs/08-错误码与协议冻结.md)。

---

## 7. 工程结构

```text
apps/api/            FastAPI 对外接口、内部 A2A 接口、Agent 服务
domain/              状态机、错误码、配置、预算、置信度、Diff/工作区、影响策略、生成层守卫
a2a/                 Agent Card/Task/Message/Artifact 契约、Schema 注册、InProcess/HTTP Invoker
agents/coordinator/  父任务编排、审批与合并
agents/{review,impact,fix,verify}/   四类业务 Agent
tools/               工具注册中心与只读/写工具
rules/               24 条确定性规则与规则引擎
sandbox/             Docker 沙箱、危险模式预审查与安全降级
repositories/        SQLAlchemy 模型、仓储、审计、恢复、合成仓库
evals/               黄金集、指标、评测运行器、故障注入矩阵
migrations/          Alembic 迁移（含追加式审计触发器）
schemas/             JSON Schema（协议与产物契约）
web/                 Dashboard（React 18 + TypeScript + Vite；工作台，非营销页）
docker/              api / sandbox / web 镜像定义与 nginx 反向代理配置
docs/                设计、流程、API、计划、A2A 契约、评测、微调边界、错误码冻结
scripts/             启动、迁移、验收、评测、故障注入与冒烟脚本
tests/               阶段一~阶段七的分层测试（协议/状态机/持久化/切片/A2A/沙箱/审批/评测/故障）
```

---

## 8. 文档导航

- [`docs/00-落地就绪评审.md`](docs/00-落地就绪评审.md)：P0 落地对照、首个 Vertical Slice、各阶段实施记录
- [`docs/01-架构设计.md`](docs/01-架构设计.md)：组件分层、数据流、失败恢复与安全边界
- [`docs/02-审查流程与状态机.md`](docs/02-审查流程与状态机.md)：处理步骤、状态迁移与异常语义
- [`docs/03-API与数据模型.md`](docs/03-API与数据模型.md)：接口清单、实体字段、幂等与并发
- [`docs/04-开发计划与验收清单.md`](docs/04-开发计划与验收清单.md)：排期、Definition of Done、风险与降级
- [`docs/05-A2A协议与Agent契约.md`](docs/05-A2A协议与Agent契约.md)：Agent Card、Task、Message、Artifact、恢复语义
- [`docs/06-A2A故障场景与评测.md`](docs/06-A2A故障场景与评测.md)：故障矩阵、对照实验与指标定义
- [`docs/07-微调边界与后续路线.md`](docs/07-微调边界与后续路线.md)：项目三边界与项目四候选方向
- [`docs/08-错误码与协议冻结.md`](docs/08-错误码与协议冻结.md)：错误码表、Schema 冻结与文档冲突裁决
- [`docs/09-API示例.md`](docs/09-API示例.md)：可直接执行的 API 调用示例与响应片段
- [`docs/11-本地Git工作流.md`](docs/11-本地Git工作流.md)：从当前本地仓库生成 Diff、运行闭环并导出补丁

---

## 9. 明确不做（宪法第十一条）

不做真实 GitHub/GitLab 接入与真实 PR 推送、不做多语言、不做 Kubernetes/多租户、
不做绕过人工审批的自动合并、不做模型微调（LoRA/QLoRA/SFT/DPO）、不实现公网 Agent 发现。

## 10. 已知取舍（详见 docs/08）

- **编排运行时**：使用显式状态机 + 持久化状态意图实现（保留 `AgentInvoker` / `AgentHandler`
  可替换接口），未引入 LangGraph 依赖。
- **共享数据库部署**：`a2a_task.side` 区分 Coordinator 与 Agent 两侧记录，Task ID 由服务端分配。
- **任务分支**：MVP 的"任务分支"是内容目录下的合成仓库（`var/repos/<task_id>`），不接入远端仓库。
- **本地 Git 适配**：`scripts/review_local_repo.py` 只读当前本地仓库并生成 Diff；服务端不读取本地路径，审批后的结果仍写入隔离的任务分支，当前工作目录不会被修改。
- **生成层**：默认确定性 provider（不访问网络）；启用 OpenAI 兼容接口后必须与 `offline`
  分开统计指标（docs/04 §4）。
- **沙箱不可用降级**：Docker 或沙箱镜像不可用时，Fix/Verify 返回 `SANDBOX_UNAVAILABLE`，
  系统只运行规则与报告链路，**绝不**在宿主机执行提交代码。
