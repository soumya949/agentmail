"""P7: production hardening — no secret leakage in logs, per-agent HALT
isolation, concurrency, webhook secret rotation, handoff, latency budget."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time

import pytest
from conftest import FakeAgentMail, make_agent, make_runtime

from openbox_agentmail.errors import AgentMailBlockedError, AgentMailHaltedError
from openbox_agentmail.webhook_relay import WebhookAuth

SEND = dict(inbox_id="inbox_1", to=["alice@example.com"], subject="hi", text="hello")
FAKE_SECRET = "obx_test_conformance"
FAKE_PRIVATE = "PRIVATE-KEY-MATERIAL-xyz"
FAKE_AM_KEY = "am_us_inbox_testkey123"


# ── secret handling ──────────────────────────────────────────────────────────


def test_no_secrets_in_logs_or_wire(core, fake_mail, caplog):
    """API keys / private key material must never appear in logs or wire
    payloads (telemetry captures args + results, not credentials)."""
    runtime = make_runtime(
        core,
        agent_private_key=FAKE_PRIVATE,
        agent_did="did:aip:bb9f002e-e3ff-5970-8625-b6e2102c05e4",
        api_key=FAKE_SECRET,
    )
    from openbox_agentmail.client import OpenBoxMailAgent

    agent = OpenBoxMailAgent(fake_mail, runtime)
    with caplog.at_level(logging.DEBUG):
        core.queue += [{}, {}, {}]
        agent.inboxes.messages.send(**SEND)
        core.queue += [{"verdict": "block", "reason": "x"}, {}]
        with pytest.raises(AgentMailBlockedError):
            agent.inboxes.messages.send(**SEND)
        agent.close()

    hay = "\n".join(r.getMessage() for r in caplog.records)
    for payload in core.payloads:
        hay += json.dumps(payload)
    for secret in (FAKE_SECRET, FAKE_PRIVATE, FAKE_AM_KEY):
        assert secret not in hay


def test_api_key_not_in_activity_input(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    core.queue += [{}, {}, {}]
    agent.inboxes.messages.send(**SEND, headers={"Authorization": FAKE_AM_KEY})
    started = [p for p in core.payloads if p.get("activity_type") == "agentmail.send_message"][0]
    # headers are call args, not catalogued content — they must not leak
    assert FAKE_AM_KEY not in json.dumps(started["activity_input"])


# ── webhook rotation ─────────────────────────────────────────────────────────


def test_webhook_secret_rotation_accepts_both():
    auth = WebhookAuth(secret=["old-secret", "new-secret"])
    assert auth.check({"authorization": "Bearer old-secret"})
    assert auth.check({"authorization": "Bearer new-secret"})
    assert not auth.check({"authorization": "Bearer wrong"})


def test_webhook_required_headers_list_values():
    auth = WebhookAuth({"x-hook-secret": ["a1", "a2"], "x-tag": "fixed"})
    assert auth.check({"x-hook-secret": "a2", "x-tag": "fixed"})
    assert not auth.check({"x-hook-secret": "a1", "x-tag": "changed"})


# ── multi-agent isolation ────────────────────────────────────────────────────


def test_halt_does_not_leak_between_agents(core):
    mail_a, mail_b = FakeAgentMail(), FakeAgentMail()
    agent_a, _ = make_agent(core, mail=mail_a)
    agent_b, _ = make_agent(core, mail=mail_b)

    core.queue += [{}, {"verdict": "halt", "reason": "kill switch"}]
    with pytest.raises(AgentMailHaltedError):
        agent_a.inboxes.messages.send(**SEND)

    # Same shared Core backend, different runtime/context store:
    core.queue += [{}, {}, {}]
    res = agent_b.inboxes.messages.send(**SEND)
    assert res.message_id == "m_sent_1"


def test_concurrent_calls_same_agent(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    n = 20
    core.queue += [{}] + [{}] * (2 * n)  # WorkflowStarted + started/completed each

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outs = list(pool.map(lambda i: agent.inboxes.messages.send(**{**SEND, "subject": f"s{i}"}), range(n)))

    assert all(o.message_id == "m_sent_1" for o in outs)
    assert len([c for c in mail.calls if c[0] == "messages.send"]) == n
    started = [p for p in core.payloads if p.get("event_type") == "WorkflowStarted"]
    assert len(started) == 1  # one session despite concurrency


def test_sequential_governance_overhead_budget(core, fake_mail):
    """Each governed call adds two evaluates; assert p95 overhead stays sane
    against FakeCore (no network). Budget is generous for CI noise."""
    agent, _ = make_agent(core, mail=fake_mail)
    n = 50
    core.queue += [{}] + [{}] * (2 * n)
    # warmup
    agent.inboxes.messages.list("inbox_1")
    core.queue = [{}] * (2 * n)

    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        agent.inboxes.messages.send(**SEND)
        times.append(time.perf_counter() - t0)

    times.sort()
    p95 = times[int(n * 0.95) - 1]
    assert p95 < 0.25, f"governance overhead p95 too high: {p95:.3f}s"


# ── handoff ──────────────────────────────────────────────────────────────────


def test_emit_handoff_calls_client(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    called = {}

    def fake_handoff(target, reason=None):
        called.update(target=target, reason=reason)
        return {"ok": True}

    agent.runtime.client.emit_handoff = fake_handoff
    out = agent.governor.emit_handoff("agent:b", reason="routing")
    assert out == {"ok": True} and called["target"] == "agent:b"


# ── crash / session close ────────────────────────────────────────────────────


def test_close_emits_workflow_completed_and_blocks_new_session_noise(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, {}, {}, {}]
    agent.inboxes.messages.send(**SEND)
    agent.close()
    assert core.payloads[-1]["event_type"] == "WorkflowCompleted"
    # closing again emits nothing
    n = len(core.payloads)
    agent.close()
    assert len(core.payloads) == n


def test_close_with_error_emits_workflow_failed(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    core.queue += [{}, {}, {}, {}]
    agent.inboxes.messages.send(**SEND)
    agent.close(error="RuntimeError('boom')")
    assert core.payloads[-1]["event_type"] == "WorkflowFailed"


# ── error field must stay wire-safe (live regression, Sep 2026) ──────────────
# AgentMail SDK errors repr to kilobytes of headers with undecodable bytes;
# Core answered HTTP 400 and the ActivityCompleted(error) was dropped, so a
# real failure went unrecorded.


def test_activity_error_field_is_bounded_and_printable(core, fake_mail):
    huge = "x" * 9000 + "\udcff\x00\x07" + "y" * 9000

    class Boom(Exception):
        def __str__(self):
            return huge

    def explode(**kw):
        raise Boom()

    fake_mail.inboxes.messages.send = explode
    agent, _ = make_agent(core, mail=fake_mail)

    with pytest.raises(Boom):
        agent.inboxes.messages.send(inbox_id="inbox_1", to=["a@x.com"], text="t")

    completed = [p for p in core.lifecycle_payloads if p["event_type"] == "ActivityCompleted"]
    assert completed[0]["failed"] is True
    err = completed[0]["activity_output"]["error"]
    assert err.startswith("Boom:")           # type survives truncation
    assert len(err) < 1100                   # bounded
    assert err.endswith("...[truncated]")
    assert all(c.isprintable() or c == " " for c in err)
    err.encode("utf-8")                      # must not raise


def test_error_text_survives_a_broken_str(core, fake_mail):
    class Nasty(Exception):
        def __str__(self):
            raise RuntimeError("cannot stringify")

    def explode(**kw):
        raise Nasty()

    fake_mail.inboxes.messages.send = explode
    agent, _ = make_agent(core, mail=fake_mail)

    with pytest.raises(Nasty):
        agent.inboxes.messages.send(inbox_id="inbox_1", to=["a@x.com"], text="t")

    completed = [p for p in core.lifecycle_payloads if p["event_type"] == "ActivityCompleted"]
    assert completed[0]["activity_output"]["error"] == "Nasty: <unprintable>"
