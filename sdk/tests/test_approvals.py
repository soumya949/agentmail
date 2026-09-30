"""P4: draft-mode approvals, approval_resume signal, skip_activity_types,
background relay (ack-then-process) with retry + dead-letter."""

from __future__ import annotations

import asyncio
import json

import pytest
from conftest import INBOUND_EVENT, FakeAgentMail, make_agent, make_runtime

from openbox_agentmail import (
    AgentMailSettings,
    DraftResolution,
    PendingApproval,
)
from openbox_agentmail.approvals import MemoryApprovalStore
from openbox_agentmail.errors import ApprovalRejectedError
from openbox_agentmail.webhook_relay import InboundRelay, WebhookAuth, quarantine_on_blocked

REQ_APPROVAL = {"verdict": "require_approval", "approval_id": "ap_1", "reason": "external send"}
ALLOW = {"verdict": "allow"}


def _draft_agent(core, mail=None, store=None):
    return make_agent(
        core,
        settings=AgentMailSettings(approval_mode="draft", approval_store=store),
        mail=mail,
    )


def _signal_payloads(core):
    return [p for p in core.payloads if p.get("signal_name")]


# ── draft mode: create → approve → send ─────────────────────────────────────


def test_draft_mode_returns_pending_and_creates_draft(core, fake_mail):
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL, {}, {}]  # ws, send started, draft started, draft done

    out = agent.inboxes.messages.send("inbox_1", to=["a@x.com"], subject="s", text="hi")

    assert isinstance(out, PendingApproval)
    assert out.approval_id == "ap_1"
    assert out.draft_id == "d_1"
    assert out.created_draft is True
    # Draft staged from governed args; nothing sent yet
    kinds = [name for name, _ in fake_mail.calls]
    assert kinds == ["drafts.create"]
    create_kw = fake_mail.calls[0][1]
    assert create_kw["to"] == ["a@x.com"] and create_kw["subject"] == "s"
    assert create_kw["client_id"] == out.activity_id


def test_draft_mode_approved_sends_draft(core, fake_mail):
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL, {}, {}]
    pending = agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")

    core.queue += [{"action": "allow"}, {}, {}, {}]  # poll, resume signal, send started, send done
    res = pending.wait()

    assert isinstance(res, DraftResolution) and res.status == "sent"
    kinds = [name for name, _ in fake_mail.calls]
    assert kinds == ["drafts.create", "drafts.send"]
    send_kw = fake_mail.calls[1][1]
    assert send_kw["draft_id"] == "d_1"
    # idempotency key = the ORIGINAL send activity_id
    assert send_kw["idempotency_key"] == pending.activity_id
    assert core.approval_requests[0]["activity_id"] == pending.activity_id
    assert _signal_payloads(core)[0]["signal_name"] == "approval_resume"


def test_draft_mode_rejected_deletes_draft(core, fake_mail):
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL, {}, {}]
    pending = agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")

    core.queue += [{"action": "block", "reason": "no external"}, {}, {}, {}]
    res = pending.wait()

    assert res.status == "rejected"
    kinds = [name for name, _ in fake_mail.calls]
    assert kinds == ["drafts.create", "drafts.delete"]  # cleaned up, never sent
    resume = _signal_payloads(core)[0]
    assert resume["decision"] == "rejected"


def test_draft_mode_expired_deletes_draft(core, fake_mail):
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL, {}, {}]
    pending = agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")

    core.queue += [{"expired": True}, {}, {}, {}]
    res = pending.wait()

    assert res.status == "expired"
    assert [n for n, _ in fake_mail.calls] == ["drafts.create", "drafts.delete"]


def test_draft_mode_send_draft_reuses_existing(core, fake_mail):
    """send_draft under draft mode: the caller's draft is held, never created
    or deleted — only its send is gated."""
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL]
    pending = agent.inboxes.drafts.send("inbox_1", "d_existing")

    assert pending.draft_id == "d_existing" and pending.created_draft is False
    assert fake_mail.calls == []  # nothing touched yet

    core.queue += [{"action": "block", "reason": "no"}, {}]
    res = pending.wait()
    assert res.status == "rejected"
    assert fake_mail.calls == []  # caller's draft is NOT deleted


def test_draft_mode_reply_maps_in_reply_to(core, fake_mail):
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL, {}, {}]
    pending = agent.inboxes.messages.reply("inbox_1", "m_1", text="re")

    create_kw = fake_mail.calls[0][1]
    assert create_kw["in_reply_to"] == "m_1"
    assert pending.draft_id == "d_1"


def test_pending_approvals_listed_and_rehydratable(core, fake_mail):
    store = MemoryApprovalStore()
    agent, _ = _draft_agent(core, fake_mail, store=store)
    core.queue += [{}, REQ_APPROVAL, {}, {}]
    pending = agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")

    assert agent.governor.pending_approvals() == [pending]
    # simulate restart: resolvers re-attach from the store
    clone = PendingApproval(
        approval_id=pending.approval_id,
        activity_id=pending.activity_id,
        workflow_id=pending.workflow_id,
        run_id=pending.run_id,
        activity_type=pending.activity_type,
        inbox_id=pending.inbox_id,
        draft_id=pending.draft_id,
        created_draft=True,
    )
    agent.governor.rehydrate(clone)
    core.queue += [{"action": "allow"}, {}, {}, {}]
    assert clone.wait().status == "sent"
    assert agent.governor.pending_approvals() == []


def test_draft_mode_guardrail_redaction_feeds_draft(core, fake_mail):
    agent, _ = _draft_agent(core, fake_mail)
    redact = {
        "verdict": "require_approval",
        "approval_id": "ap_1",
        "guardrails": {
            "validation_passed": True,
            "redacted_input": {"text": "hi [REDACTED]"},
            "input_type": "activity_input",
        },
    }
    core.queue += [{}, redact, {}, {}]
    agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi secret")

    assert fake_mail.calls[0][1]["text"] == "hi [REDACTED]"


def test_non_send_still_waits_in_draft_mode(core, fake_mail):
    """Modify/admin actions can't be drafted — REQUIRE_APPROVAL polls inline."""
    agent, _ = _draft_agent(core, fake_mail)
    core.queue += [{}, REQ_APPROVAL, {"action": "allow"}, {}, {}, {}]
    out = agent.inboxes.messages.update("inbox_1", "m_1", add_labels=["x"])
    assert out is not None
    assert [n for n, _ in fake_mail.calls] == ["messages.update"]


def test_draft_mode_requires_mail_client(core):
    runtime = make_runtime(core)
    from openbox_agentmail.errors import OpenBoxConfigError
    from openbox_agentmail.governor import MailGovernor

    with pytest.raises(OpenBoxConfigError):
        MailGovernor(runtime, AgentMailSettings(approval_mode="draft"))


# ── wait mode: approval_resume + skip_activity_types ─────────────────────────


def test_wait_mode_emits_approval_resume(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, REQ_APPROVAL, {"action": "allow"}, {}, {}, {}]
    agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")

    resume = _signal_payloads(core)
    assert len(resume) == 1 and resume[0]["signal_name"] == "approval_resume"
    assert resume[0]["decision"] == "approved"
    assert [n for n, _ in mail.calls] == ["messages.send"]


def test_wait_mode_rejection_emits_resume(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, REQ_APPROVAL, {"action": "block", "reason": "no"}, {}]
    with pytest.raises(ApprovalRejectedError):
        agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")
    assert _signal_payloads(core)[0]["decision"] == "rejected"
    assert mail.calls == []


def test_skip_activity_types_proceeds_without_poll(core):
    core.queue += [{}, REQ_APPROVAL, {}, {}]
    runtime = make_runtime(
        core,
        hitl={
            "enabled": True,
            "poll_interval_ms": 1,
            "max_wait_ms": 100,
            "skip_activity_types": ["agentmail.send_message"],
        },
    )
    mail = FakeAgentMail()
    from openbox_agentmail.client import OpenBoxMailAgent

    agent = OpenBoxMailAgent(mail, runtime, AgentMailSettings())
    out = agent.inboxes.messages.send("inbox_1", to=["a@x.com"], text="hi")

    assert out is not None
    assert [n for n, _ in mail.calls] == ["messages.send"]
    assert core.approval_requests == []


# ── relay: background queue ──────────────────────────────────────────────────


def _relay(governor, mail=None, **kw):
    mail = mail or FakeAgentMail()
    return InboundRelay(governor, auth=WebhookAuth(secret="s3cret"), **kw)


def _headers():
    return {"authorization": "Bearer s3cret"}


def test_background_acks_before_processing(core):
    handled = []
    agent, _ = make_agent(core)
    relay = _relay(agent.governor, background=True, handler=lambda i: handled.append(i))

    async def go():
        resp = await relay.ahandle(_headers(), json.dumps(INBOUND_EVENT).encode())
        assert resp.status == 200 and resp.body["status"] == "accepted"
        assert handled == []  # not processed yet
        await relay.drain()
        assert len(handled) == 1
        await relay.aclose()

    asyncio.run(go())


def test_background_block_skips_handler_and_calls_on_blocked(core):
    handled, blocked = [], []
    agent, _ = make_agent(core)
    relay = _relay(
        agent.governor,
        background=True,
        handler=lambda i: handled.append(i),
        on_blocked=lambda ev, e: blocked.append(str(e)),
    )
    core.queue += [{}, {"verdict": "block", "reason": "spam"}]

    async def go():
        resp = await relay.ahandle(_headers(), json.dumps(INBOUND_EVENT).encode())
        assert resp.body["status"] == "accepted"
        await relay.drain()
        await relay.aclose()

    asyncio.run(go())
    assert handled == [] and len(blocked) == 1


def test_background_retries_then_dead_letters(core):
    dead = []
    agent, _ = make_agent(core)
    relay = _relay(
        agent.governor,
        background=True,
        max_retries=2,
        retry_backoff=0.001,
        on_dead_letter=lambda ev: dead.append(ev),
    )
    original = agent.governor.areceive_inbound
    calls = {"n": 0}

    async def outage(*a, **k):
        calls["n"] += 1
        from openbox_agentmail.errors import GovernanceAPIError

        raise GovernanceAPIError("core down")

    agent.governor.areceive_inbound = outage
    try:
        async def go():
            resp = await relay.ahandle(_headers(), json.dumps(INBOUND_EVENT).encode())
            assert resp.body["status"] == "accepted"
            await relay.drain()
            await relay.aclose()

        asyncio.run(go())
    finally:
        agent.governor.areceive_inbound = original

    assert calls["n"] == 3  # initial + 2 retries
    assert len(dead) == 1


def test_background_dedupe_still_applies(core):
    agent, _ = make_agent(core)
    relay = _relay(agent.governor, background=True)

    async def go():
        body = json.dumps(INBOUND_EVENT).encode()
        await relay.ahandle(_headers(), body)
        r2 = await relay.ahandle(_headers(), body)
        assert r2.body["status"] == "duplicate"
        await relay.drain()
        await relay.aclose()

    asyncio.run(go())


def test_quarantine_on_blocked_labels_message(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    cb = quarantine_on_blocked(agent)
    core.queue += [{}, {}, {}]  # WorkflowStarted, ActivityStarted, ActivityCompleted
    cb(INBOUND_EVENT, Exception("blocked"))
    kinds = [n for n, _ in mail.calls]
    assert kinds == ["messages.update"]
    assert mail.calls[0][1]["add_labels"] == ["quarantine"]
    assert mail.calls[0][1]["message_id"] == "m_in_1"


def test_metadata_has_sdk_versions(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    core.queue += [{}, {}, {}]
    agent.inboxes.messages.list("inbox_1")
    started = [p for p in core.payloads if p.get("activity_type") == "agentmail.list_messages"][0]
    assert started["agentmail_sdk_version"]
    assert started["openbox_sdk_version"]


# ── approval_id must never gate polling (live regression, Sep 2026) ──────────
# Core registers the approval and may omit approval_id from the evaluate
# response. The poll is keyed by (workflow_id, run_id, activity_id), so an
# absent id must not short-circuit into a false rejection — that turned every
# real pending approval into an instant reject against the live dashboard.

NO_ID_APPROVAL = {"verdict": "require_approval", "reason": "needs a human"}


def test_inbound_approval_without_approval_id_still_polls(core, fake_mail):
    core.queue += [{}, NO_ID_APPROVAL, {"action": "allow"}]
    agent, mail = make_agent(core, mail=fake_mail)

    received = agent.governor.receive_inbound(INBOUND_EVENT)

    assert len(core.approval_requests) == 1
    req = core.approval_requests[0]
    assert req["activity_id"] == received.activity_id != ""
    assert [n for n, _ in mail.calls] == ["messages.get"]


def test_send_approval_without_approval_id_still_polls(core, fake_mail):
    core.queue += [{}, NO_ID_APPROVAL, {"action": "allow"}]
    agent, mail = make_agent(core, mail=fake_mail)

    agent.inboxes.messages.send(inbox_id="inbox_1", to=["a@x.com"], text="hi")

    assert len(core.approval_requests) == 1
    assert [n for n, _ in mail.calls] == ["messages.send"]


def test_approval_without_id_still_honours_rejection(core, fake_mail):
    core.queue += [{}, NO_ID_APPROVAL, {"action": "block", "reason": "no"}]
    agent, mail = make_agent(core, mail=fake_mail)

    with pytest.raises(ApprovalRejectedError):
        agent.governor.receive_inbound(INBOUND_EVENT)
    assert mail.calls == []  # fail-safe intact: never fetched


def test_approval_refused_when_hitl_disabled(core, fake_mail):
    """The one unrecoverable case: no poller at all -> fail safe."""
    core.queue += [{}, NO_ID_APPROVAL]
    agent, mail = make_agent(core, mail=fake_mail, hitl={"enabled": False})

    with pytest.raises(ApprovalRejectedError):
        agent.governor.receive_inbound(INBOUND_EVENT)
    assert core.approval_requests == []
    assert mail.calls == []
