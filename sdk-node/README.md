# openbox-agentmail-sdk (TypeScript)

OpenBox governance for AgentMail agents — the TypeScript port of
`openbox-agentmail-sdk-python` (`../sdk`). Same catalogue, same wire
contract, same verdicts: every AgentMail call is evaluated by OpenBox
before it executes, every result is evaluated before the agent sees it.

```ts
import { AgentMailClient } from "agentmail";
import { createOpenBoxMailAgent } from "openbox-agentmail-sdk";

const mail = createOpenBoxMailAgent({
  agentmailClient: new AgentMailClient({ apiKey: process.env.AGENTMAIL_API_KEY! }),
});

await mail.inboxes.messages.send("inbox_…@agentmail.to", {
  to: ["alice@example.com"], subject: "hi", text: "hello",
});
await mail.close();
```

## Environment

```
OPENBOX_API_URL=https://core.openbox.ai
OPENBOX_API_KEY=obx_…
OPENBOX_AGENT_DID=did:aip:…            # + OPENBOX_AGENT_PRIVATE_KEY (b64 seed) → signed requests
AGENTMAIL_API_KEY=am_…
```

`OPENBOX_AGENTMAIL_*` prefixed names take precedence; `OPENBOX_DID`/
`OPENBOX_URL` aliases are accepted. No TypeScript core SDK exists yet —
`src/core.ts` reimplements the signed v1 wire (Ed25519 canonical string,
`/api/v1/governance/evaluate` + `/approval`).

## Surfaces

- **REST**: `OpenBoxMailAgent` proxies `agentmail`; uncatalogued methods are refused.
- **Inbound**: `InboundRelay.handle(headers, body)` — constant-time header auth
  (rotation: pass `secret: [old, new]`), `event_id` dedupe, content events
  enforced, status events telemetry-only.
- **MCP**: `McpProxy.handle(method, headers, body)` — `tools/call` governed,
  `tools/list` builds a classification report, `Mcp-Session-Id` passthrough.
- **Approvals**: `approvalMode: "draft"` stages an AgentMail draft on
  REQUIRE_APPROVAL and resolves it later (`governor.resolveDraft(pending)`);
  `"wait"` polls inline. Both emit an `approval_resume` signal.
- **CONSTRAIN**: `max_recipients`, `allowed_domains`, `strip_attachments`,
  `force_bcc` rewrite the call; unsatisfiable/unknown escalate to approval.

## Dev

```powershell
npm install
npm test          # node:test + --experimental-transform-types (Node ≥ 20.6)
npm run typecheck
```

Shared wire fixtures live in `../sdk/tests/policy_fixtures/` — Node builders
must produce byte-equivalent JSON to the Python builders.
