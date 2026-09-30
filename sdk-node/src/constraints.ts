/** CONSTRAIN vocabulary — mirrors sdk/src/openbox_agentmail/constraints.py. */

import { domainOf } from "./contracts.ts";

export class ConstraintViolation extends Error {}

function asList(v: unknown): string[] {
  if (v === null || v === undefined) return [];
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return v.map(String);
  return [String(v)];
}

function recipientDomains(args: Record<string, unknown>): Set<string> {
  const out = new Set<string>();
  for (const f of ["to", "cc", "bcc"])
    for (const r of asList(args[f])) if (r.includes("@")) out.add(domainOf(r));
  return out;
}

export function applyConstraints(
  args: Record<string, unknown>,
  constraints: Array<Record<string, unknown>> | null | undefined,
): [Record<string, unknown>, string[]] {
  if (!constraints?.length) return [args, []];
  const out = { ...args };
  const applied: string[] = [];
  for (const c of constraints) {
    const type = String(c?.type ?? "");
    if (type === "max_recipients") {
      const cap = c.count;
      if (typeof cap !== "number") throw new ConstraintViolation("max_recipients needs an integer 'count'");
      const total = ["to", "cc", "bcc"].reduce((n, f) => n + asList(out[f]).length, 0);
      if (total > cap) throw new ConstraintViolation(`recipient count ${total} exceeds max_recipients ${cap}`);
      applied.push(`max_recipients<=${cap}`);
    } else if (type === "allowed_domains") {
      const domains = new Set(((c.domains as string[]) ?? []).map((d) => String(d).toLowerCase()));
      const bad = [...recipientDomains(out)].filter((d) => !domains.has(d));
      if (bad.length) throw new ConstraintViolation(`recipient domains outside allowed set: ${bad.sort()}`);
      applied.push(`allowed_domains=${[...domains].sort()}`);
    } else if (type === "strip_attachments") {
      if (out.attachments) out.attachments = [];
      applied.push("strip_attachments");
    } else if (type === "force_bcc") {
      const address = c.address;
      if (typeof address !== "string" || !address.includes("@")) throw new ConstraintViolation("force_bcc needs an 'address'");
      const bcc = asList(out.bcc);
      if (!bcc.includes(address)) out.bcc = [...bcc, address];
      applied.push(`force_bcc=${address}`);
    } else if (type === "require_approval" || type === "approval") {
      throw new ConstraintViolation("constraint requires human approval");
    } else {
      throw new ConstraintViolation(`unknown constraint type ${type}`);
    }
  }
  return [out, applied];
}
