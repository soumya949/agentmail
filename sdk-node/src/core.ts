/** Minimal OpenBox Core client — the signed-request wire contract of
 *  openbox_core (v1 endpoints), reimplemented for TypeScript since no
 *  TypeScript base SDK exists yet.
 *
 *  Wire:
 *    POST {apiUrl}/api/v1/governance/evaluate   flat event envelope
 *    POST {apiUrl}/api/v1/governance/approval   {workflow_id, run_id, activity_id}
 *    GET  {apiUrl}/api/v1/auth/validate         key check
 *
 *  Signed requests add X-OpenBox-Agent-{DID,Timestamp,Nonce,Signature} +
 *  X-OpenBox-Body-SHA256 over  METHOD\npath\ntimestamp\nnonce\nbody_sha256
 *  signed with the base64-encoded 32-byte Ed25519 seed.
 */

import { createHash, createPrivateKey, randomBytes, sign } from "node:crypto";
import {
  ApprovalExpiredError,
  ApprovalRejectedError,
  ApprovalTimeoutError,
  GovernanceAPIError,
  OpenBoxAuthError,
  OpenBoxConfigError,
} from "./errors.ts";

const EVALUATE_PATH = "/api/v1/governance/evaluate";
const APPROVAL_PATH = "/api/v1/governance/approval";
const AUTH_VALIDATE_PATH = "/api/v1/auth/validate";
export const EVENT_SOURCE = "agentmail-telemetry";
export const SDK_IDENTIFIER = "openbox-agentmail-node-v0.1.0";

const PKCS8_PREFIX = Buffer.from("302e020100300506032b657004220420", "hex");

export type Verdict = "allow" | "constrain" | "require_approval" | "block" | "halt";

export interface EvaluationResult {
  verdict: Verdict;
  reason: string | null;
  policyId: string | null;
  approvalId: string | null;
  guardrails: {
    validation_passed: boolean;
    input_type?: string | null;
    redacted_input?: unknown;
    reasons?: Array<{ reason?: string } | string>;
  } | null;
  constraints: Array<Record<string, unknown>> | null;
  fallbackUsed: boolean;
  raw: Record<string, unknown>;
}

function parseVerdict(raw: unknown): Verdict {
  const v = String(raw ?? "").toLowerCase();
  if (v === "deny") return "block";
  if (["constrain", "require_approval", "block", "halt", "allow"].includes(v)) return v as Verdict;
  return "allow"; // unknown verdict strings parse to ALLOW (Core contract)
}

export function parseEvaluation(data: Record<string, any>): EvaluationResult {
  const g = data.guardrails ?? data.guardrails_result ?? null;
  return {
    verdict: parseVerdict(data.verdict ?? data.action ?? "continue"),
    reason: data.reason ?? null,
    policyId: data.policy_id ?? null,
    approvalId: data.approval_id ?? null,
    guardrails: g
      ? {
          validation_passed: g.validation_passed !== false,
          input_type: g.input_type ?? null,
          redacted_input: g.redacted_input ?? null,
          reasons: g.reasons ?? [],
        }
      : null,
    constraints: data.constraints ?? null,
    fallbackUsed: Boolean(data.fallback_used),
    raw: data,
  };
}

export interface ApprovalResult {
  allowShaped: boolean;
  expired: boolean;
  reason: string | null;
}

export function parseApproval(data: Record<string, any>): ApprovalResult {
  const source = typeof data.action === "string" && data.action.trim() ? data.action : data.verdict;
  const v = parseVerdict(source);
  return { allowShaped: v === "allow", expired: Boolean(data.expired), reason: data.reason ?? null };
}

export interface OpenBoxOptions {
  apiUrl: string;
  apiKey: string;
  agentDid?: string | null;
  agentPrivateKey?: string | null;
  onApiError?: "fail_closed" | "fail_open";
  timeoutMs?: number;
  /** Test hook: replace fetch. */
  fetchImpl?: typeof fetch;
}

function loadSeed(privateKeyB64: string): ReturnType<typeof createPrivateKey> {
  let seed: Buffer;
  try {
    seed = Buffer.from(privateKeyB64, "base64");
  } catch {
    throw new OpenBoxConfigError("Invalid agent private key: not valid base64 (key bytes not shown).");
  }
  if (seed.length !== 32)
    throw new OpenBoxConfigError(`Invalid agent private key: expected 32-byte Ed25519 seed, got ${seed.length} (key bytes not shown).`);
  const der = Buffer.concat([PKCS8_PREFIX, seed]);
  return createPrivateKey({ key: der, format: "der", type: "pkcs8" });
}

export class OpenBoxClient {
  readonly apiUrl: string;
  readonly apiKey: string;
  readonly onApiError: "fail_closed" | "fail_open";
  readonly timeoutMs: number;
  private _key: ReturnType<typeof createPrivateKey> | null = null;
  private _did: string | null = null;
  private _fetch: typeof fetch;

  constructor(opts: OpenBoxOptions) {
    if (!opts.apiUrl) throw new OpenBoxConfigError("api_url is required");
    if (!opts.apiKey) throw new OpenBoxConfigError("api_key is required");
    this.apiUrl = opts.apiUrl.replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.onApiError = opts.onApiError ?? "fail_closed";
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    if ((opts.agentDid && !opts.agentPrivateKey) || (!opts.agentDid && opts.agentPrivateKey))
      throw new OpenBoxConfigError("agent_did and agent_private_key must be provided together");
    if (opts.agentDid && opts.agentPrivateKey) {
      if (!/^did:aip:[0-9a-f-]{36}$/i.test(opts.agentDid))
        throw new OpenBoxConfigError(`Invalid agent DID format: ${opts.agentDid}`);
      this._did = opts.agentDid;
      this._key = loadSeed(opts.agentPrivateKey);
    }
    this._fetch = opts.fetchImpl ?? fetch;
  }

  private _headers(method: string, path: string, body: Buffer): Record<string, string> {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.apiKey}`,
      "User-Agent": `OpenBox-SDK/${SDK_IDENTIFIER}`,
      "X-OpenBox-SDK-Version": SDK_IDENTIFIER,
      "Content-Type": "application/json",
    };
    if (this._key && this._did) {
      const bodySha256 = createHash("sha256").update(body).digest("hex");
      const timestamp = new Date().toISOString().replace(/Z$/, "+00:00");
      const nonce = randomBytes(24).toString("base64url");
      const canonical = [method.toUpperCase(), path, timestamp, nonce, bodySha256].join("\n");
      const signature = sign(null, Buffer.from(canonical, "utf8"), this._key).toString("base64");
      headers["X-OpenBox-Agent-DID"] = this._did;
      headers["X-OpenBox-Agent-Timestamp"] = timestamp;
      headers["X-OpenBox-Agent-Nonce"] = nonce;
      headers["X-OpenBox-Agent-Signature"] = signature;
      headers["X-OpenBox-Body-SHA256"] = bodySha256;
    }
    return headers;
  }

  private async _post(path: string, payload: Record<string, unknown>): Promise<{ status: number; data: any }> {
    const body = Buffer.from(JSON.stringify(payload), "utf8"); // compact — hashed bytes = sent bytes
    const resp = await this._fetch(`${this.apiUrl}${path}`, {
      method: "POST",
      headers: this._headers("POST", path, body),
      body,
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (resp.status === 401 || resp.status === 403) {
      throw new OpenBoxAuthError(`OpenBox auth failed (HTTP ${resp.status})`);
    }
    let data: any = null;
    try {
      data = await resp.json();
    } catch {
      data = {};
    }
    return { status: resp.status, data };
  }

  /** POST an event envelope. Network failures honour on_api_error. */
  async evaluate(event: Record<string, unknown>): Promise<EvaluationResult> {
    let out: { status: number; data: any };
    try {
      out = await this._post(EVALUATE_PATH, event);
    } catch (e) {
      if (e instanceof OpenBoxAuthError) throw e;
      if (this.onApiError === "fail_closed") throw new GovernanceAPIError(`Governance API unreachable: ${e}`);
      return parseEvaluation({ verdict: "allow", fallback_used: true, reason: String(e) });
    }
    if (out.status >= 400) {
      if (this.onApiError === "fail_closed") throw new GovernanceAPIError(`Governance API error: HTTP ${out.status}`);
      return parseEvaluation({ verdict: "allow", fallback_used: true, reason: `HTTP ${out.status}` });
    }
    return parseEvaluation(out.data ?? {});
  }

  async pollApprovalOnce(workflowId: string, runId: string, activityId: string): Promise<ApprovalResult | null> {
    try {
      const out = await this._post(APPROVAL_PATH, {
        workflow_id: workflowId,
        run_id: runId,
        activity_id: activityId,
      });
      if (out.status >= 400) return null;
      const r = parseApproval(out.data ?? {});
      if (!r.expired && !r.allowShaped && !(out.data?.action || out.data?.verdict)) return null; // still pending
      return r;
    } catch (e) {
      if (e instanceof OpenBoxAuthError) throw e;
      return null; // transport error => still pending
    }
  }

  async waitForApproval(
    workflowId: string,
    runId: string,
    activityId: string,
    { pollIntervalMs = 1000, maxWaitMs = 300_000 }: { pollIntervalMs?: number; maxWaitMs?: number | null } = {},
  ): Promise<ApprovalResult> {
    const deadline = maxWaitMs == null ? Infinity : Date.now() + maxWaitMs;
    for (;;) {
      const r = await this.pollApprovalOnce(workflowId, runId, activityId);
      if (r) {
        if (r.allowShaped) return r;
        if (r.expired) throw new ApprovalExpiredError(r.reason ?? "Approval window expired");
        throw new ApprovalRejectedError(r.reason ?? "Approval rejected");
      }
      if (Date.now() >= deadline) throw new ApprovalTimeoutError("Approval wait timed out");
      await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
    }
  }

  async validateApiKey(): Promise<void> {
    const body = Buffer.alloc(0);
    const resp = await this._fetch(`${this.apiUrl}${AUTH_VALIDATE_PATH}`, {
      headers: this._headers("GET", AUTH_VALIDATE_PATH, body),
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!resp.ok) throw new OpenBoxAuthError(`OpenBox API key validation failed (HTTP ${resp.status})`);
  }
}

export function resolveOpenBoxConfig(env: Record<string, string | undefined> = process.env): OpenBoxOptions {
  const get = (...names: string[]) => names.map((n) => env[n]).find((v) => v);
  const apiUrl = get("OPENBOX_AGENTMAIL_API_URL", "OPENBOX_API_URL", "OPENBOX_URL");
  const apiKey = get("OPENBOX_AGENTMAIL_API_KEY", "OPENBOX_API_KEY");
  const onApiError = (get("OPENBOX_AGENTMAIL_ON_API_ERROR", "OPENBOX_ON_API_ERROR") ?? "fail_closed") as "fail_closed" | "fail_open";
  return {
    apiUrl: apiUrl ?? "",
    apiKey: apiKey ?? "",
    agentDid: get("OPENBOX_AGENTMAIL_AGENT_DID", "OPENBOX_AGENT_DID", "OPENBOX_AGENTMAIL_DID", "OPENBOX_DID"),
    agentPrivateKey: get("OPENBOX_AGENTMAIL_AGENT_PRIVATE_KEY", "OPENBOX_AGENT_PRIVATE_KEY", "OPENBOX_AGENTMAIL_PRIVATE_KEY", "OPENBOX_PRIVATE_KEY"),
    onApiError,
  };
}
