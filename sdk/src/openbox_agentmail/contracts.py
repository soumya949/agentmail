"""Builders for ``activity_input`` / ``activity_output`` / inbound signal fields.

These are the shapes policy authors write Rego against (see docs/policy-cookbook.md).
Local validation raises ``ContractError`` before any network call.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from openbox_core.errors import ContractError
from openbox_core.serialization import to_json_safe

from .catalog import ActionClass, ActionSpec
from .config import AgentMailSettings

__all__ = ["build_activity_input", "build_activity_output", "build_inbound_signal",
           "build_inbound_activity_input", "build_trigger_signal", "domain_of"]

_RECIPIENT_FIELDS = ("to", "cc", "bcc")
_CONTENT_FIELDS = ("subject", "text", "html")


def domain_of(address: str) -> str:
    """``"Alice <alice@Example.com>"`` -> ``"example.com"``."""
    addr = address.strip()
    if "<" in addr and addr.endswith(">"):
        addr = addr[addr.rfind("<") + 1 : -1]
    return addr.rpartition("@")[2].lower()


def _as_list(value: Any) -> list[str]:
    if value is None or value is Ellipsis:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def _attachment_meta(att: Any, include_bytes: bool, max_chars: int | None = None) -> dict[str, Any]:
    get = (lambda k: att.get(k)) if isinstance(att, Mapping) else (lambda k: getattr(att, k, None))
    content = get("content")
    size = None
    if isinstance(content, str):
        try:
            size = len(base64.b64decode(content, validate=False))
        except Exception:
            size = len(content)
    elif isinstance(content, (bytes, bytearray)):
        size = len(content)
    meta = {
        "filename": get("filename"),
        "content_type": get("content_type"),
        "size": size if size is not None else get("size"),
        "attachment_id": get("attachment_id"),
    }
    if include_bytes and content is not None:
        encoded = content if isinstance(content, str) else base64.b64encode(content).decode()
        if max_chars is not None and len(encoded) > max_chars:
            encoded = encoded[:max_chars]
            meta["truncated"] = True
        meta["content"] = encoded
    return meta


def _content_hash(args: Mapping[str, Any]) -> str:
    h = hashlib.sha256()
    for f in _CONTENT_FIELDS:
        v = args.get(f)
        if isinstance(v, str):
            h.update(f.encode())
            h.update(v.encode())
    return h.hexdigest()


def _validate(spec: ActionSpec, args: Mapping[str, Any]) -> None:
    missing: list[str] = []
    if spec.resource in ("messages", "drafts", "threads") and not args.get("inbox_id"):
        missing.append("inbox_id")
    if spec.action == "send_message" and not any(_as_list(args.get(f)) for f in _RECIPIENT_FIELDS):
        missing.append("to|cc|bcc")
    if spec.method in ("reply", "reply_all", "forward") and not args.get("message_id"):
        missing.append("message_id")
    if spec.action == "send_draft" and not args.get("draft_id"):
        missing.append("draft_id")
    if missing:
        raise ContractError(
            f"{spec.activity_type}: missing required fields {missing}",
            code="AGENTMAIL_INPUT_MISSING_FIELDS",
            detail={"missing": missing, "activity_type": spec.activity_type},
        )


def build_activity_input(
    spec: ActionSpec, args: Mapping[str, Any], settings: AgentMailSettings,
    max_body_size: int | None = None,
) -> dict[str, Any]:
    """Flat, JSON-safe ``activity_input`` for an outbound AgentMail operation."""
    _validate(spec, args)
    clean = {k: v for k, v in args.items() if v is not Ellipsis and k != "request_options"}
    inbox_id = clean.get("inbox_id")

    if settings.inbox_ids is not None and inbox_id and inbox_id not in settings.inbox_ids:
        raise ContractError(
            f"inbox {inbox_id!r} is outside this agent's configured inbox_ids",
            code="AGENTMAIL_INBOX_OUT_OF_SCOPE",
            detail={"inbox_id": inbox_id},
        )

    payload: dict[str, Any] = {
        "action": spec.action,
        "action_class": spec.action_class.value,
        "surface": settings.surface,
        "inbox_id": inbox_id,
        "pod_id": clean.get("pod_id") or settings.pod_id,
    }

    if spec.action_class in (ActionClass.SEND, ActionClass.DRAFT):
        recipients: dict[str, list[str]] = {f: _as_list(clean.get(f)) for f in _RECIPIENT_FIELDS}
        all_rcpts = [r for lst in recipients.values() for r in lst]
        domains = sorted({domain_of(r) for r in all_rcpts if "@" in r})
        internal = {d.lower() for d in settings.internal_domains}
        if inbox_id and "@" in str(inbox_id):
            internal.add(domain_of(str(inbox_id)))
        payload.update(recipients)
        payload.update(
            {
                "reply_to": _as_list(clean.get("reply_to")),
                "recipient_domains": domains,
                "recipient_count": len(all_rcpts),
                "external_recipients": any(d not in internal for d in domains),
                "content_sha256": _content_hash(clean),
                "html_present": bool(clean.get("html")),
                "attachments": [
                    _attachment_meta(a, settings.attachment_scan, max_body_size)
                    for a in (clean.get("attachments") or [])
                ],
                "labels": _as_list(clean.get("labels")),
                "send_at": clean.get("send_at"),
                "message_id": clean.get("message_id"),
                "draft_id": clean.get("draft_id"),
                "thread_id": clean.get("thread_id"),
            }
        )
        if settings.content_mode == "full":
            for f in _CONTENT_FIELDS:
                payload[f] = clean.get(f)
        else:
            payload["subject"] = clean.get("subject")
    else:
        # Reads / modify / admin: pass the (non-secret) call arguments through.
        for k, v in clean.items():
            if k in ("inbox_id", "pod_id"):
                continue
            payload[k] = v

    return to_json_safe(payload, exclude_none=False)


def build_activity_output(spec: ActionSpec, result: Any, settings: AgentMailSettings,
                          max_body_size: int | None = None) -> dict[str, Any]:
    """``activity_output`` for ``ActivityCompleted``. Always an object."""
    data = _dump(result)
    if spec.action_class is ActionClass.READ_ATTACHMENT and isinstance(data, dict):
        if not settings.attachment_scan:
            data = {k: v for k, v in data.items() if k not in ("content", "url")}
        elif max_body_size is not None and isinstance(data.get("content"), str) and len(data["content"]) > max_body_size:
            data = dict(data)
            data["content"] = data["content"][:max_body_size]
            data["truncated"] = True
    # Read responses: truncate oversized text fields so privacy.max_body_size
    # bounds what leaves the agent boundary.
    if isinstance(data, dict) and max_body_size is not None:
        for f in ("text", "html", "extracted_text"):
            v = data.get(f)
            if isinstance(v, str) and len(v) > max_body_size:
                data[f] = v[:max_body_size]
                data["truncated"] = True
    if not isinstance(data, dict):
        data = {"result": data}
    data.setdefault("action", spec.action)
    return to_json_safe(data, exclude_none=False)


def build_inbound_signal(event: Mapping[str, Any], settings: AgentMailSettings,
                         max_body_size: int | None = None) -> dict[str, Any]:
    """Flat fields for ``SignalReceived`` built from an AgentMail webhook/websocket payload."""
    if not isinstance(event.get("message"), Mapping):
        status = _status_fields(event)
        if status is not None:
            return to_json_safe(status, exclude_none=False)
    message = event.get("message") or {}
    sender = message.get("from") or message.get("from_") or ""
    fields: dict[str, Any] = {
        "agentmail_event_type": event.get("event_type"),
        "agentmail_event_id": event.get("event_id"),
        "inbox_id": message.get("inbox_id") or event.get("inbox_id"),
        "thread_id": message.get("thread_id"),
        "message_id": message.get("message_id"),
        "sender": sender,
        "sender_domain": domain_of(sender) if sender else None,
        "reply_to": _as_list(message.get("reply_to")),
        "to": _as_list(message.get("to")),
        "cc": _as_list(message.get("cc")),
        "subject": message.get("subject"),
        "labels": _as_list(message.get("labels")),
        "attachments": [_attachment_meta(a, False) for a in (message.get("attachments") or [])],
        "headers": _auth_headers(message.get("headers") or {}),
        "size": message.get("size"),
    }
    if settings.content_mode == "full":
        for f in ("text", "extracted_text"):
            v = message.get(f)
            if isinstance(v, str) and max_body_size is not None and len(v) > max_body_size:
                v = v[:max_body_size]
                fields["truncated"] = True
            fields[f] = v
        fields["html_present"] = bool(message.get("html"))
    else:
        fields["content_sha256"] = _content_hash(message)
    return to_json_safe(fields, exclude_none=False)


def build_trigger_signal(name: str, data: Mapping[str, Any] | None, source: str | None,
                         settings: AgentMailSettings,
                         max_body_size: int | None = None) -> dict[str, Any]:
    """Flat fields for a caller-declared trigger ``SignalReceived``.

    The trigger is whatever caused the agent to act — an API request, a bot, a
    schedule, a user instruction. The SDK cannot observe it, so the caller
    declares it and we record it verbatim. ``trigger_data`` is namespaced so a
    caller's keys can never overwrite the fields policy relies on.
    """
    if not isinstance(name, str) or not name.strip():
        raise ContractError(
            "emit_trigger: 'name' must be a non-empty string",
            code="AGENTMAIL_TRIGGER_NAME_INVALID",
        )
    if data is not None and not isinstance(data, Mapping):
        raise ContractError(
            f"emit_trigger: 'data' must be a mapping, got {type(data).__name__}",
            code="AGENTMAIL_TRIGGER_DATA_INVALID",
        )
    fields: dict[str, Any] = {
        "trigger": name.strip(),
        "trigger_source": source,
        "direction": "inbound",
        "surface": settings.surface,
    }
    capped: dict[str, Any] = {}
    for k, v in (data or {}).items():
        if isinstance(v, str) and max_body_size is not None and len(v) > max_body_size:
            v = v[:max_body_size]
            fields["truncated"] = True
        capped[str(k)] = v
    fields["trigger_data"] = capped
    return to_json_safe(fields, exclude_none=False)


def build_inbound_activity_input(event: Mapping[str, Any], settings: AgentMailSettings,
                                 max_body_size: int | None = None,
                                 transport: str = "webhook") -> dict[str, Any]:
    """``activity_input`` for ``agentmail.receive_message``: the same fields as
    the inbound signal (sender, auth headers, subject, body…) plus the action
    keys every activity carries, so one Rego rule shape covers both."""
    message = event.get("message")
    if not isinstance(message, Mapping):
        message = {}
    inbox_id = message.get("inbox_id") or event.get("inbox_id")
    missing = [k for k, v in (("inbox_id", inbox_id), ("message_id", message.get("message_id"))) if not v]
    if missing:
        raise ContractError(
            f"agentmail.receive_message: inbound event missing {missing}",
            code="AGENTMAIL_INBOUND_MISSING_FIELDS",
            detail={"missing": missing, "event_id": event.get("event_id")},
        )
    if settings.inbox_ids is not None and inbox_id not in settings.inbox_ids:
        raise ContractError(
            f"inbound mail for inbox {inbox_id!r} is outside this agent's configured inbox_ids",
            code="AGENTMAIL_INBOX_OUT_OF_SCOPE",
            detail={"inbox_id": inbox_id},
        )
    fields = build_inbound_signal(event, settings, max_body_size)
    return {
        "action": "receive_message",
        "action_class": ActionClass.READ.value,
        "direction": "inbound",
        "surface": settings.surface,
        "transport": transport,
        "pod_id": message.get("pod_id") or settings.pod_id,
        **fields,
    }


# Delivery-status events carry their details under one of these keys
# (agentmail.events.types.Message{Sent,Delivered,Bounced,...}Event).
_STATUS_KEYS = ("send", "delivery", "bounce", "complaint", "reject", "open")


def _status_fields(event: Mapping[str, Any]) -> dict[str, Any] | None:
    for key in _STATUS_KEYS:
        body = event.get(key)
        if isinstance(body, Mapping):
            fields: dict[str, Any] = {
                "agentmail_event_type": event.get("event_type"),
                "agentmail_event_id": event.get("event_id"),
                "inbox_id": body.get("inbox_id"),
                "thread_id": body.get("thread_id"),
                "message_id": body.get("message_id"),
                "timestamp": body.get("timestamp"),
                "recipients": body.get("recipients") or [],
            }
            if key == "bounce":
                fields["bounce_type"] = body.get("type")
                fields["bounce_sub_type"] = body.get("sub_type")
            return fields
    return None


_AUTH_HEADER_KEYS = ("authentication-results", "received-spf", "dkim-signature", "arc-authentication-results")


def _auth_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in headers.items() if str(k).lower() in _AUTH_HEADER_KEYS}


def _dump(obj: Any) -> Any:
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    if isinstance(obj, (bytes, bytearray)):
        return {"bytes": len(obj)}
    if isinstance(obj, Iterable) and not isinstance(obj, (str, Mapping)):
        return {"items": [_dump(o) for o in obj]}
    return to_json_safe(obj)
