/** activity_input / activity_output / inbound signal builders.
 *  Byte-compatible with sdk/src/openbox_agentmail/contracts.py — the shared
 *  fixtures under sdk/tests/policy_fixtures assert equivalence. */

import { createHash } from "node:crypto";
import { ActionClass, type ActionSpec } from "./catalog.ts";
import { ContractError } from "./errors.ts";

const RECIPIENT_FIELDS = ["to", "cc", "bcc"] as const;
const CONTENT_FIELDS = ["subject", "text", "html"] as const;
const AUTH_HEADER_KEYS = new Set([
  "authentication-results", "received-spf", "dkim-signature", "arc-authentication-results",
]);

export interface AgentMailSettings {
  agentName?: string;
  workflowType?: string;
  inboxIds?: Set<string> | string[] | null;
  podId?: string | null;
  internalDomains?: Set<string> | string[];
  contentMode?: "full" | "metadata_only";
  attachmentScan?: boolean;
  readOnApiError?: "fail_open" | "fail_closed";
  allowFallbackForWrites?: boolean;
  approvalMode?: "wait" | "draft";
  surface?: string;
}

export const toSnakeKey = (k: string) => k.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
export const toCamelKey = (k: string) => k.replace(/_([a-z0-9])/g, (_m, c: string) => c.toUpperCase());

/** agentmail-node models are camelCase; the OpenBox wire (shared with the
 *  Python SDK) is snake_case. */
export function snakeKeysDeep(v: any): any {
  if (Array.isArray(v)) return v.map(snakeKeysDeep);
  if (v && typeof v === "object" && !(v instanceof Uint8Array) && !(v instanceof Date)) {
    const out: Record<string, unknown> = {};
    for (const [k, x] of Object.entries(v)) out[toSnakeKey(k)] = snakeKeysDeep(x);
    return out;
  }
  return v;
}

export function camelKeys(o: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) out[toCamelKey(k)] = v;
  return out;
}

export function domainOf(address: string): string {
  let addr = address.trim();
  if (addr.includes("<") && addr.endsWith(">")) addr = addr.slice(addr.lastIndexOf("<") + 1, -1);
  const at = addr.lastIndexOf("@");
  return at < 0 ? "" : addr.slice(at + 1).toLowerCase();
}

function asList(v: unknown): string[] {
  if (v === null || v === undefined) return [];
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return v.map(String);
  return [String(v)];
}

function attachmentMeta(att: any, includeBytes: boolean, maxChars?: number | null): Record<string, unknown> {
  const get = (k: string) => (att && typeof att === "object" ? att[k] : undefined);
  const content = get("content");
  let size: number | undefined;
  if (typeof content === "string") {
    try {
      size = Buffer.from(content, "base64").length;
    } catch {
      size = content.length;
    }
  } else if (content instanceof Uint8Array) size = content.length;
  const meta: Record<string, unknown> = {
    filename: get("filename") ?? null,
    content_type: get("content_type") ?? null,
    size: size ?? get("size") ?? null,
    attachment_id: get("attachment_id") ?? null,
  };
  if (includeBytes && content !== undefined && content !== null) {
    let encoded = typeof content === "string" ? content : Buffer.from(content).toString("base64");
    if (maxChars != null && encoded.length > maxChars) {
      encoded = encoded.slice(0, maxChars);
      meta.truncated = true;
    }
    meta.content = encoded;
  }
  return meta;
}

function contentHash(args: Record<string, unknown>): string {
  const h = createHash("sha256");
  for (const f of CONTENT_FIELDS) {
    const v = args[f];
    if (typeof v === "string") {
      h.update(f);
      h.update(v);
    }
  }
  return h.digest("hex");
}

function validate(spec: ActionSpec, args: Record<string, unknown>): void {
  const missing: string[] = [];
  if (["messages", "drafts", "threads"].includes(spec.resource) && !args.inbox_id) missing.push("inbox_id");
  if (spec.action === "send_message" && !RECIPIENT_FIELDS.some((f) => asList(args[f]).length)) missing.push("to|cc|bcc");
  if (["reply", "reply_all", "forward"].includes(spec.method) && !args.message_id) missing.push("message_id");
  if (spec.action === "send_draft" && !args.draft_id) missing.push("draft_id");
  if (missing.length)
    throw new ContractError(`${spec.activityType}: missing required fields ${missing.join(",")}`, "AGENTMAIL_INPUT_MISSING_FIELDS", { missing, activity_type: spec.activityType });
}

export function buildActivityInput(
  spec: ActionSpec,
  args: Record<string, unknown>,
  settings: AgentMailSettings,
  maxBodySize?: number | null,
): Record<string, unknown> {
  validate(spec, args);
  const clean: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(args)) if (v !== undefined && k !== "request_options") clean[k] = v;
  const inboxId = clean.inbox_id;
  const inboxIds = settings.inboxIds ? new Set(settings.inboxIds) : null;
  if (inboxIds && inboxId && !inboxIds.has(String(inboxId)))
    throw new ContractError(`inbox ${inboxId} is outside this agent's configured inbox_ids`, "AGENTMAIL_INBOX_OUT_OF_SCOPE", { inbox_id: inboxId });

  const payload: Record<string, unknown> = {
    action: spec.action,
    action_class: spec.actionClass,
    surface: settings.surface ?? "rest",
    inbox_id: inboxId ?? null,
    pod_id: clean.pod_id ?? settings.podId ?? null,
  };

  if (spec.actionClass === ActionClass.SEND || spec.actionClass === ActionClass.DRAFT) {
    const recipients: Record<string, string[]> = {};
    const all: string[] = [];
    for (const f of RECIPIENT_FIELDS) {
      recipients[f] = asList(clean[f]);
      all.push(...recipients[f]);
    }
    const domains = [...new Set(all.filter((r) => r.includes("@")).map(domainOf))].sort();
    const internal = new Set([...(settings.internalDomains ?? [])].map((d) => d.toLowerCase()));
    if (typeof inboxId === "string" && inboxId.includes("@")) internal.add(domainOf(inboxId));
    Object.assign(payload, recipients, {
      reply_to: asList(clean.reply_to),
      recipient_domains: domains,
      recipient_count: all.length,
      external_recipients: domains.some((d) => !internal.has(d)),
      content_sha256: contentHash(clean),
      html_present: Boolean(clean.html),
      attachments: ((clean.attachments as unknown[]) ?? []).map((a) => attachmentMeta(a, settings.attachmentScan ?? false, maxBodySize)),
      labels: asList(clean.labels),
      send_at: clean.send_at ?? null,
      message_id: clean.message_id ?? null,
      draft_id: clean.draft_id ?? null,
      thread_id: clean.thread_id ?? null,
    });
    if ((settings.contentMode ?? "full") === "full") {
      for (const f of CONTENT_FIELDS) payload[f] = clean[f] ?? null;
    } else {
      payload.subject = clean.subject ?? null;
    }
  } else {
    for (const [k, v] of Object.entries(clean)) {
      if (k === "inbox_id" || k === "pod_id") continue;
      payload[k] = v;
    }
  }
  return payload;
}

function dump(obj: any): any {
  if (obj === null || obj === undefined) return {};
  if (obj instanceof Uint8Array) return { bytes: obj.length };
  if (Array.isArray(obj)) return { items: obj.map(dump) };
  if (typeof obj === "object") return snakeKeysDeep(JSON.parse(JSON.stringify(obj)));
  return obj;
}

export function buildActivityOutput(
  spec: ActionSpec,
  result: unknown,
  settings: AgentMailSettings,
  maxBodySize?: number | null,
): Record<string, unknown> {
  let data = dump(result);
  if (spec.actionClass === ActionClass.READ_ATTACHMENT && data && typeof data === "object" && !Array.isArray(data)) {
    if (!settings.attachmentScan) {
      const { content: _c, url: _u, ...rest } = data;
      data = rest;
    } else if (maxBodySize != null && typeof data.content === "string" && data.content.length > maxBodySize) {
      data = { ...data, content: data.content.slice(0, maxBodySize), truncated: true };
    }
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) data = { result: data };
  if (maxBodySize != null) {
    for (const f of ["text", "html", "extracted_text"]) {
      const v = data[f];
      if (typeof v === "string" && v.length > maxBodySize) {
        data[f] = v.slice(0, maxBodySize);
        data.truncated = true;
      }
    }
  }
  if (data.action === undefined) data.action = spec.action;
  return data;
}

export function buildInboundSignal(
  event: Record<string, any>,
  settings: AgentMailSettings,
  maxBodySize?: number | null,
): Record<string, unknown> {
  if (!event.message || typeof event.message !== "object") {
    for (const key of ["send", "delivery", "bounce", "complaint", "reject", "open"]) {
      const body = event[key];
      if (body && typeof body === "object") {
        const fields: Record<string, unknown> = {
          agentmail_event_type: event.event_type ?? null,
          agentmail_event_id: event.event_id ?? null,
          inbox_id: body.inbox_id ?? null,
          thread_id: body.thread_id ?? null,
          message_id: body.message_id ?? null,
          timestamp: body.timestamp ?? null,
          recipients: body.recipients ?? [],
        };
        if (key === "bounce") {
          fields.bounce_type = body.type ?? null;
          fields.bounce_sub_type = body.sub_type ?? null;
        }
        return fields;
      }
    }
  }
  const message: Record<string, any> = event.message ?? {};
  const sender = message.from ?? message.from_ ?? "";
  const headers: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(message.headers ?? {}))
    if (AUTH_HEADER_KEYS.has(k.toLowerCase())) headers[k] = v;

  const fields: Record<string, unknown> = {
    agentmail_event_type: event.event_type ?? null,
    agentmail_event_id: event.event_id ?? null,
    inbox_id: message.inbox_id ?? event.inbox_id ?? null,
    thread_id: message.thread_id ?? null,
    message_id: message.message_id ?? null,
    sender,
    sender_domain: sender ? domainOf(sender) : null,
    reply_to: asList(message.reply_to),
    to: asList(message.to),
    cc: asList(message.cc),
    subject: message.subject ?? null,
    labels: asList(message.labels),
    attachments: (message.attachments ?? []).map((a: unknown) => attachmentMeta(a, false)),
    headers,
    size: message.size ?? null,
  };
  if ((settings.contentMode ?? "full") === "full") {
    for (const f of ["text", "extracted_text"]) {
      let v = message[f];
      if (typeof v === "string" && maxBodySize != null && v.length > maxBodySize) {
        v = v.slice(0, maxBodySize);
        fields.truncated = true;
      }
      fields[f] = v ?? null;
    }
    fields.html_present = Boolean(message.html);
  } else {
    fields.content_sha256 = contentHash(message);
  }
  return fields;
}
