"""Inbound AgentMail webhook relay.

Pipeline (ARCHITECTURE.md §7):

    POST -> authenticate (custom delivery headers, constant-time)
         -> validate payload shape -> dedupe by event_id
         -> content events: receive_message activity (screen -> fetch the
            message with messages.get -> screen the fetched content)
            status events / fetch_on_receive=False: SignalReceived
         -> enforce verdict -> deliver governed event to the application handler

Response semantics: governance verdicts ACK 200 (a BLOCK is a decision, not a
transient failure — AgentMail must not retry). Auth failures 401, malformed
payloads 400, handler errors 500, Core-unreachable or message-fetch failure
503 (AgentMail retries and the delivery may succeed later). A 500/503 releases
the event_id from dedupe so that retry is processed, not dropped.
"""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from openbox_core.contracts.results import EvaluationResult
from openbox_core.errors import (
    ContractError,
    GovernanceAPIError,
    GuardrailsValidationError,
    OpenBoxConfigError,
)

from .errors import (
    AgentMailBlockedError,
    AgentMailHaltedError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    InboundFetchError,
)
from .governor import MailGovernor, ReceivedMessage

__all__ = [
    "WebhookAuth",
    "DedupeStore",
    "MemoryDedupe",
    "GovernedInbound",
    "RelayResponse",
    "InboundRelay",
    "CONTENT_EVENTS",
    "STATUS_EVENTS",
    "quarantine_on_blocked",
    "resolve_fetch_client",
    "release_event",
]

logger = logging.getLogger(__name__)

# Inbound content events: untrusted email entering the agent — always enforced.
CONTENT_EVENTS = frozenset(
    {
        "message.received",
        "message.received.spam",
        "message.received.blocked",
        "message.received.unauthenticated",
    }
)
# Everything else is delivery-status telemetry: recorded, never delivered to
# the app handler, never enforced.
STATUS_EVENTS = frozenset(
    {
        "message.sent",
        "message.delivered",
        "message.bounced",
        "message.complained",
        "message.rejected",
        "domain.verified",
    }
)

Handler = Callable[["GovernedInbound"], Any]


class WebhookAuth:
    """Authenticate deliveries via the custom headers configured on the
    AgentMail webhook (``headers`` field at create / ``update_headers``).

    AgentMail has no HMAC signature scheme — the shared-secret header IS the
    credential, so every required header is compared in constant time.

    Values may be a single string or a list of acceptable values — use a list
    to rotate secrets without downtime (accept old AND new during the
    ``update_headers`` propagation window, then drop the old value).
    """

    def __init__(
        self,
        required_headers: Mapping[str, str | list[str]] | None = None,
        *,
        secret: str | list[str] | None = None,
    ):
        if secret is not None:
            secrets = [secret] if isinstance(secret, str) else list(secret)
            required_headers = {"authorization": [f"Bearer {s}" for s in secrets]}
        self._required = {
            k.lower(): ([v] if isinstance(v, str) else list(v))
            for k, v in (required_headers or {}).items()
        }

    @property
    def configured(self) -> bool:
        return bool(self._required)

    def check(self, headers: Mapping[str, str]) -> bool:
        if not self._required:
            return False  # never accept unauthenticated deliveries by default
        lower = {k.lower(): v for k, v in headers.items()}
        for key, accepted in self._required.items():
            got = lower.get(key, "")
            if not any(hmac.compare_digest(got, a) for a in accepted):
                return False
        return True


class DedupeStore(Protocol):
    """``seen_or_add`` is required. An optional ``async forget(event_id)`` lets
    the relay release an id whose processing failed transiently, so the
    sender's retry is processed instead of being dropped as a duplicate."""

    async def seen_or_add(self, event_id: str) -> bool: ...


class MemoryDedupe:
    """In-process TTL dedupe keyed on ``event_id``. Production deployments with
    more than one replica should inject a shared store implementing
    ``DedupeStore`` (Redis/DB)."""

    def __init__(self, ttl_seconds: float = 86400.0):
        self._ttl = ttl_seconds
        self._seen: dict[str, float] = {}

    async def seen_or_add(self, event_id: str) -> bool:
        now = time.monotonic()
        for k, exp in list(self._seen.items()):
            if exp < now:
                del self._seen[k]
        if event_id in self._seen:
            return True
        self._seen[event_id] = now + self._ttl
        return False

    async def forget(self, event_id: str) -> None:
        self._seen.pop(event_id, None)


async def release_event(dedupe: Any, event_id: Any) -> None:
    """Undo ``seen_or_add`` after a transient failure (store permitting)."""
    forget = getattr(dedupe, "forget", None)
    if forget is None or not isinstance(event_id, str) or not event_id:
        return
    try:
        out = forget(event_id)
        if inspect.isawaitable(out):
            await out
    except Exception as e:  # noqa: BLE001
        logger.warning("dedupe forget failed for %s: %s", event_id, e)


@dataclass
class GovernedInbound:
    """What the application handler receives. ``event`` is the original webhook
    payload with any guardrail-redacted content merged back onto ``message``
    (with ``fetch_on_receive``, ``message`` is the copy fetched from AgentMail
    inside the ``receive_message`` activity); ``fields`` is the normalized dict
    OpenBox evaluated; ``activity_id`` links to that activity on the dashboard
    (``None`` on the SignalReceived path)."""

    event: dict[str, Any]
    fields: dict[str, Any]
    result: EvaluationResult
    activity_id: str | None = None

    @property
    def verdict(self) -> str:
        return self.result.verdict.value


@dataclass
class RelayResponse:
    status: int
    body: dict[str, Any] = field(default_factory=dict)

    def json(self) -> bytes:
        return json.dumps(self.body).encode()


_ENFORCED_STATUSES = {200: "delivered"}


class InboundRelay:
    """Framework-agnostic relay. ``handle``/``ahandle`` take raw headers+body;
    ``asgi()`` returns a dependency-free ASGI app mountable anywhere.

    ``background=True`` enables ack-then-process (ARCHITECTURE §4.4): once a
    delivery is authenticated, validated and deduplicated the relay ACKs 200
    immediately and the event is screened by a bounded pool of workers on the
    running event loop. Transient Core outages (the inline 503 case) are
    retried internally with backoff and then dead-lettered — AgentMail never
    sees them, so the app stays responsive and inbound ``REQUIRE_APPROVAL``
    decisions wait without holding the HTTP request open.

    ``fetch_on_receive=True`` (default) governs each content event as an
    ``agentmail.receive_message`` activity and fetches the message with
    ``messages.get`` inside it — the handler gets the fetched, screened copy
    and the dashboard shows the GET as child spans. ``agentmail_client`` is
    the RAW client used for the fetch (default: the governor's own client).
    ``fetch_on_receive=False`` keeps the SignalReceived-only path.

    ``arrival_signal=True`` also emits a telemetry-only ``SignalReceived`` when
    mail arrives, ahead of the activity. ``session_per_message=True`` makes each
    email its own workflow — note behavioural rules only match prior activity
    within a session.
    """

    _STOP = object()

    def __init__(
        self,
        governor: MailGovernor,
        *,
        auth: WebhookAuth,
        handler: Handler | None = None,
        dedupe: DedupeStore | None = None,
        on_blocked: Callable[[dict[str, Any], Exception], Any] | None = None,
        background: bool = False,
        concurrency: int = 4,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        on_dead_letter: Callable[[dict[str, Any]], Any] | None = None,
        agentmail_client: Any = None,
        fetch_on_receive: bool = True,
        arrival_signal: bool = False,
        session_per_message: bool = False,
    ):
        self.governor = governor
        self.fetch_on_receive = fetch_on_receive
        self.arrival_signal = arrival_signal
        self.session_per_message = session_per_message
        self._fetch_client = resolve_fetch_client(governor, agentmail_client, fetch_on_receive)
        self.auth = auth
        self.handler = handler
        self.dedupe = dedupe or MemoryDedupe()
        # e.g. apply an AgentMail "quarantine" label to the blocked message
        self.on_blocked = on_blocked
        self.background = background
        self.concurrency = max(1, concurrency)
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.on_dead_letter = on_dead_letter
        self._queue: Any = None
        self._workers: list[Any] = []
        self._loop: Any = None

    async def _blocked(self, event: dict[str, Any], e: Exception, reason: str) -> RelayResponse:
        if self.on_blocked is not None:
            try:
                out = self.on_blocked(event, e)
                if inspect.isawaitable(out):
                    await out
            except Exception as cb_err:  # noqa: BLE001 — the block itself already stands
                logger.warning("on_blocked callback failed: %s", cb_err)
        return RelayResponse(200, {"status": "blocked", "reason": reason})

    # ── background queue ─────────────────────────────────────────────────

    def _ensure_workers(self) -> None:
        import asyncio

        loop = asyncio.get_running_loop()
        if self._workers and self._loop is loop:
            return
        self._loop = loop
        self._queue = asyncio.Queue()
        self._workers = [loop.create_task(self._worker()) for _ in range(self.concurrency)]

    async def _worker(self) -> None:
        import asyncio

        while True:
            event = await self._queue.get()
            try:
                if event is self._STOP:
                    return
                resp: RelayResponse | None = None
                for attempt in range(self.max_retries + 1):
                    resp = await self._process_event(event)
                    if resp.status != 503:
                        break
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_backoff * (2**attempt))
                if resp is not None and resp.status == 503:
                    logger.error("inbound event dead-lettered after retries: %s", event.get("event_id"))
                    if self.on_dead_letter is not None:
                        try:
                            out = self.on_dead_letter(event)
                            if inspect.isawaitable(out):
                                await out
                        except Exception as e:  # noqa: BLE001
                            logger.warning("on_dead_letter callback failed: %s", e)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait until every queued event has been processed (tests/shutdown)."""
        if self._queue is not None:
            await self._queue.join()

    async def aclose(self) -> None:
        """Drain the queue, then stop workers. Call on app shutdown."""
        if self._queue is None:
            return
        await self._queue.join()
        for _ in self._workers:
            self._queue.put_nowait(self._STOP)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        self._queue = None

    # ── pipeline ─────────────────────────────────────────────────────────

    def _validate(self, headers: Mapping[str, str], body: bytes) -> dict[str, Any] | RelayResponse:
        if not self.auth.check(headers):
            return RelayResponse(401, {"error": "unauthorized"})
        try:
            event = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return RelayResponse(400, {"error": "invalid JSON"})
        if not isinstance(event, dict) or not isinstance(event.get("event_type"), str):
            return RelayResponse(400, {"error": "missing event_type"})
        et = event["event_type"]
        if et in CONTENT_EVENTS or et in STATUS_EVENTS:
            # Only content events carry ``message``; status events put their
            # details under send/delivery/bounce/complaint/reject/open.
            if et in CONTENT_EVENTS and not isinstance(event.get("message"), dict):
                return RelayResponse(400, {"error": f"{et} requires a message object"})
            return event
        return RelayResponse(400, {"error": f"unknown event_type {et!r}"})

    async def ahandle(self, headers: Mapping[str, str], body: bytes) -> RelayResponse:
        validated = self._validate(headers, body)
        if isinstance(validated, RelayResponse):
            return validated
        event = validated

        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id:
            if await self.dedupe.seen_or_add(event_id):
                return RelayResponse(200, {"status": "duplicate"})

        if self.background:
            self._ensure_workers()
            await self._queue.put(event)
            return RelayResponse(200, {"status": "accepted"})

        resp = await self._process_event(event)
        if resp.status >= 500:
            # AgentMail will redeliver; let that retry through dedupe.
            await release_event(self.dedupe, event_id)
        return resp

    async def _screen(self, event: dict[str, Any], enforce: bool) -> tuple[Any, dict[str, Any], dict[str, Any], str | None]:
        """Returns ``(result, fields, governed_event, activity_id)``."""
        if enforce and self.arrival_signal:
            # Telemetry-only: records that mail ARRIVED, ahead of the activity
            # that reads it. Enforcement stays on the activity.
            await self.governor.ascreen_inbound(event, enforce=False)
        if enforce and self.fetch_on_receive:
            received: ReceivedMessage = await self.governor.areceive_inbound(
                event, agentmail_client=self._fetch_client, transport="webhook"
            )
            return received.result, received.fields, {**event, "message": received.message}, received.activity_id
        result, fields = await self.governor.ascreen_inbound(event, enforce=enforce)
        return result, fields, _merge_redaction(event, fields), None

    async def _process_event(self, event: dict[str, Any]) -> RelayResponse:
        if self.session_per_message:
            try:
                return await self._process_one(event)
            finally:
                await self.governor.aclose_session()
        return await self._process_one(event)

    async def _process_one(self, event: dict[str, Any]) -> RelayResponse:
        event_type = event["event_type"]
        event_id = event.get("event_id")
        enforce = event_type in CONTENT_EVENTS
        try:
            result, fields, governed, activity_id = await self._screen(event, enforce)
        except (AgentMailBlockedError, AgentMailHaltedError, GuardrailsValidationError) as e:
            return await self._blocked(event, e, str(e))
        except (ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError) as e:
            return await self._blocked(event, e, f"approval: {e}")
        except ContractError as e:
            # Malformed for governance (no message_id) or outside this agent's
            # inbox scope — a decision, not transient: never delivered.
            return await self._blocked(event, e, f"contract: {e}")
        except GovernanceAPIError as e:
            # Screen could not run — tell AgentMail to retry rather than
            # dropping unscreened mail on the floor.
            return RelayResponse(503, {"error": f"governance unavailable: {e}"})
        except InboundFetchError as e:
            return RelayResponse(503, {"error": f"message fetch failed: {e}"})

        if not enforce:
            return RelayResponse(200, {"status": "recorded"})

        inbound = GovernedInbound(event=governed, fields=fields, result=result, activity_id=activity_id)
        if self.handler is not None:
            try:
                out = self.handler(inbound)
                if inspect.isawaitable(out):
                    await out
            except Exception as e:  # noqa: BLE001 — app failure, safe to retry
                logger.warning("inbound handler failed for %s: %s", event_id, e)
                return RelayResponse(500, {"error": "handler failed"})
        return RelayResponse(200, {"status": "delivered", "verdict": result.verdict.value})

    def handle(self, headers: Mapping[str, str], body: bytes) -> RelayResponse:
        """Sync pipeline for WSGI/Flask-style integrations."""
        import asyncio

        return asyncio.run(self.ahandle(headers, body))

    # ── ASGI ─────────────────────────────────────────────────────────────

    def asgi(self):
        """Bare ASGI app: POST only, reads the whole body, runs the pipeline."""
        relay = self

        async def app(scope, receive, send):
            if scope["type"] != "http" or scope.get("method") != "POST":
                await _respond(send, 405, b'{"error": "method not allowed"}')
                return
            chunks = []
            more = True
            while more:
                msg = await receive()
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
            resp = await relay.ahandle(headers, b"".join(chunks))
            await _respond(send, resp.status, resp.json())

        return app

    # ── framework adapters ───────────────────────────────────────────────

    def flask_blueprint(self, name: str = "openbox_agentmail_relay", path: str = "/agentmail-webhook"):
        """``flask.Blueprint`` exposing ``POST {path}`` through the pipeline."""
        from flask import Blueprint, request

        relay = self
        bp = Blueprint(name, __name__)

        @bp.post(path)
        def _receive():
            resp = relay.handle(dict(request.headers), request.get_data())
            return resp.body, resp.status

        return bp

    def fastapi_router(self, path: str = "/agentmail-webhook"):
        """``fastapi.APIRouter`` exposing ``POST {path}`` through the pipeline."""
        from fastapi import APIRouter, Request, Response

        relay = self
        router = APIRouter()

        @router.post(path)
        async def _receive(request: Request):
            body = await request.body()
            resp = await relay.ahandle(dict(request.headers), body)
            return Response(content=resp.json(), status_code=resp.status, media_type="application/json")

        return router


async def _respond(send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def quarantine_on_blocked(agent: Any, label: str = "quarantine") -> Callable[[dict[str, Any], Exception], Any]:
    """Build an ``on_blocked`` callback that applies a label to the blocked
    message via the governed client (``messages.update(add_labels=[label])``),
    so blocked mail is visible in the AgentMail console too. Works with sync or
    async governed agents."""

    def _cb(event: dict[str, Any], _err: Exception) -> Any:
        message = event.get("message") or {}
        inbox_id, message_id = message.get("inbox_id"), message.get("message_id")
        if inbox_id and message_id:
            return agent.inboxes.messages.update(inbox_id, message_id, add_labels=[label])
        return None

    return _cb


def resolve_fetch_client(governor: MailGovernor, client: Any, fetch_on_receive: bool) -> Any:
    """The RAW AgentMail client used for the in-activity fetch. A governed
    agent is unwrapped (its fetch would otherwise become a nested
    ``get_message`` activity). Fails at construction, not on the first email."""
    if not fetch_on_receive:
        return client
    if client is not None and hasattr(client, "governor") and getattr(client, "raw", None) is not None:
        client = client.raw
    if client is None and getattr(governor, "_mail_client", None) is None:
        raise OpenBoxConfigError(
            "fetch_on_receive=True needs an AgentMail client: pass agentmail_client= "
            "or build the governor with one (fetch_on_receive=False to disable)"
        )
    return client


def _merge_redaction(event: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    """Overlay guardrail-redacted fields back onto ``event['message']`` so the
    handler sees the governed content, not the raw email."""
    message = event.get("message")
    if not isinstance(message, dict):
        return event
    out = dict(event)
    merged = dict(message)
    for k, v in fields.items():
        if k in merged:
            merged[k] = v
    out["message"] = merged
    return out
