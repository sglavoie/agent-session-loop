"""Focused portion of agent_session_loop/session_overview.py."""

from .session_overview_recent import *


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


__all__ = ["argparse", "json", "sys", "Iterator", "dataclass", "UTC", "datetime", "Path", "Any", "plans_errors", "plans_files", "plans_metadata", "plans_registry", "responses", "state", "state_locks", "state_payloads", "workspaces", "format_duration_seconds", "DEFAULT_RECENT_LIMIT", "RUNNING_KIND", "RECENT_KIND", "STATE_JSON_NAME", "LOCK_JSON_NAME", "RUNS_DIR_NAME", "RESULT_JSON_NAME", "TURN_PREFIX", "RunningRow", "RecentRow", "_coerce_nonempty_str", "_workspace_fields", "StaleLock", "SkippedArtifact", "OverviewCounts", "parse_iso_timestamp", "format_local_time", "plan_basename_label", "plan_label", "running_sort_key", "recent_sort_key", "_read_json_object", "_coerce_int", "_coerce_float", "_read_state_dir_plan_dir_fallbacks", "_resolve_recent_plan_dir", "_resolve_session_id", "_build_recent_row", "_iter_state_plan_dirs", "_iter_run_dirs", "_iter_turn_result_paths", "scan_recent_rows", "_coerce_pid", "_running_lock_metadata", "_build_running_row", "scan_running_rows"]  # fmt: skip
