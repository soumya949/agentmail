/** Drives the REAL agentmail-node client through the governed proxy, with
 *  AgentMail's HTTP layer intercepted via the client's ``fetch`` option. This
 *  catches shape drift (method names, positional signature, field casing)
 *  that a hand-written fake cannot. */

import { test } from "node:test";
import assert from "node:assert/strict";
import { AgentMailClient } from "agentmail";
import { OpenBoxClient } from "../src/core.ts";
import { MailGovernor, type PendingApproval } from "../src/governor.ts";
import { OpenBoxMailAgent } from "../src/client.ts";
import { AgentMailBlockedError } from "../src/errors.ts";
import { FakeCore, jsonResponse } from "./helpers.ts";

interface HttpCall {
  method: string;
  path: string;
  body: any;
  headers: Record<string, string>;
}

function realAgent(core: FakeCore, settings: any = {}) {
  const http: HttpCall[] = [];
  const amFetch = async (url: any, init: any = {}): Promise<Response> => {
    const u = new URL(String(url));
    const headers: Record<string, string> = {};
    new Headers(init.headers).forEach((v, k) => (headers[k] = v));
    const body = init.body ? JSON.parse(String(init.body)) : null;
    http.push({ method: init.method ?? "GET", path: u.pathname, body, headers });
    if (u.pathname.endsWith("/drafts") && init.method === "POST")
      return jsonResponse({ inbox_id: "inbox_1", draft_id: "d_real", labels: [], updated_at: "2026-01-01T00:00:00Z", created_at: "2026-01-01T00:00:00Z" });
    if (u.pathname.includes("/attachments/"))
      return jsonResponse({ attachment_id: "a_1", filename: "x.pdf", size: 3, content_type: "application/pdf", download_url: "https://x", expires_at: "2026-01-01T00:00:00Z" });
    return jsonResponse({ message_id: "m_real", thread_id: "t_real" });
  };
  const mail = new AgentMailClient({ apiKey: "am_test", baseUrl: "https://api.agentmail.test", fetch: amFetch as any });
  const client = new OpenBoxClient({ apiUrl: "https://core.test", apiKey: "obx_test", fetchImpl: core.fetchImpl as any });
  const governor = new MailGovernor(client, settings, { hitl: { enabled: true, pollIntervalMs: 1, maxWaitMs: 1000 } }, mail);
  return { agent: new OpenBoxMailAgent(mail, governor), http };
}

test("real client: send is governed and hits POST /inboxes/{id}/messages/send", async () => {
  const core = new FakeCore();
  const { agent, http } = realAgent(core);
  const res = await agent.inboxes.messages.send("inbox_1", { to: ["a@example.com"], subject: "hi", text: "yo", replyTo: ["r@example.com"] });
  assert.equal(res.messageId, "m_real");
  assert.equal(http.length, 1);
  assert.equal(http[0].method, "POST");
  assert.match(http[0].path, /\/inboxes\/inbox_1\/messages\/send$/);
  assert.equal(http[0].body.subject, "hi");
  const started = core.payloads.find((p) => p.event_type === "ActivityStarted");
  assert.equal(started.activity_type, "agentmail.send_message");
  assert.deepEqual(started.activity_input.reply_to, ["r@example.com"]);
  const done = core.payloads.find((p) => p.event_type === "ActivityCompleted");
  assert.equal(done.activity_output.message_id, "m_real");
});

test("real client: block prevents any AgentMail HTTP", async () => {
  const core = new FakeCore({}, { verdict: "block", reason: "no" });
  const { agent, http } = realAgent(core);
  await assert.rejects(() => agent.inboxes.messages.send("inbox_1", { to: ["a@example.com"], text: "x" }), AgentMailBlockedError);
  assert.equal(http.length, 0);
});

test("real client: camelCase methods (getAttachment, replyAll) are catalogued", async () => {
  const core = new FakeCore();
  const { agent, http } = realAgent(core);
  await agent.inboxes.messages.getAttachment("inbox_1", "m_1", "a_1");
  await agent.inboxes.messages.replyAll("inbox_1", "m_1", { text: "thanks" });
  const types = core.payloads.filter((p) => p.event_type === "ActivityStarted").map((p) => p.activity_type);
  assert.deepEqual(types, ["agentmail.get_attachment", "agentmail.reply_all"]);
  assert.match(http[0].path, /\/messages\/m_1\/attachments\/a_1$/);
  assert.match(http[1].path, /\/messages\/m_1\/reply-all$/);
});

test("real client: requestOptions pass through and never enter activity_input", async () => {
  const core = new FakeCore();
  const { agent, http } = realAgent(core);
  await agent.inboxes.messages.send("inbox_1", { to: ["a@example.com"], text: "x" }, { idempotencyKey: "idem-1" });
  assert.equal(http[0].headers["idempotency-key"], "idem-1");
  const started = core.payloads.find((p) => p.event_type === "ActivityStarted");
  assert.equal(JSON.stringify(started.activity_input).includes("idem-1"), false);
});

test("real client: draft mode creates, then sends the draft with idempotency key", async () => {
  const core = new FakeCore({}, { verdict: "require_approval", approval_id: "ap" }, {}, {});
  const { agent, http } = realAgent(core, { approvalMode: "draft" });
  const pending = (await agent.inboxes.messages.send("inbox_1", { to: ["a@example.com"], subject: "s", text: "t" })) as PendingApproval;
  assert.equal(pending.draftId, "d_real");
  assert.match(http[0].path, /\/inboxes\/inbox_1\/drafts$/);
  assert.equal(http[0].body.client_id, pending.activityId);
  assert.equal(http[0].body.inbox_id, undefined);

  core.queue.push({ action: "allow" }, {}, {}, {});
  const res = await agent.governor.resolveDraft(pending);
  assert.equal(res.status, "sent");
  assert.match(http[1].path, /\/inboxes\/inbox_1\/drafts\/d_real\/send$/);
  assert.equal(http[1].headers["idempotency-key"], pending.activityId);
});
