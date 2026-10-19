"""Lifecycle state."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from . import lifecycle_common as common
from . import lifecycle_definitions as definitions
from .installation import (
    DATABASE_FILE,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    READY_FILE,
    RUNTIME_LOCK_FILE,
    atomic,
)

# ---------------------------------------------------------------------------
# State


def snapshot(state: Path, target: Path) -> None:
    """Copy one state directory, taking the database through SQLite itself.

    A file copy of an open database copies a moment that never existed. The
    online backup API copies a consistent one, and the integrity check is what
    turns "a file appeared" into "a database a release could open". The lock,
    the readiness note and the write-ahead files are skipped: they describe the
    process that was running, not the observations it recorded.
    """
    target.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    for source in state.iterdir():
        if source.name in (RUNTIME_LOCK_FILE, READY_FILE) or source.name.endswith(
            ("-wal", "-shm")
        ):
            continue
        if source.is_symlink() or not source.is_file():
            raise common.LifecycleError("unexpected mutable state entry")
        if source.name == DATABASE_FILE:
            atomic(target / source.name, b"")
            with (
                closing(
                    sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                ) as original,
                closing(sqlite3.connect(target / source.name)) as copy,
            ):
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise common.LifecycleError("snapshot integrity check failed")
            (target / source.name).chmod(PRIVATE_FILE_MODE)
        else:
            atomic(target / source.name, source.read_bytes())


def release_check(prefix: Path, release: Path, python: str, state: Path) -> None:
    """Ask one release, in a child process, whether it can read one state."""
    common.command(
        [
            *definitions.service_arguments(prefix, python),
            "--state",
            str(state),
            "--check",
        ],
        cwd=release,
        env=common.service_environment(common.ROLE_SERVICE),
    )
