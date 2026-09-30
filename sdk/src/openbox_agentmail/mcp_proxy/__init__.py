"""Governed proxy for the hosted AgentMail MCP server.

Point MCP clients at this proxy instead of ``https://mcp.agentmail.to/mcp``.
``tools/call`` requests are classified by the action catalogue and governed;
all other JSON-RPC traffic (initialize, tools/list, notifications) is relayed
verbatim. Governance failures come back as JSON-RPC errors — the standard
channel MCP clients surface as tool errors.
"""

from __future__ import annotations

__all__ = ["AgentMailMCPProxy", "create_mcp_proxy_app"]


def __getattr__(name: str):
    if name in __all__:
        from . import proxy

        return getattr(proxy, name)
    raise AttributeError(name)


def _lazy():
    from .proxy import AgentMailMCPProxy, create_mcp_proxy_app

    return AgentMailMCPProxy, create_mcp_proxy_app
