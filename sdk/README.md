# openbox-agentmail-sdk-python

OpenBox governance for [AgentMail](https://www.agentmail.to) agents.

Every outbound AgentMail operation (send, reply, forward, drafts, deletes,
label changes, admin), every read that pulls untrusted email content into the
agent, and every inbound webhook delivery is evaluated by OpenBox policy —
with verdicts, approvals, guardrail redactions and telemetry visible in the
OpenBox dashboard.

## Install

```bash
pip install openbox-agentmail-sdk-python

# optional extras
pip install "openbox-agentmail-sdk-python[mcp]"      # MCP proxy (httpx, uvicorn)
pip install "openbox-agentmail-sdk-python[toolkit]"  # OpenAI / LangChain / LiveKit toolkits
pip install "openbox-agentmail-sdk-python[all]"
```

Requires Python 3.11+. Runtime dependencies: `openbox-sdk-python[http]`, `agentmail`.

## Quick start

```python
from openbox_agentmail import create_openbox_mail_agent

mail = create_openbox_mail_agent()  # reads env, validates the key, emits WorkflowStarted on first use

msg = mail.inboxes.messages.send(
    inbox_id="agent@your-domain.com",
    to="customer@example.com",
    subject="Hello",
    text="Hi from a governed agent.",
)
mail.close()  # WorkflowCompleted
```

Configuration (env, prefix `OPENBOX_AGENTMAIL` falling back to `OPENBOX_`):

| Variable | Purpose |
| --- | --- |
| `OPENBOX_AGENTMAIL_API_URL` | OpenBox Core URL — **required**, there is no default |
| `OPENBOX_AGENTMAIL_API_KEY` | `obx_live_*` / `obx_test_*` key — **required** |
| `OPENBOX_AGENTMAIL_AGENT_DID` / `_AGENT_PRIVATE_KEY` | DID identity (optional) |
| `OPENBOX_AGENTMAIL_ON_API_ERROR` | `fail_closed` (default) or `fail_open` |
| `AGENTMAIL_API_KEY` | AgentMail API key — **required** |
| `AGENTMAIL_INBOX_ID` | Inbox the examples read (e.g. `agent@your-domain.com`) |

CrewAI-style names (`OPENBOX_URL`, `<PREFIX>_DID`, `<PREFIX>_PRIVATE_KEY`) are
accepted as deprecated aliases.

## What gets governed

- **Outbound**: `send`, `reply`, `reply_all`, `forward`, `send_draft` and every
  other catalogued method → `ActivityStarted` before the call, `ActivityCompleted`
  after. `BLOCK`/`HALT` raise before AgentMail is touched; `REQUIRE_APPROVAL`
  polls the dashboard; `CONSTRAIN` on writes **fails closed** — see
  [CONSTRAIN is not usable for email](#constrain-is-not-usable-for-email).
- **Inbound**: mount `InboundRelay.asgi()` on your webhook endpoint (or run
  `WebsocketInbound` — no public URL needed). Deliveries are authenticated
  (custom delivery headers), deduplicated, and each received email becomes an
  `agentmail.receive_message` activity: `ActivityStarted` screens what
  AgentMail pushed (sender, SPF/DKIM headers, subject, body) and can
  `BLOCK`/`HALT`/`REQUIRE_APPROVAL`; the message is then fetched with
  `messages.get` *inside* the activity (the GET shows as child HTTP spans);
  `ActivityCompleted` screens the fetched copy. Only then does your handler
  get it — with guardrail redactions from both stages applied.
  `fetch_on_receive=False` keeps the older `SignalReceived`-only path.
  Delivery-status events (`message.sent`, `message.bounced`, …) are always
  `SignalReceived` telemetry, never enforced.
- **Reads**: `get`/`list`/`search`/`get_attachment` responses are screened on
  `ActivityCompleted` — retrieved mail is untrusted input and gets the same
  injection checks as inbound webhooks.
- **Uncatalogued methods** (new AgentMail features, typos) are refused rather
  than passed through ungoverned.

## Inbound webhook relay

```python
from openbox_agentmail import InboundRelay, WebhookAuth, create_openbox_mail_agent

agent = create_openbox_mail_agent()
relay = InboundRelay(
    agent.governor,
    auth=WebhookAuth(secret="the-header-secret-you-set-on-the-webhook"),
    handler=lambda inbound: process(inbound.event["message"]),
)
app = relay.asgi()  # mount under your HTTP stack (uvicorn, FastAPI router, ...)
```

Register the webhook with a matching delivery header:

```python
agent.raw.webhooks.create(
    url="https://you.example.com/webhooks/agentmail",
    event_types=["message.received", "message.received.spam"],
    headers={"Authorization": "Bearer the-header-secret-you-set-on-the-webhook"},
)
```

Blocks return `200` (a decision, not a transient error). Core outages and
message-fetch failures return `503` so AgentMail retries rather than dropping
unscreened mail — the event id is released from dedupe so that retry is
processed. With `background=True` processing is ack-then-process: auth +
validation + dedupe ack first, then a background queue screens and delivers
(retry → dead-letter on outages).

The fetch uses the governor's raw AgentMail client, or `agentmail_client=`
(a governed agent passed there is unwrapped so the fetch isn't a nested
`get_message` activity). The relay refuses to start with `fetch_on_receive=True`
and no client.

`WebhookAuth(secret=[old, new])` accepts both values during a rotation
window. Flask/FastAPI mounts: `relay.flask_blueprint()`,
`relay.fastapi_router()`. The same screening also runs over the AgentMail
WebSocket channel via `WebsocketInbound`; a socket can't ask AgentMail to
redeliver, so transient failures retry locally (`screen_retries`,
`retry_backoff`) and then go to `on_dead_letter(event, error)` — the email
stays in the AgentMail inbox.

## CONSTRAIN is not usable for email

The SDK implements a constraint vocabulary (`force_bcc`, `max_recipients`,
`allowed_domains`, `strip_attachments`, `require_approval`) that rewrites a send
to make it compliant. **In practice you cannot reach it**: the OpenBox rule
builder offers a CONSTRAIN decision but no way to author the constraints, so a
CONSTRAIN rule only ever emits `["run_in_sandbox"]` — a code-execution
directive that means nothing for an email.

The SDK therefore **fails closed** on any CONSTRAIN it cannot satisfy, raising
`AgentMailBlockedError` with a message naming the constraint. It deliberately
does *not* escalate to a human: Core registers an approval for
`REQUIRE_APPROVAL` only, so polling would wait forever.

**Use `REQUIRE APPROVAL` or `BLOCK` for mail policies.** The same applies to
inbound — a received message cannot be rewritten, so CONSTRAIN refuses it.

`BLOCK AND PATCH` is likewise unreachable: the dashboard pins it to
`activity_type == llm_call`, so `AgentMailBlockedError.patched_args()` can never
fire for an email action.

## Recording why the agent acted

The SDK sees your AgentMail calls but never the thing that caused them — an API
request, a bot, a cron tick. Declare it and it is recorded as a `SignalReceived`
ordered *before* the activities it caused:

```python
mail.emit_trigger("api_request", {"reason": "invoice overdue"}, source="billing-service")
mail.inboxes.messages.send(...)
```

Nothing is emitted automatically — a fabricated event in an audit trail is worse
than a missing one. `BLOCK`/`HALT` on a trigger stop the agent before AgentMail
is touched, so you can refuse to act on a caller you do not trust. Caller data is
namespaced under `trigger_data` and cannot forge the fields policy matches on.

## Scoping sessions

By default one agent is one long-lived workflow. To make each task, request or
email its own `WorkflowStarted … WorkflowCompleted`:

```python
with mail.session():                 # async: `async with mail.session():`
    mail.emit_trigger("api_request", source="billing-service")
    mail.inboxes.messages.send(...)
```

Inbound has the same option — `InboundRelay(..., session_per_message=True,
arrival_signal=True)` (also on `WebsocketInbound`), where `arrival_signal` adds a
telemetry-only `SignalReceived` when mail arrives.

**Trade-off:** behavioural rules match prior activity *within* a session, so
per-task sessions stop a rule like "must read mail before sending" from ever
being satisfied. Keep one long session when cross-task sequences matter.

See `examples/api_server.py` for a governed HTTP endpoint mapping verdicts onto
status codes (403 policy refused · 502 AgentMail refused · 202 pending approval).

## Approvals

`REQUIRE_APPROVAL` waits inline by default. `AgentMailSettings(approval_mode="draft")`
instead stages an AgentMail **draft** and returns a `PendingApproval`
immediately; call `pending.wait()` to poll the decision: approved →
`drafts.send` with the activity id as idempotency key, rejected/expired →
`drafts.delete`. Every resolution emits an
`approval_resume` signal. Pending approvals persist in a pluggable store
(memory default) so resolution survives restarts.

## MCP

`openbox-agentmail-mcp` (HTTP, uvicorn — needs the `[mcp]` extra) or
`python -m openbox_agentmail.mcp_proxy --stdio`. `tools/call` is governed; `tools/list` builds a
classification report (`proxy.tool_report`, unknowns governed as writes);
`Mcp-Session-Id` passes through. The proxy holds the AgentMail key — client
auth headers never reach upstream.

## Failure policy

Sending email is irreversible, so this SDK defaults to `on_api_error="fail_closed"`
and refuses fail-open fallbacks on write actions even when fail-open is
configured (override via `AgentMailSettings(allow_fallback_for_writes=True)`).
Reads default to fail-closed too (`read_on_api_error`).

## Layout

- `openbox_agentmail.client` — `OpenBoxMailAgent` / `AsyncOpenBoxMailAgent` drop-in clients
- `openbox_agentmail.governor` — the single governance path (also used by relay / MCP / toolkit)
- `openbox_agentmail.approvals` — `PendingApproval` store + `ApprovalResolver` (draft mode)
- `openbox_agentmail.constraints` — the `CONSTRAIN` vocabulary applied to send args
- `openbox_agentmail.webhook_relay` — authenticated, deduplicating inbound relay (ASGI/Flask/FastAPI)
- `openbox_agentmail.websocket_inbound` — same screening over the AgentMail WebSocket channel
- `openbox_agentmail.mcp_proxy` — governed MCP proxy (HTTP + stdio)
- `openbox_agentmail.catalog` — AgentMail method → OpenBox activity catalogue
- `openbox_agentmail.contracts` — `activity_input`/`activity_output` shapes (what policies see)

A TypeScript port with the same wire contract lives in `../sdk-node/`.

## Dashboard

One governed agent = one **workflow** by default (`agent.session()` scopes it per task instead). Each AgentMail call = one **activity**
(`agentmail.<action>`). Each inbound email = one `agentmail.receive_message`
**activity** with the `messages.get` HTTP spans under it (or one **signal**
with `fetch_on_receive=False`); delivery-status events are signals. Approvals appear
in the dashboard's approval queue keyed by the same workflow/run/activity ids.
