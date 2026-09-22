"""Moving a deployed Companion installation onto this package, and back off it.

Two prefixes already exist on two hosts: a systemd user service on the Pi and a
launch agent on the Mac, both installed by the now-retired Companion lifecycle.
Neither can be re-provisioned. The connector token was handed out once and is stored as
a digest; the database holds months of run history whose ``run_key`` values are
already published in notification links; the incident rows are identities an
operator has seen. So the upgrade is a migration of what is there rather than a
fresh install beside it, and this module is that migration written down.

Five rules, and each one is a way the obvious version of this would lose
something:

* **Nothing is migrated implicitly.** :class:`Migration` is an operator action
  with a :meth:`~Migration.plan` that changes nothing and names every step it
  would take. An install that quietly rewrote the prefix it found would give an
  operator no moment at which to say no.
* **Identities are copied, never recomputed.** The database moves through
  SQLite's own online backup and then through
  :func:`asl_workbench.store.migrate`, so every ``run_key``, ``plan_key`` and
  ``incident_id`` is the one that was already there. A migration that rebuilt
  them from source data would break every link the Pi has published and split
  every open incident in two.
* **An uncertain action is closed, never repeated.** The connector's journal is
  reconciled by :func:`reconcile_journal` the way a restart reconciles it:
  ``pending`` never ran and becomes ``expired``; anything still open was
  attempted and becomes ``unconfirmed``. Neither carries a reply body or an
  action token, because the journal has no column for either -- which is what
  makes "the migration cannot re-send an answer" a property of the file rather
  than a promise about this code.
* **The whole prefix is backed up, not the database.** The retired snapshot
  covered three files. :data:`BACKUP_MEMBERS` plus the integration receipts is
  what an installation actually consists of, and a restore that recreated the
  database beside receipts describing host resources from another era would be
  a restore of half a system.
* **A restore never revives a revoked credential.** Credential *stores* are the
  one member a backup does not write back: revocation is a decision made after
  the backup was taken, and rolling an application back is not a reason to undo
  it. See :func:`restore`.

The rollback half lives next door in :mod:`asl_workbench.lifecycle`, which owns
the switch: this module supplies :func:`paired_backup` -- which state snapshot
goes with which release -- and :func:`restore_state`, and the lifecycle calls
both inside the same runtime lock that stops the service.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from typing import Any

from .history import checkpoint_document, checkpoints_from_document
from .installation import (
    BACKUPS_DIRECTORY,
    CONFIG_PATH,
    CONNECTOR_TOKEN_PATH,
    DATABASE_FILE,
    DATABASE_PATH,
    INTEGRATION_LOCK,
    LIFECYCLE_LOCK,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    READY_FILE,
    RECEIPTS_DIRECTORY,
    RUNTIME_LOCK,
    RUNTIME_LOCK_FILE,
    SECRETS_PATH,
    STATE_DIRECTORY,
    SUMMARY_TOKEN_PATH,
    InstallationError,
    atomic,
    document,
    lock,
    private,
    safe_prefix,
    write_json,
)
from .integrations import PI_SITE, IntegrationSite, service_endpoint
from .observation import ObservationPolicy
from .operations import (
    DETAIL_CONNECTOR_RESTARTED,
    OPERATION_ACCEPTED,
    OPERATION_DISPATCHED,
    OPERATION_EXPIRED,
    OPERATION_PENDING,
    OPERATION_STATUSES,
    OPERATION_UNCONFIRMED,
    OperationError,
    advance_record,
    persist_operation,
)
from .store import StoreError, WorkbenchStore

#: The deployment roles, spelled here rather than imported from
#: :mod:`asl_workbench.lifecycle`: the lifecycle imports this module, and a
#: migration that imported it back would close the loop.
ROLE_CONNECTOR = "connector"
ROLE_SERVICE = "service"

# -- What the Companion left behind -----------------------------------------
#
# Paths an already-deployed prefix carries. They are facts about installed
# hosts rather than choices, and the only one that has to change is the
# database: `asl_workbench.installation.DATABASE_FILE` is what every release
# opens, and a release cannot be asked to look for two names.

LEGACY_DATABASE_FILE = "companion.sqlite3"
LEGACY_DATABASE_PATH = f"{STATE_DIRECTORY}/{LEGACY_DATABASE_FILE}"

#: The connector's dispatch journal and its history checkpoint, by the names
#: the Companion gave them. Both keep their names: a rename with no reader on
#: the other side would be churn that an interrupted migration could only make
#: harder to reason about.
JOURNAL_FILE = "operations.json"
JOURNAL_PATH = f"{STATE_DIRECTORY}/{JOURNAL_FILE}"
CHECKPOINT_FILE = "history.json"
CHECKPOINT_PATH = f"{STATE_DIRECTORY}/{CHECKPOINT_FILE}"

#: ``companion/commands.py``'s ``JOURNAL_SCHEMA_VERSION``. Mirrored rather than
#: imported -- this package may not import the Pi's -- and asserted against a
#: journal this package writes back out under the same number, because the
#: Companion is still the application that may have to read it after a
#: rollback.
JOURNAL_SCHEMA_VERSION = 1

#: The legacy connector configuration's fields, and this package's. The two
#: differences are the whole of the connector translation: ``harnesses`` named
#: a Mac-side selection the portable connector no longer reads, and
#: ``target_id`` is what the transport hello is judged against.
LEGACY_CONNECTOR_KEYS: frozenset[str] = frozenset(
    {"role", "settings", "connector_id", "asl", "harnesses", "asl_state", "plan_roots"}
)
CONNECTOR_KEYS: frozenset[str] = frozenset(
    {"role", "settings", "connector_id", "target_id", "asl", "asl_state", "plan_roots"}
)

#: The settings an installed service's configuration is reduced to. Every other
#: key the Companion's ``config.py`` carried described behaviour this package
#: decides elsewhere, and a configuration that kept them would invite an
#: operator to set one and watch nothing happen.
SERVICE_SETTINGS_KEYS: tuple[str, ...] = (
    "owner_identity",
    "observation_only",
    "bind_host",
    "bind_port",
    "public_origin",
)

# -- The migration record ---------------------------------------------------

#: Written before the first durable write and removed after the last one. While
#: it exists the prefix is mid-migration, and :meth:`Migration.recover` is the
#: only thing that should touch it.
MIGRATION_PATH = "migration.json"
MIGRATION_SCHEMA_VERSION = 1

STEP_CONFIGURATION = "configuration"
STEP_CREDENTIALS = "credentials"
STEP_DATABASE = "database"
STEP_CHECKPOINTS = "checkpoints"
STEP_JOURNAL = "journal"
STEP_RECEIPTS = "receipts"

#: In the order they are applied. The database goes first because it is the
#: step with the most to lose, and the receipts last because their step only
#: reads.
MIGRATION_STEPS: tuple[str, ...] = (
    STEP_DATABASE,
    STEP_CONFIGURATION,
    STEP_CREDENTIALS,
    STEP_CHECKPOINTS,
    STEP_JOURNAL,
    STEP_RECEIPTS,
)

# -- Backups ----------------------------------------------------------------

#: One backup directory holds the prefix's members at their prefix-relative
#: paths plus this manifest, which is what turns "a directory appeared" into "a
#: set of files whose bytes are known".
BACKUP_MANIFEST = "manifest.json"
BACKUP_SCHEMA_VERSION = 1

#: Everything a migration touches, plus what a restore of one would be
#: incomplete without. The Companion's own snapshot covered the first three of
#: these; the rest is the expansion this module exists to make -- the issued
#: tokens, the connector's journal and checkpoint, and (added separately,
#: because their names are not fixed) the integration receipts.
BACKUP_MEMBERS: tuple[str, ...] = (
    CONFIG_PATH,
    SECRETS_PATH,
    CONNECTOR_TOKEN_PATH,
    SUMMARY_TOKEN_PATH,
    DATABASE_PATH,
    LEGACY_DATABASE_PATH,
    JOURNAL_PATH,
    CHECKPOINT_PATH,
)

#: Members copied through SQLite rather than as bytes. A file copy of an open
#: database copies a moment that never existed.
DATABASE_MEMBERS: frozenset[str] = frozenset({DATABASE_PATH, LEGACY_DATABASE_PATH})

#: Members a restore leaves alone unless it is told otherwise, and the key that
#: makes one of them a credential store rather than an issued token.
CREDENTIAL_MEMBERS: frozenset[str] = frozenset(
    {SECRETS_PATH, CONNECTOR_TOKEN_PATH, SUMMARY_TOKEN_PATH}
)
CREDENTIAL_STORE_KEY = "credentials"

#: The middle segment that marks a snapshot as data kept *after* an upgrade,
#: set aside by a rollback rather than taken before a switch. It is what keeps
#: :func:`paired_backup` from ever offering post-upgrade data as the state a
#: previous release was paired with.
RETAINED_MARKER = "retained"

#: ``<nanoseconds>-<first twelve of the artifact digest>``, which is how
#: :meth:`asl_workbench.lifecycle.Installation._switch` names the snapshot it
#: takes before it puts a release in place. Matched in full rather than by
#: suffix: ``<nanoseconds>-retained-<digest>`` ends the same way and means the
#: opposite thing.
_ARTIFACT_SEGMENT = 12
_SNAPSHOT_NAME = re.compile(r"(?P<stamp>[0-9]+)-(?P<artifact>[0-9a-f]{1,12})\Z")

#: What one JSON document under the prefix may weigh. The journal and the
#: checkpoint are small by construction -- a bounded number of open operations,
#: one entry per plan directory -- so a large one is a file that is not what it
#: claims to be, and parsing it first is how that is discovered too late.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: How many files one backup may hold. The members above plus receipts for a
#: host's worth of integrations, with room to spare.
MAX_BACKUP_MEMBERS = 256


#: The statuses a migration answers for. ``unconfirmed`` is deliberately not
#: among them: it already says "nobody can account for this", and restamping it
#: would replace whatever detail code explained it with this one.
UNRECONCILED_STATUSES: frozenset[str] = frozenset(
    {OPERATION_PENDING, OPERATION_DISPATCHED, OPERATION_ACCEPTED}
)


class MigrationError(InstallationError):
    """A migration, a backup or a restore refused rather than acted."""


# ---------------------------------------------------------------------------
# Pure translations


def reconcile_journal(
    records: Iterable[Mapping[str, Any]], *, now: float
) -> list[dict[str, Any]]:
    """Close every operation the migrated connector cannot account for.

    The same two answers a restart gives, for the same reason. ``pending`` is
    the one state that demonstrably never ran -- admission and attempt are
    separate steps -- so it expires. Everything else still open was attempted,
    and nothing on this side of the wire can establish that it did not happen,
    so it becomes ``unconfirmed``: not settled, still open to late evidence,
    and closed to any further attempt.

    Nothing is re-dispatched and nothing is re-sent. The records carry no reply
    text and no action token to re-send *with*, and the projection below is the
    same allowlist the journal is written through, so a legacy record that
    somehow carried one loses it here rather than downstream.
    """
    reconciled: list[dict[str, Any]] = []
    for entry in records:
        if not isinstance(entry, Mapping):
            continue
        operation_id = entry.get("operation_id")
        status = entry.get("status")
        if not isinstance(operation_id, str) or not operation_id:
            continue
        if not isinstance(status, str) or status not in OPERATION_STATUSES:
            continue
        try:
            record = persist_operation(entry)
            if status in UNRECONCILED_STATUSES:
                record = advance_record(
                    record,
                    OPERATION_EXPIRED
                    if status == OPERATION_PENDING
                    else OPERATION_UNCONFIRMED,
                    now=now,
                    detail_code=DETAIL_CONNECTOR_RESTARTED,
                )
        except OperationError:
            # A record this package cannot project is a record it cannot
            # reason about either. Dropping it is the Companion's own posture
            # for an unreadable journal, and the alternative -- carrying it
            # forward unexamined -- is carrying forward the one thing that
            # could name an action.
            continue
        reconciled.append(record)
    reconciled.sort(key=lambda record: str(record["operation_id"]))
    return reconciled


def journal_document(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The durable journal document, under the version both halves read."""
    return {
        "version": JOURNAL_SCHEMA_VERSION,
        "operations": [dict(record) for record in records],
    }


def migrate_journal(data: object, *, now: float) -> dict[str, Any]:
    """Read one legacy journal document and return its reconciled successor.

    A document from another schema yields an empty journal rather than a
    refusal, matching ``companion/commands.py``: the operations it describes
    are, by the time anybody is migrating, certainly over, and refusing to
    migrate over a file nobody can read would strand an installation on a
    journal entry.
    """
    if not isinstance(data, Mapping) or data.get("version") != JOURNAL_SCHEMA_VERSION:
        return journal_document([])
    entries = data.get("operations")
    return journal_document(
        reconcile_journal(entries if isinstance(entries, list) else [], now=now)
    )


def migrate_checkpoints(data: object) -> dict[str, Any]:
    """Carry a legacy history checkpoint into this package's payload shape.

    The Companion's payload is this one without ``open_questions``, so the
    carry is a read and a write rather than a rewrite -- and it is worth doing
    explicitly because the alternative is discovering at the first import that
    the document was silently discarded and a week of history is being read
    from the top again.
    """
    return checkpoint_document(list(checkpoints_from_document(data).values()))


def migrate_connector_configuration(
    data: Mapping[str, Any], *, target_id: str
) -> dict[str, Any]:
    """Translate a legacy connector configuration, given the target it pairs to.

    ``target_id`` cannot be derived: the Companion never recorded one, and the
    transport hello is judged against it, so a value invented here would attach
    the Mac to a service nobody named. It is a required argument for exactly
    that reason.
    """
    if not target_id or len(target_id) > 128:
        raise MigrationError("a connector migration requires a target id")
    unknown = sorted(set(data) - LEGACY_CONNECTOR_KEYS - CONNECTOR_KEYS)
    if unknown:
        raise MigrationError("connector configuration carries unknown fields")
    migrated = {key: data[key] for key in CONNECTOR_KEYS if key in data}
    migrated["role"] = ROLE_CONNECTOR
    migrated["target_id"] = target_id
    if set(migrated) != CONNECTOR_KEYS:
        raise MigrationError("connector configuration is missing required fields")
    return migrated


def migrate_service_configuration(
    data: Mapping[str, Any], *, site: IntegrationSite
) -> dict[str, Any]:
    """Reduce a legacy service configuration to the fields this package reads.

    ``observation_only`` is made explicit rather than left to a default. The
    Companion's default was "mutations permitted" and this package's is "observe
    only", so a configuration that simply omitted it would change meaning in
    the one direction an operator would not notice until a mutation was
    refused.
    """
    raw = data.get("settings")
    if not isinstance(raw, Mapping):
        raise MigrationError("service configuration has no settings object")
    settings: dict[str, Any] = {
        key: raw[key] for key in SERVICE_SETTINGS_KEYS if key in raw
    }
    settings["observation_only"] = bool(raw.get("observation_only", True))
    owner = settings.get("owner_identity")
    if not isinstance(owner, str) or not owner:
        raise MigrationError("service configuration has no owner identity")
    for key, expected in (
        ("bind_host", site.bind_host),
        ("bind_port", site.bind_port),
        ("public_origin", site.public_origin),
    ):
        # Refused rather than rewritten. A prefix configured for a different
        # endpoint is a deployment this migration was not shown, and silently
        # pointing it at this site would move a running service.
        if key in settings and settings[key] != expected:
            raise MigrationError("service configuration names another endpoint")
    return {"role": ROLE_SERVICE, "settings": settings}


# ---------------------------------------------------------------------------
# Files


def digest_of(path: Path) -> str:
    """The SHA-256 of one file, read in bounded pieces."""
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_document(path: Path) -> object:
    """Read one bounded JSON value from a private file under the prefix."""
    private(path)
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise MigrationError("document exceeds size bound")
    try:
        return json.loads(path.read_bytes())
    except ValueError as error:
        raise MigrationError("document is not readable JSON") from error


def copy_database(source: Path, destination: Path) -> None:
    """Copy one database through SQLite, and prove the copy is a database.

    The online backup API copies a consistent moment; the integrity check is
    what turns "a file appeared" into "a database a release could open". Both
    are the Companion's own snapshot rules, kept because they are the reason
    its backups were restorable.
    """
    # The write-ahead files belong to the database that is being replaced, not
    # to the one arriving. Leaving them beside a restored file is how a
    # rollback ends up with a database and a log that disagree about which
    # transactions happened.
    for suffix in ("-wal", "-shm"):
        destination.with_name(destination.name + suffix).unlink(missing_ok=True)
    atomic(destination, b"")
    readable = source.as_uri() + "?mode=ro"
    try:
        with (
            closing(sqlite3.connect(readable, uri=True)) as original,
            closing(sqlite3.connect(destination)) as copy,
        ):
            original.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("copied database failed its integrity check")
    except sqlite3.Error as error:
        raise MigrationError("database could not be copied") from error
    destination.chmod(PRIVATE_FILE_MODE)


def members_of(prefix: Path, *, receipts: bool = True) -> tuple[str, ...]:
    """Every backup member this prefix actually carries, prefix-relative.

    Receipts are enumerated rather than listed: their names come from whichever
    integrations were run, and a fixed list would quietly stop backing up the
    next one somebody adds. ``integration.lock`` is not a receipt and is not
    collected -- restoring a lock file would restore a claim about a process
    that is long gone.
    """
    found = [name for name in BACKUP_MEMBERS if (prefix / name).is_file()]
    directory = prefix / RECEIPTS_DIRECTORY
    if receipts and directory.is_dir():
        found += sorted(
            f"{RECEIPTS_DIRECTORY}/{path.name}"
            for path in directory.iterdir()
            if path.is_file() and path.suffix == ".json" and not path.is_symlink()
        )
    if len(found) > MAX_BACKUP_MEMBERS:
        raise MigrationError("prefix holds more members than a backup may carry")
    return tuple(found)


def backup(
    prefix: Path, destination: Path, *, members: Sequence[str] | None = None
) -> dict[str, Any]:
    """Copy one prefix's members into a fresh directory, and say what is in it.

    Staged into a temporary directory and renamed into place, so an interrupted
    backup leaves nothing that a restore could later mistake for a complete
    one. No lock is taken here: every caller in this package already holds the
    lifecycle lock, and ``flock`` is per open file description -- taking it
    again from the same process would refuse rather than nest.
    """
    safe_prefix(prefix)
    names = tuple(members) if members is not None else members_of(prefix)
    if destination.exists():
        raise MigrationError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    stage = Path(tempfile.mkdtemp(dir=destination.parent, prefix="backup-"))
    files: dict[str, str] = {}
    for name in names:
        source = prefix / name
        private(source)
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        if name in DATABASE_MEMBERS:
            copy_database(source, target)
        else:
            atomic(target, source.read_bytes())
        files[name] = digest_of(target)
    write_json(
        stage / BACKUP_MANIFEST, {"version": BACKUP_SCHEMA_VERSION, "files": files}
    )
    os.replace(stage, destination)
    return {"version": BACKUP_SCHEMA_VERSION, "files": files}


def verify(source: Path) -> dict[str, str]:
    """Read one backup's manifest and prove the directory matches it.

    Both directions. A missing file is an incomplete backup; a file the
    manifest does not name is a directory somebody has added to, and restoring
    from either would put bytes in a prefix that nothing vouched for.
    """
    private(source, directory=True)
    manifest = document(source / BACKUP_MANIFEST)
    if manifest.get("version") != BACKUP_SCHEMA_VERSION:
        raise MigrationError("unsupported backup schema")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise MigrationError("backup manifest has no file list")
    present = {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file()
    } - {BACKUP_MANIFEST}
    if present != set(files):
        raise MigrationError("backup contents do not match its manifest")
    for name, digest in sorted(files.items()):
        path = source / name
        private(path)
        if not isinstance(digest, str) or digest_of(path) != digest:
            raise MigrationError(f"backup member does not match its digest: {name}")
    return {str(name): str(digest) for name, digest in files.items()}


def restore(
    source: Path, prefix: Path, *, credentials: bool = False
) -> dict[str, list[str]]:
    """Write a verified backup back into a prefix, one member at a time.

    Credential stores are the member this refuses to write. Revocation is a
    decision taken *after* the backup was made -- a token handed to somebody
    who should no longer have it, a pairing withdrawn -- and an application
    being put back is not a reason to undo it. The live document stands, and
    the member is reported as preserved rather than silently skipped, so an
    operator reading the result can see that it was a decision.

    ``credentials`` widens this to the issued token files -- the connector's
    token and the summary token -- which carry no revocation state of their own
    (the store that can disable them is the service's). It is what makes
    undoing an interrupted migration possible, because moving that token is one
    of the things a migration does.
    """
    safe_prefix(prefix)
    files = verify(source)
    report: dict[str, list[str]] = {"restored": [], "preserved": []}
    for name in sorted(files):
        staged = source / name
        target = prefix / name
        if name in CREDENTIAL_MEMBERS and not _credential_restorable(
            staged, target, allowed=credentials
        ):
            report["preserved"].append(name)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        if name in DATABASE_MEMBERS:
            copy_database(staged, target)
        else:
            atomic(target, staged.read_bytes())
            target.chmod(PRIVATE_FILE_MODE)
        report["restored"].append(name)
    return report


def _credential_restorable(staged: Path, target: Path, *, allowed: bool) -> bool:
    """Whether one credential member may be written back over a live prefix.

    Never for a document carrying a credential store, whichever side carries
    it: writing the backup's store back would re-enable whatever it recorded as
    enabled, and writing it over a store that has since disabled something
    would do it without saying so.
    """
    if not allowed:
        return False
    return not any(_carries_credential_store(path) for path in (staged, target))


def _carries_credential_store(path: Path) -> bool:
    """Whether one file holds a credential store.

    A token file holds a token and nothing else, so a file that does not parse
    as a JSON object is not a store. The size bound is still enforced rather
    than swallowed: an unreadably large file here would be answered "not a
    store", and the whole point of the question is that answering it wrongly
    reinstates somebody.
    """
    if not path.is_file():
        return False
    private(path)
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise MigrationError("credential document exceeds size bound")
    try:
        value = json.loads(path.read_bytes())
    except ValueError:
        return False
    return isinstance(value, Mapping) and CREDENTIAL_STORE_KEY in value


# ---------------------------------------------------------------------------
# State, and the release it goes with


def paired_backup(prefix: Path, artifact: str) -> Path | None:
    """The state snapshot taken just before ``artifact`` was put in place.

    That snapshot is the data the *previous* release was running against, which
    is exactly what a rollback to that release needs: an application and a data
    set that were a working pair. The newest match wins, because a release can
    be installed more than once and only the last switch describes the state it
    displaced.
    """
    directory = prefix / BACKUPS_DIRECTORY
    if not directory.is_dir():
        return None
    wanted = str(artifact)[:_ARTIFACT_SEGMENT]
    matches: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        found = _SNAPSHOT_NAME.fullmatch(path.name)
        if found and found.group("artifact") == wanted:
            matches.append((int(found.group("stamp")), path))
    if not matches:
        return None
    return max(matches)[1]


def retained_name(stamp: int, artifact: str) -> str:
    """What a rollback calls the post-upgrade data it sets aside.

    Deliberately not a name :func:`paired_backup` can match. Post-upgrade data
    offered later as "the state this release was paired with" would pair a
    release with data written by its successor.
    """
    return f"{stamp}-{RETAINED_MARKER}-{str(artifact)[:_ARTIFACT_SEGMENT]}"


def restore_state(state: Path, source: Path) -> tuple[str, ...]:
    """Put one state snapshot back, replacing whatever is in ``state`` now.

    The inverse of :func:`asl_workbench.lifecycle.snapshot` and skipping the
    same three things for the same reason: the lock, the readiness note and the
    write-ahead files describe the process that was running, not the
    observations it recorded. The caller holds the runtime lock, so there is no
    such process while this runs.

    Files in ``state`` that the snapshot does not name are removed. A rollback
    that left a newer release's file beside a restored database would leave the
    installation carrying a fact from a version it is no longer running.
    """
    private(source, directory=True)
    private(state, directory=True)
    restored: list[str] = []
    for entry in sorted(source.iterdir()):
        if entry.name in (RUNTIME_LOCK_FILE, READY_FILE) or entry.name.endswith(
            ("-wal", "-shm")
        ):
            continue
        if entry.is_symlink() or not entry.is_file():
            raise MigrationError("unexpected entry in state snapshot")
        if entry.name == DATABASE_FILE:
            copy_database(entry, state / entry.name)
        else:
            atomic(state / entry.name, entry.read_bytes())
            (state / entry.name).chmod(PRIVATE_FILE_MODE)
        restored.append(entry.name)
    for entry in sorted(state.iterdir()):
        if (
            entry.name in restored
            or entry.name in (RUNTIME_LOCK_FILE, READY_FILE)
            or entry.name.endswith(("-wal", "-shm"))
        ):
            continue
        if entry.is_file() and not entry.is_symlink():
            entry.unlink()
    return tuple(restored)


# ---------------------------------------------------------------------------
# The migration


class Migration:
    """One prefix, moved from the Companion's layout onto this package's.

    Built around a plan that changes nothing, an apply that writes a record
    before its first durable write, and a recover that finishes undoing an
    apply that stopped halfway. The three exist as separate operations because
    a migration is the one lifecycle step whose input is a system somebody has
    been running for months: it has to be describable before it is run, and
    reversible while it is.
    """

    def __init__(
        self,
        prefix: Path,
        *,
        target_id: str | None = None,
        site: IntegrationSite = PI_SITE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.prefix = prefix
        self.target_id = target_id
        self.site = site
        self.clock = clock

    # -- Reading -----------------------------------------------------------

    def role(self) -> str:
        """Which half of the deployment this prefix is, legacy layout or not."""
        try:
            data = read_document(self.prefix / CONFIG_PATH)
        except OSError as error:
            raise MigrationError("prefix carries no configuration") from error
        if not isinstance(data, Mapping):
            raise MigrationError("prefix carries no configuration")
        role = data.get("role")
        if role not in (ROLE_CONNECTOR, ROLE_SERVICE):
            raise MigrationError("prefix is not a Companion or Workbench installation")
        return str(role)

    def plan(self) -> dict[str, Any]:
        """Name every step this migration would take, and take none of them.

        A step is listed when the prefix carries something the step would
        change. A plan with no steps is the normal answer for a prefix that has
        already been migrated, which is what makes running this twice safe.
        """
        safe_prefix(self.prefix)
        now = self.clock()
        role = self.role()
        configuration = read_document(self.prefix / CONFIG_PATH)
        if not isinstance(configuration, Mapping):
            raise MigrationError("prefix carries no configuration")
        steps: list[str] = []
        if (self.prefix / LEGACY_DATABASE_PATH).is_file():
            steps.append(STEP_DATABASE)
        if dict(configuration) != self._migrated_configuration(role, configuration):
            steps.append(STEP_CONFIGURATION)
        if role == ROLE_CONNECTOR and (self.prefix / SECRETS_PATH).is_file():
            steps.append(STEP_CREDENTIALS)
        if self._rewrites(self.prefix / CHECKPOINT_PATH, migrate_checkpoints):
            steps.append(STEP_CHECKPOINTS)
        if self._rewrites(
            self.prefix / JOURNAL_PATH, lambda data: migrate_journal(data, now=now)
        ):
            steps.append(STEP_JOURNAL)
        # Re-reading the receipts changes nothing; it is how a migration finds
        # out that the files it is about to vouch for are still readable. So it
        # accompanies work rather than being work, and a prefix with nothing
        # left to migrate is not given a step for it.
        if steps and members_of(self.prefix, receipts=True) != members_of(
            self.prefix, receipts=False
        ):
            steps.append(STEP_RECEIPTS)
        return {
            "version": MIGRATION_SCHEMA_VERSION,
            "role": role,
            "steps": [step for step in MIGRATION_STEPS if step in steps],
            "members": list(members_of(self.prefix)),
        }

    @staticmethod
    def _rewrites(
        path: Path, migrated: Callable[[Mapping[str, Any]], dict[str, Any]]
    ) -> bool:
        """Whether migrating one document would produce something else.

        Asked instead of "does the file exist", because both documents this is
        used for keep their names across the migration: it is their contents
        that change. Reconciling an already reconciled journal is a fixpoint,
        so the answer settles to no and the migration stops listing a step.
        """
        if not path.is_file():
            return False
        stored = read_document(path)
        if not isinstance(stored, Mapping):
            raise MigrationError(f"expected a JSON object: {path.name}")
        return dict(stored) != migrated(stored)

    def _migrated_configuration(
        self, role: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        if role == ROLE_CONNECTOR:
            return migrate_connector_configuration(
                data, target_id=self.target_id or str(data.get("target_id") or "")
            )
        return migrate_service_configuration(data, site=self.site)

    # -- Applying ----------------------------------------------------------

    def apply(self) -> dict[str, Any]:
        """Back the prefix up, then carry out the plan, or put it all back.

        Everything that can refuse refuses before the backup, and everything
        after the migration record is written is either finished or undone by
        :meth:`recover`. The locks are the ones an installation already uses:
        the lifecycle lock so no installer runs beside this, the runtime lock
        so the service is demonstrably not serving, and the integration lock so
        no receipt is written while the receipts are being read.
        """
        safe_prefix(self.prefix)
        if (self.prefix / MIGRATION_PATH).exists():
            raise MigrationError("interrupted migration; run recover before continuing")
        # A connector prefix has no receipts and so may have no directory to
        # take the integration lock in. Creating an empty one is idempotent and
        # is not a migration step: it is where the lock lives.
        (self.prefix / RECEIPTS_DIRECTORY).mkdir(
            exist_ok=True, mode=PRIVATE_DIRECTORY_MODE
        )
        with (
            lock(self.prefix / LIFECYCLE_LOCK),
            lock(self.prefix / RUNTIME_LOCK),
            lock(self.prefix / RECEIPTS_DIRECTORY / INTEGRATION_LOCK),
        ):
            plan = self.plan()
            if not plan["steps"]:
                return {"migrated": False, "steps": [], "backup": None, **plan}
            destination = self.prefix / BACKUPS_DIRECTORY / f"{time.time_ns()}-legacy"
            record = {
                **plan,
                "backup": destination.name,
                "started_at": self.clock(),
            }
            write_json(self.prefix / MIGRATION_PATH, record)
            try:
                backup(self.prefix, destination)
                for step in plan["steps"]:
                    getattr(self, f"_{step}")()
                self._verify(plan["role"])
            except BaseException:
                # The same unwinding a later `recover` would do, taken now
                # because this process is still here to do it. Anything else
                # would leave a prefix whose only route back is a command
                # nobody has been told to run.
                self._unwind(destination)
                (self.prefix / MIGRATION_PATH).unlink(missing_ok=True)
                raise
            (self.prefix / MIGRATION_PATH).unlink()
        return {"migrated": True, **record}

    def _database(self) -> None:
        """Move the observations onto the name every release opens.

        Through SQLite's backup API and then through the store's own
        migrations, so the rows -- and therefore every run key, plan key and
        incident id in them -- are the ones that were already there. The legacy
        file is unlinked only after the new one has been opened and checked:
        until then it is the only copy outside the backup.
        """
        source = self.prefix / LEGACY_DATABASE_PATH
        destination = self.prefix / DATABASE_PATH
        if destination.exists():
            raise MigrationError("prefix carries both databases; recover first")
        copy_database(source, destination)
        try:
            with WorkbenchStore.open(
                destination,
                policy=ObservationPolicy(),
                created_at=self.clock(),
            ) as store:
                if not store.healthy():
                    raise MigrationError("migrated store is unhealthy")
        except StoreError as error:
            raise MigrationError("migrated store refused this release") from error
        source.unlink()
        for suffix in ("-wal", "-shm"):
            (self.prefix / (LEGACY_DATABASE_PATH + suffix)).unlink(missing_ok=True)

    def _configuration(self) -> None:
        role = self.role()
        data = read_document(self.prefix / CONFIG_PATH)
        if not isinstance(data, Mapping):
            raise MigrationError("prefix carries no configuration")
        write_json(self.prefix / CONFIG_PATH, self._migrated_configuration(role, data))
        (self.prefix / CONFIG_PATH).chmod(PRIVATE_FILE_MODE)

    def _credentials(self) -> None:
        """Move the connector's issued token to the path this package reads.

        The Companion kept it in ``credentials/secrets.json``, which on a
        service prefix is the credential store. One name for two different
        documents is how a connector prefix ends up being read as a half
        provisioned service, so the connector's token moves and the old file
        goes.
        """
        source = self.prefix / SECRETS_PATH
        data = read_document(source)
        if not isinstance(data, Mapping) or set(data) != {"connector_token"}:
            raise MigrationError("connector secrets carry more than an issued token")
        token = data["connector_token"]
        if not isinstance(token, str) or not token:
            raise MigrationError("connector secrets carry no issued token")
        destination = self.prefix / CONNECTOR_TOKEN_PATH
        if destination.is_file() and read_document(destination) != dict(data):
            raise MigrationError("prefix already carries a different connector token")
        write_json(destination, {"connector_token": token})
        destination.chmod(PRIVATE_FILE_MODE)
        source.unlink()

    def _checkpoints(self) -> None:
        path = self.prefix / CHECKPOINT_PATH
        write_json(path, migrate_checkpoints(read_document(path)))
        path.chmod(PRIVATE_FILE_MODE)

    def _journal(self) -> None:
        path = self.prefix / JOURNAL_PATH
        write_json(path, migrate_journal(read_document(path), now=self.clock()))
        path.chmod(PRIVATE_FILE_MODE)

    def _receipts(self) -> None:
        """Read every receipt, and change none of them.

        The Companion wrote receipts this package's
        :class:`~asl_workbench.integrations.ReceiptStore` reads unchanged, so
        the migration for them is a proof rather than a rewrite: a receipt that
        cannot be read is found now, while the backup is one step away, instead
        of at the first integration run against a host it describes.
        """
        directory = self.prefix / RECEIPTS_DIRECTORY
        private(directory, directory=True)
        for name in members_of(self.prefix):
            if name.startswith(RECEIPTS_DIRECTORY + "/"):
                document(self.prefix / name)

    def _verify(self, role: str) -> None:
        """Read the migrated prefix back through the readers that will use it."""
        if role == ROLE_SERVICE:
            service_endpoint(self.prefix / CONFIG_PATH, self.site)
            return
        data = read_document(self.prefix / CONFIG_PATH)
        if not isinstance(data, Mapping) or set(data) != CONNECTOR_KEYS:
            raise MigrationError("migrated connector configuration is not readable")
        private(self.prefix / CONNECTOR_TOKEN_PATH)

    # -- Recovering --------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Undo an interrupted migration from the backup it wrote first.

        Safe to repeat, and safe to run against a prefix with no interrupted
        migration: both answer "nothing to do". Members the migration created
        that the backup does not name are removed, because the point is to
        leave a prefix the Companion can still be started against.
        """
        safe_prefix(self.prefix)
        path = self.prefix / MIGRATION_PATH
        if not path.exists():
            return {"recovered": False}
        record = document(path)
        source = self.prefix / BACKUPS_DIRECTORY / str(record.get("backup", ""))
        if not source.is_dir():
            raise MigrationError("interrupted migration names no usable backup")
        with (
            lock(self.prefix / LIFECYCLE_LOCK),
            lock(self.prefix / RUNTIME_LOCK),
        ):
            report = self._unwind(source)
            path.unlink()
        return {"recovered": True, **report}

    def _unwind(self, source: Path) -> dict[str, list[str]]:
        """Put the backed-up members back, and remove the ones it does not name.

        The removal is the half that is easy to leave out and expensive to have
        left out: a half-finished migration's ``state/workbench.db`` sitting
        beside the restored legacy database is a prefix that neither
        application will start against, and every later attempt would refuse on
        the grounds that it cannot tell which database is the real one.
        """
        known = set(verify(source))
        report = restore(source, self.prefix, credentials=True)
        removed: list[str] = []
        for name in members_of(self.prefix):
            if name not in known:
                (self.prefix / name).unlink()
                for suffix in ("-wal", "-shm") if name in DATABASE_MEMBERS else ():
                    (self.prefix / (name + suffix)).unlink(missing_ok=True)
                removed.append(name)
        return {**report, "removed": removed}


__all__ = [
    "BACKUP_MANIFEST",
    "BACKUP_MEMBERS",
    "BACKUP_SCHEMA_VERSION",
    "CHECKPOINT_PATH",
    "CONNECTOR_KEYS",
    "CREDENTIAL_MEMBERS",
    "DATABASE_MEMBERS",
    "JOURNAL_PATH",
    "JOURNAL_SCHEMA_VERSION",
    "LEGACY_CONNECTOR_KEYS",
    "LEGACY_DATABASE_FILE",
    "LEGACY_DATABASE_PATH",
    "MAX_BACKUP_MEMBERS",
    "MAX_DOCUMENT_BYTES",
    "MIGRATION_PATH",
    "MIGRATION_SCHEMA_VERSION",
    "MIGRATION_STEPS",
    "RETAINED_MARKER",
    "ROLE_CONNECTOR",
    "ROLE_SERVICE",
    "SERVICE_SETTINGS_KEYS",
    "STEP_CHECKPOINTS",
    "STEP_CONFIGURATION",
    "STEP_CREDENTIALS",
    "STEP_DATABASE",
    "STEP_JOURNAL",
    "STEP_RECEIPTS",
    "UNRECONCILED_STATUSES",
    "Migration",
    "MigrationError",
    "backup",
    "copy_database",
    "digest_of",
    "journal_document",
    "members_of",
    "migrate_checkpoints",
    "migrate_connector_configuration",
    "migrate_journal",
    "migrate_service_configuration",
    "paired_backup",
    "read_document",
    "reconcile_journal",
    "restore",
    "restore_state",
    "retained_name",
    "verify",
]
