/** CodePilot 审查工作台：直接进入任务工作台，无营销页。 */

import React from "react";
import { ApiError, CodePilotClient, OperationKeys, type Identity } from "./api";
import { AuditPanel } from "./components/AuditPanel";
import { AuthPage } from "./components/AuthPage";
import { ErrorBanner } from "./components/Common";
import { CreateForm, type CreateFormValues } from "./components/CreateForm";
import { HistoryPanel } from "./components/HistoryPanel";
import {
  FindingsPanel,
  ImpactPanel,
  PatchPanel,
  ResumePanel,
  TaskHeader,
} from "./components/TaskView";
import type {
  ArtifactSummary,
  AuditEvent,
  ErrorPayload,
  ImpactSummaryView,
  Patch,
  ReadyStatus,
  ReviewDetail,
  ReviewTask,
  VerifyEvidenceView,
} from "./types";

/** 父任务进入这些状态后停止轮询（终态或等待人工动作）。 */
const TERMINAL = new Set([
  "MERGED",
  "REJECTED",
  "FAILED",
  "PENDING_APPROVAL",
  "NEEDS_HUMAN",
  "REVIEWED",
]);

function readStoredIdentity(): Identity | null {
  const raw = window.localStorage.getItem("codepilot.identity");
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as Identity;
      if (parsed.actorId && parsed.role && parsed.token) return parsed;
    } catch {
      /* 忽略损坏的本地存储 */
    }
  }
  return null;
}

function Workbench({ identity, onLogout }: { identity: Identity; onLogout: () => void }): React.ReactElement {
  const [taskId, setTaskId] = React.useState<string>("");
  const [manualTaskId, setManualTaskId] = React.useState<string>("");
  const [detail, setDetail] = React.useState<ReviewDetail | null>(null);
  const [history, setHistory] = React.useState<ReviewTask[]>([]);
  const [patches, setPatches] = React.useState<Patch[]>([]);
  const [audit, setAudit] = React.useState<AuditEvent[]>([]);
  const [ready, setReady] = React.useState<ReadyStatus | null>(null);
  const [error, setError] = React.useState<(ErrorPayload & { status?: number }) | null>(null);
  const [auditError, setAuditError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [autoRefresh, setAutoRefresh] = React.useState(true);
  const [historyLoading, setHistoryLoading] = React.useState(false);

  const client = React.useMemo(() => new CodePilotClient(identity), [identity]);
  // 用户操作级幂等键：同一次操作失败后重试复用同一个键，成功后清空。
  const keys = React.useMemo(() => new OperationKeys(), []);

  // 切换身份意味着换命令主体（幂等键按 actor 隔离），因此清理未完成的键。
  React.useEffect(() => {
    keys.clearAll();
  }, [identity, keys]);

  const handleError = React.useCallback((exc: unknown) => {
    setNotice(null);
    if (exc instanceof ApiError) {
      setError({ ...exc.payload, status: exc.status });
    } else {
      setError({ code: "CLIENT_ERROR", message: String(exc) });
    }
  }, []);

  const loadReady = React.useCallback(async () => {
    try {
      setReady(await client.readyz());
    } catch (exc) {
      setReady(null);
      handleError(exc);
    }
  }, [client, handleError]);

  const refresh = React.useCallback(
    async (id: string) => {
      if (!id) return;
      try {
        const next = await client.getReview(id);
        setDetail(next);
        setPatches(await client.listPatches(id));
        setError(null);
      } catch (exc) {
        handleError(exc);
      }
    },
    [client, handleError],
  );

  const loadHistory = React.useCallback(async () => {
    setHistoryLoading(true);
    try {
      setHistory(await client.listReviews());
    } catch (exc) {
      handleError(exc);
    } finally {
      setHistoryLoading(false);
    }
  }, [client, handleError]);

  const loadAudit = React.useCallback(
    async (id: string) => {
      if (identity.role !== "admin") {
        setAudit([]);
        setAuditError(null);
        return;
      }
      try {
        setAudit(await client.listAudit({ taskId: id || undefined, limit: 200 }));
        setAuditError(null);
      } catch (exc) {
        setAudit([]);
        setAuditError(
          exc instanceof ApiError
            ? `${exc.status} ${exc.payload.code}: ${exc.payload.message}`
            : String(exc),
        );
      }
    },
    [client, identity.role],
  );

  React.useEffect(() => {
    void loadReady();
  }, [loadReady]);

  React.useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  React.useEffect(() => {
    if (!taskId) return;
    void refresh(taskId);
    void loadAudit(taskId);
  }, [taskId, refresh, loadAudit]);

  React.useEffect(() => {
    if (!taskId || !autoRefresh) return undefined;
    const status = detail?.task.status ?? "DRAFT";
    if (TERMINAL.has(status)) return undefined;
    const timer = window.setInterval(() => {
      void refresh(taskId);
      void loadAudit(taskId);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [taskId, autoRefresh, detail?.task.status, refresh, loadAudit]);

  const onCreate = async (values: CreateFormValues) => {
    if (busy) return; // 连续点击不产生第二个任务
    setBusy(true);
    const action = `create:${values.inputType}:${values.baseCommit}`;
    try {
      const created = await client.createReview(
        {
          input_type: values.inputType,
          content: values.content,
          base_commit: values.baseCommit,
          context_policy: values.contextPolicy,
          mode: values.mode,
          custom_task_id: values.customTaskId || undefined,
        },
        { idempotencyKey: keys.keyFor(action) },
      );
      keys.clear(action);
      setTaskId(created.task.id);
      setError(null);
      setNotice(
        created.created
          ? values.mode === "a2a"
            ? "任务已创建：代码审查 Agent 和影响分析 Agent 已开始分工协作，结果会自动显示在右侧。"
            : "任务已创建，审查结果会自动显示在右侧。"
          : "检测到这是同一次提交，已打开原任务。",
      );
      await refresh(created.task.id);
      await loadAudit(created.task.id);
      await loadHistory();
    } catch (exc) {
      handleError(exc);
    } finally {
      setBusy(false);
    }
  };

  const openTask = async (id: string) => {
    const nextId = id.trim();
    if (!nextId) {
      setError({ code: "TASK_ID_REQUIRED", message: "请先填写要打开的任务编号。" });
      return;
    }
    const matched = history.find((task) => task.id === nextId || task.custom_task_id === nextId);
    const resolvedId = matched?.id ?? nextId;
    setTaskId(resolvedId);
    setManualTaskId(nextId);
    setError(null);
    await refresh(resolvedId);
    await loadAudit(resolvedId);
  };

  const leaveTask = () => {
    keys.clearAll();
    setTaskId("");
    setManualTaskId("");
    setDetail(null);
    setPatches([]);
    setAudit([]);
    setAuditError(null);
    setError(null);
    setNotice("已退出当前结果。历史记录已保留，现在可以放入下一份代码。");
  };

  const checkConnection = async () => {
    try {
      setReady(await client.readyz());
      setError(null);
      setNotice("服务连接正常。现在可以继续创建审查任务。");
    } catch (exc) {
      handleError(exc);
    }
  };

  const runAction = async (action: string, work: (key: string) => Promise<unknown>, done: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await work(keys.keyFor(action));
      keys.clear(action);
      setError(null);
      setNotice(done);
      if (taskId) {
        await refresh(taskId);
        await loadAudit(taskId);
      }
    } catch (exc) {
      // 失败时保留幂等键：用户重试是同一次操作的重试，不是新命令。
      handleError(exc);
    } finally {
      setBusy(false);
    }
  };

  const impact = React.useMemo<ImpactSummaryView | null>(() => {
    if (!detail) return null;
    const artifact = detail.artifacts.find((item: ArtifactSummary) => item.artifact_type === "ImpactReport");
    if (!artifact) return null;
    const summary = detail.task.summary ?? {};
    if (!summary.risk_level) return null;
    return {
      changed_symbols: (summary.changed_symbols as string[]) ?? [],
      direct_callers: (summary.direct_callers as string[]) ?? [],
      direct_callees: (summary.direct_callees as string[]) ?? [],
      affected_files: (summary.affected_files as string[]) ?? [],
      risk_level: String(summary.risk_level),
      uncertain: Boolean(summary.uncertain),
    };
  }, [detail]);

  const evidence = React.useMemo<VerifyEvidenceView | null>(() => {
    const raw = detail?.task.summary?.verify_evidence as VerifyEvidenceView | undefined;
    return raw ?? null;
  }, [detail]);

  const canApprove = identity.role === "approver" || identity.role === "admin";
  const canResume = identity.role === "admin";
  const status = detail?.task.status ?? "";
  const fixDisabledReason =
    status !== "REVIEWED"
      ? "审查完成后才能让修复 Agent 生成建议"
      : detail?.task.mode === "offline"
        ? "离线规则扫描不生成修复建议"
        : "";

  return React.createElement(
    "div",
    { className: "app" },
    React.createElement(
      "div",
      { className: "topbar" },
      React.createElement("h1", null, "CodePilot 审查工作台"),
      React.createElement(
        "span",
        { className: "muted" },
        ready
          ? `系统状态：可用 · ${ready.agents.length} 个 Agent 已就绪`
          : "系统连接中",
      ),
      React.createElement("button", { className: "nav-button", type: "button", onClick: () => document.getElementById("create-review")?.scrollIntoView({ behavior: "smooth" }) }, "创建审查"),
      React.createElement("button", { className: "nav-button", type: "button", onClick: () => document.getElementById("review-history")?.scrollIntoView({ behavior: "smooth" }) }, "审查历史"),
      React.createElement("div", { className: "spacer" }),
      React.createElement(
        "div",
        { className: "identity" },
        React.createElement("span", null, `${identity.displayName ?? "用户"} · 工号 ${identity.actorId}`),
        React.createElement(
          "label",
          { style: { display: "flex", gap: 6, alignItems: "center", margin: 0 } },
          React.createElement("input", {
            type: "checkbox",
            style: { width: "auto" },
            checked: autoRefresh,
            onChange: (e: React.ChangeEvent<HTMLInputElement>) => setAutoRefresh(e.target.checked),
          }),
          "审查中自动更新",
        ),
        React.createElement("button", { onClick: () => void checkConnection() }, "检查服务连接"),
        React.createElement("span", { className: "muted" }, identity.role === "admin" ? "管理员" : identity.role === "approver" ? "审批人" : "开发者"),
        React.createElement("button", { type: "button", onClick: onLogout }, "退出登录"),
      ),
    ),
    React.createElement(
      "div",
      { className: "layout" },
      React.createElement(
        "div",
        { id: "create-review" },
        React.createElement(ErrorBanner, { error }),
        notice ? React.createElement("div", { className: "notice-box" }, notice) : null,
        React.createElement(CreateForm, { onSubmit: onCreate, busy }),
        React.createElement(
          "div",
          { className: "panel" },
          React.createElement("h2", null, "按编号打开审查记录"),
          React.createElement("label", null, "已有任务编号（手动粘贴，长度不限）"),
          React.createElement(
            "div",
            { className: "row" },
            React.createElement("input", {
              value: manualTaskId,
              onChange: (e: React.ChangeEvent<HTMLInputElement>) => setManualTaskId(e.target.value),
              placeholder: "把任务编号粘贴到这里",
            }),
            React.createElement("button", { onClick: () => void openTask(manualTaskId) }, "打开这份记录"),
          ),
          React.createElement(
            "div",
            { className: "hint" },
            "刚创建的任务会自动显示在右侧。也可以直接从下方“审查历史”打开，不需要记住编号。",
          ),
        ),
        React.createElement(HistoryPanel, {
          tasks: history,
          loading: historyLoading,
          onOpen: (id) => void openTask(id),
          onRefresh: () => void loadHistory(),
        }),
        identity.role === "admin"
          ? React.createElement(AuditPanel, {
              events: audit,
              error: auditError,
              onRefresh: () => void loadAudit(taskId),
            })
          : null,
      ),
      React.createElement(
        "div",
        null,
        !detail
          ? React.createElement("div", { className: "panel muted" }, "请先在左侧选择代码并创建任务。创建后，这里会展示 A2A 协作过程和审查结果。")
          : React.createElement(
              React.Fragment,
              null,
              React.createElement(
                "div",
                { className: "task-toolbar" },
                React.createElement("strong", null, `正在查看：${detail.task.custom_task_id || detail.task.id}`),
                React.createElement("button", { type: "button", onClick: leaveTask }, "结束查看，准备下一份代码"),
              ),
              React.createElement(TaskHeader, { detail }),
              !TERMINAL.has(detail.task.status)
                ? React.createElement("div", { className: "processing-banner", role: "status" },
                    React.createElement("span", { className: "spinner", "aria-hidden": "true" }),
                    React.createElement("span", null, `正在审查中：${detail.task.current_step || "Agent 正在协作处理"}，页面会自动更新`),
                  )
                : null,
              React.createElement(ResumePanel, {
                detail,
                canResume,
                busy,
                onResume: (targetStatus, reason, expectedVersion) =>
                  void runAction(
                    `resume:${detail.task.id}:${targetStatus}`,
                    (key) =>
                      client.resume(
                        detail.task.id,
                        { target_status: targetStatus, expected_version: expectedVersion, reason },
                        { idempotencyKey: key },
                      ),
                    `已提交人工恢复 → ${targetStatus}`,
                  ),
              }),
              React.createElement(FindingsPanel, { comments: detail.comments }),
              React.createElement(ImpactPanel, { impact, artifacts: detail.artifacts }),
              React.createElement(PatchPanel, {
                patches,
                evidence,
                canApprove,
                canFix: status === "REVIEWED" && detail.task.mode !== "offline",
                fixDisabledReason,
                busy,
                onFix: () =>
                  void runAction(
                    `fix:${detail.task.id}`,
                    (key) => client.triggerFix(detail.task.id, { idempotencyKey: key }),
                    "已触发 Fix / Verify 链路",
                  ),
                onApprove: (patch, reason) =>
                  void runAction(
                    `approve:${patch.id}:${patch.patch_version}`,
                    (key) => client.approve(patch.id, patch.patch_version, reason, { idempotencyKey: key }),
                    `已批准补丁 v${patch.patch_version}`,
                  ),
                onReject: (patch, reason) =>
                  void runAction(
                    `reject:${patch.id}:${patch.patch_version}`,
                    (key) => client.reject(patch.id, patch.patch_version, reason, { idempotencyKey: key }),
                    `已拒绝补丁 v${patch.patch_version}`,
                  ),
                onMerge: (patch) =>
                  void runAction(
                    `merge:${patch.id}:${patch.patch_version}`,
                    (key) => client.merge(patch.id, patch.patch_version, { idempotencyKey: key }),
                    `补丁 v${patch.patch_version} 已合并到任务分支`,
                  ),
              }),
            ),
      ),
    ),
  );
}

export default function App(): React.ReactElement {
  const [identity, setIdentity] = React.useState<Identity | null>(readStoredIdentity);
  if (!identity) return React.createElement(AuthPage, { onAuthenticated: setIdentity });
  return React.createElement(Workbench, {
    identity,
    onLogout: () => {
      if (identity.token) void new CodePilotClient(identity).authLogout(identity.token);
      window.localStorage.removeItem("codepilot.identity");
      setIdentity(null);
    },
  });
}
