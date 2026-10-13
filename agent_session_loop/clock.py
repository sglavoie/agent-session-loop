"""UTC clock helpers shared by the runner and the turn-execution boundary.

These are the run loop's original time primitives, moved to a leaf module so
the ``turn_execution_*`` modules can stamp turn records without importing
the runner. Every moment ASL persists is UTC and ISO 8601 with an explicit
offset, which is what the AgentMux contract corpus requires of a timestamp.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any


def utc_run_id() -> str:
    """Return a run identity that sorts by start time and names its process."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{os.getpid()}"


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso_now() -> str:
    return utc_now().isoformat()


def isoformat(moment: datetime) -> str:
    return moment.isoformat()


def duration_seconds(started_at: datetime, finished_at: datetime) -> float:
    """Return a non-negative millisecond-rounded span between two moments."""
    return round(max(0.0, (finished_at - started_at).total_seconds()), 3)


def parse_datetime(value: Any) -> datetime | None:
    """Read a persisted timestamp back as an aware UTC datetime, or ``None``.

    A naive value is read as UTC rather than rejected: ASL never writes one,
    but a record hand-edited by an operator still has to be replayable.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
