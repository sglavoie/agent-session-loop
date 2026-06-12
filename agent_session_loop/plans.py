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
HANDOFF_HEADING = "## Handoff"
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


def _final_handoff_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return index
    return None


def read_session_status(session: SessionFile) -> SessionStatus:
    """Read the status marker from the final handoff block in a session file."""
    try:
        lines = session.path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _raise_read_error(session.path, error)

    handoff_start = _final_handoff_start(lines)
    if handoff_start is None:
        return SessionStatus(
            session=session,
            status="empty",
            raw_value="",
            line_number=None,
        )

    for index in range(handoff_start + 1, len(lines)):
        line = lines[index]
        if line.startswith("## "):
            break
        if line.startswith(STATUS_PREFIX):
            raw_value = line[len(STATUS_PREFIX) :].strip()
            return SessionStatus(
                session=session,
                status=parse_status_value(raw_value),
                raw_value=raw_value,
                line_number=index + 1,
            )

    return SessionStatus(
        session=session,
        status="empty",
        raw_value="",
        line_number=None,
    )


def _split_table_row(line: str) -> tuple[str, list[str]] | None:
    if not line.lstrip().startswith("|"):
        return None
    leading = line[: len(line) - len(line.lstrip())]
    return leading, line.strip().strip("|").split("|")


def _normalize_table_header(value: str) -> str:
    cleaned = value.strip().strip("`").lower()
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned)


def _is_separator_cell(value: str) -> bool:
    return re.fullmatch(r"\s*:?-{3,}:?\s*", value) is not None


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(_is_separator_cell(cell) for cell in cells)


def _session_number_from_cell(value: str) -> int | None:
    text = value.strip().strip("`")
    match = re.fullmatch(r"(?:session[\s-]*|s)?([1-9][0-9]*)", text, re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1))


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _sync_status_table_block(
    block: list[str],
    status_by_session: dict[int, StatusValue],
) -> tuple[list[str], bool]:
    if len(block) < 2:
        return block, False

    header = _split_table_row(block[0])
    separator = _split_table_row(block[1])
    if header is None or separator is None or not _is_separator_row(separator[1]):
        return block, False

    headers = [_normalize_table_header(cell) for cell in header[1]]
    try:
        status_index = headers.index("status")
    except ValueError:
        return block, False

    session_index = next(
        (
            index
            for index, name in enumerate(headers)
            if name in {"#", "session", "session #", "session number"}
        ),
        None,
    )
    if session_index is None:
        return block, False

    changed = False
    synced_rows = 0
    updated = list(block)
    for row_index, row in enumerate(block[2:], start=2):
        parsed = _split_table_row(row)
        if parsed is None:
            continue
        leading, cells = parsed
        if max(session_index, status_index) >= len(cells):
            continue
        session_number = _session_number_from_cell(cells[session_index])
        if session_number is None:
            continue
        status = status_by_session.get(session_number)
        if status not in {"pending", "complete"}:
            continue
        synced_rows += 1
        new_status_cell = _replace_cell_content(cells[status_index], status)
        if cells[status_index] != new_status_cell:
            cells[status_index] = new_status_cell
            updated[row_index] = _join_table_row(leading, cells)
            changed = True

    if synced_rows == 0:
        return block, False
    return updated, changed


def _sync_status_tables_in_text(
    text: str,
    status_by_session: dict[int, StatusValue],
) -> tuple[str, bool]:
    lines = text.splitlines(keepends=False)
    updated: list[str] = []
    changed = False
    index = 0

    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            updated.append(lines[index])
            index += 1
            continue

        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        synced_block, block_changed = _sync_status_table_block(block, status_by_session)
        updated.extend(synced_block)
        changed = changed or block_changed

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(updated) + suffix, changed


def _write_text(path: Path, text: str) -> None:
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as error:
        _raise_read_error(path, error)


def sync_derived_status_ledgers(
    snapshot: PlanSnapshot,
    *,
    sync_manifest: bool = True,
    sync_context: bool = True,
) -> tuple[Path, ...]:
    """Sync recognized derived status tables from authoritative handoff statuses."""
    status_by_session = {
        session.number: session.status for session in snapshot.sessions
    }
    changed_paths: list[Path] = []

    for path, enabled in (
        (snapshot.manifest_path, sync_manifest),
        (snapshot.context_path, sync_context),
    ):
        if not enabled:
            continue
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as error:
            _raise_read_error(path, error)
        updated, changed = _sync_status_tables_in_text(current, status_by_session)
        if changed:
            _write_text(path, updated)
            changed_paths.append(path)

    return tuple(changed_paths)


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
