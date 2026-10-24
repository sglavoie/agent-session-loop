"""Low-level file, path, and hashing helpers for plan directories."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import NoReturn

from . import durability
from .plans_errors import (
    PlanError,
)

PLAN_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")


def canonicalize_path(path: str | os.PathLike[str]) -> Path:
    """Return a filesystem-canonical path without requiring it to exist."""
    expanded = os.path.expanduser(os.fspath(path))
    return Path(os.path.realpath(os.path.abspath(expanded)))


def plan_title_from_markdown(content: str) -> str | None:
    """Return the first ``# H1`` title in Markdown, if present."""
    for line in content.splitlines():
        match = PLAN_TITLE_RE.match(line)
        if match:
            return match.group(1)
    return None


def _fsync_directory(path: Path) -> None:
    """Make directory-entry changes durable before a transaction advances."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        durability.fullsync(descriptor)
    finally:
        os.close(descriptor)


def path_sha256(path: str | Path) -> str:
    canonical_path = canonicalize_path(path)
    digest = hashlib.sha256()
    digest.update(str(canonical_path).encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    _update_digest_from_file(digest, path)
    return digest.hexdigest()


def _raise_read_error(path: Path, error: OSError) -> NoReturn:
    raise PlanError(f"cannot read {path}: {error}") from error


def _update_digest_from_file(digest, path: Path) -> None:
    try:
        with path.open("rb") as plan_file:
            for chunk in iter(lambda: plan_file.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        _raise_read_error(path, error)


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError as error:
        _raise_read_error(path, error)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via temp-file + `os.replace` so the destination is never
    half-written."""
    try:
        durability.replace_text(path, text)
    except OSError as error:
        _raise_read_error(path, error)
