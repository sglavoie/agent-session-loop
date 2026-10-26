"""The overseer ledger's schema, migrations, and connection setup.

Migrations are appended and never edited; :func:`migrate` replays the steps a
database has not seen, one transaction each, and refuses a database written by
a newer build.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path

from .overseer_store_models import LedgerError

Migration = Callable[[sqlite3.Connection], None]


#: Schema version 1, one statement per entry.
#:
#: Separate statements rather than one script because ``executescript`` ends
#: whatever transaction is open before it runs, which would commit the tables
#: without the ``user_version`` bump that says they exist -- exactly the torn
#: migration the version counter is there to prevent.
_BASE_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE policy_epoch (
        id                  INTEGER PRIMARY KEY CHECK (id = 1),
        epoch               INTEGER NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        observed_at         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE enrollment (
        plan_dir            TEXT    PRIMARY KEY,
        enrolled            INTEGER NOT NULL,
        revoked             INTEGER NOT NULL,
        epoch               INTEGER NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        access              TEXT    NOT NULL,
        request_digest      TEXT    NOT NULL,
        last_failure        TEXT,
        first_seen_at       TEXT    NOT NULL,
        observed_at         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE observation_cursor (
        plan_dir     TEXT    NOT NULL,
        stream       TEXT    NOT NULL,
        position     INTEGER NOT NULL,
        marker       TEXT    NOT NULL,
        observed_at  TEXT    NOT NULL,
        PRIMARY KEY (plan_dir, stream)
    )
    """,
    """
    CREATE TABLE incident (
        incident_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_dir            TEXT    NOT NULL,
        run_id              TEXT    NOT NULL,
        kind                TEXT    NOT NULL,
        status              TEXT    NOT NULL,
        summary             TEXT    NOT NULL,
        epoch               INTEGER NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        opened_at           TEXT    NOT NULL,
        settled_at          TEXT
    )
    """,
    """
    CREATE TABLE evidence (
        evidence_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id         INTEGER NOT NULL
                            REFERENCES incident (incident_id) ON DELETE CASCADE,
        plan_dir            TEXT    NOT NULL,
        alias               TEXT    NOT NULL,
        kind                TEXT    NOT NULL,
        locator             TEXT    NOT NULL,
        digest              TEXT    NOT NULL,
        epoch               INTEGER NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        sealed              INTEGER NOT NULL,
        recorded_at         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE decision (
        decision_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_dir            TEXT    NOT NULL,
        access              TEXT    NOT NULL,
        allowed             INTEGER NOT NULL,
        available           INTEGER NOT NULL,
        failure             TEXT,
        reason              TEXT    NOT NULL,
        summary             TEXT    NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        request_digest      TEXT    NOT NULL,
        recorded_at         TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE action (
        action_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_dir            TEXT    NOT NULL,
        incident_id         INTEGER REFERENCES incident (incident_id),
        operation           TEXT    NOT NULL,
        target              TEXT    NOT NULL,
        status              TEXT    NOT NULL,
        epoch               INTEGER NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        request_digest      TEXT    NOT NULL,
        attempted_at        TEXT    NOT NULL,
        settled_at          TEXT
    )
    """,
    """
    CREATE TABLE receipt (
        receipt_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        action_id     INTEGER NOT NULL UNIQUE
                      REFERENCES action (action_id) ON DELETE CASCADE,
        outcome       TEXT    NOT NULL,
        detail        TEXT    NOT NULL,
        locator       TEXT    NOT NULL,
        confirmed_at  TEXT    NOT NULL
    )
    """,
    "CREATE INDEX evidence_by_plan ON evidence (plan_dir, sealed)",
    "CREATE INDEX incident_by_plan ON incident (plan_dir, status)",
    "CREATE INDEX action_by_status ON action (status, action_id)",
    "CREATE INDEX decision_by_plan ON decision (plan_dir, decision_id)",
)

#: Schema version 2: the row two watchers race for.
#:
#: ``expires_at`` is epoch seconds rather than an ISO string because it is the
#: only column anything compares with an inequality, and the ISO strings this
#: module writes elsewhere are not safely orderable as text: ``isoformat``
#: drops the microsecond field when it is zero, so two stamps a microsecond
#: apart can sort the wrong way round.
_WATCH_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE watch_lease (
        plan_dir     TEXT    PRIMARY KEY,
        watcher_id   TEXT    NOT NULL,
        run_id       TEXT    NOT NULL,
        host         TEXT    NOT NULL,
        pid          INTEGER NOT NULL,
        expires_at   REAL    NOT NULL,
        claimed_at   TEXT    NOT NULL,
        renewed_at   TEXT    NOT NULL,
        released_at  TEXT
    )
    """,
    "CREATE INDEX watch_lease_by_watcher ON watch_lease (watcher_id, plan_dir)",
)

#: Schema version 3: the column that makes "no meaningful progress" durable.
#:
#: A dispatcher that may retry a failed action only twice before escalating
#: needs the count to survive the process, or restarting is how a caller buys
#: two more attempts. It also needs the count to reset when -- and only when --
#: the run's situation actually moved, which is why the discriminator is a
#: token supplied by the caller rather than a counter the ledger increments: a
#: counter has to be told when to reset, and whoever tells it is whoever
#: evades it. ``progress_token`` is instead a fingerprint of the run's
#: situation, and the count is simply how many rows carry the one in force.
#: What it excludes is the point -- see
#: :func:`agent_session_loop.overseer_dispatch.progress_token`.
#:
#: ``ALTER TABLE ... ADD COLUMN`` with a non-null default rewrites no rows and
#: runs inside the migration's transaction like any other statement, so a
#: version 1 or version 2 database reaches version 3 without its existing
#: actions being touched. They read as having been attempted under the empty
#: token, which is the honest record: nothing observed their situation.
_ACTION_PROGRESS_SCHEMA: tuple[str, ...] = (
    "ALTER TABLE action ADD COLUMN progress_token TEXT NOT NULL DEFAULT ''",
    """
    CREATE INDEX action_by_progress
    ON action (plan_dir, progress_token, status)
    """,
)


#: Schema version 4: the multi-repository checkpoint a restart has to finish.
#:
#: A checkpoint that spans a plan's whole registry commits one repository at a
#: time, and git offers nothing that makes several commits one transaction. The
#: only honest way to be restart-safe is therefore to record what has already
#: landed after every single commit, so a process that dies between the second
#: and the third can be told which two are done. That is what this table is:
#: ``record`` is the whole durable group --- per-repository snapshots, commit
#: shas, and the aliases still outstanding --- and it is rewritten at each
#: boundary rather than appended to, because the group's *current* obligation is
#: the thing a retry needs and a log of obligations would have to be replayed to
#: produce it.
#:
#: ``group_key`` is the primary key rather than a surrogate id, because the key
#: is derived from the plan, the incident and the dispatch target: a retry
#: computes the same key and finds the same row, which is what makes "reconcile
#: before retrying" possible for a process that never saw the first attempt.
_CHECKPOINT_GROUP_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE checkpoint_group (
        group_key           TEXT    PRIMARY KEY,
        plan_dir            TEXT    NOT NULL,
        incident_id         INTEGER,
        target              TEXT    NOT NULL,
        status              TEXT    NOT NULL,
        phase               TEXT    NOT NULL,
        epoch               INTEGER NOT NULL,
        config_fingerprint  TEXT    NOT NULL,
        request_digest      TEXT    NOT NULL,
        record              TEXT    NOT NULL,
        opened_at           TEXT    NOT NULL,
        updated_at          TEXT    NOT NULL
    )
    """,
    """
    CREATE INDEX checkpoint_group_by_plan
    ON checkpoint_group (plan_dir, status, group_key)
    """,
)


def _create_base_schema(connection: sqlite3.Connection) -> None:
    """Schema version 1: the eight tables the overseer's memory is made of."""
    for statement in _BASE_SCHEMA:
        connection.execute(statement)


def _create_watch_schema(connection: sqlite3.Connection) -> None:
    """Schema version 2: the watcher ownership table."""
    for statement in _WATCH_SCHEMA:
        connection.execute(statement)


def _create_action_progress_schema(connection: sqlite3.Connection) -> None:
    """Schema version 3: the situation an action was attempted under."""
    for statement in _ACTION_PROGRESS_SCHEMA:
        connection.execute(statement)


def _create_checkpoint_group_schema(connection: sqlite3.Connection) -> None:
    """Schema version 4: the multi-repository checkpoint still owed."""
    for statement in _CHECKPOINT_GROUP_SCHEMA:
        connection.execute(statement)


#: Every schema step, in order, each one taking the database from version
#: ``index`` to version ``index + 1``. Migrations are appended and never edited:
#: a database created by an older ASL replays exactly the steps it has not seen.
#:
#: The machinery is general rather than inlined into :func:`open_ledger`,
#: because the moment it matters is the moment it is too late to add: an
#: installed overseer's ledger has to survive the second version, and a
#: migration path first written when a second version exists is a migration
#: path nothing has ever run. Version 2 is that second version, and it replays
#: onto a version 1 database without touching a single existing row. Version 3
#: adds a column to a table that already holds rows, which is the other shape a
#: migration comes in, and it too rewrites nothing. Version 4 adds a table, like
#: version 2, and is likewise invisible to every row already stored.
MIGRATIONS: tuple[Migration, ...] = (
    _create_base_schema,
    _create_watch_schema,
    _create_action_progress_schema,
    _create_checkpoint_group_schema,
)

#: The schema version this module writes and accepts. Derived from the migration
#: list so the two can never disagree.
SCHEMA_VERSION = len(MIGRATIONS)


def apply_pragmas(connection: sqlite3.Connection) -> None:
    """Durability and integrity settings, applied before any statement runs.

    ``synchronous=FULL`` plus ``fullfsync`` is the same barrier
    :mod:`agent_session_loop.durability` reaches for by hand elsewhere: on
    Darwin an ordinary ``fsync`` does not flush the device cache, so a commit
    that returned would not necessarily survive a power loss. ``foreign_keys``
    is off by default in SQLite, which would make the receipt-to-action and
    evidence-to-incident references decorative.
    """
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    if sys.platform == "darwin":
        connection.execute("PRAGMA fullfsync = 1")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


def migrate(connection: sqlite3.Connection, path: Path) -> None:
    """Replay every migration this database has not seen, one per transaction.

    Per transaction, not one for all of them: ``user_version`` is written inside
    the same transaction as the statements it describes, so a step that fails
    rolls back both its schema changes and its version bump, and the next open
    resumes at exactly the step that failed rather than at the beginning.
    """
    version = user_version(connection)
    if version > SCHEMA_VERSION:
        raise LedgerError(
            f"overseer ledger {path} is schema version {version}, newer than the "
            f"version {SCHEMA_VERSION} this build understands; upgrade ASL rather "
            "than letting an older build write into it",
            reason="ledger_unsupported",
        )
    for index in range(version, SCHEMA_VERSION):
        migration = MIGRATIONS[index]
        connection.execute("BEGIN IMMEDIATE")
        try:
            migration(connection)
            connection.execute(f"PRAGMA user_version = {index + 1}")
            connection.execute("COMMIT")
        except BaseException as error:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise LedgerError(
                f"overseer ledger {path} could not be migrated to schema "
                f"version {index + 1}: {error}",
                reason="ledger_unreadable",
            ) from error


def user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0
