"""Incremental ``events.jsonl`` tailer feeding self-launched chunk heartbeats."""

from __future__ import annotations

import time

from asl_autopilot_constants import DEFAULT_STALL_IDLE_THRESHOLD
from asl_autopilot_status import _events_path_from_status

from agent_session_loop import events as _events
from agent_session_loop import tail as _tail


class _EventsTailer:
    """Derive per-tick heartbeat snapshots from an appended ``events.jsonl``.

    Constructed with the ``events_path`` named by the first authoritative
    ``asl status`` payload, it follows the JSONL file with
    :func:`agent_session_loop.tail.follow_events`, folding each newly appended
    ``turn_heartbeat``/``verification_heartbeat``/mailbox wait lifecycle/
    ``run_paused``/``run_finished`` record into a running view of the supervised
    child. :meth:`snapshot`
    returns a :func:`_status_snapshot`-shaped dict plus whether the event stream
    has gone silent for at least ``stall_threshold`` seconds — the authoritative
    stall signal for the event-driven path, so the 30s ``state.json`` refresh lag
    can no longer raise a false positive. Because the runner emits a
    ``turn_heartbeat`` every 30s while it is alive, a stream that stays silent
    past the threshold means the runner itself stopped, not merely a quiet child.
    This silence is measured directly against the monotonic clock, so it keeps
    aging even after ``idle_seconds`` freezes at the last heartbeat value —
    a below-threshold idle sample can no longer suppress the stall indefinitely.

    The underlying follower tolerates versionless (v1) records, unparseable
    "garbage" lines (skipped), and truncation or rotation of the events file
    (reopened from the start), so a tailer never crashes the supervised run; on
    any internal error the caller disables it and falls back to the injectable
    ``status`` probe.
    """

    def __init__(
        self,
        events_path: str,
        *,
        clock=time.monotonic,
        stall_threshold: float = DEFAULT_STALL_IDLE_THRESHOLD,
        follow=None,
    ) -> None:
        follow = follow if follow is not None else _tail.follow_events
        self._events = follow(events_path)
        self._clock = clock
        self._stall_threshold = stall_threshold
        self._snapshot: dict = {"state": "unknown"}
        self._pending_outbox: int | None = None
        self._last_event_at = clock()

    def snapshot(self):
        """Fold newly appended records and return ``(snapshot, events_stale)``.

        Reads the next batch of records the follower has to offer (all history on
        the first call, then only appended records), updates the derived snapshot
        to reflect the latest folded event, and reports whether the stream has
        been silent at least ``stall_threshold`` seconds. The returned snapshot is
        a fresh copy so callers cannot mutate the tailer's internal state.
        """
        now = self._clock()
        saw_event = False
        for record in next(self._events):
            saw_event = True
            self._apply(record)
        if saw_event:
            self._last_event_at = now
        events_stale = (now - self._last_event_at) >= self._stall_threshold
        return dict(self._snapshot), events_stale

    def _apply(self, record: object) -> None:
        if not isinstance(record, dict):
            return
        name = record.get("name")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        pending_outbox = payload.get("pending_outbox")
        if (
            isinstance(pending_outbox, int)
            and not isinstance(pending_outbox, bool)
            and pending_outbox >= 0
        ):
            self._pending_outbox = pending_outbox

        def update_snapshot(snapshot: dict) -> None:
            if self._pending_outbox is not None:
                snapshot["pending_outbox"] = self._pending_outbox
            self._snapshot = snapshot

        if name == _events.TURN_HEARTBEAT_EVENT:
            snapshot = {
                "state": "running",
                "session_id": payload.get("session_id"),
                "session_number": payload.get("session_number"),
                "total_sessions": payload.get("total_sessions"),
                "idle_seconds": payload.get("idle_seconds"),
                "elapsed_seconds": payload.get("elapsed_seconds"),
            }
            # The runner duplicates its live `hang_suspected` block into every
            # heartbeat while suspicion is active; a heartbeat without the
            # block is an implicit retraction (the fold drops the key).
            hang_suspected = payload.get("hang_suspected")
            if isinstance(hang_suspected, dict):
                snapshot["hang_suspected"] = hang_suspected
            # Same contract for the CPU sample: a heartbeat without the block
            # means no fresh sample, never stale evidence of progress.
            cpu_progress = payload.get("hang_cpu_progress")
            if isinstance(cpu_progress, dict):
                snapshot["hang_cpu_progress"] = cpu_progress
            update_snapshot(snapshot)
        elif name == "turn_started":
            update_snapshot(
                {
                    "state": "running",
                    "session_id": payload.get("session_id"),
                    "session_number": payload.get("session_number"),
                    "total_sessions": payload.get("total_sessions"),
                    "idle_seconds": 0,
                    "elapsed_seconds": 0,
                }
            )
        elif name == _events.TURN_HANG_SUSPECTED_EVENT:
            # One-shot per suspicion episode; fold onto the current snapshot so
            # escalation sees the block even before the next heartbeat rides in.
            snapshot = dict(self._snapshot)
            hang_suspected = payload.get("hang_suspected")
            if isinstance(hang_suspected, dict):
                snapshot["hang_suspected"] = hang_suspected
                cpu_progress = payload.get("hang_cpu_progress")
                if isinstance(cpu_progress, dict):
                    snapshot["hang_cpu_progress"] = cpu_progress
                else:
                    snapshot.pop("hang_cpu_progress", None)
                update_snapshot(snapshot)
        elif name == _events.TURN_HANG_CLEARED_EVENT:
            snapshot = dict(self._snapshot)
            snapshot.pop("hang_suspected", None)
            cpu_progress = payload.get("hang_cpu_progress")
            if isinstance(cpu_progress, dict):
                snapshot["hang_cpu_progress"] = cpu_progress
            else:
                snapshot.pop("hang_cpu_progress", None)
            # A retraction invalidates the idle reading from any earlier
            # record in the same batch: keeping it would let a pre-clear
            # 900s heartbeat trip the idle ceiling on the very tick the
            # runner withdrew suspicion. The runner emits the turn's fresh
            # idle on the cleared payload; without it, drop the stale value.
            idle = payload.get("idle_seconds")
            if isinstance(idle, (int, float)) and not isinstance(idle, bool):
                snapshot["idle_seconds"] = idle
            else:
                snapshot.pop("idle_seconds", None)
            update_snapshot(snapshot)
        elif name == _events.VERIFICATION_HEARTBEAT_EVENT:
            update_snapshot({"state": "verifying"})
        elif (
            name == _events.MAILBOX_EVENT
            and payload.get("action") == _events.MAILBOX_ACTION_WAIT_STARTED
        ):
            # A reply wait is live, bounded runner activity, not a terminal
            # pause. The runner records the actual unanswered-question count
            # from the same mailbox snapshot that established the wait.
            update_snapshot(
                {
                    "state": "waiting_for_reply",
                    "question_id": payload.get("question_id"),
                    "session_id": payload.get("session_id"),
                    "session_number": payload.get("session_number"),
                    "turn_index": payload.get("turn_index"),
                    "base_deadline": payload.get("base_deadline"),
                    "hard_deadline": payload.get("hard_deadline"),
                    "unsupervised_deadline_at": payload.get("unsupervised_deadline_at"),
                    "unsupervised_ceiling_seconds": payload.get(
                        "unsupervised_ceiling_seconds"
                    ),
                    "last_lease_activity_at": payload.get("last_lease_activity_at"),
                }
            )
        elif (
            name == _events.MAILBOX_EVENT
            and payload.get("action") == _events.MAILBOX_ACTION_WAIT_CLEARED
        ):
            # The next turn or terminal pause follows immediately, but clearing
            # here prevents the old wait from surviving until that next event.
            update_snapshot({"state": "unknown"})
        elif name == _events.RUN_PAUSED_EVENT:
            update_snapshot({"state": "paused", "reason": payload.get("reason")})
        elif name == "run_finished":
            update_snapshot({"state": "unknown"})


def _build_events_tailer(
    status_probe, clock, stall_threshold: float
) -> _EventsTailer | None:
    """Build an :class:`_EventsTailer` from the first ``asl status`` payload.

    Probes ``status_probe`` once to learn the plan's ``events_path``, then opens
    a tailer on it. Returns ``None`` — so :func:`_invoke_run` falls back to
    per-tick status probes — when there is no probe, the probe fails, the payload
    advertises no events path, or the tailer cannot be constructed.
    """
    if status_probe is None:
        return None
    try:
        status = status_probe()
    except Exception:
        return None
    events_path = _events_path_from_status(status)
    if not events_path:
        return None
    try:
        return _EventsTailer(events_path, clock=clock, stall_threshold=stall_threshold)
    except Exception:
        return None
