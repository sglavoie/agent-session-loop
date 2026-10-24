"""Platform-aware barriers and atomic writers for durable ASL ledger writes.

Darwin's :func:`os.fsync` does not force a device-cache flush.  Ledger writes
therefore use ``fcntl.F_FULLFSYNC`` on macOS and the ordinary POSIX ``fsync``
barrier elsewhere.

The file writers live here rather than in the runner because the turn
execution boundary writes the same artifacts the run loop does, and both must
land through one atomic replace with one durability rule.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


def fullsync(fd: int) -> None:
    """Flush completed regular-file writes through the device cache."""
    if sys.platform == "darwin":
        command = getattr(fcntl, "F_FULLFSYNC", None)
        if command is None:
            os.fsync(fd)
            return
        try:
            fcntl.fcntl(fd, command)
            return
        except OSError as error:
            unsupported_errors = {
                errno.ENOTSUP,
                getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
            }
            if error.errno not in unsupported_errors:
                raise
    os.fsync(fd)


def write_text(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, creating the parent first.

    A reader -- including one that trusts ``last-message.txt`` for a pause
    signal -- sees the previous file or the complete new one, never a
    truncated or half-written one. Like :func:`write_json_file` without
    ``durable``, this is an atomic replace without the fullsync barriers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_text(path, text)


def append_text(path: Path, text: str) -> None:
    """Append ``text`` to ``path``, creating the parent directory first.

    This is the one writer here that is *not* an atomic replace, and it adds no
    barrier: it carries a harness's live output stream, where a follower reads
    each chunk as it lands. The stream is provisional -- the turn's final
    :func:`write_text` replaces the whole log atomically -- so a crash
    mid-append can at worst leave a torn tail on a log that is about to be
    rewritten, or on the log of a turn that never finished.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


#: The per-component filename byte limit (NAME_MAX) on the filesystems ASL uses.
_NAME_MAX_BYTES = 255


def staging_path(path: Path) -> Path:
    """A per-process, per-thread staging name beside ``path``.

    Two threads writing the same artifact must not share a staging file, or
    one replaces the other's half-written bytes. A target whose name leaves no
    room for the decoration is staged under a digest of its name instead, so a
    name that fits the directory also has a staging name that does.
    """
    suffix = f".{os.getpid()}.{threading.get_ident()}.tmp"
    name = f".{path.name}{suffix}"
    if len(os.fsencode(name)) > _NAME_MAX_BYTES:
        digest = hashlib.sha256(os.fsencode(path.name)).hexdigest()[:16]
        name = f".{digest}{suffix}"
    return path.with_name(name)


def sync_directory(path: Path) -> None:
    """Make directory-entry changes under ``path`` durable."""
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        fullsync(directory_fd)
    finally:
        os.close(directory_fd)


def replace_bytes(
    path: Path,
    payload: bytes,
    *,
    sync_file: bool = False,
    sync_parent: bool = False,
    mode: int = 0o666,
    permissions: int | None = None,
) -> None:
    """Atomically replace ``path`` with ``payload`` through a unique staging file.

    A reader sees the old file or the new one, never a partial write. The
    parent directory must already exist. ``sync_file`` fullsyncs the staged
    bytes before the rename, and ``sync_parent`` fullsyncs the directory after
    it. ``mode`` is the creation mode (the umask applies, as with
    :func:`open`); ``permissions`` is set exactly, before the rename, for a
    file that must never appear with any other mode. On any failure the
    staging file is removed and the exception propagates unchanged.
    """
    temporary_path = staging_path(path)
    try:
        fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(payload)
            if permissions is not None:
                os.fchmod(temporary_file.fileno(), permissions)
            if sync_file:
                temporary_file.flush()
                fullsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        if sync_parent:
            sync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            temporary_path.unlink()


def replace_text(
    path: Path,
    text: str,
    *,
    sync_file: bool = False,
    sync_parent: bool = False,
    mode: int = 0o666,
    permissions: int | None = None,
) -> None:
    """:func:`replace_bytes` for UTF-8 text, written without newline translation."""
    replace_bytes(
        path,
        text.encode("utf-8"),
        sync_file=sync_file,
        sync_parent=sync_parent,
        mode=mode,
        permissions=permissions,
    )


def write_json_file(
    path: Path, payload: dict[str, Any], *, durable: bool = False
) -> None:
    """Atomically replace ``path`` with ``payload`` rendered as sorted JSON.

    ``durable`` adds the file and directory barriers a ledger needs: a reader
    that has seen an event naming this artifact must find the artifact itself,
    which only holds if both the bytes and the rename survived the crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_text(
        path,
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        sync_file=durable,
        sync_parent=durable,
    )


def write_bytes_file(path: Path, payload: bytes) -> None:
    """Atomically replace ``path`` with ``payload``, creating the parent first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_bytes(path, payload)
