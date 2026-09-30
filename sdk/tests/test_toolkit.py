"""P2: agentmail_toolkit integration — governed client injected into toolkits."""

import pytest
from conftest import make_agent

from openbox_agentmail.errors import AgentMailBlockedError
from openbox_agentmail.toolkit import governed_toolkit, governed_tools


def test_governed_toolkit_uses_governed_client(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    tk = governed_toolkit("langchain", agent)
    assert tk.client is agent
    tools = tk.get_tools()
    names = {t.name for t in tools}
    assert "send_message" in names and "get_thread" in names


def test_governed_tools_subset(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    tools = governed_tools("langchain", agent, names=["send_message"])
    assert [t.name for t in tools] == ["send_message"]


def test_toolkit_function_calls_are_governed(core, fake_mail):
    """The toolkit's own function layer calls client.inboxes.* — through our
    proxy every call emits governed activities."""
    from agentmail_toolkit import functions

    agent, _ = make_agent(core, mail=fake_mail)
    res = functions.send_message(
        agent, {"inbox_id": "inbox_1", "to": ["a@b.c"], "subject": "s", "text": "t"}
    )
    assert res.message_id == "m_sent_1"
    types = [p["event_type"] for p in core.lifecycle_payloads]
    assert types == ["WorkflowStarted", "ActivityStarted", "ActivityCompleted"]
    assert core.lifecycle_payloads[1]["activity_type"] == "agentmail.send_message"


def test_toolkit_blocked_send_never_reaches_agentmail(core, fake_mail):
    from agentmail_toolkit import functions

    core.queue.extend([{}, {"verdict": "block", "reason": "policy"}])
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(AgentMailBlockedError):
        functions.send_message(
            agent, {"inbox_id": "inbox_1", "to": ["a@b.c"], "subject": "s"}
        )
    assert fake_mail.calls == []


def test_unknown_framework_rejected(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    with pytest.raises(ValueError):
        governed_toolkit("delphi", agent)
