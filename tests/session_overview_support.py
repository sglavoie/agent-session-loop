"""Shared fixtures for test_session_overview."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_session_loop import plans_files, session_overview
from tests.support_base import write_json as _write_json

DEAD_PID = 4_242_424


def _make_plan_state_dir(
    state_root: Path,
    plan_dir: Path,
    *,
    lock: dict[str, object] | None = None,
    state_payload: dict[str, object] | None = None,
) -> Path:
    """Create a `<sha>/` state directory under `state_root` for a given plan dir.

    Writes a `state.json` containing the plan_dir so the recent scanner's
    fallback path resolution can find it. Optionally writes `lock.json`.
    """
    canonical_plan_dir = plans_files.canonicalize_path(plan_dir)
    state_dir = state_root / plans_files.path_sha256(canonical_plan_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {"plan_dir": str(canonical_plan_dir)}
    if state_payload is not None:
        payload.update(state_payload)
    _write_json(state_dir / session_overview.STATE_JSON_NAME, payload)
    if lock is not None:
        lock_with_defaults = {
            "plan_dir": str(canonical_plan_dir),
            "created_at": "2026-06-19T00:00:00+00:00",
            **lock,
        }
        _write_json(state_dir / session_overview.LOCK_JSON_NAME, lock_with_defaults)
    return state_dir


def _make_turn_artifact(
    state_dir: Path,
    *,
    run_id: str,
    turn_label: str,
    payload: dict[str, object],
) -> Path:
    """Write `runs/<run_id>/turn-<turn_label>/result.json`."""
    turn_dir = (
        state_dir / session_overview.RUNS_DIR_NAME / run_id / f"turn-{turn_label}"
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    result_path = turn_dir / session_overview.RESULT_JSON_NAME
    result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return result_path


def _active_turn_payload(
    *,
    run_id: str = "20260619T000000Z-1",
    turn_index: int = 1,
    session_id: str = "1",
    started_at: str = "2026-06-19T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session": int(session_id) if session_id.isdigit() else session_id,
        "harness": "claude",
        "started_at": started_at,
        "elapsed_seconds": 12.5,
        "idle_seconds": 0.0,
        "last_activity_at": started_at,
    }


def _active_verification_payload(
    *,
    run_id: str = "20260619T000000Z-1",
    turn_index: int = 1,
    session_id: str = "1",
    started_at: str = "2026-06-19T00:00:00+00:00",
    check_index: int | None = 0,
    check_count: int = 2,
    check_name: str = "operator-surface-tests",
    check_kind: str = "command",
    check_started_at: str | None = "2026-06-19T00:00:01+00:00",
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session": int(session_id) if session_id.isdigit() else session_id,
        "total_sessions": 5,
        "started_at": started_at,
        "elapsed_seconds": 5.5,
        "check_count": check_count,
        "check_names": [check_name, "operator-surface-compile"],
        "check_kinds": [check_kind, "command"],
        "current_check_index": check_index,
        "current_check_name": check_name,
        "current_check_kind": check_kind,
        "current_check_started_at": check_started_at,
        "check_elapsed_seconds": 1.5,
        "heartbeat_count": 0,
    }


def _turn_result_payload(
    *,
    plan_dir: Path,
    run_id: str,
    turn_index: int,
    session_id: str,
    finished_at: str,
    status_result: str = "complete",
    duration_seconds: float = 1.0,
    harness: str = "claude",
    include_plan_dir: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "turn_index": turn_index,
        "session_id": session_id,
        "session": int(session_id) if session_id.isdigit() else session_id,
        "harness": harness,
        "status_result": status_result,
        "started_at": "2026-06-19T00:00:00+00:00",
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
    }
    if include_plan_dir:
        payload["after"] = {"plan_dir": str(plan_dir)}
        payload["before"] = {"plan_dir": str(plan_dir)}
    return payload


def _build_args(
    state_dir: Path | None,
    *,
    running: bool = False,
    recent: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    json_output: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        running=running,
        recent=recent,
        limit=(session_overview.DEFAULT_RECENT_LIMIT if limit is None else limit),
        state_dir=str(state_dir) if state_dir is not None else None,
        verbose=verbose,
        json=json_output,
    )
