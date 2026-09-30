/** Inbound webhook relay — mirrors webhook_relay.py.
 *  POST -> auth (custom delivery headers, timing-safe) -> validate -> dedupe
 *       -> screenInbound (SignalReceived) -> enforce -> handler. */

import { timingSafeEqual } from "node:crypto";
import type { EvaluationResult } from "./core.ts";
import {
  AgentMailBlockedError,
  AgentMailHaltedError,
  ApprovalExpiredError,
  ApprovalRejectedError,
  ApprovalTimeoutError,
  GovernanceAPIError,
  GuardrailsValidationError,
} from "./errors.ts";
import type { MailGovernor } from "./governor.ts";

export const CONTENT_EVENTS = new Set([
  "message.received", "message.received.spam", "message.received.blocked", "message.received.unauthenticated",
]);
export const STATUS_EVENTS = new Set([
  "message.sent", "message.delivered", "message.bounced", "message.complained", "message.rejected", "domain.verified",
]);

export interface RelayResponse {
  status: number;
  body: Record<string, unknown>;
}

export interface GovernedInbound {
  event: Record<string, any>;
  fields: Record<string, unknown>;
  result: EvaluationResult;
}

function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  if (ab.length !== bb.length) return false;
  return timingSafeEqual(ab, bb);
}

/** Constant-time header check; values may be lists for rotation windows. */
export class WebhookAuth {
  private required: Record<string, string[]>;

  constructor(requiredHeaders?: Record<string, string | string[]>, opts?: { secret?: string | string[] }) {
    let headers = requiredHeaders;
    if (opts?.secret) {
      const secrets = Array.isArray(opts.secret) ? opts.secret : [opts.secret];
      headers = { authorization: secrets.map((s) => `Bearer ${s}`) };
    }
    this.required = {};
    for (const [k, v] of Object.entries(headers ?? {}))
      this.required[k.toLowerCase()] = Array.isArray(v) ? v : [v];
  }

  check(headers: Record<string, string>): boolean {
    const lower: Record<string, string> = {};
    for (const [k, v] of Object.entries(headers)) lower[k.toLowerCase()] = v;
    for (const [k, accepted] of Object.entries(this.required)) {
      const got = lower[k] ?? "";
      if (!accepted.some((a) => safeEqual(got, a))) return false;
    }
    return Object.keys(this.required).length > 0;
  }
}

export class MemoryDedupe {
  private seen = new Map<string, number>();
  constructor(private ttlSeconds = 86400) {}
  async seenOrAdd(id: string): Promise<boolean> {
    const now = Date.now() / 1000;
    for (const [k, exp] of this.seen) if (exp < now) this.seen.delete(k);
    if (this.seen.has(id)) return true;
    this.seen.set(id, now + this.ttlSeconds);
    return false;
  }
}

export class InboundRelay {
  constructor(
    public governor: MailGovernor,
    public opts: {
      auth: WebhookAuth;
      handler?: (inbound: GovernedInbound) => unknown | Promise<unknown>;
      dedupe?: MemoryDedupe;
      onBlocked?: (event: Record<string, any>, err: Error) => unknown;
    },
  ) {}

  private async blocked(event: Record<string, any>, e: Error, reason: string): Promise<RelayResponse> {
    try {
      await this.opts.onBlocked?.(event, e);
    } catch {
      /* the block already stands */
    }
    return { status: 200, body: { status: "blocked", reason } };
  }

  private validate(headers: Record<string, string>, body: string | Buffer): Record<string, any> | RelayResponse {
    if (!this.opts.auth.check(headers)) return { status: 401, body: { error: "unauthorized" } };
    let event: any;
    try {
      event = JSON.parse(typeof body === "string" ? body : body.toString("utf8"));
    } catch {
      return { status: 400, body: { error: "invalid JSON" } };
    }
    if (!event || typeof event !== "object" || typeof event.event_type !== "string")
      return { status: 400, body: { error: "missing event_type" } };
    const et = event.event_type;
    if (CONTENT_EVENTS.has(et) || STATUS_EVENTS.has(et)) {
      // only content events carry `message`; status events use send/delivery/bounce/...
      if (CONTENT_EVENTS.has(et) && (typeof event.message !== "object" || event.message === null))
        return { status: 400, body: { error: `${et} requires a message object` } };
      return event;
    }
    return { status: 400, body: { error: `unknown event_type ${et}` } };
  }

  async handle(headers: Record<string, string>, body: string | Buffer): Promise<RelayResponse> {
    const validated = this.validate(headers, body);
    if ("status" in validated && "body" in validated && !("event_type" in validated)) return validated as RelayResponse;
    const event = validated as Record<string, any>;

    const eventId = event.event_id;
    if (typeof eventId === "string" && eventId) {
      if (await (this.opts.dedupe ?? new MemoryDedupe()).seenOrAdd(eventId))
        return { status: 200, body: { status: "duplicate" } };
    }

    const enforce = CONTENT_EVENTS.has(event.event_type);
    let result: EvaluationResult;
    let fields: Record<string, unknown>;
    try {
      [result, fields] = await this.governor.screenInbound(event, { enforce });
    } catch (e) {
      if (
        e instanceof AgentMailBlockedError || e instanceof AgentMailHaltedError ||
        e instanceof GuardrailsValidationError
      ) return this.blocked(event, e as Error, String(e));
      if (e instanceof ApprovalRejectedError || e instanceof ApprovalExpiredError || e instanceof ApprovalTimeoutError)
        return this.blocked(event, e as Error, `approval: ${e}`);
      if (e instanceof GovernanceAPIError)
        return { status: 503, body: { error: `governance unavailable: ${e}` } };
      throw e;
    }

    if (!enforce) return { status: 200, body: { status: "recorded" } };

    const inbound: GovernedInbound = { event: mergeRedaction(event, fields), fields, result };
    try {
      await this.opts.handler?.(inbound);
    } catch {
      return { status: 500, body: { error: "handler failed" } };
    }
    return { status: 200, body: { status: "delivered", verdict: result.verdict } };
  }
}

function mergeRedaction(event: Record<string, any>, fields: Record<string, unknown>): Record<string, any> {
  const message = event.message;
  if (!message || typeof message !== "object") return event;
  const merged = { ...message };
  for (const [k, v] of Object.entries(fields)) if (k in merged) merged[k] = v;
  return { ...event, message: merged };
}
