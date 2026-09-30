import { test } from "node:test";
import assert from "node:assert/strict";
import { OpenBoxClient } from "../src/core.ts";
import { MailGovernor, type PendingApproval } from "../src/governor.ts";
import { OpenBoxMailAgent } from "../src/client.ts";
import {
  AgentMailBlockedError,
  AgentMailHaltedError,
  ApprovalRejectedError,
  GovernanceAPIError,
  UncataloguedActionError,
} from "../src/errors.ts";
import { FakeCore, INBOUND_EVENT, makeMail, SEND_ARGS } from "./helpers.ts";

function makeAgent(core: FakeCore, mail = makeMail(), settings: any = {}, govOpts: any = {}) {
  const client = new OpenBoxClient({
    apiUrl: "https://core.test",
    apiKey: "obx_test_conformance",
    onApiError: "fail_closed",
    fetchImpl: core.fetchImpl as any,
  });
  const governor = new MailGovernor(client, settings, { hitl: { enabled: true, pollIntervalMs: 1, maxWaitMs: 2000 }, ...govOpts }, mail);
  const agent = new OpenBoxMailAgent(mail, governor);
  return { agent, mail, governor };
}

test("allow: full lifecycle emitted once, call executes", async () => {
  const core = new FakeCore();
  const { agent, mail } = makeAgent(core);
  const res = await agent.inboxes.messages.send("inbox_1", SEND_ARGS);
  assert.equal(res.messageId, "m_1");
  assert.equal(mail.calls.length, 1);
  const types = core.lifecycle().map((p) => p.event_type);
  assert.deepEqual(types, ["WorkflowStarted", "ActivityStarted", "ActivityCompleted"]);
  assert.equal(core.payloads[1].activity_type, "agentmail.send_message");
  assert.equal(core.payloads[1].activity_input.inbox_id, "inbox_1");
  // camelCase model -> snake_case wire, matching the Python SDK
  assert.equal(core.payloads[2].activity_output.message_id, "m_1");
  assert.equal(core.payloads[2].activity_output.messageId, undefined);
  assert.equal(core.payloads[0].source, "agentmail-telemetry");
});

test("block: AgentMail never called, error carries reason", async () => {
  const core = new FakeCore({}, { verdict: "block", reason: "external sends denied" });
  const { agent, mail } = makeAgent(core);
  await assert.rejects(() => agent.inboxes.messages.send("inbox_1", SEND_ARGS), AgentMailBlockedError);
  assert.equal(mail.calls.length, 0);
});

test("halt: stops the session and short-circuits later calls locally", async () => {
  const core = new FakeCore({}, { verdict: "halt", reason: "kill" });
  const { agent, mail } = makeAgent(core);
  await assert.rejects(() => agent.inboxes.messages.send("inbox_1", SEND_ARGS), AgentMailHaltedError);
  const before = core.payloads.length;
  await assert.rejects(() => agent.inboxes.messages.list("inbox_1"), AgentMailHaltedError);
  assert.equal(core.payloads.length, before); // no new wire traffic
});

test("require_approval polls then executes; approval_resume emitted", async () => {
  const core = new FakeCore({}, { verdict: "require_approval", approval_id: "ap_1" }, { action: "allow" }, {}, {});
  const { agent, mail } = makeAgent(core);
  const res = await agent.inboxes.messages.send("inbox_1", SEND_ARGS);
  assert.equal(res.messageId, "m_1");
  assert.equal(core.approvalRequests.length, 1);
  const resume = core.payloads.find((p) => p.signal_name === "approval_resume");
  assert.equal(resume.decision, "approved");
});

test("require_approval rejected: no call, resume says rejected", async () => {
  const core = new FakeCore({}, { verdict: "require_approval", approval_id: "ap_1" }, { action: "block", reason: "no" }, {});
  const { agent, mail } = makeAgent(core);
  await assert.rejects(() => agent.inboxes.messages.send("inbox_1", SEND_ARGS), ApprovalRejectedError);
  assert.equal(mail.calls.length, 0);
  assert.equal(core.payloads.find((p) => p.signal_name === "approval_resume").decision, "rejected");
});

test("draft mode: send defers to draft, resolves to governed send", async () => {
  const core = new FakeCore({}, { verdict: "require_approval", approval_id: "ap_1" }, {}, {});
  const mail = makeMail();
  const { agent } = makeAgent(core, mail, { approvalMode: "draft" });

  const pending = (await agent.inboxes.messages.send("inbox_1", SEND_ARGS)) as PendingApproval;
  assert.equal(pending.draftId, "d_1");
  assert.equal(mail.calls[0].name, "drafts.create");
  // real SDK: create(inbox_id, request) with camelCase fields
  assert.deepEqual(mail.calls[0].ids, ["inbox_1"]);
  assert.equal(mail.calls[0].request.clientId, pending.activityId);
  assert.equal(mail.calls[0].request.inbox_id, undefined);
  assert.equal(mail.calls.filter((c) => c.name === "messages.send").length, 0);

  core.queue.push({ action: "allow" }, {}, {}, {}); // poll, resume, send started, send done
  const res = await agent.governor.resolveDraft(pending);
  assert.equal(res.status, "sent");
  assert.equal(mail.calls[1].name, "drafts.send");
  // real SDK: send(inbox_id, draft_id, request, requestOptions{idempotencyKey})
  assert.deepEqual(mail.calls[1].ids, ["inbox_1", "d_1"]);
  assert.equal(mail.calls[1].options?.idempotencyKey, pending.activityId);
});

test("draft mode rejected deletes the draft", async () => {
  const core = new FakeCore({}, { verdict: "require_approval", approval_id: "ap_1" }, {}, {});
  const mail = makeMail();
  const { agent } = makeAgent(core, mail, { approvalMode: "draft" });
  const pending = (await agent.inboxes.messages.send("inbox_1", SEND_ARGS)) as PendingApproval;
  core.queue.push({ action: "block" }, {}, {}, {});
  const res = await agent.governor.resolveDraft(pending);
  assert.equal(res.status, "rejected");
  assert.equal(mail.calls[1].name, "drafts.delete");
  assert.deepEqual(mail.calls[1].ids, ["inbox_1", "d_1"]);
});

test("constrain strip_attachments is applied to the real call", async () => {
  const core = new FakeCore({}, { verdict: "constrain", constraints: [{ type: "strip_attachments" }] }, {});
  const { agent, mail } = makeAgent(core);
  await agent.inboxes.messages.send("inbox_1", { ...SEND_ARGS, attachments: [{ filename: "a.pdf" }] });
  assert.deepEqual(mail.calls[0].request.attachments, []);
});

// An unsatisfiable CONSTRAIN fails closed and NEVER polls. Core registers an
// approval for REQUIRE_APPROVAL only, so polling on a CONSTRAIN waits on an
// approval that will never exist — with maxWaitMs unset that hangs forever.
// Verified live against a real OpenBox Core (Sep 2026).

test("constrain violation fails closed and never polls", async () => {
  const core = new FakeCore(
    {},
    { verdict: "constrain", constraints: [{ type: "allowed_domains", domains: ["corp.example"] }] },
  );
  const { agent, mail } = makeAgent(core);
  await assert.rejects(
    () => agent.inboxes.messages.send("inbox_1", SEND_ARGS),
    (e: any) => e.constructor.name === "AgentMailBlockedError" && /could not be satisfied/.test(e.message),
  );
  assert.equal(core.approvalRequests.length, 0, "must not poll for an approval Core never registers");
  assert.equal(mail.calls.length, 0, "AgentMail must not be called");
});

test("bare constrain fails closed and never polls", async () => {
  const core = new FakeCore({}, { verdict: "constrain" });
  const { agent, mail } = makeAgent(core);
  await assert.rejects(
    () => agent.inboxes.messages.send("inbox_1", SEND_ARGS),
    (e: any) => e.constructor.name === "AgentMailBlockedError" && /without any constraints/.test(e.message),
  );
  assert.equal(core.approvalRequests.length, 0);
  assert.equal(mail.calls.length, 0);
});

test("Core's bare-string constraints are understood, not reported as unknown", async () => {
  // Core sends ["run_in_sandbox"] — strings, not objects.
  const core = new FakeCore({}, { verdict: "constrain", constraints: ["run_in_sandbox"] });
  const { agent, mail } = makeAgent(core);
  await assert.rejects(
    () => agent.inboxes.messages.send("inbox_1", SEND_ARGS),
    (e: any) => /no meaning for an email action/.test(e.message),
  );
  assert.equal(core.approvalRequests.length, 0);
  assert.equal(mail.calls.length, 0);
});

test("string form of a real constraint still applies", async () => {
  const core = new FakeCore({}, { verdict: "constrain", constraints: ["strip_attachments"] }, {});
  const { agent, mail } = makeAgent(core);
  await agent.inboxes.messages.send("inbox_1", { ...SEND_ARGS, attachments: [{ filename: "a.pdf" }] });
  assert.deepEqual(mail.calls[0].request.attachments, []);
});

test("inbound constrain fails closed and never polls", async () => {
  const core = new FakeCore({}, { verdict: "constrain", constraints: ["run_in_sandbox"] });
  const { agent } = makeAgent(core);
  await assert.rejects(
    () => agent.governor.screenInbound(INBOUND_EVENT, { enforce: true }),
    (e: any) => /cannot be rewritten/.test(e.message),
  );
  assert.equal(core.approvalRequests.length, 0);
});

test("a failed AgentMail call is recorded, not dropped", async () => {
  // Core rejects an ActivityCompleted carrying a top-level `error` with HTTP
  // 400 and drops the whole event, so the failure must ride inside
  // activity_output instead.
  const core = new FakeCore({}, {}, {});
  const mail = makeMail();
  const { agent } = makeAgent(core, mail);
  mail.inboxes.messages.send = () => Promise.reject(new Error("x".repeat(4000)));
  await assert.rejects(() => agent.inboxes.messages.send("inbox_1", SEND_ARGS));
  const completed = core.payloads.filter((p: any) => p.event_type === "ActivityCompleted");
  assert.equal(completed.length, 1);
  assert.equal(completed[0].error, undefined, "must NOT send a top-level error field");
  assert.equal(completed[0].failed, true);
  assert.equal(completed[0].activity_output.status, "failed");
  assert.ok(completed[0].activity_output.error.length < 1100, "error text must be bounded");
});

test("guardrail redacted_input is applied to the real call", async () => {
  const core = new FakeCore(
    {},
    { verdict: "allow", guardrails: { validation_passed: true, input_type: "activity_input", redacted_input: { text: "[redacted]" } } },
    {},
  );
  const { agent, mail } = makeAgent(core);
  await agent.inboxes.messages.send("inbox_1", { ...SEND_ARGS, text: "secret" });
  assert.equal(mail.calls[0].request.text, "[redacted]");
});

test("uncatalogued methods are refused before any wire call", async () => {
  const core = new FakeCore();
  const mail = makeMail();
  (mail.inboxes.messages as any).transmute = () => Promise.resolve({});
  const { agent } = makeAgent(core, mail);
  // refusal is eager: the attribute access itself throws before a call exists
  assert.throws(() => (agent.inboxes.messages as any).transmute("inbox_1"), UncataloguedActionError);
  assert.equal(core.payloads.length, 0);
});

test("core outage fails closed on writes", async () => {
  const failing = async () => new Response("x", { status: 500 });
  const client = new OpenBoxClient({ apiUrl: "https://core.test", apiKey: "k", fetchImpl: failing as any });
  const mail = makeMail();
  const governor = new MailGovernor(client, {}, {}, mail);
  const agent = new OpenBoxMailAgent(mail, governor);
  // WorkflowStarted evaluation fails closed before the call
  await assert.rejects(() => agent.inboxes.messages.send("inbox_1", SEND_ARGS), GovernanceAPIError);
  assert.equal(mail.calls.length, 0);
});

test("close emits WorkflowCompleted", async () => {
  const core = new FakeCore({}, {}, {}, {});
  const { agent } = makeAgent(core);
  await agent.inboxes.messages.send("inbox_1", SEND_ARGS);
  await agent.close();
  assert.equal(core.payloads[core.payloads.length - 1].event_type, "WorkflowCompleted");
});
