# Changelog

All notable changes to `openbox-agentmail-sdk-python`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-09-30

First release. Governs every AgentMail operation through OpenBox policy: sends,
replies, forwards, drafts, reads, inbound mail, MCP tool calls and framework
toolkits all pass through one choke point.

### Added

- **Governed REST client** — `OpenBoxMailAgent` / `AsyncOpenBoxMailAgent` are
  drop-in replacements for `agentmail.AgentMail`. Every catalogued method is
  evaluated before the call (`ActivityStarted`) and after it
  (`ActivityCompleted`). Uncatalogued methods are refused rather than passed
  through ungoverned.
- **All five verdicts** — `ALLOW`, `CONSTRAIN`, `REQUIRE_APPROVAL`, `BLOCK`,
  `HALT`. `BLOCK`/`HALT` raise before AgentMail is touched; `HALT` stops the
  whole session locally.
- **Inbound as a governed activity** — each received email becomes an
  `agentmail.receive_message` activity: the pushed copy is screened, the
  authoritative message is fetched with `messages.get` *inside* the activity
  (so the GET appears as child spans), then the fetched content is screened
  again. `fetch_on_receive=False` keeps the older `SignalReceived`-only path.
- **Transports** — `InboundRelay` (ASGI, Flask, FastAPI) with authentication,
  secret rotation and TTL dedupe; `WebsocketInbound` for setups with no public
  URL; a governed MCP proxy over HTTP and stdio.
- **`emit_trigger()` / `aemit_trigger()`** — record *why* the agent acted (an
  API request, a bot, a cron tick) as a `SignalReceived` ordered ahead of the
  activities it caused. Nothing is emitted automatically.
- **`agent.session()`** — scope one workflow to a block of work, so each task,
  request or email is its own `WorkflowStarted … WorkflowCompleted`. Inbound
  has `session_per_message` and `arrival_signal` equivalents.
- **Approvals** — inline `wait` mode, or `draft` mode which stages a real
  AgentMail draft for a human to read before deciding. Pending approvals
  persist in a pluggable store so they survive a restart.
- **Guardrails** — `redacted_input` is applied to outgoing call arguments and
  to content returned to the agent, on both the input and output stages.
- **Framework toolkits** — `governed_tools()` injects the governed client into
  the OpenAI, LangChain and LiveKit AgentMail toolkits.
- Typed package (`py.typed`), import-safe root (importing the package pulls no
  HTTP, crypto or OpenTelemetry dependency).

### Fixed

Five defects found only by running against a live OpenBox Core — all passed the
offline suite first.

- **Approvals never completed.** Core returns `REQUIRE_APPROVAL` without an
  `approval_id`, and the adapter treated that as unpollable, auto-rejecting
  instantly without ever contacting Core. The poll is keyed by
  `(workflow_id, run_id, activity_id)`, so `approval_id` is not needed; only a
  missing poller now refuses.
- **Failed activities were never recorded.** Core rejects any
  `ActivityCompleted` carrying a top-level `error` field with
  `HTTP 400 "invalid request body"`, dropping the event, so a failed send left
  an activity that never finished. The failure now travels inside
  `activity_output` (`failed: true`, `status`, `error`), bounded to 1000
  printable characters.
- **`CONSTRAIN` hung the caller indefinitely.** Core registers approvals for
  `REQUIRE_APPROVAL` only, yet an unsatisfiable constraint fell through to the
  approval poll — which has no default timeout. An unsatisfiable, unknown or
  bare `CONSTRAIN` now fails closed immediately and never polls.
- The same hang on the inbound path.
- **Constraints are bare strings.** Core sends `["run_in_sandbox"]`, not
  objects; every string constraint was reported as unrecognised. Strings are
  now normalised, and directives meaningless for email are named as such.

### Known limitations

- **`CONSTRAIN` cannot be used for email.** The OpenBox rule builder offers the
  decision but no way to author the constraints, so a `CONSTRAIN` rule only ever
  emits `["run_in_sandbox"]` and the SDK refuses it. Use `REQUIRE APPROVAL` or
  `BLOCK`. The constraint vocabulary is implemented and tested for when Core
  supports authoring it.
- **`BLOCK AND PATCH` cannot be used for email.** The rule builder pins it to
  `activity_type == llm_call`, so `AgentMailBlockedError.patched_args()` can
  never fire for a mail action.
- **Rule order matters on the dashboard.** An `ALLOW` rule placed above a more
  restrictive rule silently disables it; precedence is list order, not verdict
  severity.
- Governance covers calls made *through this SDK*. The AgentMail console, raw
  API-key calls and the hosted MCP used without this proxy are not governed.
- The Node/TypeScript SDK in `sdk-node/` has not received the fixes above and
  should not be used yet.

[Unreleased]: https://github.com/soumya949/agentmail/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/soumya949/agentmail/releases/tag/v0.1.0
