"""Detecting and recording runs whose process died."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import artifacts, events, plans_files, plans_models, state
from .state import (
    LOCK_FILE_NAME,
    LockInfo,
    StateError,
    _dump_state_bytes,
    _nonempty_str,
    plan_state_dir,
    state_root,
)
from .state_assurance_pending import (
    ASSURANCE_PENDING_FILE_NAME,
    _write_assurance_pending_record,
)
from .state_plan_refs import (
    _read_json_object_safe,
    _recorded_content_hash,
    iter_plan_state_dirs,
)

# Sidecar `flock` target that serializes abandonment. Deliberately not the
# plan lock: abandonment runs *while* another process holds or is taking that
# lock, so the two claims have to be independent.
ABANDON_CLAIM_FILE_NAME = "abandon.lock"


# How long an unfinished run may go without any lock file before its absence
# counts as proof of death. A booting runner writes `state.json` before its
# lock, so the window has to outlast that gap by a wide margin.
ABANDONED_NO_LOCK_GRACE_SECONDS = 120.0


ABANDONED_REASON = "abandoned"


ABANDONED_TRIGGER_LOCK_TAKEOVER = "lock-takeover"


ABANDONED_TRIGGER_STATUS = "status"


def _stale_runner_state(
    runner_state: dict[str, Any],
    lock_info: LockInfo | None,
) -> tuple[bool, str | None]:
    """Detect unfinished `state.json` that no active lock is guarding.

    Returns ``(stale_unfinished, stale_reason)``. The pair is ``(False, None)``
    when the runner state is absent, unreadable, already finished, or still
    held by an active lock. When an active lock is missing but the persisted
    state never set ``run_finished_at``, surface a short, human-readable
    reason so the operator can see the orphan in both JSON and text output.
    """
    if not runner_state.get("present"):
        return False, None
    if runner_state.get("error"):
        return False, None
    if runner_state.get("run_finished_at"):
        return False, None
    if lock_info is not None and lock_info.active:
        return False, None
    if lock_info is None:
        return True, "runner state has not finished but no lock file is present"
    return (
        True,
        f"runner state has not finished but lock is stale: {lock_info.reason}",
    )


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a ``Z`` suffix and naive input."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _unfinished_run_id(state_obj: dict[str, Any] | None) -> str | None:
    """Return the run id of a state that recorded a start but never a finish."""
    if not isinstance(state_obj, dict):
        return None
    if state_obj.get("run_finished_at") is not None:
        return None
    return _nonempty_str(state_obj.get("run_id"))


def _abandonment_is_proven(
    state_obj: dict[str, Any],
    *,
    lock_path: Path,
    lock_info: LockInfo | None,
) -> bool:
    """Decide whether the run that wrote ``state_obj`` is provably dead.

    Deliberately narrow, because the write it authorizes is irreversible: a
    stale lock with no usable pid (wrong plan directory, unparseable payload)
    proves nothing, and a pid recorded on another host says nothing about this
    host's process table. Holding the lock is not itself proof either — the
    caller that just took a stale lock over still has to prove the previous
    owner is dead.
    """
    if lock_info is None:
        started_at = _parse_timestamp(state_obj.get("run_started_at"))
        if started_at is None:
            return False
        age_seconds = (datetime.now(UTC) - started_at).total_seconds()
        if age_seconds < ABANDONED_NO_LOCK_GRACE_SECONDS:
            return False
        # A booting runner writes state.json before its lock, so re-read the
        # lock rather than trusting a snapshot taken before that gap closed.
        return not lock_path.exists()
    if lock_info.active or lock_info.pid is None or lock_info.foreign_host:
        return False
    return not state.is_pid_running(lock_info.pid)


def _abandoned_event_payload(
    payload: dict[str, Any],
    *,
    state_path: Path,
    events_path: Path,
) -> dict[str, Any]:
    """Build the synthetic ``run_finished`` payload for an abandoned run.

    A strict subset of the payload
    :func:`run_persistence._run_finished_event_payload` builds, so existing
    consumers keep reading the same field names, plus the two keys that mark
    the record as reconstructed rather than observed.
    """
    counts: dict[str, Any] | None = None
    final_snapshot = payload.get("final_snapshot")
    if isinstance(final_snapshot, dict):
        snapshot_counts = final_snapshot.get("counts")
        if isinstance(snapshot_counts, dict):
            counts = snapshot_counts
    return {
        "run_id": payload.get("run_id"),
        "run_dir": payload.get("run_dir"),
        "ok": payload.get("ok"),
        "paused": payload.get("paused"),
        "reason": payload.get("reason"),
        "run_started_at": payload.get("run_started_at"),
        "run_finished_at": payload.get("run_finished_at"),
        "duration_seconds": payload.get("duration_seconds"),
        "counts": counts,
        "completed_sessions": payload.get("completed_sessions"),
        "completed_session_ids": payload.get("completed_session_ids"),
        "state_path": str(state_path),
        "events_path": str(events_path),
        "abandoned": payload.get("abandoned"),
        "assurance_pending": payload.get("assurance_pending"),
        "synthetic": True,
        "outcome": events.RUN_OUTCOME_ABANDONED,
    }


@contextmanager
def _abandonment_claim(plan_state_dir_path: Path) -> Iterator[bool]:
    """Hold the per-plan exclusive claim on terminalizing an abandoned run.

    ``flock`` is held by the open file description rather than the process, so
    this excludes concurrent threads as well as concurrent processes, and the
    kernel drops it if the holder dies mid-write. Failing to take it yields
    ``False`` instead of waiting: the claim is only ever held for the two writes
    below, so contention means another caller is already publishing the terminal
    record, and blocking would put an unbounded wait inside ``asl status`` and
    lock acquisition for no gain.
    """
    claim_path = plan_state_dir_path / ABANDON_CLAIM_FILE_NAME
    fd: int | None = None
    held = False
    try:
        fd = os.open(claim_path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError:
        pass
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                with suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def mark_run_abandoned(
    plan_state: str | Path,
    *,
    lock_info: LockInfo | None,
    trigger: str,
) -> bool:
    """Give a run that died mid-turn the terminal record it never wrote.

    A killed runner leaves ``run_finished_at`` null forever, so every consumer
    that waits for a terminal event — ``asl tail --follow``, the supervisor,
    state-based readiness — waits forever. This reconstructs that ending, but
    only once death is proven; see :func:`_abandonment_is_proven`.

    Returns whether the terminal state was written — exactly one caller ever
    gets ``True`` for a given run, including under concurrency, because the
    recheck and both writes run under :func:`_abandonment_claim`. Best-effort
    throughout and never raises: hygiene must not be able to break lock
    acquisition or ``asl status``, both of which call it on paths that must
    keep working.
    """
    try:
        plan_state_dir_path = Path(plan_state)
        state_path = plan_state_dir_path / events.STATE_FILE_NAME
        state_obj = _read_json_object_safe(state_path)
        run_id = _unfinished_run_id(state_obj)
        if run_id is None or state_obj is None:
            return False
        if not _abandonment_is_proven(
            state_obj,
            lock_path=plan_state_dir_path / LOCK_FILE_NAME,
            lock_info=lock_info,
        ):
            return False

        # Everything from the final recheck through the event append runs under
        # one claim. Without it two callers can both pass the recheck and each
        # publish a terminal record; a caller that cannot take the claim is by
        # definition racing one that already holds it, so it declines.
        with _abandonment_claim(plan_state_dir_path) as claimed:
            if not claimed:
                return False

            # Re-read immediately before writing. A runner that finished
            # between the first read and now owns the terminal record, and
            # idempotence depends on losing that race instead of overwriting
            # the winner.
            current = _read_json_object_safe(state_path)
            if current is None or _unfinished_run_id(current) != run_id:
                return False

            detected_at = datetime.now(UTC).isoformat()
            payload = dict(current)
            prior_reason = current.get("reason")
            payload.update(
                {
                    "ok": False,
                    "reason": ABANDONED_REASON,
                    "run_finished_at": detected_at,
                    # The same fields ``commit_settlement._finish_run`` clears. A wait
                    # record is deliberately left behind: it is the only
                    # evidence of what the run died holding, and it already
                    # reads as expired once no active lock backs it.
                    "active_turn": None,
                    "active_verification": None,
                    "active_finalization": None,
                    "abandoned": {
                        "detected_at": detected_at,
                        "trigger": trigger,
                        "lock_reason": (
                            lock_info.reason if lock_info is not None else None
                        ),
                        "detected_by_pid": os.getpid(),
                        # Overwriting ``reason`` loses the only record that the
                        # run had already stopped cleanly before it was killed,
                        # which is what separates a supervisor-requested stop
                        # from a death nobody asked for.
                        "prior_reason": prior_reason,
                    },
                }
            )
            # The audit this run never ran is recorded, never launched: this
            # code path runs inside `asl status` and inside lock acquisition,
            # and neither may block on a harness.
            record = _write_assurance_pending_record(
                plan_state_dir_path,
                payload,
                run_id=run_id,
                trigger=trigger,
                detected_at=detected_at,
            )
            if record is not None:
                payload["assurance_pending"] = {
                    "run_id": run_id,
                    "recorded_at": detected_at,
                    "skip": record["skip"],
                    "path": str(Path(record["run_dir"]) / ASSURANCE_PENDING_FILE_NAME),
                }
            events_path = plan_state_dir_path / events.EVENTS_FILE_NAME
            artifacts.atomic_write_bytes(
                state_path, _dump_state_bytes(payload), durable=True
            )
            # The terminal state is already durable; an unwritable events
            # file must not downgrade that to a failure the caller retries.
            with contextlib.suppress(OSError):
                events.append_event(
                    events_path,
                    events.Event(
                        name=events.RUN_FINISHED_EVENT,
                        payload=_abandoned_event_payload(
                            payload,
                            state_path=state_path,
                            events_path=events_path,
                        ),
                    ),
                )
            return True
    except Exception:
        # Death has already cost the caller its run; refusing to record that
        # is strictly better than raising into a lock acquire or a status read.
        return False


def _live_run_id(
    runner_state: dict[str, Any],
    lock_info: LockInfo | None,
) -> str | None:
    """Return the run id of a process that can still consume a mailbox answer.

    Stricter than :func:`_stale_runner_state`, which reports a *finished* run as
    non-stale: a finished or paused run is equally unable to act on a reply, so
    its questions are history rather than a live block.
    """
    run_id = runner_state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    if not runner_state.get("present") or runner_state.get("error"):
        return None
    if runner_state.get("paused") or runner_state.get("run_finished_at"):
        return None
    if lock_info is None or not lock_info.active:
        return None
    return run_id


def _migrate_command_text(
    plan_dir: str,
    from_plan_dir: str,
    state_dir: str | Path | None,
) -> str:
    """Build the ``asl migrate-state`` command that relocates orphaned state."""
    parts = ["asl", "migrate-state", plan_dir, "--from", from_plan_dir]
    if state_dir:
        parts.extend(["--state-dir", str(state_dir)])
    return shlex.join(parts)


def orphaned_state_candidates(
    snapshot: plans_models.PlanSnapshot,
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Find moved-plan state directories whose content matches this plan.

    When this plan's own state directory already holds ``state.json`` the scan
    is skipped (the common case) and an empty list is returned. Otherwise every
    per-plan state directory under the state root is inspected for a recorded
    ``plan_dir`` that no longer exists on disk whose recorded content hash
    matches the current plan content hash; each match is a migration candidate.
    The recorded hash prefers ``final_snapshot.content_sha256`` (a completed run
    mutates handoffs, so the moved plan matches its final content) and falls
    back to ``initial_snapshot.content_sha256`` only for legacy state. Discovery
    is read-only and tolerates malformed or concurrently removed records.
    """
    root = state_root(state_dir)
    plan_state = plan_state_dir(snapshot.plan_dir, state_dir)
    if (plan_state / events.STATE_FILE_NAME).exists():
        return []
    try:
        state_dirs = list(iter_plan_state_dirs(root))
    except StateError:
        return []
    candidates: list[dict[str, Any]] = []
    for candidate_dir in state_dirs:
        if candidate_dir == plan_state:
            continue
        state_obj = _read_json_object_safe(candidate_dir / events.STATE_FILE_NAME)
        if state_obj is None:
            continue
        recorded_plan_dir = _nonempty_str(state_obj.get("plan_dir"))
        if recorded_plan_dir is None:
            continue
        recorded_hash = _recorded_content_hash(state_obj)
        if recorded_hash != snapshot.content_sha256:
            continue
        try:
            still_exists = plans_files.canonicalize_path(recorded_plan_dir).is_dir()
        except OSError, ValueError:
            still_exists = False
        if still_exists:
            continue
        candidates.append(
            {
                "old_plan_dir": recorded_plan_dir,
                "state_dir": str(candidate_dir),
                "run_finished_at": state_obj.get("run_finished_at"),
                "migrate_command": _migrate_command_text(
                    str(snapshot.plan_dir), recorded_plan_dir, state_dir
                ),
            }
        )
    candidates.sort(key=lambda item: item["state_dir"])
    return candidates
