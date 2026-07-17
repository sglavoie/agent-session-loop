"""Operational state and locking boundaries.

Runner state exists to coordinate local execution, capture logs, and recover
from pauses. It must never replace the plan files as the durable completion
record.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import config, events, plans, responses
from .formatting import format_duration_seconds


LOCK_FILE_NAME = "lock.json"
RUNS_DIR_NAME = "runs"
RESULT_FILE_NAME = "result.json"
TURN_DIR_PREFIX = "turn-"


class StateError(Exception):
    """Raised when operational state cannot be read or updated safely."""


class AmbiguousPlanError(StateError):
    """Raised when a bare plan label matches multiple plan directories.

    A subclass of :class:`StateError` so existing ``except StateError`` guards
    keep working, while callers that need the ``ambiguous-plan`` reason can
    catch this type first. Carries the sorted canonical candidates and the
    resolved state root that produced them.
    """

    def __init__(
        self,
        label: str,
        candidates: list[Path],
        state_root: Path,
    ) -> None:
        self.label = label
        self.candidates = sorted(candidates, key=str)
        self.state_root = state_root
        joined = ", ".join(str(candidate) for candidate in self.candidates)
        super().__init__(
            f"plan label {label!r} is ambiguous under {state_root}: "
            f"matches {joined}"
        )


@dataclass(frozen=True)
class LockInfo:
    """Parsed operational lock state for one plan directory."""

    path: Path
    pid: int | None
    plan_dir: str
    created_at: str
    stale: bool
    reason: str
    raw: dict[str, Any]

    @property
    def active(self) -> bool:
        return not self.stale


@dataclass(frozen=True)
class UnlockResult:
    """Result from removing or checking a plan lock."""

    path: Path
    had_lock: bool
    removed: bool
    was_active: bool
    was_stale: bool
    message: str


def state_root(state_dir: str | Path | None = None) -> Path:
    if state_dir is None:
        return config.default_state_root()
    return plans.canonicalize_path(state_dir)


def plan_state_dir(plan_dir: str | Path, state_dir: str | Path | None = None) -> Path:
    return state_root(state_dir) / plans.path_sha256(plan_dir)


def lock_path_for_plan(plan_dir: str | Path, state_dir: str | Path | None = None) -> Path:
    return plan_state_dir(plan_dir, state_dir) / LOCK_FILE_NAME


def _is_bare_label(raw_value: str) -> bool:
    """Return True when ``raw_value`` is a bare basename with no path parts.

    Only bare labels trigger state lookup. Anything containing a directory
    component (``foo/bar``, ``./foo``, ``/abs``) or the ``.``/``..`` relative
    references retains the current path-only behavior.
    """
    if raw_value in ("", ".", ".."):
        return False
    head, tail = os.path.split(raw_value)
    if head:
        return False
    return tail == raw_value


def _read_json_object_safe(path: Path) -> dict[str, Any] | None:
    """Best-effort read of a JSON object; None on any failure or non-object.

    Tolerates malformed, unreadable, or concurrently removed files so one bad
    metadata record cannot block discovery of other candidates.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _plan_dir_from_result(payload: dict[str, Any]) -> str | None:
    """Recover a plan directory from a turn result, ``after`` before ``before``."""
    for section_key in ("after", "before"):
        section = payload.get(section_key)
        if isinstance(section, dict):
            value = _nonempty_str(section.get("plan_dir"))
            if value:
                return value
    return None


def _iter_result_paths(runs_dir: Path) -> Iterator[Path]:
    """Yield ``runs/*/turn-*/result.json`` files under one state directory."""
    try:
        if not runs_dir.is_dir():
            return
        run_dirs = sorted(runs_dir.iterdir())
    except OSError:
        return
    for run_dir in run_dirs:
        try:
            if not run_dir.is_dir():
                continue
            turn_dirs = sorted(run_dir.iterdir())
        except OSError:
            continue
        for turn_dir in turn_dirs:
            try:
                if not turn_dir.name.startswith(TURN_DIR_PREFIX):
                    continue
                if not turn_dir.is_dir():
                    continue
                result_path = turn_dir / RESULT_FILE_NAME
                if result_path.is_file():
                    yield result_path
            except OSError:
                continue


def _collect_plan_dir_strings(state_dir: Path) -> list[str]:
    """Gather raw ``plan_dir`` strings from one per-plan state directory.

    Sources, in order: ``state.json``, ``lock.json``, then every
    ``runs/*/turn-*/result.json`` recovering ``after.plan_dir`` before
    ``before.plan_dir``. Missing or malformed records are silently skipped.
    """
    results: list[str] = []
    state_obj = _read_json_object_safe(state_dir / events.STATE_FILE_NAME)
    if state_obj is not None:
        value = _nonempty_str(state_obj.get("plan_dir"))
        if value:
            results.append(value)
    lock_obj = _read_json_object_safe(state_dir / LOCK_FILE_NAME)
    if lock_obj is not None:
        value = _nonempty_str(lock_obj.get("plan_dir"))
        if value:
            results.append(value)
    for result_path in _iter_result_paths(state_dir / RUNS_DIR_NAME):
        result_obj = _read_json_object_safe(result_path)
        if result_obj is None:
            continue
        value = _plan_dir_from_result(result_obj)
        if value:
            results.append(value)
    return results


def _iter_plan_state_dirs(state_root: Path) -> Iterator[Path]:
    """Yield per-plan state directories under ``state_root``.

    A missing root scans as an empty corpus. A root that exists but is not a
    directory, or that cannot be enumerated, raises :class:`StateError`.
    """
    if not state_root.exists():
        return
    if not state_root.is_dir():
        raise StateError(f"state root is not a directory: {state_root}")
    try:
        children = sorted(state_root.iterdir())
    except OSError as error:
        raise StateError(
            f"could not scan state root {state_root}: {error}"
        ) from error
    for child in children:
        try:
            if child.is_dir():
                yield child
        except OSError:
            continue


def resolve_plan_reference(raw_value: str, state_root: Path) -> Path:
    """Resolve a plan argument to a plan directory, honoring direct paths.

    Existing directories always win and are returned canonicalized without any
    state lookup. Path-like nonexistent arguments (those with a directory
    component) also bypass lookup and return the canonicalized argument. Only a
    bare, nonexistent label scans the state corpus under ``state_root``,
    matching persisted ``plan_dir`` basenames exactly and case-sensitively
    against existing plan directories.

    Returns the sole matching plan directory, or the canonicalized argument
    when nothing matches. Raises :class:`AmbiguousPlanError` when more than one
    existing plan directory shares the requested basename. Discovery is
    read-only and independent of process liveness.
    """
    original = plans.canonicalize_path(raw_value)
    if original.is_dir():
        return original
    if not _is_bare_label(raw_value):
        return original

    matches: dict[str, Path] = {}
    for state_dir in _iter_plan_state_dirs(state_root):
        for raw_plan_dir in _collect_plan_dir_strings(state_dir):
            try:
                candidate = plans.canonicalize_path(raw_plan_dir)
            except (OSError, ValueError):
                continue
            key = str(candidate)
            if key in matches:
                continue
            if candidate.name != raw_value:
                continue
            try:
                is_existing_dir = candidate.is_dir()
            except OSError:
                continue
            if is_existing_dir:
                matches[key] = candidate

    if not matches:
        return original
    if len(matches) == 1:
        return next(iter(matches.values()))
    raise AmbiguousPlanError(raw_value, list(matches.values()), state_root)


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _coerce_pid(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _read_lock_path(lock_path: Path, expected_plan_dir: Path) -> LockInfo | None:
    if not lock_path.exists():
        return None

    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return LockInfo(
            path=lock_path,
            pid=None,
            plan_dir="",
            created_at="",
            stale=True,
            reason=f"invalid lock file: {error}",
            raw={},
        )

    if not isinstance(raw, dict):
        raw = {}

    pid = _coerce_pid(raw.get("pid"))
    plan_dir = str(raw.get("plan_dir", ""))
    created_at = str(raw.get("created_at", ""))
    stale = False
    reason = "active"

    if plan_dir and plans.canonicalize_path(plan_dir) != expected_plan_dir:
        stale = True
        reason = "lock plan directory does not match requested plan"
    elif pid is None:
        stale = True
        reason = "lock has no valid pid"
    elif not is_pid_running(pid):
        stale = True
        reason = f"pid {pid} is not running"

    return LockInfo(
        path=lock_path,
        pid=pid,
        plan_dir=plan_dir,
        created_at=created_at,
        stale=stale,
        reason=reason,
        raw=raw,
    )


def read_lock_info(plan_dir: str | Path, state_dir: str | Path | None = None) -> LockInfo | None:
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    return _read_lock_path(lock_path_for_plan(canonical_plan_dir, state_dir), canonical_plan_dir)


def acquire_lock(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    owner: str = "agent-session-loop",
) -> LockInfo:
    """Create a lock for a plan, removing stale locks first."""
    snapshot = plans.inspect_plan(plan_dir)
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "owner": owner,
        "pid": os.getpid(),
        "plan_dir": str(snapshot.plan_dir),
        "plan_path_hash": snapshot.path_hash,
        "plan_hash": snapshot.plan_hash,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    for _attempt in range(2):
        try:
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as error:
            existing = read_lock_info(snapshot.plan_dir, state_dir)
            if existing is not None and existing.stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if existing is None:
                continue
            raise StateError(
                f"plan is locked by pid {existing.pid} at {lock_path}"
            ) from error

        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            json.dump(payload, lock_file, sort_keys=True)
            lock_file.write("\n")
        lock_info = read_lock_info(snapshot.plan_dir, state_dir)
        if lock_info is None:
            raise StateError(f"created lock disappeared: {lock_path}")
        return lock_info

    raise StateError(f"could not acquire lock at {lock_path}")


def release_lock(plan_dir: str | Path, state_dir: str | Path | None = None) -> None:
    lock_path = lock_path_for_plan(plan_dir, state_dir)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return


def unlock_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
    *,
    force: bool = False,
) -> UnlockResult:
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    lock_path = lock_path_for_plan(canonical_plan_dir, state_dir)
    lock_info = _read_lock_path(lock_path, canonical_plan_dir)

    if lock_info is None:
        return UnlockResult(
            path=lock_path,
            had_lock=False,
            removed=False,
            was_active=False,
            was_stale=False,
            message=f"no lock present at {lock_path}",
        )

    if lock_info.active and not force:
        raise StateError(
            f"active lock held by pid {lock_info.pid} at {lock_path}; use --force to remove it"
        )

    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass

    state_label = "active" if lock_info.active else "stale"
    return UnlockResult(
        path=lock_path,
        had_lock=True,
        removed=True,
        was_active=lock_info.active,
        was_stale=lock_info.stale,
        message=f"removed {state_label} lock at {lock_path}",
    )


def _status_counts(snapshot: plans.PlanSnapshot) -> dict[str, int]:
    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    return counts


def _session_number_alias(session_id: str) -> int | None:
    parsed = plans.parse_session_id(session_id)
    return parsed.as_int() if parsed.is_integer else None


def _session_compat_value(session_id: str) -> int | str:
    number = _session_number_alias(session_id)
    return number if number is not None else session_id


def _session_payload(session: plans.SessionStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session.id,
        "status": session.status,
        "raw_value": session.raw_value,
        "line_number": session.line_number,
        "path": str(session.path),
    }
    number = _session_number_alias(session.id)
    if number is not None:
        payload["number"] = number
    return payload


def _session_label_from_payload(payload: dict[str, Any]) -> str:
    session_id = payload.get("session_id")
    if session_id is not None:
        return str(session_id)
    session = payload.get("session")
    if session is not None:
        return str(session)
    return "unknown"


def _lock_payload(lock_info: LockInfo | None, lock_path: Path) -> dict[str, Any]:
    if lock_info is None:
        return {
            "present": False,
            "active": False,
            "stale": False,
            "path": str(lock_path),
        }
    return {
        "present": True,
        "active": lock_info.active,
        "stale": lock_info.stale,
        "pid": lock_info.pid,
        "plan_dir": lock_info.plan_dir,
        "created_at": lock_info.created_at,
        "reason": lock_info.reason,
        "path": str(lock_info.path),
    }


def _runner_state_payload(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {
            "present": False,
            "path": str(state_path),
            "error": None,
            "active_turn": None,
            "active_verification": None,
        }

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The file vanished between ``exists()`` and ``read_text()``; treat
        # it as absent rather than malformed so concurrent runner cleanup
        # does not surface a 500 to the Workbench UI.
        return {
            "present": False,
            "path": str(state_path),
            "error": None,
            "active_turn": None,
            "active_verification": None,
        }
    except (OSError, json.JSONDecodeError) as error:
        return {
            "present": True,
            "path": str(state_path),
            "error": str(error),
            "active_turn": None,
            "active_verification": None,
        }

    if not isinstance(raw, dict):
        return {
            "present": True,
            "path": str(state_path),
            "error": "state file did not contain a JSON object",
            "active_turn": None,
            "active_verification": None,
        }

    active_turn = raw.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

    active_verification = raw.get("active_verification")
    if not isinstance(active_verification, dict):
        active_verification = None

    return {
        "present": True,
        "path": str(state_path),
        "error": None,
        "run_id": raw.get("run_id"),
        "harness": raw.get("harness"),
        "ok": raw.get("ok"),
        "paused": raw.get("paused"),
        "reason": raw.get("reason"),
        "run_started_at": raw.get("run_started_at"),
        "run_finished_at": raw.get("run_finished_at"),
        "duration_seconds": raw.get("duration_seconds"),
        "active_turn": active_turn,
        "active_verification": active_verification,
    }


def _progress_journal_payload(plan_dir: Path) -> dict[str, Any]:
    """Summarize the runner-managed `progress.md` journal for `asl status`.

    Shape stays stable across the three states a journal can be in so JSON
    consumers can branch cleanly: absent (`present=False`), readable
    (`entry_count`/`last_appended` populated), or present-but-malformed
    (`error` set, counts None).
    """
    journal_path = plans.progress_path(plan_dir)
    if not journal_path.exists():
        return {
            "present": False,
            "path": str(journal_path),
            "entry_count": None,
            "last_appended": None,
            "error": None,
        }
    try:
        entries = plans.read_progress_entries(plan_dir)
    except plans.PlanError as error:
        return {
            "present": True,
            "path": str(journal_path),
            "entry_count": None,
            "last_appended": None,
            "error": str(error),
        }
    last_appended = entries[-1].completed_at if entries else None
    return {
        "present": True,
        "path": str(journal_path),
        "entry_count": len(entries),
        "last_appended": last_appended,
        "error": None,
    }


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


def status_payload(
    snapshot: plans.PlanSnapshot,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    # Local import avoids a module cycle: repair planning depends on state for
    # its mutating materializer, while this status path only uses its read-only
    # continuation evaluator after state has finished importing.
    from . import review_continuation

    plan_state = plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    event_path = plan_state / events.EVENTS_FILE_NAME
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = _read_lock_path(lock_path, snapshot.plan_dir)
    next_pending = snapshot.next_pending
    runner_state = _runner_state_payload(state_path)
    stale_unfinished, stale_reason = _stale_runner_state(runner_state, lock_info)
    runner_state["stale_unfinished"] = stale_unfinished
    runner_state["stale_reason"] = stale_reason
    progress_journal = _progress_journal_payload(snapshot.plan_dir)
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, event_path
    ).to_payload()
    return responses.success_payload(
        "status",
        plan_dir=str(snapshot.plan_dir),
        context_path=str(snapshot.context_path),
        manifest_path=str(snapshot.manifest_path),
        content_sha256=snapshot.content_sha256,
        path_hash=snapshot.path_hash,
        plan_hash=snapshot.plan_hash,
        state_dir=str(plan_state),
        state_path=str(state_path),
        events_path=str(event_path),
        runner_state=runner_state,
        active_turn=runner_state.get("active_turn"),
        active_verification=runner_state.get("active_verification"),
        counts=_status_counts(snapshot),
        next_pending=(
            _session_compat_value(next_pending.id)
            if next_pending is not None
            else None
        ),
        next_pending_id=next_pending.id if next_pending is not None else None,
        sessions=[_session_payload(session) for session in snapshot.sessions],
        lock=_lock_payload(lock_info, lock_path),
        progress_journal=progress_journal,
        review_resume=review_resume,
    )


def _print_text_status(payload: dict[str, Any]) -> None:
    print(f"Plan: {payload['plan_dir']}")
    print(f"Plan hash: {payload['plan_hash']}")
    print(f"Content SHA-256: {payload['content_sha256']}")
    print(f"State dir: {payload['state_dir']}")
    print(f"State file: {payload['state_path']}")
    print(f"Events: {payload['events_path']}")

    lock = payload["lock"]
    if not lock["present"]:
        print("Lock: none")
    elif lock["active"]:
        print(f"Lock: active pid {lock['pid']} ({lock['path']})")
    else:
        print(f"Lock: stale ({lock['reason']}) ({lock['path']})")

    runner_state = payload.get("runner_state", {})
    if runner_state.get("present") and runner_state.get("error"):
        print(f"Run state: unreadable ({runner_state['error']})")
    elif runner_state.get("present"):
        run_id = runner_state.get("run_id") or "unknown"
        run_started_at = runner_state.get("run_started_at") or "unknown"
        print(f"Run state: run {run_id} started {run_started_at}")
        if runner_state.get("stale_unfinished"):
            stale_reason = runner_state.get("stale_reason") or "stale unfinished"
            print(f"Run state: stale/orphaned ({stale_reason})")

    progress_journal = payload.get("progress_journal")
    if isinstance(progress_journal, dict):
        if not progress_journal.get("present"):
            print("Progress journal: not initialized")
        elif progress_journal.get("error"):
            print(f"Progress journal: unreadable ({progress_journal['error']})")
        else:
            entry_count = progress_journal.get("entry_count") or 0
            last_appended = progress_journal.get("last_appended")
            entry_word = "entry" if entry_count == 1 else "entries"
            if last_appended:
                print(
                    f"Progress journal: {entry_count} {entry_word}, "
                    f"last appended {last_appended}"
                )
            else:
                print(f"Progress journal: {entry_count} {entry_word}")

    active_turn = payload.get("active_turn")
    if isinstance(active_turn, dict):
        print(
            "Active turn: "
            f"session {_session_label_from_payload(active_turn)} "
            f"turn {active_turn.get('turn_index')} "
            f"elapsed {format_duration_seconds(active_turn.get('elapsed_seconds'))} "
            f"idle {format_duration_seconds(active_turn.get('idle_seconds'))}"
        )
        print(f"  Last activity: {active_turn.get('last_activity_at') or 'none'}")
        action_required = active_turn.get("action_required")
        if isinstance(action_required, dict):
            print(
                "  Action required: "
                f"{action_required.get('reason')} "
                f"({action_required.get('source')})"
            )
        print(f"  Stdout: {active_turn.get('stdout_path')}")
        print(f"  Stderr: {active_turn.get('stderr_path')}")
        print(f"  Last message: {active_turn.get('last_message_path')}")
        print(f"  Result: {active_turn.get('result_path')}")

    active_verification = payload.get("active_verification")
    if isinstance(active_verification, dict):
        check_count = active_verification.get("check_count")
        check_index = active_verification.get("current_check_index")
        if check_index is None:
            check_position = (
                f"-/{check_count}" if check_count is not None else "-"
            )
        else:
            check_position = (
                f"{int(check_index) + 1}/{check_count}"
                if check_count is not None
                else str(int(check_index) + 1)
            )
        check_name = active_verification.get("current_check_name") or "pending"
        print(
            "Active verification: "
            f"session {_session_label_from_payload(active_verification)} "
            f"turn {active_verification.get('turn_index')} "
            f"check {check_position} {check_name} "
            f"elapsed {format_duration_seconds(active_verification.get('elapsed_seconds'))} "
            f"check elapsed {format_duration_seconds(active_verification.get('check_elapsed_seconds'))}"
        )
        check_kind = active_verification.get("current_check_kind")
        if check_kind:
            print(f"  Check kind: {check_kind}")
        heartbeat_count = active_verification.get("heartbeat_count")
        if heartbeat_count is not None:
            print(f"  Heartbeats: {heartbeat_count}")

    print("Sessions:")
    for session in payload["sessions"]:
        session_id = session.get("id", session.get("number", "unknown"))
        print(f"  {session_id}: {session['status']} {Path(session['path']).name}")

    if payload["next_pending"] is None:
        print("Next pending: none")
    else:
        next_pending = payload.get("next_pending_id") or payload["next_pending"]
        print(f"Next pending: session {next_pending}")


def status_from_args(args: argparse.Namespace) -> int:
    """Show parsed plan and runner status from CLI arguments.

    ``args.plan_dir`` (advertised as ``PLAN_DIR_OR_ID``) may be either an
    existing plan directory or a bare compact plan label printed after ``plan``
    by ``asl sessions``. Labels are resolved through the state corpus before
    inspection; direct paths always take precedence and never trigger a lookup.
    """
    try:
        resolved_plan_dir = resolve_plan_reference(
            args.plan_dir, state_root(args.state_dir)
        )
        snapshot = plans.inspect_plan(resolved_plan_dir)
        payload = status_payload(snapshot, args.state_dir)
    except AmbiguousPlanError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "status",
                    reason="ambiguous-plan",
                    error=str(error),
                    paused=False,
                    details={
                        "state_root": str(error.state_root),
                        "candidates": [
                            str(candidate) for candidate in error.candidates
                        ],
                    },
                    plan_dir=str(args.plan_dir),
                )
            )
        else:
            print(
                f"agent-session-loop status: {error}; "
                "pass a full plan directory path to disambiguate",
                file=sys.stderr,
            )
        return 1
    except (plans.PlanError, StateError) as error:
        if args.json:
            reason = (
                "malformed-plan"
                if isinstance(error, plans.PlanError)
                else "state-error"
            )
            responses.print_json(
                responses.error_payload(
                    "status",
                    reason=reason,
                    error=str(error),
                    paused=False,
                    plan_dir=str(args.plan_dir),
                )
            )
        else:
            print(f"agent-session-loop status: {error}", file=sys.stderr)
        return 1

    if args.json:
        responses.print_json(payload)
    else:
        _print_text_status(payload)
    return 0


def _unlock_payload(
    result: UnlockResult,
    *,
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    canonical_plan_dir = plans.canonicalize_path(plan_dir)
    return responses.success_payload(
        "unlock",
        reason="ok",
        plan_dir=str(canonical_plan_dir),
        state_dir=str(plan_state_dir(canonical_plan_dir, state_dir)),
        lock_path=str(result.path),
        had_lock=result.had_lock,
        removed=result.removed,
        active=result.was_active,
        stale=result.was_stale,
        message=result.message,
    )


def _unlock_error_reason(error: Exception) -> str:
    if isinstance(error, plans.PlanError):
        return "malformed-plan"
    if isinstance(error, OSError):
        return "os-error"
    if "active lock" in str(error).lower():
        return "active-lock"
    return "state-error"


def _unlock_error_details(args: argparse.Namespace) -> dict[str, Any]:
    try:
        canonical_plan_dir = plans.canonicalize_path(args.plan_dir)
        return {
            "plan_dir": str(canonical_plan_dir),
            "lock_path": str(lock_path_for_plan(canonical_plan_dir, args.state_dir)),
        }
    except (plans.PlanError, OSError):
        return {"plan_dir": str(args.plan_dir)}


def unlock_from_args(args: argparse.Namespace) -> int:
    """Remove a plan lock from CLI arguments."""
    try:
        result = unlock_plan(args.plan_dir, args.state_dir, force=args.force)
    except (plans.PlanError, StateError, OSError) as error:
        if args.json:
            details = _unlock_error_details(args)
            responses.print_json(
                responses.error_payload(
                    "unlock",
                    reason=_unlock_error_reason(error),
                    error=str(error),
                    paused=False,
                    details=details,
                    plan_dir=details.get("plan_dir", str(args.plan_dir)),
                    lock_path=details.get("lock_path"),
                )
            )
        else:
            print(f"agent-session-loop unlock: {error}", file=sys.stderr)
        return 1

    if args.json:
        responses.print_json(
            _unlock_payload(result, plan_dir=args.plan_dir, state_dir=args.state_dir)
        )
    else:
        print(result.message)
    return 0
