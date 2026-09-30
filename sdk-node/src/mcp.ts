/** Governed MCP proxy — mirrors mcp_proxy/proxy.py.
 *  tools/call governed; everything else relayed to the hosted upstream. */

import { classifyMcpTool } from "./catalog.ts";
import {
  AgentMailBlockedError,
  AgentMailHaltedError,
  ApprovalExpiredError,
  ApprovalRejectedError,
  ApprovalTimeoutError,
  ContractError,
  GuardrailsValidationError,
  UncataloguedActionError,
} from "./errors.ts";
import type { MailGovernor } from "./governor.ts";

export const DEFAULT_MCP_UPSTREAM = "https://mcp.agentmail.to/mcp";

const HOP_BY_HOP = new Set(["host", "content-length", "connection", "transfer-encoding", "keep-alive", "te", "trailer", "upgrade"]);

export interface McpResponse {
  status: number;
  headers: Record<string, string>;
  body: string;
}

const toSnake = (o: Record<string, unknown>) =>
  Object.fromEntries(Object.entries(o).map(([k, v]) => [k.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase(), v]));

function toCamel(o: Record<string, unknown>, originalKeys: Set<string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) {
    const [head, ...rest] = k.split("_");
    const camel = head + rest.map((p) => p[0].toUpperCase() + p.slice(1)).join("");
    out[originalKeys.has(camel) || !originalKeys.has(k) ? camel : k] = v;
  }
  return out;
}

function parseRpcBody(contentType: string, body: string): any {
  if (contentType.includes("text/event-stream")) {
    const data = body.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim());
    if (!data.length) throw new Error("empty SSE response from upstream MCP");
    return JSON.parse(data[data.length - 1]);
  }
  return JSON.parse(body);
}

export class McpProxy {
  toolReport: Record<string, string> = {};
  unknownTools: string[] = [];

  constructor(
    public governor: MailGovernor,
    public opts: {
      upstreamUrl?: string;
      agentmailApiKey?: string;
      localToken?: string;
      toolTypeMap?: Record<string, string>;
      fetchImpl?: typeof fetch;
    } = {},
  ) {}

  private authorized(headers: Record<string, string>): boolean {
    if (!this.opts.localToken) return true;
    const got = Object.entries(headers).find(([k]) => k.toLowerCase() === "authorization")?.[1] ?? "";
    return got === `Bearer ${this.opts.localToken}`;
  }

  private fwdHeaders(headers: Record<string, string>): Record<string, string> {
    const drop = new Set(HOP_BY_HOP);
    if (this.opts.agentmailApiKey || this.opts.localToken) {
      drop.add("authorization");
      drop.add("x-api-key");
    }
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(headers)) if (!drop.has(k.toLowerCase())) out[k] = v;
    if (this.opts.agentmailApiKey) out["x-api-key"] = this.opts.agentmailApiKey;
    return out;
  }

  private async forward(method: string, headers: Record<string, string>, body?: string): Promise<{ status: number; headers: Record<string, string>; body: string }> {
    const f = this.opts.fetchImpl ?? fetch;
    const resp = await f(this.opts.upstreamUrl ?? DEFAULT_MCP_UPSTREAM, {
      method,
      headers: this.fwdHeaders(headers),
      body: body ?? undefined,
    });
    const outHeaders: Record<string, string> = {};
    resp.headers.forEach((v, k) => {
      if (!HOP_BY_HOP.has(k.toLowerCase())) outHeaders[k] = v;
    });
    return { status: resp.status, headers: outHeaders, body: await resp.text() };
  }

  private jsonrpcError(id: unknown, message: string, code = -32000): McpResponse {
    return { status: 200, headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: id ?? null, error: { code, message } }) };
  }

  private toolError(id: unknown, message: string): McpResponse {
    return {
      status: 200,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: id ?? null,
        result: { content: [{ type: "text", text: `Blocked by OpenBox: ${message}` }], isError: true },
      }),
    };
  }

  async handle(method: string, headers: Record<string, string>, body: string): Promise<McpResponse> {
    if (!this.authorized(headers)) return { status: 401, headers: { "content-type": "application/json" }, body: '{"error":"unauthorized"}' };
    if (method !== "POST") return this.forward(method, headers, body);
    let req: any;
    try {
      req = JSON.parse(body);
    } catch {
      return this.jsonrpcError(null, "invalid JSON", -32700);
    }
    if (Array.isArray(req)) return this.jsonrpcError(null, "batched JSON-RPC is not supported by the governed proxy", -32600);
    if (req?.method === "tools/call") return this.governedCall(req, headers);
    if (req?.method === "tools/list") return this.toolsList(method, headers, body);
    return this.forward(method, headers, body);
  }

  private async toolsList(method: string, headers: Record<string, string>, body: string): Promise<McpResponse> {
    const out = await this.forward(method, headers, body);
    if (out.status < 400) {
      try {
        const env = parseRpcBody(out.headers["content-type"] ?? "", out.body);
        for (const tool of env?.result?.tools ?? []) {
          if (typeof tool?.name === "string")
            this.toolReport[tool.name] = classifyMcpTool(tool.name, this.opts.toolTypeMap).actionClass;
        }
        this.unknownTools = Object.entries(this.toolReport).filter(([, c]) => c === "unknown").map(([n]) => n);
      } catch {
        /* reporting must not break the relay */
      }
    }
    return out;
  }

  private async governedCall(req: any, headers: Record<string, string>): Promise<McpResponse> {
    const reqId = req.id;
    const params = req.params ?? {};
    const name = params.name ?? "";
    const args = params.arguments ?? {};
    if (!args || typeof args !== "object" || Array.isArray(args))
      return this.jsonrpcError(reqId, "tools/call arguments must be an object", -32602);

    const spec = classifyMcpTool(name, this.opts.toolTypeMap);
    const originalKeys = new Set(Object.keys(args));
    let upstreamHeaders: Record<string, string> = {};

    const callUpstream = async (finalArgs: Record<string, unknown>) => {
      const fwd = { ...req, params: { ...params, arguments: toCamel(finalArgs, originalKeys) } };
      const { status, headers: h, body: b } = await this.forward("POST", headers, JSON.stringify(fwd));
      upstreamHeaders = h;
      if (status >= 400) throw new AgentMailBlockedError(`upstream MCP returned ${status}`, spec.activityType);
      const env = parseRpcBody(h["content-type"] ?? "", b);
      if ("error" in env) throw new AgentMailBlockedError(`upstream JSON-RPC error: ${JSON.stringify(env.error)}`, spec.activityType);
      return env.result ?? {};
    };

    try {
      const result = await this.governor.run(spec, toSnake(args), callUpstream);
      const value = result && typeof result === "object" && "value" in result ? result.value : result;
      if (value && typeof value === "object") delete (value as any).action;
      const outHeaders: Record<string, string> = { "content-type": "application/json" };
      for (const [k, v] of Object.entries(upstreamHeaders)) if (k.toLowerCase() === "mcp-session-id") outHeaders["mcp-session-id"] = v;
      return { status: 200, headers: outHeaders, body: JSON.stringify({ jsonrpc: "2.0", id: reqId ?? null, result: value }) };
    } catch (e) {
      if (
        e instanceof AgentMailBlockedError || e instanceof AgentMailHaltedError ||
        e instanceof ApprovalRejectedError || e instanceof ApprovalExpiredError ||
        e instanceof ApprovalTimeoutError || e instanceof GuardrailsValidationError ||
        e instanceof ContractError || e instanceof UncataloguedActionError
      ) return this.toolError(reqId, String(e));
      throw e;
    }
  }
}
