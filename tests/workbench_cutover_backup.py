"""Focused portion of tests/workbench_cutover.py."""

from .workbench_cutover_contract import *

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


__all__ = ["argparse", "hashlib", "json", "plistlib", "re", "sqlite3", "tarfile", "tempfile", "time", "Mapping", "Sequence", "closing", "fields", "Path", "Any", "lifecycle", "migration", "BACKUPS_DIRECTORY", "DEPLOYMENT_PATH", "PRIVATE_DIRECTORY_MODE", "PRIVATE_FILE_MODE", "RELEASES_DIRECTORY", "atomic", "document", "PI_SITE", "IntegrationSite", "DEPLOYMENT_PRODUCTION", "PRODUCTION_PREFIX_NAME", "OWNED_SITE_FIELDS", "PRODUCTION_CHECKS", "PHONE_CHECKS", "HOST_ROLES", "CHECK_HOSTS", "SCHEMA_VERSION", "STAGE", "OUTCOME_PASSED", "VERDICT_ACCEPTED", "VERDICT_PENDING", "PROVENANCE_ACTUAL_HOST", "MAX_CLOCK_SKEW_SECONDS", "FORBIDDEN_HOST_MARKERS", "_REVISION", "_DIGEST", "_HOSTNAME", "_NAME", "CutoverError", "check_production", "check_prefix_production", "BACKUP_EXCLUDED", "backup_members", "BACKUP_SCHEMA_VERSION", "BACKUP_MANIFEST", "MAX_BACKUP_BYTES", "_digest_file", "table_counts", "_installed_artifact", "capture_backup", "verify_backup"]  # fmt: skip
