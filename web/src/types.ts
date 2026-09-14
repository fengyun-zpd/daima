/** CodePilot API 响应类型（与 apps/api/schemas.py 对齐）。 */

export type ParentStatus =
  | "DRAFT"
  | "REVIEWING"
  | "REVIEWED"
  | "FIXING"
  | "TESTING"
  | "PENDING_APPROVAL"
  | "MERGED"
  | "NEEDS_HUMAN"
  | "REJECTED"
  | "FAILED";

export type RunMode = "single" | "a2a" | "offline";
export type ActorRole = "developer" | "approver" | "admin";

export interface ErrorPayload {
  code: string;
  message: string;
  trace_id?: string | null;
  details?: Record<string, unknown> | null;
}

export interface ChildTask {
  id: string;
  agent_id: string;
  task_type: string;
  status: string;
  attempt: number;
  max_attempts: number;
  deadline: string;
  state_version: number;
  correlation_id: string;
  transport: string;
  error_code?: string | null;
  error_message?: string | null;
  duration_ms?: number | null;
  created_at: string;
  completed_at?: string | null;
}

export interface ArtifactSummary {
  id: string;
  task_id: string;
  artifact_type: string;
  schema_version: string;
  content_hash: string;
  size_bytes: number;
  validated: boolean;
  validation_error?: string | null;
  created_at: string;
}

export interface Comment {
  id: string;
  file: string;
  line: number;
  rule_id: string;
  cwe: string;
  severity: string;
  confidence: number;
  confidence_level: string;
  message: string;
  evidence: string;
  suggestion: string;
  auto_fixable: boolean;
  fix_category?: string | null;
  impact_scope: string;
  citations: string[];
}

export interface ReviewTask {
  id: string;
  trace_id: string;
  mode: string;
  status: ParentStatus;
  state_version: number;
  owner: string;
  actor_id: string;
  input_type: string;
  base_commit: string;
  context_policy: string;
  input_summary: Record<string, unknown>;
  summary: Record<string, unknown>;
  current_step?: string | null;
  needs_human_from?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  idempotency_key: string;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

export interface AuditTailItem {
  id: string;
  event_type: string;
  agent_id?: string | null;
  state_version?: number | null;
  error_code?: string | null;
  created_at: string;
}

export interface ReviewDetail {
  task: ReviewTask;
  child_tasks: ChildTask[];
  artifacts: ArtifactSummary[];
  comments: Comment[];
  audit_tail: AuditTailItem[];
}

/** 补丁状态（与 domain/enums.py::PatchStatus 对齐）。 */
export type PatchStatus =
  | "candidate"
  | "rejected"
  | "verified"
  | "pending_approval"
  | "approved"
  | "applied"
  | "rolled_back";

export interface Patch {
  id: string;
  task_id: string;
  patch_version: number;
  patch_hash: string;
  status: PatchStatus | string;
  fix_category: string;
  target_branch: string;
  changed_files: string[];
  changed_functions: string[];
  scope_drift: boolean;
  scope_drift_files: string[];
  wide_impact: boolean;
  coverage_before?: number | null;
  coverage_after?: number | null;
  coverage_delta?: number | null;
  test_gap: boolean;
  sandbox_run_id?: string | null;
  diff: string;
  approvals: Array<{
    id: string;
    decided_by: string;
    decided_role: string;
    decision: string;
    reason: string;
    patch_version: number;
    created_at: string;
  }>;
}

export interface AuditEvent {
  id: string;
  trace_id?: string | null;
  task_id?: string | null;
  parent_task_id?: string | null;
  agent_id?: string | null;
  actor_id: string;
  actor_role: string;
  action: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  state_version?: number | null;
  before_state: Record<string, unknown>;
  after_state: Record<string, unknown>;
  error_code?: string | null;
  duration_ms?: number | null;
  created_at: string;
}

export interface ReadyStatus {
  status: string;
  database: string;
  agents: string[];
  tools: string[];
  recoverable_tasks: number;
  /** 实际传输层：`inprocess`（单进程/未配置 A2A 端点）或 `http`（拆分形态走 HTTP/SSE）。 */
  transport: "inprocess" | "http" | string;
}

export interface ImpactSummaryView {
  changed_symbols: string[];
  direct_callers: string[];
  direct_callees: string[];
  affected_files: string[];
  risk_level: string;
  uncertain: boolean;
}

export interface VerifyEvidenceView {
  patch_version: number;
  sandbox_run_id: string;
  exit_code: number;
  apply_check: { ok: boolean; command: string; message: string };
  pre_scan: { ok: boolean; blocked_patterns: string[] };
  lint: { ok: boolean; tool: string; issue_count: number; summary: string };
  unit_tests: { ok: boolean; passed: number; failed: number; errors: number; total: number };
  regression: { ok: boolean; baseline_failed_total: number; baseline_failed_fixed: number; pass_rate: number };
  coverage: { before: number | null; after: number | null; delta: number | null; source: string };
  test_gap: boolean;
  scope_drift: boolean;
  evidence_sufficient: boolean;
  tiers: Array<{ tier: string; ok: boolean; detail: string }>;
  sanitized_output: string;
}
