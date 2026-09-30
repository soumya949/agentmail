# OpenBox x AgentMail SDK - Status (P0-P10 complete) and remaining roadmap

Repo: `E:\open_box\agentmail` | Python SDK: `sdk/` (`openbox-agentmail-sdk-python` 0.1.0)
Node/TS SDK: `sdk-node/` (`openbox-agentmail-sdk` 0.1.0)
Architecture: `ARCHITECTURE.md` | Base SDK: `ref/openbox-sdk-python` | Deps: `openbox-sdk-python>=1.3.1,<2`, `agentmail>=2.0,<3`

---

## Part A — What is done (P0–P10)

### Verification

- **Python:** 192 tests pass offline, `ruff check src tests` clean, `uv build` produces wheel + sdist.
- **Node/TS:** 36 tests pass (`node --experimental-transform-types --test`), `tsc --noEmit` clean.
- **Cross-language:** `sdk/tests/policy_fixtures/*.json` are generated from the Python builders and asserted byte-equivalent from the Node builders.
- **Live smoke (real credentials, read-only):** OpenBox `/api/v1/auth/validate` OK, governed `messages.list` against `<your-inbox>@agentmail.to` OK, workflow id `6c1594ee-b883-47cf-8e72-9b2ae26f18dd`. No email was sent.
- **Full live verdict matrix (Sep 30 2026, real dashboard policies, real email):**

  | | ALLOW | BLOCK | REQUIRE_APPROVAL | CONSTRAIN | Guardrail |
  |---|---|---|---|---|---|
  | **Outbound** (`send_message`) | sent | refused, nothing arrived | approve -> sent - reject -> refused | see P10, unusable from the dashboard | PII masked |
  | **Inbound** (`receive_message`) | delivered | refused before fetch | reject -> never delivered | refuses (cannot rewrite received mail) | PII masked |

  Verified `<your-inbox>@agentmail.to` against `<sender>@example.com` / `<recipient>@example.com`. A BLOCKed send produces `ActivityStarted BLOCK` with **no spans and no ActivityCompleted** - proof the AgentMail call was never built. Recipients confirmed nothing arrived.

### P0 — Core governance (REST client)

`config.py` (env resolution `OPENBOX_AGENTMAIL_*` → `OPENBOX_*`, aliases), `runtime.py` (fresh `ContextStore` per agent — the base default is process-global and would leak HALT), `adapter.py` (email-aware verdict handlers), `catalog.py` (sole classification source; `with_raw_response`/`with_options` twins stripped before lookup), `contracts.py` (activity_input/output + inbound signal builders), `governor.py` (`run`/`arun`/`screen_inbound`/`ascreen_inbound`), `redaction.py`, `client.py` (`OpenBoxMailAgent` drop-in proxy).

### P1 — Inbound relay

`webhook_relay.py`: `WebhookAuth` (AgentMail custom delivery headers — no HMAC — with rotation windows accepting old+new secrets), TTL dedupe on `event_id`, `SignalReceived` enforcement (content events enforced; delivery-status events telemetry-only), bare ASGI + `handle`/`ahandle`.

### P2 — Framework toolkits

`toolkit.governed_toolkit(framework, agent)` injects the governed proxy into `agentmail_toolkit.{openai,langchain,livekit}.AgentMailToolkit` — the toolkits call `client.inboxes.*` so every tool is governed for free. `TOOL_TYPE_MAP` exported for the OpenBox LangChain middleware path.

### P3 — MCP governed proxy

`mcp_proxy/`: `tools/call` governed via `classify_mcp_tool` (hosted tool names incl. camelCase↔snake_case argument conversion — the real server uses `inboxId`-style names), everything else relayed verbatim. Credential isolation: the proxy holds the AgentMail key, client `Authorization`/`x-api-key` never reach upstream; optional `local_token` guards the listener. Blocked calls return JSON-RPC `isError` tool results with the reason. SSE/streamed upstream responses parsed (last `data:` event). Upstream errors recorded as errored ActivityCompleted, never as success.

### P4 — Approval UX and relay hardening

- **Draft-mode approvals** (`approval_mode="draft"`): REQUIRE_APPROVAL on a send stages an AgentMail draft (redacted args, governed `create_draft` activity) and returns `PendingApproval`; `ApprovalResolver`/`resolve_pending` polls, sends with `idempotency_key = activity_id` on approval, deletes on rejection/expiry — pending items persist in a pluggable store so resolution survives restart.
- **`approval_resume` signal** emitted for every resolved approval (wait and draft modes).
- Relay ack-then-process: background queue with bounded workers, retry → dead-letter, `on_blocked` callback, `quarantine_on_blocked` helper (labels `quarantine` via a governed `update_message`).
- `hitl.skip_activity_types` honoured; `agentmail_sdk_version`/`openbox_sdk_version` on every event.
- New module: `approvals.py` (`PendingApproval`, store, resolver).

### P5 — More transports

- `websocket_inbound.py`: AgentMail WebSocket → same `ascreen_inbound` pipeline; reconnect with backoff and event dedupe.
- `mcp_proxy/stdio.py`: stdio JSON-RPC transport (stdout kept pure — logs go to stderr); `python -m openbox_agentmail.mcp_proxy --transport stdio`.
- `Mcp-Session-Id` passthrough on governed calls; `tools/list` builds a classification report (`tool_report`, `unknown_tools` — unknowns governed as writes/fail-closed).
- `relay.flask_blueprint()` / `relay.fastapi_router()` helpers (imports kept lazy — no hard deps).

### P6 — Policy depth and CONSTRAIN

- `constraints.py`: structured vocabulary — `max_recipients`, `allowed_domains`, `strip_attachments`, `force_bcc`, `require_approval`. Satisfiable constraints rewrite the real call args and are recorded as `applied_constraints` on the activity; unsatisfiable or unknown constraints still escalate to approval. Bare CONSTRAIN (no list) keeps the old escalate-to-approval posture.
- Attachment text is screened: `attachment_scan=True` carries content into `activity_input`/`activity_output`, capped by `privacy.max_body_size` (default 64 KiB chars) with `truncated` markers. Read outputs also cap `text`/`html`/`extracted_text`.
- `AgentMailBlockedError.patched_args(args)` merges a Core patch directive for retry — a fresh governed evaluation, never a bypass.
- `metadata_only` verified end-to-end: wire carries `content_sha256`, never `text`/`html`.
- `tests/policy_fixtures/` JSON fixtures for cookbook Rego/opa evaluation.

### P7 — Production readiness

- Secret-scan test: API keys/private key material asserted absent from logs and wire payloads.
- Load/latency + concurrency tests: p95 governance overhead budget, 20-call fan-out with a single `WorkflowStarted`, HALT isolation between agents sharing a Core.
- `governor.emit_handoff`/`aemit_handoff` → `EvaluationClient.emit_handoff`.
- Webhook header rotation (old+new secrets accepted).
- Catalogue coverage gate (`test_real_client_surface_is_covered`) fails on any new unclassified SDK method.
- `close()` idempotent; `WorkflowFailed` on error close.

### P8 — Node / TypeScript SDK (`sdk-node/`)

Full port: `catalog.ts`, `contracts.ts`, `constraints.ts`, `errors.ts`, `core.ts` (signed v1 wire — Ed25519 canonical `METHOD\npath\ntimestamp\nnonce\nbody_sha256`, base64 seed → PKCS8, same headers/endpoints), `governor.ts` (async-only lifecycle incl. draft mode + approvals), `client.ts` (Proxy drop-in, positional-arg naming + camel↔snake), `relay.ts` (auth/rotation/dedupe/blocked path), `mcp.ts` (tools/call governance, session id, tools/list report). Shared fixtures: Python-generated `activity_input`/`inbound_signal` JSON asserted deep-equal in Node tests.

### P9 — Inbound as a governed activity (`agentmail.receive_message`)

- Content events (webhook + websocket) are now an activity, not only a signal: `ActivityStarted` screens the pushed email (sender, SPF/DKIM headers, subject, body) → approval with a **real activity_id** (the SignalReceived path polled with `""`) → `messages.get` fetched *inside* the activity (HTTP GET child spans on the dashboard) → `ActivityCompleted` output guardrails on the fetched copy → handler gets the fetched, redacted message (`GovernedInbound.activity_id`).
- `fetch_on_receive=True` default on `InboundRelay` / `WebsocketInbound`; `False` keeps SignalReceived. Status events stay signals. Governor API: `receive_inbound` / `areceive_inbound` → `ReceivedMessage`.
- Fail closed everywhere: Core outage or fetch failure (`InboundFetchError`) → never delivered. Webhook: 503; **fixed** a pre-existing bug where the 503/500 left the event_id in dedupe so AgentMail's retry was dropped as "duplicate" (`MemoryDedupe.forget`). WebSocket: local `screen_retries` then `on_dead_letter(event, error)`.
- Not yet ported to `sdk-node/relay.ts` (still SignalReceived).

**Node parity (Sep 30 2026):** the P10 *fixes* are ported — bugs 2-5 above are
closed in `sdk-node/` (it never had bug 1, since it never gated polling on
`approval_id`), plus two Node-only defects: the default dedupe was rebuilt per
request so it never deduplicated, and a 5xx left the id in dedupe so AgentMail's
redelivery was swallowed. 44 tests, `tsc` clean. The P9/P10 *features* are still
Python-only: no `receive_inbound`/fetch-on-receive, no `emit_trigger`, no
session scoping, no WebSocket transport.

### P10 - Live hardening (Sep 30 2026): five bugs only a real Core could expose

Every one of these passed the offline suite and failed against the live dashboard. The offline fakes
agreed with our assumptions; Core did not.

1. **Approvals never worked at all.** Core returns `REQUIRE_APPROVAL` **without an `approval_id`**, and
   `adapter._require_poller` treated that as unpollable - auto-rejecting instantly, with
   `approval polls attempted: 0`. The poll is keyed by `(workflow_id, run_id, activity_id)`;
   `approval_id` is not used for it. Every approval in production would have failed closed. Only a
   missing poller refuses now. *This overturned `test_require_approval_no_approval_id_fails_safe`,
   which encoded the old behaviour deliberately - its premise (no id implies no approval exists) is
   false against real Core.*
2. **Errored activities vanished.** Core rejects an `ActivityCompleted` carrying a top-level `error`
   field with `HTTP 400 "invalid request body"` - **any** error, verified down to a one-word string,
   with or without `activity_output`. The whole event was dropped, so a failed send left a dangling
   `ActivityStarted` forever. Failures are what an audit trail most needs. The failure now rides inside
   `activity_output` (`failed: true`, `status: "failed"`, `error`), a shape confirmed 200 before any
   code was written. `_error_text()` also bounds it to 1000 printable chars - some SDK errors repr to
   kilobytes of headers with undecodable bytes.
3. **CONSTRAIN hung the caller forever.** Core registers an approval for `REQUIRE_APPROVAL` **only**,
   yet an unsatisfiable CONSTRAIN fell through to the approval poll - which with the default
   `hitl.max_wait_ms=None` polls indefinitely. In a service that is a permanently stuck request and a
   thread leak. An unsatisfiable CONSTRAIN now fails closed immediately and **never polls**; the tests
   assert `core.approval_requests == []`. *This overturned four tests asserting escalate-to-approval.*
4. **The same hang on the inbound path** (`_receive_needs_approval` and the legacy SignalReceived
   path) - closed pre-emptively before it could be hit.
5. **Constraints are bare strings, not objects.** Core sends `["run_in_sandbox"]`; `apply_constraints`
   required mappings and reported every string as "unrecognised". Now normalised, so
   `["strip_attachments"]` works in string form. `run_in_sandbox` is named explicitly as inapplicable
   to email.

New capability in the same pass:

- **`emit_trigger()` / `aemit_trigger()`** - the SDK cannot observe *why* an agent acted (an API
  request, a bot, a cron tick), so the caller declares it and it is recorded as a real
  `SignalReceived` ordered ahead of the activities it caused. Nothing is ever emitted automatically:
  a fabricated event in an audit trail is worse than a missing one. BLOCK/HALT on a trigger are
  enforced; `REQUIRE_APPROVAL` fails closed with guidance, because a signal envelope has no
  `activity_id` to key an approval poll on. Caller data is namespaced under `trigger_data` so it
  cannot forge the fields policy matches on.
- **`agent.session()` / `async with agent.session()`** - scopes one workflow to a block of work, so
  each task/request/email is its own `WorkflowStarted ... WorkflowCompleted`. Trade-off: behavioural
  rules only match prior activity *within* a session. HALT deliberately survives session boundaries.
- **`arrival_signal` / `session_per_message`** on `InboundRelay` and `WebsocketInbound`, both default
  off. `arrival_signal` is presentational - the activity already carries the same arrival fields, and
  enforcement stays on the activity.
- **`examples/api_server.py`** - a stdlib governed send endpoint mapping verdicts onto HTTP status
  (403 policy refused, 502 AgentMail refused, 202 pending approval, 503 halted). The 403/502 split
  matters: *we* said no versus *the provider* said no.

### Issues found and fixed along the way

- **P0 audit:** session ordering (activity built before `WorkflowStarted` → empty ids), process-global `ContextStore` leaking HALT across agents, `with_raw_response`/`with_options` refused, over-strict `approval_id` requirement, NameError in error telemetry.
- **P3 audit (live):** hosted MCP uses camelCase args (`inboxId`) — proxy converts both ways; upstream URL needed `/mcp`; client credentials were forwarded upstream (now stripped + server-held key); blocked calls didn't return MCP tool errors; SSE responses unparsable; upstream errors recorded as success.
- **P4:** `_bind` nested `**kwargs` instead of expanding (broke `add_labels=` quarantine calls); draft-mode previously fell back silently to wait (now real, or `OpenBoxConfigError` if impossible).
- **P5:** stdio transport keeps stdout pure for the protocol channel; websocket reconnect treats exhausted test factories as terminal.
- **P6:** bare `CONSTRAIN` (empty constraints) still escalates — an unrecognized constraint is never silently ignored. *(Superseded in P10: it now fails closed rather than escalating, because Core registers no approval to escalate to.)*
- **P10 (dashboard, not code):** an **ALLOW rule ordered above a BLOCK rule silently disables it** - no warning, and it cost two debugging rounds. Rule precedence is list order, not verdict severity. Deploying any rule regenerates the whole Rego policy, so an unrelated edit can reorder and silently disable a control.

---

## Part B — What remains (dashboard/live-dependent and upstream items)

Everything implementable offline is built. What remains needs either a real dashboard policy, a published package, or an upstream change:

1. **Live draft-approval walkthrough** — add a "require approval" rule in the dashboard, send from `<your-inbox>@agentmail.to` to `<recipient>@example.com`, approve in the UI, confirm delivery + `approval_resume` in Session Replay. This is a real send; run when you want it.
2. **`opa test` against `tests/policy_fixtures/`** — needs the `opa` binary; the fixtures are generated and the cookbook Rego is in `docs/policy-cookbook.md`.
3. **Upstream PR to `openbox-sdk-python`:** add `activity_output=` to `activity_completed` (currently passed via `extra`).
3b. **Core-side issues to raise (all found live, Sep 30 2026):**
   - `activity_completed(error=...)` is in the base SDK public API but Core rejects it with HTTP 400 - an SDK/Core contract mismatch. **Revert the `activity_output` workaround once fixed.**
   - `EvaluationClient._parse_evaluate_response` discards the 4xx response body, logging only the status. Recovering Core actual message required monkey-patching it; logging `response.text` would have saved an hour.
   - **CONSTRAIN is unauthorable from the rule builder** - it offers the decision but no way to specify constraints, so it can only emit `["run_in_sandbox"]` and produce a refusal. Until fixed, tell users to use REQUIRE APPROVAL or BLOCK for mail.
   - **BLOCK AND PATCH is `llm_call`-only** - hard-pinned to `activity_type == llm_call` with an LLM-routing patch vocabulary (providers, regions), so it can never match an email action. `AgentMailBlockedError.patched_args()` implements the generic contract correctly but is **unreachable** until Core supports patch for non-LLM activities.
   - An ALLOW rule above a BLOCK rule silently disables it, with no warning in the UI.
4. **CI matrix** (Python 3.11–3.13, Node 20/22/24) and PyPI/npm publish.
5. **Live soak:** 1 h mixed traffic, every `WorkflowStarted` has a close.
5b. **Blocked on credentials/config, not code:**
   - `quarantine_on_blocked` needs an AgentMail key with the **`message_update`** permission (currently 403 `missing_permission`). The label path itself works - the error is surfaced and the listener survives it.
   - Draft-mode approval (`approval_mode="draft"`) still unexercised live.
   - Guardrail scoping: the PII guardrail ran on **send** responses too, masking the returned `message_id`. Set its Activity Type to `agentmail.receive_message`.
6. **Workaround watch:** Node SDK signs v1 requests itself; if OpenBox ships a TypeScript core SDK, `sdk-node/src/core.ts` should delegate to it.

## How to run what exists today

```powershell
cd E:\open_box\agentmail\sdk
.venv\Scripts\python.exe -m pytest tests -q        # 192 tests, offline
.venv\Scripts\python.exe examples\live_smoke.py    # live, read-only, uses .env

cd ..\sdk-node
npm test                                           # 36 tests, offline
```
