"""Read-only compact status projections for the live run terminal."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import clock, plans_models, plans_payloads
from .formatting import (
    format_duration_seconds,
)
from .run_reporting_output import _local_timestamp


def _format_local_datetime(value: Any) -> str:
    parsed = clock.parse_datetime(value)
    if parsed is None:
        return "none"
    return _local_timestamp(parsed)


def _compact_value(value: Any, *, fallback: str = "none") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _compact_status_counts(
    snapshot: plans_models.PlanSnapshot | None,
    fallback: dict[str, Any] | None,
) -> dict[str, int] | None:
    if snapshot is not None:
        return plans_payloads.status_counts(snapshot)
    if isinstance(fallback, dict):
        counts = fallback.get("counts")
        if isinstance(counts, dict):
            return {
                "pending": int(counts.get("pending", 0)),
                "complete": int(counts.get("complete", 0)),
                "empty": int(counts.get("empty", 0)),
                "unknown": int(counts.get("unknown", 0)),
            }
    return None


def _compact_next_pending_id(
    snapshot: plans_models.PlanSnapshot | None,
    fallback: dict[str, Any] | None,
) -> str | None:
    if snapshot is not None:
        next_pending = snapshot.next_pending
        return next_pending.id if next_pending is not None else None
    if isinstance(fallback, dict):
        value = fallback.get("next_pending_id")
        if value is None:
            value = fallback.get("next_pending")
        if value is not None:
            return str(value)
    return None


def _compact_artifact_source(
    payload: dict[str, Any],
    active_turn: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if active_turn is not None:
        return active_turn
    turns = payload.get("turns")
    if isinstance(turns, list):
        for turn in reversed(turns):
            if isinstance(turn, dict):
                return turn
    return None


def _compact_total_duration_seconds(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> float | None:
    started_at = clock.parse_datetime(payload.get("run_started_at"))
    if started_at is None:
        return None
    finished_at = clock.parse_datetime(payload.get("run_finished_at"))
    if finished_at is None:
        finished_at = clock.utc_now() if now is None else now
    return clock.duration_seconds(started_at, finished_at)


def _compact_status_lines(
    *,
    payload: dict[str, Any],
    control: dict[str, Any],
    snapshot: plans_models.PlanSnapshot | None,
    plan_error: str | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Build a short live status snapshot without mutating runner state."""

    active_turn = payload.get("active_turn")
    if not isinstance(active_turn, dict):
        active_turn = None

    active_verification = payload.get("active_verification")
    if not isinstance(active_verification, dict):
        active_verification = None

    fallback_snapshot = payload.get("final_snapshot") or payload.get("initial_snapshot")
    if not isinstance(fallback_snapshot, dict):
        fallback_snapshot = None
    counts = _compact_status_counts(snapshot, fallback_snapshot)
    total_sessions = sum(counts.values()) if counts is not None else None
    next_pending_id = _compact_next_pending_id(snapshot, fallback_snapshot)
    turns = payload.get("turns")
    turn_count = len(turns) if isinstance(turns, list) else 0
    next_turn_index = turn_count + 1
    total_duration_seconds = _compact_total_duration_seconds(payload, now=now)

    if counts is None:
        counts_text = "sessions unavailable"
    else:
        counts_text = (
            f"sessions {counts['complete']}/{total_sessions} complete, "
            f"{counts['pending']} pending"
        )

    if active_verification is not None:
        check_index = active_verification.get("current_check_index")
        check_count_value = active_verification.get("check_count")
        if check_index is None:
            check_position = (
                f"-/{check_count_value}" if check_count_value is not None else "-"
            )
        else:
            check_position = (
                f"{int(check_index) + 1}/{check_count_value}"
                if check_count_value is not None
                else str(int(check_index) + 1)
            )
        check_name = active_verification.get("current_check_name") or "pending"
        current_text = (
            f"status: verifying session "
            f"{_format_session_label(active_verification)} "
            f"turn {active_verification.get('turn_index')} "
            f"check {check_position} {check_name}; {counts_text}"
        )
        elapsed = active_verification.get("elapsed_seconds")
        check_elapsed = active_verification.get("check_elapsed_seconds")
        timing_parts = []
        if elapsed is not None:
            timing_parts.append(f"elapsed {format_duration_seconds(elapsed)}")
        if check_elapsed is not None:
            timing_parts.append(
                f"check elapsed {format_duration_seconds(check_elapsed)}"
            )
        if total_duration_seconds is not None:
            timing_parts.append(
                f"total {format_duration_seconds(total_duration_seconds)}"
            )
        if timing_parts:
            current_text += f"; {', '.join(timing_parts)}"
    elif active_turn is not None:
        current_text = (
            f"status: active session {_format_session_label(active_turn)} "
            f"turn {active_turn.get('turn_index')}; {counts_text}"
        )
        elapsed = active_turn.get("elapsed_seconds")
        idle = active_turn.get("idle_seconds")
        timing_parts = []
        if elapsed is not None or idle is not None:
            timing_parts.extend(
                (
                    f"elapsed {format_duration_seconds(elapsed)}",
                    f"idle {format_duration_seconds(idle)}",
                )
            )
        if total_duration_seconds is not None:
            timing_parts.append(
                f"total {format_duration_seconds(total_duration_seconds)}"
            )
        if timing_parts:
            current_text += f"; {', '.join(timing_parts)}"
    elif next_pending_id is not None:
        total_suffix = "" if total_sessions is None else f"/{total_sessions}"
        state_text = "idle" if turn_count else "no active turn"
        current_text = (
            f"status: {state_text}; next session {next_pending_id}{total_suffix} "
            f"turn {next_turn_index}; {counts_text}"
        )
        if total_duration_seconds is not None:
            current_text += f"; total {format_duration_seconds(total_duration_seconds)}"
    else:
        current_text = f"status: no active turn; {counts_text}; next session none"
        if total_duration_seconds is not None:
            current_text += f"; total {format_duration_seconds(total_duration_seconds)}"

    active_harness = (
        active_turn.get("harness")
        if active_turn is not None and active_turn.get("harness") is not None
        else payload.get("harness")
    )
    model_value = (
        active_turn.get("model")
        if active_turn is not None and "model" in active_turn
        else payload.get("model")
    )
    model_source_value = (
        active_turn.get("model_source")
        if active_turn is not None and "model_source" in active_turn
        else payload.get("model_source")
    )
    effort_value = (
        active_turn.get("effort")
        if active_turn is not None and "effort" in active_turn
        else payload.get("effort")
    )
    effort_source_value = (
        active_turn.get("effort_source")
        if active_turn is not None and "effort_source" in active_turn
        else payload.get("effort_source")
    )
    model_source = _compact_value(model_source_value)
    effort_source = _compact_value(effort_source_value)
    metadata_line = (
        f"status: harness active {_compact_value(active_harness)}; "
        f"future {_compact_value(control.get('next_harness'), fallback='unchanged')}; "
        f"model {_compact_value(model_value)} ({model_source}); "
        f"effort {_compact_value(effort_value)} ({effort_source})"
    )

    stop_state = "on" if control.get("stop_after_current") else "off"
    tail_state = "on" if control.get("tail_enabled") else "off"
    control_line = (
        f"status: controls stop-after-current {stop_state}; live tail {tail_state}"
    )

    artifact_source = _compact_artifact_source(payload, active_turn)
    if artifact_source is None:
        paths_line = "status paths: none yet"
    else:
        paths_line = (
            "status paths: "
            f"stdout {_compact_value(artifact_source.get('stdout_path'))}; "
            f"stderr {_compact_value(artifact_source.get('stderr_path'))}; "
            f"last-message {_compact_value(artifact_source.get('last_message_path'))}; "
            f"result {_compact_value(artifact_source.get('result_path'))}"
        )

    lines = [current_text]
    lines.extend(_note_lines(payload, prefix="status note"))
    if plan_error is not None:
        lines.append(f"status: plan inspection error: {plan_error}")
    lines.extend([metadata_line, control_line, paths_line])
    return lines


def _format_session_label(payload: dict[str, Any]) -> str:
    session = payload.get("session_id") or payload.get("session")
    total = payload.get("total_sessions")
    if total is None:
        return str(session)
    return f"{session}/{total}"


def _note_lines(payload: dict[str, Any], *, prefix: str) -> list[str]:
    note = payload.get("note")
    if not isinstance(note, str) or note == "":
        return []
    lines = note.splitlines()
    if not lines:
        lines = [""]
    return [f"{prefix}: {line}" for line in lines]
