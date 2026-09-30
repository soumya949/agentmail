# Architecture — OpenBox × AgentMail SDK (`openbox-agentmail-sdk-python`)

**Status:** v2. Verified against source code. Supersedes PRD §4–§6 where they conflict.
**Date:** Sep 26, 2026
**Verified against:**
- `OpenBox-AI/openbox-sdk-python` @ 1.3.1 (`openbox_core`): the base SDK this package is built on
- `OpenBox-AI/openbox-crewai-sdk-python` @ 1.0.0: reference for UX, lifecycle and guardrail handling
- `openbox-docs.pdf` (docs.openbox.ai): verdict semantics, dashboard, Rego policy input
- docs.agentmail.to, `agentmail-python`, `agentmail-mcp`, `agentmail-toolkit`

---

## 0. PRD review: verified vs. corrected

### 0.1 Confirmed by the base SDK source (keep as written)
| PRD claim | Evidence |
|---|---|
| Evaluate endpoint `POST /api/v1/governance/evaluate` | `openbox_core/client.py` `EVALUATE_PATH`. Also `/api/v1/governance/approval`, `/api/v1/auth/validate`, `/api/v1/handoffs`. v2 (Okta) and v3 (workload) paths are selected automatically by identity |
| Build on `openbox-sdk-python` / `openbox_core`, never reimplement | `.github/instructions/openbox-sdk-python.instructions.md` ("Do Not Reimplement" list) |
| Always-strict gate: `ContractError` before any network call, no loose mode | `openbox_core/gate.py`, `validation/event_rules.py` |
| AIP DID + Ed25519 signing | `openbox_core/identity.py`. Framework SDKs must **not** sign directly |
| `EventEnvelope`, `ActivityContext`, `ContextStore`, `FrameworkAdapter` | `contracts/events.py`, `contracts/context.py`, `context.py`, `adapters/base.py` |

### 0.2 Corrections
| # | PRD says | Source of truth says | Fix |
|---|---|---|---|
| 1 | Verdicts `ALLOW / DENY / REQUIRES_APPROVAL` | `Verdict` enum has **5 tiers**: `ALLOW`, `CONSTRAIN`, `REQUIRE_APPROVAL`, `BLOCK`, `HALT`. Priority `HALT > BLOCK > guardrails-fail > REQUIRE_APPROVAL > CONSTRAIN > ALLOW`. Rego `CONTINUE` → `ALLOW`, `STOP` → `HALT` | Handle all 5 (§5) |
| 2 | Custom "EventEnvelope (agent identity, action type, recipient, risk signals)" | `EventEnvelope` is a generic wire type built **only** via factories: `workflow_started/completed/failed`, `activity_started/completed`, `signal_received`, `handoff`, `hook`. Required: `workflow_id`, `run_id`, `workflow_type` (+ `signal_name` for signals) | AgentMail data goes inside `activity_input` / `activity_output` / signal fields (§3). No new wire type |
| 3 | Error names `OpenBoxDeniedError` | Base SDK already has `GovernanceBlockedError`, `GovernanceHaltError`, `GuardrailsValidationError`, `GovernanceAPIError`, `ApprovalRejectedError`, `ApprovalExpiredError`, `ApprovalTimeoutError`, `ContractError`, `OpenBoxAuthError` | Subclass or re-export these. Don't invent parallel names (§5.3) |
| 4 | `OPENBOX_FAILURE_MODE=closed\|open`, default closed | Base field is `on_api_error` (`fail_open`\|`fail_closed`), env `<PREFIX>_ON_API_ERROR`, **base default `fail_open`**. HTTP 401/403 always fail closed regardless (1.3.0 security fix) | Use `on_api_error`. This SDK passes `fail_closed` explicitly as its default (email is irreversible) |
| 5 | Env vars `OPENBOX_URL`, `OPENBOX_MAILAGENT_DID`, `OPENBOX_MAILAGENT_PRIVATE_KEY`, "identical to CrewAI" | `OpenBoxConfig.resolve(env_prefix=...)` reads `<PREFIX>_API_URL`, `_API_KEY`, `_AGENT_DID`, `_AGENT_PRIVATE_KEY`, `_ON_API_ERROR`, `_TIMEOUT_SECONDS`, `_AGENT_NAME`, and falls back to `OPENBOX_*`. **CrewAI 1.0.0 doesn't use `openbox_core`**: it vendors its own `openbox/core` with `OPENBOX_URL` + `{PREFIX}_DID/_PRIVATE_KEY`. The two families currently differ | Follow `openbox_core` (the future direction). Accept CrewAI-style names as **aliases** in `config.py`, mapped to explicit `resolve()` args (§7) |
| 6 | Only allow/deny | `EvaluationResult.guardrails.redacted_input` + `input_type` (`activity_input`\|`activity_output`) carry **redacted data**. `validation_passed=false` must raise `GuardrailsValidationError`. CrewAI applies redaction on both sides | Send redacted input to AgentMail. Return redacted output to the agent (§5.1) |
| 7 | "SDK polls or receives webhook" for approval | `OpenBoxRuntime.evaluate_lifecycle` (sync) **returns** `REQUIRE_APPROVAL` without driving it. Only `aevaluate_lifecycle` calls `adapter.handle_approval`. Polling is `ApprovalPoller` → `POST /api/v1/governance/approval` keyed by `(workflow_id, run_id, activity_id)` | Sync path drives `ApprovalPoller.wait_for_decision`. Async path goes through the adapter (§6) |
| 8 | Governs outbound + inbound webhooks only | Read tools (`get_message`, `get_thread`, `list_*`, `get_attachment`) also inject untrusted mail into the agent | Govern reads on `ActivityCompleted` (output guardrails) |
| 9 | "OTel telemetry pipeline" automatic | Base `install_instrumentation()` patches httpx/requests/DB/file. Hooks fire **only inside a bound `ActivityContext`** (`hooks/preflight.py`: "no bound context → skip silently"), and the OpenBox Core URL is ignored automatically | HTTP instrumentation **on by default**, so each `agentmail.*` Activity shows the AgentMail HTTP request/response as child spans, the same shape as LLM spans under an Activity in other SDKs. DB/file off by default (§8) |
| 10 | MCP has 24 fixed tools. Toolkit = `langchain-agentmail`. 4 webhook events | Hosted MCP serves a runtime tool catalog. `agentmail-toolkit` targets OpenAI Agents SDK / Vercel AI / MCP. 10 webhook events + WebSocket channel | Dynamic MCP classification. Governed-client injection into the toolkit. All 10 events handled (§4) |
| 11 | Python version unspecified | `openbox-sdk-python` requires **Python ≥ 3.11** (CrewAI SDK allows 3.10 only because it vendors) | `requires-python = ">=3.11"` |

Everything else in the PRD is sound and kept: the problem statement, layering on top of AgentMail key permissions, zero hardcoded rules, SDK/frontend separation, and the phasing idea.

---

## 1. Design principles

1. **Thin adapter over `openbox_core`.** This package only (a) maps AgentMail operations to `ActivityContext` + event factories, (b) implements `FrameworkAdapter`, and (c) applies verdicts and redaction to AgentMail calls. Signing, HTTP, validation, fail-mode, result parsing, approval polling and redaction/truncation all belong to `openbox_core`.
2. **OpenBox decides, the SDK enforces.** No policy in code. Everything is authored on the dashboard.
3. **One choke point.** REST client, toolkit, MCP proxy and inbound relay all call one `MailGovernor`.
4. **Input and output governed.** `ActivityStarted` runs before the call (policy + input guardrails). `ActivityCompleted` runs after (output guardrails, injection screening, trust).
5. **Irreversibility-aware.** Sends can't be undone. Completed-stage verdicts only affect **future** work (a base SDK invariant). Blocking rules for sends must live on `ActivityStarted`.
6. **Import-safe package root.** `import openbox_agentmail` doesn't import httpx/cryptography/OTel (same rule and test as the base SDK).

---

## 2. System context

```
                      ┌─────────────────────────────────────────────────┐
                      │                 OpenBox Core                     │
                      │ /api/v1/governance/evaluate · /approval ·        │
                      │ /auth/validate   Guardrails → Policy (OPA/Rego)  │
                      │ → Behavioral rules → Verdict    Dashboard/Audit  │
                      └───────▲─────────────────────────┬────────────────┘
        signed (DID/Ed25519)  │ EventEnvelope           │ EvaluationResult
        by openbox_core       │ (flat wire fields)      │ (+ guardrails.redacted_input)
 ┌──────────────┐        ┌────┴─────────────────────────▼─────────────────────┐        ┌──────────────┐
 │ Agent        │ call   │ openbox-agentmail-sdk                               │ REST / │ AgentMail    │
 │ (any runtime)│───────►│  surfaces: OpenBoxMailAgent · toolkit · MCP proxy · │  MCP   │ API / MCP    │
 │              │◄───────│            inbound relay                            │───────►│              │
 └──────────────┘ result │           │                                         │◄───────│              │
                         │           ▼                                         │        └──────┬───────┘
                         │  MailGovernor (catalog → ActivityContext → events)  │  webhook /    │
                         │           │                                         │  websocket    │
                         │           ▼                                         │◄──────────────┘
                         │  openbox_core: OpenBoxRuntime → GovernanceGate →    │
                         │  EvaluationClient · ApprovalPoller · ContextStore   │
                         │  AgentMailAdapter (FrameworkAdapter impl)           │
                         └─────────────────────────────────────────────────────┘
```

---

## 3. Event model: AgentMail onto `openbox_core` factories

### 3.1 Identity mapping

| `openbox_core` field | AgentMail value |
|---|---|
| `workflow_type` | `"AgentMail Agent"` (or `f"{agent_name} Agent"`, matching CrewAI's `"<role> Agent"`) |
| `workflow_id` | UUID per governed agent session |
| `run_id` | UUID per session start (new after halt/close) |
| `task_queue` | `inbox_id` (keeps per-inbox filtering in the dashboard) |
| `activity_id` | UUID per AgentMail operation. Also used as the AgentMail idempotency key |
| `activity_type` | `agentmail.<action>` (e.g. `agentmail.send_message`) |
| `agent_name` | from config (`<PREFIX>_AGENT_NAME`) |
| `session_id` / `multi_agent_session_id` | passed through if the host framework supplies them |
| `metadata` | `{pod_id, inbox_id, surface: "rest"\|"toolkit"\|"mcp"\|"inbound", agentmail_sdk_version}` |

### 3.2 Event sequence

| When | Factory | Enforced? |
|---|---|---|
| First governed call in a session | `events.workflow_started(...)` | yes (BLOCK/HALT refuses the session) |
| Before each AgentMail operation | `events.activity_started(..., activity_input=<§3.4>)` | **yes**: gate for the real call |
| After the AgentMail response | `events.activity_completed(..., extra={"activity_output": <§3.5>})`. On failure the error rides INSIDE `activity_output` (`failed: true`, `status`, `error`) — Core rejects a top-level `error=` with HTTP 400 and drops the whole event | output redaction / withhold. Stop verdicts affect future work only |
| Inbound `message.received*` | `events.signal_received(signal_name="agentmail.message_received", extra={...})` | **yes**: gates the developer handler |
| Inbound delivery events (`message.sent/delivered/bounced/complained/rejected`, `domain.verified`) | `events.signal_received(signal_name="agentmail.<event>")` | telemetry only (verdict recorded, never blocks) |
| Approval resolved | `events.signal_received(signal_name="approval_resume")` | telemetry |
| Session close / crash | `events.workflow_completed(extra={"status": "completed"\|"halted"})` / `workflow_failed(error=...)` | telemetry |

**Decision: the wire field is `activity_output`.** Core's output guardrails (`input_type="activity_output"`), Rego (`input.activity_output`), the dashboard, the CrewAI SDK and the LLM-completion payloads of other SDKs all use `activity_output`. The `activity_completed` factory's `result=` parameter is **not used**; `lifecycle.py` passes `extra={"activity_output": {...}}`. `activity_output` is always an object (`{"result": ...}` for scalar returns) so it matches CrewAI's shape. A follow-up PR to `openbox-sdk-python` adding an `activity_output=` parameter to the factory is worthwhile but not a blocker.

**Timeline shape per operation** (what the dashboard shows, matching the pattern you've seen with LLM calls):

```
ActivityStarted   agentmail.send_message      activity_input = {...}          ← policy + input guardrails
  └─ hook span    http_request  stage=started   POST api.agentmail.to/...     ← telemetry (hook_trigger=true)
  └─ hook span    http_request  stage=completed status=200, response body     ← telemetry
ActivityCompleted agentmail.send_message      activity_output = {...}         ← output guardrails
```
The hook spans come from `openbox_core`'s HTTP instrumentation (§8). Policy should be written against the Activity boundary; the spans are evidence.

### 3.3 Action catalogue (`catalog.py`, single source for all surfaces)

| `activity_type` | Class | Outage behaviour | Notes |
|---|---|---|---|
| `agentmail.send_message` | `send` | block | |
| `agentmail.reply_to_message` / `reply_all` | `send` | block | |
| `agentmail.forward_message` | `send` | block | |
| `agentmail.send_draft` | `send` | block | |
| `agentmail.create_draft` / `update_draft` | `draft` | block | includes `send_at` |
| `agentmail.delete_draft` / `delete_message` / `delete_thread` | `modify` | block | |
| `agentmail.update_message` (labels) | `modify` | block | |
| `agentmail.create_inbox` / `delete_inbox` / `*webhook*` / `*domain*` / `*api_key*` | `admin` | block | |
| `agentmail.list_* / get_* / search_*` | `read` | `read_on_api_error` | output screened |
| `agentmail.get_attachment` | `read_attachment` | `read_on_api_error` | bytes only if `attachment_scan` |
| MCP tool not in catalogue | `unknown` | block | never silently passed through |

Overrides are allowed via `tool_type_map` (the same name the LangChain/DeepAgents SDKs use).

### 3.4 `activity_input` (policy authors write Rego against this)

```json
{
  "action": "send_message",
  "inbox_id": "agent@domain.com",
  "pod_id": "pod_abc",
  "to": ["alice@example.com"], "cc": [], "bcc": [],
  "recipient_domains": ["example.com"],
  "external_recipients": true,
  "recipient_count": 1,
  "subject": "Following up",
  "text": "…",
  "html": null,
  "content_sha256": "…",
  "reply_to_message_id": null,
  "thread_id": null,
  "attachments": [{"filename": "q3.pdf", "content_type": "application/pdf", "size": 48213}],
  "send_at": null,
  "labels": []
}
```
- `content_mode="full"` (default) includes `text`/`html`, because guardrails can't scan a hash. `metadata_only` sends `content_sha256` only.
- Size and redaction limits are enforced by `openbox_core` `PrivacyConfig` (`max_body_size`, `redact_keys`) before signing. The SDK doesn't truncate itself.
- `external_recipients` is computed against `internal_domains` config (default: the inbox's own domain).

### 3.5 `activity_output`
- Writes: `{"message_id", "thread_id", "status"}`
- Reads: the returned objects (subject, from, text/extracted_text, labels, auth results), so output guardrails can mask PII or flag injection **before the agent sees them**
- Attachments: metadata plus extracted text (AgentMail extracts PDF/DOCX text). Bytes only with `attachment_scan=True`

### 3.6 Example dashboard policies

```rego
package openbox
default result := {"decision": "CONTINUE", "reason": ""}

result := {"decision": "REQUIRE_APPROVAL", "reason": "External email requires approval"} if {
  input.event_type == "ActivityStarted"
  startswith(input.activity_type, "agentmail.")
  input.activity_input.external_recipients
}

result := {"decision": "BLOCK", "reason": "Bulk send over limit"} if {
  input.activity_type == "agentmail.send_message"
  input.activity_input.recipient_count > 20
}
```
Core evaluates **policy before guardrails**. A non-ALLOW policy verdict can skip guardrails (CrewAI docs caveat). The policy cookbook must say so.

---

## 4. Access surfaces

### 4.1 REST: `OpenBoxMailAgent` (P0)
A composition proxy over `agentmail.AgentMail` / `AsyncAgentMail`. It mirrors the native resource tree, so migration is a one-line change:

```python
from openbox_agentmail import create_openbox_mail_agent

with create_openbox_mail_agent(env_prefix="OPENBOX_MAILAGENT") as mail:
    mail.inboxes.messages.send(inbox_id="agent@domain.com",
                               to="alice@example.com", subject="Hi", text="…")
```
- Every method resolves through `catalog.py`. **Uncatalogued methods raise `UncataloguedActionError`** (subclass of `OpenBoxConfigError`) (fail safe when AgentMail adds endpoints). A CI test introspects the pinned `agentmail` client for coverage.
- The async variant uses `aevaluate_lifecycle` and the adapter's `handle_approval`.

### 4.2 Framework toolkits (P2)
- `agentmail-toolkit` tools wrap the REST client, so we **inject the governed client** rather than reimplement tools: `governed_toolkit(framework="openai", ...)`.
- LangChain/LangGraph/DeepAgents users already running `openbox-langchain` / `openbox-langgraph` middleware are governed at the tool boundary already. For them we export `TOOL_TYPE_MAP` and recommend **not** double-wrapping, to avoid duplicate Activities and approvals.

### 4.3 MCP governed proxy (P3)
```
MCP client ──► openbox-agentmail-mcp (streamable HTTP | stdio) ──► https://mcp.agentmail.to/mcp
```
- `tools/list`: forwarded and cached. Tools are classified via the catalogue (unknown → `unknown`, blocked).
- `tools/call`: `MailGovernor.run()` with an executor that forwards upstream. BLOCK/HALT/guardrail failure become an MCP result with `isError: true` and a redacted reason (the same approach as the CopilotKit SDK's `governance_blocked` frame).
- The proxy holds the AgentMail key (`x-api-key` upstream). Clients authenticate to the proxy with a local bearer token.

### 4.4 Inbound relay (P1)
```
AgentMail ─webhook─► /agentmail/webhook ─► verify auth ─► 200 ack ─► queue ─► SignalReceived ─► handler
AgentMail ─websocket─► subscriber ───────────────────────────────► SignalReceived ─► handler
```
```python
from openbox_agentmail.inbound import InboundRelay
relay = InboundRelay(env_prefix="OPENBOX_MAILAGENT", webhook_secret=os.environ["AGENTMAIL_WEBHOOK_SECRET"])

@relay.on_message
def handle(msg): ...        # only runs if allowed; msg may be guardrail-redacted

app = relay.asgi()      # or relay.flask_blueprint()
```
- Delivery auth is verified first: AgentMail custom delivery `headers` (constant-time compare), plus signature verification if enabled on the account. Failure returns 401 and nothing is evaluated.
- Ack fast, then process async. Dedupe on `event_id` (pluggable store, in-memory by default).
- Signal payload: sender, domain, SPF/DKIM/DMARC results, labels, subject, body per `content_mode`, attachment metadata, inbox/thread ids.
- Verdicts: ALLOW → handler(redacted msg). BLOCK → handler skipped, optional `on_blocked` (e.g. apply the AgentMail label `quarantine`). REQUIRE_APPROVAL → held until approved. HALT → session halted.
- Injection detection lives in dashboard guardrails. The SDK only supplies the fields.

---

## 5. Enforcement: `MailGovernor` + `AgentMailAdapter`

### 5.1 Algorithm (sync; async is identical with `a*` calls)

```
run(action, args, executor):
  0. if context_store halt requested  → raise AgentMailHaltedError        (no network)
  1. spec = catalog.lookup(action)     → unknown: OpenBoxConfigError
     activity_input = contracts.build_input(spec, args)   # local validation → ContractError
  2. ensure_session()                  → runtime.evaluate_lifecycle(workflow_started)
  3. ctx = ActivityContext(... activity_type=spec.activity_type, activity_input=...)
     with activity_scope(ctx, store=runtime.context_store):
  4.   r1 = runtime.evaluate_lifecycle(events.activity_started(...))
          # runtime already: HALT → request_halt + adapter.raise_lifecycle_blocked
          #                  BLOCK → adapter.raise_lifecycle_blocked
          #                  guardrails.validation_passed=false → GuardrailsValidationError
          # network error: on_api_error (fail_closed → GovernanceAPIError);
          #                401/403 → OpenBoxAuthError (always)
          # r1.fallback_used and spec.class != read → treat as fail_closed
  5.   if r1.verdict is REQUIRE_APPROVAL: approvals.resolve(r1, ctx)   # §6
       if r1.verdict is CONSTRAIN:       writes → apply satisfiable constraints else raise AgentMailBlockedError; reads → proceed + log (§13)
  6.   args = apply_redaction(args, r1.guardrails, "activity_input")
  7.   try: result = executor(args, idempotency_key=ctx.activity_id)
       except AgentMailError as e:
            runtime.gate.evaluate(activity_completed(error=str(e)))  # telemetry
            raise
  8.   r2 = runtime.gate.evaluate(activity_completed(extra={"activity_output": serialize(result)}))
  9.   if r2 stop-shaped:
           mark future work blocked (HALT → request_halt)
           if spec.class is read → raise (the agent never sees the content)
           else → return result, flagged governance_warning (the send already happened)
       return apply_redaction(result, r2.guardrails, "activity_output")
```

Notes:
- Step 8 uses `gate.evaluate` directly, not `runtime.evaluate_lifecycle`, so a completed-stage BLOCK is handled by step 9 rather than raised generically. This respects the invariant "completed never undoes work".
- **fail-open safety net:** even if a user sets `on_api_error=fail_open`, `send`/`draft`/`modify`/`admin` classes refuse `fallback_used=True` results unless `allow_fallback_for_writes=True` is set explicitly.
- `CONSTRAIN` writes apply satisfiable constraints (`constraints.py`); unsatisfiable, unknown or bare CONSTRAIN raises `AgentMailBlockedError` (fail safe, never escalates — Core registers approvals for `REQUIRE_APPROVAL` only). Reads proceed with the constraints logged. See §13.

### 5.2 `AgentMailAdapter` (implements `FrameworkAdapter`)

| Callback | Behaviour |
|---|---|
| `name` | `"agentmail"` |
| `raise_lifecycle_blocked(result)` | HALT → `AgentMailHaltedError`, BLOCK → `AgentMailBlockedError` (with `reason`, `policy_id`, `governance_event_id`, `activity_type`) |
| `raise_hook_blocked(result)` | same as above (only relevant if the user opts into instrumentation) |
| `handle_approval(result, context)` (async) | `ApprovalPoller.await_decision(...)` or draft-mode (§6) |
| `handle_approval_sync(result, context)` | `ApprovalPoller.wait_for_decision(...)` or draft-mode |
| `on_completed_hook_result(result, context)` | stop verdict → `context_store.request_halt()` / mark session. Never undoes |

MCP and toolkit surfaces catch these errors and convert them to their native error shape. The adapter itself stays surface-agnostic.

### 5.3 Errors (`openbox_agentmail.errors`)

```
openbox_core.OpenBoxError
├── ContractError                       (re-export)
├── OpenBoxConfigError / OpenBoxAuthError (re-export)
├── GovernanceAPIError                  (re-export; fail_closed outage)
├── GuardrailsValidationError           (re-export)
├── GovernanceBlockedError ─► AgentMailBlockedError      (+ activity_type, patch directive)
├── GovernanceHaltError    ─► AgentMailHaltedError
├── ApprovalRejectedError / ApprovalExpiredError / ApprovalTimeoutError (re-export)
```
`AgentMailBlockedError.patch` exposes `openbox_core.contracts.results.handle_patch(result)`, an optional remediation hint (e.g. a suggested redacted body). It is **never auto-applied**. The agent can choose to retry with it, which triggers a fresh evaluation.

---

## 6. Human-in-the-loop

`REQUIRE_APPROVAL` → the request appears in the dashboard **Approvals** queue with full `activity_input`. Session Replay shows the draft email.

| `approval_mode` | Behaviour |
|---|---|
| `"wait"` (default) | Poll `/api/v1/governance/approval` via `ApprovalPoller` (`hitl.poll_interval_ms`, `hitl.max_wait_ms`) until approved (proceed), rejected (`ApprovalRejectedError`), expired (`ApprovalExpiredError`), or out of budget / unreachable (`ApprovalTimeoutError`, fail safe) |
| `"draft"` (sends only) | Create an **AgentMail draft** from the (redacted) args and return `PendingApproval(draft_id, approval_id, activity_id)` right away. A background resolver polls: approve → `send_draft` (idempotency key = original `activity_id`), reject or expire → `delete_draft`. Emits `SignalReceived("approval_resume")` |

`hitl.enabled=False` means REQUIRE_APPROVAL is **rejected** (the base `CoreAdapter` fail-safe semantics), never auto-allowed. `hitl.skip_activity_types` is honoured.

---

## 7. Configuration

### 7.1 Environment (resolved by `OpenBoxConfig.resolve(env_prefix=...)`)

```bash
# openbox_core canonical names (prefix-specific first, then global OPENBOX_*)
OPENBOX_MAILAGENT_API_URL=https://core.openbox.ai      # or global OPENBOX_API_URL
OPENBOX_MAILAGENT_API_KEY=obx_live_...
OPENBOX_MAILAGENT_AGENT_DID=did:aip:...
OPENBOX_MAILAGENT_AGENT_PRIVATE_KEY=<base64 32-byte ed25519 seed>
OPENBOX_MAILAGENT_ON_API_ERROR=fail_closed             # SDK passes fail_closed if unset
OPENBOX_MAILAGENT_TIMEOUT_SECONDS=30
OPENBOX_MAILAGENT_AGENT_NAME=SupportMailAgent

# AgentMail
AGENTMAIL_API_KEY=am_...                 # use a scoped, permission-restricted key
AGENTMAIL_WEBHOOK_SECRET=...             # inbound relay only
```
**CrewAI-compat aliases** (read by `config.py` only when the canonical name is missing, passed as explicit args to `resolve()`, and a deprecation warning is logged): `OPENBOX_URL` → `api_url`, `{PREFIX}_DID` → `agent_did`, `{PREFIX}_PRIVATE_KEY` → `agent_private_key`.
Okta (`okta_ai_agent`) and workload identity work with no changes, because `openbox_core` handles them.

### 7.2 Factory: `create_openbox_mail_agent(**opts)`

| Option | Default | Maps to |
|---|---|---|
| `env_prefix` | `"OPENBOX_AGENTMAIL"` | `OpenBoxConfig.resolve(env_prefix=)` |
| `agentmail_client` | built from `AGENTMAIL_API_KEY` | wrapped client (bring-your-own supported) |
| `agent_name` | `"AgentMailAgent"` | `agent_name` |
| `on_api_error` | `"fail_closed"` | base `on_api_error` (writes) |
| `read_on_api_error` | `"fail_closed"` | SDK-level override for `read*` classes |
| `allow_fallback_for_writes` | `False` | §5.1 safety net |
| `inbox_ids` / `pod_id` | `None` | local allow-scope check + metadata |
| `internal_domains` | inbox domain(s) | `external_recipients` computation |
| `content_mode` | `"full"` | `full` \| `metadata_only` |
| `attachment_scan` | `False` | include attachment bytes |
| `approval_mode` | `"wait"` | §6 |
| `hitl` | base defaults (`enabled=True`, `poll_interval_ms=5000`) | `HitlConfig` |
| `privacy` | base defaults (`max_body_size=65536`) | `PrivacyConfig` |
| `tool_type_map` | `{}` | catalogue overrides |
| `instrumentation` | HTTP on, DB/file/function off | `InstrumentationConfig` (§8) |
| `validate` | `True` | `client.validate_api_key()` at startup (`/api/v1/auth/validate`) |

**Multi-inbox:** one instance = one OpenBox agent identity (DID), which may span several inboxes within the AgentMail key scope. `inbox_id` is on every event (`task_queue` + input), so per-inbox policy is possible. Use a separate `env_prefix` for separate trust scores.

**Multi-agent:** if a host framework hands work between governed agents, use `openbox_core` `handoff(...)` via `EvaluationClient.emit_handoff`. We don't implement it ourselves.

---

## 8. Instrumentation stance

- **Default: HTTP instrumentation on, DB/file/function off** (`InstrumentationConfig(http_enabled=True, db_enabled=False, file_enabled=False, function_enabled=False)`). `agentmail-python` uses httpx, so every AgentMail call inside an `activity_scope` produces started/completed `http_request` hook spans under the owning Activity, which is the standard OpenBox timeline shape.
- **Safe for host apps:** `HookRuntime` skips any HTTP call with no bound `ActivityContext`, so the host application's unrelated traffic is neither governed nor reported. The OpenBox Core URL is excluded automatically by the base.
- **No duplicate approvals:** the policy cookbook tells authors to gate `ActivityStarted`/`ActivityCompleted` and treat `hook_trigger=true` payloads as telemetry (the same guidance the CrewAI docs give). `hitl.skip_activity_types` is not needed because hooks and Activities share the same `activity_id`.
- Users can turn off HTTP spans (`instrumentation={"http_enabled": False}`) or enable DB/file capture if their agent does more than email.
- `runtime.close()` / `aclose()` is called on context-manager exit and in relay/proxy shutdown hooks.

---

## 9. Package layout

```
agentmail/
├── sdk/                                   # publishable Python SDK
│   ├── src/openbox_agentmail/
│   │   ├── __init__.py                    # light exports only (import-safe)
│   │   ├── py.typed                       # PEP 561 marker
│   │   ├── config.py                      # opts + CrewAI aliases -> OpenBoxConfig.resolve
│   │   ├── runtime.py                     # build_runtime(adapter, fresh ContextStore)
│   │   ├── adapter.py                     # AgentMailAdapter (FrameworkAdapter)
│   │   ├── catalog.py                     # action -> activity_type / class; RECEIVE_MESSAGE
│   │   ├── contracts.py                   # activity_input/output, inbound + trigger builders
│   │   ├── constraints.py                 # CONSTRAIN vocabulary (unreachable today, see 13)
│   │   ├── governor.py                    # MailGovernor: run/arun, receive_inbound, emit_trigger
│   │   ├── redaction.py                   # apply guardrails.redacted_input to args/results
│   │   ├── approvals.py                   # wait-mode poller + draft-mode store/resolvers
│   │   ├── client.py                      # OpenBoxMailAgent / Async..., session()
│   │   ├── toolkit.py                     # governed_tools(), TOOL_TYPE_MAP
│   │   ├── webhook_relay.py               # InboundRelay: ASGI / Flask / FastAPI, auth, dedupe
│   │   ├── websocket_inbound.py           # WebsocketInbound (no public URL needed)
│   │   ├── mcp_proxy/                     # server.py, stdio.py, __main__.py
│   │   └── errors.py
│   ├── tests/                             # 192 tests, offline (FakeCore)
│   ├── examples/                          # api_server, listen_inbound, reply_flow, my_test, ...
│   ├── docs/policy-cookbook.md
│   ├── LICENSE
│   ├── pyproject.toml                     # requires-python >=3.11
│   └── README.md
├── sdk-node/                              # TypeScript port (behind Python, see STATUS_AND_ROADMAP)
├── ref/                                   # vendored reference copies, NOT published
├── ARCHITECTURE.md                        # this file
└── STATUS_AND_ROADMAP.md
```

**Dependencies:** `openbox-sdk-python>=1.3.1,<2`, `agentmail` (pinned minor), `pydantic>=2`.
**Extras:** `[mcp]` (`mcp`), `[toolkit]` (`agentmail-toolkit`), `[all]`.

---

## 10. Testing

Use `openbox_core.conformance` (`FakeCore`, `fake_client`, `build_conformance_runtime`, `assert_hook_wire_shape`). Don't build a custom fake backend.

**Verdict matrix** (for each class `send`, `draft`, `modify`, `admin`, `read`), asserting whether the AgentMail executor was called and what was raised or returned:
- ALLOW / ALLOW + input redaction / ALLOW + output redaction
- CONSTRAIN → fail-closed path (never polls)
- REQUIRE_APPROVAL → approve / reject / expire / poll-timeout / `hitl.enabled=False`
- BLOCK (started) → executor not called. BLOCK (completed) → read withheld, write flagged + future blocked
- HALT → executor not called, later calls short-circuit with no network
- `guardrails.validation_passed=false` → `GuardrailsValidationError` even when verdict is REQUIRE_APPROVAL
- Network error × `fail_open` / `fail_closed`. `fallback_used` on a write is refused. 401/403 → `OpenBoxAuthError`
- Malformed input → `ContractError` with zero HTTP calls

Plus: import safety, config resolution (prefix > global > alias), catalogue coverage, idempotency (approval retry never double-sends), inbound auth/dedupe, MCP `isError` mapping, instrumentation ignore-list.

---

## 11. Dashboard outcome

- **Agents:** `AgentMailAgent` with trust score and tier, driven by verdicts and delivery signals (bounces/complaints).
- **Sessions / Replay / Timeline:** `WorkflowStarted` → `agentmail.message_received` signal → `agentmail.reply_to_message` Activity (verdict) → `approval_resume` → `WorkflowCompleted`.
- **Approvals:** pending sends with recipients, subject and body.
- **Audit Log:** every verdict with `reason`, `policy_id`, `governance_event_id`.
- **Policies / Guardrails / Behavioral Rules:** authored on `activity_type` = `agentmail.*`, `task_queue` = inbox, and `activity_input.*` fields.

---

## 12. Phasing

| Phase | Scope |
|---|---|
| **P0** | `config`, `runtime`, `adapter`, `catalog`, `contracts`, `governor`, `redaction`, wait-mode approvals, REST `OpenBoxMailAgent` (sync + async), errors, conformance + matrix tests |
| **P1** | Inbound relay (webhook auth, ack/queue, dedupe, SignalReceived gating). Early because inbound is the main injection vector |
| **P2** | Toolkit adapters + `TOOL_TYPE_MAP`; policy cookbook |
| **P3** | MCP governed proxy (HTTP + stdio) |
| **P4** | Draft-mode approvals, WebSocket inbound, applying `CONSTRAIN` once a schema exists, base-SDK PR for `activity_output=`, Node SDK on the TS base SDK |

---

## 13. Decisions on previously open items

| # | Item | Decision | Rationale |
|---|---|---|---|
| 1 | Output field name | **`activity_output`**, passed via `extra=` (§3.2) | Matches Core guardrails, Rego input, dashboard, CrewAI and LLM-completion payloads. Non-blocking follow-up: add `activity_output=` to the base factory |
| 2 | `source` / `sdk_engine` | `source="agentmail-telemetry"`, `sdk_engine="agentmail"`, `sdk_version=<package version>` | Same convention as `crewai-telemetry`. `EventEnvelope.source` and `OpenBoxConfig.sdk_engine` are both public fields, so no Core change is needed. Lets the dashboard distinguish integrations |
| 3 | `CONSTRAIN` | Satisfiable constraints rewrite the call for `send/draft/modify/admin`; unsatisfiable, unknown or bare CONSTRAIN raises `AgentMailBlockedError`. `ALLOW` + logged for `read`. `result.constraints` attached to the error | Verified live (Sep 2026): Core sends bare strings (`["run_in_sandbox"]`) and registers no approval for CONSTRAIN, so polling hangs forever. Failing closed is the only safe option |
| 4 | Env-var names | `openbox_core` canonical names; CrewAI names accepted as aliases with a deprecation warning (§7.1) | The base SDK is the declared direction for all framework SDKs. Aliases keep the "zero new concepts" promise for CrewAI users |

## 14. Alignment check (v2)

| Requirement | Where satisfied |
|---|---|
| Data goes to OpenBox **first**, AgentMail only on ALLOW | §5.1 steps 4–7: executor runs only after `evaluate_lifecycle(activity_started)` passes |
| All policy on the dashboard, none in SDK | §1, §3.6: SDK ships a catalogue and field builders, no rules |
| Values visible on the OpenBox dashboard | §3.1 identity mapping, §3.2 timeline shape, §11 |
| Inbound mail screened before the handler | §4.4: `SignalReceived` gate + handler skipped on BLOCK |
| Reads screened before the agent sees content | §5.1 step 9: completed-stage BLOCK withholds read results |
| Same base SDK, same errors, same verdicts as the family | §0.1, §5.2, §5.3: `openbox_core` runtime/adapter/errors reused |
| One `pip install`, ~5 lines | §4.1 quick start |
| REST + toolkit + MCP surfaces | §4.1–4.3 share `MailGovernor` |
| Fail closed on outages for sends | §5.1, §7.2 (`on_api_error=fail_closed`, fallback refused for writes) |
| SDK and test frontend separated | §9 |


---

## 15. Implementation status (v4)

Built under `sdk/` (`openbox-agentmail-sdk-python` 0.1.0) and `sdk-node/`
(`openbox-agentmail-sdk` 0.1.0). **Python: 134 tests green**, ruff clean,
`uv build` produces wheel + sdist. **Node: 31 tests green**, `tsc --noEmit`
clean. Live smoke (read-only) passes against real Core + AgentMail.

| Phase | Status |
|---|---|
| P0 -- config/runtime/adapter/catalog/contracts/governor/redaction, sync+async `OpenBoxMailAgent`, verdict matrix vs `FakeCore` | **Done** |
| P1 -- inbound relay: `WebhookAuth` (custom delivery headers; AgentMail has no HMAC), TTL dedupe, `SignalReceived` enforcement, bare ASGI app | **Done** |
| P2 -- `toolkit.governed_toolkit(framework, agent)` injects the governed proxy into `agentmail_toolkit.{openai,langchain,livekit}.AgentMailToolkit`; `TOOL_TYPE_MAP` exported; `docs/policy-cookbook.md` | **Done** |
| P3 -- `mcp_proxy/` HTTP proxy: `tools/call` governed via `classify_mcp_tool`, camelCase<->snake_case args, credential isolation (server-held key), SSE parsing, `isError` on blocks | **Done** |
| P4 -- draft-mode approvals (`PendingApproval` store + resolver, `approval_resume` signal), relay ack-then-process queue, `hitl.skip_activity_types`, SDK version metadata, quarantine helper | **Done** |
| P5 -- WebSocket inbound channel (reconnect + dedupe), MCP stdio transport, `Mcp-Session-Id` passthrough, `tools/list` classification report, Flask blueprint + FastAPI router | **Done** |
| P6 -- CONSTRAIN vocabulary (`constraints.py`: `max_recipients`, `allowed_domains`, `strip_attachments`, `force_bcc`, `require_approval`; violations/unknowns fail closed), attachment text screened with `privacy.max_body_size` caps, `patched_args` retry helper, `metadata_only` wire check, policy fixtures | **Done** |
| P7 -- secret-scan test, latency/concurrency/HALT-isolation tests, `emit_handoff`, webhook header rotation, coverage gate | **Done** (CI matrix, PyPI publish pending) |
| P8 -- `sdk-node/` TypeScript port: same catalogue/contracts/governor/relay/MCP, self-contained signed v1 client, shared wire fixtures asserted equivalent | **Done** |

Notable implementation findings vs the design:

- `OpenBoxRuntime` defaults to a **process-global** `ContextStore`; `build_runtime` passes a fresh one per agent or a HALT would poison every agent in the process.
- `FakeCore` pops queued responses FIFO for `/evaluate` AND `/approval` alike -- test queues lead with `{}` for `WorkflowStarted`.
- The wire body is **flat** -- `activity_input`/`activity_output`/`activity_id`/`source` are top-level, verified against captured payloads.
- `with_raw_response`/`with_options` wrapper resources are stripped before classification so raw-response calls stay governed.
- Approval polls key on `workflow_id`/`run_id`/`activity_id` (never `approval_id`), so `approval_id` is only required for an explicit `REQUIRE_APPROVAL`; `CONSTRAIN` writes never poll — they fail closed, because Core registers no approval for CONSTRAIN and the poll would hang forever. An absent `approval_id` must NOT block a REQUIRE_APPROVAL poll: Core omits it.
- AgentMail webhook authentication is **custom delivery headers** (write-only, rotatable via `update_headers`) -- the relay does constant-time header comparison, not HMAC.
- The hosted MCP server uses **camelCase arguments** (`inboxId`); the proxy converts to snake_case for contracts and back for upstream calls.
- Bare `CONSTRAIN` (no constraint list) raises `AgentMailBlockedError`; only constraints the SDK recognises are applied. Core's bare-string form (`["run_in_sandbox"]`) is normalised and named as inapplicable to email.
- The Node SDK signs v1 requests itself (Ed25519 canonical string over `METHOD\npath\ntimestamp\nnonce\nbody_sha256`) -- replace `core.ts` if a TypeScript base SDK ships.
