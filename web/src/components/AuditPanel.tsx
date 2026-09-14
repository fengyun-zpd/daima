/** 管理员排查记录：追查任务与 Agent 执行经过，普通审查无需查看。 */

import React from "react";
import type { AuditEvent } from "../types";

export function AuditPanel({
  events,
  onRefresh,
  error,
}: {
  events: AuditEvent[];
  onRefresh: () => void;
  error: string | null;
}): React.ReactElement {
  const [filter, setFilter] = React.useState("");
  const filtered = filter
    ? events.filter((event) =>
        `${event.event_type} ${event.action} ${event.actor_id} ${event.entity_id}`
          .toLowerCase()
          .includes(filter.toLowerCase()),
      )
    : events;
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement(
      "div",
      { className: "row" },
      React.createElement("h2", { style: { flex: 1 } }, `管理员排查记录（${filtered.length}）`),
      React.createElement("button", { onClick: onRefresh }, "刷新管理员记录"),
    ),
    React.createElement("div", { className: "hint" }, "用于排查谁在何时创建了任务、Agent 执行到哪一步以及失败原因。普通审查不需要操作这里。"),
    React.createElement("input", {
      placeholder: "按操作人、任务编号或事件搜索",
      value: filter,
      onChange: (e) => setFilter(e.target.value),
    }),
    error ? React.createElement("div", { className: "error-box" }, error) : null,
    filtered.length === 0
      ? React.createElement("div", { className: "muted" }, "当前任务还没有可供管理员排查的事件。")
      : React.createElement(
          "table",
          null,
          React.createElement(
            "thead",
            null,
            React.createElement(
              "tr",
              null,
              React.createElement("th", null, "时间"),
              React.createElement("th", null, "事件"),
              React.createElement("th", null, "操作人"),
              React.createElement("th", null, "关联对象"),
              React.createElement("th", null, "摘要"),
            ),
          ),
          React.createElement(
            "tbody",
            null,
            [...filtered].reverse().map((event) =>
              React.createElement(
                "tr",
                { key: event.id },
                React.createElement(
                  "td",
                  { className: "mono" },
                  new Date(event.created_at).toLocaleTimeString(),
                ),
                React.createElement(
                  "td",
                  null,
                  event.event_type,
                  event.error_code
                    ? React.createElement(
                        "div",
                        { className: "mono severity-critical" },
                        event.error_code,
                      )
                    : null,
                ),
                React.createElement(
                  "td",
                  { className: "mono" },
                  `${event.actor_role}/${event.actor_id}`,
                ),
                React.createElement(
                  "td",
                  { className: "mono" },
                  `${event.entity_type}:${event.entity_id.slice(0, 16)}`,
                ),
                React.createElement(
                  "td",
                  { className: "muted" },
                  JSON.stringify(event.after_state).slice(0, 120),
                ),
              ),
            ),
          ),
        ),
  );
}
