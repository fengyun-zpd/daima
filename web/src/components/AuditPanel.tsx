/** 审计事件查看：按任务或 trace 过滤，展示 actor / action / 事件类型与状态摘要。 */

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
      React.createElement("h2", { style: { flex: 1 } }, `系统审计（${filtered.length}）`),
      React.createElement("button", { onClick: onRefresh }, "刷新"),
    ),
    React.createElement("input", {
      placeholder: "过滤：事件类型 / 动作 / actor / 实体",
      value: filter,
      onChange: (e) => setFilter(e.target.value),
    }),
    error ? React.createElement("div", { className: "error-box" }, error) : null,
    filtered.length === 0
      ? React.createElement("div", { className: "muted" }, "切换为“管理员”身份后可查看系统审计记录。")
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
              React.createElement("th", null, "actor"),
              React.createElement("th", null, "实体"),
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
