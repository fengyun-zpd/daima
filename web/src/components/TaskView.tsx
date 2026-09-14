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

export function FindingsPanel({ comments }: { comments: Comment[] }): React.ReactElement {
  const sorted = [...comments].sort(
    (a, b) => a.file.localeCompare(b.file) || a.line - b.line,
  );
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement("h2", null, `审查意见（${comments.length}）`),
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
              React.createElement("th", null, "级别"),
              React.createElement("th", null, "规则"),
              React.createElement("th", null, "位置"),
              React.createElement("th", null, "置信度"),
              React.createElement("th", null, "说明"),
              React.createElement("th", null, "可修复"),
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
                  comment.severity,
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
                  `${comment.confidence.toFixed(2)} (${comment.confidence_level})`,
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
                    ? React.createElement("span", { className: "badge" }, comment.fix_category ?? "auto")
                    : "-",
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
    React.createElement("h2", null, "ImpactReport 摘要"),
    impact
      ? React.createElement(
          React.Fragment,
          null,
          React.createElement(KeyValue, { label: "risk_level", value: impact.risk_level }),
          React.createElement(KeyValue, {
            label: "changed_symbols",
            value: impact.changed_symbols.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "direct_callers",
            value: impact.direct_callers.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "direct_callees",
            value: impact.direct_callees.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "affected_files",
            value: impact.affected_files.join(", ") || "-",
          }),
          React.createElement(KeyValue, {
            label: "uncertain",
            value: String(impact.uncertain),
          }),
        )
      : React.createElement("div", { className: "muted" }, "尚未生成 ImpactReport"),
    React.createElement("h2", { style: { marginTop: 12 } }, `Artifact（${artifacts.length}）`),
    React.createElement(
      "table",
      null,
      React.createElement(
        "thead",
        null,
        React.createElement(
          "tr",
          null,
          React.createElement("th", null, "类型"),
          React.createElement("th", null, "Schema"),
          React.createElement("th", null, "内容哈希"),
          React.createElement("th", null, "校验"),
        ),
      ),
      React.createElement(
        "tbody",
        null,
        artifacts.map((artifact) =>
          React.createElement(
            "tr",
            { key: artifact.id },
            React.createElement("td", null, artifact.artifact_type),
            React.createElement("td", { className: "mono" }, artifact.schema_version),
            React.createElement("td", { className: "mono" }, artifact.content_hash.slice(0, 22) + "…"),
            React.createElement(
              "td",
              null,
              React.createElement(
                "span",
                { className: `badge status-${artifact.validated ? "ok" : "fail"}` },
                artifact.validated ? "validated" : "rejected",
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
      React.createElement("h2", { style: { flex: 1 } }, `候选补丁（${patches.length}）`),
      React.createElement(
        "button",
        {
          onClick: onFix,
          disabled: busy || !canFix,
          title: canFix ? "" : fixDisabledReason,
        },
        "生成候选补丁",
      ),
    ),
    !canFix && fixDisabledReason
      ? React.createElement("div", { className: "hint" }, fixDisabledReason)
      : null,
    !latest
      ? React.createElement("div", { className: "muted" }, "尚无候选补丁")
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
          React.createElement("h2", { style: { marginTop: 12 } }, "unified diff"),
          React.createElement("pre", { className: "diff" }, latest.diff),
          React.createElement("h2", { style: { marginTop: 12 } }, "审批记录"),
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
          React.createElement("label", null, "审批原因（拒绝时必填）"),
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
              "批准",
            ),
            React.createElement(
              "button",
              {
                className: "danger",
                disabled: busy || !canDecide,
                onClick: () => onReject(latest, reason),
                title: decideReason,
              },
              "拒绝",
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
    React.createElement("h2", { style: { marginTop: 12 } }, "VerifyEvidence"),
    !evidence
      ? React.createElement("div", { className: "muted" }, "尚无验证证据")
      : React.createElement(
          React.Fragment,
          null,
          React.createElement(KeyValue, {
            label: "sandbox_run_id",
            value: evidence.sandbox_run_id,
          }),
          React.createElement(KeyValue, { label: "exit_code", value: evidence.exit_code }),
          React.createElement(KeyValue, {
            label: "apply_check",
            value: evidence.apply_check.ok ? "ok" : evidence.apply_check.message,
          }),
          React.createElement(KeyValue, { label: "pre_scan", value: evidence.pre_scan.ok ? "ok" : "blocked" }),
          React.createElement(KeyValue, {
            label: "lint",
            value: `${evidence.lint.tool} issues=${evidence.lint.issue_count}`,
          }),
          React.createElement(KeyValue, {
            label: "unit_tests",
            value: `${evidence.unit_tests.passed} passed / ${evidence.unit_tests.failed} failed`,
          }),
          React.createElement(KeyValue, {
            label: "regression",
            value: `${evidence.regression.baseline_failed_fixed}/${evidence.regression.baseline_failed_total}`,
          }),
          React.createElement(KeyValue, {
            label: "coverage",
            value: `${evidence.coverage.before ?? "-"} → ${evidence.coverage.after ?? "-"}`,
          }),
          React.createElement(KeyValue, { label: "test_gap", value: String(evidence.test_gap) }),
          React.createElement(KeyValue, {
            label: "evidence_sufficient",
            value: String(evidence.evidence_sufficient),
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
            React.createElement("summary", { className: "muted" }, "沙箱脱敏输出"),
            React.createElement("pre", { className: "diff" }, evidence.sanitized_output || "(空)"),
          ),
        ),
  );
}

export function TaskHeader({ detail }: { detail: ReviewDetail }): React.ReactElement {
  const summary = detail.task.summary ?? {};
  return React.createElement(
    "div",
    { className: "panel" },
    React.createElement(
      "div",
      { className: "row" },
      React.createElement("h2", { style: { flex: 1 } }, `任务 ${detail.task.id}`),
      React.createElement(StatusBadge, { status: detail.task.status }),
      React.createElement("span", { className: "badge" }, `mode ${detail.task.mode}`),
    ),
    React.createElement(KeyValue, { label: "trace_id", value: detail.task.trace_id }),
    React.createElement(KeyValue, { label: "state_version", value: detail.task.state_version }),
    React.createElement(KeyValue, { label: "owner", value: detail.task.owner }),
    React.createElement(KeyValue, {
      label: "input",
      value: `${detail.task.input_type} @ ${detail.task.base_commit} (${detail.task.context_policy})`,
    }),
    React.createElement(KeyValue, {
      label: "current_step",
      value: detail.task.current_step ?? "-",
    }),
    React.createElement(KeyValue, {
      label: "findings",
      value: String(summary.findings ?? detail.comments.length),
    }),
    React.createElement(KeyValue, {
      label: "risk_level",
      value: String(summary.risk_level ?? "-"),
    }),
    React.createElement(KeyValue, {
      label: "affected_files",
      value: JSON.stringify(summary.affected_files ?? []),
    }),
    React.createElement(KeyValue, {
      label: "auto_fixable",
      value: String(summary.auto_fixable ?? detail.comments.filter((item) => item.auto_fixable).length),
    }),
    React.createElement(KeyValue, {
      label: "wide_impact / human_review_required",
      value: `${summary.wide_impact ?? false} / ${summary.human_review_required ?? false}`,
    }),
    detail.task.needs_human_from
      ? React.createElement(KeyValue, { label: "needs_human_from", value: detail.task.needs_human_from })
      : null,
    detail.task.error_code
      ? React.createElement(
          "div",
          { className: "mono severity-critical" },
          `${detail.task.error_code}: ${detail.task.error_message ?? ""}`,
        )
      : null,
    React.createElement(TaskTimeline, {
      children: detail.child_tasks,
      audit: detail.audit_tail,
    }),
  );
}

/** NEEDS_HUMAN 说明与人工恢复入口（仅 admin 可提交）。 */
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
      `当前状态 ${detail.task.status}，无需人工恢复入口（仅 NEEDS_HUMAN 可恢复）。`,
    );
  }
  const failure = detail.child_tasks.filter((child) => child.error_code);
  return React.createElement(
    "div",
    { className: "panel error-box" },
    React.createElement("h2", { style: { margin: 0 } }, "需要人工介入（NEEDS_HUMAN）"),
    React.createElement(KeyValue, {
      label: "needs_human_from",
      value: detail.task.needs_human_from ?? "-",
    }),
    React.createElement(KeyValue, { label: "error_code", value: detail.task.error_code ?? "-" }),
    React.createElement(KeyValue, {
      label: "error_message",
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
    React.createElement("label", null, "恢复目标状态（仅 admin；永远不能恢复到 MERGED）"),
    React.createElement(
      "select",
      {
        value: target,
        onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setTarget(e.target.value),
      },
      React.createElement("option", { value: "REVIEWING" }, "REVIEWING"),
      React.createElement("option", { value: "REVIEWED" }, "REVIEWED"),
      React.createElement("option", { value: "REJECTED" }, "REJECTED"),
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
          title: canResume ? "" : "仅 admin 角色可恢复任务",
          onClick: () => onResume(target, reason, detail.task.state_version),
        },
        "提交人工恢复",
      ),
      React.createElement(
        "span",
        { className: "hint" },
        `expected_version = ${detail.task.state_version}（乐观锁，版本变化会被拒绝）`,
      ),
    ),
  );
}
