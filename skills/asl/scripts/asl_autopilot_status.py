"""``asl status`` probing, snapshots, and events-file attach waits for the autopilot."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from asl_autopilot_command_parse import _child_env, _status_argv
from asl_autopilot_constants import HELPER_NAME, RUN_STAGE
from asl_autopilot_launch_policy import _status_workspace
from asl_autopilot_mailbox_watcher import _MailboxWatcher, _poll_mailbox_watcher


def _emit_heartbeat(message: str) -> None:
    """Default heartbeat sink: concise progress on stderr, never stdout.

    stdout is reserved for the final JSON payload so callers can parse it
    without stripping progress noise.
    """
    sys.stderr.write(f"[{HELPER_NAME}] {message}\n")
    sys.stderr.flush()


def _status_error(error: str) -> dict:
    """Structured failure payload for a status probe that could not be read."""
    return {"ok": False, "stage": RUN_STAGE, "reason": "status-error", "error": error}


def _invoke_status(parsed: dict, *, runner=subprocess.run) -> dict:
    """Run ``asl status --json`` and return its parsed payload.

    On any transport failure (spawn error, non-zero exit, unparseable output)
    returns a ``status-error`` payload with ``ok: False`` so the supervisor can
    stop cleanly instead of guessing at state.
    """
    argv = _status_argv(parsed)
    try:
        completed = runner(
            argv,
            env=_child_env(parsed),
            cwd=parsed.get("cwd"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return _status_error(f"could not run `asl status`: {exc}")
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()[:400]
        return _status_error(f"`asl status` exited {completed.returncode}: {detail}")
    text = (completed.stdout or "").strip()
    if not text:
        return _status_error("`asl status --json` produced no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return _status_error(f"could not parse `asl status --json`: {exc}")
    if not isinstance(payload, dict):
        return _status_error("`asl status --json` did not return an object")
    return payload


def _status_snapshot(status: object) -> dict:
    """Distill an ``asl status --json`` payload into a heartbeat snapshot.

    Returns a small dict describing what the supervised run is doing right now:
    its ``state`` (``running``/``verifying``/``finalizing``/
    ``waiting_for_reply``/``paused``/``unknown``), the live
    ``session_id``/``session_number``/``total_sessions`` labels, and the
    ``idle_seconds``/``elapsed_seconds`` health signals.  A readable mailbox
    projection also contributes ``pending_outbox`` from its unanswered-question
    count. A missing, non-dict, or ``ok: False`` payload — or one with no live
    activity — degrades to ``{"state": "unknown"}`` so callers can fall back to
    the plain heartbeat without special-casing every failure mode. A paused run
    yields ``{"state": "paused"}``, which :func:`_format_heartbeat` also renders
    as the plain line. Pause takes precedence over a stale wait record.
    """
    if not isinstance(status, dict) or not status.get("ok"):
        return {"state": "unknown"}

    runner_state = (
        status.get("runner_state")
        if isinstance(status.get("runner_state"), dict)
        else {}
    )
    mailbox_status = status.get("mailbox")
    pending_outbox = (
        mailbox_status.get("open_questions")
        if isinstance(mailbox_status, dict)
        else None
    )
    common = {}
    workspace = _status_workspace(status)
    if workspace is not None:
        common["workspace_root"] = workspace["workspace_root"]
        common["registry_digest"] = workspace.get("registry_digest")
        common["repository_aliases"] = [
            repository["alias"] for repository in workspace["repositories"][:20]
        ]
        common["repository_count"] = len(workspace["repositories"])
    if (
        isinstance(pending_outbox, int)
        and not isinstance(pending_outbox, bool)
        and pending_outbox >= 0
    ):
        common["pending_outbox"] = pending_outbox

    if runner_state.get("paused") or status.get("paused"):
        return {
            "state": "paused",
            "reason": runner_state.get("reason") or status.get("reason"),
            **common,
        }

    waiting_for_reply = runner_state.get("waiting_for_reply") or status.get(
        "waiting_for_reply"
    )
    if isinstance(waiting_for_reply, dict) and waiting_for_reply:
        waiting_status = runner_state.get("waiting_for_reply_status")
        if waiting_status is None:
            waiting_status = status.get("waiting_for_reply_status")
        snapshot = {
            "state": "waiting_for_reply",
            "question_id": waiting_for_reply.get("question_id"),
            "session_id": waiting_for_reply.get("session_id"),
            "session_number": waiting_for_reply.get("session_number"),
            "turn_index": waiting_for_reply.get("turn_index"),
            "base_deadline": waiting_for_reply.get("base_deadline"),
            "hard_deadline": waiting_for_reply.get("hard_deadline"),
            "unsupervised_deadline_at": waiting_for_reply.get(
                "unsupervised_deadline_at"
            ),
            "unsupervised_ceiling_seconds": waiting_for_reply.get(
                "unsupervised_ceiling_seconds"
            ),
            "last_lease_activity_at": waiting_for_reply.get("last_lease_activity_at"),
            **common,
        }
        if isinstance(waiting_status, str) and waiting_status:
            snapshot["waiting_for_reply_status"] = waiting_status
        return snapshot

    active_turn = runner_state.get("active_turn") or status.get("active_turn")
    if isinstance(active_turn, dict) and active_turn:
        snapshot = {
            "state": "running",
            "session_id": active_turn.get("session_id"),
            "session_number": active_turn.get("session_number"),
            "total_sessions": active_turn.get("total_sessions"),
            "idle_seconds": active_turn.get("idle_seconds"),
            "elapsed_seconds": active_turn.get("elapsed_seconds"),
            **common,
        }
        # The runner's hang sentinel persists its suspicion block on the live
        # turn; carrying it here covers the attach path and the per-tick probe
        # fallback, so escalation sees the same evidence either way.
        hang_suspected = active_turn.get("hang_suspected")
        if isinstance(hang_suspected, dict):
            snapshot["hang_suspected"] = hang_suspected
        # Published on every sampled tick, suspicion or not, so the idle
        # ceiling can tell a silent worker from a stopped one.
        cpu_progress = active_turn.get("hang_cpu_progress")
        if isinstance(cpu_progress, dict):
            snapshot["hang_cpu_progress"] = cpu_progress
        return snapshot

    active_verification = runner_state.get("active_verification") or status.get(
        "active_verification"
    )
    if isinstance(active_verification, dict) and active_verification:
        return {"state": "verifying", **common}

    active_finalization = runner_state.get("active_finalization") or status.get(
        "active_finalization"
    )
    if isinstance(active_finalization, dict) and active_finalization:
        return {
            "state": "finalizing",
            "phase": active_finalization.get("phase"),
            "elapsed_seconds": active_finalization.get("elapsed_seconds"),
            "cancellable": active_finalization.get("cancellable"),
            **common,
        }

    return {"state": "unknown", **common}


def _events_path_from_status(status: object) -> str | None:
    """Return the ``events_path`` an ``asl status`` payload advertises, if any.

    ``asl status --json`` includes the plan's ``events.jsonl`` path at the top
    level (``state.status_payload``). A non-dict, ``ok: False``, or path-less
    payload yields ``None`` so callers fall back to per-tick status probes.
    """
    if isinstance(status, dict) and status.get("ok"):
        path = status.get("events_path")
        if isinstance(path, str) and path:
            return path
    return None


def _events_size(path: str | None) -> int | None:
    """Current byte size of an ``events.jsonl``, or ``None`` when unreadable."""
    if not path:
        return None
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _attach_events_wait(
    events_path,
    *,
    poll_interval: float,
    heartbeat_interval: float,
    clock,
    sleep,
    events_stat,
    mailbox_watcher: _MailboxWatcher | None = None,
    heartbeat=_emit_heartbeat,
) -> None:
    """Wait between attach-mode full ``asl status`` probes, watching events.jsonl.

    Sleeps ``poll_interval`` slices while ``os.stat``-ing the events file and
    returns — signalling the caller to run a fresh full probe — once the
    ``heartbeat_interval`` elapses or the file grows (new activity or a terminal
    event). When a mailbox watcher is active, each sleep slice also honors its
    poll/lease deadline and emits newly observed questions through the supplied
    heartbeat sink. With no events file to watch (no path, or an unreadable one)
    it degrades to a single bounded sleep so callers without a live event stream
    keep the legacy full-probe-every-poll cadence.
    """

    def poll_mailbox(now: float) -> None:
        if mailbox_watcher is None:
            return
        _poll_mailbox_watcher(
            mailbox_watcher,
            now=now,
            heartbeat=heartbeat,
        )

    def sleep_slice() -> float:
        duration = poll_interval
        if mailbox_watcher is not None:
            deadline = mailbox_watcher.next_deadline()
            if deadline is not None:
                duration = min(duration, max(0.0, deadline - clock()))
        sleep(duration)
        if mailbox_watcher is not None:
            poll_mailbox(clock())
        return duration

    baseline = events_stat(events_path)
    if baseline is None:
        sleep_slice()
        return
    start = clock()
    while True:
        sleep_slice()
        current = events_stat(events_path)
        if current is None or current != baseline:
            return
        if clock() - start >= heartbeat_interval:
            return
