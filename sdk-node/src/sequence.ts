/**
 * Ordering for a session's governance events.
 *
 * Core orders a session's events by the `timestamp` the SDK stamps on them,
 * and the dashboard renders that order literally. Events built within the same
 * millisecond tie (no defined order), and `Date.now()` is not monotonic (NTP
 * correction, sleep/wake), so a backwards step reorders everything after it.
 *
 * So each event gets a strictly increasing timestamp within its run, plus an
 * explicit `sequence` number that stays unambiguous even if two events are
 * later normalized to the same instant. Mirrors `event-sequence.ts` in the
 * OpenBox OpenRouter SDK and `sequence.py` in the Python SDK.
 */

class EventSequencer {
  /** Last timestamp handed out, in ms. Never decreases. */
  private lastMs = 0;
  private seq = 0;

  next(): { timestamp: string; sequence: number } {
    // Strictly increasing: a tie or a backwards clock step advances by 1ms
    // rather than repeating or regressing.
    const now = Date.now();
    this.lastMs = now > this.lastMs ? now : this.lastMs + 1;
    this.seq += 1;
    return { timestamp: new Date(this.lastMs).toISOString(), sequence: this.seq };
  }
}

// Keyed by run_id so concurrent sessions keep independent sequences — a shared
// counter would interleave two runs' numbering and make each look full of holes.
const sequencers = new Map<string, EventSequencer>();

/** Return `event` stamped with its position in its run. */
export function stamp<T extends Record<string, unknown>>(event: T): T {
  const runId = event.run_id;
  if (typeof runId !== "string" || !runId) return event;
  let s = sequencers.get(runId);
  if (!s) {
    s = new EventSequencer();
    sequencers.set(runId, s);
  }
  const { timestamp, sequence } = s.next();
  // A closed session's state is dead weight in a long-lived process.
  if (event.event_type === "WorkflowCompleted" || event.event_type === "WorkflowFailed") sequencers.delete(runId);
  return { ...event, timestamp: event.timestamp ?? timestamp, sequence };
}
