/** CodePilot 审查工作台：直接进入任务工作台，无营销页。 */

import React from "react";
import { ApiError, CodePilotClient, OperationKeys, type Identity } from "./api";
import { AuditPanel } from "./components/AuditPanel";
import { ErrorBanner } from "./components/Common";
import { CreateForm, type CreateFormValues } from "./components/CreateForm";
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

function readStoredIdentity(): Identity {
  const raw = window.localStorage.getItem("codepilot.identity");
  if (raw) {
    try {
      const parsed = JSON.parse(raw) as Identity;
      if (parsed.actorId && parsed.role) return parsed;
    } catch {
      /* 忽略损坏的本地存储 */
    }
  }
  return { actorId: "dev-1", role: "developer" };
}

export default function App(): React.ReactElement {
  const [identity, setIdentity] = React.useState<Identity>(readStoredIdentity);
  const [taskId, setTaskId] = React.useState<string>("");
  const [detail, setDetail] = React.useState<ReviewDetail | null>(null);
  const [patches, setPatches] = React.useState<Patch[]>([]);
  const [audit, setAudit] = React.useState<AuditEvent[]>([]);
  const [ready, setReady] = React.useState<ReadyStatus | null>(null);
  const [error, setError] = React.useState<(ErrorPayload & { status?: number }) | null>(null);
  const [auditError, setAuditError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [autoRefresh, setAutoRefresh] = React.useState(true);

  const client = React.useMemo(() => new CodePilotClient(identity), [identity]);
  // 用户操作级幂等键：同一次操作失败后重试复用同一个键，成功后清空。
  const keys = React.useMemo(() => new OperationKeys(), []);

  React.useEffect(() => {
    window.localStorage.setItem("codepilot.identity", JSON.stringify(identity));
  }, [identity]);

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

  const loadAudit = React.useCallback(
    async (id: string) => {
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
    [client],
  );

  React.useEffect(() => {
    void loadReady();
  }, [loadReady]);

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
        },
        { idempotencyKey: keys.keyFor(action) },
      );
      keys.clear(action);
      setTaskId(created.task.id);
      setError(null);
      setNotice(created.created ? null : "后端命中幂等记录，返回的是原任务（未重复创建）。");
      await refresh(created.task.id);
      await loadAudit(created.task.id);
    } catch (exc) {
      handleError(exc);
    } finally {
      setBusy(false);
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
      ? `只有 REVIEWED 状态可以生成候选补丁（当前 ${status || "-"}）`
      : detail?.task.mode === "offline"
        ? "offline 模式不生成补丁"
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
          ? `API ${ready.status} · transport=${ready.transport} · db=${ready.database} · tools ${ready.tools.length} · agents ${ready.agents.length} · 待恢复 ${ready.recoverable_tasks}`
          : "API 未就绪",
      ),
      React.createElement("div", { className: "spacer" }),
      React.createElement(
        "div",
        { className: "identity" },
        React.createElement("span", null, "身份"),
        React.createElement("input", {
          style: { width: 120 },
          value: identity.actorId,
          onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
            setIdentity({ ...identity, actorId: e.target.value }),
        }),
        React.createElement(
          "select",
          {
            style: { width: 120 },
            value: identity.role,
            onChange: (e: React.ChangeEvent<HTMLSelectElement>) =>
              setIdentity({ ...identity, role: e.target.value as Identity["role"] }),
          },
          React.createElement("option", { value: "developer" }, "developer"),
          React.createElement("option", { value: "approver" }, "approver"),
          React.createElement("option", { value: "admin" }, "admin"),
        ),
        React.createElement(
          "label",
          { style: { display: "flex", gap: 6, alignItems: "center", margin: 0 } },
          React.createElement("input", {
            type: "checkbox",
            style: { width: "auto" },
            checked: autoRefresh,
            onChange: (e: React.ChangeEvent<HTMLInputElement>) => setAutoRefresh(e.target.checked),
          }),
          "轮询刷新",
        ),
        React.createElement("button", { onClick: () => void loadReady() }, "重连"),
      ),
    ),
    React.createElement(
      "div",
      { className: "layout" },
      React.createElement(
        "div",
        null,
        React.createElement(ErrorBanner, { error }),
        notice ? React.createElement("div", { className: "notice-box" }, notice) : null,
        React.createElement(CreateForm, { onSubmit: onCreate, busy }),
        React.createElement(
          "div",
          { className: "panel" },
          React.createElement("h2", null, "当前任务"),
          React.createElement("label", null, "任务 ID（可粘贴已有任务）"),
          React.createElement(
            "div",
            { className: "row" },
            React.createElement("input", {
              value: taskId,
              onChange: (e: React.ChangeEvent<HTMLInputElement>) => setTaskId(e.target.value.trim()),
              placeholder: "task-01J...",
            }),
            React.createElement("button", { onClick: () => void refresh(taskId) }, "加载"),
          ),
          React.createElement(
            "div",
            { className: "hint" },
            "创建任务后自动加载；非终态任务每 2 秒轮询一次，进入终态或等待人工动作后自动停止（也可用 SSE 端点 /events）。",
          ),
        ),
        React.createElement(AuditPanel, {
          events: audit,
          error: auditError,
          onRefresh: () => void loadAudit(taskId),
        }),
      ),
      React.createElement(
        "div",
        null,
        !detail
          ? React.createElement("div", { className: "panel muted" }, "在左侧创建或加载一个审查任务。")
          : React.createElement(
              React.Fragment,
              null,
              React.createElement(TaskHeader, { detail }),
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
