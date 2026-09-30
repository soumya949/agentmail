"""P6: CONSTRAIN vocabulary, attachment screening, patch retries,
metadata_only wire shape, cookbook fixtures."""

from __future__ import annotations

import json
import pathlib

import pytest
from conftest import INBOUND_EVENT, make_agent, make_runtime

from openbox_agentmail import AgentMailSettings
from openbox_agentmail.constraints import ConstraintViolation, apply_constraints
from openbox_agentmail.errors import AgentMailBlockedError

SEND = dict(inbox_id="inbox_1", to=["alice@example.com"], subject="hi", text="hello")


def _constrain(*constraints):
    return {"verdict": "constrain", "constraints": list(constraints)}


def _started(core, activity_type="agentmail.send_message"):
    return [p for p in core.payloads if p.get("activity_type") == activity_type]


def _started_events(core, activity_type="agentmail.send_message"):
    return [p for p in _started(core, activity_type) if p.get("event_type") == "ActivityStarted"]


def _completed(core, activity_type="agentmail.send_message"):
    return [
        p for p in _started(core, activity_type)
        if p.get("event_type") == "ActivityCompleted"
    ]


# ── CONSTRAIN ────────────────────────────────────────────────────────────────


def test_constrain_strip_attachments_applied(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "strip_attachments"}), {}]
    agent.inboxes.messages.send(**SEND, attachments=[{"filename": "a.pdf", "content": "eHl6"}])

    kw = mail.calls[0][1]
    assert kw["attachments"] == []
    # dashboard sees what was applied
    assert _completed(core)[0]["applied_constraints"] == ["strip_attachments"]


def test_constrain_force_bcc_applied(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "force_bcc", "address": "audit@corp.example"}), {}]
    agent.inboxes.messages.send(**SEND)

    assert mail.calls[0][1]["bcc"] == ["audit@corp.example"]


def test_constrain_allowed_domains_pass(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "allowed_domains", "domains": ["example.com"]}), {}]
    agent.inboxes.messages.send(**SEND)
    assert [n for n, _ in mail.calls] == ["messages.send"]


# An unsatisfiable CONSTRAIN fails closed immediately and never polls — see
# test_bare_constrain_on_write_fails_closed_without_polling for why.


def test_constrain_violation_fails_closed(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "allowed_domains", "domains": ["corp.example"]})]
    with pytest.raises(AgentMailBlockedError, match="could not be satisfied"):
        agent.inboxes.messages.send(**SEND)
    assert mail.calls == []
    assert core.approval_requests == []


def test_constrain_max_recipients_violation_fails_closed(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "max_recipients", "count": 1})]
    with pytest.raises(AgentMailBlockedError, match="exceeds max_recipients"):
        agent.inboxes.messages.send(**SEND, cc=["b@x.com", "c@y.com"])
    assert mail.calls == []
    assert core.approval_requests == []


def test_constrain_unknown_type_fails_closed(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "holographic_watermark"})]
    with pytest.raises(AgentMailBlockedError, match="unknown constraint type"):
        agent.inboxes.messages.send(**SEND)
    assert core.approval_requests == []
    assert mail.calls == []


def test_core_string_constraints_are_understood(core, fake_mail):
    """Core sends bare strings, e.g. ["run_in_sandbox"] — not objects. They must
    be recognised and named, not reported as 'unrecognised constraint'."""
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, {"verdict": "constrain", "constraints": ["run_in_sandbox"]}]
    with pytest.raises(AgentMailBlockedError, match="no meaning for an email action"):
        agent.inboxes.messages.send(**SEND)
    assert core.approval_requests == []
    assert mail.calls == []


def test_string_form_of_a_real_constraint_still_applies(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, {"verdict": "constrain", "constraints": ["strip_attachments"]}, {}]
    agent.inboxes.messages.send(**SEND, attachments=[{"filename": "x.pdf"}])
    assert mail.calls[0][1]["attachments"] == []


def test_constrain_on_read_proceeds(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [{}, _constrain({"type": "allowed_domains", "domains": []}), {}]
    out = agent.inboxes.messages.get("inbox_1", "m_1")
    assert out is not None
    assert core.approval_requests == []  # reads never escalate


def test_apply_constraints_unit():
    args = {"to": ["a@x.com"], "attachments": [{"filename": "f"}]}
    out, applied = apply_constraints(args, [{"type": "strip_attachments"}])
    assert out["attachments"] == [] and applied
    with pytest.raises(ConstraintViolation):
        apply_constraints({"to": ["a@x.com", "b@y.com"]}, [{"type": "max_recipients", "count": 1}])
    with pytest.raises(ConstraintViolation):
        apply_constraints({"to": ["a@x.com"]}, [{"type": "wat"}])


# ── attachment screening + body-size cap ─────────────────────────────────────


def test_attachment_content_excluded_by_default(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    core.queue += [{}, {}, {}]
    agent.inboxes.messages.get_attachment("inbox_1", "m_1", "a_1")
    out = _completed(core, "agentmail.get_attachment")[0]["activity_output"]
    assert "content" not in out and "url" not in out


def test_attachment_content_scanned_when_enabled(core, fake_mail):
    agent, _ = make_agent(core, settings=AgentMailSettings(attachment_scan=True), mail=fake_mail)
    core.queue += [{}, {}, {}]
    agent.inboxes.messages.get_attachment("inbox_1", "m_1", "a_1")
    out = _completed(core, "agentmail.get_attachment")[0]["activity_output"]
    assert out["content"] == "QUJD"


def test_attachment_content_capped_by_privacy(core, fake_mail):
    runtime = make_runtime(core, privacy={"max_body_size": 2})
    from openbox_agentmail.client import OpenBoxMailAgent

    mail = fake_mail
    agent = OpenBoxMailAgent(mail, runtime, AgentMailSettings(attachment_scan=True))
    core.queue += [{}, {}, {}]
    agent.inboxes.messages.get_attachment("inbox_1", "m_1", "a_1")
    out = _completed(core, "agentmail.get_attachment")[0]["activity_output"]
    assert out["content"] == "QUJD"[:2] and out["truncated"] is True


# ── patch retry ──────────────────────────────────────────────────────────────


def test_block_patch_offers_patched_args_and_retry_reevaluates(core, fake_mail):
    agent, mail = make_agent(core, mail=fake_mail)
    core.queue += [
        {},
        {"verdict": "block", "reason": "PII",
         "patch": {"new_input": {"text": "[pii removed]"}}},
    ]
    send_args = {**SEND, "text": "call 555-1234"}
    with pytest.raises(AgentMailBlockedError) as exc:
        agent.inboxes.messages.send(**send_args)

    patched = exc.value.patched_args(send_args)
    assert patched is not None and patched["text"] == "[pii removed]"

    # retry is a fresh governed call, not a bypass
    core.queue += [{}, {}]
    agent.inboxes.messages.send(**patched)
    assert mail.calls[-1][1]["text"] == "[pii removed]"
    assert len(_started_events(core)) == 2


def test_block_without_patch_returns_none(core, fake_mail):
    agent, _ = make_agent(core, mail=fake_mail)
    core.queue += [{}, {"verdict": "block", "reason": "no"}]
    with pytest.raises(AgentMailBlockedError) as exc:
        agent.inboxes.messages.send(**SEND)
    assert exc.value.patched_args(dict(SEND)) is None


# ── metadata_only wire shape ─────────────────────────────────────────────────


def test_metadata_only_sends_hash_not_body(core, fake_mail):
    agent, _ = make_agent(core, settings=AgentMailSettings(content_mode="metadata_only"), mail=fake_mail)
    core.queue += [{}, {}, {}]
    agent.inboxes.messages.send(**SEND)
    ai = _started(core)[0]["activity_input"]
    assert ai["content_sha256"]
    assert "text" not in ai and "html" not in ai
    assert ai["subject"] == "hi"  # subject kept for routing context


def test_metadata_only_inbound(core, fake_mail):
    agent, _ = make_agent(core, settings=AgentMailSettings(content_mode="metadata_only"), mail=fake_mail)
    core.queue += [{}, {}]
    _, fields = agent.governor.screen_inbound(INBOUND_EVENT)
    assert fields["content_sha256"]
    assert "text" not in fields and "extracted_text" not in fields


# ── cookbook fixtures ────────────────────────────────────────────────────────


def test_policy_fixtures_match_builder_output():
    """Regenerate fixtures with build_activity_input so docs/policy-cookbook.rego
    examples and ``opa test`` fixtures never drift from the wire shape."""
    from openbox_agentmail.catalog import lookup
    from openbox_agentmail.contracts import build_activity_input

    settings = AgentMailSettings()
    fixture_dir = pathlib.Path(__file__).parent / "policy_fixtures"
    send = fixture_dir / "send_message_input.json"
    assert send.exists(), "policy fixture missing"

    spec = lookup(("messages", "send"))
    expected = build_activity_input(spec, dict(SEND), settings)
    captured = json.loads(send.read_text())
    assert captured == expected
