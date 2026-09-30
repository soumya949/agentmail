"""Inbound relay: auth, dedupe, enforced screening, redaction delivery, ASGI."""

import json

from conftest import INBOUND_EVENT, make_agent

from openbox_agentmail.webhook_relay import (
    GovernedInbound,
    InboundRelay,
    MemoryDedupe,
    WebhookAuth,
)

AUTH = {"authorization": "Bearer hook-secret"}


def make_relay(core, fake_mail, delivered=None, **kw):
    agent, _ = make_agent(core, mail=fake_mail)
    seen = delivered if delivered is not None else []
    kw.setdefault("auth", WebhookAuth(secret="hook-secret"))
    kw.setdefault("handler", lambda inbound: seen.append(inbound))
    relay = InboundRelay(agent.governor, **kw)
    return relay, seen


def body(event=INBOUND_EVENT):
    return json.dumps(event).encode()


async def test_auth_failure_rejected(core, fake_mail):
    relay, seen = make_relay(core, fake_mail)
    resp = await relay.ahandle({"authorization": "Bearer wrong"}, body())
    assert resp.status == 401
    assert seen == []


async def test_no_auth_config_rejects_everything(core, fake_mail):
    relay, seen = make_relay(core, fake_mail, auth=WebhookAuth())
    resp = await relay.ahandle(AUTH, body())
    assert resp.status == 401


async def test_malformed_payload_400(core, fake_mail):
    relay, _ = make_relay(core, fake_mail)
    assert (await relay.ahandle(AUTH, b"{not json")).status == 400
    assert (await relay.ahandle(AUTH, b'{"foo": 1}')).status == 400
    assert (await relay.ahandle(AUTH, b'{"event_type": "alien.event"}')).status == 400
    assert (await relay.ahandle(AUTH, b'{"event_type": "message.received"}')).status == 400


async def test_allowed_event_delivered(core, fake_mail):
    relay, seen = make_relay(core, fake_mail)
    resp = await relay.ahandle(AUTH, body())
    assert resp.status == 200
    assert resp.body["status"] == "delivered"
    assert len(seen) == 1
    inbound = seen[0]
    assert isinstance(inbound, GovernedInbound)
    assert inbound.fields["sender_domain"] == "evil.example"
    # governed as a receive_message activity (fetch_on_receive default)
    types = [p["event_type"] for p in core.lifecycle_payloads]
    assert types == ["WorkflowStarted", "ActivityStarted", "ActivityCompleted"]
    assert core.lifecycle_payloads[1]["activity_type"] == "agentmail.receive_message"


async def test_blocked_event_not_delivered_but_acked(core, fake_mail):
    core.queue.extend([{}, {"verdict": "block", "reason": "spam source"}])
    relay, seen = make_relay(core, fake_mail)
    resp = await relay.ahandle(AUTH, body())
    assert resp.status == 200  # decided — AgentMail must not retry
    assert resp.body["status"] == "blocked"
    assert seen == []


async def test_on_blocked_callback(core, fake_mail):
    core.queue.extend([{}, {"verdict": "block", "reason": "spam"}])
    quarantined = []
    relay, seen = make_relay(core, fake_mail, on_blocked=lambda ev, e: quarantined.append(ev["event_id"]))
    resp = await relay.ahandle(AUTH, body())
    assert resp.body["status"] == "blocked"
    assert quarantined == ["evt_1"]
    assert seen == []


async def test_inbound_guardrail_failure_blocks(core, fake_mail):
    core.queue.extend([{}, {"verdict": "allow",
                            "guardrails": {"validation_passed": False, "reasons": [{"reason": "pii"}]}}])
    relay, seen = make_relay(core, fake_mail)
    resp = await relay.ahandle(AUTH, body())
    assert resp.body["status"] == "blocked"
    assert seen == []


async def test_deduped_event_id(core, fake_mail):
    relay, seen = make_relay(core, fake_mail)
    await relay.ahandle(AUTH, body())
    resp = await relay.ahandle(AUTH, body())
    assert resp.body["status"] == "duplicate"
    assert len(seen) == 1


REAL_SENT_EVENT = {  # agentmail.events.types.MessageSentEvent shape
    "type": "event", "event_type": "message.sent", "event_id": "evt_sent_1",
    "send": {"inbox_id": "inbox_1", "thread_id": "t_1", "message_id": "m_out_1",
             "timestamp": "2026-09-29T05:00:00Z", "recipients": ["bob@example.com"]},
}
REAL_BOUNCED_EVENT = {
    "type": "event", "event_type": "message.bounced", "event_id": "evt_b_1",
    "bounce": {"inbox_id": "inbox_1", "thread_id": "t_1", "message_id": "m_out_1",
               "timestamp": "2026-09-29T05:00:00Z", "type": "Permanent",
               "sub_type": "General", "recipients": [{"address": "x@nowhere.example"}]},
}


async def test_real_status_event_shapes_accepted_and_recorded(core, fake_mail):
    """Real AgentMail status events carry send/delivery/bounce, NOT message —
    they must be recorded (200), not rejected (400 → endless retries)."""
    relay, seen = make_relay(core, fake_mail)
    for ev in (REAL_SENT_EVENT, REAL_BOUNCED_EVENT):
        resp = await relay.ahandle(AUTH, json.dumps(ev).encode())
        assert resp.status == 200, resp.body
        assert resp.body["status"] == "recorded"
    assert seen == []
    signals = [p for p in core.lifecycle_payloads if p["event_type"] == "SignalReceived"]
    sent = next(p for p in signals if p["agentmail_event_type"] == "message.sent")
    assert sent["signal_name"] == "agentmail.message_sent"
    assert sent["inbox_id"] == "inbox_1"
    assert sent["message_id"] == "m_out_1"
    assert sent["recipients"] == ["bob@example.com"]
    bounced = next(p for p in signals if p["agentmail_event_type"] == "message.bounced")
    assert bounced["bounce_type"] == "Permanent"


async def test_content_event_still_requires_message(core, fake_mail):
    relay, _ = make_relay(core, fake_mail)
    resp = await relay.ahandle(AUTH, b'{"event_type": "message.received", "send": {}}')
    assert resp.status == 400


async def test_status_events_recorded_not_delivered(core, fake_mail):
    relay, seen = make_relay(core, fake_mail)
    event = {"event_type": "message.delivered", "event_id": "e9",
             "message": {"message_id": "m_1"}}
    resp = await relay.ahandle(AUTH, json.dumps(event).encode())
    assert resp.body["status"] == "recorded"
    assert seen == []
    types = [p["event_type"] for p in core.lifecycle_payloads]
    assert "SignalReceived" in types


async def test_redacted_content_reaches_handler(core, fake_mail):
    core.queue.extend([
        {},
        {"verdict": "allow",
         "guardrails": {"validation_passed": True, "input_type": "signal",
                        "redacted_input": {"text": "[scrubbed]"}}},
    ])
    relay, seen = make_relay(core, fake_mail)
    resp = await relay.ahandle(AUTH, body())
    assert resp.body["status"] == "delivered"
    assert seen[0].event["message"]["text"] == "[scrubbed]"


async def test_handler_error_returns_500_for_retry(core, fake_mail):
    def boom(_inbound):
        raise RuntimeError("app down")

    relay, _ = make_relay(core, fake_mail, handler=boom)
    resp = await relay.ahandle(AUTH, body())
    assert resp.status == 500


async def test_memory_dedupe_ttl():
    d = MemoryDedupe(ttl_seconds=0)
    assert await d.seen_or_add("e1") is False
    assert await d.seen_or_add("e1") is True  # within ttl=0? expiry == now
    # ttl 0 entries expire on next call
    d2 = MemoryDedupe(ttl_seconds=3600)
    assert await d2.seen_or_add("e1") is False
    assert await d2.seen_or_add("e1") is True


def test_asgi_end_to_end(core, fake_mail):
    from starlette.testclient import TestClient

    relay, seen = make_relay(core, fake_mail)
    client = TestClient(relay.asgi())
    r = client.post("/webhook", content=body(), headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "delivered"
    assert len(seen) == 1
    r2 = client.post("/webhook", content=body(), headers={"authorization": "bad"})
    assert r2.status_code == 401
    r3 = client.get("/webhook")
    assert r3.status_code == 405
