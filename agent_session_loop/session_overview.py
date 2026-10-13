"""Overview data contracts for the `asl sessions` command.

Defines the typed payload surface for running activity rows, recent
turn rows, stale lock records, skipped artifact records, and aggregate
counts. Kept independent from CLI rendering so the recent and running
scanner sessions and the CLI/rendering session can share one shape.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    plans_errors,
    plans_files,
    plans_metadata,
    plans_registry,
    responses,
    state,
    state_locks,
    state_payloads,
    workspaces,
)
from .formatting import format_duration_seconds

__all__ = [
    "RUNNING_KIND",
    "RECENT_KIND",
    "STATE_JSON_NAME",
    "LOCK_JSON_NAME",
    "RUNS_DIR_NAME",
    "RESULT_JSON_NAME",
    "TURN_PREFIX",
    "DEFAULT_RECENT_LIMIT",
    "RunningRow",
    "RecentRow",
    "StaleLock",
    "SkippedArtifact",
    "OverviewCounts",
    "format_duration_seconds",
    "parse_iso_timestamp",
    "format_local_time",
    "plan_basename_label",
    "plan_label",
    "running_sort_key",
    "recent_sort_key",
    "scan_recent_rows",
    "scan_running_rows",
    "build_sessions_payload",
    "sessions_from_args",
]


DEFAULT_RECENT_LIMIT = 20


RUNNING_KIND = "running"
RECENT_KIND = "recent"

STATE_JSON_NAME = "state.json"
LOCK_JSON_NAME = "lock.json"
RUNS_DIR_NAME = "runs"
RESULT_JSON_NAME = "result.json"
TURN_PREFIX = "turn-"


@dataclass(frozen=True)
class RunningRow:
    """Currently running ASL-managed session turn."""

    kind: str
    plan_dir: Path
    state_dir: Path
    run_id: str
    harness: str
    session_id: str
    turn_index: int | None
    pid: int | None
    started_at: str
    elapsed_seconds: float | None
    idle_seconds: float | None
    last_activity_at: str | None
    lock_active: bool
    lock_stale: bool
    check_index: int | None = None
    check_count: int | None = None
    check_name: str | None = None
    check_kind: str | None = None
    cwd: str | None = None
    workspace_root: str | None = None
    registry_digest: str | None = None
    repositories: tuple[dict[str, Any], ...] = ()
    # Claimed by the lock itself, so it is readable before the run has written
    # any state; empty for a version 1 lock, which declares nothing.
    repository_roots: tuple[str, ...] = ()
    hostname: str | None = None


@dataclass(frozen=True)
class RecentRow:
    """Recently completed ASL-managed session turn."""

    plan_dir: Path
    state_dir: Path
    run_id: str
    harness: str
    session_id: str
    turn_index: int | None
    status: str
    finished_at: str
    duration_seconds: float | None
    artifact_dir: Path
    session_kind: str | None = None
    cwd: str | None = None
    workspace_root: str | None = None
    registry_digest: str | None = None
    repositories: tuple[dict[str, Any], ...] = ()


def _workspace_fields(
    plan_dir: Path, payload: dict[str, Any]
) -> tuple[str | None, str | None, str | None, tuple[dict[str, Any], ...]]:
    root = _coerce_nonempty_str(payload.get("workspace_root"))
    digest = _coerce_nonempty_str(payload.get("registry_digest"))
    raw_rows = payload.get("repositories")
    rows = (
        tuple(dict(row) for row in raw_rows if isinstance(row, dict))
        if isinstance(raw_rows, list)
        else ()
    )
    cwd = _coerce_nonempty_str(payload.get("cwd"))
    if root is not None and rows:
        return cwd or root, root, digest, rows
    try:
        registry = plans_registry.read_repository_registry(plan_dir)
        metadata = plans_metadata.load_plan_metadata(plan_dir)
    except plans_errors.PlanError, OSError:
        return cwd, None, None, ()
    if registry is None or not isinstance(
        metadata, plans_metadata.WorkspacePlanMetadata
    ):
        return cwd, None, None, ()
    workspace_root = str(plans_files.canonicalize_path(metadata.workspace_root))
    repositories = tuple(
        {
            "alias": repository.alias,
            "path": repository.path,
            "origin": repository.origin,
            "resolved_root": str(
                plans_files.canonicalize_path(Path(workspace_root) / repository.path)
            ),
        }
        for repository in registry.repositories
    )
    return workspace_root, workspace_root, metadata.registry_digest, repositories


@dataclass(frozen=True)
class StaleLock:
    """Stale lock encountered while scanning state directories."""

    plan_dir: str
    lock_path: Path
    pid: int | None
    reason: str
    created_at: str
    # A lock recorded on another host is stale only in the sense that nothing
    # here can judge its owner, so it still holds whatever it claims.
    foreign_host: bool = False
    repository_roots: tuple[str, ...] = ()
    hostname: str | None = None


@dataclass(frozen=True)
class SkippedArtifact:
    """Artifact or state file skipped during overview scanning."""

    path: Path
    reason: str


@dataclass(frozen=True)
class OverviewCounts:
    """Aggregate counts surfaced alongside overview rows."""

    running: int = 0
    recent: int = 0
    stale_locks: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "running": self.running,
            "recent": self.recent,
            "stale_locks": self.stale_locks,
            "skipped": self.skipped,
        }


def parse_iso_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, returning None when unparseable.

    Accepts the trailing-Z form used by JSON state payloads and returns a
    timezone-aware datetime. Naive datetimes are coerced to UTC so callers
    can compare or sort safely.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
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


def format_local_time(value: Any) -> str:
    """Format a timestamp value as local time for compact text output."""
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return "unknown"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def plan_basename_label(plan_dir: str | Path) -> str:
    """Return the basename of a plan directory for compact text output."""
    name = Path(str(plan_dir)).name
    return name or str(plan_dir)


def plan_label(plan_dir: str | Path, *, verbose: bool = False) -> str:
    """Return a plan label: full canonical path when verbose, basename otherwise."""
    if verbose:
        return str(plans_files.canonicalize_path(plan_dir))
    return plan_basename_label(plan_dir)


def running_sort_key(row: RunningRow) -> tuple[float, str, str, str]:
    """Sort running rows newest first with stable tie-breakers."""
    started = parse_iso_timestamp(row.started_at)
    started_key = -started.timestamp() if started is not None else float("inf")
    return (started_key, str(row.plan_dir), row.run_id, row.session_id)


def recent_sort_key(row: RecentRow) -> tuple[float, str, str, str]:
    """Sort recent rows by most recent finish first with stable tie-breakers."""
    finished = parse_iso_timestamp(row.finished_at)
    finished_key = -finished.timestamp() if finished is not None else float("inf")
    return (finished_key, str(row.plan_dir), row.run_id, row.session_id)


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from `path`; raise on malformed JSON or non-object."""
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return parsed


def _coerce_nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _read_state_dir_plan_dir_fallbacks(
    state_dir: Path,
) -> tuple[str | None, str | None]:
    """Return (state_plan_dir, lock_plan_dir) for plan-path recovery fallbacks."""
    state_plan_dir: str | None = None
    lock_plan_dir: str | None = None
    state_path = state_dir / STATE_JSON_NAME
    if state_path.is_file():
        try:
            state_payload = _read_json_object(state_path)
        except OSError, ValueError, json.JSONDecodeError:
            state_payload = None
        if state_payload is not None:
            state_plan_dir = _coerce_nonempty_str(state_payload.get("plan_dir"))
    lock_path = state_dir / LOCK_JSON_NAME
    if lock_path.is_file():
        try:
            lock_payload = _read_json_object(lock_path)
        except OSError, ValueError, json.JSONDecodeError:
            lock_payload = None
        if lock_payload is not None:
            lock_plan_dir = _coerce_nonempty_str(lock_payload.get("plan_dir"))
    return state_plan_dir, lock_plan_dir


def _resolve_recent_plan_dir(
    payload: dict[str, Any],
    *,
    state_plan_dir: str | None,
    lock_plan_dir: str | None,
) -> Path | None:
    candidates: list[str | None] = []
    after = payload.get("after")
    before = payload.get("before")
    if isinstance(after, dict):
        candidates.append(_coerce_nonempty_str(after.get("plan_dir")))
    if isinstance(before, dict):
        candidates.append(_coerce_nonempty_str(before.get("plan_dir")))
    candidates.append(state_plan_dir)
    candidates.append(lock_plan_dir)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return plans_files.canonicalize_path(candidate)
        except OSError, ValueError:
            continue
    return None


def _resolve_session_id(payload: dict[str, Any]) -> str:
    session_id = _coerce_nonempty_str(payload.get("session_id"))
    if session_id is not None:
        return session_id
    session_value = payload.get("session")
    if session_value is None:
        return ""
    if isinstance(session_value, str):
        return session_value.strip()
    return str(session_value)


def _build_recent_row(
    payload: dict[str, Any],
    *,
    state_dir: Path,
    state_plan_dir: str | None,
    lock_plan_dir: str | None,
    result_path: Path,
) -> RecentRow | None:
    plan_dir = _resolve_recent_plan_dir(
        payload,
        state_plan_dir=state_plan_dir,
        lock_plan_dir=lock_plan_dir,
    )
    if plan_dir is None:
        return None
    run_id = _coerce_nonempty_str(payload.get("run_id")) or ""
    harness = _coerce_nonempty_str(payload.get("harness")) or ""
    session_id = _resolve_session_id(payload)
    turn_index = _coerce_int(payload.get("turn_index"))
    status = _coerce_nonempty_str(payload.get("status_result")) or "unknown"
    finished_raw = _coerce_nonempty_str(payload.get("finished_at"))
    started_raw = _coerce_nonempty_str(payload.get("started_at"))
    finished_at = finished_raw or started_raw or ""
    duration_seconds = _coerce_float(payload.get("duration_seconds"))
    artifact_dir_value = _coerce_nonempty_str(payload.get("artifact_dir"))
    artifact_dir = (
        Path(artifact_dir_value) if artifact_dir_value else result_path.parent
    )
    session_kind = _coerce_nonempty_str(payload.get("session_kind"))
    cwd, workspace_root, registry_digest, repositories = _workspace_fields(
        plan_dir, payload
    )
    return RecentRow(
        plan_dir=plan_dir,
        state_dir=state_dir,
        run_id=run_id,
        harness=harness,
        session_id=session_id,
        turn_index=turn_index,
        status=status,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        artifact_dir=artifact_dir,
        session_kind=session_kind,
        cwd=cwd,
        workspace_root=workspace_root,
        registry_digest=registry_digest,
        repositories=repositories,
    )


def _iter_state_plan_dirs(state_root: Path) -> Iterator[Path]:
    if not state_root.exists():
        return
    if not state_root.is_dir():
        raise NotADirectoryError(f"state root is not a directory: {state_root}")
    try:
        children = list(state_root.iterdir())
    except OSError as error:
        raise OSError(f"could not scan state root {state_root}: {error}") from error
    for child in children:
        try:
            is_state_dir = child.is_dir()
        except OSError:
            continue
        if is_state_dir:
            yield child


def _iter_run_dirs(state_dir: Path) -> Iterator[Path]:
    runs_dir = state_dir / RUNS_DIR_NAME
    if not runs_dir.is_dir():
        return
    try:
        children = list(runs_dir.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_dir():
            yield child


def _iter_turn_result_paths(run_dir: Path) -> Iterator[Path]:
    try:
        children = list(run_dir.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir():
            continue
        if not child.name.startswith(TURN_PREFIX):
            continue
        result_path = child / RESULT_JSON_NAME
        if result_path.is_file():
            yield result_path


def scan_recent_rows(
    state_root_path: str | Path | None = None,
    *,
    limit: int | None = None,
) -> tuple[tuple[RecentRow, ...], tuple[SkippedArtifact, ...]]:
    """Scan immutable turn `result.json` artifacts under the ASL state root.

    Walks every per-plan state directory under ``state_root_path`` (or the
    default ASL state root when ``None``), discovers each
    ``runs/<run_id>/turn-*/result.json`` artifact, and builds one
    :class:`RecentRow` per turn. Includes successful, failed, timed-out, and
    paused turns; status is read straight from the artifact's
    ``status_result`` field. Each row's primary timestamp is the artifact's
    ``finished_at`` value, falling back to ``started_at`` when the turn
    never finished. Plan paths are recovered from ``after.plan_dir``,
    ``before.plan_dir``, the per-plan ``state.json``, and finally the
    per-plan ``lock.json`` in that order. Rows are deduplicated by their
    ``result_path`` so repeated runs for the same plan are not collapsed.

    Returns a tuple ``(rows, skipped)`` sorted newest finish first; malformed
    or unreadable artifacts are reported as :class:`SkippedArtifact` records
    instead of aborting the whole scan. ``limit`` truncates ``rows`` after
    sorting; it does not affect ``skipped``.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")

    state_root = state.state_root(state_root_path)
    rows_by_result_path: dict[str, RecentRow] = {}
    skipped: list[SkippedArtifact] = []
    for state_dir in _iter_state_plan_dirs(state_root):
        state_plan_dir, lock_plan_dir = _read_state_dir_plan_dir_fallbacks(state_dir)
        for run_dir in _iter_run_dirs(state_dir):
            for result_path in _iter_turn_result_paths(run_dir):
                key = str(result_path)
                if key in rows_by_result_path:
                    continue
                try:
                    payload = _read_json_object(result_path)
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    skipped.append(SkippedArtifact(path=result_path, reason=str(error)))
                    continue
                row = _build_recent_row(
                    payload,
                    state_dir=state_dir,
                    state_plan_dir=state_plan_dir,
                    lock_plan_dir=lock_plan_dir,
                    result_path=result_path,
                )
                if row is None:
                    skipped.append(
                        SkippedArtifact(
                            path=result_path,
                            reason="unable to resolve plan directory",
                        )
                    )
                    continue
                rows_by_result_path[key] = row
    rows = sorted(rows_by_result_path.values(), key=recent_sort_key)
    if limit is not None:
        rows = rows[:limit]
    return tuple(rows), tuple(skipped)


def _coerce_pid(value: Any) -> int | None:
    pid = _coerce_int(value)
    if pid is None or pid <= 0:
        return None
    return pid


def _running_lock_metadata(
    raw_lock: dict[str, Any],
) -> tuple[str | None, int | None, str]:
    """Extract (plan_dir, pid, created_at) from a raw lock payload."""
    plan_dir_value = _coerce_nonempty_str(raw_lock.get("plan_dir"))
    pid_value = _coerce_pid(raw_lock.get("pid"))
    created_at = _coerce_nonempty_str(raw_lock.get("created_at")) or ""
    return plan_dir_value, pid_value, created_at


def _build_running_row(
    *,
    lock_info: state.LockInfo,
    state_dir: Path,
    plan_dir: Path,
    state_payload: dict[str, Any],
) -> RunningRow:
    cwd, workspace_root, registry_digest, repositories = _workspace_fields(
        plan_dir, state_payload
    )
    active_verification = state_payload.get("active_verification")
    active_turn = state_payload.get("active_turn")
    check_index: int | None = None
    check_count: int | None = None
    check_name: str | None = None
    check_kind: str | None = None
    if isinstance(active_verification, dict):
        kind = "verification"
        run_id = (
            _coerce_nonempty_str(active_verification.get("run_id"))
            or _coerce_nonempty_str(state_payload.get("run_id"))
            or ""
        )
        harness = _coerce_nonempty_str(state_payload.get("harness")) or ""
        session_id = _resolve_session_id(active_verification)
        turn_index = _coerce_int(active_verification.get("turn_index"))
        started_at = (
            _coerce_nonempty_str(active_verification.get("started_at"))
            or lock_info.created_at
        )
        elapsed_seconds = _coerce_float(active_verification.get("elapsed_seconds"))
        idle_seconds = None
        last_activity_at = _coerce_nonempty_str(
            active_verification.get("current_check_started_at")
        ) or _coerce_nonempty_str(active_verification.get("started_at"))
        check_index = _coerce_int(active_verification.get("current_check_index"))
        check_count = _coerce_int(active_verification.get("check_count"))
        check_name = _coerce_nonempty_str(active_verification.get("current_check_name"))
        check_kind = _coerce_nonempty_str(active_verification.get("current_check_kind"))
    elif isinstance(active_turn, dict):
        kind = "turn"
        run_id = (
            _coerce_nonempty_str(active_turn.get("run_id"))
            or _coerce_nonempty_str(state_payload.get("run_id"))
            or ""
        )
        harness = (
            _coerce_nonempty_str(active_turn.get("harness"))
            or _coerce_nonempty_str(state_payload.get("harness"))
            or ""
        )
        session_id = _resolve_session_id(active_turn)
        turn_index = _coerce_int(active_turn.get("turn_index"))
        started_at = (
            _coerce_nonempty_str(active_turn.get("started_at")) or lock_info.created_at
        )
        elapsed_seconds = _coerce_float(active_turn.get("elapsed_seconds"))
        idle_seconds = _coerce_float(active_turn.get("idle_seconds"))
        last_activity_at = _coerce_nonempty_str(active_turn.get("last_activity_at"))
    else:
        kind = "loop"
        run_id = _coerce_nonempty_str(state_payload.get("run_id")) or ""
        harness = _coerce_nonempty_str(state_payload.get("harness")) or ""
        session_id = ""
        turn_index = None
        started_at = lock_info.created_at
        elapsed_seconds = None
        idle_seconds = None
        last_activity_at = None
    return RunningRow(
        kind=kind,
        plan_dir=plan_dir,
        state_dir=state_dir,
        run_id=run_id,
        harness=harness,
        session_id=session_id,
        turn_index=turn_index,
        pid=lock_info.pid,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        idle_seconds=idle_seconds,
        last_activity_at=last_activity_at,
        lock_active=True,
        lock_stale=False,
        check_index=check_index,
        check_count=check_count,
        check_name=check_name,
        check_kind=check_kind,
        cwd=cwd,
        workspace_root=workspace_root,
        registry_digest=registry_digest,
        repositories=repositories,
        repository_roots=lock_info.repository_roots,
        hostname=lock_info.owner_hostname,
    )


def scan_running_rows(
    state_root_path: str | Path | None = None,
    *,
    reclaim: bool = True,
) -> tuple[
    tuple[RunningRow, ...],
    tuple[StaleLock, ...],
    tuple[SkippedArtifact, ...],
]:
    """Scan active ``lock.json`` files for currently running ASL session turns.

    Walks every per-plan state directory under ``state_root_path`` (or the
    default ASL state root when ``None``), reads ``lock.json`` from each one,
    and classifies it. Lock liveness reuses ``state.is_pid_running``
    semantics through :func:`state._read_lock_path`: positive PIDs whose
    process is still reachable produce a :class:`RunningRow`; anything else
    (dead PID, missing PID, mismatched plan directory, invalid JSON shape)
    is captured as a :class:`StaleLock` and excluded from the running rows.
    Locks whose file cannot be read or parsed are captured as
    :class:`SkippedArtifact` records so the scan can continue.

    When a running lock's per-plan ``state.json`` includes an ``active_turn``
    payload, the running row has ``kind="turn"`` and inherits session, turn,
    started_at, elapsed_seconds, idle_seconds, and last_activity_at from the
    active turn. When the lock is active but no ``active_turn`` exists yet,
    the running row has ``kind="loop"`` with session/turn left unknown so the
    CLI can still surface a "runner is alive between turns" signal; run_id
    and harness fall back to the top-level ``state.json`` fields when present.

    Returns ``(rows, stale_locks, skipped)``. ``rows`` is sorted newest-start
    first via :func:`running_sort_key`; the stale and skipped tuples preserve
    discovery order so callers can correlate them with directory iteration.

    Reading a lock reclaims it when this host has proven its owner dead. Pass
    ``reclaim=False`` to classify without that side effect, which is what a
    caller that has promised to write nothing needs.
    """
    state_root = state.state_root(state_root_path)
    rows: list[RunningRow] = []
    stale_locks: list[StaleLock] = []
    skipped: list[SkippedArtifact] = []
    for state_dir in _iter_state_plan_dirs(state_root):
        lock_path = state_dir / LOCK_JSON_NAME
        if not lock_path.is_file():
            continue
        try:
            raw_lock = _read_json_object(lock_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            skipped.append(
                SkippedArtifact(
                    path=lock_path,
                    reason=f"lock unreadable: {error}",
                )
            )
            continue
        plan_dir_value, pid_value, created_at_value = _running_lock_metadata(raw_lock)
        if plan_dir_value is None:
            stale_locks.append(
                StaleLock(
                    plan_dir="",
                    lock_path=lock_path,
                    pid=pid_value,
                    reason="lock has no plan_dir",
                    created_at=created_at_value,
                )
            )
            continue
        try:
            canonical_plan_dir = plans_files.canonicalize_path(plan_dir_value)
        except (OSError, ValueError) as error:
            stale_locks.append(
                StaleLock(
                    plan_dir=plan_dir_value,
                    lock_path=lock_path,
                    pid=pid_value,
                    reason=f"could not canonicalize plan_dir: {error}",
                    created_at=created_at_value,
                )
            )
            continue
        expected_state_dir_name = plans_files.path_sha256(canonical_plan_dir)
        if state_dir.name != expected_state_dir_name:
            stale_locks.append(
                StaleLock(
                    plan_dir=str(canonical_plan_dir),
                    lock_path=lock_path,
                    pid=pid_value,
                    reason=(
                        "lock state directory does not match plan hash: "
                        f"expected {expected_state_dir_name}, got {state_dir.name}"
                    ),
                    created_at=created_at_value,
                )
            )
            continue
        lock_info = state_locks._read_lock_path(
            lock_path, canonical_plan_dir, reclaim=reclaim
        )
        if lock_info is None:
            continue
        if lock_info.stale:
            stale_locks.append(
                StaleLock(
                    plan_dir=lock_info.plan_dir or str(canonical_plan_dir),
                    lock_path=lock_info.path,
                    pid=lock_info.pid,
                    reason=lock_info.reason,
                    created_at=lock_info.created_at,
                    foreign_host=lock_info.foreign_host,
                    repository_roots=lock_info.repository_roots,
                    hostname=lock_info.owner_hostname,
                )
            )
            continue
        state_payload = state_payloads._runner_state_payload(
            state_dir / STATE_JSON_NAME,
            lock_info=lock_info,
        )
        rows.append(
            _build_running_row(
                lock_info=lock_info,
                state_dir=state_dir,
                plan_dir=canonical_plan_dir,
                state_payload=state_payload,
            )
        )
    rows.sort(key=running_sort_key)
    return tuple(rows), tuple(stale_locks), tuple(skipped)


def _running_row_payload(row: RunningRow) -> dict[str, Any]:
    return {
        "kind": row.kind,
        "plan_dir": str(row.plan_dir),
        "state_dir": str(row.state_dir),
        "run_id": row.run_id,
        "harness": row.harness,
        "session_id": row.session_id,
        "turn_index": row.turn_index,
        "pid": row.pid,
        "started_at": row.started_at,
        "elapsed_seconds": row.elapsed_seconds,
        "idle_seconds": row.idle_seconds,
        "last_activity_at": row.last_activity_at,
        "lock_active": row.lock_active,
        "lock_stale": row.lock_stale,
        "check_index": row.check_index,
        "check_count": row.check_count,
        "check_name": row.check_name,
        "check_kind": row.check_kind,
        "cwd": row.cwd,
        "workspace_root": row.workspace_root,
        "registry_digest": row.registry_digest,
        "repositories": [dict(repository) for repository in row.repositories],
    }


def _recent_row_payload(row: RecentRow) -> dict[str, Any]:
    return {
        "plan_dir": str(row.plan_dir),
        "state_dir": str(row.state_dir),
        "run_id": row.run_id,
        "harness": row.harness,
        "session_id": row.session_id,
        "turn_index": row.turn_index,
        "status": row.status,
        "finished_at": row.finished_at,
        "duration_seconds": row.duration_seconds,
        "artifact_dir": str(row.artifact_dir),
        "session_kind": row.session_kind,
        "cwd": row.cwd,
        "workspace_root": row.workspace_root,
        "registry_digest": row.registry_digest,
        "repositories": [dict(repository) for repository in row.repositories],
    }


def _stale_lock_payload(lock: StaleLock) -> dict[str, Any]:
    return {
        "plan_dir": lock.plan_dir,
        "lock_path": str(lock.lock_path),
        "pid": lock.pid,
        "reason": lock.reason,
        "created_at": lock.created_at,
    }


def _skipped_payload(skipped: SkippedArtifact) -> dict[str, Any]:
    return {
        "path": str(skipped.path),
        "reason": skipped.reason,
    }


def build_sessions_payload(
    *,
    state_root_path: Path,
    show_running: bool,
    show_recent: bool,
    limit: int,
    running_rows: tuple[RunningRow, ...],
    recent_rows: tuple[RecentRow, ...],
    stale_locks: tuple[StaleLock, ...],
    skipped: tuple[SkippedArtifact, ...],
) -> dict[str, Any]:
    """Build the canonical `asl sessions` JSON payload."""
    counts = OverviewCounts(
        running=len(running_rows),
        recent=len(recent_rows),
        stale_locks=len(stale_locks),
        skipped=len(skipped),
    )
    return responses.success_payload(
        "sessions",
        state_root=str(state_root_path),
        filters={"running": show_running, "recent": show_recent},
        limit=limit,
        running=[_running_row_payload(row) for row in running_rows],
        recent=[_recent_row_payload(row) for row in recent_rows],
        stale_locks=[_stale_lock_payload(lock) for lock in stale_locks],
        skipped=[_skipped_payload(item) for item in skipped],
        counts=counts.as_dict(),
    )


def _format_int(value: int | None) -> str:
    return "?" if value is None else str(value)


def _print_running_section(
    payload: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    rows = payload["running"]
    if not rows:
        print("Running sessions: none")
        return
    print(f"Running sessions ({len(rows)}):")
    for row in rows:
        plan = plan_label(row["plan_dir"], verbose=verbose)
        session_id = row["session_id"] or "?"
        turn_index = _format_int(row["turn_index"])
        harness = row["harness"] or "unknown"
        pid = _format_int(row["pid"])
        check_suffix = ""
        workspace_suffix = ""
        if row.get("workspace_root"):
            repositories = row.get("repositories") or []
            visible, omitted = workspaces.bounded_repository_rows(repositories)
            aliases = [
                str(repository.get("alias"))
                for repository in visible
                if isinstance(repository, dict) and repository.get("alias")
            ]
            alias_text = ",".join(aliases)
            if omitted:
                alias_text += f",+{omitted}"
            workspace_suffix = f"  workspace {row['workspace_root']}" + (
                f" [{alias_text}]" if alias_text else ""
            )
        if row.get("kind") == "verification":
            check_index = row.get("check_index")
            check_count = row.get("check_count")
            if check_index is None:
                position = f"-/{check_count}" if check_count is not None else "-"
            else:
                position = (
                    f"{int(check_index) + 1}/{check_count}"
                    if check_count is not None
                    else str(int(check_index) + 1)
                )
            check_name = row.get("check_name") or "pending"
            check_suffix = f"  check {position} {check_name}"
        print(
            f"  {row['kind']:<12}"
            f"  started {format_local_time(row['started_at'])}"
            f"  elapsed {format_duration_seconds(row['elapsed_seconds'])}"
            f"  idle {format_duration_seconds(row['idle_seconds'])}"
            f"  pid {pid}"
            f"  harness {harness}"
            f"  session {session_id} turn {turn_index}"
            f"{check_suffix}"
            f"{workspace_suffix}"
            f"  plan {plan}"
        )


def _print_recent_section(
    payload: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    rows = payload["recent"]
    if not rows:
        print("Recent sessions: none")
        return
    print(f"Recent sessions ({len(rows)}):")
    for row in rows:
        plan = plan_label(row["plan_dir"], verbose=verbose)
        session_id = row["session_id"] or "?"
        harness = row["harness"] or "unknown"
        run_id = row["run_id"] or "unknown"
        status = row["status"] or "unknown"
        kind = row.get("session_kind")
        kind_label = f" ({kind})" if kind else ""
        workspace_suffix = (
            f"  workspace {row['workspace_root']}" if row.get("workspace_root") else ""
        )
        print(
            f"  finished {format_local_time(row['finished_at'])}"
            f"  dur {format_duration_seconds(row['duration_seconds'])}"
            f"  {status}"
            f"  harness {harness}"
            f"  session {session_id}{kind_label}"
            f"  run {run_id}"
            f"{workspace_suffix}"
            f"  plan {plan}"
        )


def _print_footer_warnings(
    payload: dict[str, Any],
    *,
    verbose: bool,
) -> None:
    stale_locks = payload["stale_locks"]
    if stale_locks:
        print(f"Stale locks ({len(stale_locks)}):")
        for lock in stale_locks:
            plan = (
                plan_label(lock["plan_dir"], verbose=verbose)
                if lock["plan_dir"]
                else "unknown"
            )
            pid = _format_int(lock["pid"])
            print(
                f"  plan {plan}  pid {pid}  reason {lock['reason']}"
                f"  lock {lock['lock_path']}"
            )
    skipped = payload["skipped"]
    if skipped:
        print(f"Skipped artifacts ({len(skipped)}):")
        for item in skipped:
            print(f"  {item['path']}  reason {item['reason']}")


def _print_text_sessions(payload: dict[str, Any], *, verbose: bool) -> None:
    filters = payload["filters"]
    print(f"State root: {payload['state_root']}")
    if filters["running"]:
        _print_running_section(payload, verbose=verbose)
    if filters["recent"]:
        _print_recent_section(payload, verbose=verbose)
    _print_footer_warnings(payload, verbose=verbose)


def sessions_from_args(args: argparse.Namespace) -> int:
    """Render `asl sessions` output from CLI arguments."""
    if args.running and args.recent:
        show_running = True
        show_recent = True
    elif args.running:
        show_running = True
        show_recent = False
    elif args.recent:
        show_running = False
        show_recent = True
    else:
        show_running = True
        show_recent = True

    limit = args.limit if args.limit is not None else DEFAULT_RECENT_LIMIT
    if limit < 0:
        message = "--limit must be a non-negative integer"
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "sessions",
                    reason="input-error",
                    error=message,
                    paused=False,
                )
            )
        else:
            print(f"agent-session-loop sessions: {message}", file=sys.stderr)
        return 1

    try:
        resolved_state_root = state.state_root(args.state_dir)
    except OSError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "sessions",
                    reason="state-error",
                    error=str(error),
                    paused=False,
                    state_dir=str(args.state_dir) if args.state_dir else None,
                )
            )
        else:
            print(f"agent-session-loop sessions: {error}", file=sys.stderr)
        return 1

    running_rows: tuple[RunningRow, ...] = ()
    stale_locks: tuple[StaleLock, ...] = ()
    recent_rows: tuple[RecentRow, ...] = ()
    skipped_combined: list[SkippedArtifact] = []

    try:
        if show_running:
            running_rows, stale_locks, running_skipped = scan_running_rows(
                args.state_dir
            )
            skipped_combined.extend(running_skipped)
        if show_recent:
            recent_rows, recent_skipped = scan_recent_rows(args.state_dir, limit=limit)
            skipped_combined.extend(recent_skipped)
    except OSError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    "sessions",
                    reason="state-error",
                    error=str(error),
                    paused=False,
                    state_root=str(resolved_state_root),
                )
            )
        else:
            print(f"agent-session-loop sessions: {error}", file=sys.stderr)
        return 1

    payload = build_sessions_payload(
        state_root_path=resolved_state_root,
        show_running=show_running,
        show_recent=show_recent,
        limit=limit,
        running_rows=running_rows,
        recent_rows=recent_rows,
        stale_locks=stale_locks,
        skipped=tuple(skipped_combined),
    )

    if args.json:
        responses.print_json(payload)
    else:
        _print_text_sessions(payload, verbose=args.verbose)
    return 0
