"""Plan directory inspection helpers.

The plan files are the durable source of truth. This module will parse
`manifest.md`, `context.md`, exact `session-N.md` files, and handoff status
lines without trusting generated runner state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Literal, NoReturn


StatusValue = Literal["pending", "complete", "empty", "unknown"]
STATUS_PREFIX = "- **Status**:"
SESSION_FILE_RE = re.compile(r"^session-([1-9][0-9]*)\.md$")


class PlanError(Exception):
    """Raised when a plan directory cannot be inspected safely."""


@dataclass(frozen=True)
class SessionFile:
    """A numerically identified `session-N.md` file."""

    number: int
    path: Path


@dataclass(frozen=True)
class SessionStatus:
    """Parsed handoff status for one session file."""

    session: SessionFile
    status: StatusValue
    raw_value: str
    line_number: int | None

    @property
    def number(self) -> int:
        return self.session.number

    @property
    def path(self) -> Path:
        return self.session.path


@dataclass(frozen=True)
class PlanSnapshot:
    """Current durable state read from a split session plan directory."""

    plan_dir: Path
    context_path: Path
    manifest_path: Path
    parallel_validation_path: Path | None
    sessions: tuple[SessionStatus, ...]
    content_sha256: str
    path_hash: str
    fingerprint: str

    @property
    def plan_hash(self) -> str:
        return self.fingerprint

    @property
    def next_pending(self) -> SessionStatus | None:
        return next((session for session in self.sessions if session.status == "pending"), None)


def canonicalize_path(path: str | Path) -> Path:
    """Return a filesystem-canonical path without requiring it to exist."""
    expanded = os.path.expanduser(os.fspath(path))
    return Path(os.path.realpath(os.path.abspath(expanded)))


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


def discover_session_files(plan_dir: str | Path) -> tuple[SessionFile, ...]:
    """Find exact `session-N.md` files, excluding expansion notes."""
    canonical_dir = canonicalize_path(plan_dir)
    sessions: list[SessionFile] = []

    try:
        directory_entries = tuple(canonical_dir.iterdir())
    except OSError as error:
        _raise_read_error(canonical_dir, error)

    for path in directory_entries:
        match = SESSION_FILE_RE.match(path.name)
        if match is None:
            continue
        sessions.append(SessionFile(number=int(match.group(1)), path=path))

    return tuple(sorted(sessions, key=lambda session: session.number))


def parse_status_value(raw_value: str) -> StatusValue:
    normalized = raw_value.strip().lower()
    if normalized == "":
        return "empty"
    if normalized in {"pending", "complete"}:
        return normalized
    return "unknown"


def read_session_status(session: SessionFile) -> SessionStatus:
    """Read the first exact handoff status marker from a session file."""
    try:
        lines = session.path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _raise_read_error(session.path, error)

    for line_number, line in enumerate(lines, start=1):
        if line.startswith(STATUS_PREFIX):
            raw_value = line[len(STATUS_PREFIX) :].strip()
            return SessionStatus(
                session=session,
                status=parse_status_value(raw_value),
                raw_value=raw_value,
                line_number=line_number,
            )

    return SessionStatus(session=session, status="empty", raw_value="", line_number=None)


def plan_content_sha256(
    plan_dir: str | Path,
    sessions: tuple[SessionFile, ...] | None = None,
) -> str:
    """Hash the current plan files in deterministic relative-path order."""
    canonical_dir = canonicalize_path(plan_dir)
    selected_sessions = discover_session_files(canonical_dir) if sessions is None else sessions
    relative_names = ["context.md", "manifest.md"]

    if _is_file(canonical_dir / "parallel-validation.md"):
        relative_names.append("parallel-validation.md")

    for session in selected_sessions:
        relative_names.append(session.path.name)
        expansion_name = f"session-{session.number}.expand.md"
        if _is_file(canonical_dir / expansion_name):
            relative_names.append(expansion_name)

    digest = hashlib.sha256()
    for relative_name in relative_names:
        file_path = canonical_dir / relative_name
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        _update_digest_from_file(digest, file_path)
        digest.update(b"\0")
    return digest.hexdigest()


def plan_fingerprint(plan_dir: str | Path, content_hash: str) -> str:
    canonical_dir = canonicalize_path(plan_dir)
    digest = hashlib.sha256()
    digest.update(str(canonical_dir).encode("utf-8", "surrogateescape"))
    digest.update(b"\0")
    digest.update(content_hash.encode("ascii"))
    return digest.hexdigest()


def inspect_plan(plan_dir: str | Path) -> PlanSnapshot:
    """Inspect a split plan directory and return durable session state."""
    canonical_dir = canonicalize_path(plan_dir)
    if not canonical_dir.is_dir():
        raise PlanError(f"plan directory does not exist: {canonical_dir}")

    context_path = canonical_dir / "context.md"
    manifest_path = canonical_dir / "manifest.md"
    if not _is_file(context_path):
        raise PlanError(f"missing context.md in plan directory: {canonical_dir}")
    if not _is_file(manifest_path):
        raise PlanError(f"missing manifest.md in plan directory: {canonical_dir}")

    sessions = discover_session_files(canonical_dir)
    if not sessions:
        raise PlanError(f"no session-N.md files found in plan directory: {canonical_dir}")

    statuses = tuple(read_session_status(session) for session in sessions)
    content_hash = plan_content_sha256(canonical_dir, sessions)

    parallel_validation_path = canonical_dir / "parallel-validation.md"
    return PlanSnapshot(
        plan_dir=canonical_dir,
        context_path=context_path,
        manifest_path=manifest_path,
        parallel_validation_path=(
            parallel_validation_path if _is_file(parallel_validation_path) else None
        ),
        sessions=statuses,
        content_sha256=content_hash,
        path_hash=path_sha256(canonical_dir),
        fingerprint=plan_fingerprint(canonical_dir, content_hash),
    )
