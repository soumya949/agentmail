"""Framework toolkit integration.

``agentmail_toolkit`` (OpenAI Agents SDK, LangChain, LiveKit) builds tools that
invoke ``client.inboxes.*`` directly. Passing an ``OpenBoxMailAgent`` as that
client routes every tool call through the governor — no per-tool patching.

    from agentmail_toolkit.openai import AgentMailToolkit
    from openbox_agentmail import create_openbox_mail_agent, governed_tools

    agent = create_openbox_mail_agent(surface="openai-toolkit")
    tools = governed_tools("openai", agent)
    # Agent(..., tools=tools)
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["governed_toolkit", "governed_tools", "TOOLKIT_FRAMEWORKS"]

TOOLKIT_FRAMEWORKS = {
    "openai": "agentmail_toolkit.openai",
    "langchain": "agentmail_toolkit.langchain",
    "livekit": "agentmail_toolkit.livekit",
}


def governed_toolkit(framework: str, agent: Any, **kwargs: Any) -> Any:
    """Instantiate ``agentmail_toolkit.<framework>.AgentMailToolkit`` bound to
    the governed ``agent`` (an ``OpenBoxMailAgent`` — the duck-typed proxy, so
    every tool call is evaluated). ``kwargs`` forward to the toolkit ctor."""
    module = TOOLKIT_FRAMEWORKS.get(framework)
    if module is None:
        raise ValueError(f"unknown toolkit framework {framework!r}; choose from {sorted(TOOLKIT_FRAMEWORKS)}")
    cls = importlib.import_module(module).AgentMailToolkit
    return cls(client=agent, **kwargs)


def governed_tools(framework: str, agent: Any, names: list[str] | None = None, **kwargs: Any) -> list[Any]:
    """Return the framework's tool list for ``Agent(tools=...)`` /
    ``bind_tools(...)``, every call governed by OpenBox."""
    return governed_toolkit(framework, agent, **kwargs).get_tools(names)
