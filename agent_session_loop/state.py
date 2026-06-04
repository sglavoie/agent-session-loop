"""Operational state and locking boundaries.

Runner state exists to coordinate local execution, capture logs, and recover
from pauses. It must never replace the plan files as the durable completion
record.
"""

from __future__ import annotations

import argparse
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


class StateError(Exception):
    """Raised when operational state cannot be read or updated safely."""


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
        }

    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "present": True,
            "path": str(state_path),
            "error": str(error),
            "active_turn": None,
        }

    if not isinstance(raw, dict):
        return {
            "present": True,
            "path": str(state_path),
            "error": "state file did not contain a JSON object",
            "active_turn": None,
        }

    active_turn = raw.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

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
    }


def status_payload(
    snapshot: plans.PlanSnapshot,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    plan_state = plan_state_dir(snapshot.plan_dir, state_dir)
    state_path = plan_state / events.STATE_FILE_NAME
    lock_path = lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = _read_lock_path(lock_path, snapshot.plan_dir)
    next_pending = snapshot.next_pending
    runner_state = _runner_state_payload(state_path)
    return responses.success_payload(
        "status",
        plan_dir=str(snapshot.plan_dir),
        context_path=str(snapshot.context_path),
        manifest_path=str(snapshot.manifest_path),
        parallel_validation_path=(
            str(snapshot.parallel_validation_path)
            if snapshot.parallel_validation_path is not None
            else None
        ),
        content_sha256=snapshot.content_sha256,
        path_hash=snapshot.path_hash,
        plan_hash=snapshot.plan_hash,
        state_dir=str(plan_state),
        state_path=str(state_path),
        events_path=str(plan_state / events.EVENTS_FILE_NAME),
        runner_state=runner_state,
        active_turn=runner_state.get("active_turn"),
        counts=_status_counts(snapshot),
        next_pending=next_pending.number if next_pending is not None else None,
        sessions=[
            {
                "number": session.number,
                "status": session.status,
                "raw_value": session.raw_value,
                "line_number": session.line_number,
                "path": str(session.path),
            }
            for session in snapshot.sessions
        ],
        lock=_lock_payload(lock_info, lock_path),
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

    active_turn = payload.get("active_turn")
    if isinstance(active_turn, dict):
        print(
            "Active turn: "
            f"session {active_turn.get('session')} "
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

    print("Sessions:")
    for session in payload["sessions"]:
        print(f"  {session['number']}: {session['status']} {Path(session['path']).name}")

    if payload["next_pending"] is None:
        print("Next pending: none")
    else:
        print(f"Next pending: session {payload['next_pending']}")


def status_from_args(args: argparse.Namespace) -> int:
    """Show parsed plan and runner status from CLI arguments."""
    try:
        snapshot = plans.inspect_plan(args.plan_dir)
        payload = status_payload(snapshot, args.state_dir)
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
