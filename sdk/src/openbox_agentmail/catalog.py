"""Action catalogue: maps every ``agentmail`` client method to an OpenBox
``activity_type`` and an action class. Single source of truth for the REST
proxy, toolkit adapters, and the MCP proxy.

Import-safe: no network, no heavy imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ActionClass", "ActionSpec", "lookup", "classify_mcp_tool", "ACTIVITY_PREFIX", "RECEIVE_MESSAGE"]

ACTIVITY_PREFIX = "agentmail."


class ActionClass(StrEnum):
    SEND = "send"  # leaves the org: send / reply / forward / send_draft
    DRAFT = "draft"  # creates or edits an unsent message
    MODIFY = "modify"  # deletes, label changes
    ADMIN = "admin"  # inboxes, pods, domains, keys, webhooks, lists, providers
    READ = "read"  # returns mail content into the agent
    READ_ATTACHMENT = "read_attachment"
    UNKNOWN = "unknown"

    @property
    def is_write(self) -> bool:
        return self in (
            ActionClass.SEND,
            ActionClass.DRAFT,
            ActionClass.MODIFY,
            ActionClass.ADMIN,
            ActionClass.UNKNOWN,
        )

    @property
    def is_read(self) -> bool:
        return self in (ActionClass.READ, ActionClass.READ_ATTACHMENT)


@dataclass(frozen=True)
class ActionSpec:
    action: str  # short name, e.g. "send_message"
    activity_type: str  # wire activity_type, e.g. "agentmail.send_message"
    action_class: ActionClass
    resource: str  # last resource segment, e.g. "messages"
    method: str  # client method name, e.g. "send"


# Inbound email pushed by AgentMail (webhook / websocket). Not a client method —
# the governor wraps each received message in this activity and fetches the
# authoritative copy (``messages.get``) inside it. A READ: untrusted content
# entering the agent, so reads' fail-closed and output-guardrail rules apply.
RECEIVE_MESSAGE = ActionSpec(
    action="receive_message",
    activity_type=f"{ACTIVITY_PREFIX}receive_message",
    action_class=ActionClass.READ,
    resource="messages",
    method="receive",
)


# Friendly names for the actions policy authors will reference most.
_FRIENDLY: dict[tuple[str, str], str] = {
    ("messages", "send"): "send_message",
    ("messages", "reply"): "reply_to_message",
    ("messages", "reply_all"): "reply_all",
    ("messages", "forward"): "forward_message",
    ("messages", "get"): "get_message",
    ("messages", "get_raw"): "get_raw_message",
    ("messages", "batch_get"): "batch_get_messages",
    ("messages", "list"): "list_messages",
    ("messages", "search"): "search_messages",
    ("messages", "update"): "update_message",
    ("messages", "batch_update"): "batch_update_messages",
    ("messages", "delete"): "delete_message",
    ("messages", "get_attachment"): "get_attachment",
    ("drafts", "create"): "create_draft",
    ("drafts", "update"): "update_draft",
    ("drafts", "send"): "send_draft",
    ("drafts", "delete"): "delete_draft",
    ("drafts", "get"): "get_draft",
    ("drafts", "list"): "list_drafts",
    ("drafts", "get_attachment"): "get_attachment",
    ("threads", "get"): "get_thread",
    ("threads", "list"): "list_threads",
    ("threads", "search"): "search_threads",
    ("threads", "update"): "update_thread",
    ("threads", "delete"): "delete_thread",
    ("threads", "get_attachment"): "get_attachment",
    ("inboxes", "create"): "create_inbox",
    ("inboxes", "delete"): "delete_inbox",
    ("inboxes", "get"): "get_inbox",
    ("inboxes", "list"): "list_inboxes",
    ("inboxes", "search"): "search_inboxes",
    ("inboxes", "update"): "update_inbox",
}

_SEND_METHODS = {"send", "reply", "reply_all", "forward"}
_READ_METHODS = {"get", "list", "search", "batch_get", "get_raw", "me", "query_events",
                 "query_rates", "query_usage", "get_setup_link", "get_zone_file",
                 "list_accounts", "get_headers"}
_MODIFY_METHODS = {"delete", "update", "batch_update"}
_MAIL_RESOURCES = {"messages", "drafts", "threads"}
_ADMIN_RESOURCES = {
    "inboxes", "pods", "domains", "api_keys", "webhooks", "lists", "accounts",
    "providers", "organizations", "agent", "auth", "metrics", "events", "websockets",
}


def _classify(resource: str, method: str) -> ActionClass:
    if method == "get_attachment":
        return ActionClass.READ_ATTACHMENT
    if resource in _MAIL_RESOURCES:
        if method in _SEND_METHODS:
            return ActionClass.SEND
        if resource == "drafts" and method in ("create", "update"):
            return ActionClass.DRAFT
        if method in _MODIFY_METHODS:
            return ActionClass.MODIFY
        if method in _READ_METHODS:
            return ActionClass.READ
        return ActionClass.UNKNOWN
    if resource in _ADMIN_RESOURCES:
        # Admin reads still return non-mail metadata; treat them as reads so
        # they aren't blocked on a Core outage, but they still emit activities.
        if method in _READ_METHODS:
            return ActionClass.READ
        return ActionClass.ADMIN
    return ActionClass.UNKNOWN


# Transparent wrapper resources in the AgentMail client: they return the same
# methods shaped differently (raw httpx responses / extra request options) and
# are governed identically, so they are stripped before classification.
_TRANSPARENT_SEGMENTS = frozenset({"with_raw_response", "with_options"})


def lookup(path: tuple[str, ...]) -> ActionSpec:
    """Resolve a client attribute path like ``("inboxes", "messages", "send")``.

    Raises ``KeyError`` when the method cannot be classified so callers refuse
    it rather than passing it through ungoverned.
    """
    path = tuple(s for s in path if s not in _TRANSPARENT_SEGMENTS)
    if len(path) < 2:
        raise KeyError(path)
    resource, method = path[-2], path[-1]
    action_class = _classify(resource, method)
    if action_class is ActionClass.UNKNOWN:
        raise KeyError(path)
    action = _FRIENDLY.get((resource, method), f"{resource}.{method}")
    return ActionSpec(
        action=action,
        activity_type=f"{ACTIVITY_PREFIX}{action}",
        action_class=action_class,
        resource=resource,
        method=method,
    )


# Hosted MCP tool names -> catalogue path. Anything not listed is UNKNOWN.
_MCP_TOOLS: dict[str, tuple[str, ...]] = {
    "send_message": ("messages", "send"),
    "reply_to_message": ("messages", "reply"),
    "reply_all": ("messages", "reply_all"),
    "forward_message": ("messages", "forward"),
    "get_message": ("messages", "get"),
    "list_messages": ("messages", "list"),
    "search_messages": ("messages", "search"),
    "update_message": ("messages", "update"),
    "delete_message": ("messages", "delete"),
    "get_attachment": ("messages", "get_attachment"),
    "create_draft": ("drafts", "create"),
    "update_draft": ("drafts", "update"),
    "send_draft": ("drafts", "send"),
    "delete_draft": ("drafts", "delete"),
    "get_draft": ("drafts", "get"),
    "list_drafts": ("drafts", "list"),
    "get_thread": ("threads", "get"),
    "list_threads": ("threads", "list"),
    "search_threads": ("threads", "search"),
    "update_thread": ("threads", "update"),
    "delete_thread": ("threads", "delete"),
    "create_inbox": ("inboxes", "create"),
    "delete_inbox": ("inboxes", "delete"),
    "get_inbox": ("inboxes", "get"),
    "list_inboxes": ("inboxes", "list"),
    "search_inboxes": ("inboxes", "search"),
    "update_inbox": ("inboxes", "update"),
    "list_list_entries": ("lists", "list"),
    "get_list_entry": ("lists", "get"),
    "create_list_entry": ("lists", "create"),
    "delete_list_entry": ("lists", "delete"),
    "agent_verify": ("agent", "verify"),
    "list_providers": ("providers", "list"),
    "search_providers": ("providers", "search"),
    "get_provider": ("providers", "get"),
    "connect_provider": ("providers", "connect"),
    "list_accounts": ("accounts", "list"),
    "auth_me": ("auth", "me"),
    "list_organizations": ("organizations", "list"),
    "select_organization": ("organizations", "select"),
}


# Semantic tool-type vocabulary (shared with the OpenBox LangChain/LangGraph
# middleware) -> catalogue action class.
_TYPE_TO_CLASS: dict[str, ActionClass] = {
    "email_send": ActionClass.SEND,
    "email_draft": ActionClass.DRAFT,
    "email_read": ActionClass.READ,
    "email_attachment_read": ActionClass.READ_ATTACHMENT,
    "email_modify": ActionClass.MODIFY,
    "email_admin": ActionClass.ADMIN,
}


def _class_for(tool_type: str) -> ActionClass:
    if tool_type in _TYPE_TO_CLASS:
        return _TYPE_TO_CLASS[tool_type]
    try:
        return ActionClass(tool_type)  # allow raw class values too
    except ValueError:
        return ActionClass.UNKNOWN


def classify_mcp_tool(tool_name: str, tool_type_map: dict[str, str] | None = None) -> ActionSpec:
    """Classify an MCP tool. Unknown tools get ``ActionClass.UNKNOWN`` (still
    governed, treated as a write for outage purposes, never passed silently)."""
    if tool_type_map and tool_name in tool_type_map:
        cls = _class_for(tool_type_map[tool_name])
        return ActionSpec(tool_name, f"{ACTIVITY_PREFIX}{tool_name}", cls, "mcp", tool_name)
    path = _MCP_TOOLS.get(tool_name)
    if path is not None:
        spec = lookup(path)
        # activity_type uses the MCP tool name so dashboard policies match the
        # names MCP clients show; mail tools share the REST friendly names.
        return ActionSpec(tool_name, f"{ACTIVITY_PREFIX}{tool_name}", spec.action_class, spec.resource, spec.method)
    return ActionSpec(tool_name, f"{ACTIVITY_PREFIX}{tool_name}", ActionClass.UNKNOWN, "mcp", tool_name)
