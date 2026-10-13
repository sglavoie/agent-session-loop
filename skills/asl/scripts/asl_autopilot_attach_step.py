"""Attach-to-active-run step of the supervision loop."""

from __future__ import annotations

from asl_autopilot_hang import _hang_payload
from asl_autopilot_heartbeat import _heartbeat_detail
from asl_autopilot_lock_state import (
    _lock_created_at,
    _parse_timestamp,
    _runner_run_id,
    _state_written_after_lock,
)
from asl_autopilot_mailbox_watcher import _poll_mailbox_watcher
from asl_autopilot_status import (
    _attach_events_wait,
    _status_snapshot,
)


def _attach_step(session, status: dict, pending: int) -> dict | None:
    # Record who holds the plan and how much work was pending when we
    # first attached, so the unlocked iteration can attribute this
    # run's progress and read its persisted terminal reason. A lock
    # created at a different moment means another run took over; track
    # the latest, since it owns the lock we are waiting on, and drop any
    # run_id observed for the previous holder — it no longer identifies
    # the run we are waiting on. The lock is always observable; its
    # holder's `run_id` only becomes so once the runner persists its
    # initial state, which happens after the lock is taken.
    parsed = session.parsed
    heartbeat = session.heartbeat
    if session.attached_pending is None:
        session.attached_pending = pending
    observed_lock_created_at = _lock_created_at(status)
    if observed_lock_created_at != session.attached_lock_created_at:
        session.attached_lock_created_at = observed_lock_created_at
        session.attached_run_id = None
    # Trust a visible `run_id` only once it demonstrably came from the
    # current holder: the previous run's `state.json` stays on disk
    # until the new one overwrites it, so inside the startup window the
    # `run_id` on offer names the *wrong* run. Older `asl status`
    # payloads carry no lock `created_at` to check against; there,
    # trusting whatever identity is visible is still the best available.
    observed_run_id = _runner_run_id(status)
    if observed_run_id is not None and (
        _parse_timestamp(session.attached_lock_created_at) is None
        or _state_written_after_lock(status, session.attached_lock_created_at)
    ):
        session.attached_run_id = observed_run_id
    now = session.clock()
    if session.attach_start is None:
        session.attach_start = now
    attach_elapsed = now - session.attach_start
    if attach_elapsed >= session.attach_max_seconds:
        return session.run_result(
            False,
            "capped",
            status=status,
            chunks=session.chunks,
            error=(
                "attached run did not finish within the supervision "
                f"budget ({int(attach_elapsed)}s attached, limit "
                f"{int(session.attach_max_seconds)}s)"
            ),
        )
    if not parsed.get("no_mailbox") and session.mailbox_watcher is None:
        candidate = session.mailbox_watcher_factory(
            status,
            clock=session.clock,
            include_unanswered_backlog=True,
        )
        if candidate is not None and candidate.establish(now=now):
            session.mailbox_watcher = candidate
    if session.mailbox_watcher is not None:
        session.mailbox_watcher.set_run_id(session.attached_run_id, now=now)
        _poll_mailbox_watcher(
            session.mailbox_watcher,
            now=now,
            heartbeat=heartbeat,
        )
    escalation = session.attach_hang_escalation.observe(_status_snapshot(status))
    if escalation is not None:
        heartbeat(
            f"attached to active run; escalating worker_hung — {escalation['class']}"
        )
        return session.run_result(
            False,
            "worker_hung",
            status=status,
            chunks=session.chunks,
            hang=_hang_payload(escalation, parsed, status),
        )
    if (
        session.last_attach_beat is None
        or now - session.last_attach_beat >= session.heartbeat_interval
    ):
        context = f"(pending {pending}, elapsed {int(now - session.attach_start)}s)"
        is_stall, detail = _heartbeat_detail(
            _status_snapshot(status), session.stall_threshold
        )
        if detail is None:
            message = f"attached to active run; waiting {context}"
        elif is_stall:
            message = (
                f"attached to active run; WARN possibly stalled — {detail} {context}"
            )
        else:
            message = f"attached to active run; waiting {context} | {detail}"
        heartbeat(message)
        session.last_attach_beat = now
    # Between full probes, watch events.jsonl by os.stat rather than
    # re-running `asl status` every poll; a fresh full probe (which keeps
    # lock/pending classification authoritative) runs once the heartbeat
    # interval elapses or the file grows.
    _attach_events_wait(
        status.get("events_path"),
        poll_interval=session.poll_interval,
        heartbeat_interval=session.heartbeat_interval,
        clock=session.clock,
        sleep=session.sleep,
        events_stat=session.events_stat,
        mailbox_watcher=session.mailbox_watcher,
        heartbeat=heartbeat,
    )
    return None
