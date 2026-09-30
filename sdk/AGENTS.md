# Agent notes — openbox-agentmail-sdk-python

## Layout

- `src/openbox_agentmail/` — the SDK. `__init__.py` must stay import-safe (no
  httpx/agentmail/crypto at import; heavy names resolve via `__getattr__`).
- `governor.py` — the single governance path. REST proxy, webhook relay, MCP
  proxy and toolkit all funnel through `MailGovernor.run` / `arun` /
  `screen_inbound` / `ascreen_inbound`. Do not add a second evaluation path.
- `catalog.py` — the only place AgentMail methods are classified. Anything not
  classified is refused (`UncataloguedActionError`), never passed through.
- `constraints.py` — the structured `CONSTRAIN` vocabulary applied to write
  args; unknown types, unsatisfiable constraints and bare CONSTRAIN (no list)
  all **fail closed** with `AgentMailBlockedError`. They never escalate and
  never poll — Core registers approvals for `REQUIRE_APPROVAL` only, so a poll
  would hang forever. NOTE: the dashboard cannot author constraints at all
  (it only emits `["run_in_sandbox"]`), so CONSTRAIN always refuses in
  practice. Use REQUIRE APPROVAL or BLOCK for mail.
- `approvals.py` — `PendingApproval` + store + resolver for draft-mode
  approvals (`approval_mode="draft"`).
- `websocket_inbound.py` — AgentMail WebSocket → same `ascreen_inbound`
  pipeline (reconnect + event dedupe). `mcp_proxy/stdio.py` — stdio transport;
  never log to stdout there.
- `tests/` — pytest; FakeCore from `openbox_core.conformance.fake_core` stands
  in for OpenBox Core, `conftest.FakeAgentMail` for the AgentMail client.
- `../sdk-node/` — the TypeScript port (node:test). Wire fixtures shared via
  `tests/policy_fixtures/`; regenerate with the Python builders if the
  contract changes.

## Commands

```powershell
# venv already exists at sdk/.venv
& ".venv\Scripts\python.exe" -m pytest tests -q     # all tests (no API keys needed)
& ".venv\Scripts\python.exe" -m ruff check src tests
uv pip install <pkg>                                 # add a dependency (uv-managed venv)
uv build                                             # package build check
& ".venv\Scripts\python.exe" examples\live_smoke.py  # LIVE read-only check; reads sdk/.env (git-ignored)
```

- Credentials live in `sdk/.env` (never commit; never print). `OPENBOX_API_URL`
  must be set — the base SDK has no default (`https://core.openbox.ai`).
- Hosted MCP tool args are camelCase; the proxy converts to snake_case for
  governance and back for upstream. Test MCP with the real manifest schema.

## Invariants (tests enforce)

- `WorkflowStarted` is emitted exactly once per governed agent, before the first
  `ActivityStarted`; `WorkflowCompleted`/`WorkflowFailed` on `close()`.
- Malformed calls raise `ContractError` before ANY event hits the wire.
- `BLOCK`/`HALT` prevent the AgentMail call; `HALT` also short-circuits every
  later call locally via the per-runtime `ContextStore`.
- `REQUIRE_APPROVAL` polls `/governance/approval` with workflow/run/activity
  ids from the `ActivityContext`, not the response. An absent `approval_id`
  must NOT stop the poll — Core omits it, and refusing auto-rejected every
  real approval.
- `CONSTRAIN` never polls: on a write or on inbound it raises
  `AgentMailBlockedError` immediately.
- An `ActivityCompleted` must NOT carry a top-level `error` field — Core
  rejects it with HTTP 400 and drops the whole event. Failure rides inside
  `activity_output` (`failed: true`, `status`, `error`).
- Guardrail `redacted_input` is APPLIED to the real AgentMail call / result /
  inbound event — never just logged.
- `on_api_error` defaults to `fail_closed`; fail-open fallbacks are refused on
  writes even when configured (`allow_fallback_for_writes`).
- The wire body is flat: `activity_input`/`activity_output`/`activity_id`/
  `source` are top-level keys, NOT nested under `payload`.

## Key base-SDK facts

- `openbox_core` 1.3.1: `OpenBoxRuntime(config, adapter, client=, context_store=)`
  — pass a FRESH `ContextStore()` per agent (the default store is
  process-global; a HALT would leak between agents).
- `evaluate_lifecycle` (sync) enforces BLOCK/HALT/guardrails but returns
  REQUIRE_APPROVAL undriven; `aevaluate_lifecycle` drives
  `adapter.handle_approval` itself. `Verdict`s: ALLOW, CONSTRAIN,
  REQUIRE_APPROVAL, BLOCK, HALT.
- `FakeCore` pops queued responses FIFO for BOTH `/evaluate` and `/approval`
  — every test queue needs a leading `{}` for the WorkflowStarted evaluation.
- AgentMail webhook auth = custom delivery headers (no HMAC).
