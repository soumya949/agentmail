/** CONSTRAIN vocabulary — mirrors sdk/src/openbox_agentmail/constraints.py.
 *
 *  Core sends constraints as BARE STRINGS (e.g. ["run_in_sandbox"]) as well as
 *  objects, so strings are normalised to {type: <string>} before lookup.
 *
 *  A constraint that cannot be satisfied makes the caller FAIL CLOSED — the
 *  governor turns ConstraintViolation into AgentMailBlockedError. It never
 *  escalates to approval: Core registers an approval for REQUIRE_APPROVAL
 *  only, so polling on a CONSTRAIN waits forever.
 *
 *  Reality check: the OpenBox rule builder cannot author constraints at all —
 *  a CONSTRAIN rule only ever emits ["run_in_sandbox"], which is meaningless
 *  for email. Use REQUIRE APPROVAL or BLOCK for mail. */

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

/** Core-level directives aimed at code execution — valid constraints, just
 *  meaningless for "send an email". Naming them gives a far better error. */
const NOT_APPLICABLE_TO_EMAIL = new Set(["run_in_sandbox", "sandbox", "dry_run"]);

export function applyConstraints(
  args: Record<string, unknown>,
  constraints: Array<Record<string, unknown> | string> | null | undefined,
): [Record<string, unknown>, string[]] {
  if (!constraints?.length) return [args, []];
  const out = { ...args };
  const applied: string[] = [];
  for (const raw of constraints) {
    const c: Record<string, unknown> = typeof raw === "string" ? { type: raw } : raw;
    if (c === null || typeof c !== "object") throw new ConstraintViolation(`unrecognised constraint ${String(raw)}`);
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
    } else if (NOT_APPLICABLE_TO_EMAIL.has(type)) {
      throw new ConstraintViolation(
        `constraint '${type}' has no meaning for an email action — there is no code to run. ` +
          "Use REQUIRE APPROVAL or BLOCK for mail instead",
      );
    } else {
      throw new ConstraintViolation(`unknown constraint type ${type}`);
    }
  }
  return [out, applied];
}
