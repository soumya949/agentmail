"""Ordering for a session's governance events.

Core orders a session's events by the ``timestamp`` the SDK stamps on them,
and the dashboard renders that order literally. Wall-clock stamps break it two
ways: events built within the same millisecond tie (no defined order), and the
clock is not monotonic (NTP correction, sleep/wake) so a backwards step
reorders everything after it.

So each event gets a strictly increasing timestamp within its run, plus an
explicit ``sequence`` number that stays unambiguous even if two events are later
normalized to the same instant. Mirrors ``event-sequence.ts`` in the OpenBox
OpenRouter SDK so both SDKs present sessions the same way.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from openbox_core.contracts.events import rfc3339_from_datetime

_CLOSING = frozenset({"WorkflowCompleted", "WorkflowFailed"})


class EventSequencer:
    """Per-run ordering state. One instance per ``run_id``; see :func:`stamp`."""

    def __init__(self) -> None:
        self._last_ms = 0
        self._seq = 0

    def next(self) -> tuple[str, int]:
        # Strictly increasing: a tie or a backwards clock step advances by 1ms
        # rather than repeating or regressing.
        now = time.time_ns() // 1_000_000
        self._last_ms = now if now > self._last_ms else self._last_ms + 1
        self._seq += 1
        ts = datetime.fromtimestamp(self._last_ms / 1000, tz=UTC)
        return rfc3339_from_datetime(ts), self._seq


# Keyed by run_id so concurrent sessions keep independent sequences — a shared
# counter would interleave two runs' numbering and make each look full of holes.
_sequencers: dict[str, EventSequencer] = {}
_lock = threading.Lock()


def stamp(run_id: str | None, event_type: str) -> tuple[str | None, int | None]:
    """Return ``(timestamp, sequence)`` for the next event of ``run_id``.

    ``(None, None)`` when there is no run to order within; the sending layer
    then stamps the timestamp itself, as before.
    """
    if not run_id:
        return None, None
    with _lock:
        seq = _sequencers.get(run_id)
        if seq is None:
            seq = _sequencers[run_id] = EventSequencer()
        result = seq.next()
        # A closed session's state is dead weight in a long-lived process.
        if event_type in _CLOSING:
            _sequencers.pop(run_id, None)
    return result
