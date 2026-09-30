"""``openbox-agentmail-mcp`` entry point: run the governed MCP proxy with uvicorn.

Env: standard OPENBOX_AGENTMAIL_* / OPENBOX_* config plus
  AGENTMAIL_API_KEY                    held by the proxy, sent upstream as x-api-key
  OPENBOX_AGENTMAIL_PROXY_TOKEN        clients must send Authorization: Bearer <token>
  OPENBOX_AGENTMAIL_MCP_UPSTREAM       default https://mcp.agentmail.to/mcp
  OPENBOX_AGENTMAIL_PROXY_HOST / _PORT default 127.0.0.1:8765
"""

from __future__ import annotations

import os


def main() -> None:
    import sys

    import uvicorn

    from ..config import AGENTMAIL_MCP_URL, AgentMailSettings, resolve_openbox_config
    from ..errors import OpenBoxConfigError
    from ..governor import MailGovernor
    from ..runtime import build_runtime
    from .proxy import AgentMailMCPProxy

    runtime = build_runtime(resolve_openbox_config())
    runtime.client.validate_api_key()
    runtime.install_instrumentation()
    governor = MailGovernor(runtime, AgentMailSettings(surface="mcp"))
    proxy = AgentMailMCPProxy(
        governor,
        upstream_url=os.environ.get("OPENBOX_AGENTMAIL_MCP_UPSTREAM", AGENTMAIL_MCP_URL),
        agentmail_api_key=os.environ.get("AGENTMAIL_API_KEY"),
        local_token=os.environ.get("OPENBOX_AGENTMAIL_PROXY_TOKEN"),
    )

    if "--stdio" in sys.argv:
        import asyncio

        from .stdio import serve_stdio

        asyncio.run(serve_stdio(proxy))
        return

    host = os.environ.get("OPENBOX_AGENTMAIL_PROXY_HOST", "127.0.0.1")
    if proxy.local_token is None and host not in ("127.0.0.1", "localhost", "::1"):
        raise OpenBoxConfigError("OPENBOX_AGENTMAIL_PROXY_TOKEN is required when binding beyond loopback")
    uvicorn.run(proxy.asgi(), host=host, port=int(os.environ.get("OPENBOX_AGENTMAIL_PROXY_PORT", "8765")))


if __name__ == "__main__":
    main()
