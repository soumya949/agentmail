import { test } from "node:test";
import assert from "node:assert/strict";
import { OpenBoxClient } from "../src/core.ts";
import { MailGovernor } from "../src/governor.ts";
import { InboundRelay, WebhookAuth, MemoryDedupe, type GovernedInbound } from "../src/relay.ts";
import { FakeCore, makeMail, INBOUND_EVENT } from "./helpers.ts";

function makeRelay(core: FakeCore, opts: Partial<ConstructorParameters<typeof InboundRelay>[1]> = {}) {
  const client = new OpenBoxClient({ apiUrl: "https://core.test", apiKey: "k", fetchImpl: core.fetchImpl as any });
  const governor = new MailGovernor(client, {}, { hitl: { enabled: true, pollIntervalMs: 1, maxWaitMs: 500 } });
  const seen: GovernedInbound[] = [];
  const relay = new InboundRelay(governor, {
    auth: new WebhookAuth(undefined, { secret: "hook-secret" }),
    handler: (i) => { seen.push(i); },
    dedupe: new MemoryDedupe(),
    ...opts,
  });
  return { relay, seen, governor };
}

const AUTH = { authorization: "Bearer hook-secret" };
const body = (e = INBOUND_EVENT) => JSON.stringify(e);

test("unauthenticated delivery rejected", async () => {
  const { relay } = makeRelay(new FakeCore());
  const r = await relay.handle({}, body());
  assert.equal(r.status, 401);
});

test("content event delivered after screening", async () => {
  const { relay, seen } = makeRelay(new FakeCore());
  const r = await relay.handle(AUTH, body());
  assert.equal(r.status, 200);
  assert.equal(r.body.status, "delivered");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].result.verdict, "allow");
});

test("blocked inbound never reaches the handler; acks 200", async () => {
  const blocked: string[] = [];
  const { relay, seen } = makeRelay(new FakeCore({}, { verdict: "block", reason: "phish" }), {
    onBlocked: (_e, err) => blocked.push(String(err)),
  });
  const r = await relay.handle(AUTH, body());
  assert.equal(r.status, 200);
  assert.equal(r.body.status, "blocked");
  assert.equal(seen.length, 0);
  assert.equal(blocked.length, 1);
});

test("duplicate event_id delivered once", async () => {
  const { relay, seen } = makeRelay(new FakeCore());
  await relay.handle(AUTH, body());
  const r2 = await relay.handle(AUTH, body());
  assert.equal(r2.body.status, "duplicate");
  assert.equal(seen.length, 1);
});

test("status events are telemetry only", async () => {
  const { relay, seen } = makeRelay(new FakeCore());
  const r = await relay.handle(AUTH, JSON.stringify({
    event_type: "message.delivered", event_id: "e2",
    message: { inbox_id: "i", message_id: "m" },
  }));
  assert.equal(r.body.status, "recorded");
  assert.equal(seen.length, 0);
});

test("real status event shapes (send/bounce, no message) are recorded, not 400", async () => {
  const core = new FakeCore();
  const { relay, seen } = makeRelay(core);
  const r = await relay.handle(AUTH, JSON.stringify({
    type: "event", event_type: "message.sent", event_id: "evt_s",
    send: { inbox_id: "inbox_1", thread_id: "t", message_id: "m_out", timestamp: "x", recipients: ["b@x.io"] },
  }));
  assert.equal(r.status, 200);
  assert.equal(r.body.status, "recorded");
  assert.equal(seen.length, 0);
  const sig = core.payloads.find((p) => p.signal_name === "agentmail.message_sent");
  assert.equal(sig.message_id, "m_out");
  assert.deepEqual(sig.recipients, ["b@x.io"]);
  const bad = await relay.handle(AUTH, JSON.stringify({ event_type: "message.received", event_id: "z", send: {} }));
  assert.equal(bad.status, 400);
});

test("secret rotation accepts old and new", () => {
  const auth = new WebhookAuth(undefined, { secret: ["old", "new"] });
  assert.ok(auth.check({ authorization: "Bearer old" }));
  assert.ok(auth.check({ authorization: "Bearer new" }));
  assert.ok(!auth.check({ authorization: "Bearer bad" }));
});

// Regressions: the default dedupe used to be rebuilt per request (so it never
// deduplicated — makeRelay injects one, which is why this went unnoticed), and
// a 5xx left the id in dedupe, so AgentMail's redelivery was dropped as a
// duplicate and the mail was lost.

test("default dedupe (none injected) actually deduplicates", async () => {
  const seen: GovernedInbound[] = [];
  const { relay } = makeRelay(new FakeCore(), {
    dedupe: undefined,
    handler: (i: GovernedInbound) => { seen.push(i); },
  });
  await relay.handle(AUTH, body());
  const r2 = await relay.handle(AUTH, body());
  assert.equal(r2.body.status, "duplicate");
  assert.equal(seen.length, 1);
});

test("a 5xx releases the event id so the retry is processed", async () => {
  let calls = 0;
  const { relay } = makeRelay(new FakeCore(), {
    dedupe: undefined,
    handler: () => { calls += 1; if (calls === 1) throw new Error("app down"); },
  });
  const first = await relay.handle(AUTH, body());
  assert.equal(first.status, 500);
  const retry = await relay.handle(AUTH, body());
  assert.equal(retry.body.status, "delivered", "redelivery must not be swallowed as duplicate");
  assert.equal(calls, 2);
});
