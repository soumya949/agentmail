"""OpenBox governance for AgentMail.

Import-safe root: only errors, the catalogue and version are imported eagerly.
Runtime pieces (httpx, cryptography, OTel) load on first use via the factories.
"""

from __future__ import annotations

from typing import Any

from .catalog import ActionClass, ActionSpec, classify_mcp_tool, lookup
from .errors import (
    AgentMailBlockedError,
    AgentMailHaltedError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    ContractError,
    GovernanceAPIError,
    GuardrailsValidationError,
    InboundFetchError,
    OpenBoxAuthError,
    OpenBoxConfigError,
    OpenBoxError,
    UncataloguedActionError,
)

__version__ = "0.1.0"

# Classification hints for OpenBox LangChain / LangGraph / DeepAgents middleware
# users who already govern tools there and should NOT double-wrap. Keys are the
# catalogue's friendly action names (= agentmail_toolkit tool names); values use
# the shared semantic vocabulary resolved by catalog._TYPE_TO_CLASS.
TOOL_TYPE_MAP: dict[str, str] = {
    "send_message": "email_send",
    "reply_to_message": "email_send",
    "reply_all": "email_send",
    "forward_message": "email_send",
    "send_draft": "email_send",
    "create_draft": "email_draft",
    "update_draft": "email_draft",
    "get_message": "email_read",
    "get_raw_message": "email_read",
    "get_thread": "email_read",
    "list_messages": "email_read",
    "list_threads": "email_read",
    "list_drafts": "email_read",
    "get_draft": "email_read",
    "search_threads": "email_read",
    "search_messages": "email_read",
    "get_inbox": "email_read",
    "list_inboxes": "email_read",
    "search_inboxes": "email_read",
    "get_attachment": "email_attachment_read",
    "update_message": "email_modify",
    "update_thread": "email_modify",
    "update_inbox": "email_modify",
    "delete_message": "email_modify",
    "delete_thread": "email_modify",
    "delete_draft": "email_modify",
    "create_inbox": "email_admin",
    "delete_inbox": "email_admin",
}

__all__ = [
    "__version__",
    "TOOL_TYPE_MAP",
    "ActionClass",
    "ActionSpec",
    "lookup",
    "classify_mcp_tool",
    "OpenBoxError",
    "ContractError",
    "OpenBoxConfigError",
    "OpenBoxAuthError",
    "GovernanceAPIError",
    "GuardrailsValidationError",
    "ApprovalExpiredError",
    "ApprovalRejectedError",
    "ApprovalTimeoutError",
    "AgentMailBlockedError",
    "AgentMailHaltedError",
    "InboundFetchError",
    "UncataloguedActionError",
    # lazy
    "OpenBoxMailAgent",
    "AsyncOpenBoxMailAgent",
    "create_openbox_mail_agent",
    "create_async_openbox_mail_agent",
    "AgentMailSettings",
    "MailGovernor",
    "ReceivedMessage",
    "InboundRelay",
    "GovernedInbound",
    "WebhookAuth",
    "MemoryDedupe",
    "governed_toolkit",
    "governed_tools",
    "PendingApproval",
    "DraftResolution",
    "ApprovalStore",
    "MemoryApprovalStore",
    "quarantine_on_blocked",
    "WebsocketInbound",
    "ConstraintViolation",
    "apply_constraints",
]

_LAZY = {
    "OpenBoxMailAgent": ".client",
    "AsyncOpenBoxMailAgent": ".client",
    "create_openbox_mail_agent": ".client",
    "create_async_openbox_mail_agent": ".client",
    "AgentMailSettings": ".config",
    "MailGovernor": ".governor",
    "ReceivedMessage": ".governor",
    "InboundRelay": ".webhook_relay",
    "GovernedInbound": ".webhook_relay",
    "WebhookAuth": ".webhook_relay",
    "MemoryDedupe": ".webhook_relay",
    "governed_toolkit": ".toolkit",
    "governed_tools": ".toolkit",
    "PendingApproval": ".approvals",
    "DraftResolution": ".approvals",
    "ApprovalStore": ".approvals",
    "MemoryApprovalStore": ".approvals",
    "quarantine_on_blocked": ".webhook_relay",
    "WebsocketInbound": ".websocket_inbound",
    "ConstraintViolation": ".constraints",
    "apply_constraints": ".constraints",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(name)
    import importlib

    return getattr(importlib.import_module(module, __name__), name)
