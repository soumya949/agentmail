import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { lookup, classifyMcpTool, ActionClass } from "../src/catalog.ts";
import { buildActivityInput, buildInboundSignal, domainOf } from "../src/contracts.ts";
import { SEND_ARGS, INBOUND_EVENT } from "./helpers.ts";

const FIXTURES = fileURLToPath(new URL("../../sdk/tests/policy_fixtures/", import.meta.url));

test("activity_input is byte-equivalent to the Python wire shape", () => {
  const spec = lookup(["messages", "send"]);
  const out = buildActivityInput(spec, { inbox_id: "inbox_1", ...SEND_ARGS }, {});
  const expected = JSON.parse(readFileSync(`${FIXTURES}/send_message_input.json`, "utf8"));
  assert.deepEqual(out, expected);
});

test("inbound signal fields are byte-equivalent to Python", () => {
  const out = buildInboundSignal(INBOUND_EVENT, {});
  const expected = JSON.parse(readFileSync(`${FIXTURES}/inbound_signal.json`, "utf8"));
  assert.deepEqual(out, expected);
});

test("same catalogue: friendly names and classes", () => {
  assert.equal(lookup(["inboxes", "messages", "send"]).activityType, "agentmail.send_message");
  assert.equal(lookup(["inboxes", "messages", "reply"]).actionClass, ActionClass.SEND);
  assert.equal(lookup(["inboxes", "drafts", "create"]).actionClass, ActionClass.DRAFT);
  assert.equal(lookup(["inboxes", "messages", "get"]).actionClass, ActionClass.READ);
  assert.equal(lookup(["inboxes", "messages", "get_attachment"]).actionClass, ActionClass.READ_ATTACHMENT);
  assert.equal(lookup(["inboxes", "create"]).actionClass, ActionClass.ADMIN);
  assert.equal(lookup(["inboxes", "with_raw_response", "messages", "send"]).activityType, "agentmail.send_message");
  assert.throws(() => lookup(["inboxes", "messages", "transmute"]));
});

test("same MCP classification incl. unknown tools", () => {
  assert.equal(classifyMcpTool("send_message").actionClass, ActionClass.SEND);
  assert.equal(classifyMcpTool("list_inboxes").actionClass, ActionClass.READ);
  assert.equal(classifyMcpTool("brand_new_tool").actionClass, ActionClass.UNKNOWN);
});

test("domainOf handles display-name addresses", () => {
  assert.equal(domainOf("Alice <alice@Example.com>"), "example.com");
  assert.equal(domainOf("bob@x.io"), "x.io");
});
