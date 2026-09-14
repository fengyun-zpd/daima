/** 审查历史：让用户不记任务编号也能重新打开此前结果。 */

import React from "react";

import { StatusBadge } from "./Common";
import type { ReviewTask } from "../types";

const MODE_LABEL: Record<string, string> = {
  a2a: "多 Agent 协作",
  single: "单 Agent",
  offline: "本地规则扫描",
};

export function HistoryPanel({
  tasks,
  loading,
  onOpen,
  onRefresh,
}: {
  tasks: ReviewTask[];
  loading: boolean;
  onOpen: (taskId: string) => void;
  onRefresh: () => void;
}): React.ReactElement {
  const [filter, setFilter] = React.useState("");
  const normalized = filter.trim().toLowerCase();
  const visible = normalized
    ? tasks.filter((task) => `${task.id} ${task.custom_task_id ?? ""} ${task.base_commit} ${task.status}`.toLowerCase().includes(normalized))
    : tasks;

  return React.createElement(
    "div",
    { className: "panel", id: "review-history" },
    React.createElement(
      "div",
      { className: "row" },
      React.createElement("h2", { style: { flex: 1 } }, "审查历史"),
      React.createElement("button", { type: "button", onClick: onRefresh, disabled: loading }, loading ? "刷新中…" : "刷新记录"),
    ),
    React.createElement("div", { className: "hint" }, "每次创建的审查都会保留。退出当前结果不会删除记录，随时可以从这里重新打开。"),
    React.createElement("input", {
      value: filter,
      placeholder: "搜索任务编号或版本备注",
      onChange: (event: React.ChangeEvent<HTMLInputElement>) => setFilter(event.target.value),
    }),
    visible.length === 0
      ? React.createElement("div", { className: "muted history-empty" }, loading ? "正在读取审查记录…" : "还没有审查记录。放入第一份代码后，记录会显示在这里。")
      : React.createElement(
          "div",
          { className: "history-list" },
          visible.map((task) =>
            React.createElement(
              "div",
              { key: task.id, className: "history-item" },
              React.createElement(
                "div",
                { className: "row" },
                React.createElement("strong", { className: "history-id", title: task.custom_task_id || task.id }, task.custom_task_id || task.id),
                React.createElement(StatusBadge, { status: task.status }),
              ),
              React.createElement("div", { className: "hint" }, `${MODE_LABEL[task.mode] ?? task.mode} · ${task.base_commit || "未填写版本备注"} · 系统 ID ${task.id}`),
              React.createElement("div", { className: "hint" }, `创建于 ${new Date(task.created_at).toLocaleString("zh-CN")}`),
              React.createElement("button", { type: "button", onClick: () => onOpen(task.id) }, "查看这份审查结果"),
            ),
          ),
        ),
  );
}
