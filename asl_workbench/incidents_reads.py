# ruff: noqa: F403, F405
"""Retention and read-only incident diagnostics."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .incidents_models import *


def prune(
    connection: sqlite3.Connection, *, policy: ObservationPolicy, now: float
) -> int:
    """Drop aged-out incidents and settled watches. Returns incidents removed.

    An incident still ``publishing`` is never deleted however old it is: it is
    the record of a send whose outcome nobody established, and deleting it
    would turn an unknown into a silence.
    """
    cutoff = now - policy.summary_retention_seconds
    removed = connection.execute(
        "DELETE FROM incident WHERE resolved_at < ? AND delivery_state != ?",
        (cutoff, DELIVERY_PUBLISHING),
    ).rowcount
    connection.execute(
        "DELETE FROM incident_watch WHERE active = 0 AND question_id IS NULL "
        "AND last_seen_at < ?",
        (cutoff,),
    )
    return max(removed, 0)


# -- Reading ----------------------------------------------------------------


def diagnostics(
    connection: sqlite3.Connection, *, limit: int = DEFAULT_DIAGNOSTICS_LIMIT
) -> list[dict[str, Any]]:
    """Recent incidents and their delivery states, newest first.

    A free function rather than only an engine method because a cockpit read
    has no business constructing an engine: doing so rewrites every row a dead
    process left ``publishing``, which is recovery and not a GET.
    """
    bound = min(max(int(limit), 1), MAX_DIAGNOSTICS_LIMIT)
    rows = connection.execute(
        f"SELECT {', '.join(DIAGNOSTIC_FIELDS)} FROM incident "
        "ORDER BY detected_at DESC, incident_id LIMIT ?",
        (bound,),
    ).fetchall()
    return [dict(row) for row in rows]
