# 本地 Git 工作流

## 目标

CodePilot 的服务端协议仍然只接收 Diff/ZIP。为了让个人开发者可以直接拿自己的 Python 项目演示，仓库附带
`scripts/review_local_repo.py`：它在客户端读取本地 Git 的 `base..head`，生成 unified diff，再调用现有 API。

这不是 GitHub/GitLab 接入，也不需要 OAuth、Webhook 或远程推送。服务端不会读取 `--repo` 路径。

## 最小用法

在目标仓库有至少两个提交时执行：

```powershell
python scripts/review_local_repo.py --repo D:\src\demo --base HEAD~1 --mode a2a
```

脚本会：

1. 验证路径是 Git 工作目录；
2. 解析基线 commit，生成 `base..HEAD` diff；
3. 创建 CodePilot 审查任务并等待 `REVIEWED` 或异常终态；
4. 把完整报告写入 `var/local-review/<task_id>.json`。

先只检查 diff，不调用服务：

```powershell
python scripts/review_local_repo.py --repo . --base HEAD~1 --dry-run --diff-output var\local-review\input.diff
```

## Fix、验证、审批与任务分支

```powershell
python scripts/review_local_repo.py `
  --repo D:\src\demo `
  --base HEAD~1 `
  --mode a2a `
  --trigger-fix `
  --patch-output var\local-review\candidate.patch
```

只有当 Verify 通过并进入 `PENDING_APPROVAL` 时，才可以显式审批和写入任务分支：

```powershell
python scripts/review_local_repo.py --repo D:\src\demo --base HEAD~1 --trigger-fix --approve --merge
```

`--merge` 的结果是 CodePilot 服务内部的 `var/repos/<task_id>` 与 `codepilot/<task_id>` 分支。脚本不会修改
调用方当前目录，也不会写入 `main`、`master` 或 `develop`。需要把结果带回真实仓库时，先检查导出的 patch，
再由开发者在自己的 Git 工作流中决定是否应用。

## 输入与边界

- 默认比较 `HEAD~1..HEAD`，可用任意本地 ref，例如 `main..feature/login`；
- 空 diff、无效 ref、非 Git 目录会在本地直接失败；
- API 仍执行大小、路径、规则、Artifact、权限、幂等和 Docker 沙箱门禁；
- Diff 只包含提交内容，未提交的工作区改动不会被读取；需要审查它们时先提交到临时分支；
- 生成层默认离线，`offline` 模式不生成补丁；
- 不接入 GitHub/GitLab，不创建远程 PR，不上传源码到第三方服务。

## 验收

```powershell
python -m pytest tests/test_local_git_adapter.py -q
python scripts/review_local_repo.py --repo . --base HEAD --dry-run
```

第二条命令预期返回“没有文件变更”，用于确认当前目录校验和错误提示路径正常。真实闭环仍需先启动 API、
PostgreSQL 与沙箱镜像，具体步骤见 `docs/10-个人开发者验收与演示剧本.md`。
