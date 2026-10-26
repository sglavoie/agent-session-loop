# ruff: noqa: F403, F405
from .migration_schema import *
from .migration_schema import _ARTIFACT_SEGMENT, _SNAPSHOT_NAME


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
    try:
        for name in names:
            source = prefix / name
            private(source)
            target = stage / name
            target.parent.mkdir(
                parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE
            )
            if name in DATABASE_MEMBERS:
                copy_database(source, target)
            else:
                atomic(target, source.read_bytes())
            files[name] = digest_of(target)
        write_json(
            stage / BACKUP_MANIFEST, {"version": BACKUP_SCHEMA_VERSION, "files": files}
        )
        os.replace(stage, destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
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
