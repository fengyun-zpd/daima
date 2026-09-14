/** 任务详情：Finding、ImpactReport、PatchCandidate、VerifyEvidence、审批与审计。 */

import React from "react";
import type {
  ArtifactSummary,
  Comment,
  ImpactSummaryView,
  Patch,
  ReviewDetail,
  VerifyEvidenceView,
} from "../types";
import { KeyValue, StatusBadge, TaskTimeline } from "./Common";

function SeverityClass(severity: string): string {
  return `severity-${severity}`;
}

const severityText: Record<string, string> = {
  critical: "严重风险",
  warning: "需要关注",
  info: "提示",
  high: "高",
  medium: "中",
  low: "低",
};

const artifactTypeText: Record<string, string> = {
  Finding: "审查发现",
  ReviewComment: "审查意见",
  ImpactReport: "影响范围分析",
  PatchCandidate: "修复建议",
  VerifyEvidence: "验证结果",
};

export function FindingsPanel({ comments }: { comments: Comment[] }): React.ReactElement {
  const sorted = [...comments].sort(
    (a, b) => a.file.localeCompare(b.file) || a.line - b.line,
  );
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement("h2", null, `发现的问题（${comments.length}）`),
    React.createElement("div", { className: "hint" }, "按风险等级查看每个问题的位置、原因和是否可让修复 Agent 尝试处理。"),
    sorted.length === 0
      ? React.createElement("div", { className: "muted" }, "暂无意见")
      : React.createElement(
          "table",
          null,
          React.createElement(
            "thead",
            null,
            React.createElement(
              "tr",
              null,
              React.createElement("th", null, "风险等级"),
              React.createElement("th", null, "检查项"),
              React.createElement("th", null, "代码位置"),
              React.createElement("th", null, "把握程度"),
              React.createElement("th", null, "为什么是问题"),
              React.createElement("th", null, "能否自动修复"),
            ),
          ),
          React.createElement(
            "tbody",
            null,
            sorted.map((comment) =>
              React.createElement(
                "tr",
                { key: comment.id },
                React.createElement(
                  "td",
                  { className: SeverityClass(comment.severity) },
                  severityText[comment.severity] ?? comment.severity,
                ),
                React.createElement(
                  "td",
                  { className: "mono" },
                  comment.rule_id,
                  React.createElement("div", { className: "muted" }, comment.cwe),
                ),
                React.createElement(
                  "td",
                  { className: "mono" },
                  `${comment.file}:${comment.line}`,
                ),
                React.createElement(
                  "td",
                  { className: "mono" },
                  comment.confidence_level === "high" ? "高" : comment.confidence_level === "medium" ? "中" : "低",
                ),
                React.createElement(
                  "td",
                  null,
                  comment.message,
                  React.createElement(
                    "div",
                    { className: "mono muted" },
                    comment.evidence.slice(0, 120),
                  ),
                ),
                React.createElement(
                  "td",
                  null,
                  comment.auto_fixable
                    ? React.createElement("span", { className: "badge" }, "可以尝试")
                    : "需要人工判断",
                ),
              ),
            ),
          ),
        ),
  );
}

export function ImpactPanel({
  impact,
  artifacts,
}: {
  impact: ImpactSummaryView | null;
  artifacts: ArtifactSummary[];
}): React.ReactElement {
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement("h2", null, "影响范围"),
    React.createElement("div", { className: "hint" }, "影响分析 Agent 会在这里告诉你：这个改动还可能关联哪些文件和函数。"),
    impact
      ? React.createElement(
          React.Fragment,
          null,
          React.createElement(KeyValue, { label: "整体风险", value: severityText[impact.risk_level] ?? impact.risk_level }),
          React.createElement(KeyValue, {
            label: "改动的函数或变量",
            value: impact.changed_symbols.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "可能受影响的调用方",
            value: impact.direct_callers.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "改动调用的函数",
            value: impact.direct_callees.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "可能受影响的文件",
            value: impact.affected_files.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "是否存在不确定性",
            value: impact.uncertain ? "有，需要人工确认" : "没有明显不确定性",
          }),
        )
      : React.createElement("div", { className: "muted" }, "影响分析 Agent 还在处理，结果完成后会显示在这里。"),
    React.createElement("h2", { style: { marginTop: 12 } }, `协作产物（${artifacts.length}）`),
    React.createElement(
      "table",
      null,
      React.createElement(
        "thead",
        null,
        React.createElement(
          "tr",
          null,
          React.createElement("th", null, "结果类型"),
          React.createElement("th", null, "版本"),
          React.createElement("th", null, "结果指纹"),
          React.createElement("th", null, "是否可信"),
        ),
      ),
      React.createElement(
        "tbody",
        null,
        artifacts.map((artifact) =>
          React.createElement(
            "tr",
            { key: artifact.id },
            React.createElement(
              "td",
              null,
              artifactTypeText[artifact.artifact_type] ?? artifact.artifact_type,
            ),
            React.createElement("td", { className: "mono" }, artifact.schema_version),
            React.createElement("td", { className: "mono" }, artifact.content_hash.slice(0, 22) + "…"),
            React.createElement(
              "td",
              null,
              React.createElement(
                "span",
                { className: `badge status-${artifact.validated ? "ok" : "fail"}` },
                artifact.validated ? "已校验" : "未通过校验",
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

export function PatchPanel({
  patches,
  evidence,
  canApprove,
  canFix,
  fixDisabledReason,
  onFix,
  onApprove,
  onReject,
  onMerge,
  busy,
}: {
  patches: Patch[];
  evidence: VerifyEvidenceView | null;
  canApprove: boolean;
  canFix: boolean;
  fixDisabledReason: string;
  onFix: () => void;
  onApprove: (patch: Patch, reason: string) => void;
  onReject: (patch: Patch, reason: string) => void;
  onMerge: (patch: Patch) => void;
  busy: boolean;
}): React.ReactElement {
  const [reason, setReason] = React.useState("测试证据充分，影响范围符合预期");
  const latest = patches.length ? patches[patches.length - 1] : null;
  // 按钮门禁：审批只在补丁进入 pending_approval 时可用；合并只在 approved 时可用。
  const patchStatus = latest?.status ?? "";
  const canDecide = canApprove && patchStatus === "pending_approval";
  const canMerge = canApprove && patchStatus === "approved";
  const decideReason = !canApprove
    ? "仅 approver / admin 角色可审批"
    : patchStatus !== "pending_approval"
      ? `补丁状态为 ${patchStatus || "-"}，需先通过 Verify 进入 pending_approval`
      : "";
  const mergeReason = !canApprove
    ? "仅 approver / admin 角色可合并"
    : patchStatus !== "approved"
      ? `补丁状态为 ${patchStatus || "-"}，需先审批通过`
      : "";
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement(
      "div",
      { className: "row" },
      React.createElement("h2", { style: { flex: 1 } }, `自动修复建议（${patches.length}）`),
      React.createElement(
        "button",
        {
          onClick: onFix,
          disabled: busy || !canFix,
          title: canFix ? "" : fixDisabledReason,
        },
        "让 Agent 尝试修复",
      ),
    ),
    !canFix && fixDisabledReason
      ? React.createElement("div", { className: "hint" }, fixDisabledReason)
      : null,
    !latest
      ? React.createElement("div", { className: "muted" }, "审查完成后，可点击“让 Agent 尝试修复”。修复 Agent 生成建议，验证 Agent 运行检查。")
      : React.createElement(
          React.Fragment,
          null,
          React.createElement(KeyValue, { label: "patch_version", value: latest.patch_version }),
          React.createElement(KeyValue, {
            label: "status",
            value: React.createElement(StatusBadge, { status: latest.status }),
          }),
          React.createElement(KeyValue, { label: "patch_hash", value: latest.patch_hash }),
          React.createElement(KeyValue, { label: "fix_category", value: latest.fix_category }),
          React.createElement(KeyValue, { label: "target_branch", value: latest.target_branch }),
          React.createElement(KeyValue, {
            label: "changed_files",
            value: latest.changed_files.join(", "),
          }),
          React.createElement(KeyValue, {
            label: "changed_functions",
            value: latest.changed_functions.join(", ") || "-",
          }),
          React.createElement(KeyValue, { label: "scope_drift", value: String(latest.scope_drift) }),
          React.createElement(KeyValue, { label: "wide_impact", value: String(latest.wide_impact) }),
          React.createElement(KeyValue, { label: "test_gap", value: String(latest.test_gap) }),
          React.createElement(KeyValue, {
            label: "coverage_delta",
            value:
              latest.coverage_delta === null || latest.coverage_delta === undefined
                ? "-"
                : latest.coverage_delta.toFixed(4),
          }),
          React.createElement("h2", { style: { marginTop: 12 } }, "建议修改内容"),
          React.createElement("pre", { className: "diff" }, latest.diff),
          React.createElement("h2", { style: { marginTop: 12 } }, "人工确认记录"),
          latest.approvals.length === 0
            ? React.createElement("div", { className: "muted" }, "尚无审批记录")
            : React.createElement(
                "table",
                null,
                React.createElement(
                  "tbody",
                  null,
                  latest.approvals.map((approval) =>
                    React.createElement(
                      "tr",
                      { key: approval.id },
                      React.createElement("td", null, approval.decision),
                      React.createElement("td", null, approval.decided_by),
                      React.createElement("td", { className: "mono" }, `v${approval.patch_version}`),
                      React.createElement("td", null, approval.reason),
                    ),
                  ),
                ),
              ),
          React.createElement("label", null, "确认说明（拒绝时必填）"),
          React.createElement("input", { value: reason, onChange: (e) => setReason(e.target.value) }),
          React.createElement(
            "div",
            { className: "row", style: { marginTop: 8 } },
            React.createElement(
              "button",
              {
                className: "primary",
                disabled: busy || !canDecide,
                onClick: () => onApprove(latest, reason),
                title: decideReason,
              },
              "确认并允许合并",
            ),
            React.createElement(
              "button",
              {
                className: "danger",
                disabled: busy || !canDecide,
                onClick: () => onReject(latest, reason),
                title: decideReason,
              },
              "拒绝此建议",
            ),
            React.createElement(
              "button",
              {
                disabled: busy || !canMerge,
                onClick: () => onMerge(latest),
                title: mergeReason,
              },
              "合并到任务分支",
            ),
          ),
          decideReason || mergeReason
            ? React.createElement(
                "div",
                { className: "hint" },
                [decideReason, mergeReason].filter(Boolean).join("；"),
              )
            : null,
        ),
    React.createElement("h2", { style: { marginTop: 12 } }, "验证结果"),
    !evidence
      ? React.createElement("div", { className: "muted" }, "生成修复建议后，验证 Agent 会在这里展示检查结果。")
      : React.createElement(
          React.Fragment,
          null,
          React.createElement(KeyValue, {
            label: "验证环境",
            value: evidence.sandbox_run_id,
          }),
          React.createElement(KeyValue, { label: "程序退出结果", value: evidence.exit_code === 0 ? "正常" : `异常（${evidence.exit_code}）` }),
          React.createElement(KeyValue, {
            label: "修改能否应用",
            value: evidence.apply_check.ok ? "可以" : evidence.apply_check.message,
          }),
          React.createElement(KeyValue, { label: "安全复查", value: evidence.pre_scan.ok ? "通过" : "发现阻断项" }),
          React.createElement(KeyValue, {
            label: "lint",
            value: `${evidence.lint.tool} issues=${evidence.lint.issue_count}`,
          }),
          React.createElement(KeyValue, {
            label: "单元测试",
            value: `${evidence.unit_tests.passed} 个通过，${evidence.unit_tests.failed} 个失败`,
          }),
          React.createElement(KeyValue, {
            label: "regression",
            value: `${evidence.regression.baseline_failed_fixed}/${evidence.regression.baseline_failed_total}`,
          }),
          React.createElement(KeyValue, {
            label: "测试覆盖率",
            value: `${evidence.coverage.before ?? "-"} → ${evidence.coverage.after ?? "-"}`,
          }),
          React.createElement(KeyValue, { label: "是否缺少测试", value: evidence.test_gap ? "是" : "否" }),
          React.createElement(KeyValue, {
            label: "证据是否足够",
            value: evidence.evidence_sufficient ? "足够" : "不足，需要人工确认",
          }),
          React.createElement(
            "table",
            null,
            React.createElement(
              "tbody",
              null,
              evidence.tiers.map((tier) =>
                React.createElement(
                  "tr",
                  { key: `${tier.tier}-${tier.detail}` },
                  React.createElement("td", null, tier.tier),
                  React.createElement(
                    "td",
                    null,
                    React.createElement(
                      "span",
                      { className: `badge status-${tier.ok ? "ok" : "fail"}` },
                      tier.ok ? "ok" : "fail",
                    ),
                  ),
                  React.createElement("td", { className: "muted" }, tier.detail),
                ),
              ),
            ),
          ),
          React.createElement(
            "details",
            null,
            React.createElement("summary", { className: "muted" }, "查看验证环境输出（已脱敏）"),
            React.createElement("pre", { className: "diff" }, evidence.sanitized_output || "(空)"),
          ),
        ),
  );
}

export function TaskHeader({ detail }: { detail: ReviewDetail }): React.ReactElement {
  const summary = detail.task.summary ?? {};
  const modeName = detail.task.mode === "a2a" ? "A2A 多 Agent 协作" : detail.task.mode === "single" ? "单 Agent 快速审查" : "离线规则扫描";
  const stateText: Record<string, string> = {
    DRAFT: "等待开始",
    REVIEWING: "正在审查",
    REVIEWED: "审查完成，等待你的下一步操作",
    FIXING: "修复 Agent 正在生成建议",
    TESTING: "验证 Agent 正在检查修复结果",
    PENDING_APPROVAL: "等待人工确认",
    MERGED: "已合并",
    NEEDS_HUMAN: "需要你提供更多信息",
    REJECTED: "已结束",
    FAILED: "执行失败",
  };
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement(
      "div",
      { className: "row" },
      React.createElement("h2", { style: { flex: 1 } }, "总任务进度"),
      React.createElement(StatusBadge, { status: detail.task.status }),
      React.createElement("span", { className: "badge a2a-badge" }, modeName),
    ),
    React.createElement("div", { className: "task-state" }, stateText[detail.task.status] ?? detail.task.status),
    React.createElement(
      "div",
      { className: "hint" },
      detail.task.mode === "a2a"
        ? "A2A 已将本次审查拆分给多个专职 Agent 协作完成，下面可查看每一步的处理结果。"
        : "本次任务由一个审查流程处理；切换到 A2A 模式可让多个 Agent 分工协作。",
    ),
    React.createElement(KeyValue, {
      label: "审查内容",
      value: `${detail.task.input_type === "zip" ? "项目 ZIP" : "Python 代码"} · 阅读范围：${detail.task.context_policy}`,
    }),
    React.createElement(KeyValue, {
      label: "当前步骤",
      value: stateText[detail.task.status] ?? detail.task.current_step ?? "处理中",
    }),
    React.createElement(KeyValue, {
      label: "发现的问题",
      value: String(summary.findings ?? detail.comments.length),
    }),
    React.createElement(KeyValue, {
      label: "整体风险",
      value: severityText[String(summary.risk_level ?? "")] ?? String(summary.risk_level ?? "暂未评估"),
    }),
    React.createElement(KeyValue, {
      label: "可能受影响的文件",
      value: (summary.affected_files as string[] | undefined)?.join(", ") || "暂未评估",
    }),
    React.createElement(KeyValue, {
      label: "可尝试自动修复",
      value: `${summary.auto_fixable ?? detail.comments.filter((item) => item.auto_fixable).length} 个`,
    }),
    detail.task.needs_human_from
      ? React.createElement(KeyValue, { label: "需要人工处理的位置", value: detail.task.needs_human_from })
      : null,
    detail.task.error_code
      ? React.createElement(
        "div",
          { className: "severity-critical" },
          `任务遇到问题：${detail.task.error_message ?? detail.task.error_code}`,
        )
      : null,
    React.createElement(TaskTimeline, {
      children: detail.child_tasks,
      audit: detail.audit_tail,
    }),
    React.createElement(
      "details",
      { className: "technical-details" },
      React.createElement("summary", { className: "muted" }, "查看任务技术详情"),
      React.createElement(KeyValue, { label: "任务 ID", value: detail.task.id }),
      React.createElement(KeyValue, { label: "trace_id", value: detail.task.trace_id }),
      React.createElement(KeyValue, { label: "版本号", value: detail.task.state_version }),
      React.createElement(KeyValue, { label: "base_commit", value: detail.task.base_commit }),
    ),
  );
}

/** 只有任务需要补充信息时才显示人工恢复入口（仅管理员可提交）。 */
export function ResumePanel({
  detail,
  canResume,
  busy,
  onResume,
}: {
  detail: ReviewDetail;
  canResume: boolean;
  busy: boolean;
  onResume: (targetStatus: string, reason: string, expectedVersion: number) => void;
}): React.ReactElement {
  const [target, setTarget] = React.useState("REVIEWING");
  const [reason, setReason] = React.useState("补充上下文后继续评审");
  if (detail.task.status !== "NEEDS_HUMAN") {
    return React.createElement(
      "div",
      { className: "panel muted" },
      "当前任务无需你处理。只有 Agent 缺少必要信息或执行异常时，才会在这里提示你继续操作。",
    );
  }
  const failure = detail.child_tasks.filter((child) => child.error_code);
  return React.createElement(
    "div",
    { className: "panel error-box" },
    React.createElement("h2", { style: { margin: 0 } }, "需要你补充信息后继续"),
    React.createElement(KeyValue, {
      label: "从哪个步骤开始需要补充",
      value: detail.task.needs_human_from ?? "-",
    }),
    React.createElement(KeyValue, { label: "错误编号", value: detail.task.error_code ?? "-" }),
    React.createElement(KeyValue, {
      label: "错误说明",
      value: detail.task.error_message ?? "-",
    }),
    failure.length
      ? React.createElement(
          "div",
          { className: "mono muted" },
          failure
            .map((child) => `${child.task_type}: ${child.error_code} ${child.error_message ?? ""}`)
            .join("\n"),
        )
      : null,
    React.createElement("label", null, "继续到哪一步（仅管理员可操作）"),
    React.createElement(
      "select",
      {
        value: target,
        onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setTarget(e.target.value),
      },
      React.createElement("option", { value: "REVIEWING" }, "重新开始审查"),
      React.createElement("option", { value: "REVIEWED" }, "直接结束审查"),
      React.createElement("option", { value: "REJECTED" }, "结束并拒绝本次任务"),
    ),
    React.createElement("label", null, "恢复原因（必填）"),
    React.createElement("input", {
      value: reason,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => setReason(e.target.value),
    }),
    React.createElement(
      "div",
      { className: "row", style: { marginTop: 8 } },
      React.createElement(
        "button",
        {
          className: "primary",
          disabled: busy || !canResume || !reason.trim(),
          title: canResume ? "" : "仅管理员角色可恢复任务",
          onClick: () => onResume(target, reason, detail.task.state_version),
        },
        "提交人工恢复",
      ),
      React.createElement(
        "span",
        { className: "hint" },
        `当前任务版本：${detail.task.state_version}。任务状态发生变化时会要求刷新后再提交。`,
      ),
    ),
  );
}
