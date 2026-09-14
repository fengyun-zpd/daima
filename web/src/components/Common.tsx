/** 通用展示组件：状态徽标、键值行、时间线与错误条。 */

import React from "react";
import type { AuditTailItem, ChildTask } from "../types";

export function StatusBadge({ status }: { status: string }): React.ReactElement {
  return React.createElement("span", { className: `badge status-${status}` }, status);
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
  return React.createElement(
    "div",
    null,
    React.createElement("h2", null, "父子任务时间线"),
    React.createElement(
      "ul",
      { className: "timeline" },
      children.map((child) =>
        React.createElement(
          "li",
          { key: child.id },
          React.createElement(
            "div",
            { className: "row" },
            React.createElement("strong", null, `${child.task_type} · ${child.agent_id}`),
            React.createElement(StatusBadge, { status: child.status }),
            React.createElement(
              "span",
              { className: "muted" },
              `attempt ${child.attempt}/${child.max_attempts} · transport ${child.transport}`,
            ),
          ),
          React.createElement(
            "div",
            { className: "mono muted" },
            `${child.id} · deadline ${new Date(child.deadline).toLocaleString()}`,
          ),
          child.error_code
            ? React.createElement(
                "div",
                { className: "mono severity-critical" },
                `${child.error_code}: ${child.error_message ?? ""}`,
              )
            : null,
        ),
      ),
      audit.slice(-8).map((event) =>
        React.createElement(
          "li",
          { key: event.id, className: "muted" },
          React.createElement(
            "div",
            { className: "row" },
            React.createElement("span", { className: "mono" }, event.event_type),
            event.agent_id ? React.createElement("span", { className: "muted" }, event.agent_id) : null,
            React.createElement(
              "span",
              { className: "muted" },
              new Date(event.created_at).toLocaleTimeString(),
            ),
          ),
        ),
      ),
    ),
  );
}
