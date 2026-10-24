# ruff: noqa: F401
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
