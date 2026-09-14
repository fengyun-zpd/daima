/** 通用展示组件：状态徽标、键值行、时间线与错误条。 */

import React from "react";
import type { AuditTailItem, ChildTask } from "../types";

export function StatusBadge({ status }: { status: string }): React.ReactElement {
  const labels: Record<string, string> = {
    DRAFT: "等待开始",
    REVIEWING: "审查中",
    REVIEWED: "审查完成",
    FIXING: "修复中",
    TESTING: "验证中",
    PENDING_APPROVAL: "等待确认",
    MERGED: "已合并",
    NEEDS_HUMAN: "需人工处理",
    REJECTED: "已拒绝",
    FAILED: "失败",
    completed: "已完成",
    working: "处理中",
    pending: "等待中",
    failed: "失败",
  };
  return React.createElement("span", { className: `badge status-${status}` }, labels[status] ?? status);
}

export function KeyValue({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}): React.ReactElement {
  return React.createElement(
    "div",
    { className: "kv" },
    React.createElement("span", { className: "muted" }, label),
    React.createElement("span", { className: "mono" }, value ?? "-"),
  );
}

export function ErrorBanner({
  error,
}: {
  error: { code: string; message: string; trace_id?: string | null; status?: number; details?: unknown } | null;
}): React.ReactElement | null {
  if (!error) return null;
  const hint =
    error.status === 403
      ? "当前身份无权限（可切换为 approver / admin）"
      : error.status === 409
        ? "冲突：状态、版本或幂等键不匹配"
        : error.status === 422
          ? "请求体校验失败"
          : error.status === 503
            ? "服务暂不可用（如沙箱不可用）"
            : null;
  return React.createElement(
    "div",
    { className: "error-box" },
    React.createElement(
      "div",
      { className: "mono" },
      `${error.status ? `HTTP ${error.status} · ` : ""}${error.code}: ${error.message}`,
    ),
    hint ? React.createElement("div", { className: "muted" }, hint) : null,
    error.trace_id ? React.createElement("div", { className: "mono muted" }, `trace_id: ${error.trace_id}`) : null,
    error.details
      ? React.createElement("div", { className: "mono muted" }, JSON.stringify(error.details).slice(0, 400))
      : null,
  );
}

export function TaskTimeline({
  children,
  audit,
}: {
  children: ChildTask[];
  audit: AuditTailItem[];
}): React.ReactElement {
  const agentDescription: Record<string, { name: string; work: string }> = {
    "review-agent": { name: "代码审查 Agent", work: "检查安全风险、代码缺陷和可自动修复的问题" },
    "impact-agent": { name: "影响分析 Agent", work: "分析改动会影响哪些文件、函数和调用关系" },
    "fix-agent": { name: "修复 Agent", work: "根据已确认的问题生成候选补丁" },
    "verify-agent": { name: "验证 Agent", work: "在受控环境中检查补丁能否应用并运行测试" },
  };
  const statusText: Record<string, string> = {
    completed: "已完成",
    working: "正在处理",
    pending: "等待开始",
    failed: "执行失败",
    canceled: "已取消",
  };
  const completed = children.filter((child) => child.status === "completed").length;
  return React.createElement(
    "div",
    { className: "a2a-flow" },
    React.createElement("h2", null, "A2A 协作过程"),
    React.createElement(
      "p",
      { className: "hint" },
      children.length
        ? `总任务把工作分给 ${children.length} 个专职 Agent。当前已完成 ${completed}/${children.length} 个步骤；它们的结果会自动汇总到下方的“发现的问题”和“影响范围”。`
        : "任务创建后，系统会在这里显示参与协作的 Agent 及其处理进度。",
    ),
    React.createElement(
      "ul",
      { className: "timeline a2a-timeline" },
      children.map((child) =>
        React.createElement(
          "li",
          { key: child.id },
          React.createElement(
            "div",
            { className: "row" },
            React.createElement("strong", null, agentDescription[child.agent_id]?.name ?? child.agent_id),
            React.createElement(StatusBadge, { status: child.status }),
            React.createElement("span", { className: "muted" }, statusText[child.status] ?? child.status),
          ),
          React.createElement("div", { className: "hint" }, agentDescription[child.agent_id]?.work ?? "处理分配的协作任务"),
          child.error_code
            ? React.createElement(
                "div",
                { className: "severity-critical" },
                `该步骤未完成：${child.error_message ?? child.error_code}`,
              )
            : null,
          React.createElement(
            "details",
            { className: "technical-details" },
            React.createElement("summary", { className: "muted" }, "技术详情"),
            React.createElement(
              "div",
              { className: "mono muted" },
              `任务类型 ${child.task_type} · attempt ${child.attempt}/${child.max_attempts} · transport ${child.transport}`,
            ),
            React.createElement(
              "div",
              { className: "mono muted" },
              `${child.id} · 截止 ${new Date(child.deadline).toLocaleString()}`,
            ),
          ),
        ),
      ),
    ),
    audit.length
      ? React.createElement(
          "details",
          { className: "technical-details" },
          React.createElement("summary", { className: "muted" }, "查看系统事件记录"),
          audit.slice(-8).map((event) =>
            React.createElement(
              "div",
              { key: event.id, className: "mono muted" },
              `${new Date(event.created_at).toLocaleTimeString()} · ${event.event_type}${event.agent_id ? ` · ${event.agent_id}` : ""}`,
            ),
          ),
        )
      : null,
  );
}
