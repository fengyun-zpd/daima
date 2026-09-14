# ADR002：本地 Git 只读适配器

## 状态

已接受（2026-09-14）。

## 背景

项目的服务端协议以 Diff/ZIP 为输入，个人开发者还需要能够用自己的本地 Python 仓库完成演示。直接让服务端接收路径会破坏沙箱和部署边界，也会把远程 Git 平台接入带入 MVP。

## 决策

新增 `scripts/review_local_repo.py` 作为客户端适配器：

- 在调用方机器验证 Git 工作目录；
- 读取指定 `base..head` 的 unified diff 和基线 commit；
- 通过现有 `POST /api/v1/reviews` 提交 Diff；
- 可选触发 Fix/Verify、导出报告和候选补丁；
- `--approve --merge` 仍经过现有审批门，结果只写服务端 `var/repos/<task_id>` 的 `codepilot/<task_id>` 分支。

## 约束

适配器不读取未提交工作区改动，不保存远程凭据，不调用 GitHub/GitLab API，不修改当前工作目录，不允许把 `main`、`master` 或 `develop` 作为写入目标。服务端安全不变量、幂等和审计契约保持不变。

## 验证

`tests/test_local_git_adapter.py` 覆盖 Git 根目录解析、基线 SHA、Diff 生成、空 Diff 和非法路径。完整闭环继续使用现有 API、Docker 沙箱、审批和任务分支验收脚本。
