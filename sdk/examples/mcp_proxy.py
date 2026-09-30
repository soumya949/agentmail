"""Run the governed MCP proxy: point your MCP client at http://127.0.0.1:8765/mcp
instead of https://mcp.agentmail.to/mcp. tools/call is governed; everything
else is relayed verbatim.

Equivalent: `openbox-agentmail-mcp` console script.
"""

from openbox_agentmail import AgentMailSettings, MailGovernor
from openbox_agentmail.mcp_proxy import create_mcp_proxy_app
from openbox_agentmail.runtime import create_openbox_runtime

runtime = create_openbox_runtime()
governor = MailGovernor(runtime, AgentMailSettings(surface="mcp"))
app = create_mcp_proxy_app(governor)

# uvicorn examples.mcp_proxy:app --port 8765
