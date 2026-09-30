"""Governed AgentMail WebSocket inbound channel (P5).

``websockets.connect()`` streams the same event objects the webhook
endpoint delivers. Each message is deduplicated and governed exactly like
the webhook relay: content events become an ``agentmail.receive_message``
activity (screen -> fetch with ``messages.get`` -> screen the fetched copy;
``fetch_on_receive=False`` keeps the ``SignalReceived`` path), status events
are ``SignalReceived`` telemetry. Only allowed content reaches the handler.

A socket cannot ask AgentMail to redeliver, so transient failures (Core
unreachable, fetch failed) are retried locally ``screen_retries`` times and
then handed to ``on_dead_letter`` — never delivered unscreened. The email
itself stays in the AgentMail inbox either way.

The connection is sync (``websockets.sync`` in the AgentMail SDK) — run
``listen()`` on a thread; ``stop()`` terminates the loop and closes the
socket. Reconnects use exponential backoff up to ``max_retries``; dedupe
survives reconnects, so redelivered events are dropped once.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from openbox_core.errors import ContractError, GovernanceAPIError, GuardrailsValidationError

from .errors import (
    AgentMailBlockedError,
    AgentMailHaltedError,
    ApprovalExpiredError,
    ApprovalRejectedError,
    ApprovalTimeoutError,
    InboundFetchError,
)
from .governor import MailGovernor
from .webhook_relay import (
    CONTENT_EVENTS,
    DedupeStore,
    GovernedInbound,
    MemoryDedupe,
    _merge_redaction,
    resolve_fetch_client,
)

__all__ = ["WebsocketInbound"]

logger = logging.getLogger(__name__)


def _to_dict(raw: Any) -> dict[str, Any] | None:
    """Normalize a socket message (pydantic model / mapping / JSON str) to dict."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if hasattr(raw, "model_dump"):
        try:
            return raw.model_dump(mode="json", by_alias=True)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(raw, str):
        import json

        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Socket frames wrap events (``{"type": ..., "event": {...}}``) or carry
    the event directly."""
    if isinstance(payload.get("event"), Mapping):
        return dict(payload["event"])
    if "event_type" in payload:
        return payload
    return None


class WebsocketInbound:
    """Args:
        governor: the shared ``MailGovernor`` (its session owns the Signals).
        agentmail_client: an ``agentmail.AgentMail`` — ``websockets.connect``
            is taken from it unless ``connect`` overrides (tests).
        handler: receives ``GovernedInbound`` for allowed content events.
        subscribe: filter dict ``{"event_types": [...], "inbox_ids": [...],
            "pod_ids": [...]}`` sent via ``send_subscribe`` on each connect.
        dedupe / on_blocked: same semantics as ``InboundRelay``.
        arrival_signal: also emit a telemetry-only ``SignalReceived`` when mail
            arrives, before the activity that reads it. Presentational for
            inbound - the activity already carries the same arrival fields.
        session_per_message: make each email its own workflow
            (``WorkflowCompleted`` after each) instead of one long session.
            Note behavioural rules only see prior activity *within* a session.
        fetch_on_receive: govern content events as ``receive_message``
            activities with an in-activity ``messages.get`` (default True).
            The fetch uses ``agentmail_client`` (raw), else the governor's.
        screen_retries / retry_backoff: local retries for transient failures.
        on_dead_letter: ``(event, error)`` once retries are exhausted.
    """

    def __init__(
        self,
        governor: MailGovernor,
        agentmail_client: Any = None,
        *,
        connect: Callable[..., Any] | None = None,
        handler: Callable[[GovernedInbound], Any] | None = None,
        on_blocked: Callable[[dict[str, Any], Exception], Any] | None = None,
        dedupe: DedupeStore | None = None,
        subscribe: Mapping[str, Any] | None = None,
        max_retries: int = 5,
        backoff: float = 1.0,
        fetch_on_receive: bool = True,
        arrival_signal: bool = False,
        session_per_message: bool = False,
        screen_retries: int = 3,
        retry_backoff: float = 0.5,
        on_dead_letter: Callable[[dict[str, Any], Exception], Any] | None = None,
    ):
        self.governor = governor
        self._client = agentmail_client
        self._connect = connect
        self.handler = handler
        self.on_blocked = on_blocked
        self.dedupe = dedupe or MemoryDedupe()
        self.subscribe = dict(subscribe or {})
        self.max_retries = max_retries
        self.backoff = backoff
        self.fetch_on_receive = fetch_on_receive
        self.arrival_signal = arrival_signal
        self.session_per_message = session_per_message
        self._fetch_client = resolve_fetch_client(governor, agentmail_client, fetch_on_receive)
        self.screen_retries = max(0, screen_retries)
        self.retry_backoff = retry_backoff
        self.on_dead_letter = on_dead_letter
        self._stopped = False
        self._ws: Any = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def stop(self) -> None:
        self._stopped = True
        ws = self._ws
        if ws is not None:
            for target in (ws, getattr(ws, "_websocket", None)):
                if target is not None and hasattr(target, "close"):
                    try:
                        target.close()
                    except Exception:  # noqa: BLE001 — best effort shutdown
                        pass

    def _open(self) -> Any:
        """Return the ``websockets.connect()`` context manager."""
        if self._connect is not None:
            return self._connect()
        if self._client is None:
            raise GovernanceAPIError("WebsocketInbound needs an agentmail client or connect= factory")
        return self._client.websockets.connect()

    def _subscribe(self, ws: Any) -> None:
        if not self.subscribe or not hasattr(ws, "send_subscribe"):
            return
        message: Any = self.subscribe
        try:
            from agentmail.websockets.types.subscribe import Subscribe

            message = Subscribe(type="subscribe", **self.subscribe)
        except Exception:  # noqa: BLE001 — fake sockets in tests accept dicts
            pass
        ws.send_subscribe(message)

    # ── main loop ────────────────────────────────────────────────────────

    def listen(self) -> None:
        """Connect → subscribe → dispatch loop with reconnect/backoff.
        Blocks the calling thread; intended for ``threading.Thread``."""
        attempts = 0
        while not self._stopped:
            try:
                try:
                    ws_ctx = self._open()
                except (StopIteration, IndexError):
                    # Injected connect factories raise these when out of
                    # sockets — treat as "no more connections", not a
                    # retryable transport error.
                    return
                with ws_ctx as ws:
                    self._ws = ws
                    self._subscribe(ws)
                    attempts = 0
                    for raw in ws:
                        if self._stopped:
                            break
                        self._dispatch(raw)
            except Exception as e:  # noqa: BLE001 — reconnect on transport errors
                if self._stopped:
                    break
                attempts += 1
                if attempts > self.max_retries:
                    logger.error("websocket inbound gave up after %d reconnects: %s", attempts, e)
                    break
                delay = self.backoff * (2 ** (attempts - 1))
                logger.warning("websocket inbound reconnecting in %.1fs: %s", delay, e)
                time.sleep(delay)
            finally:
                self._ws = None

    # ── per-message pipeline ─────────────────────────────────────────────

    def _seen(self, event_id: str) -> bool:
        return asyncio.run(self.dedupe.seen_or_add(event_id))

    def _screen_once(self, event: dict[str, Any], enforce: bool) -> GovernedInbound:
        if enforce and self.arrival_signal:
            # Telemetry-only SignalReceived recording that mail ARRIVED, before
            # the activity that fetches and reads it. Enforcement stays on the
            # activity - one place to write policy, one place it is applied.
            self.governor.screen_inbound(event, enforce=False)
        if enforce and self.fetch_on_receive:
            received = self.governor.receive_inbound(
                event, agentmail_client=self._fetch_client, transport="websocket"
            )
            return GovernedInbound(event={**event, "message": received.message}, fields=received.fields,
                                   result=received.result, activity_id=received.activity_id)
        result, fields = self.governor.screen_inbound(event, enforce=enforce)
        return GovernedInbound(event=_merge_redaction(event, fields), fields=fields, result=result)

    def _screen_with_retry(self, event: dict[str, Any], enforce: bool) -> GovernedInbound:
        attempt = 0
        while True:
            try:
                return self._screen_once(event, enforce)
            except (GovernanceAPIError, InboundFetchError) as e:
                if attempt >= self.screen_retries or self._stopped:
                    raise
                delay = self.retry_backoff * (2**attempt)
                attempt += 1
                logger.warning("websocket event %s: transient failure, retry %d/%d in %.1fs: %s",
                               event.get("event_id"), attempt, self.screen_retries, delay, e)
                time.sleep(delay)

    def _dispatch(self, raw: Any) -> None:
        payload = _to_dict(raw)
        if payload is None:
            logger.warning("skipping unparseable websocket frame")
            return
        event = _extract_event(payload)
        if event is None:
            return  # control frames (subscribed acks, pings)

        event_id = event.get("event_id")
        if isinstance(event_id, str) and event_id and self._seen(event_id):
            logger.info("duplicate websocket event %s dropped", event_id)
            return

        enforce = event.get("event_type") in CONTENT_EVENTS
        if self.session_per_message:
            try:
                self._deliver(event, enforce, event_id)
            finally:
                # One workflow per email: WorkflowCompleted closes here and the
                # next message opens a fresh session.
                self.governor.close_session()
            return
        self._deliver(event, enforce, event_id)

    def _deliver(self, event: dict[str, Any], enforce: bool, event_id: Any) -> None:
        try:
            inbound = self._screen_with_retry(event, enforce)
        except (AgentMailBlockedError, AgentMailHaltedError, GuardrailsValidationError,
                ApprovalRejectedError, ApprovalExpiredError, ApprovalTimeoutError,
                ContractError) as e:
            if self.on_blocked is not None:
                try:
                    self.on_blocked(event, e)
                except Exception as cb:  # noqa: BLE001
                    logger.warning("on_blocked callback failed: %s", cb)
            return
        except (GovernanceAPIError, InboundFetchError) as e:
            logger.error("websocket event %s dead-lettered after %d retries: %s",
                         event_id, self.screen_retries, e)
            if self.on_dead_letter is not None:
                try:
                    self.on_dead_letter(event, e)
                except Exception as cb:  # noqa: BLE001
                    logger.warning("on_dead_letter callback failed: %s", cb)
            return

        if not enforce or self.handler is None:
            return
        try:
            out = self.handler(inbound)
            if inspect.isawaitable(out):
                asyncio.run(out)
        except Exception as e:  # noqa: BLE001 — handler bugs must not kill the loop
            logger.warning("inbound handler failed for %s: %s", event_id, e)
