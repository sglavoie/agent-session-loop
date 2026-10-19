# ruff: noqa: F401, F403, F405, I001
"""Translate fresh snapshots and imported history into incident state."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from .incidents_models import *
from .incidents_persistence import epoch, plan_epoch, record, resolve


def observe_snapshot(
    connection: sqlite3.Connection,
    *,
    policy: ObservationPolicy,
    envelope: Mapping[str, Any],
    now: float,
) -> None:
    """Update watches from a newly accepted snapshot, within its transaction."""
    epoch(connection, now)
    payload = envelope["payload"]
    connector_id = str(envelope["connector_id"])
    observed_at = float(payload["observed_at"])
    # What the *source* saw, not what arrived. A projection that took a minute
    # to reach here describes a corpus a minute old, and treating it as current
    # is how a stale view resurrects a run whose ending is already stored.
    # ``observed_at`` is stamped when the corpus walk starts, so it already is
    # that moment; the snapshot's ``projection_age_seconds`` is the walk's own
    # duration, and subtracting it again would count the walk twice.
    source_at = min(now, observed_at)
    stale = policy.observation_stale_seconds
    if (
        payload.get("contract_set_id") not in policy.supported_contract_set_ids
        or now - source_at > stale
        or observed_at > now + stale
    ):
        # An incompatible, late, or future-dated observation is recorded by the
        # store and never used as incident evidence.
        return

    runs = list(payload["runs"])
    seen = {str(run["run_key"]) for run in runs}
    if not payload["truncated"]:
        _stop_watching_omitted(connection, connector_id, seen, source_at, now)
    for run in runs:
        _observe_run(connection, connector_id, run, source_at=source_at, now=now)


def _stop_watching_omitted(
    connection: sqlite3.Connection,
    connector_id: str,
    seen: set[str],
    source_at: float,
    now: float,
) -> None:
    """A whole view that omits a watched run is entitled to stop watching it."""
    watched = connection.execute(
        "SELECT run_key, source_at FROM incident_watch WHERE connector_id = ?",
        (connector_id,),
    ).fetchall()
    for old in watched:
        if str(old["run_key"]) in seen or source_at <= float(old["source_at"]):
            continue
        connection.execute(
            "UPDATE incident_watch SET active = 0, question_id = NULL, "
            "question_since = NULL, source_at = ? "
            "WHERE connector_id = ? AND run_key = ?",
            (source_at, connector_id, old["run_key"]),
        )
        for kind in OBSERVABLE_KINDS:
            resolve(connection, connector_id, str(old["run_key"]), now, kind=kind)


def _observe_run(
    connection: sqlite3.Connection,
    connector_id: str,
    run: Mapping[str, Any],
    *,
    source_at: float,
    now: float,
) -> None:
    run_key = str(run["run_key"])
    plan_key = str(run["plan_key"])
    first_seen_at = plan_epoch(connection, connector_id, plan_key, now)

    # A delayed projection must not resurrect a run whose ending we hold.
    terminal = connection.execute(
        "SELECT MAX(recorded_at) AS latest FROM history_entry "
        "WHERE connector_id = ? AND run_key = ?",
        (connector_id, run_key),
    ).fetchone()["latest"]
    if terminal is not None and source_at <= float(terminal):
        return

    previous = connection.execute(
        "SELECT * FROM incident_watch WHERE connector_id = ? AND run_key = ?",
        (connector_id, run_key),
    ).fetchone()
    # Repeated projections of the same old source are not fresh evidence.
    if previous is not None and source_at <= float(previous["source_at"]):
        return

    question_id = (
        str(run["question_id"])
        if run.get("question_id") and run.get("unanswered_count")
        else None
    )
    if question_id is None:
        asked_since = None
    elif previous is not None and previous["question_id"] == question_id:
        # The same question is the same wait. Restarting the timer on every
        # snapshot would mean a question is never unanswered for long enough.
        asked_since = previous["question_since"]
    else:
        try:
            question_asked_at = float(run["question_asked_at"])
        except (KeyError, TypeError, ValueError):
            question_asked_at = now
        # A bounded worker wait can briefly disappear between two waits for the
        # same durable mailbox question.  Recover its original age from the
        # projection instead of restarting the notification timer.  The plan's
        # local monitoring epoch prevents an old question discovered at service
        # startup from producing an immediate historical alert, while the upper
        # clamp rejects a source clock that claims the question is in the future.
        asked_since = max(first_seen_at, min(question_asked_at, now))

    connection.execute(
        "INSERT INTO incident_watch (connector_id, run_key, plan_key, project, "
        "source_at, last_seen_at, active, question_id, question_since) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(connector_id, run_key) DO UPDATE SET "
        "plan_key = excluded.plan_key, project = excluded.project, "
        "source_at = excluded.source_at, last_seen_at = excluded.last_seen_at, "
        "active = excluded.active, question_id = excluded.question_id, "
        "question_since = excluded.question_since",
        (
            connector_id,
            run_key,
            plan_key,
            str(run["project"]),
            source_at,
            source_at,
            int(str(run["primary_state"]) in ACTIVE_STATES),
            question_id,
            asked_since,
        ),
    )
    resolve(
        connection,
        connector_id,
        run_key,
        now,
        kind=INCIDENT_UNANSWERED_QUESTION,
        except_question=question_id,
    )
    resolve(connection, connector_id, run_key, now, kind=INCIDENT_LOST_OBSERVATION)
    # Evidence just changed, so anything still queued for this run deserves to
    # be looked at now rather than after its backoff.
    connection.execute(
        "UPDATE incident SET next_attempt_at = MIN(next_attempt_at, ?) "
        "WHERE connector_id = ? AND run_key = ? AND delivery_state = ? "
        "AND resolved_at IS NULL",
        (now, connector_id, run_key, DELIVERY_PENDING),
    )
    if str(run["primary_state"]) in ACTIVE_STATES:
        # Fresh resumed or replacement work makes a delayed failure obsolete.
        _resolve_superseded_failures(
            connection, connector_id, plan_key, before=source_at, now=now
        )


def _resolve_superseded_failures(
    connection: sqlite3.Connection,
    connector_id: str,
    plan_key: str,
    *,
    before: float,
    now: float,
) -> None:
    rows = connection.execute(
        "SELECT run_key FROM incident WHERE connector_id = ? AND plan_key = ? "
        "AND kind = ? AND resolved_at IS NULL AND evidence_at < ?",
        (connector_id, plan_key, INCIDENT_TERMINAL_FAILURE, before),
    ).fetchall()
    for failure in rows:
        resolve(
            connection,
            connector_id,
            str(failure["run_key"]),
            now,
            kind=INCIDENT_TERMINAL_FAILURE,
        )


def observe_history(
    connection: sqlite3.Connection,
    *,
    policy: ObservationPolicy,
    connector_id: str,
    entry: Mapping[str, Any],
    now: float,
) -> None:
    """Settle watches and failure incidents from one imported ending."""
    plan_key = str(entry["plan_key"])
    run_key = str(entry["run_key"])
    recorded_at = float(entry["recorded_at"])
    window = max(
        epoch(connection, now), plan_epoch(connection, connector_id, plan_key, now)
    )

    watch = connection.execute(
        "SELECT * FROM incident_watch WHERE connector_id = ? AND run_key = ?",
        (connector_id, run_key),
    ).fetchone()
    if watch is None or float(watch["source_at"]) <= recorded_at:
        # An ending at least as new as the newest projection settles the run.
        connection.execute(
            "UPDATE incident_watch SET active = 0, question_id = NULL, "
            "question_since = NULL WHERE connector_id = ? AND run_key = ?",
            (connector_id, run_key),
        )
        for kind in OBSERVABLE_KINDS:
            resolve(connection, connector_id, run_key, now, kind=kind)

    if str(entry["outcome"]) != "failed":
        resolve(connection, connector_id, run_key, now, kind=INCIDENT_TERMINAL_FAILURE)
        if str(entry["outcome"]) == "completed":
            _resolve_superseded_failures(
                connection, connector_id, plan_key, before=recorded_at, now=now
            )
        return

    # First startup, and imported or reconstructed endings, never produce old
    # alerts: the failure is real, and nobody is being woken for it now.
    finished_at = entry.get("run_finished_at")
    if (
        entry.get("reconstructed")
        or finished_at is None
        or min(recorded_at, float(finished_at)) <= window
    ):
        return

    newer_active = connection.execute(
        "SELECT 1 FROM incident_watch WHERE connector_id = ? AND plan_key = ? "
        "AND active = 1 AND source_at > ?",
        (connector_id, plan_key, recorded_at),
    ).fetchone()
    completed_successor = connection.execute(
        "SELECT 1 FROM history_entry WHERE connector_id = ? AND plan_key = ? "
        "AND outcome = 'completed' AND recorded_at > ?",
        (connector_id, plan_key, recorded_at),
    ).fetchone()
    if (
        newer_active is not None
        or completed_successor is not None
        or now - recorded_at > policy.summary_retention_seconds
    ):
        return

    record(
        connection,
        kind=INCIDENT_TERMINAL_FAILURE,
        connector_id=connector_id,
        run_key=run_key,
        plan_key=plan_key,
        project=str(entry["project"]),
        now=now,
        evidence_at=recorded_at,
    )
