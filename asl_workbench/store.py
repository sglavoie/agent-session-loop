"""The portable SQLite store: migrations, ordered ingestion, retention, reads.

This is the Companion's store, moved into the package that both halves of the
application can load, and it is a module rather than a handful of queries at
the endpoints because of four properties that are easy to break one endpoint at
a time.

**Ingestion is ordered, and the order is time rather than the generation
counter.** Each upload carries the observation time and the generation of the
process that produced it. The generation is what tells a reconnect from an
emptied corpus, but it is *not* a monotonic sequence across a source's own
restarts -- a process that restarts counts its generations from one again while
its connector id stays the same. Ordering on the counter would therefore refuse
every upload from a restarted source forever. So observation time orders, and
the generation refuses only what time cannot: a late upload from a session that
has already been replaced.

**Ingestion is atomic.** A snapshot is one transaction: the connector row, the
runs it carries, the absence of the runs it does not, the incident evidence,
and the retention pass all commit together or none of them do. A half-applied
snapshot is the one state a cockpit cannot describe honestly, because its row
set would belong to no observation that ever happened.

**Omission is not replacement evidence.** A complete view marks omitted rows
absent; a partial one retains them as ``unconfirmed``. A positively newer run
for the same connector and plan can retire its predecessor even in a partial
view. None of these proves completion; terminal outcomes arrive through
history. The same rule governs the catalog, where it has one extra edge: a
generation is delivered in pages, so a generation that claims to be complete
still cannot retire anything until its *final* page has arrived. Retiring at
page one would empty the catalog on every refresh of a corpus large enough to
need two pages.

**Catalog storage is separate from run history.** They answer different
questions -- what plans exist, and what runs ended -- and the Companion
conflated them by keeping plan labels in a JSON blob in ``service_meta``
keyed by connector. Migration 4 creates a real ``catalog_entry`` table and
carries those blobs into it, so every label and title an installation already
had survives while project membership stops being a copy on every ending.

What is stored is bounded by :mod:`asl_workbench.observation`'s allowlists
rather than by these tables: every column here is named in one of them, or is
ingestion bookkeeping the source never sent.
``tests/test_workbench_store.py`` asserts exactly that against the schema.

Migrations 1 through 3 are the Companion's, statement for statement. An
existing ``companion.sqlite3`` at ``user_version = 3`` therefore skips straight
to migration 4 with every run, ending, operation and incident it holds, and
every identifier those rows are keyed by.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from . import freshness as freshness_module
from . import incidents
from .observation import (
    MAX_LABEL_CHARS,
    MAX_WARNINGS,
    OBSERVATION_ABSENT,
    OBSERVATION_CURRENT,
    OBSERVATION_UNCONFIRMED,
    ObservationPolicy,
    completeness_of,
    export_catalog_entry,
    export_history_entry,
    export_run_summary,
    persist_catalog_entry,
    persist_history_entry,
    persist_run_summary,
    require_policy,
)
from .operations import (
    OPERATION_OUTSTANDING_STATUSES,
    OPERATION_TERMINAL_STATUSES,
    REASON_UNKNOWN_OPERATION,
    OperationError,
    export_operation,
    next_status,
    persist_operation,
)

# One integer in ``PRAGMA user_version`` is the whole migration ledger. A table
# of applied migrations would need a migration of its own before it could be
# read.
SCHEMA_VERSION = 4

#: The version an installation migrated from the Companion arrives at. Named so
#: the upgrade test can assert it rather than a bare ``3``.
COMPANION_SCHEMA_VERSION = 3

# How often retention runs on the ingestion path. Uploads arrive every few
# seconds and the delete is indexed, but a delete per upload is still a delete
# per upload for no gain: nothing expires in the gap that would not expire in
# the next one.
PRUNE_INTERVAL_SECONDS = 300.0

# What one read may return. A bound rather than a page cursor, because a
# cockpit shows a working set and not an archive, and an unbounded read is the
# one query that turns a busy corpus into a service outage. The catalog is the
# exception and pages properly -- see `catalog`.
DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 500

META_LAST_PRUNED_AT = "last_pruned_at"
META_CREATED_AT = "created_at"
#: The prefix of the Companion's per-connector project blobs. Read once, by
#: migration 4, and never written again.
META_PROJECT_CATALOG_PREFIX = "projects."

KIND_HEARTBEAT = "heartbeat"
KIND_SNAPSHOT = "snapshot"
KIND_HISTORY_BATCH = "history_batch"
KIND_CATALOG_PAGE = "catalog_page"

#: The generation a carried-over Companion label belongs to. Not a real
#: generation, which is what makes the first complete catalog able to retire
#: it: a plan the Mac no longer enumerates is genuinely gone.
LEGACY_GENERATION = ""

# Ingestion refusals. Distinct from any frame vocabulary because these are
# answers about the *store's* state, not about the message: the same bytes that
# are refused now would have been accepted a minute ago.
REFUSED_STALE_GENERATION = "stale-generation"
REFUSED_STALE_OBSERVATION = "stale-observation"
INGEST_REFUSAL_CODES: tuple[str, ...] = (
    REFUSED_STALE_GENERATION,
    REFUSED_STALE_OBSERVATION,
)

_ORDER_NEWER = "newer"


class StoreError(RuntimeError):
    """The store could not be opened or migrated. Nothing was written."""


class IngestRefused(RuntimeError):
    """An upload was refused for what the store already holds, not for its shape."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in INGEST_REFUSAL_CODES:
            raise AssertionError(f"undeclared ingest refusal code: {code}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class IngestInvalid(ValueError):
    """An upload was refused for its own shape, before anything was written."""


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------

# Written out rather than generated from the allowlists. A schema derived from
# a constant would change shape the moment the constant did, which is the
# property a migration exists to prevent; a literal schema plus a test that
# compares it to the allowlist catches the drift instead of absorbing it.
_MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE service_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE connector (
        connector_id                     TEXT PRIMARY KEY,
        first_seen_at                    REAL NOT NULL,

        heartbeat_observed_at            REAL,
        heartbeat_received_at            REAL,
        heartbeat_generation             INTEGER,
        bridge_alive                     INTEGER,
        bridge_protocol                  INTEGER,
        contract_set_id                  TEXT,
        asl_version                      TEXT,
        degraded_reason                  TEXT,
        heartbeat_projection_age_seconds REAL,

        snapshot_observed_at             REAL,
        snapshot_received_at             REAL,
        snapshot_generation              INTEGER,
        snapshot_digest                  TEXT,
        snapshot_source_digest           TEXT,
        snapshot_contract_set_id         TEXT,
        snapshot_row_count               INTEGER,
        snapshot_truncated               INTEGER,
        snapshot_failed_projections      INTEGER,
        snapshot_projection_age_seconds  REAL,
        snapshot_staleness_bound_seconds REAL,
        snapshot_warnings                TEXT,

        history_batch_id                 TEXT,
        history_generated_at             REAL,
        history_received_at              REAL,
        history_window_start             REAL,
        history_window_end               REAL,
        history_complete                 INTEGER,
        history_more                     INTEGER
    )
    """,
    """
    CREATE TABLE run_summary (
        connector_id      TEXT NOT NULL,
        run_key           TEXT NOT NULL,
        project           TEXT NOT NULL,
        plan_key          TEXT NOT NULL,
        run_id            TEXT,
        session_id        TEXT,
        harness           TEXT,
        primary_state     TEXT NOT NULL,
        phase             TEXT,
        urgency_group     TEXT,
        pause_reason      TEXT,
        outcome           TEXT,
        question_id       TEXT,
        question_asked_at REAL,
        unanswered_count  INTEGER,
        run_started_at    REAL,
        run_finished_at   REAL,
        last_activity_at  REAL,
        observed_at       REAL NOT NULL,
        received_at       REAL NOT NULL,
        source_generation INTEGER,
        present           INTEGER NOT NULL DEFAULT 1,
        absent_since      REAL,
        PRIMARY KEY (connector_id, run_key)
    )
    """,
    "CREATE INDEX run_summary_observed_at ON run_summary (observed_at)",
    "CREATE INDEX run_summary_present ON run_summary (connector_id, present)",
    """
    CREATE TABLE history_entry (
        connector_id            TEXT NOT NULL,
        entry_id                TEXT NOT NULL,
        source_key              TEXT NOT NULL,
        plan_key                TEXT NOT NULL,
        run_key                 TEXT NOT NULL,
        project                 TEXT NOT NULL,
        run_id                  TEXT,
        harness                 TEXT,
        model                   TEXT,
        outcome                 TEXT NOT NULL,
        pause_reason            TEXT,
        recorded_at             REAL NOT NULL,
        run_started_at          REAL,
        run_finished_at         REAL,
        duration_seconds        REAL,
        turn_count              INTEGER,
        completed_session_count INTEGER,
        pending_session_count   INTEGER,
        completed_session_ids   TEXT,
        review_completed        INTEGER,
        review_outcome          TEXT,
        reconstructed           INTEGER,
        event_schema_version    INTEGER,
        received_at             REAL NOT NULL,
        PRIMARY KEY (connector_id, entry_id)
    )
    """,
    "CREATE INDEX history_entry_recorded_at ON history_entry (recorded_at)",
)

# The operation journal. Its most important property is what it does not have a
# column for: neither the reply text nor the action token that would let the
# reply be sent again. Both live in bounded memory for as long as the command
# is undelivered and are gone the moment it stops being deliverable, which is
# what makes "answers are not retained across a reconnect" a fact about the
# schema rather than a promise about the code.
_MIGRATION_2: tuple[str, ...] = (
    """
    CREATE TABLE operation (
        operation_id  TEXT PRIMARY KEY,
        connector_id  TEXT NOT NULL,
        command_id    TEXT NOT NULL,
        method        TEXT NOT NULL,
        status        TEXT NOT NULL,
        plan_key      TEXT NOT NULL,
        run_key       TEXT NOT NULL,
        run_id        TEXT,
        question_id   TEXT,
        requested_at  REAL NOT NULL,
        deliver_by    REAL NOT NULL,
        dispatched_at REAL,
        settled_at    REAL,
        detail_code   TEXT,
        received_at   REAL NOT NULL,
        updated_at    REAL NOT NULL
    )
    """,
    # One outstanding mutation per plan is a question about `(plan_key,
    # status)`, and every poll asks `(status, deliver_by)`. Both are answered
    # from an index so the command path never scans the journal while a
    # heartbeat waits.
    "CREATE INDEX operation_plan ON operation (plan_key, status)",
    "CREATE INDEX operation_status ON operation (status, deliver_by)",
    "CREATE INDEX operation_requested_at ON operation (requested_at)",
)

# Catalog storage. New in this package, and additive on purpose: the connector
# row grows catalog columns rather than being rewritten, because rewriting it
# would mean copying every existing connector through a temporary table for
# eight nullable fields.
_MIGRATION_4: tuple[str, ...] = (
    "ALTER TABLE connector ADD COLUMN catalog_generation TEXT",
    "ALTER TABLE connector ADD COLUMN catalog_observed_at REAL",
    "ALTER TABLE connector ADD COLUMN catalog_received_at REAL",
    "ALTER TABLE connector ADD COLUMN catalog_source_generation INTEGER",
    "ALTER TABLE connector ADD COLUMN catalog_total INTEGER",
    "ALTER TABLE connector ADD COLUMN catalog_delivered INTEGER",
    "ALTER TABLE connector ADD COLUMN catalog_complete INTEGER",
    "ALTER TABLE connector ADD COLUMN catalog_final INTEGER",
    # `exists` is an SQLite keyword, so the column is quoted here and wherever
    # it is named. Renaming it would have been easier and would have broken the
    # property this schema is checked for: every column is a field the
    # allowlist permits, under the name the allowlist uses.
    """
    CREATE TABLE catalog_entry (
        connector_id            TEXT NOT NULL,
        plan_key                TEXT NOT NULL,
        run_key                 TEXT,
        title                   TEXT,
        display_label           TEXT NOT NULL,
        primary_state           TEXT,
        recommended_action      TEXT,
        urgency_rank            INTEGER,
        urgency_group           TEXT,
        activity_label          TEXT,
        projects                TEXT NOT NULL,
        project_group           TEXT NOT NULL,
        counts                  TEXT,
        sources                 TEXT,
        "exists"                INTEGER,
        pinned                  INTEGER,
        next_pending_session_id TEXT,
        last_activity_at        TEXT,
        last_activity_source    TEXT,
        completed_at            TEXT,
        pause_reason            TEXT,
        harness                 TEXT,
        registry_digest         TEXT,
        repository_count        INTEGER,
        warning_count           INTEGER,
        search_text             TEXT,
        generation              TEXT NOT NULL,
        observed_at             REAL NOT NULL,
        received_at             REAL NOT NULL,
        present                 INTEGER NOT NULL DEFAULT 1,
        absent_since            REAL,
        PRIMARY KEY (connector_id, plan_key)
    )
    """,
    "CREATE INDEX catalog_entry_generation ON catalog_entry (connector_id, generation)",
    "CREATE INDEX catalog_entry_present ON catalog_entry (connector_id, present)",
)

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
    (3, incidents.MIGRATION),
    (4, _MIGRATION_4),
)

# Columns that are the store's own bookkeeping rather than something a source
# uploaded. Listed so the schema test can assert that every *other* column is a
# field the allowlist already permits.
_RUN_BOOKKEEPING: frozenset[str] = frozenset(
    {"connector_id", "received_at", "source_generation", "present", "absent_since"}
)
_HISTORY_BOOKKEEPING: frozenset[str] = frozenset({"connector_id", "received_at"})
_OPERATION_BOOKKEEPING: frozenset[str] = frozenset(
    {"connector_id", "received_at", "updated_at"}
)
_CATALOG_BOOKKEEPING: frozenset[str] = frozenset(
    {
        "connector_id",
        "generation",
        "observed_at",
        "received_at",
        "present",
        "absent_since",
    }
)

#: Every table whose columns are checked against an allowlist, with the
#: bookkeeping set that is allowed to be on neither side.
SCHEMA_BOOKKEEPING: Mapping[str, frozenset[str]] = {
    "run_summary": _RUN_BOOKKEEPING,
    "history_entry": _HISTORY_BOOKKEEPING,
    "operation": _OPERATION_BOOKKEEPING,
    "catalog_entry": _CATALOG_BOOKKEEPING,
}

# The persisted columns that are booleans in the record and integers on disk.
_HISTORY_BOOLEANS: tuple[str, ...] = ("review_completed", "reconstructed")
_CATALOG_BOOLEANS: tuple[str, ...] = ("exists", "pinned")
# The persisted columns that are sequences in the record and JSON text on disk.
_CATALOG_JSON: tuple[str, ...] = ("projects", "counts", "sources")

_RUN_COLUMNS: tuple[str, ...] = (
    "connector_id",
    "run_key",
    "project",
    "plan_key",
    "run_id",
    "session_id",
    "harness",
    "primary_state",
    "phase",
    "urgency_group",
    "pause_reason",
    "outcome",
    "question_id",
    "question_asked_at",
    "unanswered_count",
    "run_started_at",
    "run_finished_at",
    "last_activity_at",
    "observed_at",
    "received_at",
    "source_generation",
    "present",
    "absent_since",
)

_HISTORY_COLUMNS: tuple[str, ...] = (
    "connector_id",
    "entry_id",
    "source_key",
    "plan_key",
    "run_key",
    "project",
    "run_id",
    "harness",
    "model",
    "outcome",
    "pause_reason",
    "recorded_at",
    "run_started_at",
    "run_finished_at",
    "duration_seconds",
    "turn_count",
    "completed_session_count",
    "pending_session_count",
    "completed_session_ids",
    "review_completed",
    "review_outcome",
    "reconstructed",
    "event_schema_version",
    "received_at",
)

_OPERATION_COLUMNS: tuple[str, ...] = (
    "operation_id",
    "connector_id",
    "command_id",
    "method",
    "status",
    "plan_key",
    "run_key",
    "run_id",
    "question_id",
    "requested_at",
    "deliver_by",
    "dispatched_at",
    "settled_at",
    "detail_code",
    "received_at",
    "updated_at",
)

_CATALOG_COLUMNS: tuple[str, ...] = (
    "connector_id",
    "plan_key",
    "run_key",
    "title",
    "display_label",
    "primary_state",
    "recommended_action",
    "urgency_rank",
    "urgency_group",
    "activity_label",
    "projects",
    "project_group",
    "counts",
    "sources",
    "exists",
    "pinned",
    "next_pending_session_id",
    "last_activity_at",
    "last_activity_source",
    "completed_at",
    "pause_reason",
    "harness",
    "registry_digest",
    "repository_count",
    "warning_count",
    "search_text",
    "generation",
    "observed_at",
    "received_at",
    "present",
    "absent_since",
)

_CATALOG_IDENTITY: tuple[str, ...] = ("connector_id", "plan_key")


def _quoted(columns: Sequence[str]) -> str:
    """Column names, always quoted, because one of them is a keyword."""
    return ", ".join(f'"{column}"' for column in columns)


def connect(path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open one database with the pragmas this store depends on.

    ``check_same_thread`` is off and every operation takes the store's lock
    instead: the server is threaded, the write volume is one source, and one
    serialised connection is both simpler to reason about and immune to the
    ``database is locked`` retry loop a pool would need.
    """
    connection = sqlite3.connect(
        path, timeout=timeout, check_same_thread=False, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    return connection


def schema_version(connection: sqlite3.Connection) -> int:
    """The migration ledger: one integer."""
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate(connection: sqlite3.Connection, *, created_at: float = 0.0) -> int:
    """Bring one database up to :data:`SCHEMA_VERSION`, returning the version reached.

    Each step commits with the version bump that records it, so an interrupted
    migration leaves the database at the last version whose statements all ran.
    Re-running on a current database is a no-op, which is what makes restarting
    safe; running against a *newer* database is refused outright, because a
    downgrade that silently writes through a newer schema is how two versions
    of a service corrupt one file.
    """
    current = schema_version(connection)
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"database schema {current} is newer than this application "
            f"({SCHEMA_VERSION})"
        )
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            # `PRAGMA user_version` does not take a parameter binding.
            connection.execute(f"PRAGMA user_version={int(version)}")
            hook = _MIGRATION_HOOKS.get(version)
            if hook is not None:
                hook(connection, created_at)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        current = version
    return current


def _seed_created_at(connection: sqlite3.Connection, created_at: float) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO service_meta (key, value) VALUES (?, ?)",
        (META_CREATED_AT, json.dumps(created_at)),
    )


def _carry_project_catalogs(connection: sqlite3.Connection, created_at: float) -> None:
    """Turn the Companion's per-connector project blobs into catalog rows.

    The blobs held every plan label, title and project membership an
    installation had accumulated, and dropping them would have re-labelled
    months of retained history "Unknown project" on the first start after the
    upgrade. They are carried in at :data:`LEGACY_GENERATION` with no state, so
    they can label a row and cannot pretend to be a catalog entry somebody
    observed. The blobs themselves are left in place: nothing reads them, and
    deleting them inside the migration would make the upgrade irreversible for
    an operator who wanted to roll back to the Companion.
    """
    rows = connection.execute(
        "SELECT key, value FROM service_meta WHERE key LIKE ?",
        (META_PROJECT_CATALOG_PREFIX + "%",),
    ).fetchall()
    for row in rows:
        connector_id = str(row["key"])[len(META_PROJECT_CATALOG_PREFIX) :]
        try:
            catalog = json.loads(row["value"])
        except ValueError:
            continue
        if not isinstance(catalog, dict):
            continue
        for plan_key, metadata in catalog.items():
            if not isinstance(metadata, dict):
                continue
            projects = [str(name) for name in metadata.get("projects") or []]
            title = metadata.get("title")
            connection.execute(
                "INSERT OR IGNORE INTO catalog_entry (connector_id, plan_key, "
                "display_label, title, projects, project_group, generation, "
                "observed_at, received_at, present) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    connector_id,
                    str(plan_key),
                    str(metadata.get("plan") or plan_key),
                    str(title) if title else None,
                    json.dumps(projects),
                    " + ".join(projects)[:MAX_LABEL_CHARS],
                    LEGACY_GENERATION,
                    0.0,
                    created_at,
                ),
            )


#: Migration steps that need more than SQL. Each runs inside its migration's
#: own transaction, so a failed carry-over rolls the whole version back.
_MIGRATION_HOOKS: Mapping[int, Callable[[sqlite3.Connection, float], None]] = {
    1: _seed_created_at,
    4: _carry_project_catalogs,
}


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IngestResult:
    """What one accepted upload changed."""

    kind: str
    connector_id: str
    duplicate: bool = False
    stored: int = 0
    updated: int = 0
    absent: int = 0
    pruned: int = 0

    def as_document(self) -> dict[str, Any]:
        return {
            "status": "duplicate" if self.duplicate else "accepted",
            "kind": self.kind,
            "stored": self.stored,
            "updated": self.updated,
            "absent": self.absent,
            "pruned": self.pruned,
        }


@dataclass(frozen=True)
class SourceStatus:
    """Whether one source of truth is whole, and why it is not."""

    completeness: str
    reasons: tuple[str, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {"completeness": self.completeness, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ConnectorStatus:
    """One source's connection health, projection health, and completeness.

    Three separate questions with three separate answers. A source that is
    heartbeating perfectly while its projection stopped is the case this shape
    exists to be able to describe.
    """

    connector_id: str
    freshness: freshness_module.Freshness
    compatible: bool
    contract_set_id: str | None
    bridge_alive: bool | None
    degraded_reason: str | None
    source_generation: int | None
    row_count: int
    truncated: bool | None
    warnings: tuple[str, ...]
    runs: SourceStatus
    history: SourceStatus
    catalog: SourceStatus
    last_heartbeat_at: float | None
    last_snapshot_at: float | None
    asl_version: str | None = None
    bridge_protocol: int | None = None
    projection_age_seconds: float | None = None

    @property
    def degraded(self) -> bool:
        return (
            self.freshness.label != freshness_module.FRESHNESS_LIVE
            or self.degraded_reason is not None
            or not self.compatible
            or self.completeness != freshness_module.COMPLETENESS_COMPLETE
        )

    @property
    def completeness(self) -> str:
        """The weakest completeness among this source's three streams."""
        return freshness_module.aggregate_completeness(
            (
                self.runs.completeness,
                self.history.completeness,
                self.catalog.completeness,
            )
        )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "connector_id": self.connector_id,
            **self.freshness.as_document(),
            "compatible": self.compatible,
            "contract_set_id": self.contract_set_id,
            "bridge_alive": self.bridge_alive,
            "bridge_protocol": self.bridge_protocol,
            "asl_version": self.asl_version,
            "degraded_reason": self.degraded_reason,
            "source_generation": self.source_generation,
            "row_count": self.row_count,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "sources": {
                "runs": self.runs.as_document(),
                "history": self.history.as_document(),
                "catalog": self.catalog.as_document(),
            },
            "last_heartbeat_at": self.last_heartbeat_at,
            "last_snapshot_at": self.last_snapshot_at,
        }
        return document


@dataclass(frozen=True)
class Counts:
    """Aggregate numbers, carrying no identity at all.

    This is what a summary-only credential is entitled to: how much there is
    and how healthy it looks, never what any of it is called. Adding a field
    here is adding a number; adding an identifier would make this a different
    kind of answer, and the credential that reads it was not issued for that.
    """

    runs: int = 0
    needs_attention: int = 0
    running: int = 0
    history_entries: int = 0
    connectors: int = 0
    absent_runs: int = 0
    unconfirmed_runs: int = 0
    plans: int = 0
    absent_plans: int = 0
    unconfirmed_plans: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def as_document(self) -> dict[str, int]:
        document = {
            "runs": self.runs,
            "needs_attention": self.needs_attention,
            "running": self.running,
            "absent_runs": self.absent_runs,
            "unconfirmed_runs": self.unconfirmed_runs,
            "history_entries": self.history_entries,
            "connectors": self.connectors,
            "plans": self.plans,
            "absent_plans": self.absent_plans,
            "unconfirmed_plans": self.unconfirmed_plans,
        }
        document.update(self.extra)
        return document


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    return int(value) if isinstance(value, (int, float)) else None


def _operation_record(row: sqlite3.Row | None) -> dict[str, Any]:
    """One journal row as a record, with the columns it never filled omitted.

    Omitting nulls rather than carrying them keeps ``dispatched_at`` absent on
    an operation nobody has collected, so "never dispatched" reads the same in
    the record as it does in the status.
    """
    if row is None:  # pragma: no cover - callers re-read a row they just wrote
        raise StoreError("the operation vanished inside its own transaction")
    return {
        column: row[column] for column in _OPERATION_COLUMNS if row[column] is not None
    }


def _decoded_list(value: Any) -> list[Any] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except ValueError:
        return None
    return decoded if isinstance(decoded, list) else None


def _decoded_mapping(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except ValueError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _warning_list(value: Any) -> tuple[str, ...]:
    decoded = _decoded_list(value) or []
    return tuple(entry for entry in decoded if isinstance(entry, str))[:MAX_WARNINGS]


def _clamp_limit(limit: Any) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        return DEFAULT_READ_LIMIT
    return max(1, min(limit, MAX_READ_LIMIT))


def _legacy_project(project: str) -> str:
    """Old installations used plan directory names as project names."""
    if re.search(r"[0-9a-f]{12}-[0-9a-f]{12}$", project) or project == "unnamed plan":
        return "Unknown project"
    return project


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class WorkbenchStore:
    """Every read and write this application performs, over one connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        policy: ObservationPolicy,
        owns_connection: bool = False,
        created_at: float = 0.0,
    ) -> None:
        self._connection = connection
        self._policy = require_policy(policy)
        self._owns = owns_connection
        self._lock = threading.RLock()
        self._connection.row_factory = sqlite3.Row
        # A connection handed in by a test may still be in Python's implicit
        # transaction mode; the transaction helper below owns every BEGIN.
        self._connection.isolation_level = None
        self.schema_version = migrate(self._connection, created_at=created_at)

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        policy: ObservationPolicy,
        timeout: float = 5.0,
        created_at: float = 0.0,
    ) -> WorkbenchStore:
        """Open, migrate, and own one database file."""
        connection = connect(path, timeout=timeout)
        try:
            return cls(
                connection,
                policy=policy,
                owns_connection=True,
                created_at=created_at,
            )
        except BaseException:
            connection.close()
            raise

    def close(self) -> None:
        if self._owns:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    @property
    def policy(self) -> ObservationPolicy:
        return self._policy

    # -- transactions ----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One all-or-nothing unit of work, with the write lock taken up front.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: the writes
        below read what is already stored before deciding what to write, and a
        deferred transaction would take the write lock only at the first write,
        after the decision was made against a snapshot another writer could
        have replaced.
        """
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection, held against concurrent writers, for reads only."""
        with self._lock:
            yield self._connection

    def meta(self, key: str, default: Any = None) -> Any:
        """One decoded service-metadata value."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM service_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_meta(self, key: str, value: Any) -> None:
        """Write one service-metadata value, inside whatever transaction is open."""
        with self._lock:
            self._connection.execute(
                "INSERT INTO service_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    @property
    def created_at(self) -> float:
        value = self.meta(META_CREATED_AT, 0.0)
        return float(value) if isinstance(value, (int, float)) else 0.0

    # -- labelling -------------------------------------------------------
    def _plan_labels(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Every plan's label, title and project membership, from the catalog.

        Read from ``catalog_entry`` rather than from the run rows: the catalog
        is the enumeration that knows what a plan is called, and a run summary
        only ever carried a guess.
        """
        labels: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._connection.execute(
            "SELECT connector_id, plan_key, display_label, title, projects, "
            "project_group FROM catalog_entry"
        ):
            labels[(str(row["connector_id"]), str(row["plan_key"]))] = {
                "display_label": row["display_label"],
                "title": row["title"],
                "projects": [
                    str(name) for name in _decoded_list(row["projects"]) or []
                ],
                "project_group": row["project_group"],
            }
        return labels

    @staticmethod
    def _label_record(
        record: dict[str, Any], labels: Mapping[tuple[str, str], dict[str, Any]]
    ) -> None:
        metadata = labels.get((str(record["connector_id"]), str(record["plan_key"])))
        if metadata and metadata["projects"]:
            record["project"] = " + ".join(metadata["projects"])[:MAX_LABEL_CHARS]
            record["plan_label"] = metadata["display_label"]
            if metadata["title"]:
                record["plan_title"] = metadata["title"]
        else:
            record["project"] = _legacy_project(str(record["project"]))

    @staticmethod
    def _project_names(
        record: Mapping[str, Any], labels: Mapping[tuple[str, str], dict[str, Any]]
    ) -> list[str]:
        metadata = labels.get((str(record["connector_id"]), str(record["plan_key"])))
        if metadata and metadata["projects"]:
            return list(metadata["projects"])
        return [_legacy_project(str(record["project"]))]

    def _project_clause(
        self,
        project: str,
        parameters: list[Any],
        labels: Mapping[tuple[str, str], dict[str, Any]],
        *,
        prefix: str = "",
    ) -> str:
        memberships: dict[str, set[str]] = {}
        for row in self._connection.execute(
            "SELECT DISTINCT connector_id, plan_key, project FROM run_summary "
            "UNION SELECT DISTINCT connector_id, plan_key, project FROM history_entry"
        ):
            if project in self._project_names(dict(row), labels):
                memberships.setdefault(str(row["connector_id"]), set()).add(
                    str(row["plan_key"])
                )
        return _membership_clause(memberships, parameters, prefix=prefix)

    @staticmethod
    def _title_clause(
        search: str,
        parameters: list[Any],
        labels: Mapping[tuple[str, str], dict[str, Any]],
        *,
        prefix: str = "",
    ) -> str:
        """Match manifest titles before applying the read limit."""
        needle = search.strip().casefold()
        memberships: dict[str, set[str]] = {}
        for (connector_id, plan_key), metadata in labels.items():
            title = metadata["title"] or ""
            if needle in str(title).casefold() and title:
                memberships.setdefault(connector_id, set()).add(plan_key)
        return _membership_clause(memberships, parameters, prefix=prefix)

    # -- ordering --------------------------------------------------------
    @staticmethod
    def _order(
        *,
        stored_observed_at: Any,
        stored_generation: Any,
        observed_at: float,
        generation: int | None,
    ) -> str:
        """Decide whether one upload supersedes what is already stored.

        Time decides, and the generation only refuses what time cannot. A
        restart resets the generation counter while the connector id stays the
        same, so a lower generation carrying a *newer* observation is a
        restarted source rather than a replay -- refusing it would leave a
        restarted Mac permanently unable to report.
        """
        if stored_observed_at is None:
            return _ORDER_NEWER
        if observed_at > float(stored_observed_at):
            return _ORDER_NEWER
        if (
            generation is not None
            and stored_generation is not None
            and generation < int(stored_generation)
        ):
            return REFUSED_STALE_GENERATION
        return REFUSED_STALE_OBSERVATION

    def _connector_row(self, connector_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM connector WHERE connector_id = ?", (connector_id,)
        ).fetchone()

    def _ensure_connector(self, connector_id: str, *, now: float) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO connector (connector_id, first_seen_at) "
            "VALUES (?, ?)",
            (connector_id, now),
        )

    # -- ingestion -------------------------------------------------------
    def ingest_heartbeat(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Record transport liveness. Says nothing about the projection."""
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        observed_at = float(payload["observed_at"])
        generation = _as_int(payload.get("source_generation"))
        with self.transaction() as connection:
            row = self._connector_row(connector_id)
            if row is not None and row["heartbeat_observed_at"] is not None:
                if observed_at == float(row["heartbeat_observed_at"]):
                    # The same instant from the same source is a retry of one
                    # heartbeat, not a second one.
                    return IngestResult(
                        kind=KIND_HEARTBEAT, connector_id=connector_id, duplicate=True
                    )
                order = self._order(
                    stored_observed_at=row["heartbeat_observed_at"],
                    stored_generation=row["heartbeat_generation"],
                    observed_at=observed_at,
                    generation=generation,
                )
                if order != _ORDER_NEWER:
                    raise IngestRefused(
                        order,
                        "a newer heartbeat is already recorded for this connector",
                    )
            self._ensure_connector(connector_id, now=received_at)
            connection.execute(
                """
                UPDATE connector SET
                    heartbeat_observed_at = ?,
                    heartbeat_received_at = ?,
                    heartbeat_generation = ?,
                    bridge_alive = ?,
                    bridge_protocol = ?,
                    contract_set_id = ?,
                    asl_version = ?,
                    degraded_reason = ?,
                    heartbeat_projection_age_seconds = ?
                WHERE connector_id = ?
                """,
                (
                    observed_at,
                    received_at,
                    generation,
                    1 if payload.get("bridge_alive") else 0,
                    _as_int(payload.get("bridge_protocol")),
                    payload.get("contract_set_id"),
                    payload.get("asl_version"),
                    payload.get("degraded_reason"),
                    payload.get("projection_age_seconds"),
                    connector_id,
                ),
            )
            pruned = self._maybe_prune(now=received_at)
        return IngestResult(
            kind=KIND_HEARTBEAT, connector_id=connector_id, pruned=pruned
        )

    def ingest_snapshot(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Replace the projection for one source, wholly or not at all."""
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        observed_at = float(payload["observed_at"])
        generation = _as_int(payload.get("source_generation"))
        digest = str(payload["digest"])
        truncated = bool(payload["truncated"])
        runs = list(payload["runs"])

        with self.transaction() as connection:
            row = self._connector_row(connector_id)
            if row is not None and row["snapshot_observed_at"] is not None:
                if (
                    observed_at == float(row["snapshot_observed_at"])
                    and digest == row["snapshot_digest"]
                ):
                    # Byte-identical redelivery. The source retried an upload
                    # whose response it never saw, and a retry must not disturb
                    # what the first delivery already established.
                    return IngestResult(
                        kind=KIND_SNAPSHOT, connector_id=connector_id, duplicate=True
                    )
                order = self._order(
                    stored_observed_at=row["snapshot_observed_at"],
                    stored_generation=row["snapshot_generation"],
                    observed_at=observed_at,
                    generation=generation,
                )
                if order != _ORDER_NEWER:
                    raise IngestRefused(
                        order,
                        "a newer snapshot is already recorded for this connector",
                    )
            self._ensure_connector(connector_id, now=received_at)

            previously_present = {
                str(existing["run_key"])
                for existing in connection.execute(
                    "SELECT run_key FROM run_summary "
                    "WHERE connector_id = ? AND present = 1",
                    (connector_id,),
                )
            }
            if not truncated:
                # A whole observation is the only kind that can say a run is
                # gone. Every row is cleared first and then re-marked by the
                # upserts below, so absence is decided by this snapshot alone.
                connection.execute(
                    "UPDATE run_summary SET present = 0, "
                    "absent_since = COALESCE(absent_since, ?) "
                    "WHERE connector_id = ? AND present = 1",
                    (received_at, connector_id),
                )

            seen: set[str] = set()
            for index, run in enumerate(runs):
                run_key = str(run["run_key"])
                if run_key in seen:
                    # Refused mid-write on purpose: the transaction is what
                    # makes this safe, and a snapshot that counts one run twice
                    # would otherwise leave a row set no observation produced.
                    raise IngestInvalid(f"snapshot.runs[{index}] repeats run {run_key}")
                seen.add(run_key)
                self._write_run(
                    connection,
                    run,
                    connector_id=connector_id,
                    observed_at=observed_at,
                    received_at=received_at,
                    generation=generation,
                )

            retired = self._retire_superseded_runs(
                connection,
                connector_id,
                runs,
                seen=seen,
                previously_present=previously_present,
                received_at=received_at,
            )

            connection.execute(
                """
                UPDATE connector SET
                    snapshot_observed_at = ?,
                    snapshot_received_at = ?,
                    snapshot_generation = ?,
                    snapshot_digest = ?,
                    snapshot_source_digest = ?,
                    snapshot_contract_set_id = ?,
                    snapshot_row_count = ?,
                    snapshot_truncated = ?,
                    snapshot_failed_projections = ?,
                    snapshot_projection_age_seconds = ?,
                    snapshot_staleness_bound_seconds = ?,
                    snapshot_warnings = ?
                WHERE connector_id = ?
                """,
                (
                    observed_at,
                    received_at,
                    generation,
                    digest,
                    payload.get("source_digest"),
                    payload.get("contract_set_id"),
                    int(payload["row_count"]),
                    1 if truncated else 0,
                    _as_int(payload.get("failed_projections")),
                    payload.get("projection_age_seconds"),
                    payload.get("staleness_bound_seconds"),
                    json.dumps(list(payload.get("warnings") or [])),
                    connector_id,
                ),
            )
            incidents.observe_snapshot(
                connection, policy=self._policy, envelope=envelope, now=received_at
            )
            pruned = self._maybe_prune(now=received_at)
        absent = len(retired) if truncated else len(previously_present - seen)
        return IngestResult(
            kind=KIND_SNAPSHOT,
            connector_id=connector_id,
            stored=len(seen),
            absent=absent,
            pruned=pruned,
        )

    def _retire_superseded_runs(
        self,
        connection: sqlite3.Connection,
        connector_id: str,
        runs: Sequence[Mapping[str, Any]],
        *,
        seen: set[str],
        previously_present: set[str],
        received_at: float,
    ) -> set[str]:
        """Retire runs a newer run for the same plan positively replaced.

        Unlike omission, a newer run for the same plan is positive replacement
        evidence, and it holds even when the observation that carried it was
        capped: the new run's existence is a fact the snapshot asserted rather
        than a gap it left.
        """
        retired: set[str] = set()
        for run in runs:
            if not run.get("run_id"):
                continue
            existing = connection.execute(
                "SELECT run_key, run_id, run_started_at, observed_at FROM run_summary "
                "WHERE connector_id = ? AND plan_key = ?",
                (connector_id, run["plan_key"]),
            ).fetchall()
            for old in existing:
                if str(old["run_key"]) in seen or old["run_id"] == run["run_id"]:
                    continue
                started = run.get("run_started_at")
                # An unidentified predecessor is always superseded; an
                # identified one only by a demonstrably later start. A run
                # whose start is older, or unknown, is not evidence at all.
                previous_at = (
                    old["run_started_at"]
                    if old["run_started_at"] is not None
                    else old["observed_at"]
                )
                supersedes = old["run_id"] is None or (
                    started is not None and started > previous_at
                )
                if not supersedes:
                    continue
                if str(old["run_key"]) in previously_present:
                    retired.add(str(old["run_key"]))
                connection.execute(
                    "UPDATE run_summary SET present = 0, "
                    "absent_since = COALESCE(absent_since, ?) "
                    "WHERE connector_id = ? AND run_key = ?",
                    (received_at, connector_id, old["run_key"]),
                )
                incidents.retire_run(
                    connection, connector_id, str(old["run_key"]), received_at
                )
        return retired

    def _write_run(
        self,
        connection: sqlite3.Connection,
        run: Mapping[str, Any],
        *,
        connector_id: str,
        observed_at: float,
        received_at: float,
        generation: int | None,
    ) -> None:
        """Upsert one run row, through the persistence allowlist.

        The allowlist is applied to the wire row rather than the row being
        trusted: the allowlist, not this schema, is what decides that a widened
        snapshot shape cannot widen what is kept.
        """
        record = persist_run_summary({**run, "observed_at": observed_at})
        values = {column: record.get(column) for column in _RUN_COLUMNS}
        values.update(
            {
                "connector_id": connector_id,
                "run_key": record["run_key"],
                "observed_at": observed_at,
                "received_at": received_at,
                "source_generation": generation,
                "present": 1,
                "absent_since": None,
            }
        )
        connection.execute(
            _upsert_statement("run_summary", _RUN_COLUMNS, ("connector_id", "run_key")),
            values,
        )

    def ingest_history(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Upsert one history batch. Redelivery changes nothing.

        History has no generation ordering, because a batch is not a view of
        the corpus: entries are keyed by the run they describe and a run's
        ending does not change. Only the batch *metadata* is ordered, so a late
        batch cannot overwrite a newer import's completeness claim.
        """
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        entries = list(payload["entries"])
        generated_at = float(payload["generated_at"])

        with self.transaction() as connection:
            self._ensure_connector(connector_id, now=received_at)
            row = self._connector_row(connector_id)
            existing = {
                str(found["entry_id"])
                for found in connection.execute(
                    "SELECT entry_id FROM history_entry WHERE connector_id = ?",
                    (connector_id,),
                )
            }
            stored = 0
            updated = 0
            for entry in entries:
                record = persist_history_entry(entry)
                if str(record["entry_id"]) in existing:
                    updated += 1
                else:
                    stored += 1
                self._write_history_entry(
                    connection,
                    record,
                    connector_id=connector_id,
                    received_at=received_at,
                )
                incidents.observe_history(
                    connection,
                    policy=self._policy,
                    connector_id=connector_id,
                    entry=record,
                    now=received_at,
                )
            recorded_generated_at = row["history_generated_at"] if row else None
            if recorded_generated_at is None or generated_at >= float(
                recorded_generated_at
            ):
                connection.execute(
                    """
                    UPDATE connector SET
                        history_batch_id = ?,
                        history_generated_at = ?,
                        history_received_at = ?,
                        history_window_start = ?,
                        history_window_end = ?,
                        history_complete = ?,
                        history_more = ?
                    WHERE connector_id = ?
                    """,
                    (
                        payload.get("batch_id"),
                        generated_at,
                        received_at,
                        payload.get("window_start"),
                        payload.get("window_end"),
                        1 if payload.get("complete") else 0,
                        1 if payload.get("more") else 0,
                        connector_id,
                    ),
                )
            pruned = self._maybe_prune(now=received_at)
        return IngestResult(
            kind=KIND_HISTORY_BATCH,
            connector_id=connector_id,
            stored=stored,
            updated=updated,
            duplicate=stored == 0 and updated > 0,
            pruned=pruned,
        )

    def _write_history_entry(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        *,
        connector_id: str,
        received_at: float,
    ) -> None:
        values = {column: record.get(column) for column in _HISTORY_COLUMNS}
        values["connector_id"] = connector_id
        values["received_at"] = received_at
        ids = record.get("completed_session_ids")
        values["completed_session_ids"] = None if ids is None else json.dumps(list(ids))
        for column in _HISTORY_BOOLEANS:
            if values[column] is not None:
                values[column] = 1 if values[column] else 0
        connection.execute(
            _upsert_statement(
                "history_entry", _HISTORY_COLUMNS, ("connector_id", "entry_id")
            ),
            values,
        )

    def ingest_catalog(
        self, envelope: Mapping[str, Any], *, received_at: float
    ) -> IngestResult:
        """Apply one page of one catalog generation.

        Three facts travel on every page and all three are load-bearing. The
        ``generation`` says which enumeration this page belongs to, so pages
        from two overlapping scans never mix. ``complete`` says whether that
        enumeration could see everything, so a generation built while a plan
        was mid-write never retires anything. ``final`` says whether the
        generation has finished arriving, so a complete generation does not
        retire the plans its later pages were going to carry.

        The ordering rule is the snapshot's: observation time decides, and the
        source generation counter only refuses what time cannot.
        """
        connector_id = str(envelope["connector_id"])
        payload = envelope["payload"]
        observed_at = float(payload["observed_at"])
        generation = str(payload["generation"])
        source_generation = _as_int(payload.get("source_generation"))
        complete = bool(payload.get("complete", False))
        final = bool(payload.get("final", False))
        offset = max(0, int(payload.get("offset", 0)))
        total = _as_int(payload.get("total"))
        entries = list(payload["entries"])

        with self.transaction() as connection:
            self._ensure_connector(connector_id, now=received_at)
            row = self._connector_row(connector_id)
            if row is None:  # pragma: no cover - inserted immediately above
                raise StoreError("the connector vanished inside its own transaction")
            delivered = _as_int(row["catalog_delivered"]) or 0
            if row["catalog_generation"] == generation:
                duplicate = offset + len(entries) <= delivered
                delivered = max(delivered, offset + len(entries))
                observed_at = max(
                    observed_at, float(row["catalog_observed_at"] or observed_at)
                )
                already_final = bool(row["catalog_final"])
            else:
                order = self._order(
                    stored_observed_at=row["catalog_observed_at"],
                    stored_generation=row["catalog_source_generation"],
                    observed_at=observed_at,
                    generation=source_generation,
                )
                if order != _ORDER_NEWER:
                    raise IngestRefused(
                        order,
                        "a newer catalog generation is already recorded for "
                        "this connector",
                    )
                duplicate = False
                delivered = offset + len(entries)
                already_final = False

            known = {
                str(found["plan_key"])
                for found in connection.execute(
                    "SELECT plan_key FROM catalog_entry WHERE connector_id = ?",
                    (connector_id,),
                )
            }
            stored = 0
            updated = 0
            seen: set[str] = set()
            for index, entry in enumerate(entries):
                plan_key = str(entry["plan_key"])
                if plan_key in seen:
                    raise IngestInvalid(
                        f"catalog.entries[{index}] repeats plan {plan_key}"
                    )
                seen.add(plan_key)
                if plan_key in known:
                    updated += 1
                else:
                    stored += 1
                self._write_catalog_entry(
                    connection,
                    entry,
                    connector_id=connector_id,
                    generation=generation,
                    observed_at=observed_at,
                    received_at=received_at,
                )

            absent = 0
            if complete and final:
                # Only here. A partial generation never proves a plan is gone,
                # and a complete generation that is still arriving has not yet
                # said what it carries.
                absent = max(
                    0,
                    connection.execute(
                        "UPDATE catalog_entry SET present = 0, "
                        "absent_since = COALESCE(absent_since, ?) "
                        "WHERE connector_id = ? AND present = 1 AND generation != ?",
                        (received_at, connector_id, generation),
                    ).rowcount,
                )

            connection.execute(
                """
                UPDATE connector SET
                    catalog_generation = ?,
                    catalog_observed_at = ?,
                    catalog_received_at = ?,
                    catalog_source_generation = ?,
                    catalog_total = ?,
                    catalog_delivered = ?,
                    catalog_complete = ?,
                    catalog_final = ?
                WHERE connector_id = ?
                """,
                (
                    generation,
                    observed_at,
                    received_at,
                    source_generation,
                    total,
                    delivered,
                    1 if complete else 0,
                    1 if (final or already_final) else 0,
                    connector_id,
                ),
            )
            pruned = self._maybe_prune(now=received_at)
        return IngestResult(
            kind=KIND_CATALOG_PAGE,
            connector_id=connector_id,
            duplicate=duplicate,
            stored=stored,
            updated=updated,
            absent=absent,
            pruned=pruned,
        )

    def _write_catalog_entry(
        self,
        connection: sqlite3.Connection,
        entry: Mapping[str, Any],
        *,
        connector_id: str,
        generation: str,
        observed_at: float,
        received_at: float,
    ) -> None:
        record = persist_catalog_entry(entry)
        values: dict[str, Any] = {
            column: record.get(column) for column in _CATALOG_COLUMNS
        }
        for column in _CATALOG_JSON:
            payload = record.get(column)
            values[column] = json.dumps(
                list(payload)
                if isinstance(payload, (list, tuple))
                else dict(payload)
                if isinstance(payload, Mapping)
                else []
            )
        for column in _CATALOG_BOOLEANS:
            if values[column] is not None:
                values[column] = 1 if values[column] else 0
        values.update(
            {
                "connector_id": connector_id,
                "plan_key": record["plan_key"],
                "display_label": record.get("display_label") or record["plan_key"],
                "project_group": record.get("project_group") or "",
                "generation": generation,
                "observed_at": observed_at,
                "received_at": received_at,
                "present": 1,
                "absent_since": None,
            }
        )
        connection.execute(
            _upsert_statement("catalog_entry", _CATALOG_COLUMNS, _CATALOG_IDENTITY),
            values,
        )

    # -- retention -------------------------------------------------------
    def _maybe_prune(self, *, now: float) -> int:
        last = self.meta(META_LAST_PRUNED_AT)
        if (
            isinstance(last, (int, float))
            and now - float(last) < PRUNE_INTERVAL_SECONDS
        ):
            return 0
        return self._prune(now=now)

    def _prune(self, *, now: float) -> int:
        """Delete everything that has aged out of the retention window.

        Runs and history share one cutoff, because both answer the same
        operator question -- what happened in the last seven days -- and a run
        summary that outlived its history row would be a row with no ending.

        Catalog entries are not a time series and do not age out while they are
        present: a plan that has existed untouched for a year is still a plan.
        Only a retired entry expires, measured from when it went absent.
        """
        cutoff = now - self._policy.summary_retention_seconds
        runs = self._connection.execute(
            "DELETE FROM run_summary WHERE observed_at < ?", (cutoff,)
        ).rowcount
        entries = self._connection.execute(
            "DELETE FROM history_entry WHERE recorded_at < ?", (cutoff,)
        ).rowcount
        plans = self._connection.execute(
            "DELETE FROM catalog_entry WHERE present = 0 AND absent_since < ?",
            (cutoff,),
        ).rowcount
        # Only settled operations age out. A journal row that is still open is
        # the record of a mutation whose outcome nobody established, and
        # deleting it would both erase that admission and silently release the
        # one-outstanding-mutation hold that keeps a second attempt from
        # running against an unknown first one.
        marks = ", ".join("?" * len(OPERATION_TERMINAL_STATUSES))
        operations = self._connection.execute(
            f"DELETE FROM operation WHERE requested_at < ? AND status IN ({marks})",
            (cutoff, *sorted(OPERATION_TERMINAL_STATUSES)),
        ).rowcount
        self.set_meta(META_LAST_PRUNED_AT, now)
        return (
            max(0, runs)
            + max(0, entries)
            + max(0, plans)
            + max(0, operations)
            + incidents.prune(self._connection, policy=self._policy, now=now)
        )

    def prune(self, *, now: float) -> int:
        """Run the retention pass regardless of when the last one ran."""
        with self.transaction():
            return self._prune(now=now)

    # -- operation journal -----------------------------------------------
    def record_operation(
        self, record: Mapping[str, Any], *, connector_id: str, received_at: float
    ) -> dict[str, Any]:
        """Write one operation, or return the one this id already names.

        A repeated operation id is the same operation and never a second one.
        That is the whole of the idempotence guarantee an operator's browser
        relies on: a retried submission, a double-clicked button, and a reload
        that resends the form all converge on one row rather than on three
        mutations of one run.
        """
        projected = persist_operation(record)
        with self.transaction() as connection:
            existing = self._operation_row(str(projected["operation_id"]))
            if existing is not None:
                return _operation_record(existing)
            values = {column: projected.get(column) for column in _OPERATION_COLUMNS}
            values["connector_id"] = connector_id
            values["received_at"] = received_at
            values["updated_at"] = received_at
            connection.execute(
                "INSERT INTO operation ({columns}) VALUES ({marks})".format(
                    columns=", ".join(_OPERATION_COLUMNS),
                    marks=", ".join("?" * len(_OPERATION_COLUMNS)),
                ),
                [values[column] for column in _OPERATION_COLUMNS],
            )
            return _operation_record(
                self._operation_row(str(projected["operation_id"]))
            )

    def advance_operation(
        self,
        operation_id: str,
        status: str,
        *,
        now: float,
        detail_code: str | None = None,
        dispatched_at: float | None = None,
        settled_at: float | None = None,
    ) -> dict[str, Any]:
        """Move one operation to ``status``, refusing a forbidden transition.

        The legality check and the write share one transaction. Checking
        outside it would let two callers each read ``pending`` and each decide
        they were the one dispatching it.
        """
        with self.transaction() as connection:
            row = self._operation_row(operation_id)
            if row is None:
                raise OperationError(
                    REASON_UNKNOWN_OPERATION, f"unknown operation: {operation_id!r}"
                )
            current = str(row["status"])
            if current == status:
                # Re-reporting the state an operation is already in is a
                # retried delivery of one fact, not an illegal move.
                return _operation_record(row)
            next_status(current, status)
            connection.execute(
                "UPDATE operation SET status = ?, "
                "detail_code = COALESCE(?, detail_code), "
                "dispatched_at = COALESCE(?, dispatched_at), "
                "settled_at = COALESCE(?, settled_at), updated_at = ? "
                "WHERE operation_id = ?",
                (status, detail_code, dispatched_at, settled_at, now, operation_id),
            )
            return _operation_record(self._operation_row(operation_id))

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        """One journal row, or ``None``."""
        with self.reading():
            row = self._operation_row(operation_id)
        return None if row is None else _operation_record(row)

    def outstanding_operation(self, plan_key: str) -> dict[str, Any] | None:
        """The operation that blocks a second mutation on this plan, if any."""
        marks = ", ".join("?" * len(OPERATION_OUTSTANDING_STATUSES))
        with self.reading() as connection:
            row = connection.execute(
                f"SELECT * FROM operation WHERE plan_key = ? AND status IN ({marks}) "
                "ORDER BY requested_at ASC LIMIT 1",
                (plan_key, *sorted(OPERATION_OUTSTANDING_STATUSES)),
            ).fetchone()
        return None if row is None else _operation_record(row)

    def operations_in_status(
        self, status: str, *, connector_id: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Every operation in one state, oldest request first."""
        query = "SELECT * FROM operation WHERE status = ?"
        parameters: list[Any] = [status]
        if connector_id is not None:
            query += " AND connector_id = ?"
            parameters.append(connector_id)
        query += " ORDER BY requested_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(max(0, int(limit)))
        with self.reading() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_operation_record(row) for row in rows]

    def exported_operations(
        self, *, now: float, limit: int = DEFAULT_READ_LIMIT
    ) -> list[dict[str, Any]]:
        """What a cockpit may read back about recent operations."""
        cutoff = now - self._policy.summary_retention_seconds
        bound = _clamp_limit(limit)
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM operation WHERE requested_at >= ? "
                "ORDER BY requested_at DESC LIMIT ?",
                (cutoff, bound),
            ).fetchall()
        return [export_operation(_operation_record(row)) for row in rows]

    def command_target(self, run_key: str) -> dict[str, Any] | None:
        """Identifiers for a run in the newest snapshot, or ``None``.

        ``plan_key`` is in here and in nothing a cockpit reads: the browser
        names a run, and the service resolves the plan that run belongs to, so
        the one-outstanding-mutation rule is enforced against an identifier the
        client never chose.
        """
        wanted = (
            "connector_id",
            "run_key",
            "plan_key",
            "run_id",
            "question_id",
            "primary_state",
            "observed_at",
        )
        with self.reading() as connection:
            row = connection.execute(
                f"SELECT {', '.join('r.' + name for name in wanted)} "
                "FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                "WHERE r.run_key = ? AND r.present = 1 "
                "AND r.observed_at = c.snapshot_observed_at "
                "ORDER BY r.observed_at DESC LIMIT 1",
                (run_key,),
            ).fetchone()
        if row is None:
            return None
        return {name: row[name] for name in wanted if row[name] is not None}

    def _operation_row(self, operation_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM operation WHERE operation_id = ?", (operation_id,)
        ).fetchone()

    # -- reads -----------------------------------------------------------
    def connector_statuses(self, *, now: float) -> list[ConnectorStatus]:
        """Evaluate every source's health, each part separately."""
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM connector ORDER BY connector_id"
            ).fetchall()
        return [self._connector_status(row, now=now) for row in rows]

    def _connector_status(self, row: sqlite3.Row, *, now: float) -> ConnectorStatus:
        contract_set_id = row["snapshot_contract_set_id"] or row["contract_set_id"]
        compatible = contract_set_id in self._policy.supported_contract_set_ids
        truncated = _as_bool(row["snapshot_truncated"])
        source_at = row["snapshot_observed_at"]
        if source_at is not None:
            source_at -= row["snapshot_projection_age_seconds"] or 0
        evaluated = freshness_module.evaluate(
            now=now,
            policy=self._policy,
            last_heartbeat_at=row["heartbeat_observed_at"],
            last_snapshot_at=source_at,
            truncated=truncated,
            compatible=compatible,
        )
        run_reasons: list[str] = []
        if truncated:
            run_reasons.append("the newest snapshot was truncated")
        failed = _as_int(row["snapshot_failed_projections"]) or 0
        if failed:
            run_reasons.append(f"ASL could not read {failed} plan(s) during its walk")
        if row["snapshot_observed_at"] is None:
            run_reasons.append("no snapshot has been received")

        history_complete = _as_bool(row["history_complete"])
        history_reasons: list[str] = []
        if history_complete is False:
            history_reasons.append(
                "the last history import could not read every source"
            )
        if row["history_generated_at"] is None:
            history_reasons.append("no history has been imported")
        if _as_bool(row["history_more"]):
            history_reasons.append("more history batches were still to come")

        catalog_complete = _as_bool(row["catalog_complete"])
        catalog_final = _as_bool(row["catalog_final"])
        catalog_reasons: list[str] = []
        if catalog_complete is False:
            catalog_reasons.append(
                "the last catalog generation could not see every plan"
            )
        if row["catalog_generation"] is None:
            catalog_reasons.append("no catalog has been received")
        elif not catalog_final:
            catalog_reasons.append("the newest catalog generation is still arriving")
        if catalog_complete and not catalog_final:
            # Complete but unfinished is not complete yet, and reporting it as
            # complete is what would let a half-delivered catalog retire work.
            catalog_complete = None

        return ConnectorStatus(
            connector_id=str(row["connector_id"]),
            freshness=evaluated,
            compatible=compatible,
            contract_set_id=contract_set_id,
            bridge_alive=_as_bool(row["bridge_alive"]),
            degraded_reason=row["degraded_reason"],
            source_generation=_as_int(row["snapshot_generation"]),
            row_count=_as_int(row["snapshot_row_count"]) or 0,
            truncated=truncated,
            warnings=_warning_list(row["snapshot_warnings"]),
            runs=SourceStatus(
                completeness=completeness_of(truncated), reasons=tuple(run_reasons)
            ),
            history=SourceStatus(
                completeness=completeness_of(history_complete, invert=True),
                reasons=tuple(history_reasons),
            ),
            catalog=SourceStatus(
                completeness=completeness_of(catalog_complete, invert=True),
                reasons=tuple(catalog_reasons),
            ),
            last_heartbeat_at=row["heartbeat_observed_at"],
            last_snapshot_at=row["snapshot_observed_at"],
            asl_version=row["asl_version"],
            bridge_protocol=_as_int(row["bridge_protocol"]),
            projection_age_seconds=row["snapshot_projection_age_seconds"],
        )

    def runs(
        self,
        *,
        now: float,
        project: str | None = None,
        limit: int = DEFAULT_READ_LIMIT,
        include_absent: bool = False,
        search: str | None = None,
        harness: str | None = None,
    ) -> list[dict[str, Any]]:
        """The cockpit's rows, each stamped with its source's freshness.

        Only rows the newest observation still carries are returned by default.
        A row the projection dropped is not a row that finished, so it leaves
        the live view and its ending -- if it has one -- arrives through
        history.
        """
        cutoff = now - self._policy.summary_retention_seconds
        clauses = ["observed_at >= ?"]
        parameters: list[Any] = [cutoff]
        if not include_absent:
            clauses.append("present = 1")
        with self.reading() as connection:
            labels = self._plan_labels()
            if search and search.strip():
                clauses.append(
                    self._title_clause(search, parameters, labels, prefix="r.")
                )
            if harness:
                clauses.append("harness = ?")
                parameters.append(harness)
            if project is not None:
                clauses.append(
                    self._project_clause(project, parameters, labels, prefix="r.")
                )
            parameters.append(_clamp_limit(limit))
            statement = (
                "SELECT r.*, CASE "
                f"WHEN r.present = 0 THEN '{OBSERVATION_ABSENT}' "
                "WHEN r.observed_at = c.snapshot_observed_at "
                f"THEN '{OBSERVATION_CURRENT}' "
                f"ELSE '{OBSERVATION_UNCONFIRMED}' END AS observation_status "
                "FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY CASE WHEN r.observed_at = c.snapshot_observed_at "
                "AND primary_state IN ('running', 'verifying') THEN 0 "
                "WHEN r.observed_at = c.snapshot_observed_at THEN 1 ELSE 2 END, "
                "COALESCE(last_activity_at, run_started_at, 0) DESC, "
                "run_key ASC LIMIT ?"
            )
            rows = connection.execute(statement, parameters).fetchall()
        freshness_labels = {
            status.connector_id: status.freshness.label
            for status in self.connector_statuses(now=now)
        }
        exported: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["freshness"] = freshness_labels.get(
                str(row["connector_id"]), freshness_module.FRESHNESS_LOST
            )
            record["last_observed_at"] = row["observed_at"]
            self._label_record(record, labels)
            exported.append(export_run_summary(record))
        return exported

    def history(
        self,
        *,
        now: float,
        project: str | None = None,
        outcome: str | None = None,
        limit: int = DEFAULT_READ_LIMIT,
        search: str | None = None,
        harness: str | None = None,
    ) -> list[dict[str, Any]]:
        """Terminal runs inside the retention window, newest first."""
        cutoff = now - self._policy.summary_retention_seconds
        clauses = ["recorded_at >= ?"]
        parameters: list[Any] = [cutoff]
        with self.reading() as connection:
            labels = self._plan_labels()
            if search and search.strip():
                clauses.append(self._title_clause(search, parameters, labels))
            if harness:
                clauses.append("harness = ?")
                parameters.append(harness)
            if project is not None:
                clauses.append(self._project_clause(project, parameters, labels))
            if outcome is not None:
                clauses.append("outcome = ?")
                parameters.append(outcome)
            parameters.append(_clamp_limit(limit))
            rows = connection.execute(
                f"SELECT * FROM history_entry WHERE {' AND '.join(clauses)} "
                "ORDER BY recorded_at DESC, entry_id ASC LIMIT ?",
                parameters,
            ).fetchall()
        exported: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            for column in _HISTORY_BOOLEANS:
                record[column] = _as_bool(record[column])
            record["completed_session_ids"] = _decoded_list(
                record["completed_session_ids"]
            )
            self._label_record(record, labels)
            exported.append(export_history_entry(record))
        return exported

    def catalog(
        self,
        *,
        now: float,
        connector_id: str | None = None,
        project: str | None = None,
        search: str | None = None,
        include_absent: bool = False,
        offset: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> list[dict[str, Any]]:
        """Stored catalog entries, newest generation first, one page at a time.

        Carried-over Companion labels are never returned here. They belong to
        :data:`LEGACY_GENERATION`, which is not a generation anybody observed:
        they can label a retained run and they cannot claim a plan exists.
        """
        clauses = ["e.generation != ?"]
        parameters: list[Any] = [LEGACY_GENERATION]
        if connector_id is not None:
            clauses.append("e.connector_id = ?")
            parameters.append(connector_id)
        if not include_absent:
            clauses.append("e.present = 1")
        if search and search.strip():
            clauses.append("e.search_text LIKE ?")
            parameters.append(f"%{search.strip().casefold()}%")
        if project is not None:
            clauses.append("e.projects LIKE ?")
            parameters.append(f"%{json.dumps(project)}%")
        parameters.extend([_clamp_limit(limit), max(0, int(offset))])
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT e.*, c.catalog_generation AS current_generation, "
                "c.catalog_complete AS current_complete "
                "FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY e.urgency_rank ASC, e.plan_key ASC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
        exported: list[dict[str, Any]] = []
        for row in rows:
            exported.append(export_catalog_entry(self._catalog_record(row)))
        return exported

    @staticmethod
    def _catalog_record(row: sqlite3.Row) -> dict[str, Any]:
        record: dict[str, Any] = dict(row)
        for column in _CATALOG_BOOLEANS:
            record[column] = _as_bool(record[column])
        record["projects"] = _decoded_list(record["projects"]) or []
        record["sources"] = _decoded_list(record["sources"]) or []
        record["counts"] = _decoded_mapping(record["counts"])
        record["multi_repository"] = (record["repository_count"] or 0) > 1
        record["complete"] = _as_bool(record["current_complete"])
        record["last_observed_at"] = record["observed_at"]
        if not record["present"]:
            record["observation_status"] = OBSERVATION_ABSENT
        elif record["generation"] == record["current_generation"]:
            record["observation_status"] = OBSERVATION_CURRENT
        else:
            record["observation_status"] = OBSERVATION_UNCONFIRMED
        return record

    def harnesses(self, *, now: float) -> list[str]:
        """Harness choices cover retained work, independent of active filters."""
        cutoff = now - self._policy.summary_retention_seconds
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT DISTINCT harness FROM run_summary "
                "WHERE observed_at >= ? AND present = 1 "
                "UNION SELECT DISTINCT harness FROM history_entry "
                "WHERE recorded_at >= ? ORDER BY harness",
                (cutoff, cutoff),
            ).fetchall()
        return [str(row["harness"]) for row in rows if row["harness"]]

    def projects(self, *, now: float) -> list[str]:
        """Every project name a cockpit could filter by, sorted."""
        cutoff = now - self._policy.summary_retention_seconds
        with self.reading() as connection:
            rows = connection.execute(
                "SELECT DISTINCT connector_id, plan_key, project FROM run_summary "
                "WHERE observed_at >= ? AND present = 1 "
                "UNION SELECT DISTINCT connector_id, plan_key, project "
                "FROM history_entry WHERE recorded_at >= ? "
                "ORDER BY project ASC",
                (cutoff, cutoff),
            ).fetchall()
            labels = self._plan_labels()
        names: set[str] = set()
        for row in rows:
            names.update(self._project_names(dict(row), labels))
        return sorted(names)

    def counts(self, *, now: float) -> Counts:
        """Aggregate numbers with no identity attached."""
        cutoff = now - self._policy.summary_retention_seconds
        with self.reading() as connection:
            runs = connection.execute(
                "SELECT r.urgency_group, r.present, "
                "r.observed_at = c.snapshot_observed_at AS current, "
                "COUNT(*) AS total FROM run_summary r "
                "JOIN connector c ON c.connector_id = r.connector_id "
                "WHERE r.observed_at >= ? "
                "GROUP BY r.urgency_group, r.present, current",
                (cutoff,),
            ).fetchall()
            entries = connection.execute(
                "SELECT COUNT(*) AS total FROM history_entry WHERE recorded_at >= ?",
                (cutoff,),
            ).fetchone()
            connectors = connection.execute(
                "SELECT COUNT(*) AS total FROM connector"
            ).fetchone()
            plans = connection.execute(
                "SELECT e.present, e.generation = c.catalog_generation AS current, "
                "COUNT(*) AS total FROM catalog_entry e "
                "JOIN connector c ON c.connector_id = e.connector_id "
                "WHERE e.generation != ? GROUP BY e.present, current",
                (LEGACY_GENERATION,),
            ).fetchall()
        present = needs_attention = running = absent = unconfirmed = 0
        for row in runs:
            total = int(row["total"])
            if not row["present"]:
                absent += total
                continue
            present += total
            if not row["current"]:
                unconfirmed += total
                continue
            if row["urgency_group"] == "needs-action":
                needs_attention += total
            elif row["urgency_group"] == "in-progress":
                running += total
        plan_total = absent_plans = unconfirmed_plans = 0
        for row in plans:
            total = int(row["total"])
            if not row["present"]:
                absent_plans += total
                continue
            plan_total += total
            if not row["current"]:
                unconfirmed_plans += total
        return Counts(
            runs=present,
            needs_attention=needs_attention,
            running=running,
            absent_runs=absent,
            unconfirmed_runs=unconfirmed,
            history_entries=int(entries["total"]),
            connectors=int(connectors["total"]),
            plans=plan_total,
            absent_plans=absent_plans,
            unconfirmed_plans=unconfirmed_plans,
        )

    def columns(self, table: str) -> tuple[str, ...]:
        """The column names of one table, for tests that check the schema."""
        with self.reading() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def healthy(self) -> bool:
        """Whether the database still answers. The liveness endpoint's question."""
        try:
            with self.reading() as connection:
                connection.execute("SELECT 1 FROM service_meta LIMIT 1").fetchall()
        except sqlite3.Error:
            return False
        return True


def _upsert_statement(
    table: str, columns: Sequence[str], identity: Sequence[str]
) -> str:
    """One named-parameter upsert, with every identifier quoted."""
    assignments = ", ".join(
        f'"{column}" = excluded."{column}"'
        for column in columns
        if column not in identity
    )
    return (
        f"INSERT INTO {table} ({_quoted(columns)}) "
        f"VALUES ({', '.join(f':{column}' for column in columns)}) "
        f"ON CONFLICT({_quoted(identity)}) DO UPDATE SET {assignments}"
    )


def _membership_clause(
    memberships: Mapping[str, set[str]], parameters: list[Any], *, prefix: str = ""
) -> str:
    """A SQL clause matching exactly these ``(connector, plan)`` pairs.

    ``0`` when there are none: a filter that matched nothing must return
    nothing, and an empty ``OR`` chain would return everything.
    """
    matches: list[str] = []
    for connector_id in sorted(memberships):
        keys = sorted(memberships[connector_id])
        marks = ",".join("?" for _ in keys)
        matches.append(f"({prefix}connector_id = ? AND {prefix}plan_key IN ({marks}))")
        parameters.extend([connector_id, *keys])
    return "(" + " OR ".join(matches) + ")" if matches else "0"


__all__ = [
    "COMPANION_SCHEMA_VERSION",
    "DEFAULT_READ_LIMIT",
    "INGEST_REFUSAL_CODES",
    "KIND_CATALOG_PAGE",
    "KIND_HEARTBEAT",
    "KIND_HISTORY_BATCH",
    "KIND_SNAPSHOT",
    "LEGACY_GENERATION",
    "MAX_READ_LIMIT",
    "MIGRATIONS",
    "PRUNE_INTERVAL_SECONDS",
    "REFUSED_STALE_GENERATION",
    "REFUSED_STALE_OBSERVATION",
    "SCHEMA_BOOKKEEPING",
    "SCHEMA_VERSION",
    "ConnectorStatus",
    "Counts",
    "IngestInvalid",
    "IngestRefused",
    "IngestResult",
    "SourceStatus",
    "StoreError",
    "WorkbenchStore",
    "connect",
    "migrate",
    "schema_version",
]
