import { test } from "node:test";
import assert from "node:assert/strict";
import { OpenBoxClient } from "../src/core.ts";
import { MailGovernor } from "../src/governor.ts";
import { McpProxy } from "../src/mcp.ts";
import { FakeCore, jsonResponse } from "./helpers.ts";

function makeProxy(core: FakeCore, upstream?: (body: any) => { status: number; data: any; headers?: Record<string, string> }) {
  const client = new OpenBoxClient({ apiUrl: "https://core.test", apiKey: "k", fetchImpl: core.fetchImpl as any });
  const governor = new MailGovernor(client, {}, { hitl: { enabled: true, pollIntervalMs: 1, maxWaitMs: 500 } });
  const upstreamRequests: any[] = [];
  const fetchImpl = async (_url: string | URL, init?: any): Promise<Response> => {
    const req = JSON.parse(init.body);
    upstreamRequests.push(req);
    const r = upstream ? upstream(req) : { status: 200, data: { jsonrpc: "2.0", id: req.id, result: {} } };
    return jsonResponse(r.data, r.status, r.headers);
  };
  const proxy = new McpProxy(governor, { upstreamUrl: "https://mcp.test/mcp", fetchImpl: fetchImpl as any });
  return { proxy, upstreamRequests };
}

const call = (name: string, args: any, id = 1) =>
  JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });

const SEND_ARGS = { inboxId: "inbox_1", to: ["a@b.c"], subject: "hi", text: "yo" };

test("tools/call governed; camelCase args reach upstream", async () => {
  const core = new FakeCore();
  const { proxy, upstreamRequests } = makeProxy(core, (req) => ({
    status: 200,
    data: { jsonrpc: "2.0", id: req.id, result: { content: [{ type: "text", text: "sent" }] } },
  }));
  const resp = await proxy.handle("POST", {}, call("send_message", SEND_ARGS));
  const body = JSON.parse(resp.body);
  assert.equal(body.result.content[0].text, "sent");
  assert.equal(upstreamRequests[0].params.arguments.inboxId, "inbox_1");
  const started = core.payloads.find((p) => p.event_type === "ActivityStarted");
  assert.equal(started.activity_type, "agentmail.send_message");
  assert.equal(started.activity_input.inbox_id, "inbox_1"); // snake_case on the wire
});

test("blocked tool returns isError result with the reason", async () => {
  const core = new FakeCore({}, { verdict: "block", reason: "no external" });
  const { proxy, upstreamRequests } = makeProxy(core);
  const resp = await proxy.handle("POST", {}, call("send_message", SEND_ARGS, 7));
  const body = JSON.parse(resp.body);
  assert.equal(body.result.isError, true);
  assert.match(body.result.content[0].text, /no external/);
  assert.equal(upstreamRequests.length, 0);
});

test("unknown tools governed as writes", async () => {
  const core = new FakeCore();
  const { proxy } = makeProxy(core);
  await proxy.handle("POST", {}, call("daydream", { x: 1 }));
  const started = core.payloads.find((p) => p.event_type === "ActivityStarted");
  assert.equal(started.activity_input.action_class, "unknown");
});

test("batch requests refused", async () => {
  const { proxy } = makeProxy(new FakeCore());
  const resp = await proxy.handle("POST", {}, JSON.stringify([{ jsonrpc: "2.0", id: 1, method: "tools/list" }]));
  assert.equal(JSON.parse(resp.body).error.code, -32600);
});

test("tools/list relayed + classification report", async () => {
  const { proxy } = makeProxy(new FakeCore(), (req) => ({
    status: 200,
    data: { jsonrpc: "2.0", id: req.id, result: { tools: [{ name: "send_message" }, { name: "brand_new_tool" }] } },
  }));
  await proxy.handle("POST", {}, JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }));
  assert.equal(proxy.toolReport["send_message"], "send");
  assert.deepEqual(proxy.unknownTools, ["brand_new_tool"]);
});

test("mcp-session-id returned on governed calls", async () => {
  const core = new FakeCore();
  const { proxy } = makeProxy(core, (req) => ({
    status: 200,
    data: { jsonrpc: "2.0", id: req.id, result: { content: [] } },
    headers: { "mcp-session-id": "sess_1" },
  }));
  const resp = await proxy.handle("POST", {}, call("list_inboxes", {}));
  assert.equal(resp.headers["mcp-session-id"], "sess_1");
});

test("guardrail redaction forwarded upstream in camelCase", async () => {
  const core = new FakeCore(
    {},
    { verdict: "allow", guardrails: { validation_passed: true, input_type: "activity_input", redacted_input: { text: "[redacted]" } } },
    {},
  );
  const { proxy, upstreamRequests } = makeProxy(core);
  await proxy.handle("POST", {}, call("send_message", { ...SEND_ARGS, text: "secret" }));
  assert.equal(upstreamRequests[0].params.arguments.text, "[redacted]");
});
