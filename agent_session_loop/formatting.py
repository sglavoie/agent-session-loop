"""Human-facing formatting helpers."""

from __future__ import annotations

import math
from typing import Any


def format_duration_seconds(value: Any) -> str:
    """Format a duration value for text progress output."""
    try:
        seconds = float(value)
    except TypeError, ValueError:
        return "unknown"

    if not math.isfinite(seconds):
        return "unknown"
    if round(seconds, 1) < 60.0:
        return f"{seconds:.1f}s"

    total_seconds = int(seconds + 0.5)
    minutes, remaining_seconds = divmod(total_seconds, 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{remaining_minutes:02d}m{remaining_seconds:02d}s"
    return f"{remaining_minutes}m{remaining_seconds:02d}s"


def format_turn_started_message(payload: dict[str, Any]) -> str:
    """Format the human-facing start line for one runner turn."""
    session = payload.get("session_id") or payload.get("session", "unknown")
    total = payload.get("total_sessions")
    label = str(session) if total is None else f"{session}/{total}"
    if payload.get("session_kind") == "review":
        return f"review session started ({label})"
    return f"session {label} started"


def format_turn_finished_message(payload: dict[str, Any]) -> str:
    """Format the human-facing finish line for one runner turn."""
    session = payload.get("session_id") or payload.get("session", "unknown")
    total_sessions = payload.get("total_sessions")
    label = str(session) if total_sessions is None else f"{session}/{total_sessions}"
    message = (
        f"session {label} finished "
        f"(status {payload.get('status_result', 'unknown')}, "
        f"return code {payload.get('returncode', 'unknown')}, "
        f"duration {format_duration_seconds(payload.get('duration_seconds'))}"
    )
    if payload.get("total_duration_seconds") is not None:
        message += (
            f", total {format_duration_seconds(payload.get('total_duration_seconds'))}"
        )
    return f"{message})"
