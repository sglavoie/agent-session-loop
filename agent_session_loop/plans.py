"""Plan directory inspection helpers.

The plan files are the durable source of truth. This module will parse
`manifest.md`, `context.md`, exact `session-<id>.md` files, and handoff
status lines without trusting generated runner state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Literal, Mapping, NoReturn


StatusValue = Literal["pending", "complete", "empty", "unknown"]
STATUS_PREFIX = "- **Status**:"
HANDOFF_HEADING = "## Handoff"
SESSION_ID_PATTERN = r"[1-9][0-9]*(?:\.[1-9][0-9]*)*"
SESSION_ID_RE = re.compile(rf"^{SESSION_ID_PATTERN}$")
SESSION_FILE_RE = re.compile(rf"^session-({SESSION_ID_PATTERN})\.md$")
SESSION_EXPANSION_FILE_RE = re.compile(
    rf"^session-({SESSION_ID_PATTERN})\.expand\.md$"
)
LOOSE_SESSION_FILE_RE = re.compile(r"^session-([0-9]+(?:\.[0-9]+)*)\.md$")
SESSION_STATUS_TABLE_HEADERS = {
    "#",
    "session",
    "session #",
    "session number",
    "session id",
    "id",
}


class PlanError(Exception):
    """Raised when a plan directory cannot be inspected safely."""


@dataclass(frozen=True, order=True)
class SessionId:
    """Canonical dotted session id sorted by numeric components."""

    components: tuple[int, ...]

    def __post_init__(self) -> None:
        components = tuple(self.components)
        if not components:
            raise ValueError("session id must contain at least one component")
        if any(type(component) is not int or component <= 0 for component in components):
            raise ValueError("session id components must be positive integers")
        object.__setattr__(self, "components", components)

    @classmethod
    def parse(cls, value: int | str | SessionId) -> SessionId:
        if isinstance(value, SessionId):
            return value
        if type(value) is int:
            return cls((value,))
        if not isinstance(value, str):
            raise ValueError(f"invalid session id: {value!r}")

        text = value.strip()
        if SESSION_ID_RE.fullmatch(text) is None:
            raise ValueError(f"invalid session id: {value!r}")
        return cls(tuple(int(component) for component in text.split(".")))

    @property
    def value(self) -> str:
        return ".".join(str(component) for component in self.components)

    @property
    def is_integer(self) -> bool:
        return len(self.components) == 1

    @property
    def sort_key(self) -> tuple[int, ...]:
        return self.components

    @property
    def filename(self) -> str:
        return f"session-{self.value}.md"

    @property
    def expansion_filename(self) -> str:
        return f"session-{self.value}.expand.md"

    @property
    def display_label(self) -> str:
        return f"Session {self.value}"

    def as_int(self) -> int:
        if not self.is_integer:
            raise ValueError(f"nested session id has no integer alias: {self.value}")
        return self.components[0]

    def __str__(self) -> str:
        return self.value


def parse_session_id(value: int | str | SessionId) -> SessionId:
    """Parse and validate a canonical session id."""
    return SessionId.parse(value)


def is_valid_session_id(value: str) -> bool:
    return SESSION_ID_RE.fullmatch(value.strip()) is not None


def session_id_to_filename(value: int | str | SessionId) -> str:
    return parse_session_id(value).filename


def session_id_to_expansion_filename(value: int | str | SessionId) -> str:
    return parse_session_id(value).expansion_filename


def session_id_display_label(value: int | str | SessionId) -> str:
    return parse_session_id(value).display_label


def session_id_from_filename(filename: str) -> SessionId | None:
    match = SESSION_FILE_RE.fullmatch(filename)
    if match is None:
        return None
    return parse_session_id(match.group(1))


def session_id_from_expansion_filename(filename: str) -> SessionId | None:
    match = SESSION_EXPANSION_FILE_RE.fullmatch(filename)
    if match is None:
        return None
    return parse_session_id(match.group(1))


def _canonical_session_id_for_loose_filename(filename: str) -> SessionId | None:
    match = LOOSE_SESSION_FILE_RE.fullmatch(filename)
    if match is None:
        return None

    raw_id = match.group(1)
    try:
        components = tuple(int(component) for component in raw_id.split("."))
    except ValueError:
        return None
    if any(component <= 0 for component in components):
        return None

    canonical_id = SessionId(components)
    if canonical_id.value == raw_id:
        return None
    return canonical_id


@dataclass(frozen=True)
class SessionFile:
    """A canonical `session-<id>.md` file."""

    session_id: SessionId
    path: Path

    @property
    def id(self) -> str:
        return self.session_id.value

    @property
    def number(self) -> int:
        return self.session_id.as_int()

    @property
    def filename(self) -> str:
        return self.session_id.filename

    @property
    def expansion_filename(self) -> str:
        return self.session_id.expansion_filename

    @property
    def display_label(self) -> str:
        return self.session_id.display_label


@dataclass(frozen=True)
class SessionStatus:
    """Parsed handoff status for one session file."""

    session: SessionFile
    status: StatusValue
    raw_value: str
    line_number: int | None

    @property
    def session_id(self) -> SessionId:
        return self.session.session_id

    @property
    def id(self) -> str:
        return self.session.id

    @property
    def number(self) -> int:
        return self.session.number

    @property
    def path(self) -> Path:
        return self.session.path

    @property
    def display_label(self) -> str:
        return self.session.display_label


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

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(session.id for session in self.sessions)

    @property
    def status_by_id(self) -> dict[str, StatusValue]:
        return {session.id: session.status for session in self.sessions}

    @property
    def status_by_number(self) -> dict[int, StatusValue]:
        return {
            session.number: session.status
            for session in self.sessions
            if session.session_id.is_integer
        }


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
    """Find exact `session-<id>.md` files, excluding expansion notes."""
    canonical_dir = canonicalize_path(plan_dir)
    sessions: list[SessionFile] = []

    try:
        directory_entries = tuple(canonical_dir.iterdir())
    except OSError as error:
        _raise_read_error(canonical_dir, error)

    for path in directory_entries:
        session_id = session_id_from_filename(path.name)
        if session_id is None:
            continue
        sessions.append(SessionFile(session_id=session_id, path=path))

    return tuple(sorted(sessions, key=lambda session: session.session_id.sort_key))


def noncanonical_session_file_suggestions(
    plan_dir: str | Path,
) -> tuple[tuple[str, str], ...]:
    """Return leading-zero session filenames and their canonical replacements."""
    canonical_dir = canonicalize_path(plan_dir)
    suggestions: list[tuple[SessionId, str, str]] = []

    try:
        directory_entries = tuple(canonical_dir.iterdir())
    except OSError as error:
        _raise_read_error(canonical_dir, error)

    for path in directory_entries:
        session_id = _canonical_session_id_for_loose_filename(path.name)
        if session_id is None:
            continue
        suggestions.append((session_id, path.name, session_id.filename))

    return tuple(
        (source, target)
        for _session_id, source, target in sorted(
            suggestions, key=lambda item: (item[0].sort_key, item[1])
        )
    )


def _format_noncanonical_session_file_error(
    canonical_dir: Path,
    suggestions: tuple[tuple[str, str], ...],
) -> str:
    rendered = ", ".join(
        f"{source} -> {target}" for source, target in suggestions[:5]
    )
    remaining = len(suggestions) - 5
    if remaining > 0:
        rendered += f", and {remaining} more"
    return (
        f"no canonical session-<id>.md files found in plan directory: {canonical_dir}; "
        "leading zeroes are invalid in session IDs. Rename non-canonical session "
        f"files: {rendered}"
    )


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


def _session_id_from_cell(value: str) -> SessionId | None:
    text = value.strip().strip("`")
    match = re.fullmatch(
        rf"(?:session[\s-]*|s)?({SESSION_ID_PATTERN})",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return parse_session_id(match.group(1))


def _session_number_from_cell(value: str) -> int | None:
    session_id = _session_id_from_cell(value)
    if session_id is None or not session_id.is_integer:
        return None
    return session_id.as_int()


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _status_for_session_id(
    status_by_session: Mapping[object, StatusValue],
    session_id: SessionId,
) -> StatusValue | None:
    status = status_by_session.get(session_id.value)
    if status is not None:
        return status

    status = status_by_session.get(session_id)
    if status is not None:
        return status

    if session_id.is_integer:
        return status_by_session.get(session_id.as_int())
    return None


def _sync_status_table_block(
    block: list[str],
    status_by_session: Mapping[object, StatusValue],
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
            if name in SESSION_STATUS_TABLE_HEADERS
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
        session_id = _session_id_from_cell(cells[session_index])
        if session_id is None:
            continue
        status = _status_for_session_id(status_by_session, session_id)
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
    status_by_session: Mapping[object, StatusValue],
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
    status_by_session = snapshot.status_by_id
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
    selected_sessions = tuple(
        sorted(selected_sessions, key=lambda session: session.session_id.sort_key)
    )
    relative_names = ["context.md", "manifest.md"]

    if _is_file(canonical_dir / "parallel-validation.md"):
        relative_names.append("parallel-validation.md")

    for session in selected_sessions:
        relative_names.append(session.filename)
        expansion_name = session.expansion_filename
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
        noncanonical_suggestions = noncanonical_session_file_suggestions(canonical_dir)
        if noncanonical_suggestions:
            raise PlanError(
                _format_noncanonical_session_file_error(
                    canonical_dir, noncanonical_suggestions
                )
            )
        raise PlanError(
            f"no session-<id>.md files found in plan directory: {canonical_dir}"
        )

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
