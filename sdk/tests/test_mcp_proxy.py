"""P3: governed MCP proxy — tools/call governed, other methods relayed.

Arguments use the hosted server's real camelCase schema (mcp-manifest.json)."""

import json

import httpx
from conftest import make_agent

from openbox_agentmail.mcp_proxy import AgentMailMCPProxy


class FakeUpstream:
    """Records forwarded MCP requests and replies with canned JSON-RPC."""

    def __init__(self, sse: bool = False, rpc_error: bool = False):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.sse = sse
        self.rpc_error = rpc_error

    async def request(self, method, url, content=None, headers=None):
        body = json.loads(content) if content else {}
        self.requests.append(body)
        self.headers.append(dict(headers or {}))
        req_id = body.get("id")
        if body.get("method") == "tools/call":
            if self.rpc_error:
                payload = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "bad"}}
            else:
                payload = {"jsonrpc": "2.0", "id": req_id,
                           "result": {"content": [{"type": "text", "text": "sent: m_1"}]}}
        else:
            payload = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if self.sse:
            text = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=payload)


def make_proxy(core, fake_mail, upstream=None, **kw):
    agent, _ = make_agent(core, mail=fake_mail)
    up = upstream or FakeUpstream()
    proxy = AgentMailMCPProxy(agent.governor, upstream_url="https://mcp.test", http_client=up, **kw)
    return proxy, up


def tool_call(name, arguments, req_id=1):
    return json.dumps({
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()


SEND_ARGS = {"inboxId": "inbox_1", "to": ["a@b.c"], "subject": "hi", "text": "yo"}


async def test_send_message_tool_governed(core, fake_mail):
    proxy, up = make_proxy(core, fake_mail)
    status, _, body = await proxy.ahandle("POST", {}, tool_call("send_message", SEND_ARGS))
    assert status == 200
    resp = json.loads(body)
    assert resp["result"]["content"][0]["text"] == "sent: m_1"
    assert resp["id"] == 1
    # upstream still receives camelCase
    assert up.requests[0]["params"]["arguments"]["inboxId"] == "inbox_1"
    started = core.lifecycle_payloads[1]
    assert started["activity_type"] == "agentmail.send_message"
    ai = started["activity_input"]
    assert ai["inbox_id"] == "inbox_1"
    assert ai["recipient_domains"] == ["b.c"]
    completed = core.lifecycle_payloads[2]
    assert completed["activity_output"]["content"][0]["text"] == "sent: m_1"


async def test_camelcase_contract_validation(core, fake_mail):
    # missing inboxId must fail locally as ContractError -> isError result
    proxy, up = make_proxy(core, fake_mail)
    _, _, body = await proxy.ahandle("POST", {}, tool_call("send_message", {"to": ["a@b.c"]}))
    assert json.loads(body)["result"]["isError"] is True
    assert up.requests == []
    assert core.payloads == []


async def test_blocked_tool_returns_is_error_result(core, fake_mail):
    core.queue.extend([{}, {"verdict": "block", "reason": "no external"}])
    proxy, up = make_proxy(core, fake_mail)
    status, _, body = await proxy.ahandle("POST", {}, tool_call("send_message", SEND_ARGS, req_id=7))
    assert status == 200
    resp = json.loads(body)
    assert resp["id"] == 7
    assert resp["result"]["isError"] is True
    assert "no external" in resp["result"]["content"][0]["text"]
    assert up.requests == []


async def test_unknown_tool_still_governed(core, fake_mail):
    proxy, _ = make_proxy(core, fake_mail)
    status, _, _ = await proxy.ahandle("POST", {}, tool_call("daydream", {"x": 1}))
    assert status == 200
    assert core.lifecycle_payloads[1]["activity_input"]["action_class"] == "unknown"


async def test_manifest_tools_classified(core, fake_mail):
    from openbox_agentmail.catalog import ActionClass, classify_mcp_tool

    for name in ("list_list_entries", "search_inboxes", "auth_me", "list_providers"):
        assert classify_mcp_tool(name).action_class is ActionClass.READ
    for name in ("create_list_entry", "connect_provider", "agent_verify", "update_inbox"):
        assert classify_mcp_tool(name).action_class is ActionClass.ADMIN


async def test_non_tool_methods_relayed_verbatim(core, fake_mail):
    proxy, up = make_proxy(core, fake_mail)
    req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    status, _, _ = await proxy.ahandle("POST", {}, json.dumps(req).encode())
    assert status == 200
    assert up.requests[0]["method"] == "tools/list"
    assert core.payloads == []


async def test_redacted_args_forwarded(core, fake_mail):
    core.queue.extend([{}, {
        "verdict": "allow",
        "guardrails": {"validation_passed": True, "input_type": "activity_input",
                       "redacted_input": {"text": "[redacted]"}},
    }])
    proxy, up = make_proxy(core, fake_mail)
    await proxy.ahandle("POST", {}, tool_call("send_message", {**SEND_ARGS, "text": "secret"}))
    assert up.requests[0]["params"]["arguments"]["text"] == "[redacted]"


async def test_sse_upstream_response_parsed(core, fake_mail):
    proxy, _ = make_proxy(core, fake_mail, upstream=FakeUpstream(sse=True))
    _, _, body = await proxy.ahandle("POST", {}, tool_call("send_message", SEND_ARGS))
    assert json.loads(body)["result"]["content"][0]["text"] == "sent: m_1"


async def test_upstream_rpc_error_passed_through(core, fake_mail):
    proxy, _ = make_proxy(core, fake_mail, upstream=FakeUpstream(rpc_error=True))
    _, _, body = await proxy.ahandle("POST", {}, tool_call("send_message", SEND_ARGS))
    assert json.loads(body)["error"]["code"] == -32602
    assert core.lifecycle_payloads[-1]["event_type"] == "ActivityCompleted"


async def test_local_token_and_key_injection(core, fake_mail):
    proxy, up = make_proxy(core, fake_mail, agentmail_api_key="am_secret", local_token="tok")
    status, _, _ = await proxy.ahandle("POST", {}, tool_call("send_message", SEND_ARGS))
    assert status == 401
    assert up.requests == []
    status, _, _ = await proxy.ahandle(
        "POST", {"Authorization": "Bearer tok", "x-api-key": "client-key"},
        tool_call("send_message", SEND_ARGS))
    assert status == 200
    sent = {k.lower(): v for k, v in up.headers[0].items()}
    assert sent["x-api-key"] == "am_secret"
    assert "authorization" not in sent


async def test_batch_refused(core, fake_mail):
    proxy, _ = make_proxy(core, fake_mail)
    batch = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]).encode()
    _, _, body = await proxy.ahandle("POST", {}, batch)
    assert json.loads(body)["error"]["code"] == -32600
