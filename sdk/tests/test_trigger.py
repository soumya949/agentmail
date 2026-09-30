"""Caller-declared triggers: the SignalReceived that records WHY the agent
acted, ordered before the activities it caused."""

from __future__ import annotations

import asyncio

import pytest
from conftest import make_agent

from openbox_agentmail.errors import (
    AgentMailBlockedError,
    AgentMailHaltedError,
    ContractError,
    OpenBoxConfigError,
)

SEND = {"inbox_id": "inbox_1", "to": ["a@x.com"], "subject": "s", "text": "t"}


def _signals(core):
    return [p for p in core.lifecycle_payloads if p["event_type"] == "SignalReceived"]


def test_trigger_precedes_the_send_it_caused(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)

    agent.governor.emit_trigger(
        "api_request",
        {"reason": "invoice overdue", "requested_by": "user_8823"},
        source="billing-service",
    )
    agent.inboxes.messages.send(**SEND)

    assert [p["event_type"] for p in core.lifecycle_payloads] == [
        "WorkflowStarted",
        "SignalReceived",
        "ActivityStarted",
        "ActivityCompleted",
    ]
    sig = _signals(core)[0]
    assert sig["signal_name"] == "agentmail.trigger.api_request"
    assert sig["trigger"] == "api_request"
    assert sig["trigger_source"] == "billing-service"
    assert sig["trigger_data"] == {"reason": "invoice overdue", "requested_by": "user_8823"}
    assert sig["direction"] == "inbound"
    assert [n for n, _ in mail.calls] == ["messages.send"]


def test_trigger_data_cannot_overwrite_policy_fields(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    agent.governor.emit_trigger("x", {"trigger": "spoofed", "surface": "spoofed"})
    sig = _signals(core)[0]
    assert sig["trigger"] == "x"  # namespaced under trigger_data, not merged
    assert sig["surface"] == "rest"
    assert sig["trigger_data"]["trigger"] == "spoofed"


def test_blocked_trigger_stops_the_agent_before_agentmail(core, fake_mail):
    core.queue += [{}, {"verdict": "block", "reason": "untrusted source"}]
    agent, mail = make_agent(core, mail=fake_mail)

    with pytest.raises(AgentMailBlockedError):
        agent.governor.emit_trigger("api_request", source="sketchy-service")
    assert mail.calls == []


def test_halted_trigger_poisons_the_session(core, fake_mail):
    core.queue += [{}, {"verdict": "halt", "reason": "compromised"}]
    agent, mail = make_agent(core, mail=fake_mail)

    with pytest.raises(AgentMailHaltedError):
        agent.governor.emit_trigger("api_request")
    with pytest.raises(AgentMailHaltedError):
        agent.inboxes.messages.send(**SEND)
    assert mail.calls == []


def test_require_approval_on_trigger_fails_closed_with_guidance(core, fake_mail):
    """A signal has no activity_id, so the hold could never resolve. Refuse
    loudly instead of dropping the verdict."""
    core.queue += [{}, {"verdict": "require_approval", "approval_id": "ap_1"}]
    agent, mail = make_agent(core, mail=fake_mail)

    with pytest.raises(OpenBoxConfigError, match="activity"):
        agent.governor.emit_trigger("api_request")
    assert mail.calls == []


def test_telemetry_only_trigger_is_never_enforced(core, fake_mail):
    core.queue += [{}, {"verdict": "block", "reason": "would have blocked"}]
    agent, _ = make_agent(core, mail=fake_mail)

    agent.governor.emit_trigger("cron_tick", enforce=False)

    assert _signals(core)[0]["enforced"] is False


def test_bad_trigger_input_refused_before_any_session(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    for bad in ("", "   ", None, 7):
        with pytest.raises(ContractError):
            agent.governor.emit_trigger(bad)  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        agent.governor.emit_trigger("ok", data="not a mapping")  # type: ignore[arg-type]
    assert core.payloads == []  # no WorkflowStarted from a malformed call


def test_oversized_trigger_data_is_capped(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail, privacy={"max_body_size": 20})
    agent.governor.emit_trigger("big", {"prompt": "x" * 500})
    sig = _signals(core)[0]
    assert len(sig["trigger_data"]["prompt"]) == 20
    assert sig["truncated"] is True


def test_async_trigger_precedes_send(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)

    async def go():
        await agent.governor.aemit_trigger("webhook", source="stripe")

    asyncio.run(go())
    assert _signals(core)[0]["signal_name"] == "agentmail.trigger.webhook"


def test_async_blocked_trigger_raises(core, fake_mail):
    core.queue += [{}, {"verdict": "block", "reason": "no"}]
    agent, _ = make_agent(core, mail=fake_mail)

    with pytest.raises(AgentMailBlockedError):
        asyncio.run(agent.governor.aemit_trigger("webhook"))


def test_dotted_trigger_name_is_safe_in_signal_name(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    agent.governor.emit_trigger("slack.message.received")
    assert _signals(core)[0]["signal_name"] == "agentmail.trigger.slack_message_received"


# ── per-task sessions (agent.session) ────────────────────────────────────────


def _types(core):
    return [p["event_type"] for p in core.lifecycle_payloads]


def test_session_scopes_one_workflow_per_send(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)

    for _ in range(2):
        with agent.session():
            agent.emit_trigger("api_request", source="billing")
            agent.inboxes.messages.send(**SEND)

    assert _types(core) == [
        "WorkflowStarted", "SignalReceived", "ActivityStarted", "ActivityCompleted",
        "WorkflowCompleted",
        "WorkflowStarted", "SignalReceived", "ActivityStarted", "ActivityCompleted",
        "WorkflowCompleted",
    ]
    ids = {p["workflow_id"] for p in core.lifecycle_payloads}
    assert len(ids) == 2  # a genuinely new workflow each time


def test_without_session_everything_shares_one_workflow(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)

    agent.inboxes.messages.send(**SEND)
    agent.inboxes.messages.send(**SEND)

    assert "WorkflowCompleted" not in _types(core)
    assert len({p["workflow_id"] for p in core.lifecycle_payloads}) == 1


def test_session_closes_as_failed_on_exception(core, fake_mail):
    core.queue += [{}, {}, {"verdict": "block", "reason": "nope"}]
    agent, _ = make_agent(core, mail=fake_mail)

    with pytest.raises(AgentMailBlockedError), agent.session():
        agent.emit_trigger("api_request")
        agent.inboxes.messages.send(**SEND)

    closing = core.lifecycle_payloads[-1]
    assert closing["event_type"] == "WorkflowFailed"
    assert closing["status"] == "failed"


def test_halt_survives_session_boundaries(core, fake_mail):
    """A session is a workflow, not a reset: HALT poisons the agent."""
    core.queue += [{}, {"verdict": "halt", "reason": "compromised"}]
    agent, mail = make_agent(core, mail=fake_mail)

    with pytest.raises(AgentMailHaltedError), agent.session():
        agent.emit_trigger("api_request")

    with pytest.raises(AgentMailHaltedError), agent.session():
        agent.inboxes.messages.send(**SEND)
    assert mail.calls == []


def test_async_session_scopes_one_workflow(core, fake_mail):
    from conftest import make_runtime

    from openbox_agentmail.client import AsyncOpenBoxMailAgent

    class AsyncMessages:
        async def send(self, **kw):
            return type("M", (), {"message_id": "m_1", "model_dump": lambda s, **k: {"message_id": "m_1"}})()

    class AsyncClient:
        def __init__(self):
            self.inboxes = type("I", (), {})()
            self.inboxes.messages = AsyncMessages()

    agent = AsyncOpenBoxMailAgent(AsyncClient(), make_runtime(core))

    async def go():
        async with agent.session():
            await agent.emit_trigger("webhook", source="stripe")
            await agent.inboxes.messages.send(**SEND)

    asyncio.run(go())
    assert _types(core)[-1] == "WorkflowCompleted"
