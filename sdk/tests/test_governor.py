"""Verdict matrix + lifecycle shape against openbox_core.conformance.FakeCore."""

import pytest
from conftest import INBOUND_EVENT, make_agent
from openbox_core.conformance.fake_core import FakeCore
from openbox_core.errors import ApprovalRejectedError, GuardrailsValidationError

from openbox_agentmail.config import AgentMailSettings
from openbox_agentmail.errors import (
    AgentMailBlockedError,
    AgentMailHaltedError,
    ContractError,
    GovernanceAPIError,
)

SEND = dict(inbox_id="inbox_1", to=["alice@example.com"], subject="hi", text="hello")


def _events(core: FakeCore) -> list[str]:
    return [p.get("event_type") for p in core.lifecycle_payloads]


# ── verdict matrix ───────────────────────────────────────────────────────────


def test_allow_executes_and_emits_lifecycle(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    res = agent.inboxes.messages.send(**SEND)
    assert res.message_id == "m_sent_1"
    assert [m for m, _ in fake_mail.calls] == ["messages.send"]

    types = _events(core)
    assert types == ["WorkflowStarted", "ActivityStarted", "ActivityCompleted"]

    started = core.lifecycle_payloads[1]
    assert started["activity_type"] == "agentmail.send_message"
    ai = started["activity_input"]
    assert ai["to"] == ["alice@example.com"]
    assert ai["recipient_domains"] == ["example.com"]
    assert ai["external_recipients"] is True
    assert started["source"] == "agentmail-telemetry"

    completed = core.lifecycle_payloads[2]
    assert completed["activity_output"]["message_id"] == "m_sent_1"
    # both events share workflow/run ids and the activity id
    assert started["workflow_id"] == completed["workflow_id"]
    assert started["activity_id"] == completed["activity_id"]
    # idempotency key was injected from the activity id
    assert fake_mail.calls[0][1]["idempotency_key"] == started["activity_id"]


def test_block_prevents_call(core, fake_mail):
    core.queue.extend([{}, {"verdict": "block", "reason": "external recipient", "policy_id": "p_1"}])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(AgentMailBlockedError) as ei:
        agent.inboxes.messages.send(**SEND)
    assert ei.value.policy_id == "p_1"
    assert fake_mail.calls == []
    # WorkflowStarted evaluated (default allow), ActivityStarted blocked, no completed
    assert _events(core) == ["WorkflowStarted", "ActivityStarted"]


def test_halt_stops_session(core, fake_mail):
    core.queue.extend([{}, {"verdict": "halt", "reason": "kill"}])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(AgentMailHaltedError):
        agent.inboxes.messages.send(**SEND)
    # subsequent calls short-circuit locally — no further evaluations
    n = len(core.payloads)
    with pytest.raises(AgentMailHaltedError):
        agent.inboxes.messages.get(inbox_id="inbox_1", message_id="m_1")
    assert len(core.payloads) == n


def test_require_approval_approved_runs(core, fake_mail):
    core.queue.extend([
        {},  # WorkflowStarted
        {"verdict": "require_approval", "approval_id": "ap_1"},
        {"action": "allow"},  # approval poll
    ])
    agent, _ = make_agent(core, mail=fake_mail)
    res = agent.inboxes.messages.send(**SEND)
    assert res.message_id == "m_sent_1"
    assert len(core.approval_requests) == 1
    req = core.approval_requests[0]
    started = core.lifecycle_payloads[1]
    assert req["workflow_id"] == started["workflow_id"]
    assert req["activity_id"] == started["activity_id"]


def test_require_approval_rejected_blocks_call(core, fake_mail):
    core.queue.extend([
        {},
        {"verdict": "require_approval", "approval_id": "ap_1"},
        {"action": "block", "reason": "nope"},
    ])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(ApprovalRejectedError):
        agent.inboxes.messages.send(**SEND)
    assert fake_mail.calls == []


def test_require_approval_no_approval_id_still_polls(core, fake_mail):
    """Core may omit approval_id while still registering the approval; the poll
    is keyed by workflow/run/activity, so it must proceed. Refusing outright
    turned every live pending approval into an instant false rejection."""
    core.queue.extend([{}, {"verdict": "require_approval"}, {"action": "allow"}])
    agent, _ = make_agent(core, mail=fake_mail)
    res = agent.inboxes.messages.send(**SEND)
    assert res.message_id == "m_sent_1"
    assert len(core.approval_requests) == 1


def test_require_approval_no_approval_id_still_fails_safe(core, fake_mail):
    """...and the fail-safe is unchanged: no allow from the poll, no send."""
    core.queue.extend([{}, {"verdict": "require_approval"}, {"action": "block", "reason": "no"}])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(ApprovalRejectedError):
        agent.inboxes.messages.send(**SEND)
    assert fake_mail.calls == []


def test_bare_constrain_on_write_fails_closed_without_polling(core, fake_mail):
    """Core registers an approval for REQUIRE_APPROVAL only. Polling on a
    CONSTRAIN waits on an approval that never exists — with the default
    hitl.max_wait_ms=None that hangs the caller forever (seen live). Refuse
    immediately instead, and never poll."""
    core.queue.extend([{}, {"verdict": "constrain"}])
    agent, mail = make_agent(core, mail=fake_mail)
    with pytest.raises(AgentMailBlockedError, match="without any constraints"):
        agent.inboxes.messages.send(**SEND)
    assert core.approval_requests == []  # the hang regression
    assert mail.calls == []


def test_constrain_on_read_proceeds(core, fake_mail):
    core.queue.extend([{}, {"verdict": "constrain", "constraints": ["log-only"]}, {}])
    agent, _ = make_agent(core, mail=fake_mail)
    res = agent.inboxes.messages.get(inbox_id="inbox_1", message_id="m_1")
    assert res.message_id == "m_1"
    assert len(core.approval_requests) == 0


# ── guardrails ───────────────────────────────────────────────────────────────


def test_input_redaction_applied_to_agentmail_call(core, fake_mail):
    core.queue.extend([{}, {
        "verdict": "allow",
        "guardrails": {
            "validation_passed": True,
            "input_type": "activity_input",
            "redacted_input": {"text": "hello [REDACTED]"},
        },
    }])
    agent, _ = make_agent(core, mail=fake_mail)
    agent.inboxes.messages.send(**{**SEND, "text": "hello 4111-1111-1111-1111"})
    assert fake_mail.calls[0][1]["text"] == "hello [REDACTED]"
    # recipient fields are never rewritten by guardrails
    assert fake_mail.calls[0][1]["to"] == ["alice@example.com"]


def test_guardrail_failure_raises_before_call(core, fake_mail):
    core.queue.extend([{}, {
        "verdict": "allow",
        "guardrails": {"validation_passed": False, "reasons": [{"reason": "pii detected"}]},
    }])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(GuardrailsValidationError):
        agent.inboxes.messages.send(**SEND)
    assert fake_mail.calls == []


def test_output_redaction_on_read(core, fake_mail):
    core.queue.extend([
        {},  # workflow started
        {},  # activity started
        {
            "verdict": "allow",
            "guardrails": {
                "validation_passed": True,
                "input_type": "activity_output",
                "redacted_input": {"text": "[REDACTED INJECTION]"},
            },
        },
    ])
    agent, _ = make_agent(core, mail=fake_mail)
    res = agent.inboxes.messages.get(inbox_id="inbox_1", message_id="m_1")
    assert res.text == "[REDACTED INJECTION]"


# ── failure policy ───────────────────────────────────────────────────────────


def test_fail_closed_write_raises(core, fake_mail):
    import httpx
    from openbox_core.client import EvaluationClient

    from openbox_agentmail.client import OpenBoxMailAgent
    from openbox_agentmail.config import resolve_openbox_config
    from openbox_agentmail.runtime import build_runtime

    def boom(request):
        raise httpx.ConnectError("down")

    transport = httpx.MockTransport(boom)
    client = EvaluationClient(
        "https://core.test", "obx_test_x",
        on_api_error="fail_closed", transport=transport, async_transport=transport,
    )
    config = resolve_openbox_config(
        environ={}, api_url="https://core.test", api_key="obx_test_x", on_api_error="fail_closed"
    )
    runtime = build_runtime(config, client=client)
    agent = OpenBoxMailAgent(fake_mail, runtime, AgentMailSettings())
    with pytest.raises(GovernanceAPIError):
        agent.inboxes.messages.send(**SEND)
    assert fake_mail.calls == []


def test_fallback_allow_on_write_refused_even_fail_open(core, fake_mail):
    # even when the user chooses fail_open, a fallback verdict must not send email
    agent, _ = make_agent(
        core, mail=fake_mail, on_api_error="fail_open",
        settings=AgentMailSettings(allow_fallback_for_writes=False),
    )
    core.queue.extend([{}, {"verdict": "allow", "fallback_used": True}])
    with pytest.raises(GovernanceAPIError):
        agent.inboxes.messages.send(**SEND)
    assert fake_mail.calls == []


# ── contract validation (local, before any network) ─────────────────────────


def test_missing_inbox_id_raises_contract_error(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(ContractError):
        agent.inboxes.messages.send(to=["a@b.c"], subject="x")
    assert core.payloads == []  # never reached the wire


def test_missing_recipients_raises(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(ContractError):
        agent.inboxes.messages.send(inbox_id="inbox_1", subject="x")
    assert core.payloads == []


def test_inbox_out_of_scope(core, fake_mail):
    agent, _ = make_agent(
        core, mail=fake_mail, settings=AgentMailSettings(inbox_ids={"allowed_inbox"})
    )
    with pytest.raises(ContractError):
        agent.inboxes.messages.send(**SEND)
    assert core.payloads == []


# ── reads / completed-stage ──────────────────────────────────────────────────


def test_read_output_blocked_raises(core, fake_mail):
    core.queue.extend([{}, {}, {"verdict": "block", "reason": "injection"}])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(AgentMailBlockedError):
        agent.inboxes.messages.get(inbox_id="inbox_1", message_id="m_1")


def test_write_completed_stage_block_warns_not_raises(core, fake_mail):
    core.queue.extend([{}, {}, {"verdict": "block", "reason": "post-hoc"}])
    agent, _ = make_agent(core, mail=fake_mail)
    res = agent.inboxes.messages.send(**SEND)
    # the send already happened; caller gets a wrapped result with the warning
    assert res.governance_warning.verdict.value == "block"
    assert res.message_id == "m_sent_1"


# ── inbound signals ─────────────────────────────────────────────────────────


def test_inbound_allowed(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    result, fields = agent.governor.screen_inbound(INBOUND_EVENT)
    assert result.verdict.value == "allow"
    assert fields["sender_domain"] == "evil.example"
    assert fields["agentmail_event_type"] == "message.received"
    assert "SignalReceived" in _events(core)


def test_inbound_blocked_raises(core, fake_mail):
    core.queue.extend([{}, {"verdict": "block", "reason": "spam source"}])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(AgentMailBlockedError):
        agent.governor.screen_inbound(INBOUND_EVENT)


def test_inbound_redaction(core, fake_mail):
    core.queue.extend([
        {},
        {
            "verdict": "allow",
            "guardrails": {
                "validation_passed": True,
                "input_type": "signal",
                "redacted_input": {"text": "[scrubbed]"},
            },
        },
    ])
    agent, _ = make_agent(core, mail=fake_mail)
    _, fields = agent.governor.screen_inbound(INBOUND_EVENT)
    assert fields["text"] == "[scrubbed]"


def test_inbound_telemetry_only_status_events(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    result, _ = agent.governor.screen_inbound(
        {"event_type": "message.delivered", "event_id": "e2", "message": {"message_id": "m"}}
        , enforce=False
    )
    assert result.verdict.value == "allow"
