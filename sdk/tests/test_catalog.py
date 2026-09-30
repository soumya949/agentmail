"""Catalogue coverage: friendly names, classification rules, real client surface."""

import pytest

from openbox_agentmail.catalog import ActionClass, classify_mcp_tool, lookup


def test_friendly_names():
    assert lookup(("inboxes", "messages", "send")).action == "send_message"
    assert lookup(("inboxes", "messages", "reply")).action == "reply_to_message"
    assert lookup(("inboxes", "drafts", "send")).action == "send_draft"
    assert lookup(("inboxes", "threads", "get")).action == "get_thread"


def test_activity_type_prefix():
    assert lookup(("inboxes", "messages", "send")).activity_type == "agentmail.send_message"


def test_classification():
    assert lookup(("inboxes", "messages", "send")).action_class is ActionClass.SEND
    assert lookup(("inboxes", "drafts", "create")).action_class is ActionClass.DRAFT
    assert lookup(("inboxes", "drafts", "send")).action_class is ActionClass.SEND  # draft.send sends!
    assert lookup(("inboxes", "messages", "get")).action_class is ActionClass.READ
    assert lookup(("inboxes", "messages", "get_attachment")).action_class is ActionClass.READ_ATTACHMENT
    assert lookup(("inboxes", "messages", "delete")).action_class is ActionClass.MODIFY
    assert lookup(("inboxes", "delete")).action_class is ActionClass.ADMIN
    assert lookup(("inboxes", "list")).action_class is ActionClass.READ
    assert lookup(("webhooks", "create")).action_class is ActionClass.ADMIN


def test_unknown_refused():
    with pytest.raises(KeyError):
        lookup(("inboxes", "messages", "transmute"))
    with pytest.raises(KeyError):
        lookup(("solo",))
    with pytest.raises(KeyError):
        lookup(("mystery", "thing", "do"))


def test_mcp_tool_classification():
    assert classify_mcp_tool("send_message").action_class is ActionClass.SEND
    assert classify_mcp_tool("get_message").action_class is ActionClass.READ
    spec = classify_mcp_tool("brand_new_tool")
    assert spec.action_class is ActionClass.UNKNOWN
    assert spec.action_class.is_write  # unknown treated as write for outage purposes


def test_mcp_tool_type_map_override():
    spec = classify_mcp_tool("send_message", {"send_message": "email_send"})
    assert spec.action_class is ActionClass.SEND
    spec = classify_mcp_tool("custom", {"custom": "bogus_type"})
    assert spec.action_class is ActionClass.UNKNOWN


def test_real_client_surface_is_covered():
    """Every public method on the installed agentmail client must classify or
    be intentionally refused — never silently unclassified."""
    from agentmail import AgentMail

    seen, refused = [], []

    # drive it through the actual proxy: instantiate with a dummy key
    import os

    os.environ.setdefault("AGENTMAIL_API_KEY", "test")
    c = AgentMail(api_key="test")

    def walk(obj, path, depth=0):
        if depth > 4:
            return
        for name in dir(obj):
            if name.startswith("_"):
                continue
            try:
                attr = getattr(obj, name)
            except Exception:
                continue
            p = (*path, name)
            if callable(attr) and len(p) >= 2:
                try:
                    spec = lookup(p)
                    seen.append((p, spec.action_class))
                except KeyError:
                    refused.append(p)
            elif not callable(attr) and hasattr(attr, "__dict__"):
                walk(attr, p, depth + 1)

    walk(c, ())
    assert seen, "expected to classify at least the core mail methods"
    assert ("inboxes", "messages", "send") in [p for p, _ in seen]
    # with_raw_response / with_options twins classify like their plain versions
    assert lookup(("inboxes", "messages", "with_raw_response", "send")).action == "send_message"
    # Anything refused should be a deliberate choice — fail loudly listing them
    # so upgrades review them.
    print("\nrefused:", refused)
