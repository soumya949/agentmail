/** Governed drop-in for the agentmail Node client.
 *  mail.inboxes.messages.send(...) → governed by MailGovernor.
 *
 *  agentmail-node signatures are ``(ids..., request?, requestOptions?)`` with
 *  camelCase method names (``getAttachment``) and camelCase request fields.
 *  The governed contract is snake_case (shared with the Python SDK), so
 *  method/resource names and request fields are converted in, and the
 *  request is rebuilt camelCase on the way out. ``requestOptions`` is passed
 *  through untouched and never enters activity_input. */

import { lookup, type ActionSpec } from "./catalog.ts";
import { camelKeys, toSnakeKey, type AgentMailSettings } from "./contracts.ts";
import { resolveOpenBoxConfig, OpenBoxClient } from "./core.ts";
import { OpenBoxConfigError, UncataloguedActionError } from "./errors.ts";
import { MailGovernor, type GovernorOptions } from "./governor.ts";

/** Positional id names per resource, in order. */
const POSITIONAL: Record<string, string[]> = {
  messages: ["inbox_id", "message_id", "attachment_id"],
  drafts: ["inbox_id", "draft_id", "attachment_id"],
  threads: ["inbox_id", "thread_id", "attachment_id"],
  inboxes: ["inbox_id"],
  pods: ["pod_id"],
  domains: ["domain_id"],
  webhooks: ["webhook_id"],
  lists: ["list_id", "entry_id"],
  api_keys: ["api_key_id"],
  providers: ["provider_id"],
};

function specFor(path: string[]): ActionSpec {
  try {
    return lookup(path.map(toSnakeKey));
  } catch {
    throw new UncataloguedActionError(
      `AgentMail method ${path.join(".")} is not in the OpenBox action catalogue; refusing to call it ungoverned.`,
    );
  }
}

interface Normalized {
  args: Record<string, unknown>;
  requestOptions: unknown;
  hadRequest: boolean;
}

function normalizeArgs(spec: ActionSpec, callArgs: any[]): Normalized {
  const names = POSITIONAL[spec.resource] ?? [];
  const args: Record<string, unknown> = {};
  let requestOptions: unknown;
  let hadRequest = false;
  let idIdx = 0;
  for (const a of callArgs) {
    if (a !== null && typeof a === "object") {
      if (!hadRequest) {
        for (const [k, v] of Object.entries(a)) args[toSnakeKey(k)] = v;
        hadRequest = true;
      } else {
        requestOptions = a;
      }
    } else if (a !== undefined) {
      if (idIdx < names.length) args[names[idIdx]] = a;
      idIdx++;
    }
  }
  return { args, requestOptions, hadRequest };
}

function rebuildCall(spec: ActionSpec, final: Record<string, unknown>, n: Normalized): any[] {
  const names = POSITIONAL[spec.resource] ?? [];
  const ids = names.filter((k) => k in final).map((k) => final[k]);
  const body: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(final)) if (!names.includes(k)) body[k] = v;
  const out: any[] = [...ids];
  if (n.hadRequest || Object.keys(body).length) out.push(camelKeys(body));
  if (n.requestOptions !== undefined) {
    if (!n.hadRequest && !Object.keys(body).length) out.push(undefined);
    out.push(n.requestOptions);
  }
  return out;
}

function wrapResource(target: any, path: string[], governor: MailGovernor): any {
  return new Proxy(target, {
    get(t, prop) {
      if (typeof prop !== "string" || prop.startsWith("_")) return Reflect.get(t, prop);
      const value = t[prop];
      if (typeof value === "function") {
        const spec = specFor([...path, prop]);
        return (...callArgs: any[]) => {
          const n = normalizeArgs(spec, callArgs);
          return governor.run(spec, n.args, (final) => value.apply(t, rebuildCall(spec, final, n)));
        };
      }
      if (value && typeof value === "object") return wrapResource(value, [...path, prop], governor);
      return value;
    },
  });
}

export class OpenBoxMailAgent {
  readonly governor: MailGovernor;
  readonly raw: any;
  private _root: any;
  /** The proxied AgentMail surface is dynamic (e.g. ``agent.inboxes.messages.send``). */
  [key: string]: any;

  constructor(agentmailClient: any, governor: MailGovernor) {
    this.raw = agentmailClient;
    this.governor = governor;
    this._root = wrapResource(agentmailClient, [], governor);
    return new Proxy(this, {
      get(t, prop) {
        if (prop === "governor" || prop === "raw" || prop === "close" || prop === "_root" || prop === "then")
          return prop === "then" ? undefined : Reflect.get(t, prop);
        return Reflect.get(t._root as object, prop);
      },
    });
  }

  async close(error?: string): Promise<void> {
    await this.governor.closeSession(error);
  }
}

export function createOpenBoxMailAgent(opts: {
  agentmailClient?: any;
  settings?: AgentMailSettings;
  governor?: GovernorOptions;
  openbox?: ReturnType<typeof resolveOpenBoxConfig>;
  core?: OpenBoxClient;
} = {}): OpenBoxMailAgent {
  const core = opts.core ?? new OpenBoxClient(opts.openbox ?? resolveOpenBoxConfig());
  const mail = opts.agentmailClient;
  if (!mail)
    throw new OpenBoxConfigError("pass agentmailClient (new AgentMailClient({apiKey}) from the agentmail package)");
  return new OpenBoxMailAgent(mail, new MailGovernor(core, opts.settings ?? {}, opts.governor ?? {}, mail));
}
