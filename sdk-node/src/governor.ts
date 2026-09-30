/** MailGovernor — single choke point, async-only (Node is async-native).
 *  Mirrors sdk/src/openbox_agentmail/governor.py. */

import { randomUUID } from "node:crypto";
import { isWrite, lookup, ActionClass, type ActionSpec } from "./catalog.ts";
import { applyConstraints, ConstraintViolation } from "./constraints.ts";
import { buildActivityInput, buildActivityOutput, buildInboundSignal, camelKeys, type AgentMailSettings } from "./contracts.ts";
import { EVENT_SOURCE, OpenBoxClient, type EvaluationResult } from "./core.ts";
import {
  AgentMailBlockedError,
  AgentMailHaltedError,
  ApprovalExpiredError,
  ApprovalRejectedError,
  ApprovalTimeoutError,
  GovernanceAPIError,
  GuardrailsValidationError,
  OpenBoxConfigError,
} from "./errors.ts";

const SDK_VERSION = "0.1.0";

export interface PendingApproval {
  approvalId: string | null;
  activityId: string;
  workflowId: string;
  runId: string;
  activityType: string;
  inboxId: string | null;
  draftId: string | null;
  createdDraft: boolean;
  reason: string | null;
  policyId: string | null;
}

export interface DraftResolution {
  status: "sent" | "rejected" | "expired" | "failed";
  draftId: string | null;
  activityId: string;
  result?: unknown;
  error?: string;
}

export interface GovernorOptions {
  hitl?: { enabled?: boolean; pollIntervalMs?: number; maxWaitMs?: number | null; skipActivityTypes?: string[] };
  maxBodySize?: number | null;
}

const DRAFT_ARG_KEYS = new Set([
  "inbox_id", "to", "cc", "bcc", "reply_to", "subject", "text", "html",
  "attachments", "labels", "send_at", "thread_id", "headers",
]);

export function draftArgsFromSend(spec: ActionSpec, args: Record<string, unknown>, activityId: string): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(args)) if (DRAFT_ARG_KEYS.has(k) && args[k] !== undefined && args[k] !== null) out[k] = args[k];
  if (spec.method === "reply" || spec.method === "reply_all") {
    out.in_reply_to = args.message_id;
    if (spec.method === "reply_all") out.reply_all = true;
  } else if (spec.method === "forward") {
    out.forward_of = args.message_id;
  }
  out.client_id = activityId;
  return out;
}

export class MailGovernor {
  workflowId: string | null = null;
  runId: string | null = null;
  private _halted = false;
  readonly store: Map<string, PendingApproval> = new Map();

  constructor(
    public client: OpenBoxClient,
    public settings: AgentMailSettings = {},
    public opts: GovernorOptions = {},
    private mailClient: any = null,
  ) {
    const mode = settings.approvalMode ?? "wait";
    if (mode === "draft" && !mailClient)
      throw new OpenBoxConfigError("approvalMode 'draft' requires an AgentMail client to stage drafts");
  }

  get halted(): boolean {
    return this._halted;
  }

  private workflowFields() {
    return {
      workflow_id: this.workflowId ?? "",
      run_id: this.runId ?? "",
      workflow_type: this.settings.workflowType ?? `${this.settings.agentName ?? "AgentMailAgent"} Agent`,
    };
  }

  private baseExtra(inboxId?: string | null) {
    const extra: Record<string, unknown> = {
      agent_name: this.settings.agentName ?? "AgentMailAgent",
      surface: this.settings.surface ?? "rest",
      agentmail_sdk_version: SDK_VERSION,
      openbox_sdk_version: "node-0.1.0",
    };
    if (this.settings.podId) extra.pod_id = this.settings.podId;
    if (inboxId) extra.inbox_id = inboxId;
    return extra;
  }

  async ensureSession(): Promise<void> {
    if (this.workflowId !== null) return;
    this.workflowId = randomUUID();
    this.runId = randomUUID();
    this._halted = false;
    await this.client.evaluate({
      event_type: "WorkflowStarted",
      ...this.workflowFields(),
      source: EVENT_SOURCE,
      ...this.baseExtra(),
    });
  }

  async closeSession(error?: string): Promise<void> {
    if (this.workflowId === null) return;
    const ev = {
      event_type: error && !this._halted ? "WorkflowFailed" : "WorkflowCompleted",
      ...this.workflowFields(),
      source: EVENT_SOURCE,
      ...(error && !this._halted ? { error } : {}),
      ...this.baseExtra(),
      status: this._halted ? "halted" : error ? "failed" : "completed",
    };
    this.workflowId = this.runId = null;
    try {
      await this.client.evaluate(ev);
    } catch {
      /* telemetry must never break close */
    }
  }

  private checkFallback(spec: ActionSpec, r: EvaluationResult) {
    if (!r.fallbackUsed) return;
    if (isWrite(spec.actionClass) && !(this.settings.allowFallbackForWrites ?? false))
      throw new GovernanceAPIError(`OpenBox unreachable and fail_open fallback refused for write ${spec.activityType}`);
    if (!isWrite(spec.actionClass) && (this.settings.readOnApiError ?? "fail_closed") === "fail_closed")
      throw new GovernanceAPIError(`OpenBox unreachable (fail_closed) for ${spec.activityType}`);
  }

  private enforceStarted(spec: ActionSpec, r: EvaluationResult) {
    if (r.verdict === "block" || r.verdict === "halt") {
      if (r.verdict === "halt") this._halted = true;
      if (r.verdict === "halt") throw new AgentMailHaltedError(r.reason ?? "Halted by OpenBox policy", spec.activityType);
      throw new AgentMailBlockedError(r.reason ?? "Blocked by OpenBox policy", spec.activityType, r.policyId, (r.raw as any).patch ?? null);
    }
    if (r.guardrails && !r.guardrails.validation_passed) {
      const reasons = (r.guardrails.reasons ?? []).map((x) => (typeof x === "string" ? x : x.reason ?? "")).filter(Boolean);
      throw new GuardrailsValidationError(reasons.length ? reasons : ["Guardrails validation failed"]);
    }
  }

  private hitlSkips(spec: ActionSpec): boolean {
    const skip = this.opts.hitl?.skipActivityTypes ?? [];
    return Boolean(this.opts.hitl?.enabled && skip.includes(spec.activityType));
  }

  private async emitResume(activityId: string, decision: string, reason: string | null) {
    try {
      await this.client.evaluate({
        event_type: "SignalReceived",
        ...this.workflowFields(),
        signal_name: "approval_resume",
        source: EVENT_SOURCE,
        ...this.baseExtra(),
        activity_id: activityId,
        decision,
        reason,
      });
    } catch {
      /* telemetry */
    }
  }

  private async waitApproval(r: EvaluationResult, activityId: string) {
    let exc: Error | null = null;
    try {
      const hitl = this.opts.hitl ?? {};
      if (!hitl.enabled) throw new ApprovalRejectedError("Approval required but HITL polling is disabled");
      await this.client.waitForApproval(this.workflowId ?? "", this.runId ?? "", activityId, {
        pollIntervalMs: hitl.pollIntervalMs ?? 1000,
        maxWaitMs: hitl.maxWaitMs ?? null,
      });
    } catch (e) {
      exc = e as Error;
      throw e;
    } finally {
      const decision =
        exc === null ? "approved"
        : exc instanceof ApprovalExpiredError ? "expired"
        : exc instanceof ApprovalTimeoutError ? "timeout"
        : "rejected";
      await this.emitResume(activityId, decision, exc?.message ?? null);
    }
  }

  private draftIdOf(draft: any): string | null {
    if (!draft) return null;
    if (typeof draft === "object") return draft.draft_id ?? draft.draftId ?? null;
    return null;
  }

  private async deferToDraft(spec: ActionSpec, execArgs: Record<string, unknown>, r: EvaluationResult, activityId: string): Promise<PendingApproval> {
    const inboxId = (execArgs.inbox_id as string) ?? null;
    let draftId: string | null;
    let created = false;
    if (spec.resource === "drafts" && spec.method === "send") {
      draftId = (execArgs.draft_id as string) ?? null;
    } else {
      const createSpec = lookup(["drafts", "create"]);
      const draft = await this.run(
        createSpec,
        draftArgsFromSend(spec, execArgs, activityId),
        (a) => {
          const { inbox_id, ...body } = a;
          return this.mailClient.inboxes.drafts.create(inbox_id, camelKeys(body));
        },
        { defer: false },
      );
      draftId = this.draftIdOf(draft);
      created = true;
    }
    const pending: PendingApproval = {
      approvalId: r.approvalId,
      activityId,
      workflowId: this.workflowId ?? "",
      runId: this.runId ?? "",
      activityType: spec.activityType,
      inboxId,
      draftId,
      createdDraft: created,
      reason: r.reason,
      policyId: r.policyId,
    };
    this.store.set(pending.activityId, pending);
    return pending;
  }

  /** Resolve a PendingApproval: poll the decision, send or delete the draft. */
  async resolveDraft(pending: PendingApproval): Promise<DraftResolution> {
    let decision = "approved";
    try {
      await this.client.waitForApproval(pending.workflowId, pending.runId, pending.activityId, {
        pollIntervalMs: this.opts.hitl?.pollIntervalMs ?? 1000,
        maxWaitMs: this.opts.hitl?.maxWaitMs ?? null,
      });
    } catch (e) {
      decision = e instanceof ApprovalExpiredError || e instanceof ApprovalTimeoutError ? "expired" : "rejected";
      if (e instanceof GovernanceAPIError) decision = "failed";
    }
    await this.emitResume(pending.activityId, decision, null);
    try {
      if (decision === "approved") {
        const sendSpec = lookup(["drafts", "send"]);
        const result = await this.run(
          sendSpec,
          { inbox_id: pending.inboxId, draft_id: pending.draftId },
          (a) => this.mailClient.inboxes.drafts.send(a.inbox_id, a.draft_id, {}, { idempotencyKey: pending.activityId }),
          { defer: false },
        );
        return { status: "sent", draftId: pending.draftId, activityId: pending.activityId, result };
      }
      if (decision === "failed") return { status: "failed", draftId: pending.draftId, activityId: pending.activityId };
      if (pending.createdDraft && pending.draftId) {
        const delSpec = lookup(["drafts", "delete"]);
        await this.run(delSpec, { inbox_id: pending.inboxId, draft_id: pending.draftId },
          (a) => this.mailClient.inboxes.drafts.delete(a.inbox_id, a.draft_id), { defer: false });
      }
      return { status: decision === "expired" ? "expired" : "rejected", draftId: pending.draftId, activityId: pending.activityId };
    } catch (e) {
      return { status: "failed", draftId: pending.draftId, activityId: pending.activityId, error: String(e) };
    } finally {
      this.store.delete(pending.activityId);
    }
  }

  /** Govern one AgentMail call. Mirrors governor.py::arun. */
  async run(
    spec: ActionSpec,
    args: Record<string, unknown>,
    executor: (args: Record<string, unknown>) => Promise<any>,
    { defer = true }: { defer?: boolean } = {},
  ): Promise<any> {
    if (this.halted) throw new AgentMailHaltedError("Session halted by a previous OpenBox verdict", spec.activityType);
    const activityInput = buildActivityInput(spec, args, this.settings, this.opts.maxBodySize);
    await this.ensureSession();
    const activityId = randomUUID();
    const inboxId = args.inbox_id ? String(args.inbox_id) : null;

    const r1 = await this.client.evaluate({
      event_type: "ActivityStarted",
      ...this.workflowFields(),
      task_queue: inboxId,
      activity_id: activityId,
      activity_type: spec.activityType,
      activity_input: activityInput,
      source: EVENT_SOURCE,
      ...this.baseExtra(inboxId),
    });
    this.enforceStarted(spec, r1);
    this.checkFallback(spec, r1);

    let execArgs = this.redactArgs(args, r1);
    let needsApproval = r1.verdict === "require_approval";
    if (r1.verdict === "constrain" && isWrite(spec.actionClass)) {
      if (!r1.constraints?.length) needsApproval = true;
      else {
        try {
          [execArgs] = applyConstraints(execArgs, r1.constraints);
        } catch (e) {
          if (!(e instanceof ConstraintViolation)) throw e;
          needsApproval = true;
        }
      }
    }

    if (needsApproval) {
      if (defer && (this.settings.approvalMode ?? "wait") === "draft" && spec.actionClass === ActionClass.SEND)
        return this.deferToDraft(spec, execArgs, r1, activityId);
      if (!this.hitlSkips(spec)) await this.waitApproval(r1, activityId);
    }

    let result: any;
    try {
      result = await executor(execArgs);
    } catch (e) {
      try {
        await this.client.evaluate({
          event_type: "ActivityCompleted",
          ...this.workflowFields(),
          task_queue: inboxId,
          activity_id: activityId,
          activity_type: spec.activityType,
          error: String(e),
          source: EVENT_SOURCE,
          ...this.baseExtra(inboxId),
        });
      } catch { /* telemetry */ }
      throw e;
    }

    let r2: EvaluationResult | null = null;
    try {
      r2 = await this.client.evaluate({
        event_type: "ActivityCompleted",
        ...this.workflowFields(),
        task_queue: inboxId,
        activity_id: activityId,
        activity_type: spec.activityType,
        activity_output: buildActivityOutput(spec, result, this.settings, this.opts.maxBodySize),
        source: EVENT_SOURCE,
        ...this.baseExtra(inboxId),
      });
    } catch (e) {
      if (!isWrite(spec.actionClass) && e instanceof GovernanceAPIError) throw e;
    }
    if (r2) {
      if (r2.verdict === "block" || r2.verdict === "halt") {
        if (r2.verdict === "halt") this._halted = true;
        if (!isWrite(spec.actionClass))
          throw new AgentMailBlockedError(r2.reason ?? "Blocked by OpenBox policy", spec.activityType, r2.policyId);
      }
      if (r2.guardrails && !r2.guardrails.validation_passed && !isWrite(spec.actionClass)) {
        const reasons = (r2.guardrails.reasons ?? []).map((x) => (typeof x === "string" ? x : x.reason ?? "")).filter(Boolean);
        throw new GuardrailsValidationError(reasons);
      }
      result = this.redactResult(result, r2);
    }
    return result;
  }

  private redactArgs(args: Record<string, unknown>, r: EvaluationResult): Record<string, unknown> {
    const gr = r.guardrails;
    if (!gr || gr.input_type !== "activity_input" || !gr.redacted_input || typeof gr.redacted_input !== "object") return args;
    const out = { ...args };
    for (const key of ["subject", "text", "html"]) {
      const redacted = (gr.redacted_input as Record<string, unknown>)[key];
      if (key in out && redacted !== undefined) out[key] = redacted;
    }
    return out;
  }

  private redactResult(result: any, r: EvaluationResult): any {
    const gr = r.guardrails;
    if (!gr || gr.input_type !== "activity_output" || !gr.redacted_input || typeof gr.redacted_input !== "object") return result;
    // redacted_input mirrors the snake_case activity_output; the caller holds a camelCase model
    if (result && typeof result === "object") return { ...result, ...camelKeys(gr.redacted_input as Record<string, unknown>) };
    return gr.redacted_input;
  }

  /** Screen an inbound AgentMail event (webhook or websocket payload). */
  async screenInbound(event: Record<string, any>, { enforce = true }: { enforce?: boolean } = {}): Promise<[EvaluationResult, Record<string, unknown>]> {
    if (this.halted && enforce) throw new AgentMailHaltedError("Session halted by a previous OpenBox verdict");
    await this.ensureSession();
    const fields = buildInboundSignal(event, this.settings, this.opts.maxBodySize);
    const name = `agentmail.${String(event.event_type ?? "unknown").replace(/\./g, "_")}`;
    const ev = {
      event_type: "SignalReceived",
      ...this.workflowFields(),
      task_queue: fields.inbox_id ?? null,
      signal_name: name,
      source: EVENT_SOURCE,
      ...this.baseExtra(typeof fields.inbox_id === "string" ? fields.inbox_id : null),
      ...fields,
      enforced: enforce,
    };
    const r = await this.client.evaluate(ev);
    if (!enforce) return [r, fields];
    if (r.verdict === "block" || r.verdict === "halt") {
      if (r.verdict === "halt") this._halted = true;
      throw new AgentMailBlockedError(r.reason ?? "Blocked by OpenBox policy");
    }
    if (r.guardrails && !r.guardrails.validation_passed) {
      const reasons = (r.guardrails.reasons ?? []).map((x) => (typeof x === "string" ? x : x.reason ?? "")).filter(Boolean);
      throw new GuardrailsValidationError(reasons);
    }
    if (r.verdict === "require_approval" || r.verdict === "constrain") {
      await this.client.waitForApproval(this.workflowId ?? "", this.runId ?? "", "", {
        pollIntervalMs: this.opts.hitl?.pollIntervalMs ?? 1000,
        maxWaitMs: this.opts.hitl?.maxWaitMs ?? null,
      });
    }
    if (r.fallbackUsed && (this.settings.readOnApiError ?? "fail_closed") === "fail_closed")
      throw new GovernanceAPIError("OpenBox unreachable (fail_closed) while screening inbound mail");
    if (r.guardrails && (r.guardrails.input_type === "activity_input" || r.guardrails.input_type === "signal") &&
        r.guardrails.redacted_input && typeof r.guardrails.redacted_input === "object") {
      Object.assign(fields, r.guardrails.redacted_input);
    }
    return [r, fields];
  }
}
