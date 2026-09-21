"""Durable incident policy and persist-before-publish delivery.

An incident is the application noticing something nobody asked it to look at:
active work it has lost sight of, a question that has gone unanswered, a run
that ended badly. Three properties make this a durable engine rather than a
notification call at the point of discovery, and each of them is a failure this
module exists to be unable to have.

* **Discovery is deterministic, so a repeat is the same incident.**
  :func:`identity` hashes the kind, the connector, the run, and one
  discriminator, and every write is ``INSERT OR IGNORE``. Ten snapshots in a
  row that all show the same unanswered question produce one row, and the
  eleventh -- carrying a *different* question -- produces a second. Nothing
  compares timestamps to decide whether it has alerted already.

* **Evidence is re-read inside the claim, and again before the send.** The
  observation hooks run inside the store's ingestion transaction, so the
  evidence and the incident commit together. The publisher then re-checks
  eligibility immediately before the network call: a queued notification loses
  its claim the moment a fresher snapshot resolves it, which is the difference
  between a cockpit that pages someone about a question that was answered two
  minutes ago and one that does not.

* **Delivery is persisted before it is attempted, and uncertainty is
  recorded as uncertainty.** A crash during publishing -- including after a
  successful POST but before its receipt was saved -- comes back as
  ``unconfirmed`` on restart, and no path replays it. Replaying would be the
  alternative, and the alternative is a duplicate page for an incident that was
  already delivered.

Retirement is the fourth property and the quiet one: when a run is superseded,
its watches and undelivered notifications are cancelled and its incident
*history* is left alone. A resolved incident is a record of something that
happened, and a newer run replacing an older one does not unhappen it.

The schema here is the Companion's migration 3, statement for statement, so an
existing ``companion.sqlite3`` carries its incident history forward rather than
starting over.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from .freshness import lost_observation_incident, unanswered_question_incident
from .observation import ObservationPolicy

# -- Schema -----------------------------------------------------------------

#: Byte-identical to the Companion's ``incidents.MIGRATION``. An installation
#: already at ``user_version = 3`` skips it and keeps every row it holds.
MIGRATION: tuple[str, ...] = (
    """CREATE TABLE incident_plan (
        connector_id TEXT NOT NULL, plan_key TEXT NOT NULL,
        first_seen_at REAL NOT NULL,
        PRIMARY KEY (connector_id, plan_key)
    )""",
    """CREATE TABLE incident_watch (
        connector_id TEXT NOT NULL, run_key TEXT NOT NULL, plan_key TEXT NOT NULL,
        project TEXT NOT NULL, source_at REAL NOT NULL, last_seen_at REAL NOT NULL,
        active INTEGER NOT NULL, question_id TEXT, question_since REAL,
        PRIMARY KEY (connector_id, run_key)
    )""",
    """CREATE TABLE incident (
        incident_id TEXT PRIMARY KEY, connector_id TEXT NOT NULL,
        run_key TEXT NOT NULL, plan_key TEXT NOT NULL, project TEXT NOT NULL,
        kind TEXT NOT NULL, question_id TEXT, detected_at REAL NOT NULL,
        evidence_at REAL NOT NULL,
        resolved_at REAL, delivery_state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
        dispatched_at REAL, receipt_id TEXT, published_at REAL,
        detail_code TEXT
    )""",
    "CREATE INDEX incident_delivery ON incident (delivery_state, next_attempt_at)",
    "CREATE INDEX incident_run ON incident (connector_id, run_key, resolved_at)",
)

#: When this deployment started watching. Endings older than it belong to a
#: window nobody was monitoring, so they never alert.
EPOCH = "incident_monitoring_started_at"

# -- Vocabularies -----------------------------------------------------------

#: Active work that could be lost sight of. Every other state is either
#: finished or waiting on a person, and neither is an outage.
ACTIVE_STATES: frozenset[str] = frozenset({"running", "verifying"})

INCIDENT_LOST_OBSERVATION = "lost-observation"
INCIDENT_UNANSWERED_QUESTION = "unanswered-question"
INCIDENT_TERMINAL_FAILURE = "terminal-failure"
INCIDENT_KINDS: tuple[str, ...] = (
    INCIDENT_LOST_OBSERVATION,
    INCIDENT_UNANSWERED_QUESTION,
    INCIDENT_TERMINAL_FAILURE,
)

#: The kinds a fresh observation of the same run can resolve on its own. A
#: terminal failure is not among them: an ending does not stop having happened.
OBSERVABLE_KINDS: tuple[str, ...] = (
    INCIDENT_UNANSWERED_QUESTION,
    INCIDENT_LOST_OBSERVATION,
)

DELIVERY_PENDING = "pending"
DELIVERY_RETRY = "retry"
DELIVERY_PUBLISHING = "publishing"
DELIVERY_PUBLISHED = "published"
DELIVERY_UNCONFIRMED = "unconfirmed"
DELIVERY_CANCELLED = "cancelled"
DELIVERY_STATES: tuple[str, ...] = (
    DELIVERY_PENDING,
    DELIVERY_RETRY,
    DELIVERY_PUBLISHING,
    DELIVERY_PUBLISHED,
    DELIVERY_UNCONFIRMED,
    DELIVERY_CANCELLED,
)

#: What a publisher may report. ``unconfirmed`` is the honest answer to every
#: failure that is not a refusal to send: a timeout establishes nothing.
PUBLISH_STATES: tuple[str, ...] = (
    DELIVERY_PUBLISHED,
    DELIVERY_RETRY,
    DELIVERY_UNCONFIRMED,
)

#: Why an incident is in the delivery state it is in, when the state alone is
#: not enough. ``restart-during-publish`` is the one an operator reads most.
DETAIL_NOT_SENT = "not-sent"
DETAIL_DELIVERY_UNKNOWN = "delivery-unknown"
DETAIL_RESTART_DURING_PUBLISH = "restart-during-publish"

_DETAIL_FOR_STATE: Mapping[str, str | None] = {
    DELIVERY_RETRY: DETAIL_NOT_SENT,
    DELIVERY_UNCONFIRMED: DETAIL_DELIVERY_UNKNOWN,
    DELIVERY_PUBLISHED: None,
}

# -- Delivery timing --------------------------------------------------------

BASE_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
#: How many doublings the backoff is allowed before the ceiling takes over.
MAX_BACKOFF_DOUBLINGS = 6
#: How long an ineligible candidate steps aside for. Small on purpose: it is
#: not a punishment, it is a way of not blocking the queue behind itself.
STARVATION_DEFERRAL_SECONDS = 5.0
#: How many rows one tick considers before giving up on finding an eligible
#: one. A bound rather than a full scan, because a tick runs on a timer and a
#: corpus with ten thousand stale questions must not turn it into a table scan.
MAX_DELIVERY_CANDIDATES = 500

#: What :meth:`IncidentEngine.diagnostics` may return, and its ceiling.
DEFAULT_DIAGNOSTICS_LIMIT = 200
MAX_DIAGNOSTICS_LIMIT = 500

#: What a diagnostics row carries. Identifiers, states and timestamps -- the
#: same rule the persistence allowlists apply, stated here because an incident
#: row is never built from an uploaded record and so never passes through them.
DIAGNOSTIC_FIELDS: tuple[str, ...] = (
    "incident_id",
    "run_key",
    "project",
    "kind",
    "detected_at",
    "resolved_at",
    "delivery_state",
    "attempts",
    "receipt_id",
    "published_at",
    "detail_code",
)


def identity(
    kind: str, connector_id: str, run_key: str, discriminator: Any = None
) -> str:
    """The deterministic id for one incident.

    The discriminator is what separates "this is still the same problem" from
    "this is a new one of the same kind": the question id for an unanswered
    question, the last-seen timestamp for lost observation. Two observations
    that agree on all four produce one row, whatever the clock says.
    """
    return hashlib.sha256(
        json.dumps([kind, connector_id, run_key, discriminator]).encode()
    ).hexdigest()


def backoff_seconds(attempts: int) -> float:
    """How long after a failed attempt the next one may be made."""
    doublings = min(max(int(attempts), 0), MAX_BACKOFF_DOUBLINGS)
    return min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * 2**doublings)


# -- Publishing -------------------------------------------------------------


@dataclass(frozen=True)
class PublishResult:
    """What one publish attempt established. Often: nothing."""

    state: str
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        if self.state not in PUBLISH_STATES:
            raise ValueError(f"unknown publish state: {self.state!r}")


class Publisher(Protocol):
    """Whatever carries a notification off this host.

    ``before_send`` is called immediately before the transmission and answers
    "is this still worth sending". A publisher that ignores it will deliver
    notifications for incidents that were resolved while its connection was
    being established.
    """

    def publish(
        self,
        *,
        kind: str,
        project: str,
        plan_key: str,
        run_key: str,
        before_send: Callable[[], bool],
        question_id: str | None = None,
    ) -> PublishResult: ...


class IncidentStore(Protocol):
    """The part of a store this engine uses, and nothing else.

    Named as a protocol rather than imported, both to keep this module free of
    a circular import and because the surface is the point: an incident engine
    that could reach any attribute of the store would grow into one that reads
    run summaries directly.
    """

    @property
    def connection(self) -> sqlite3.Connection: ...

    @property
    def policy(self) -> ObservationPolicy: ...

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def reading(self) -> AbstractContextManager[sqlite3.Connection]: ...


# -- Epochs -----------------------------------------------------------------


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
    projection_age = max(0.0, float(payload.get("projection_age_seconds") or 0.0))
    # What the *source* saw, not what arrived. A projection that took a minute
    # to reach here describes a corpus a minute old, and treating it as current
    # is how a stale view resurrects a run whose ending is already stored.
    source_at = min(now, observed_at) - projection_age
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
    plan_epoch(connection, connector_id, plan_key, now)

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
        asked_since = now

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


# -- Retention --------------------------------------------------------------


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


# -- Engine -----------------------------------------------------------------


class IncidentEngine:
    """One publisher per deployment. Ticks are bounded and clock-driven.

    Construction is itself a recovery step: anything left ``publishing`` by a
    previous process becomes ``unconfirmed``, because a process that died
    mid-send cannot say whether the send happened. No path replays it.
    """

    def __init__(
        self,
        *,
        store: IncidentStore,
        clock: Callable[[], float],
        publisher: Publisher | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.publisher = publisher
        self._tick_lock = threading.Lock()
        with store.transaction() as connection:
            epoch(connection, clock())
            connection.execute(
                "UPDATE incident SET delivery_state = ?, detail_code = ? "
                "WHERE delivery_state = ?",
                (
                    DELIVERY_UNCONFIRMED,
                    DETAIL_RESTART_DURING_PUBLISH,
                    DELIVERY_PUBLISHING,
                ),
            )

    # -- evaluation ------------------------------------------------------
    def _answered(self, connection: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
        """Whether a reply to this question was admitted into the journal."""
        found = connection.execute(
            "SELECT 1 FROM operation WHERE connector_id = ? AND run_key = ? "
            "AND question_id = ? AND method = 'question.reply' "
            "AND status IN ('accepted', 'consumed') LIMIT 1",
            (row["connector_id"], row["run_key"], row["question_id"]),
        ).fetchone()
        return found is not None

    def _evaluate(self, connection: sqlite3.Connection, now: float) -> None:
        policy = self.store.policy
        watches = connection.execute(
            "SELECT * FROM incident_watch WHERE active = 1 OR question_id IS NOT NULL"
        ).fetchall()
        for found in watches:
            watch: dict[str, Any] = dict(found)
            if watch["question_id"] and self._answered(connection, watch):
                resolve(
                    connection,
                    str(watch["connector_id"]),
                    str(watch["run_key"]),
                    now,
                    kind=INCIDENT_UNANSWERED_QUESTION,
                )
                connection.execute(
                    "UPDATE incident_watch SET question_id = NULL, "
                    "question_since = NULL WHERE connector_id = ? AND run_key = ?",
                    (watch["connector_id"], watch["run_key"]),
                )
                watch["question_id"] = None
            if watch["active"] and lost_observation_incident(
                now=now,
                policy=policy,
                active_work_last_seen_at=watch["last_seen_at"],
            ):
                record(
                    connection,
                    kind=INCIDENT_LOST_OBSERVATION,
                    connector_id=str(watch["connector_id"]),
                    run_key=str(watch["run_key"]),
                    plan_key=str(watch["plan_key"]),
                    project=str(watch["project"]),
                    now=now,
                    discriminator=watch["last_seen_at"],
                )
            if watch["question_id"] and unanswered_question_incident(
                now=now, policy=policy, asked_at=watch["question_since"]
            ):
                record(
                    connection,
                    kind=INCIDENT_UNANSWERED_QUESTION,
                    connector_id=str(watch["connector_id"]),
                    run_key=str(watch["run_key"]),
                    plan_key=str(watch["plan_key"]),
                    project=str(watch["project"]),
                    now=now,
                    discriminator=watch["question_id"],
                    question_id=str(watch["question_id"]),
                )
        prune(connection, policy=policy, now=now)

    def _eligible(
        self, connection: sqlite3.Connection, row: Mapping[str, Any], now: float
    ) -> bool:
        """Whether this incident is still worth sending.

        Only an unanswered question can stop being worth sending without being
        resolved: it is the one kind whose premise -- somebody is waiting and
        we can still see them waiting -- can quietly expire. A lost observation
        and a terminal failure stay true.
        """
        if row["kind"] != INCIDENT_UNANSWERED_QUESTION:
            return True
        if self._answered(connection, row):
            return False
        stale = self.store.policy.observation_stale_seconds
        watch = connection.execute(
            "SELECT * FROM incident_watch WHERE connector_id = ? AND run_key = ?",
            (row["connector_id"], row["run_key"]),
        ).fetchone()
        if watch is None or watch["question_id"] != row["question_id"]:
            return False
        if now - float(watch["last_seen_at"]) > stale:
            return False
        connector = connection.execute(
            "SELECT * FROM connector WHERE connector_id = ?",
            (row["connector_id"],),
        ).fetchone()
        if connector is None or not connector["bridge_alive"]:
            return False
        if (
            connector["contract_set_id"]
            not in self.store.policy.supported_contract_set_ids
        ):
            return False
        heartbeat_at = connector["heartbeat_observed_at"]
        if heartbeat_at is None or now - float(heartbeat_at) > stale:
            return False
        projection_age = float(connector["heartbeat_projection_age_seconds"] or 0.0)
        return projection_age + max(0.0, now - float(heartbeat_at)) <= stale

    # -- delivery --------------------------------------------------------
    def tick(self) -> None:
        """Evaluate evidence, then carry at most one notification.

        One per tick, deliberately. The alternative is a loop holding a claim
        over a batch, and a batch is what turns a single transport hang into a
        queue of incidents nobody can tell the state of.
        """
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            claimed = self._claim()
            if claimed is None:
                return
            result = self._publish(claimed)
            self._settle(claimed, result)
        finally:
            self._tick_lock.release()

    def _claim(self) -> dict[str, Any] | None:
        now = self.clock()
        with self.store.transaction() as connection:
            self._evaluate(connection, now)
            if self.publisher is None or not self.store.policy.publish_incidents:
                return None
            candidates = connection.execute(
                "SELECT * FROM incident WHERE resolved_at IS NULL "
                "AND delivery_state IN (?, ?) AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, incident_id LIMIT ?",
                (
                    DELIVERY_PENDING,
                    DELIVERY_RETRY,
                    now,
                    MAX_DELIVERY_CANDIDATES,
                ),
            ).fetchall()
            claimed: dict[str, Any] | None = None
            for candidate in candidates:
                if self._eligible(connection, candidate, now):
                    claimed = dict(candidate)
                    break
                # A stale question must not starve newer eligible incidents
                # outside this bounded batch of candidates.
                connection.execute(
                    "UPDATE incident SET next_attempt_at = ? WHERE incident_id = ?",
                    (now + STARVATION_DEFERRAL_SECONDS, candidate["incident_id"]),
                )
            if claimed is None:
                return None
            connection.execute(
                "UPDATE incident SET delivery_state = ?, attempts = attempts + 1, "
                "dispatched_at = ?, detail_code = NULL WHERE incident_id = ?",
                (DELIVERY_PUBLISHING, now, claimed["incident_id"]),
            )
            return claimed

    def _publish(self, claimed: Mapping[str, Any]) -> PublishResult:
        """Send outside the transaction and the lock, and never raise."""
        incident_id = str(claimed["incident_id"])

        def before_send() -> bool:
            with self.store.reading() as connection:
                current = connection.execute(
                    "SELECT * FROM incident WHERE incident_id = ?", (incident_id,)
                ).fetchone()
                return bool(
                    current is not None
                    and current["resolved_at"] is None
                    and current["delivery_state"] == DELIVERY_PUBLISHING
                    and self._eligible(connection, current, self.clock())
                )

        publisher = self.publisher
        if publisher is None:  # pragma: no cover - claimed only when set
            return PublishResult(DELIVERY_UNCONFIRMED)
        try:
            result = publisher.publish(
                kind=str(claimed["kind"]),
                project=str(claimed["project"]),
                plan_key=str(claimed["plan_key"]),
                run_key=str(claimed["run_key"]),
                question_id=(
                    str(claimed["question_id"]) if claimed.get("question_id") else None
                ),
                before_send=before_send,
            )
        except Exception:  # noqa: BLE001 - an unknown failure sent nothing knowable
            # A transport that raised may still have delivered. Only a
            # publisher that says so establishes that it did not.
            return PublishResult(DELIVERY_UNCONFIRMED)
        if not isinstance(result, PublishResult):
            return PublishResult(DELIVERY_UNCONFIRMED)
        return result

    def _settle(self, claimed: Mapping[str, Any], result: PublishResult) -> None:
        with self.store.transaction() as connection:
            now = self.clock()
            connection.execute(
                "UPDATE incident SET delivery_state = CASE "
                "WHEN resolved_at IS NOT NULL AND ? = ? THEN ? ELSE ? END, "
                "receipt_id = ?, published_at = ?, next_attempt_at = ?, "
                "detail_code = ? WHERE incident_id = ?",
                (
                    result.state,
                    DELIVERY_RETRY,
                    DELIVERY_CANCELLED,
                    result.state,
                    result.receipt_id,
                    now if result.state == DELIVERY_PUBLISHED else None,
                    now + backoff_seconds(int(claimed["attempts"])),
                    _DETAIL_FOR_STATE[result.state],
                    claimed["incident_id"],
                ),
            )

    # -- reads -----------------------------------------------------------
    def diagnostics(
        self, *, limit: int = DEFAULT_DIAGNOSTICS_LIMIT
    ) -> list[dict[str, Any]]:
        """Recent incidents and their delivery states, newest first."""
        with self.store.reading() as connection:
            return diagnostics(connection, limit=limit)


__all__ = [
    "ACTIVE_STATES",
    "DELIVERY_CANCELLED",
    "DELIVERY_PENDING",
    "DELIVERY_PUBLISHED",
    "DELIVERY_PUBLISHING",
    "DELIVERY_RETRY",
    "DELIVERY_STATES",
    "DELIVERY_UNCONFIRMED",
    "DETAIL_DELIVERY_UNKNOWN",
    "DETAIL_NOT_SENT",
    "DETAIL_RESTART_DURING_PUBLISH",
    "EPOCH",
    "INCIDENT_KINDS",
    "INCIDENT_LOST_OBSERVATION",
    "INCIDENT_TERMINAL_FAILURE",
    "INCIDENT_UNANSWERED_QUESTION",
    "MIGRATION",
    "PUBLISH_STATES",
    "IncidentEngine",
    "IncidentStore",
    "PublishResult",
    "Publisher",
    "backoff_seconds",
    "diagnostics",
    "epoch",
    "identity",
    "observe_history",
    "observe_snapshot",
    "plan_epoch",
    "prune",
    "record",
    "resolve",
    "retire_run",
]
