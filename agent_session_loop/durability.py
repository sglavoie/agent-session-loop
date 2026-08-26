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
    """Write ``text`` to ``path``, creating the parent directory first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def append_text(path: Path, text: str) -> None:
    """Append ``text`` to ``path``, creating the parent directory first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def _temporary_path(path: Path) -> Path:
    """A per-process, per-thread staging name beside ``path``.

    Two threads writing the same artifact must not share a staging file, or
    one replaces the other's half-written bytes.
    """
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def write_json_file(
    path: Path, payload: dict[str, Any], *, durable: bool = False
) -> None:
    """Atomically replace ``path`` with ``payload`` rendered as sorted JSON.

    ``durable`` adds the file and directory barriers a ledger needs: a reader
    that has seen an event naming this artifact must find the artifact itself,
    which only holds if both the bytes and the rename survived the crash.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(path)
    try:
        with temporary_path.open("w", encoding="utf-8") as temporary_file:
            temporary_file.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
            if durable:
                temporary_file.flush()
                fullsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        if durable:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                fullsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def write_bytes_file(path: Path, payload: bytes) -> None:
    """Atomically replace ``path`` with ``payload``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(path)
    temporary_path.write_bytes(payload)
    os.replace(temporary_path, path)
