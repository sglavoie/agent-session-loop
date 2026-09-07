"""The overseer's durable ledger: what it saw, decided, attempted, and proved.

Discovery answers *which runs exist* and the policy client answers *may we act*.
Both are momentary. This is the boundary that remembers -- across a restart, a
crash between two writes, and a schema that has moved since the file was
created -- and it is a SQLite database rather than another JSONL file because
what it holds is relational and has to change atomically: an incident and the
evidence it rests on, an attempted dispatch and the receipt that later confirms
it, a policy change and every evidence row it invalidates.

The file is ``overseer-ledger.sqlite3``, a sibling of the per-plan state
directories under ASL's XDG state root. A *file* rather than a directory
deliberately: :func:`agent_session_loop.state.iter_plan_state_dirs` yields every
directory under that root as a plan, so a directory here would appear in the
fleet inventory as an unreadable run.

## Nothing stored here is permission

The ledger records authorization decisions. It never issues them. A stored
decision comes back as :class:`DecisionRecord`, which has no conversion to a
grant, no ``permits`` method, and no way to become one -- only
``overseer_policy_client.Decision.grant``, built from a live verified answer,
produces a :class:`~agent_session_loop.overseer_policy_client.Grant`. That
asymmetry is the whole design: "AgentMux was unavailable, but it said yes an
hour ago" has to be unrepresentable rather than merely discouraged, so the
substitution the shared requirements forbid is a thing this module cannot
express.

## Policy changes gate stored evidence

Evidence rows carry references to project content that was read under one
configuration. When :meth:`Ledger.note_policy` sees a configuration fingerprint
that differs from the one in force, it bumps the epoch and **seals** every
evidence row gathered under the old one, in the same transaction. A sealed row
is withheld from :meth:`Ledger.read_evidence` no matter who asks; it is
released only by :meth:`Ledger.release_evidence`, which requires a fresh
``context`` grant for that plan issued under the fingerprint now in force. So a
project the operator has just excluded never produces a grant, never gets its
evidence released, and never yields a row again -- while a policy edit that
left the project enrolled costs one re-authorization rather than the evidence
itself.

Incidents, decisions, actions, and receipts are ASL's own operational record
and are not gated: they say what the overseer did, not what the project
contains. The gate is on the project's content, which is what the exclusion is
about.

## Attempted is not executed

An action is written ``attempted`` before it is dispatched and reaches
``executed`` only through :meth:`Ledger.confirm_action`, which writes a receipt
in the same transaction as the status change. There is no path from
``attempted`` to ``executed`` that does not produce a receipt, and a crash
between the dispatch and the confirmation leaves the action ``attempted``
forever -- reported by :meth:`Ledger.in_doubt_actions` and never resolved by
this module in either direction. That matches the shared rule that
already-dispatched actions are not automatically undone: an overseer that
guessed "probably landed" or "probably did not" would be inventing the one fact
it does not have.

## Two watchers, one row

Ownership between concurrent watcher instances is a lease per plan, taken and
renewed through :meth:`Ledger.claim_watch` inside the same ``BEGIN IMMEDIATE``
that reads it. A lease changes hands unwillingly only by running out of time,
never by anyone deciding its holder has died: a watcher is a process that may be
on another host, and a probe that fails says the same thing about a busy watcher
as about a gone one.

Durable records this boundary writes: **one SQLite database**, always through
:meth:`Ledger.transaction`, so every multi-row write is all-or-nothing.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import clock, overseer_policy_client, state

__all__ = [
    "ACTION_STATUSES",
    "INCIDENT_STATUSES",
    "LEDGER_FILE_NAME",
    "SCHEMA_VERSION",
    "WITHHOLDING_REASONS",
    "ActionRecord",
    "DecisionRecord",
    "EnrollmentRecord",
    "EvidenceInput",
    "EvidenceRecord",
    "EvidenceView",
    "IncidentRecord",
    "Ledger",
    "LedgerError",
    "LedgerRefused",
    "ObservationCursor",
    "PolicyEpoch",
    "PolicyTransition",
    "ReceiptRecord",
    "Release",
    "WatchClaim",
    "WatchLease",
    "ledger_path",
    "open_ledger",
]

#: The ledger file, directly under ASL's XDG state root beside the per-plan
#: state directories. See the module docstring for why it is not a directory.
LEDGER_FILE_NAME = "overseer-ledger.sqlite3"

IncidentStatus = Literal["open", "resolved", "escalated", "abandoned"]

#: Every state an incident can be in. ``escalated`` is the one the shared
#: requirements name explicitly: an operation the overseer is not allowed to
#: perform stops here rather than being attempted in a weaker form.
INCIDENT_STATUSES: tuple[IncidentStatus, ...] = (
    "open",
    "resolved",
    "escalated",
    "abandoned",
)

ActionStatus = Literal["attempted", "executed", "failed", "abandoned"]

#: Every state a dispatched action can be in.
#:
#: * ``attempted`` -- written before the dispatch and never advanced by anything
#:   but a receipt. It is the in-doubt state, and a restart leaves it there.
#: * ``executed`` -- a receipt confirms the dispatch actually happened.
#: * ``failed`` -- a receipt confirms it did not.
#: * ``abandoned`` -- the overseer gave up asking; still not a claim about
#:   whether the action landed.
ACTION_STATUSES: tuple[ActionStatus, ...] = (
    "attempted",
    "executed",
    "failed",
    "abandoned",
)

Withholding = Literal[
    "unauthorized",
    "policy_unobserved",
    "policy_superseded",
    "enrollment_revoked",
]

#: Every reason stored project evidence is not handed back. Treat them as a
#: consumer contract: an operator surface reads them.
#:
#: * ``unauthorized`` -- no grant, a grant for another plan, a grant for another
#:   access category, or a grant carrying no request identity.
#: * ``policy_unobserved`` -- the ledger has not been told which configuration
#:   is in force, so it cannot tell whether this grant was issued under it.
#: * ``policy_superseded`` -- the grant names a configuration that is no longer
#:   the one in force.
#: * ``enrollment_revoked`` -- the plan's last answer from AgentMux was a
#:   refusal, or it was revoked explicitly.
WITHHOLDING_REASONS: tuple[Withholding, ...] = (
    "unauthorized",
    "policy_unobserved",
    "policy_superseded",
    "enrollment_revoked",
)


class LedgerError(Exception):
    """The ledger could not be opened, migrated, or used as asked."""


class LedgerRefused(LedgerError):
    """A write was refused because the grant presented did not permit it.

    Raised rather than returned. A read that is withheld still leaves the
    caller with a usable, honestly empty result; a write that was not permitted
    has no such result, and a return value would be one more thing a caller can
    forget to check.
    """


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
#: migration comes in, and it too rewrites nothing.
MIGRATIONS: tuple[Migration, ...] = (
    _create_base_schema,
    _create_watch_schema,
    _create_action_progress_schema,
)

#: The schema version this module writes and accepts. Derived from the migration
#: list so the two can never disagree.
SCHEMA_VERSION = len(MIGRATIONS)


@dataclass(frozen=True)
class PolicyEpoch:
    """The configuration the ledger currently believes is in force.

    ``epoch`` is zero and the fingerprint empty until :meth:`Ledger.note_policy`
    is called, which is a state in which no stored evidence is readable: not
    knowing which policy applies is never read as any policy applying.
    """

    epoch: int
    config_fingerprint: str
    observed_at: str

    @property
    def observed(self) -> bool:
        """Whether any configuration has been recorded at all."""
        return self.epoch > 0 and bool(self.config_fingerprint)


@dataclass(frozen=True)
class PolicyTransition:
    """What one observation of the current configuration did to the ledger."""

    epoch: int
    config_fingerprint: str
    previous_fingerprint: str
    changed: bool
    sealed_evidence: int


@dataclass(frozen=True)
class EnrollmentRecord:
    """The last answer AgentMux gave about one plan.

    An observation, never a permission. It carries no request digest that could
    be replayed and no method that turns it into a grant; a caller wanting to
    act asks ``overseer_policy_client.authorize`` again.
    """

    plan_dir: str
    enrolled: bool
    revoked: bool
    epoch: int
    config_fingerprint: str
    access: str
    last_failure: str | None
    first_seen_at: str
    observed_at: str


@dataclass(frozen=True)
class WatchLease:
    """One watcher's claim on one plan, as the ledger holds it.

    ``watcher_id`` is empty on a lease that was released: the row stays so the
    run it last named survives, but it is owned by nobody and the next claim
    takes it without waiting. ``expires_at`` is epoch seconds, and it is the
    *only* thing that lets a claim be taken from a watcher that did not release
    it -- see :meth:`Ledger.claim_watch`.
    """

    plan_dir: str
    watcher_id: str
    run_id: str
    host: str
    pid: int
    expires_at: float
    claimed_at: str
    renewed_at: str
    released_at: str | None

    @property
    def held(self) -> bool:
        """Whether anybody owns this lease at all."""
        return bool(self.watcher_id)

    def expired(self, now: float) -> bool:
        """Whether an unreleased claim has run out of time."""
        return self.expires_at <= now


@dataclass(frozen=True)
class WatchClaim:
    """What one attempt to own a plan did.

    ``previous_run_id`` is what makes a superseded run visible: the lease
    remembers which run the last watcher was watching, so a plan whose record
    now names a different one is a new question rather than a continuation of
    the old one.
    """

    plan_dir: str
    watcher_id: str
    run_id: str
    granted: bool
    took_over: bool
    previous_watcher_id: str
    previous_run_id: str
    expires_at: float
    holder: WatchLease | None

    @property
    def superseded_run(self) -> str:
        """The run this claim replaced, or ``""`` when it continues one."""
        if not self.granted or not self.previous_run_id:
            return ""
        return "" if self.previous_run_id == self.run_id else self.previous_run_id


@dataclass(frozen=True)
class ObservationCursor:
    """How far the overseer has read one stream of one plan."""

    plan_dir: str
    stream: str
    position: int
    marker: str
    observed_at: str


@dataclass(frozen=True)
class EvidenceInput:
    """One evidence reference, as the caller supplies it.

    A *reference*: an alias-qualified locator and a digest, not the content. The
    ledger records where the evidence was and what it hashed to, so a later read
    can be checked against the source rather than trusting a copy that outlived
    the authorization it was made under.
    """

    alias: str
    kind: str
    locator: str
    digest: str


@dataclass(frozen=True)
class EvidenceRecord:
    """One stored evidence reference, with the policy it was gathered under."""

    evidence_id: int
    incident_id: int
    plan_dir: str
    alias: str
    kind: str
    locator: str
    digest: str
    epoch: int
    config_fingerprint: str
    sealed: bool
    recorded_at: str


@dataclass(frozen=True)
class EvidenceView:
    """The evidence a caller was allowed to see, and how much it was not.

    ``withheld`` is reported rather than silently omitted: a partial view that
    reads as a whole one is how an overseer concludes there is nothing to act
    on when in fact it was not allowed to look.
    """

    plan_dir: str
    records: tuple[EvidenceRecord, ...]
    withheld: int
    refusal: Withholding | None

    @property
    def complete(self) -> bool:
        """Whether every stored row for this plan is in ``records``."""
        return self.refusal is None and self.withheld == 0


@dataclass(frozen=True)
class Release:
    """The outcome of re-authorizing a plan's sealed evidence."""

    plan_dir: str
    released: int
    refusal: Withholding | None


@dataclass(frozen=True)
class IncidentRecord:
    """One operational blocker the overseer observed on a run."""

    incident_id: int
    plan_dir: str
    run_id: str
    kind: str
    status: IncidentStatus
    summary: str
    epoch: int
    config_fingerprint: str
    opened_at: str
    settled_at: str | None


@dataclass(frozen=True)
class DecisionRecord:
    """One authorization answer, as it was recorded.

    Deliberately not convertible to a grant. See the module docstring: this
    type existing without that conversion is what makes "reuse the last
    successful authorization" unrepresentable rather than merely discouraged.
    """

    decision_id: int
    plan_dir: str
    access: str
    allowed: bool
    available: bool
    failure: str | None
    reason: str
    summary: str
    config_fingerprint: str
    request_digest: str
    recorded_at: str


@dataclass(frozen=True)
class ReceiptRecord:
    """The confirmation that an attempted action actually happened, or did not."""

    receipt_id: int
    action_id: int
    outcome: str
    detail: str
    locator: str
    confirmed_at: str


@dataclass(frozen=True)
class ActionRecord:
    """One action the overseer dispatched, and what is known about its fate."""

    action_id: int
    plan_dir: str
    incident_id: int | None
    operation: str
    target: str
    status: ActionStatus
    epoch: int
    config_fingerprint: str
    request_digest: str
    progress_token: str
    attempted_at: str
    settled_at: str | None
    receipt: ReceiptRecord | None

    @property
    def confirmed(self) -> bool:
        """Whether a receipt proves what became of this dispatch.

        ``attempted`` is never confirmed, however long ago it was written. Age
        is not evidence.
        """
        return self.receipt is not None and self.status in ("executed", "failed")

    @property
    def in_doubt(self) -> bool:
        """Whether the dispatch's outcome is genuinely unknown."""
        return self.status == "attempted"


def ledger_path(state_dir: str | Path | None = None) -> Path:
    """Where the overseer ledger lives under ``state_dir`` or the XDG default."""
    return state.state_root(state_dir) / LEDGER_FILE_NAME


def open_ledger(
    *,
    state_dir: str | Path | None = None,
    now: Callable[[], str] = clock.utc_iso_now,
) -> Ledger:
    """Open the ledger, creating and migrating it as needed.

    Every open runs the pending migrations, so an ASL that starts against a
    database an older build created reaches the current schema before it reads a
    row. A database written by a *newer* build is refused instead: replaying
    nothing onto an unknown schema and hoping the columns line up is how an
    incident ledger starts recording into the wrong shape.
    """
    path = ledger_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(path), isolation_level=None)
    except sqlite3.Error as error:
        raise LedgerError(f"could not open overseer ledger {path}: {error}") from error
    connection.row_factory = sqlite3.Row
    try:
        _apply_pragmas(connection)
        _migrate(connection, path)
    except BaseException:
        connection.close()
        raise
    return Ledger(connection=connection, path=path, now=now)


def _apply_pragmas(connection: sqlite3.Connection) -> None:
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


def _migrate(connection: sqlite3.Connection, path: Path) -> None:
    """Replay every migration this database has not seen, one per transaction.

    Per transaction, not one for all of them: ``user_version`` is written inside
    the same transaction as the statements it describes, so a step that fails
    rolls back both its schema changes and its version bump, and the next open
    resumes at exactly the step that failed rather than at the beginning.
    """
    version = _user_version(connection)
    if version > SCHEMA_VERSION:
        raise LedgerError(
            f"overseer ledger {path} is schema version {version}, newer than the "
            f"version {SCHEMA_VERSION} this build understands; upgrade ASL rather "
            "than letting an older build write into it"
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
                f"version {index + 1}: {error}"
            ) from error


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


class Ledger:
    """The overseer's durable memory, open on one SQLite database.

    Constructed by :func:`open_ledger`. Every write goes through
    :meth:`transaction`, so a caller cannot leave half of a multi-row change
    behind by returning early or raising in the middle of one.
    """

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        path: Path,
        now: Callable[[], str] = clock.utc_iso_now,
    ) -> None:
        self._connection = connection
        self._now = now
        self.path = path

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exception: object) -> Literal[False]:
        self.close()
        return False

    def close(self) -> None:
        """Checkpoint the write-ahead log and close the database.

        The checkpoint matters for what comes after: WAL keeps recent commits in
        a sidecar file, and folding them back means the ledger a backup or an
        operator copies is the whole ledger rather than most of it.
        """
        with contextlib.suppress(sqlite3.Error):
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.close()

    @property
    def schema_version(self) -> int:
        """The schema version this database is actually at."""
        return _user_version(self._connection)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block as one all-or-nothing write.

        ``BEGIN IMMEDIATE`` rather than the default deferred begin: the write
        lock is taken up front, so two overseers reaching the ledger at once
        queue on the busy timeout instead of discovering the conflict at commit
        time, after one of them has already decided what it is going to write.
        """
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    # -- policy epoch ------------------------------------------------------

    def current_epoch(self) -> PolicyEpoch:
        """The configuration the ledger believes is in force."""
        row = self._connection.execute(
            "SELECT epoch, config_fingerprint, observed_at "
            "FROM policy_epoch WHERE id = 1"
        ).fetchone()
        if row is None:
            return PolicyEpoch(epoch=0, config_fingerprint="", observed_at="")
        return PolicyEpoch(
            epoch=int(row["epoch"]),
            config_fingerprint=str(row["config_fingerprint"]),
            observed_at=str(row["observed_at"]),
        )

    def note_policy(self, *, config_fingerprint: str) -> PolicyTransition:
        """Record which configuration is in force, sealing evidence if it moved.

        The fingerprint comes from a policy answer, which is the only place it
        can honestly come from: it names the declarations AgentMux decided
        against, and ASL has no independent way to compute it.

        A change seals every unsealed evidence row in one transaction with the
        epoch bump. Doing it in two steps would leave a window in which the
        epoch has moved and the evidence has not, and a read landing inside that
        window would be handed content gathered under a policy that no longer
        applies.
        """
        if not config_fingerprint:
            raise LedgerError("a policy observation must name a configuration")
        moment = self._now()
        with self.transaction() as connection:
            current = self.current_epoch()
            if current.config_fingerprint == config_fingerprint:
                return PolicyTransition(
                    epoch=current.epoch,
                    config_fingerprint=config_fingerprint,
                    previous_fingerprint=config_fingerprint,
                    changed=False,
                    sealed_evidence=0,
                )
            epoch = current.epoch + 1
            sealed = 0
            if current.observed:
                cursor = connection.execute(
                    "UPDATE evidence SET sealed = 1 WHERE sealed = 0"
                )
                sealed = cursor.rowcount if cursor.rowcount > 0 else 0
            connection.execute(
                """
                INSERT INTO policy_epoch (id, epoch, config_fingerprint, observed_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    epoch = excluded.epoch,
                    config_fingerprint = excluded.config_fingerprint,
                    observed_at = excluded.observed_at
                """,
                (epoch, config_fingerprint, moment),
            )
            return PolicyTransition(
                epoch=epoch,
                config_fingerprint=config_fingerprint,
                previous_fingerprint=current.config_fingerprint,
                changed=current.observed,
                sealed_evidence=sealed,
            )

    # -- enrollment --------------------------------------------------------

    def record_enrollment(
        self, decision: overseer_policy_client.Decision
    ) -> EnrollmentRecord:
        """Record what AgentMux last said about this plan's enrollment.

        Three outcomes, and only one of them moves the enrollment bit upward. An
        available allowance enrolls; an available refusal revokes; an
        *unavailable* answer changes neither, and records the failure instead.
        That last case is the whole point: an overseer that could not reach
        AgentMux has learned nothing about enrollment, and a ledger that let a
        transport fault clear a revocation -- or preserve an enrollment as
        though it had been reconfirmed -- would be laundering an outage into an
        answer.
        """
        moment = self._now()
        failure = decision.failure
        with self.transaction() as connection:
            existing = self.enrollment(decision.plan_dir)
            if decision.available:
                enrolled = decision.allowed
                revoked = not decision.allowed
                fingerprint = decision.config_fingerprint
                digest = decision.request_digest
                epoch = self.current_epoch().epoch
            elif existing is not None:
                enrolled = existing.enrolled
                revoked = existing.revoked
                fingerprint = existing.config_fingerprint
                digest = ""
                epoch = existing.epoch
            else:
                enrolled = False
                revoked = False
                fingerprint = ""
                digest = ""
                epoch = 0
            first_seen = existing.first_seen_at if existing is not None else moment
            connection.execute(
                """
                INSERT INTO enrollment (
                    plan_dir, enrolled, revoked, epoch, config_fingerprint,
                    access, request_digest, last_failure, first_seen_at, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (plan_dir) DO UPDATE SET
                    enrolled = excluded.enrolled,
                    revoked = excluded.revoked,
                    epoch = excluded.epoch,
                    config_fingerprint = excluded.config_fingerprint,
                    access = excluded.access,
                    request_digest = excluded.request_digest,
                    last_failure = excluded.last_failure,
                    observed_at = excluded.observed_at
                """,
                (
                    decision.plan_dir,
                    int(enrolled),
                    int(revoked),
                    epoch,
                    fingerprint,
                    decision.access,
                    digest,
                    failure,
                    first_seen,
                    moment,
                ),
            )
        recorded = self.enrollment(decision.plan_dir)
        if recorded is None:
            raise LedgerError(
                f"enrollment for {decision.plan_dir} vanished immediately after "
                "it was written"
            )
        return recorded

    def enrollment(self, plan_dir: str) -> EnrollmentRecord | None:
        """The last enrollment answer for ``plan_dir``, if one was recorded."""
        row = self._connection.execute(
            """
            SELECT plan_dir, enrolled, revoked, epoch, config_fingerprint, access,
                   last_failure, first_seen_at, observed_at
            FROM enrollment WHERE plan_dir = ?
            """,
            (plan_dir,),
        ).fetchone()
        if row is None:
            return None
        return EnrollmentRecord(
            plan_dir=str(row["plan_dir"]),
            enrolled=bool(row["enrolled"]),
            revoked=bool(row["revoked"]),
            epoch=int(row["epoch"]),
            config_fingerprint=str(row["config_fingerprint"]),
            access=str(row["access"]),
            last_failure=_optional_text(row["last_failure"]),
            first_seen_at=str(row["first_seen_at"]),
            observed_at=str(row["observed_at"]),
        )

    def revoke(self, *, plan_dir: str, reason: str) -> int:
        """Withdraw a plan and seal everything already gathered from it.

        Used when the overseer learns a plan is excluded outside the ordinary
        decision path -- an operator withdrawal, a run whose repository set no
        longer matches what was authorized. Sealing rather than deleting: the
        shared requirements say revocation stops subsequent access, not that the
        record of what was already read is destroyed. The rows stay, unreadable,
        until a fresh grant releases them.
        """
        moment = self._now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO enrollment (
                    plan_dir, enrolled, revoked, epoch, config_fingerprint,
                    access, request_digest, last_failure, first_seen_at, observed_at
                ) VALUES (?, 0, 1, 0, '', '', '', ?, ?, ?)
                ON CONFLICT (plan_dir) DO UPDATE SET
                    enrolled = 0,
                    revoked = 1,
                    request_digest = '',
                    last_failure = excluded.last_failure,
                    observed_at = excluded.observed_at
                """,
                (plan_dir, reason, moment, moment),
            )
            cursor = connection.execute(
                "UPDATE evidence SET sealed = 1 WHERE plan_dir = ? AND sealed = 0",
                (plan_dir,),
            )
            return cursor.rowcount if cursor.rowcount > 0 else 0

    # -- watcher ownership -------------------------------------------------

    def claim_watch(
        self,
        *,
        plan_dir: str,
        watcher_id: str,
        run_id: str,
        host: str,
        pid: int,
        now: float,
        lease_seconds: float,
    ) -> WatchClaim:
        """Take or renew this watcher's claim on one plan, atomically.

        Four ways a claim is granted, and exactly one way it is refused. It is
        granted when nobody has ever claimed the plan, when the last holder
        released it, when the claimant already holds it (a renewal, which is
        also how a restart under the same identity resumes), and when an
        unreleased lease has run out of time. It is refused while another
        watcher's lease is still inside its term.

        That last case is the whole point, and the reason expiry is the only
        way a lease changes hands unwillingly: a watcher is a process somewhere,
        possibly on another host, and the observable evidence about it is the
        same whether it is busy, quiet, or gone. Probing it and reading a failed
        probe as death is the mistake
        :func:`agent_session_loop.supervision_liveness.probe` exists to refuse
        to make -- so this waits out a term the holder stopped renewing instead
        of deciding the holder is dead. A watcher that crashed costs one lease
        term of delay; a watcher that is alive and slow keeps its plans.

        The read and the write are one ``BEGIN IMMEDIATE`` transaction, so two
        watchers reaching the same plan at the same moment serialize rather than
        both concluding the row was free.
        """
        if not plan_dir or not watcher_id:
            raise LedgerError("a watch claim must name both a plan and a watcher")
        moment = self._now()
        expires_at = now + lease_seconds
        with self.transaction() as connection:
            existing = self.watch_lease(plan_dir)
            held_by_another = (
                existing is not None
                and existing.held
                and existing.watcher_id != watcher_id
            )
            if existing is not None and held_by_another and not existing.expired(now):
                return WatchClaim(
                    plan_dir=plan_dir,
                    watcher_id=watcher_id,
                    run_id=run_id,
                    granted=False,
                    took_over=False,
                    previous_watcher_id=existing.watcher_id,
                    previous_run_id=existing.run_id,
                    expires_at=existing.expires_at,
                    holder=existing,
                )
            took_over = held_by_another
            claimed_at = (
                existing.claimed_at
                if existing is not None and existing.watcher_id == watcher_id
                else moment
            )
            connection.execute(
                """
                INSERT INTO watch_lease (
                    plan_dir, watcher_id, run_id, host, pid, expires_at,
                    claimed_at, renewed_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT (plan_dir) DO UPDATE SET
                    watcher_id = excluded.watcher_id,
                    run_id = excluded.run_id,
                    host = excluded.host,
                    pid = excluded.pid,
                    expires_at = excluded.expires_at,
                    claimed_at = excluded.claimed_at,
                    renewed_at = excluded.renewed_at,
                    released_at = NULL
                """,
                (
                    plan_dir,
                    watcher_id,
                    run_id,
                    host,
                    pid,
                    expires_at,
                    claimed_at,
                    moment,
                ),
            )
            return WatchClaim(
                plan_dir=plan_dir,
                watcher_id=watcher_id,
                run_id=run_id,
                granted=True,
                took_over=took_over,
                previous_watcher_id=(
                    existing.watcher_id if existing is not None else ""
                ),
                previous_run_id=existing.run_id if existing is not None else "",
                expires_at=expires_at,
                holder=None,
            )

    def release_watch(self, *, plan_dir: str, watcher_id: str) -> bool:
        """Give up this watcher's claim, keeping what the lease remembers.

        The row is emptied of its owner rather than deleted, because the run it
        names is the fact a later watcher needs to tell a continuation from a
        run that has been replaced since anybody looked. Releasing a lease
        somebody else holds does nothing and reports ``False``: a watcher can
        only put down what it picked up.
        """
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE watch_lease
                SET watcher_id = '', expires_at = 0.0, released_at = ?
                WHERE plan_dir = ? AND watcher_id = ?
                """,
                (moment, plan_dir, watcher_id),
            )
            return cursor.rowcount == 1

    def watch_lease(self, plan_dir: str) -> WatchLease | None:
        """The lease on one plan, held or released, if the row exists."""
        row = self._connection.execute(
            f"SELECT {_WATCH_LEASE_COLUMNS} FROM watch_lease WHERE plan_dir = ?",
            (plan_dir,),
        ).fetchone()
        return None if row is None else _watch_lease(row)

    def watch_leases(self, *, watcher_id: str | None = None) -> tuple[WatchLease, ...]:
        """Every lease, or every lease one watcher holds, in plan order."""
        where = " WHERE watcher_id = ?" if watcher_id is not None else ""
        parameters = (watcher_id,) if watcher_id is not None else ()
        rows = self._connection.execute(
            f"SELECT {_WATCH_LEASE_COLUMNS} FROM watch_lease{where} ORDER BY plan_dir",
            parameters,
        ).fetchall()
        return tuple(_watch_lease(row) for row in rows)

    # -- observation cursors ----------------------------------------------

    def advance_cursor(
        self,
        *,
        plan_dir: str,
        stream: str,
        position: int,
        marker: str = "",
    ) -> ObservationCursor:
        """Record how far one stream of one plan has been read.

        Monotonic: a lower position is ignored rather than written. A cursor
        that can move backwards turns a stale observer into a replay of work the
        overseer already did, and the ledger is the only place that ordering is
        enforced.
        """
        moment = self._now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO observation_cursor (
                    plan_dir, stream, position, marker, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (plan_dir, stream) DO UPDATE SET
                    position = MAX(observation_cursor.position, excluded.position),
                    marker = CASE
                        WHEN excluded.position >= observation_cursor.position
                        THEN excluded.marker ELSE observation_cursor.marker END,
                    observed_at = CASE
                        WHEN excluded.position >= observation_cursor.position
                        THEN excluded.observed_at ELSE observation_cursor.observed_at
                        END
                """,
                (plan_dir, stream, position, marker, moment),
            )
        recorded = self.cursor(plan_dir=plan_dir, stream=stream)
        if recorded is None:
            raise LedgerError(
                f"cursor for {plan_dir}/{stream} vanished immediately after it "
                "was written"
            )
        return recorded

    def cursor(self, *, plan_dir: str, stream: str) -> ObservationCursor | None:
        """The recorded position for one stream of one plan."""
        row = self._connection.execute(
            """
            SELECT plan_dir, stream, position, marker, observed_at
            FROM observation_cursor WHERE plan_dir = ? AND stream = ?
            """,
            (plan_dir, stream),
        ).fetchone()
        if row is None:
            return None
        return ObservationCursor(
            plan_dir=str(row["plan_dir"]),
            stream=str(row["stream"]),
            position=int(row["position"]),
            marker=str(row["marker"]),
            observed_at=str(row["observed_at"]),
        )

    # -- incidents and evidence -------------------------------------------

    def open_incident(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        run_id: str,
        kind: str,
        summary: str,
        evidence: Sequence[EvidenceInput] = (),
    ) -> IncidentRecord:
        """Record one operational blocker together with the evidence for it.

        The incident row and every evidence row land in one transaction, so a
        failure partway through leaves no incident at all rather than an
        incident whose evidence is missing the row that would have contradicted
        it.

        A grant is required whenever evidence is attached, because evidence is
        project content read under authorization. An incident with no evidence
        needs none: "this run is paused" is ASL's own run state, which the
        metadata-only discovery boundary already establishes before any
        authorization is asked for.
        """
        if evidence:
            self._require_context_grant(grant=grant, plan_dir=plan_dir)
        moment = self._now()
        epoch = self.current_epoch()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO incident (
                    plan_dir, run_id, kind, status, summary, epoch,
                    config_fingerprint, opened_at, settled_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, NULL)
                """,
                (
                    plan_dir,
                    run_id,
                    kind,
                    summary,
                    epoch.epoch,
                    epoch.config_fingerprint,
                    moment,
                ),
            )
            incident_id = int(cursor.lastrowid or 0)
            for item in evidence:
                connection.execute(
                    """
                    INSERT INTO evidence (
                        incident_id, plan_dir, alias, kind, locator, digest,
                        epoch, config_fingerprint, sealed, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        incident_id,
                        plan_dir,
                        item.alias,
                        item.kind,
                        item.locator,
                        item.digest,
                        epoch.epoch,
                        epoch.config_fingerprint,
                        moment,
                    ),
                )
        recorded = self.incident(incident_id)
        if recorded is None:
            raise LedgerError(
                f"incident {incident_id} vanished immediately after it was written"
            )
        return recorded

    def settle_incident(
        self, *, incident_id: int, status: IncidentStatus
    ) -> IncidentRecord:
        """Move an incident out of ``open``."""
        if status not in INCIDENT_STATUSES or status == "open":
            raise LedgerError(
                f"{status!r} is not a settled incident status; use one of "
                f"{', '.join(s for s in INCIDENT_STATUSES if s != 'open')}"
            )
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE incident SET status = ?, settled_at = ? WHERE incident_id = ?",
                (status, moment, incident_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(f"no incident {incident_id} to settle")
        recorded = self.incident(incident_id)
        if recorded is None:
            raise LedgerError(f"incident {incident_id} vanished while being settled")
        return recorded

    def incident(self, incident_id: int) -> IncidentRecord | None:
        """One incident by identifier."""
        row = self._connection.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM incident WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        return None if row is None else _incident(row)

    def incidents(
        self,
        *,
        plan_dir: str | None = None,
        status: IncidentStatus | None = None,
    ) -> tuple[IncidentRecord, ...]:
        """Every incident matching the filters, oldest first."""
        clauses: list[str] = []
        parameters: list[object] = []
        if plan_dir is not None:
            clauses.append("plan_dir = ?")
            parameters.append(plan_dir)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT {_INCIDENT_COLUMNS} FROM incident{where} ORDER BY incident_id",
            tuple(parameters),
        ).fetchall()
        return tuple(_incident(row) for row in rows)

    def read_evidence(
        self, *, plan_dir: str, grant: overseer_policy_client.Grant | None
    ) -> EvidenceView:
        """Hand back this plan's evidence, if the current policy still permits it.

        Four gates, all of which withhold rather than raise, and all of which
        are checked before a row is read: the grant has to be for this plan and
        this access category, the ledger has to know which policy is in force,
        the grant has to have been issued under that same policy, and the plan
        must not be revoked. Sealed rows are then excluded and counted, so the
        caller is told the view is partial rather than left to infer it.
        """
        refusal = self._withholding(grant=grant, plan_dir=plan_dir)
        if refusal is not None:
            return EvidenceView(
                plan_dir=plan_dir,
                records=(),
                withheld=self._evidence_count(plan_dir=plan_dir, sealed=None),
                refusal=refusal,
            )
        rows = self._connection.execute(
            f"SELECT {_EVIDENCE_COLUMNS} FROM evidence "
            "WHERE plan_dir = ? AND sealed = 0 ORDER BY evidence_id",
            (plan_dir,),
        ).fetchall()
        return EvidenceView(
            plan_dir=plan_dir,
            records=tuple(_evidence(row) for row in rows),
            withheld=self._evidence_count(plan_dir=plan_dir, sealed=True),
            refusal=None,
        )

    def release_evidence(
        self, *, plan_dir: str, grant: overseer_policy_client.Grant | None
    ) -> Release:
        """Re-authorize sealed evidence against the policy now in force.

        This is the only way a sealed row becomes readable again, and it costs a
        fresh authorization: the grant must be for this plan, for ``context``,
        and carry the fingerprint of the configuration currently in force. A
        plan the operator has since excluded produces no such grant, so its
        evidence stays sealed permanently -- which is the requirement that
        stored evidence not survive an exclusion, expressed as an operation that
        cannot succeed rather than a rule somebody has to remember.
        """
        refusal = self._withholding(grant=grant, plan_dir=plan_dir)
        if refusal is not None:
            return Release(plan_dir=plan_dir, released=0, refusal=refusal)
        epoch = self.current_epoch()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE evidence
                SET sealed = 0, epoch = ?, config_fingerprint = ?
                WHERE plan_dir = ? AND sealed = 1
                """,
                (epoch.epoch, epoch.config_fingerprint, plan_dir),
            )
            released = cursor.rowcount if cursor.rowcount > 0 else 0
        return Release(plan_dir=plan_dir, released=released, refusal=None)

    # -- decisions ---------------------------------------------------------

    def record_decision(
        self, decision: overseer_policy_client.Decision
    ) -> DecisionRecord:
        """Record one authorization answer, allowed or not, available or not.

        Every answer is recorded, including the failures, because "we could not
        ask" is the fact an operator needs when an overseer stops acting and the
        configuration looks fine.
        """
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO decision (
                    plan_dir, access, allowed, available, failure, reason,
                    summary, config_fingerprint, request_digest, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.plan_dir,
                    decision.access,
                    int(decision.allowed),
                    int(decision.available),
                    decision.failure,
                    decision.reason,
                    decision.summary,
                    decision.config_fingerprint,
                    decision.request_digest,
                    moment,
                ),
            )
            decision_id = int(cursor.lastrowid or 0)
        recorded = self.decision(decision_id)
        if recorded is None:
            raise LedgerError(
                f"decision {decision_id} vanished immediately after it was written"
            )
        return recorded

    def decision(self, decision_id: int) -> DecisionRecord | None:
        """One recorded decision by identifier."""
        row = self._connection.execute(
            f"SELECT {_DECISION_COLUMNS} FROM decision WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return None if row is None else _decision(row)

    def decisions(self, *, plan_dir: str | None = None) -> tuple[DecisionRecord, ...]:
        """Recorded decisions, oldest first."""
        where = " WHERE plan_dir = ?" if plan_dir is not None else ""
        parameters = (plan_dir,) if plan_dir is not None else ()
        rows = self._connection.execute(
            f"SELECT {_DECISION_COLUMNS} FROM decision{where} ORDER BY decision_id",
            parameters,
        ).fetchall()
        return tuple(_decision(row) for row in rows)

    # -- actions and receipts ---------------------------------------------

    def attempt_action(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        operation: str,
        target: str,
        incident_id: int | None = None,
        progress_token: str = "",
    ) -> ActionRecord:
        """Record a dispatch *before* it happens, and return it as ``attempted``.

        Written first on purpose. A record written after a successful dispatch
        cannot exist for a dispatch that crashed halfway, which is precisely the
        case the ledger is for: the durable trace of an in-flight action has to
        outlive the process that started it.

        The grant must be an ``action`` grant for this plan, issued under the
        configuration currently in force. A grant older than the last policy
        change is refused rather than honored: the recheck the requirements ask
        for before dispatch is meaningless if a decision made under a
        superseded configuration still opens the door.

        ``progress_token`` is stored, never interpreted. The ledger's only
        stake in it is that two attempts made in the same situation carry the
        same value and two made in different ones do not, which is what
        :meth:`recovery_attempts` counts by; deciding what "the same situation"
        means is the dispatcher's job, not this table's.
        """
        self._require_grant(
            grant=grant,
            plan_dir=plan_dir,
            access=overseer_policy_client.ACCESS_ACTION,
        )
        if grant is None:
            raise LedgerRefused(f"no action grant for {plan_dir}")
        moment = self._now()
        epoch = self.current_epoch()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO action (
                    plan_dir, incident_id, operation, target, status, epoch,
                    config_fingerprint, request_digest, progress_token,
                    attempted_at, settled_at
                ) VALUES (?, ?, ?, ?, 'attempted', ?, ?, ?, ?, ?, NULL)
                """,
                (
                    plan_dir,
                    incident_id,
                    operation,
                    target,
                    epoch.epoch,
                    epoch.config_fingerprint,
                    grant.request_digest,
                    progress_token,
                    moment,
                ),
            )
            action_id = int(cursor.lastrowid or 0)
        recorded = self.action(action_id)
        if recorded is None:
            raise LedgerError(
                f"action {action_id} vanished immediately after it was written"
            )
        return recorded

    def confirm_action(
        self,
        *,
        action_id: int,
        outcome: str,
        detail: str = "",
        locator: str = "",
        status: ActionStatus = "executed",
    ) -> ActionRecord:
        """Settle an attempted dispatch with the receipt that proves its fate.

        The receipt and the status change are one transaction, so no state
        exists in which an action reads ``executed`` with nothing backing it.
        Only an ``attempted`` action can be settled: a second confirmation is
        refused rather than silently overwriting the first receipt, because two
        different answers about one dispatch means one of them is wrong and the
        ledger has no way to tell which.
        """
        if status not in ("executed", "failed", "abandoned"):
            raise LedgerError(
                f"{status!r} is not a settled action status; an attempted "
                "dispatch settles as executed, failed, or abandoned"
            )
        moment = self._now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE action SET status = ?, settled_at = ?
                WHERE action_id = ? AND status = 'attempted'
                """,
                (status, moment, action_id),
            )
            if cursor.rowcount != 1:
                raise LedgerError(
                    f"no attempted action {action_id} to confirm; an action "
                    "settles exactly once"
                )
            connection.execute(
                """
                INSERT INTO receipt (action_id, outcome, detail, locator, confirmed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (action_id, outcome, detail, locator, moment),
            )
        recorded = self.action(action_id)
        if recorded is None:
            raise LedgerError(f"action {action_id} vanished while being confirmed")
        return recorded

    def action(self, action_id: int) -> ActionRecord | None:
        """One action and its receipt, if it has one."""
        row = self._connection.execute(
            f"SELECT {_ACTION_COLUMNS} FROM action WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return _action(row, self._receipt(action_id))

    def actions(
        self,
        *,
        plan_dir: str | None = None,
        status: ActionStatus | None = None,
    ) -> tuple[ActionRecord, ...]:
        """Every action matching the filters, oldest first."""
        clauses: list[str] = []
        parameters: list[object] = []
        if plan_dir is not None:
            clauses.append("plan_dir = ?")
            parameters.append(plan_dir)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT {_ACTION_COLUMNS} FROM action{where} ORDER BY action_id",
            tuple(parameters),
        ).fetchall()
        return tuple(_action(row, self._receipt(int(row["action_id"]))) for row in rows)

    def recovery_attempts(
        self, *, plan_dir: str, progress_token: str
    ) -> tuple[ActionRecord, ...]:
        """Dispatches on one plan that ended badly in one situation.

        Keyed by the situation and *not* by the incident. Keying it by incident
        would make opening a second incident for the same trouble a way to buy
        two more attempts, and opening incidents is something the overseer does
        for itself -- so the evasion would need no bad actor, just a blocker
        observed twice.

        ``failed`` and ``abandoned`` both count. An abandoned attempt is one
        the overseer stopped waiting on, which cost the same recovery a failed
        one did; excluding it would let a caller buy attempts by abandoning
        them. ``attempted`` does not count, because an unsettled dispatch may
        yet turn out to have worked -- the dispatcher refuses to retry over one
        of those anyway, so it is never the row standing between a caller and
        an escalation.

        This is a read, not a policy: how many is too many belongs to
        :data:`agent_session_loop.overseer_dispatch.RECOVERY_LIMIT`.
        """
        rows = self._connection.execute(
            f"""
            SELECT {_ACTION_COLUMNS} FROM action
            WHERE plan_dir = ? AND progress_token = ?
              AND status IN ('failed', 'abandoned')
            ORDER BY action_id
            """,
            (plan_dir, progress_token),
        ).fetchall()
        return tuple(_action(row, self._receipt(int(row["action_id"]))) for row in rows)

    def executed_action(
        self,
        *,
        plan_dir: str,
        operation: str,
        target: str,
        progress_token: str,
    ) -> ActionRecord | None:
        """The dispatch that already did this exact thing in this situation.

        The situation is part of the identity on purpose. Without it, a second
        resumption of a run that paused again would read as a duplicate of the
        first, and the overseer would refuse to do the one thing the second
        pause is asking for; with it, "again, in the same circumstances,
        against the same target" is the only shape that counts as a repeat.

        The incident is *not* part of it, for the reason
        :meth:`recovery_attempts` gives: a repeat filed under a new incident is
        still a repeat.
        """
        row = self._connection.execute(
            f"""
            SELECT {_ACTION_COLUMNS} FROM action
            WHERE plan_dir = ? AND operation = ? AND target = ?
              AND progress_token = ? AND status = 'executed'
            ORDER BY action_id LIMIT 1
            """,
            (plan_dir, operation, target, progress_token),
        ).fetchone()
        if row is None:
            return None
        return _action(row, self._receipt(int(row["action_id"])))

    def in_doubt_actions(
        self, *, plan_dir: str | None = None
    ) -> tuple[ActionRecord, ...]:
        """Dispatches that were attempted and never confirmed either way.

        What a restart finds. The shared requirements say an already-dispatched
        action is not automatically undone, so these are reported for a human or
        a fresh observation to settle -- never rolled forward into ``executed``
        and never rolled back.
        """
        return self.actions(plan_dir=plan_dir, status="attempted")

    def _receipt(self, action_id: int) -> ReceiptRecord | None:
        row = self._connection.execute(
            """
            SELECT receipt_id, action_id, outcome, detail, locator, confirmed_at
            FROM receipt WHERE action_id = ?
            """,
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return ReceiptRecord(
            receipt_id=int(row["receipt_id"]),
            action_id=int(row["action_id"]),
            outcome=str(row["outcome"]),
            detail=str(row["detail"]),
            locator=str(row["locator"]),
            confirmed_at=str(row["confirmed_at"]),
        )

    # -- gating ------------------------------------------------------------

    def _withholding(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
    ) -> Withholding | None:
        """Why stored project evidence for ``plan_dir`` is not readable now.

        Order matters only for the message: every clause refuses, and they are
        checked before any row is touched so a withheld read never reads.
        """
        if (
            grant is None
            or grant.plan_dir != plan_dir
            or grant.access != overseer_policy_client.ACCESS_CONTEXT
            or not grant.request_digest
            or not grant.config_fingerprint
        ):
            return "unauthorized"
        epoch = self.current_epoch()
        if not epoch.observed:
            return "policy_unobserved"
        if grant.config_fingerprint != epoch.config_fingerprint:
            return "policy_superseded"
        record = self.enrollment(plan_dir)
        if record is not None and record.revoked:
            return "enrollment_revoked"
        return None

    def _require_context_grant(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
    ) -> None:
        refusal = self._withholding(grant=grant, plan_dir=plan_dir)
        if refusal is not None:
            raise LedgerRefused(f"evidence for {plan_dir} may not be stored: {refusal}")

    def _require_grant(
        self,
        *,
        grant: overseer_policy_client.Grant | None,
        plan_dir: str,
        access: str,
    ) -> None:
        """Refuse a write whose grant does not answer exactly this question."""
        if (
            grant is None
            or grant.plan_dir != plan_dir
            or grant.access != access
            or not grant.request_digest
            or not grant.config_fingerprint
        ):
            raise LedgerRefused(
                f"no {access} grant for {plan_dir}; the write was not attempted"
            )
        epoch = self.current_epoch()
        if not epoch.observed:
            raise LedgerRefused(
                f"no configuration is on record, so a {access} grant for "
                f"{plan_dir} cannot be checked against the policy in force"
            )
        if grant.config_fingerprint != epoch.config_fingerprint:
            raise LedgerRefused(
                f"the {access} grant for {plan_dir} was issued under a "
                "configuration that is no longer in force; ask again"
            )
        record = self.enrollment(plan_dir)
        if record is not None and record.revoked:
            raise LedgerRefused(f"{plan_dir} is revoked; the write was not attempted")

    def _evidence_count(self, *, plan_dir: str, sealed: bool | None) -> int:
        clause = "" if sealed is None else " AND sealed = ?"
        parameters: tuple[object, ...] = (
            (plan_dir,) if sealed is None else (plan_dir, int(sealed))
        )
        row = self._connection.execute(
            f"SELECT COUNT(*) AS total FROM evidence WHERE plan_dir = ?{clause}",
            parameters,
        ).fetchone()
        return int(row["total"]) if row is not None else 0


_INCIDENT_COLUMNS = (
    "incident_id, plan_dir, run_id, kind, status, summary, epoch, "
    "config_fingerprint, opened_at, settled_at"
)

_EVIDENCE_COLUMNS = (
    "evidence_id, incident_id, plan_dir, alias, kind, locator, digest, epoch, "
    "config_fingerprint, sealed, recorded_at"
)

_DECISION_COLUMNS = (
    "decision_id, plan_dir, access, allowed, available, failure, reason, summary, "
    "config_fingerprint, request_digest, recorded_at"
)

_ACTION_COLUMNS = (
    "action_id, plan_dir, incident_id, operation, target, status, epoch, "
    "config_fingerprint, request_digest, progress_token, attempted_at, settled_at"
)

_WATCH_LEASE_COLUMNS = (
    "plan_dir, watcher_id, run_id, host, pid, expires_at, claimed_at, "
    "renewed_at, released_at"
)


def _watch_lease(row: sqlite3.Row) -> WatchLease:
    return WatchLease(
        plan_dir=str(row["plan_dir"]),
        watcher_id=str(row["watcher_id"]),
        run_id=str(row["run_id"]),
        host=str(row["host"]),
        pid=int(row["pid"]),
        expires_at=float(row["expires_at"]),
        claimed_at=str(row["claimed_at"]),
        renewed_at=str(row["renewed_at"]),
        released_at=_optional_text(row["released_at"]),
    )


def _incident(row: sqlite3.Row) -> IncidentRecord:
    return IncidentRecord(
        incident_id=int(row["incident_id"]),
        plan_dir=str(row["plan_dir"]),
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        status=_incident_status(str(row["status"])),
        summary=str(row["summary"]),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        opened_at=str(row["opened_at"]),
        settled_at=_optional_text(row["settled_at"]),
    )


def _evidence(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=int(row["evidence_id"]),
        incident_id=int(row["incident_id"]),
        plan_dir=str(row["plan_dir"]),
        alias=str(row["alias"]),
        kind=str(row["kind"]),
        locator=str(row["locator"]),
        digest=str(row["digest"]),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        sealed=bool(row["sealed"]),
        recorded_at=str(row["recorded_at"]),
    )


def _decision(row: sqlite3.Row) -> DecisionRecord:
    return DecisionRecord(
        decision_id=int(row["decision_id"]),
        plan_dir=str(row["plan_dir"]),
        access=str(row["access"]),
        allowed=bool(row["allowed"]),
        available=bool(row["available"]),
        failure=_optional_text(row["failure"]),
        reason=str(row["reason"]),
        summary=str(row["summary"]),
        config_fingerprint=str(row["config_fingerprint"]),
        request_digest=str(row["request_digest"]),
        recorded_at=str(row["recorded_at"]),
    )


def _action(row: sqlite3.Row, receipt: ReceiptRecord | None) -> ActionRecord:
    return ActionRecord(
        action_id=int(row["action_id"]),
        plan_dir=str(row["plan_dir"]),
        incident_id=_optional_int(row["incident_id"]),
        operation=str(row["operation"]),
        target=str(row["target"]),
        status=_action_status(str(row["status"])),
        epoch=int(row["epoch"]),
        config_fingerprint=str(row["config_fingerprint"]),
        request_digest=str(row["request_digest"]),
        progress_token=str(row["progress_token"]),
        attempted_at=str(row["attempted_at"]),
        settled_at=_optional_text(row["settled_at"]),
        receipt=receipt,
    )


def _incident_status(value: str) -> IncidentStatus:
    """Read a stored status, refusing one this build does not know.

    A row written by a newer ASL naming a status this one has never heard of is
    an unreadable record, not an ``open`` one: guessing would put an incident
    into a bucket some caller then acts on.
    """
    if value not in INCIDENT_STATUSES:
        raise LedgerError(f"stored incident status {value!r} is not a known status")
    return value


def _action_status(value: str) -> ActionStatus:
    """Read a stored action status, refusing one this build does not know."""
    if value not in ACTION_STATUSES:
        raise LedgerError(f"stored action status {value!r} is not a known status")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
