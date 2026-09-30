"""HTTP proxy: agent's MCP client -> this app -> ``mcp.agentmail.to/mcp``.

Only ``tools/call`` is inspected: the tool name resolves through
``catalog.classify_mcp_tool``, the arguments become ``activity_input`` (with
guardrail redactions forwarded upstream in place of the originals), and the
JSON-RPC response becomes ``activity_output``.

Unknown tool names are governed as ``ActionClass.UNKNOWN`` writes — they are
evaluated like everything else and fail closed on Core outage.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from openbox_core.errors import ContractError, GuardrailsValidationError

from ..catalog import classify_mcp_tool
from ..config import AGENTMAIL_MCP_URL
from ..errors import (
    AgentMailBlockedError,
    AgentMailHaltedError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    GovernanceAPIError,
    UncataloguedActionError,
)
from ..governor import MailGovernor

__all__ = ["AgentMailMCPProxy", "create_mcp_proxy_app", "MCPUpstreamError"]

logger = logging.getLogger(__name__)

_HOP_BY_HOP = frozenset(
    {"host", "content-length", "connection", "transfer-encoding", "keep-alive", "te", "trailer", "upgrade"}
)
_JSON = {"content-type": "application/json"}


class MCPUpstreamError(GovernanceAPIError):
    """The upstream MCP endpoint returned a non-2xx response."""

    def __init__(self, status: int, body: bytes):
        super().__init__(f"upstream MCP returned {status}")
        self.status = status
        self.body = body


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _to_snake(args: Mapping[str, Any]) -> dict[str, Any]:
    """Hosted MCP tools take camelCase (``inboxId``); the catalogue contracts
    and policies use snake_case (``inbox_id``)."""
    return {_CAMEL_RE.sub("_", k).lower(): v for k, v in args.items()}


def _to_camel(args: Mapping[str, Any], original_keys: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        head, *rest = k.split("_")
        camel = head + "".join(p.title() for p in rest)
        out[camel if camel in original_keys or k not in original_keys else k] = v
    return out


def _parse_rpc_body(content_type: str, body: bytes) -> dict[str, Any]:
    """Streamable HTTP may answer ``application/json`` or ``text/event-stream``;
    for SSE the JSON-RPC response is the last ``data:`` event."""
    if "text/event-stream" in content_type:
        data = [ln[5:].strip() for ln in body.decode("utf-8", "replace").splitlines() if ln.startswith("data:")]
        if not data:
            raise ValueError("empty SSE response from upstream MCP")
        return json.loads(data[-1])
    return json.loads(body)


class AgentMailMCPProxy:
    """Args:
        agentmail_api_key: held by the proxy and sent upstream as ``x-api-key``;
            client-supplied credentials are never forwarded.
        local_token: clients must send ``Authorization: Bearer <local_token>``.
            ``None`` disables local auth (only for loopback development).
    """

    def __init__(
        self,
        governor: MailGovernor,
        *,
        upstream_url: str = AGENTMAIL_MCP_URL,
        agentmail_api_key: str | None = None,
        local_token: str | None = None,
        tool_type_map: dict[str, str] | None = None,
        http_client: Any = None,
    ):
        self.governor = governor
        self.upstream_url = upstream_url.rstrip("/") or upstream_url
        self.agentmail_api_key = agentmail_api_key
        self.local_token = local_token
        self.tool_type_map = tool_type_map
        # Populated by tools/list relays: tool name -> ActionClass value.
        self.tool_report: dict[str, str] = {}
        self.unknown_tools: list[str] = []
        if http_client is None:
            import httpx

            http_client = httpx.AsyncClient(timeout=60.0)
        self._http = http_client

    # ── forwarding ───────────────────────────────────────────────────────

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        if self.local_token is None:
            return True
        got = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
        return hmac.compare_digest(got, f"Bearer {self.local_token}")

    def _fwd_headers(self, headers: Mapping[str, str]) -> dict[str, str]:
        drop = set(_HOP_BY_HOP)
        if self.agentmail_api_key or self.local_token:
            drop |= {"authorization", "x-api-key"}
        out = {k: v for k, v in headers.items() if k.lower() not in drop}
        if self.agentmail_api_key:
            out["x-api-key"] = self.agentmail_api_key
        return out

    async def _forward(
        self, method: str, headers: Mapping[str, str], body: bytes, query: str = ""
    ) -> tuple[int, dict[str, str], bytes]:
        url = self.upstream_url + (f"?{query}" if query else "")
        resp = await self._http.request(
            method, url, content=body or None, headers=self._fwd_headers(headers)
        )
        out_headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP
        }
        return resp.status_code, out_headers, resp.content

    # ── tools/call ───────────────────────────────────────────────────────

    def _jsonrpc_error(self, req_id: Any, message: str, code: int = -32000) -> tuple[int, dict, bytes]:
        payload = {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        return 200, dict(_JSON), json.dumps(payload).encode()

    def _tool_error(self, req_id: Any, message: str) -> tuple[int, dict, bytes]:
        """Governance denials are tool results with ``isError: true`` so the
        model sees the reason and can adapt (ARCHITECTURE §4.3)."""
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": f"Blocked by OpenBox: {message}"}], "isError": True},
        }
        return 200, dict(_JSON), json.dumps(payload).encode()

    async def _governed_call(
        self, req: dict[str, Any], headers: Mapping[str, str], query: str
    ) -> tuple[int, dict, bytes]:
        req_id = req.get("id")
        params = req.get("params") or {}
        name = params.get("name") or ""
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._jsonrpc_error(req_id, "tools/call arguments must be an object", -32602)

        spec = classify_mcp_tool(name, self.tool_type_map)
        original_keys = set(arguments)

        upstream_resp_headers: dict[str, str] = {}

        async def call_upstream(final_args: dict[str, Any]) -> dict[str, Any]:
            upstream_args = _to_camel(final_args, original_keys)
            fwd = {**req, "params": {**params, "arguments": upstream_args}}
            status, h, resp_body = await self._forward("POST", headers, json.dumps(fwd).encode(), query)
            upstream_resp_headers.update(h)
            if status >= 400:
                raise MCPUpstreamError(status, resp_body)
            envelope = _parse_rpc_body(h.get("content-type", ""), resp_body)
            if "error" in envelope:
                raise MCPUpstreamError(200, json.dumps(envelope).encode())
            # activity_output = the tool result (content blocks), not the envelope
            return envelope.get("result") or {}

        try:
            result = await self.governor.arun(spec, _to_snake(arguments), call_upstream)
            value = getattr(result, "value", result)  # unwrap GovernedResult
            if isinstance(value, dict):
                value.pop("action", None)
            out_headers = dict(_JSON)
            # Streamable-HTTP session continuity: the client's session id must
            # come back on governed responses, not only relayed ones.
            for k, v in upstream_resp_headers.items():
                if k.lower() == "mcp-session-id":
                    out_headers["mcp-session-id"] = v
            return 200, out_headers, json.dumps({"jsonrpc": "2.0", "id": req_id, "result": value}).encode()
        except MCPUpstreamError as e:
            return e.status, dict(_JSON), e.body
        except (
            AgentMailBlockedError,
            AgentMailHaltedError,
            ApprovalRejectedError,
            ApprovalExpiredError,
            ApprovalTimeoutError,
            GuardrailsValidationError,
            ContractError,
            UncataloguedActionError,
        ) as e:
            return self._tool_error(req_id, str(e))
        except GovernanceAPIError as e:
            return self._tool_error(req_id, f"governance unavailable: {e}")

    # ── entry points ─────────────────────────────────────────────────────

    async def ahandle(
        self, method: str, headers: Mapping[str, str], body: bytes, query: str = ""
    ) -> tuple[int, dict, bytes]:
        if not self._authorized(headers):
            return 401, dict(_JSON), b'{"error": "unauthorized"}'
        if method == "POST":
            try:
                req = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._jsonrpc_error(None, "invalid JSON", -32700)
            # Batch requests: govern each tools/call individually is out of
            # scope — refuse rather than partially govern.
            if isinstance(req, list):
                return self._jsonrpc_error(None, "batched JSON-RPC is not supported by the governed proxy", -32600)
            if isinstance(req, dict) and req.get("method") == "tools/call":
                return await self._governed_call(req, headers, query)
            if isinstance(req, dict) and req.get("method") == "tools/list":
                return await self._tools_list(method, headers, body, query)
        return await self._forward(method, headers, body, query)

    async def _tools_list(
        self, method: str, headers: Mapping[str, str], body: bytes, query: str
    ) -> tuple[int, dict, bytes]:
        """Relay ``tools/list`` verbatim, then classify every advertised tool.
        New upstream tools land in ``unknown_tools`` and are governed as
        UNKNOWN writes if called."""
        status, out_headers, resp_body = await self._forward(method, headers, body, query)
        if status < 400:
            try:
                envelope = _parse_rpc_body(out_headers.get("content-type", ""), resp_body)
                tools = (envelope.get("result") or {}).get("tools") or []
                for tool in tools:
                    name = tool.get("name")
                    if not isinstance(name, str):
                        continue
                    spec = classify_mcp_tool(name, self.tool_type_map)
                    self.tool_report[name] = spec.action_class.value
                self.unknown_tools = [
                    n for n, c in self.tool_report.items() if c == "unknown"
                ]
                if self.unknown_tools:
                    logger.warning("MCP tools without catalogue classification: %s", self.unknown_tools)
            except Exception as e:  # noqa: BLE001 — reporting must not break the relay
                logger.warning("tools/list classification failed: %s", e)
        return status, out_headers, resp_body

    def asgi(self):
        proxy = self

        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            chunks = []
            more = True
            while more:
                msg = await receive()
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            status, out_headers, out_body = await proxy.ahandle(
                scope.get("method", "GET"),
                headers,
                b"".join(chunks),
                scope.get("query_string", b"").decode("latin-1"),
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in out_headers.items()]
                    or [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": out_body})

        return app


def create_mcp_proxy_app(
    governor: MailGovernor,
    *,
    upstream_url: str = AGENTMAIL_MCP_URL,
    agentmail_api_key: str | None = None,
    local_token: str | None = None,
    tool_type_map: dict[str, str] | None = None,
    http_client: Any = None,
):
    """ASGI app ready for uvicorn/FastAPI mounting."""
    return AgentMailMCPProxy(
        governor,
        upstream_url=upstream_url,
        agentmail_api_key=agentmail_api_key,
        local_token=local_token,
        tool_type_map=tool_type_map,
        http_client=http_client,
    ).asgi()
