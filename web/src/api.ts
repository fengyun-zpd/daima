/** CodePilot API 客户端：统一注入身份头与幂等键，并把错误转成可展示的对象。 */

import type {
  ActorRole,
  AuditEvent,
  ErrorPayload,
  Patch,
  ReadyStatus,
  ReviewDetail,
  ReviewTask,
} from "./types";

export interface Identity {
  actorId: string;
  displayName?: string;
  role: ActorRole;
}

/** 后端返回的可展示错误：保留 HTTP 状态码，便于页面区分 403 / 409 / 422 / 503。 */
export class ApiError extends Error {
  readonly status: number;
  readonly payload: ErrorPayload;

  constructor(status: number, payload: ErrorPayload) {
    super(`${payload.code}: ${payload.message}`);
    this.status = status;
    this.payload = payload;
  }
}

/** 幂等键的语义说明（页面提示用）。 */
export const IDEMPOTENCY_HINT =
  "同一次用户操作重试会复用同一个 Idempotency-Key；后端返回原结果而不是重复产生副作用。";

const DEFAULT_IDENTITY: Identity = { actorId: "dev-1", role: "developer" };

export function newIdempotencyKey(): string {
  const random = Math.random().toString(36).slice(2, 10);
  const stamp = Date.now().toString(36);
  return `ui-${stamp}-${random}`;
}

/**
 * 用户操作级幂等键管理器。
 *
 * - `keyFor(action)`：同一个 action（如 `fix:task-1`）在成功之前一直返回同一个键，
 *   因此"点了按钮后失败再点一次"是重试而不是新命令；
 * - `clear(action)`：操作成功（或用户主动放弃）后清空，下一次点击才是新命令；
 * - `clearAll()`：切换任务/身份时重置。
 */
export class OperationKeys {
  private keys = new Map<string, string>();

  keyFor(action: string): string {
    const existing = this.keys.get(action);
    if (existing) return existing;
    const created = newIdempotencyKey();
    this.keys.set(action, created);
    return created;
  }

  peek(action: string): string | null {
    return this.keys.get(action) ?? null;
  }

  clear(action: string): void {
    this.keys.delete(action);
  }

  clearAll(): void {
    this.keys.clear();
  }
}

interface WriteOptions {
  /** 显式指定幂等键；不传时自动生成一次性键（适合"每次点击都是新命令"的场景）。 */
  idempotencyKey?: string;
}

export class CodePilotClient {
  constructor(private identity: Identity = DEFAULT_IDENTITY) {}

  withIdentity(identity: Identity): CodePilotClient {
    return new CodePilotClient(identity);
  }

  private headers(write: boolean, idempotencyKey?: string): HeadersInit {
    const headers: Record<string, string> = {
      "X-Actor-Id": this.identity.actorId,
      "X-Actor-Role": this.identity.role,
    };
    if (write) {
      headers["Content-Type"] = "application/json";
      headers["Idempotency-Key"] = idempotencyKey ?? newIdempotencyKey();
    }
    return headers;
  }

  private async request<T>(path: string, init: RequestInit, write: boolean, idempotencyKey?: string): Promise<T> {
    const response = await fetch(path, {
      ...init,
      headers: { ...this.headers(write, idempotencyKey), ...(init.headers ?? {}) },
    });
    const text = await response.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        body = { code: `HTTP_${response.status}`, message: text.slice(0, 300) };
      }
    }
    if (!response.ok) {
      const payload = (body ?? {}) as ErrorPayload;
      throw new ApiError(response.status, {
        code: payload.code ?? `HTTP_${response.status}`,
        message: payload.message ?? response.statusText,
        trace_id: payload.trace_id,
        details: payload.details,
      });
    }
    return body as T;
  }

  readyz(): Promise<ReadyStatus> {
    return this.request<ReadyStatus>("/readyz", { method: "GET" }, false);
  }

  createReview(
    body: {
      input_type: "diff" | "zip";
      content: string;
      base_commit: string;
      context_policy: string;
      mode: string;
    },
    options: WriteOptions = {},
  ): Promise<{ task: ReviewTask; created: boolean; detail_url: string }> {
    return this.request(
      "/api/v1/reviews",
      { method: "POST", body: JSON.stringify(body) },
      true,
      options.idempotencyKey,
    );
  }

  getReview(taskId: string): Promise<ReviewDetail> {
    return this.request<ReviewDetail>(`/api/v1/reviews/${taskId}`, { method: "GET" }, false);
  }

  listReviews(limit = 100): Promise<ReviewTask[]> {
    return this.request<ReviewTask[]>(`/api/v1/reviews?limit=${limit}`, { method: "GET" }, false);
  }

  triggerFix(taskId: string, options: WriteOptions = {}): Promise<{ task_id: string; status: string; patch_id: string | null }> {
    return this.request(
      `/api/v1/reviews/${taskId}/fixes`,
      { method: "POST", body: JSON.stringify({ comment_ids: [] }) },
      true,
      options.idempotencyKey,
    );
  }

  resume(
    taskId: string,
    body: { target_status: string; expected_version: number; reason: string },
    options: WriteOptions = {},
  ): Promise<ReviewTask> {
    return this.request(
      `/api/v1/reviews/${taskId}/resume`,
      { method: "POST", body: JSON.stringify(body) },
      true,
      options.idempotencyKey,
    );
  }

  listPatches(taskId: string): Promise<Patch[]> {
    return this.request<Patch[]>(`/api/v1/reviews/${taskId}/patches`, { method: "GET" }, false);
  }

  getPatch(patchId: string): Promise<Patch> {
    return this.request<Patch>(`/api/v1/fixes/${patchId}`, { method: "GET" }, false);
  }

  approve(
    patchId: string,
    patchVersion: number,
    reason: string,
    options: WriteOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.request(
      `/api/v1/fixes/${patchId}/approval`,
      {
        method: "POST",
        body: JSON.stringify({ decision: "approve", patch_version: patchVersion, reason }),
      },
      true,
      options.idempotencyKey,
    );
  }

  reject(
    patchId: string,
    patchVersion: number,
    reason: string,
    options: WriteOptions = {},
  ): Promise<Record<string, unknown>> {
    return this.request(
      `/api/v1/fixes/${patchId}/approval`,
      {
        method: "POST",
        body: JSON.stringify({ decision: "reject", patch_version: patchVersion, reason }),
      },
      true,
      options.idempotencyKey,
    );
  }

  merge(patchId: string, patchVersion: number, options: WriteOptions = {}): Promise<Record<string, unknown>> {
    return this.request(
      `/api/v1/fixes/${patchId}/merge`,
      { method: "POST", body: JSON.stringify({ patch_version: patchVersion }) },
      true,
      options.idempotencyKey,
    );
  }

  listAudit(params: { taskId?: string; limit?: number }): Promise<AuditEvent[]> {
    const search = new URLSearchParams();
    if (params.taskId) search.set("task_id", params.taskId);
    search.set("limit", String(params.limit ?? 100));
    return this.request<AuditEvent[]>(`/api/v1/audit?${search.toString()}`, { method: "GET" }, false);
  }
}
