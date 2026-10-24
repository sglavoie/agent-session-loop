# ruff: noqa: F403, F405
"""Incident epoch, record, resolution, and retirement persistence."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .incidents_models import *


def epoch(connection: sqlite3.Connection, now: float) -> float:
    """When this deployment started watching, set on first use."""
    row = connection.execute(
        "SELECT value FROM service_meta WHERE key = ?", (EPOCH,)
    ).fetchone()
    if row is not None:
        try:
            return float(json.loads(row["value"]))
        except (TypeError, ValueError):
            pass
    connection.execute(
        "INSERT INTO service_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (EPOCH, json.dumps(now)),
    )
    return now


def plan_epoch(
    connection: sqlite3.Connection, connector_id: str, plan_key: str, now: float
) -> float:
    """When this plan was first seen here, set on first use.

    A newly discovered plan's first import may contain a week of old endings,
    even when this deployment has been running for months. Only later endings,
    or endings of a plan already seen live, belong to this monitoring window.
    """
    connection.execute(
        "INSERT OR IGNORE INTO incident_plan "
        "(connector_id, plan_key, first_seen_at) VALUES (?, ?, ?)",
        (connector_id, plan_key, now),
    )
    row = connection.execute(
        "SELECT first_seen_at FROM incident_plan "
        "WHERE connector_id = ? AND plan_key = ?",
        (connector_id, plan_key),
    ).fetchone()
    return float(row["first_seen_at"])


# -- Recording and resolution -----------------------------------------------


def resolve(
    connection: sqlite3.Connection,
    connector_id: str,
    run_key: str,
    now: float,
    *,
    kind: str | None = None,
    except_question: str | None = None,
) -> None:
    """Mark matching open incidents resolved, cancelling undelivered ones.

    A notification that has not left yet is cancelled outright. One that is
    already ``publishing`` keeps its state: the send may have succeeded, and
    rewriting it would claim knowledge of a transmission nobody observed.
    """
    statement = (
        "UPDATE incident SET resolved_at = ?, delivery_state = "
        f"CASE WHEN delivery_state IN ('{DELIVERY_PENDING}', '{DELIVERY_RETRY}') "
        f"THEN '{DELIVERY_CANCELLED}' ELSE delivery_state END "
        "WHERE connector_id = ? AND run_key = ? AND resolved_at IS NULL"
    )
    parameters: list[Any] = [now, connector_id, run_key]
    if kind is not None:
        statement += " AND kind = ?"
        parameters.append(kind)
    if except_question is not None:
        statement += " AND question_id != ?"
        parameters.append(except_question)
    connection.execute(statement, parameters)


def record(
    connection: sqlite3.Connection,
    *,
    kind: str,
    connector_id: str,
    run_key: str,
    plan_key: str,
    project: str,
    now: float,
    evidence_at: float | None = None,
    discriminator: Any = None,
    question_id: str | None = None,
) -> str:
    """Record one incident if it is not already recorded. Returns its id.

    ``INSERT OR IGNORE`` is the deduplication, and it is the whole of it: two
    callers that computed the same identity wrote the same incident, and the
    second one is not news.
    """
    if kind not in INCIDENT_KINDS:
        raise ValueError(f"unknown incident kind: {kind!r}")
    incident_id = identity(kind, connector_id, run_key, discriminator)
    connection.execute(
        "INSERT OR IGNORE INTO incident (incident_id, connector_id, run_key, "
        "plan_key, project, kind, question_id, detected_at, evidence_at, "
        "next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            incident_id,
            connector_id,
            run_key,
            plan_key,
            project,
            kind,
            question_id,
            now,
            now if evidence_at is None else evidence_at,
            now,
        ),
    )
    return incident_id


def retire_run(
    connection: sqlite3.Connection, connector_id: str, run_key: str, now: float
) -> None:
    """Cancel obsolete run-specific watches and deliveries, preserving history.

    Called when a positively newer run replaces this one. The watch stops, the
    undelivered notifications are cancelled, and every resolved incident stays
    exactly where it is: what the store keeps about a superseded run is the
    record that it was superseded, not an absence.
    """
    connection.execute(
        "UPDATE incident_watch SET active = 0, question_id = NULL, "
        "question_since = NULL WHERE connector_id = ? AND run_key = ?",
        (connector_id, run_key),
    )
    resolve(connection, connector_id, run_key, now)


# -- Observation hooks ------------------------------------------------------
