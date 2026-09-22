"""The private installation prefix: safe paths, atomic writes, exclusive locks.

Every durable thing the deployed service owns lives under one directory: its
configuration, its credentials, its database, and the receipts that say which
host resources it created. The rules that directory lives under are small, and
they are the same four rules whether the caller is an integration transaction,
a lifecycle operation, or a recovery snapshot -- so they live here once rather
than in each of them.

* **Nothing shared, nothing linked.** :func:`safe_prefix` refuses a prefix that
  is relative, that is the home directory itself, that is shallow enough to be
  somebody else's directory, or that reaches its contents through a symlink at
  any level. :func:`private` refuses a path that any other user can read. A
  credential under a group-readable directory is a credential that has already
  left.
* **A write either happened or did not.** :func:`atomic` writes a sibling
  temporary file, fsyncs it, renames it over the destination, and fsyncs the
  directory. A half-written receipt is worse than no receipt: it describes host
  state that was never reached.
* **One process at a time.** :func:`lock` takes a non-blocking ``flock`` and
  refuses rather than waits. Two integration runs interleaving over the same
  receipts would each read the other's half-applied state as the operator's.
* **A document is an object.** :func:`document` reads JSON with a private mode
  check first and rejects anything that is not a mapping, including the
  non-finite constants ``json`` would otherwise accept and later re-emit as
  text no other parser will read back.

This module deliberately preserves the retired Companion's installation rules:
an existing prefix must keep loading here without being rewritten. They are now
stated once, in the portable package, where both integration transactions and
packaging operations can reach them.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: How deep a prefix must be before it is plausibly dedicated to one service.
#: ``/home/user`` is three parts and is somebody's whole account; the fourth
#: part is the first one that can name an installation rather than a person.
MINIMUM_PREFIX_PARTS = 4

#: The modes a prefix and its contents are allowed to carry. Both are "owner
#: only": the check is that no group or other bit is set at all, rather than
#: that the mode equals one of these.
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

#: The bits that must be clear on anything under the prefix.
SHARED_MODE_BITS = 0o077

#: Where a service's own files sit under the installation prefix. These are
#: paths an *already installed* prefix carries, so they are facts rather than
#: choices: changing one orphans a file a deployed installation is still
#: reading. They live here rather than with either caller because provisioning
#: writes them and packaging reads them, and a layout described twice is a
#: layout that will eventually be described differently.
CONFIG_PATH = "config/runtime.json"
SECRETS_PATH = "credentials/secrets.json"
CONNECTOR_TOKEN_PATH = "credentials/connector-issued.json"
SUMMARY_TOKEN_PATH = "credentials/summary.token"
RECEIPTS_DIRECTORY = "integrations"

#: What the lifecycle owns: the deployment record, the interrupted-transaction
#: marker, and the four directories a content-addressed installation fills.
DEPLOYMENT_PATH = "deployment.json"
TRANSACTION_PATH = "transaction.json"
RELEASES_DIRECTORY = "releases"
DEFINITIONS_DIRECTORY = "definitions"
BACKUPS_DIRECTORY = "backups"
LOGS_DIRECTORY = "logs"

#: What lives in the state directory, by bare name. The runtime is handed a
#: *copy* of the state during an upgrade check, so it addresses these relative
#: to whichever directory it was given rather than to the prefix.
STATE_DIRECTORY = "state"
DATABASE_FILE = "workbench.db"
READY_FILE = "ready.json"
RUNTIME_LOCK_FILE = "runtime.lock"

#: The same three under the prefix, for callers holding a prefix.
DATABASE_PATH = f"{STATE_DIRECTORY}/{DATABASE_FILE}"
READY_PATH = f"{STATE_DIRECTORY}/{READY_FILE}"

#: The locks, each naming what it serializes. They are separate files because
#: they are separate questions: ``lifecycle.lock`` is "one installer", while
#: ``state/runtime.lock`` is "one process touching the state", and an upgrade
#: holds the first while briefly taking the second from the runtime it stopped.
PROVISION_LOCK = "provision.lock"
LIFECYCLE_LOCK = "lifecycle.lock"
RUNTIME_LOCK = f"{STATE_DIRECTORY}/{RUNTIME_LOCK_FILE}"
INTEGRATION_LOCK = "integration.lock"

#: The directories a provisioned prefix carries, and the ones an installed
#: prefix adds on top. Provisioning does not create the second set: a prefix
#: that holds credentials but has never been installed into is a normal state.
PREFIX_DIRECTORIES: tuple[str, ...] = (
    "config",
    "credentials",
    STATE_DIRECTORY,
    RECEIPTS_DIRECTORY,
)
INSTALLED_DIRECTORIES: tuple[str, ...] = (
    *PREFIX_DIRECTORIES,
    RELEASES_DIRECTORY,
    DEFINITIONS_DIRECTORY,
    BACKUPS_DIRECTORY,
    LOGS_DIRECTORY,
)


class InstallationError(ValueError):
    """A prefix, or a path under it, was refused rather than used."""


def private(path: Path, *, directory: bool = False) -> None:
    """Refuse a path that is shared, foreign, linked, or the wrong type.

    The type check is part of the same question rather than a separate one: a
    caller that asked for a private file and was handed a directory is about to
    read something other than what it named.
    """
    if path.is_symlink():
        raise InstallationError("symlink refused")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & SHARED_MODE_BITS:
        raise InstallationError("path must be private and owned by the service user")
    if directory != path.is_dir():
        raise InstallationError("unexpected path type")


def document(path: Path) -> dict[str, Any]:
    """Read one private JSON object, refusing anything else."""
    private(path)

    def refuse_constant(_: str) -> Any:
        raise InstallationError("nonfinite JSON")

    value = json.loads(path.read_bytes(), parse_constant=refuse_constant)
    if not isinstance(value, dict):
        raise InstallationError("expected JSON object")
    return value


def atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` durably, or leave it exactly as it was."""
    if path.is_symlink():
        raise InstallationError("symlink destination refused")
    descriptor, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    """Write one JSON document atomically, with sorted keys so it diffs."""
    atomic(path, json.dumps(value, sort_keys=True).encode("utf-8") + b"\n")


@contextmanager
def lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path``, or refuse immediately.

    Non-blocking on purpose. An operator running an integration while the
    service is starting wants to be told so, not to be left watching a command
    that has silently queued behind another one.
    """
    descriptor = os.open(
        path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, PRIVATE_FILE_MODE
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallationError("another process owns this installation") from error
        yield
    finally:
        os.close(descriptor)


def safe_prefix(prefix: Path) -> None:
    """Refuse a prefix that is not a private dedicated installation directory.

    When the prefix already exists every path under it is checked too, and the
    walk tolerates exactly one thing: a file that disappears while it runs. An
    atomic replacement finishing mid-walk is a normal event, and treating it as
    a violation would make a status command fail for the reason that the
    service is healthy.
    """
    if (
        not prefix.is_absolute()
        or prefix == Path.home()
        or len(prefix.parts) < MINIMUM_PREFIX_PARTS
        or any(ord(character) < 32 for character in str(prefix))
    ):
        raise InstallationError("use an absolute dedicated installation prefix")
    for part in (prefix, *prefix.parents):
        if part.is_symlink():
            raise InstallationError("symlink prefix refused")
    if not prefix.exists():
        return
    private(prefix, directory=True)
    for path in prefix.rglob("*"):
        if path.is_symlink():
            raise InstallationError("symlink in installation refused")
        try:
            private(path, directory=path.is_dir())
        except FileNotFoundError:
            continue


__all__ = [
    "BACKUPS_DIRECTORY",
    "CONFIG_PATH",
    "CONNECTOR_TOKEN_PATH",
    "DATABASE_FILE",
    "DATABASE_PATH",
    "DEFINITIONS_DIRECTORY",
    "DEPLOYMENT_PATH",
    "INSTALLED_DIRECTORIES",
    "INTEGRATION_LOCK",
    "LIFECYCLE_LOCK",
    "LOGS_DIRECTORY",
    "MINIMUM_PREFIX_PARTS",
    "PREFIX_DIRECTORIES",
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "PROVISION_LOCK",
    "READY_FILE",
    "READY_PATH",
    "RECEIPTS_DIRECTORY",
    "RELEASES_DIRECTORY",
    "RUNTIME_LOCK",
    "RUNTIME_LOCK_FILE",
    "SECRETS_PATH",
    "SHARED_MODE_BITS",
    "STATE_DIRECTORY",
    "SUMMARY_TOKEN_PATH",
    "TRANSACTION_PATH",
    "InstallationError",
    "atomic",
    "document",
    "lock",
    "private",
    "safe_prefix",
    "write_json",
]
