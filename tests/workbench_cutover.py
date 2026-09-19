"""The harness the production cutover is driven and recorded through.

Session 23 moves the two machines that have been running the Companion -- this
Mac and the Pi -- onto the release session 22 staged, and then has to answer
the question :mod:`tests.workbench_acceptance` asks: *did that happen, to this
release, on those machines, and did the person who owns the phone say so?* The
acceptance reader refuses a report. This module is what a report may be built
out of, and it refuses to build one out of anything that was not observed.

It is the production counterpart of :mod:`tests.workbench_staging` and is
deliberately the mirror image of it in one respect. A rehearsal's first duty is
to touch nothing production owns, so that module's central refusal is *this
site shares an identifier with production*. A cutover's first duty is the
opposite: it must act on production and on nothing else, because a cutover
recorded against a moved-aside site would be a rehearsal wearing production's
evidence. So :func:`check_production` refuses a site that has had any owned
identifier moved aside, and :func:`check_prefix_production` refuses a prefix
that is not the installation both hosts have been running all along.

Like the staging harness it is self-contained -- standard library plus
:mod:`asl_workbench` -- because half of it runs on the Pi, which has no
checkout of this repository and receives this one file beside the unpacked
release. Its rules are proved by ``tests/test_workbench_cutover.py``.

Three jobs, and each exists because of a way a cutover goes wrong:

**A backup that is still there afterwards.** A migration takes its own
snapshot inside the prefix, which is the right thing for an unwind and the
wrong thing for a cutover: the whole point of the exercise is that the prefix
is about to be rewritten, and a rollback plan that lives inside the thing being
rewritten is not a rollback plan. :func:`capture_backup` copies every member
the prefix carries into an archive *outside* it -- databases through SQLite's
online backup API, because a file copy of an open database copies a moment that
never existed -- and records each member's digest, each database's integrity
and each table's row count. :func:`verify_backup` reads that archive back and
recomputes all of it. The row counts are what makes catalog and history
continuity a comparison rather than an impression: the counts that went into
the backup are the counts the migrated service has to still be able to show.

**Native halves that both think they are production.** Both hosts install
under one name -- :data:`~asl_workbench.lifecycle.LABEL` and
:data:`~asl_workbench.lifecycle.UNIT`, which are the Companion's own, preserved
on purpose -- so the unified deployment takes the legacy one's place rather
than standing beside it. :func:`check_single_owner` is how that is proved
rather than assumed: it reads the definition the prefix carries and refuses
anything that names a second label, unit or bind port.

**A report that cannot be written by hand.** :class:`Journal` is append-only
and timestamped, and :meth:`Journal.render` refuses to produce a document
unless every check the stage owes was recorded as passed, inside the window
between the release commit and the report, on a named host -- and unless the
operator's own phone acceptance is there too, in the order the plan requires:
readiness asked for and granted, *then* a confirmation asked for and answered,
naming this release, from a device that is neither of the two deployed hosts.
An unanswered confirmation is not a missing field this module fills in with a
default. It is a ``pending`` verdict, which :meth:`Journal.render` refuses, and
that refusal is what the plan means by "an unanswered request leaves acceptance
pending".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import plistlib
import re
import sqlite3
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import fields
from pathlib import Path
from typing import Any

from asl_workbench import lifecycle, migration
from asl_workbench.installation import (
    BACKUPS_DIRECTORY,
    DEPLOYMENT_PATH,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    RELEASES_DIRECTORY,
    atomic,
    document,
)
from asl_workbench.integrations import PI_SITE, IntegrationSite
from asl_workbench.notifications import DEPLOYMENT_PRODUCTION

# ---------------------------------------------------------------------------
# What a cutover is allowed to be

#: The installation both hosts have been running since the Companion was
#: installed, by bare name. The cutover migrates *this* prefix rather than
#: installing beside it, because the plan preserves the installation prefixes.
PRODUCTION_PREFIX_NAME = "asl-companion"

#: The fields of a site that name a resource rather than describe one. This is
#: :data:`tests.workbench_staging.OWNED_SITE_FIELDS`, and the two modules are
#: pinned to each other by ``tests/test_workbench_cutover.py``: a rehearsal
#: moves every one of them aside and a cutover moves none of them.
OWNED_SITE_FIELDS: tuple[str, ...] = (
    "port",
    "bind_port",
    "public_origin",
    "publisher_user",
    "publisher_topic",
    "marker",
    "group_heading",
    "monitor_name",
)

#: The checks session 23 owes, restated here so the Pi half can be driven
#: without this repository. ``tests/test_workbench_cutover.py`` pins this to
#: :data:`tests.workbench_acceptance.STAGE_CHECKS`, so a divergence is a test
#: failure rather than a report the acceptance reader then rejects.
PRODUCTION_CHECKS: tuple[str, ...] = (
    "fresh-backups",
    "installed-versions-recorded",
    "old-connector-stopped",
    "operations-reconciled",
    "migration",
    "unified-deployment-active",
    "workers-preserved",
    "private-url-preserved",
    "catalog-continuity",
    "history-continuity",
    "old-links",
    "authenticated-reads",
    "integrations",
    "single-publisher",
    "isolated-acceptance-fixtures",
    "rollback-available",
)

#: What the operator is asked to confirm from the phone itself, and nothing
#: else. Each one is a thing only a person holding a locked phone can answer.
PHONE_CHECKS: tuple[str, ...] = (
    "locked-phone-notification",
    "deep-link",
    "live-reply",
    "reply-with-resume",
)

#: The two hosts, and the operating system each one must report.
HOST_ROLES: Mapping[str, str] = {"mac": "Darwin", "pi": "Linux"}

#: Where a check may say it was observed.
CHECK_HOSTS: tuple[str, ...] = (*HOST_ROLES, "both")

SCHEMA_VERSION = 1
STAGE = "production"
OUTCOME_PASSED = "passed"
VERDICT_ACCEPTED = "accepted"
VERDICT_PENDING = "pending"
PROVENANCE_ACTUAL_HOST = "actual-host"

#: How far a recorded moment may sit outside the window before it is read as
#: wrong rather than as skew between two machines' clocks.
MAX_CLOCK_SKEW_SECONDS = 300.0

#: Substrings that name a fixture rather than a device. Kept identical to
#: :data:`tests.workbench_acceptance.FORBIDDEN_HOST_MARKERS` -- and pinned to
#: it by the tests -- so a phone this module accepts is a phone the acceptance
#: reader accepts.
FORBIDDEN_HOST_MARKERS: tuple[str, ...] = (
    "localhost",
    "example",
    "test",
    "fake",
    "synthetic",
    "simulated",
    "unittest",
    "dummy",
    "sample",
    "stub",
    "mock",
    "fixture",
    "placeholder",
    "invalid",
)

_REVISION = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_HOSTNAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63}){0,4}")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class CutoverError(AssertionError):
    """A cutover step, or a report built out of one, was refused."""


# ---------------------------------------------------------------------------
# The site and the prefix the cutover acts on


def check_production(
    site: IntegrationSite, production: IntegrationSite = PI_SITE
) -> None:
    """Refuse a site that is not the production one, field by field.

    The inverse of :func:`tests.workbench_staging.check_isolated`, and for the
    inverse reason. A rehearsal that reused one production identifier would be
    a production change with a rehearsal's name on it; a cutover that moved one
    aside would leave the resource it was supposed to take over still pointing
    at the Companion, and would record that as production evidence.

    Every field is compared rather than a representative one, because the ways
    this goes wrong are not equivalent: a moved ``bind_port`` leaves the
    published URL pointing at the old process, a moved ``marker`` orphans the
    Homepage card instead of adopting it, and a moved ``publisher_topic`` mints
    a second publisher for the topic the operator's phone is subscribed to.
    """
    for field in OWNED_SITE_FIELDS:
        expected = getattr(production, field)
        found = getattr(site, field)
        if found != expected:
            raise CutoverError(
                f"cutover site moved the production {field} aside: "
                f"{found!r} is not {expected!r}"
            )
    if site.deployment != DEPLOYMENT_PRODUCTION:
        raise CutoverError(
            f"cutover site is recorded as the {site.deployment!r} deployment"
        )
    if site.notifications is None:
        raise CutoverError(
            "cutover site owns no notification site; the deployment that takes "
            "the Companion's place is the one that publishes to the phone"
        )
    if site.notifications != production.notifications:
        raise CutoverError("cutover site publishes somewhere production does not")
    for field in (item.name for item in fields(production)):
        if field in OWNED_SITE_FIELDS or field in ("deployment", "notifications"):
            continue
        if getattr(site, field) != getattr(production, field):
            raise CutoverError(f"cutover site changed {field}")


def check_prefix_production(
    prefix: Path, production_name: str = PRODUCTION_PREFIX_NAME
) -> None:
    """Refuse a prefix that is not the installation production has been using.

    Name rather than resolution, as in the staging harness and for the same
    reason: half the time the prefix being named is on the other host, so there
    is nothing local to resolve it against. The name is the whole of the
    identity here -- ``asl-companion`` is the directory both native definitions
    point at -- and a cutover aimed anywhere else is a cutover that would leave
    the deployed Companion exactly where it was.
    """
    path = Path(prefix)
    if not path.parts:
        raise CutoverError("a production prefix is required")
    if path.name != production_name:
        raise CutoverError(
            f"{prefix} is not the production installation at {production_name!r}"
        )


# ---------------------------------------------------------------------------
# Backups that outlive the prefix


#: Directories a backup deliberately does not copy, and why each one is safe to
#: leave behind. ``releases/`` holds unpacked artifacts and their zips, every
#: one of which is reproducible from a commit by :func:`lifecycle.pack`;
#: ``backups/`` holds the migration's own in-prefix snapshots, and copying them
#: would nest a backup inside a backup.
BACKUP_EXCLUDED: tuple[str, ...] = (RELEASES_DIRECTORY, BACKUPS_DIRECTORY)


def backup_members(prefix: Path) -> tuple[str, ...]:
    """The members this prefix actually carries, prefix-relative.

    The list comes from the migration rather than a walk of the directory, so a
    backup holds what the release says a prefix *is* -- configuration,
    credentials, databases, the connector's journal and checkpoint, the
    integration receipts -- and :data:`BACKUP_EXCLUDED` is enforced rather than
    assumed, because a member list that grew to include ``releases/`` would
    quietly turn a few megabytes of state into a copy of every artifact ever
    unpacked.
    """
    members = migration.members_of(Path(prefix))
    for member in members:
        if Path(member).parts and Path(member).parts[0] in BACKUP_EXCLUDED:
            raise CutoverError(f"{member} is not state a cutover backup carries")
    return members


BACKUP_SCHEMA_VERSION = 1
BACKUP_MANIFEST = "manifest.json"

#: What one archive may weigh. A prefix without its releases is a few megabytes
#: of database and a handful of small documents.
MAX_BACKUP_BYTES = 512 * 1024 * 1024


def _digest_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def table_counts(database: Path) -> dict[str, int]:
    """Every table in one database, and how many rows it holds.

    Counts and table names only. This is the evidence catalog and history
    continuity are checked against, and it is also the most a sanitized record
    may carry: a row count cannot quote a plan title, and the reason the
    migrated ``service_meta`` may not be quoted anywhere is that it does.
    """
    readable = database.as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(readable, uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CutoverError(f"{database.name} failed its integrity check")
            names = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                name: int(
                    connection.execute(
                        f'SELECT count(*) FROM "{name}"'  # noqa: S608 - from sqlite_master
                    ).fetchone()[0]
                )
                for name in names
            }
    except sqlite3.Error as error:
        raise CutoverError(f"{database.name} could not be read: {error}") from error


def _installed_artifact(prefix: Path) -> str:
    """Which release this prefix is running, or ``""`` if it predates them.

    The empty string is the honest answer rather than a failure, and it is the
    answer the first backup of the cutover gets: the prefix being backed up at
    that point is the legacy Companion's, which was installed before releases
    were recorded at all. A backup that refused to be taken because the thing
    it protects is old would be exactly the wrong refusal.
    """
    path = Path(prefix) / DEPLOYMENT_PATH
    if not path.exists():
        return ""
    return str(document(path).get("artifact") or "")


def capture_backup(
    prefix: Path,
    destination: Path,
    *,
    role: str,
    clock: Any = time.time,
) -> dict[str, Any]:
    """Copy one production prefix into an archive that outlives it.

    Consistency first: a database member goes through
    :func:`~asl_workbench.migration.copy_database`, which uses SQLite's online
    backup API and then verifies the result, so the copy is a moment that
    existed even though the service is still running when it is taken. Every
    other member is bytes.

    The archive is written outside the prefix and refuses to be written inside
    it, because the prefix is what is about to be migrated: a snapshot the
    migration could unwind, remove or overwrite is not the thing an operator
    rolls back to at three in the morning.
    """
    prefix = Path(prefix)
    destination = Path(destination)
    check_prefix_production(prefix)
    if role not in (lifecycle.ROLE_CONNECTOR, lifecycle.ROLE_SERVICE):
        raise CutoverError(f"{role!r} is not a deployed role")
    resolved_prefix = prefix.resolve()
    if (
        resolved_prefix == destination.resolve()
        or resolved_prefix in destination.resolve().parents
    ):
        raise CutoverError(
            f"{destination} is inside the prefix it backs up; a cutover backup "
            "has to survive the migration of that prefix"
        )
    members = backup_members(prefix)
    if not members:
        raise CutoverError(f"{prefix} carries nothing a backup would hold")
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(PRIVATE_DIRECTORY_MODE)
    captured_at = float(clock())
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(captured_at))
    name = f"{stamp}-{role}"
    archive = destination / f"{name}.tar"
    record: dict[str, Any] = {
        "version": BACKUP_SCHEMA_VERSION,
        "role": role,
        "prefix": prefix.name,
        "captured_at": captured_at,
        "artifact": _installed_artifact(prefix),
        "files": {},
        "tables": {},
    }
    with tempfile.TemporaryDirectory(prefix="wb-cutover-") as raw:
        stage = Path(raw) / name
        stage.mkdir(parents=True)
        for member in members:
            source = prefix / member
            target = stage / member
            target.parent.mkdir(parents=True, exist_ok=True)
            if member in migration.DATABASE_MEMBERS:
                migration.copy_database(source, target)
                record["tables"][member] = table_counts(target)
            else:
                atomic(target, source.read_bytes())
            digest, size = _digest_file(target)
            record["files"][member] = {"sha256": digest, "bytes": size}
        atomic(
            stage / BACKUP_MANIFEST,
            json.dumps(record, indent=2, sort_keys=True).encode(),
        )
        with tarfile.open(archive, "w") as bundle:
            for member in (*sorted(record["files"]), BACKUP_MANIFEST):
                bundle.add(stage / member, arcname=f"{name}/{member}")
    archive.chmod(PRIVATE_FILE_MODE)
    digest, size = _digest_file(archive)
    if size > MAX_BACKUP_BYTES:
        raise CutoverError(f"{archive.name} exceeds {MAX_BACKUP_BYTES} bytes")
    receipt = {**record, "archive": archive.name, "sha256": digest, "bytes": size}
    sidecar = destination / f"{name}.json"
    atomic(sidecar, json.dumps(receipt, indent=2, sort_keys=True).encode())
    sidecar.chmod(PRIVATE_FILE_MODE)
    return receipt


def verify_backup(archive: Path) -> dict[str, Any]:
    """Read one backup back and recompute everything that was recorded about it.

    A backup nobody has read is a hope. This opens the archive, checks its own
    digest against the sidecar, checks every member's digest against the
    manifest, and reopens each database member to re-run its integrity check and
    re-count its tables. What it returns is what a rollback would restore.
    """
    archive = Path(archive)
    if archive.suffix != ".tar":
        raise CutoverError(f"{archive.name} is not a cutover backup archive")
    sidecar = archive.with_suffix(".json")
    receipt = document(sidecar)
    if receipt.get("version") != BACKUP_SCHEMA_VERSION:
        raise CutoverError(f"{sidecar.name} is not a cutover backup receipt")
    digest, size = _digest_file(archive)
    if digest != receipt.get("sha256") or size != receipt.get("bytes"):
        raise CutoverError(
            f"{archive.name} is not the archive {sidecar.name} describes"
        )
    name = archive.name[: -len(".tar")]
    with tempfile.TemporaryDirectory(prefix="wb-cutover-verify-") as raw:
        root = Path(raw)
        with tarfile.open(archive) as bundle:
            for member in bundle.getmembers():
                if not member.isfile() or not member.name.startswith(name + "/"):
                    raise CutoverError(
                        f"{archive.name} carries {member.name!r}, which is not a "
                        "member of the prefix it claims to hold"
                    )
            bundle.extractall(root, filter="data")
        unpacked = root / name
        # Read as data rather than through ``document``: the archive's own copy
        # of the manifest is being compared, not trusted, and holding an
        # extracted temporary file to the private-mode rule would refuse a good
        # archive for the mode its extraction happened to give one file.
        stored = json.loads((unpacked / BACKUP_MANIFEST).read_bytes())
        if not isinstance(stored, dict):
            raise CutoverError(f"{archive.name} carries no manifest")
        if any(
            key not in receipt or receipt[key] != value for key, value in stored.items()
        ):
            raise CutoverError(f"{archive.name} disagrees with {sidecar.name}")
        files = receipt.get("files")
        if not isinstance(files, dict) or not files:
            raise CutoverError(f"{sidecar.name} records no members")
        for member, expected in sorted(files.items()):
            path = unpacked / member
            if not path.is_file():
                raise CutoverError(f"{archive.name} is missing {member}")
            digest, size = _digest_file(path)
            if digest != expected.get("sha256") or size != expected.get("bytes"):
                raise CutoverError(
                    f"{member} in {archive.name} is not the recorded file"
                )
        for member, counts in sorted((receipt.get("tables") or {}).items()):
            if table_counts(unpacked / member) != counts:
                raise CutoverError(
                    f"{member} in {archive.name} does not hold the recorded rows"
                )
    return receipt


# ---------------------------------------------------------------------------
# One production deployment, not two


def check_single_owner(
    prefix: Path, *, role: str, site: IntegrationSite = PI_SITE
) -> dict[str, Any]:
    """Refuse a definition that would leave two production processes behind.

    The release writes one launch agent on the Mac and one unit on the Pi, both
    under the Companion's own names, which is what makes the unified deployment
    a replacement rather than a neighbour. That is only true if the definition
    the prefix carries really is the one the release produced for *this*
    prefix: a definition left over from a rehearsal, or copied in with a
    migrated prefix, names a label this host answers to for a directory that is
    not there any more.

    What is returned is the identity the definition actually carries, so a
    caller can record it rather than restate it.
    """
    check_prefix_production(prefix)
    path = Path(prefix) / lifecycle.DEFINITIONS_DIRECTORY
    label, unit = lifecycle.LABEL, lifecycle.UNIT
    if role == lifecycle.ROLE_CONNECTOR:
        definition = path / f"{label}.plist"
        if not definition.is_file():
            raise CutoverError(f"{definition} is not there; nothing is installed")
        parsed = plistlib.loads(definition.read_bytes())
        found = str(parsed.get("Label") or "")
        if found != label:
            raise CutoverError(f"the launch agent names {found!r}, not {label!r}")
        arguments = [str(item) for item in parsed.get("ProgramArguments") or ()]
        # The prefix appears in the arguments as the directory the credential
        # lives under rather than as a word of its own, so this matches the way
        # the release actually writes the agent: the working directory *is* the
        # prefix, and at least one argument has to be inside it.
        if str(parsed.get("WorkingDirectory") or "") != str(prefix):
            raise CutoverError("the launch agent does not run in this prefix")
        if not any(item.startswith(f"{prefix}/") for item in arguments):
            raise CutoverError("the launch agent reads nothing out of this prefix")
        origin = next(
            (
                value
                for flag, value in zip(arguments, arguments[1:], strict=False)
                if flag == "--origin"
            ),
            "",
        )
        if origin != site.public_origin:
            # This is the private URL, in the one place a cutover could lose it
            # without noticing: a connector pointed somewhere else keeps
            # working, against the wrong service.
            raise CutoverError(
                f"the launch agent connects to {origin!r}, not the preserved "
                f"private URL {site.public_origin!r}"
            )
        return {"label": found, "origin": origin, "arguments": arguments}
    definition = path / unit
    if not definition.is_file():
        raise CutoverError(f"{definition} is not there; nothing is installed")
    content = definition.read_text(encoding="utf-8")
    start = next(
        (line for line in content.splitlines() if line.startswith("ExecStart=")), ""
    )
    if not start:
        raise CutoverError(f"{unit} declares no ExecStart")
    # Quoted rather than as a substring: ``asl-companion`` is a prefix of
    # ``asl-companion-old``, so a plain containment test accepts a unit that
    # still starts the installation this one was supposed to replace.
    if lifecycle.quote_unit(prefix) not in start:
        raise CutoverError(f"{unit} does not point at this prefix")
    if str(site.bind_port) in start or "--site" in start:
        raise CutoverError(
            f"{unit} moves its site aside; the production unit serves "
            f"{site.target}, which it takes from the release rather than its "
            "command line"
        )
    return {"unit": unit, "exec_start": start}


# ---------------------------------------------------------------------------
# The journal, and the report it may become


class Journal:
    """Every observation the cutover made, in the order it made them.

    Append-only and persisted after every write, because a cutover spans two
    hosts and an unbounded wait for a person to pick up their phone: a journal
    held in memory would lose the technical half to any interruption, and the
    technical half is the part that cannot simply be redone once the legacy
    installation has been migrated.
    """

    def __init__(self, path: Path, *, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.data: dict[str, Any] = {
            "release": None,
            "hosts": {},
            "checks": {},
            "phone": {"readiness": None, "confirmation": None},
            "notes": [],
        }
        if self.path.exists():
            stored = json.loads(self.path.read_bytes())
            if not isinstance(stored, dict):
                raise CutoverError(f"{self.path.name} is not a JSON object")
            self.data.update(stored)

    # -- writing -----------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic(self.path, json.dumps(self.data, indent=2, sort_keys=True).encode())

    def release(self, revision: str, artifact: str) -> None:
        if not _REVISION.fullmatch(revision):
            raise CutoverError(f"{revision!r} is not a full commit revision")
        if not _DIGEST.fullmatch(artifact):
            raise CutoverError(f"{artifact!r} is not an artifact digest")
        self.data["release"] = {"revision": revision, "artifact": artifact}
        self.save()

    def host(
        self,
        role: str,
        *,
        host: str,
        system: str,
        release: str,
        observed_at: float | None = None,
    ) -> None:
        if role not in HOST_ROLES:
            raise CutoverError(f"{role!r} is not a deployed host role")
        self.data["hosts"][role] = {
            "kind": PROVENANCE_ACTUAL_HOST,
            "host": host,
            "system": system,
            "release": release,
            "observed_at": float(
                observed_at if observed_at is not None else self.clock()
            ),
        }
        self.save()

    def record(
        self,
        name: str,
        *,
        host: str,
        outcome: str = OUTCOME_PASSED,
        observed_at: float | None = None,
        note: str | None = None,
    ) -> None:
        """One check, as it was observed. A second record replaces the first.

        Replacing rather than appending is what makes a cutover step repeatable
        after it failed: the failing record is the one that has to stop the
        report, and it does, right up until the step is actually redone.
        """
        if name not in PRODUCTION_CHECKS:
            raise CutoverError(f"{name!r} is not a check this stage owes")
        if host not in CHECK_HOSTS:
            raise CutoverError(f"{host!r} is not a deployed host")
        self.data["checks"][name] = {
            "outcome": outcome,
            "host": host,
            "observed_at": float(
                observed_at if observed_at is not None else self.clock()
            ),
        }
        if note:
            self.data["notes"].append({"check": name, "note": note})
        self.save()

    def phone_readiness(
        self,
        *,
        requested_at: float,
        granted_at: float | None = None,
        by: str = "owner",
    ) -> None:
        """The request that the operator lock their phone, and their answer.

        Recorded in two steps deliberately: ``granted_at`` is ``None`` until
        the person answers, and no fixture may be started while it is. The plan
        forbids starting an alert from an unanswered readiness request, and a
        field that could only be filled in after the fact is how that is kept.
        """
        if not _NAME.fullmatch(by):
            raise CutoverError(f"{by!r} is not a usable name")
        if granted_at is not None and float(granted_at) < float(requested_at):
            raise CutoverError("readiness was granted before it was asked for")
        self.data["phone"]["readiness"] = {
            "requested_at": float(requested_at),
            "granted_at": None if granted_at is None else float(granted_at),
            "by": by,
        }
        self.save()

    def granted(self) -> float:
        """When the operator declared the phone ready, or a refusal."""
        readiness = (self.data.get("phone") or {}).get("readiness")
        if not isinstance(readiness, dict) or readiness.get("granted_at") is None:
            raise CutoverError(
                "the operator has not declared the phone ready; a fixture "
                "started now would be an alert nobody agreed to receive"
            )
        return float(readiness["granted_at"])

    def phone_confirmation(
        self,
        *,
        requested_at: float,
        answered_at: float | None = None,
        verdict: str = VERDICT_PENDING,
        release: str,
        host: str,
        by: str = "owner",
        checks: Mapping[str, bool] | None = None,
    ) -> None:
        """What the operator was asked about the phone, and what they answered.

        The confirmation may only be asked for after readiness was granted --
        :meth:`granted` is called rather than trusted -- and until an answer
        arrives the verdict is ``pending``, which :meth:`render` refuses. That
        is the whole of "an unanswered request leaves acceptance pending": it is
        not a state this module can be talked out of, because the only way past
        it is a timestamp and four answers that did not exist before.
        """
        granted = self.granted()
        if float(requested_at) < granted:
            raise CutoverError(
                "the confirmation was asked for before the operator declared "
                "the phone ready"
            )
        if verdict not in (VERDICT_ACCEPTED, VERDICT_PENDING):
            raise CutoverError(f"{verdict!r} is not a verdict a person gave")
        if verdict == VERDICT_ACCEPTED and answered_at is None:
            raise CutoverError("an accepted confirmation has a moment it was answered")
        if answered_at is not None and float(answered_at) < float(requested_at):
            raise CutoverError("the confirmation was answered before it was asked")
        if not _DIGEST.fullmatch(release):
            raise CutoverError(f"{release!r} is not an artifact digest")
        self._check_device(host)
        if not _NAME.fullmatch(by):
            raise CutoverError(f"{by!r} is not a usable name")
        answers = dict.fromkeys(PHONE_CHECKS, False)
        for name, value in (checks or {}).items():
            if name not in PHONE_CHECKS:
                raise CutoverError(f"{name!r} is not a check the phone answers")
            answers[name] = bool(value)
        if verdict == VERDICT_ACCEPTED and not all(answers.values()):
            raise CutoverError(
                "an accepted confirmation answers all four phone checks: "
                + ", ".join(name for name, value in answers.items() if not value)
                + " was not confirmed"
            )
        self.data["phone"]["confirmation"] = {
            "requested_at": float(requested_at),
            "answered_at": None if answered_at is None else float(answered_at),
            "verdict": verdict,
            "release": release,
            "kind": PROVENANCE_ACTUAL_HOST,
            "host": host,
            "by": by,
            "checks": answers,
        }
        self.save()

    def _check_device(self, host: str) -> None:
        if not _HOSTNAME.fullmatch(host):
            raise CutoverError(f"{host!r} is not a device name")
        folded = host.lower()
        for marker in FORBIDDEN_HOST_MARKERS:
            if marker in folded:
                raise CutoverError(f"{host!r} names a fixture rather than a phone")
        deployed = {
            str(entry.get("host"))
            for entry in (self.data.get("hosts") or {}).values()
            if isinstance(entry, dict)
        }
        if host in deployed:
            raise CutoverError(
                f"{host!r} is a deployed host; a phone acceptance confirmed from "
                "the Mac or the Pi is not a phone acceptance"
            )

    # -- reading -----------------------------------------------------------

    def missing(self) -> list[str]:
        """Which checks have not been recorded as passed yet."""
        checks = self.data.get("checks") or {}
        return [
            name
            for name in PRODUCTION_CHECKS
            if (checks.get(name) or {}).get("outcome") != OUTCOME_PASSED
        ]

    def render(
        self, *, committed_at: float, recorded_at: float | None = None
    ) -> dict[str, Any]:
        """The report, or a refusal naming what was never observed.

        ``committed_at`` is the timestamp of the commit the evidence accepts.
        Every moment in the document has to sit between it and the report, and
        the check is here rather than only in the acceptance reader because a
        cutover that recorded a step before the release existed recorded a step
        about another release.
        """
        recorded = float(recorded_at if recorded_at is not None else self.clock())
        release = self.data.get("release")
        if not isinstance(release, dict) or set(release) != {"revision", "artifact"}:
            raise CutoverError("the cutover recorded no release")
        hosts = self.data.get("hosts") or {}
        for role in HOST_ROLES:
            if role not in hosts:
                raise CutoverError(f"the cutover recorded no {role} host")
            entry = hosts[role]
            if entry.get("system") != HOST_ROLES[role]:
                raise CutoverError(
                    f"the {role} reported {entry.get('system')!r}, "
                    f"not {HOST_ROLES[role]}"
                )
            if entry.get("release") != release["artifact"]:
                raise CutoverError(
                    f"the {role} ran a release this report does not accept"
                )
            _window(entry.get("observed_at"), committed_at, recorded, f"hosts.{role}")
        if hosts["mac"]["host"] == hosts["pi"]["host"]:
            raise CutoverError(
                "both halves name the same machine; a two-host cutover recorded "
                "from one host is not a two-host cutover"
            )
        missing = self.missing()
        if missing:
            raise CutoverError("the cutover did not record: " + ", ".join(missing))
        checks = self.data["checks"]
        for name in PRODUCTION_CHECKS:
            _window(
                checks[name].get("observed_at"),
                committed_at,
                recorded,
                f"checks.{name}",
            )
        phone = self._rendered_phone(release["artifact"], committed_at, recorded)
        return {
            "schema": SCHEMA_VERSION,
            "stage": STAGE,
            "recorded_at": recorded,
            "release": dict(release),
            "hosts": {role: dict(hosts[role]) for role in HOST_ROLES},
            "checks": {
                name: {
                    "outcome": checks[name]["outcome"],
                    "observed_at": checks[name]["observed_at"],
                    "host": checks[name]["host"],
                }
                for name in PRODUCTION_CHECKS
            },
            "phone": phone,
        }

    def _rendered_phone(
        self, artifact: str, committed_at: float, recorded: float
    ) -> dict[str, Any]:
        """The human half of the report, or the refusal that keeps it pending."""
        phone = self.data.get("phone") or {}
        readiness = phone.get("readiness")
        if not isinstance(readiness, dict):
            raise CutoverError("the operator was never asked to ready the phone")
        if readiness.get("granted_at") is None:
            raise CutoverError(
                "the operator has not declared the phone ready; acceptance stays "
                "pending until they answer"
            )
        requested = _window(
            readiness.get("requested_at"),
            committed_at,
            recorded,
            "phone.readiness.requested_at",
        )
        granted = _window(
            readiness.get("granted_at"),
            committed_at,
            recorded,
            "phone.readiness.granted_at",
        )
        if granted < requested:
            raise CutoverError("readiness was granted before it was asked for")
        confirmation = phone.get("confirmation")
        if not isinstance(confirmation, dict):
            raise CutoverError("the operator was never asked to confirm the phone")
        if confirmation.get("verdict") != VERDICT_ACCEPTED:
            raise CutoverError(
                "the operator has not confirmed the phone; acceptance stays "
                "pending until they answer"
            )
        if confirmation.get("release") != artifact:
            raise CutoverError(
                "the operator confirmed a release this report does not accept"
            )
        asked = _window(
            confirmation.get("requested_at"),
            committed_at,
            recorded,
            "phone.confirmation.requested_at",
        )
        answered = _window(
            confirmation.get("answered_at"),
            committed_at,
            recorded,
            "phone.confirmation.answered_at",
        )
        if asked < granted:
            raise CutoverError(
                "the confirmation was asked for before the phone was declared ready"
            )
        if answered < asked:
            raise CutoverError("the confirmation was answered before it was asked")
        checks = confirmation.get("checks") or {}
        unconfirmed = [name for name in PHONE_CHECKS if checks.get(name) is not True]
        if unconfirmed:
            raise CutoverError(
                "the operator did not confirm: " + ", ".join(unconfirmed)
            )
        return {
            "readiness": {
                "requested_at": requested,
                "granted_at": granted,
                "by": str(readiness.get("by") or "owner"),
            },
            "confirmation": {
                "requested_at": asked,
                "answered_at": answered,
                "verdict": VERDICT_ACCEPTED,
                "release": artifact,
                "kind": PROVENANCE_ACTUAL_HOST,
                "host": str(confirmation.get("host")),
                "by": str(confirmation.get("by") or "owner"),
                "checks": dict.fromkeys(PHONE_CHECKS, True),
            },
        }


def _window(value: Any, floor: float, ceiling: float, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CutoverError(f"{where}: a unix timestamp is required")
    moment = float(value)
    if moment < floor:
        raise CutoverError(f"{where}: predates the release it is evidence for")
    if moment > ceiling + MAX_CLOCK_SKEW_SECONDS:
        raise CutoverError(f"{where}: is later than the report that carries it")
    return moment


# ---------------------------------------------------------------------------
# The half that runs on the Pi
#
# Driven over ssh, one bounded command at a time, so every mutation the cutover
# makes to the other host is a line somebody can read back.


def _installation(prefix: Path) -> lifecycle.Installation:
    check_prefix_production(prefix)
    check_production(PI_SITE)
    return lifecycle.Installation(prefix, site=PI_SITE)


def main(argv: Sequence[str] | None = None) -> int:
    """One cutover operation, named, against the production site."""
    parser = argparse.ArgumentParser(
        prog="workbench_cutover",
        description="Drive one step of the production cutover.",
    )
    parser.add_argument(
        "action",
        choices=(
            "site",
            "backup",
            "verify-backup",
            "counts",
            "owner",
            "install",
            "upgrade",
            "status",
            "migrate",
            "rollback",
            "recover",
            "remove",
        ),
    )
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--python")
    parser.add_argument("--target-id")
    parser.add_argument("--role", default=lifecycle.ROLE_SERVICE)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--application-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args(argv)
    check_production(PI_SITE)
    if args.action == "site":
        print(
            json.dumps(
                {
                    "port": PI_SITE.port,
                    "bind_port": PI_SITE.bind_port,
                    "public_origin": PI_SITE.public_origin,
                    "deployment": PI_SITE.deployment,
                    "label": lifecycle.LABEL,
                    "unit": lifecycle.UNIT,
                    "prefix": PRODUCTION_PREFIX_NAME,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.action == "verify-backup":
        if not args.archive:
            parser.error("--archive is required")
        print(json.dumps(verify_backup(args.archive), sort_keys=True))
        return 0
    if args.action == "counts":
        if not args.database:
            parser.error("--database is required")
        print(json.dumps(table_counts(args.database), sort_keys=True))
        return 0
    if not args.prefix:
        parser.error("--prefix is required")
    if args.action == "backup":
        if not args.destination:
            parser.error("--destination is required")
        print(
            json.dumps(
                capture_backup(args.prefix, args.destination, role=args.role),
                sort_keys=True,
            )
        )
        return 0
    if args.action == "owner":
        print(
            json.dumps(check_single_owner(args.prefix, role=args.role), sort_keys=True)
        )
        return 0
    if args.action == "migrate":
        check_prefix_production(args.prefix)
        plan = migration.Migration(args.prefix, target_id=args.target_id, site=PI_SITE)
        print(json.dumps(plan.plan() if args.dry_run else plan.apply(), sort_keys=True))
        return 0
    installation = _installation(args.prefix)
    if args.action in ("install", "upgrade"):
        if not args.python:
            parser.error("--python is required; a release runs on a named interpreter")
        result = installation.install(
            artifact=args.artifact,
            digest=args.sha256,
            python=args.python,
            activate=args.activate,
            upgrade=args.action == "upgrade",
            dry_run=args.dry_run,
        )
    elif args.action == "status":
        result = installation.status()
    elif args.action == "rollback":
        result = installation.rollback(data=not args.application_only)
    elif args.action == "recover":
        result = installation.recover()
    else:
        result = installation.remove(confirm=args.confirm, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - the cutover's entry point
    raise SystemExit(main())
