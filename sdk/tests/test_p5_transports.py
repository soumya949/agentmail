"""P5: WebSocket inbound, MCP stdio transport, MCP session/tools-list
handling, Flask + FastAPI relay adapters."""

from __future__ import annotations

import contextlib
import json
import threading

import pytest
from conftest import INBOUND_EVENT, make_agent

from openbox_agentmail.mcp_proxy import AgentMailMCPProxy
from openbox_agentmail.mcp_proxy.stdio import StdioMCPProxy
from openbox_agentmail.webhook_relay import InboundRelay, WebhookAuth
from openbox_agentmail.websocket_inbound import WebsocketInbound

AUTH = {"authorization": "Bearer hook-secret"}


def _event(**over):
    ev = json.loads(json.dumps(INBOUND_EVENT))
    ev.update(over)
    return ev


# ── WebSocket inbound ────────────────────────────────────────────────────────


class FakeWS:
    def __init__(self, messages):
        self.messages = messages
        self.subscribed = []
        self.closed = False

    def __iter__(self):
        return iter(self.messages)

    def send_subscribe(self, msg):
        self.subscribed.append(msg)

    def close(self):
        self.closed = True


def _connect_factory(*sockets_or_errors):
    """connect() returns a context manager yielding each FakeWS in turn."""
    items = list(sockets_or_errors)

    def connect():
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item

        @contextlib.contextmanager
        def cm():
            yield item

        return cm()

    return connect


def make_ws(core, fake_mail, sockets, **kw):
    agent, _ = make_agent(core, mail=fake_mail)
    return WebsocketInbound(agent.governor, connect=_connect_factory(*sockets), **kw)


def test_ws_content_event_screened_and_delivered(core, fake_mail):
    delivered = []
    ws = FakeWS([_event()])
    inbound = make_ws(core, fake_mail, [ws], handler=delivered.append)

    inbound.listen()

    assert len(delivered) == 1
    assert delivered[0].verdict == "allow"
    assert core.lifecycle_payloads[0]["event_type"] == "WorkflowStarted"
    assert core.lifecycle_payloads[1]["activity_type"] == "agentmail.receive_message"
    assert [n for n, _ in fake_mail.calls] == ["messages.get"]


def test_ws_blocked_event_never_reaches_handler(core, fake_mail):
    delivered, blocked = [], []
    ws = FakeWS([_event()])
    core.queue += [{}, {"verdict": "block", "reason": "phish"}]
    inbound = make_ws(core, fake_mail, [ws], handler=delivered.append,
                      on_blocked=lambda ev, e: blocked.append(str(e)))

    inbound.listen()

    assert delivered == [] and len(blocked) == 1


def test_ws_duplicate_event_dropped(core, fake_mail):
    delivered = []
    ws = FakeWS([_event(), _event()])  # same event_id twice
    inbound = make_ws(core, fake_mail, [ws], handler=delivered.append)

    inbound.listen()

    assert len(delivered) == 1
    started = [p for p in core.lifecycle_payloads if p["event_type"] == "ActivityStarted"]
    assert len(started) == 1  # only screened once
    assert len(fake_mail.calls) == 1


def test_ws_reconnects_and_dedupes_across_sockets(core, fake_mail):
    delivered = []
    dead = ConnectionError("socket dropped")
    ws2 = FakeWS([_event()])  # same event redelivered after reconnect
    inbound = make_ws(core, fake_mail, [dead, ws2], handler=delivered.append,
                      backoff=0.001)

    inbound.listen()

    assert len(delivered) == 1


def test_ws_stop_terminates_loop(core, fake_mail):
    delivered = []

    def blocking_handler(i):
        delivered.append(i)
        inbound.stop()

    ws = FakeWS([_event(event_id="e1"), _event(event_id="e2")])
    inbound = make_ws(core, fake_mail, [ws], handler=blocking_handler)
    t = threading.Thread(target=inbound.listen)
    t.start()
    t.join(timeout=5)

    assert delivered and not t.is_alive()


def test_ws_control_frames_ignored(core, fake_mail):
    delivered = []
    frames = [
        {"type": "subscribed"},                                  # control frame
        "not json",                                              # unparseable
        {"event": _event()},                                     # wrapped event
    ]
    ws = FakeWS(frames)
    inbound = make_ws(core, fake_mail, [ws], handler=delivered.append,
                      subscribe={"event_types": ["message.received"]})

    inbound.listen()

    assert len(delivered) == 1
    assert ws.subscribed  # subscribe message sent


def test_ws_status_events_telemetry_only(core, fake_mail):
    delivered = []
    ws = FakeWS([{"event_type": "message.delivered", "event_id": "e1",
                  "message": {"inbox_id": "inbox_1", "message_id": "m_1"}}])
    inbound = make_ws(core, fake_mail, [ws], handler=delivered.append)

    inbound.listen()

    assert delivered == []
    assert core.lifecycle_payloads[-1]["signal_name"] == "agentmail.message_delivered"


# ── MCP stdio ────────────────────────────────────────────────────────────────


class FakeUpstream:
    def __init__(self):
        self.requests = []
        self.resp_headers = {}

    async def request(self, method, url, content=None, headers=None):
        body = json.loads(content) if content else {}
        self.requests.append(body)
        req_id = body.get("id")
        if body.get("method") == "tools/call":
            payload = {"jsonrpc": "2.0", "id": req_id,
                       "result": {"content": [{"type": "text", "text": "ok"}]}}
        elif body.get("method") == "tools/list":
            payload = {"jsonrpc": "2.0", "id": req_id,
                       "result": {"tools": [{"name": "send_message"}, {"name": "brand_new_tool"}]}}
        else:
            payload = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        import httpx

        return httpx.Response(200, json=payload, headers=self.resp_headers)


async def _aiter(lines):
    for ln in lines:
        yield ln


def _stdio(core, fake_mail, upstream=None):
    agent, _ = make_agent(core, mail=fake_mail)
    up = upstream or FakeUpstream()
    proxy = AgentMailMCPProxy(agent.governor, upstream_url="https://mcp.test", http_client=up)
    return StdioMCPProxy(proxy), up


async def test_stdio_tools_call_governed(core, fake_mail):
    stdio, up = _stdio(core, fake_mail)
    written = []
    line = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "send_message",
                   "arguments": {"inboxId": "i1", "to": ["a@b.c"], "text": "hi"}},
    }).encode()

    await stdio.serve(_aiter([line]), written.append)

    assert json.loads(written[0])["result"]["content"][0]["text"] == "ok"
    assert core.lifecycle_payloads[1]["activity_type"] == "agentmail.send_message"


async def test_stdio_notification_not_answered(core, fake_mail):
    stdio, up = _stdio(core, fake_mail)
    written = []
    line = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode()

    await stdio.serve(_aiter([line]), written.append)

    assert written == []  # notifications never get responses
    assert up.requests[0]["method"] == "notifications/initialized"


async def test_stdio_block_returns_is_error(core, fake_mail):
    stdio, up = _stdio(core, fake_mail)
    core.queue += [{}, {"verdict": "block", "reason": "denied"}]
    written = []
    line = json.dumps({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "send_message",
                   "arguments": {"inboxId": "i1", "to": ["a@b.c"], "text": "hi"}},
    }).encode()

    await stdio.serve(_aiter([line]), written.append)

    resp = json.loads(written[0])
    assert resp["id"] == 9 and resp["result"]["isError"] is True
    assert up.requests == []


async def test_stdio_batch_and_garbage(core, fake_mail):
    stdio, _ = _stdio(core, fake_mail)
    written = []
    lines = [b"not json at all", json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "x"}]).encode()]
    await stdio.serve(_aiter(lines), written.append)
    assert len(written) == 2
    assert json.loads(written[0])["error"]["code"] == -32700
    assert json.loads(written[1])["error"]["code"] == -32600


# ── MCP session id + tools/list report ───────────────────────────────────────


async def test_mcp_session_id_returned_on_governed_call(core, fake_mail):
    upstream = FakeUpstream()
    upstream.resp_headers = {"mcp-session-id": "sess_42"}
    agent, _ = make_agent(core, mail=fake_mail)
    proxy = AgentMailMCPProxy(agent.governor, upstream_url="https://mcp.test", http_client=upstream)
    call = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_inboxes", "arguments": {}},
    }).encode()

    _, headers, _ = await proxy.ahandle("POST", {}, call)

    assert headers.get("mcp-session-id") == "sess_42"
    assert upstream.requests[0]["method"] == "tools/call"


async def test_tools_list_classification_report(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    upstream = FakeUpstream()
    proxy = AgentMailMCPProxy(agent.governor, upstream_url="https://mcp.test", http_client=upstream)
    req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}).encode()

    status, _, body = await proxy.ahandle("POST", {}, req)

    assert status == 200
    assert proxy.tool_report["send_message"] == "send"
    assert proxy.unknown_tools == ["brand_new_tool"]
    assert json.loads(body)["result"]["tools"][1]["name"] == "brand_new_tool"


# ── Flask / FastAPI relay adapters ───────────────────────────────────────────


def _relay(core, fake_mail, **kw):
    agent, _ = make_agent(core, mail=fake_mail)
    kw.setdefault("auth", WebhookAuth(secret="hook-secret"))
    kw.setdefault("handler", lambda i: None)
    return InboundRelay(agent.governor, **kw)


def test_flask_blueprint(core, fake_mail):
    flask = pytest.importorskip("flask")
    relay = _relay(core, fake_mail)
    app = flask.Flask(__name__)
    app.register_blueprint(relay.flask_blueprint())
    client = app.test_client()

    resp = client.post("/agentmail-webhook", data=json.dumps(_event()),
                       content_type="application/json",
                       headers={"Authorization": "Bearer hook-secret"})

    assert resp.status_code == 200 and resp.get_json()["status"] == "delivered"
    resp = client.post("/agentmail-webhook", data="{}", content_type="application/json")
    assert resp.status_code == 401


async def test_fastapi_router(core, fake_mail):
    pytest.importorskip("fastapi")

    relay = _relay(core, fake_mail)
    router = relay.fastapi_router()
    endpoint = router.routes[0].endpoint

    class _Req:
        headers = {"authorization": "Bearer hook-secret"}

        async def body(self):
            return json.dumps(_event()).encode()

    resp = await endpoint(_Req())
    assert resp.status_code == 200
    assert json.loads(resp.body)["status"] == "delivered"
