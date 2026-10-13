"""The migration ledger: schema statements, hooks, and the connection opener."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from . import incidents
from .observation import (
    MAX_LABEL_CHARS,
)
from .store_types import (
    LEGACY_GENERATION,
    META_CREATED_AT,
    META_PROJECT_CATALOG_PREFIX,
    SCHEMA_VERSION,
    StoreError,
)

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
