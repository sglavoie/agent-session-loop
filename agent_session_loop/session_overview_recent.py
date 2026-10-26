"""Focused portion of agent_session_loop/session_overview.py."""

from .session_overview_types import *


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from `path`; raise on malformed JSON or non-object."""
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} did not contain a JSON object")
    return parsed


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
        if not plans_files.is_plan_state_dir_name(child.name):
            continue
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


__all__ = ["argparse", "json", "sys", "Iterator", "dataclass", "UTC", "datetime", "Path", "Any", "plans_errors", "plans_files", "plans_metadata", "plans_registry", "responses", "state", "state_locks", "state_payloads", "workspaces", "format_duration_seconds", "DEFAULT_RECENT_LIMIT", "RUNNING_KIND", "RECENT_KIND", "STATE_JSON_NAME", "LOCK_JSON_NAME", "RUNS_DIR_NAME", "RESULT_JSON_NAME", "TURN_PREFIX", "RunningRow", "RecentRow", "_coerce_nonempty_str", "_workspace_fields", "StaleLock", "SkippedArtifact", "OverviewCounts", "parse_iso_timestamp", "format_local_time", "plan_basename_label", "plan_label", "running_sort_key", "recent_sort_key", "_read_json_object", "_coerce_int", "_coerce_float", "_read_state_dir_plan_dir_fallbacks", "_resolve_recent_plan_dir", "_resolve_session_id", "_build_recent_row", "_iter_state_plan_dirs", "_iter_run_dirs", "_iter_turn_result_paths", "scan_recent_rows"]  # fmt: skip
