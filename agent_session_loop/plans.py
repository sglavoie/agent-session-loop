"""Plan directory inspection helpers.

The plan files are the durable source of truth. This module will parse
`manifest.md`, `context.md`, exact `session-<id>.md` files, and handoff
status lines without trusting generated runner state.

`progress.md` is part of the durable plan-directory surface too: a
runner-managed, append-only journal of completed sessions seeded by
`session-split` and appended after each verified-complete session by the
runner. It is excluded from `plan_content_sha256` and
`stable_readiness_sha256` so journal writes never invalidate readiness or
the plan identity. Reads go through `read_progress_entries`; writes go
through the atomic `append_progress_entry` and `write_progress_journal`
helpers.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, NoReturn

from . import durability
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    validate_repository_alias,
)

StatusValue = Literal["pending", "complete", "empty", "unknown"]
ReadinessState = Literal["draft", "reviewed", "loop-ready", "running", "complete"]
READINESS_STATES: tuple[ReadinessState, ...] = (
    "draft",
    "reviewed",
    "loop-ready",
    "running",
    "complete",
)
STATUS_PREFIX = "- **Status**:"
HANDOFF_FILES_PREFIX = "- **Files modified**:"
HANDOFF_DECISIONS_PREFIX = "- **Key decisions**:"
HANDOFF_BLOCKERS_PREFIX = "- **Blockers for next session**:"
HANDOFF_NOTES_PREFIX = "- **Notes**:"
HANDOFF_HEADING = "## Handoff"
READS_REPOSITORIES_PREFIX = "**Reads repositories**:"
WRITES_REPOSITORIES_PREFIX = "**Writes repositories**:"
DISCOVERED_FACTS_HEADING = "### Discovered Facts"
OPEN_QUESTIONS_HEADING = "### Open Questions"
# The generated final-review session owns these artifacts in the plan directory,
# not in a repository under review.  Keep the marker recognition local to the
# plan parser so a completed review can be inspected before the runner imports
# its higher-level review orchestration.
_GENERATED_REVIEW_SESSION_MARKER = "<!-- asl:generated-review-session -->"
_GENERATED_REVIEW_SESSION_VERSION_MARKER_RE = re.compile(
    r"<!--\s*asl:generated-review-session\s+version=[0-9]+\s*-->"
)
_GENERATED_REVIEW_PLAN_ARTIFACTS = frozenset({"final-review.md", "final-review.json"})
PROGRESS_FILE_NAME = "progress.md"
PROGRESS_SCHEMA_VERSION = 1
PLAN_METADATA_FILE_NAME = "plan-metadata.json"
PLAN_METADATA_SCHEMA_VERSION = 1
PLAN_METADATA_V2_SCHEMA_VERSION = 2
PLAN_METADATA_V3_SCHEMA_VERSION = 3
PLAN_TRANSACTION_DIR_NAME = ".asl-plan-transaction"
PLAN_TRANSACTION_STAGING_DIR_NAME = ".asl-plan-transaction-staging"
PLAN_TRANSACTION_CLEANUP_DIR_NAME = ".asl-plan-transaction-cleanup"
PLAN_TRANSACTION_JOURNAL_NAME = "journal.json"
PLAN_TRANSACTION_LOCK_NAME = "transaction.lock"
PLAN_TRANSACTION_SCHEMA_VERSION = 1
# Transaction publication is normally a short rename-and-replace sequence. A
# holder that exceeds this bound is treated as stalled so read-side recovery
# fails diagnostically instead of hanging status and Workbench consumers.
PLAN_TRANSACTION_LOCK_WAIT_SECONDS = 5.0
PLAN_TRANSACTION_LOCK_POLL_SECONDS = 0.01
PROGRESS_SCHEMA_COMMENT = "<!-- agent-session-loop progress-journal v1 -->"
PROGRESS_HEADER = (
    "# Progress Journal\n"
    f"{PROGRESS_SCHEMA_COMMENT}\n"
    "\n"
    "Auto-maintained by agent_session_loop. Append-only. Treat as the running\n"
    "summary of completed sessions; defer to session files for full detail.\n"
)
UNREADABLE_ARTIFACT_DIGEST = "unreadable"
READINESS_PREFIX = "**Readiness**:"
READINESS_SHA_PREFIX = "**Readiness SHA-256**:"
READINESS_CHECKED_PREFIX = "**Readiness checked at**:"
SESSION_ID_PATTERN = r"[1-9][0-9]*(?:\.[1-9][0-9]*)*"
SESSION_ID_RE = re.compile(rf"^{SESSION_ID_PATTERN}$")
SESSION_FILE_RE = re.compile(rf"^session-({SESSION_ID_PATTERN})\.md$")
SESSION_EXPANSION_FILE_RE = re.compile(rf"^session-({SESSION_ID_PATTERN})\.expand\.md$")
LOOSE_SESSION_FILE_RE = re.compile(r"^session-([0-9]+(?:\.[0-9]+)*)\.md$")
PLAN_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")
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


class SessionScopeError(PlanError):
    """A line-specific invalid multi-repository session-scope diagnostic."""

    def __init__(
        self,
        reason: str,
        *,
        line_number: int,
        source_line: str,
        session_id: str | None = None,
        path: Path | None = None,
    ) -> None:
        self.reason = reason
        self.line_number = line_number
        self.source_line = source_line
        self.session_id = session_id
        self.path = path
        super().__init__(f"line {line_number}: {reason}; source line: {source_line!r}")


@dataclass(frozen=True)
class SessionScope:
    """Exact advisory read scope and authoritative write scope for one session."""

    reads: tuple[str, ...]
    writes: tuple[str, ...]

    def __post_init__(self) -> None:
        reads = tuple(self.reads)
        writes = tuple(self.writes)
        if not reads:
            raise ValueError("Reads repositories must not be empty")
        for field, aliases in (
            ("Reads repositories", reads),
            ("Writes repositories", writes),
        ):
            if len(set(aliases)) != len(aliases):
                raise ValueError(f"{field} must not contain duplicate aliases")
            for alias in aliases:
                try:
                    validate_repository_alias(alias)
                except WorkspaceError as error:
                    raise ValueError(
                        f"{field} contains invalid alias {alias!r}: {error}"
                    ) from error
        outside_reads = tuple(alias for alias in writes if alias not in reads)
        if outside_reads:
            raise ValueError(
                "Writes repositories must be a subset of Reads repositories: "
                + ", ".join(outside_reads)
            )
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "writes", writes)

    def validate_registry(self, aliases: Iterable[str]) -> SessionScope:
        """Return this scope after validating exact aliases against a registry."""
        known = frozenset(aliases)
        unknown = tuple(
            alias for alias in (*self.reads, *self.writes) if alias not in known
        )
        if unknown:
            raise ValueError(
                "session scope contains unknown repository aliases: "
                + ", ".join(dict.fromkeys(unknown))
            )
        return self


@dataclass(frozen=True)
class QualifiedPath:
    """A canonical workspace path qualified by an exact repository alias."""

    alias: str
    path: str

    def __post_init__(self) -> None:
        try:
            validate_repository_alias(self.alias)
        except WorkspaceError as error:
            raise ValueError(
                f"invalid qualified-path alias {self.alias!r}: {error}"
            ) from error
        value = self.path
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(
                "qualified path must contain a non-empty repository-relative path"
            )
        if "\\" in value or PureWindowsPath(value).is_absolute():
            raise ValueError("qualified path must use relative POSIX syntax")
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("qualified path must not be absolute or traverse upward")
        normalized = pure.as_posix()
        if normalized in {"", "."} or normalized != value:
            raise ValueError(
                "qualified path must use canonical repository-relative syntax"
            )

    def __str__(self) -> str:
        return f"{self.alias}:{self.path}"


@dataclass(frozen=True)
class PlanMetadata:
    """Stable machine-readable workspace association for one plan."""

    cwd: str
    version: int = PLAN_METADATA_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "cwd": self.cwd}


@dataclass(frozen=True)
class WorkspacePlanMetadata:
    """Durable binding for a registered workspace."""

    workspace_root: str
    registry_digest: str
    version: int = PLAN_METADATA_V3_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "workspace_root": self.workspace_root,
            "registry_digest": self.registry_digest,
        }


PlanMetadataV2 = WorkspacePlanMetadata
PlanMetadataValue = PlanMetadata | WorkspacePlanMetadata


@dataclass(frozen=True, order=True)
class SessionId:
    """Canonical dotted session id sorted by numeric components."""

    components: tuple[int, ...]

    def __post_init__(self) -> None:
        components = tuple(self.components)
        if not components:
            raise ValueError("session id must contain at least one component")
        if any(
            type(component) is not int or component <= 0 for component in components
        ):
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


def _scope_error(
    reason: str,
    *,
    lines: Sequence[str],
    index: int | None,
    start_line: int,
) -> SessionScopeError:
    source_index = 0 if index is None else index
    source_line = lines[source_index] if lines and source_index < len(lines) else ""
    return SessionScopeError(
        reason,
        line_number=start_line + source_index,
        source_line=source_line,
    )


def _parse_scope_alias_list(
    value: str,
    *,
    field: str,
    allow_none: bool,
    lines: Sequence[str],
    index: int,
    start_line: int,
) -> tuple[str, ...]:
    if not value:
        raise _scope_error(
            f"{field} must not be empty",
            lines=lines,
            index=index,
            start_line=start_line,
        )
    parts = tuple(part.strip() for part in value.split(","))
    if any(not part for part in parts):
        raise _scope_error(
            f"{field} contains an empty repository alias",
            lines=lines,
            index=index,
            start_line=start_line,
        )
    if "(none)" in parts:
        if not allow_none or parts != ("(none)",):
            raise _scope_error(
                f"{field} uses malformed (none); it is allowed only by itself for "
                f"writes",
                lines=lines,
                index=index,
                start_line=start_line,
            )
        return ()
    return parts


def _structural_session_scope_indices(lines: Sequence[str]) -> tuple[int, ...]:
    indices: list[int] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match is not None:
            delimiter = match.group(1)[0]
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            continue
        if fence is None and line.startswith(
            (READS_REPOSITORIES_PREFIX, WRITES_REPOSITORIES_PREFIX)
        ):
            indices.append(index)
    return tuple(indices)


def strip_session_scope_lines(lines: Sequence[str]) -> tuple[str, ...]:
    """Remove structural scope fields while preserving fenced examples."""
    scope_indices = frozenset(_structural_session_scope_indices(lines))
    return tuple(line for index, line in enumerate(lines) if index not in scope_indices)


def parse_session_scope_lines(
    lines: Sequence[str],
    registry_aliases: Iterable[str],
    *,
    start_line: int = 1,
) -> SessionScope:
    """Parse and validate the exact scope fields from source or rendered lines."""
    aliases = tuple(registry_aliases)
    structural_indices = _structural_session_scope_indices(lines)
    reads_matches = tuple(
        index
        for index in structural_indices
        if lines[index].startswith(READS_REPOSITORIES_PREFIX)
    )
    writes_matches = tuple(
        index
        for index in structural_indices
        if lines[index].startswith(WRITES_REPOSITORIES_PREFIX)
    )
    for field, matches in (
        ("Reads repositories", reads_matches),
        ("Writes repositories", writes_matches),
    ):
        if not matches:
            raise _scope_error(
                f"missing exact {field!r} field",
                lines=lines,
                index=None,
                start_line=start_line,
            )
        if len(matches) != 1:
            raise _scope_error(
                f"duplicate exact {field!r} fields",
                lines=lines,
                index=matches[1],
                start_line=start_line,
            )

    reads_index = reads_matches[0]
    writes_index = writes_matches[0]
    reads = _parse_scope_alias_list(
        lines[reads_index][len(READS_REPOSITORIES_PREFIX) :].strip(),
        field="Reads repositories",
        allow_none=False,
        lines=lines,
        index=reads_index,
        start_line=start_line,
    )
    writes = _parse_scope_alias_list(
        lines[writes_index][len(WRITES_REPOSITORIES_PREFIX) :].strip(),
        field="Writes repositories",
        allow_none=True,
        lines=lines,
        index=writes_index,
        start_line=start_line,
    )
    try:
        return SessionScope(reads=reads, writes=writes).validate_registry(aliases)
    except ValueError as error:
        error_index = writes_index if str(error).startswith("Writes") else reads_index
        raise _scope_error(
            str(error),
            lines=lines,
            index=error_index,
            start_line=start_line,
        ) from error


def parse_session_scope(
    text: str,
    registry_aliases: Iterable[str],
) -> SessionScope:
    """Parse session scope metadata from a complete Markdown document."""
    return parse_session_scope_lines(text.splitlines(), registry_aliases)


def render_session_scope(scope: SessionScope) -> str:
    """Render scope metadata in its canonical two-line form."""
    writes = ", ".join(scope.writes) if scope.writes else "(none)"
    return (
        f"{READS_REPOSITORIES_PREFIX} {', '.join(scope.reads)}\n"
        f"{WRITES_REPOSITORIES_PREFIX} {writes}\n"
    )


def replaced_session_scope_text(text: str, scope: SessionScope) -> str:
    """Return ``text`` with its structural scope fields rewritten in place.

    Stripping and re-inserting the block relocates it; an already-rendered
    session file keeps its exact shape when only the two structural lines are
    substituted. Fenced examples are left alone, because the indices come from
    the same fence-aware scan the parser uses.
    """
    reads_line, writes_line = render_session_scope(scope).rstrip("\n").splitlines()
    lines = text.splitlines()
    for index in _structural_session_scope_indices(lines):
        lines[index] = (
            reads_line
            if lines[index].startswith(READS_REPOSITORIES_PREFIX)
            else writes_line
        )
    rewritten = "\n".join(lines)
    return f"{rewritten}\n" if text.endswith("\n") else rewritten


def parse_qualified_path(
    value: str,
    registry_aliases: Iterable[str] | None = None,
) -> QualifiedPath:
    """Parse one canonical ``Alias:path`` reference."""
    if not isinstance(value, str) or value.count(":") < 1:
        raise ValueError("qualified path must use Alias:path syntax")
    alias, path = value.split(":", 1)
    qualified = QualifiedPath(alias=alias, path=path)
    if registry_aliases is not None and alias not in frozenset(registry_aliases):
        raise ValueError(f"qualified path uses unknown repository alias: {alias}")
    return qualified


def render_qualified_path(path: QualifiedPath) -> str:
    """Render one canonical ``Alias:path`` reference."""
    return str(path)


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
class SessionHandoff:
    """Parsed `## Handoff` block contents for one session file."""

    session: SessionFile
    status: StatusValue
    raw_value: str
    line_number: int | None
    files_modified: tuple[str, ...] = ()
    key_decisions: str | None = None
    blockers: str | None = None
    notes: str | None = None
    discovered_facts: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()

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
class ProgressEntry:
    """One rendered session row in the runner-managed `progress.md` journal."""

    session_id: SessionId
    status: StatusValue
    completed_at: str
    files_modified: tuple[str, ...] = ()
    key_decisions: str | None = None
    discovered_facts: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    blockers: str | None = None


@dataclass(frozen=True)
class PlanSnapshot:
    """Current durable state read from a split session plan directory."""

    plan_dir: Path
    context_path: Path
    manifest_path: Path
    plan_title: str
    sessions: tuple[SessionStatus, ...]
    content_sha256: str
    path_hash: str
    fingerprint: str

    @property
    def plan_hash(self) -> str:
        return self.fingerprint

    @property
    def next_pending(self) -> SessionStatus | None:
        return next(
            (session for session in self.sessions if session.status == "pending"), None
        )

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


@dataclass(frozen=True)
class ReadinessMetadata:
    """Readiness state recorded in a plan manifest."""

    state: ReadinessState
    readiness_sha256: str
    checked_at: str


def canonicalize_path(path: str | Path) -> Path:
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


def _retire_plan_transaction(canonical_dir: Path, transaction_dir: Path) -> None:
    """Atomically remove the live marker before best-effort payload cleanup."""
    cleanup_dir = canonical_dir / PLAN_TRANSACTION_CLEANUP_DIR_NAME
    shutil.rmtree(cleanup_dir, ignore_errors=True)
    os.replace(transaction_dir, cleanup_dir)
    _fsync_directory(canonical_dir)
    shutil.rmtree(cleanup_dir)
    _fsync_directory(canonical_dir)


@contextmanager
def _plan_transaction_mutex(
    transaction_dir: Path, *, wait_seconds: float | None = None
) -> Iterator[tuple[int, int]]:
    """Exclude readers and writers through a bounded marker-scoped lock.

    The holder record is advisory diagnostic data. The kernel lock remains the
    authority, while the record lets a timed-out reader identify the process
    and thread that last acquired it.
    """
    lock_path = transaction_dir / PLAN_TRANSACTION_LOCK_NAME
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        bound = (
            PLAN_TRANSACTION_LOCK_WAIT_SECONDS
            if wait_seconds is None
            else max(wait_seconds, 0.0)
        )
        deadline = time.monotonic() + bound
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except OSError as error:
                if error.errno not in (
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EINTR,
                    errno.EWOULDBLOCK,
                ):
                    raise
                if time.monotonic() >= deadline:
                    holder = _plan_transaction_lock_holder(descriptor)
                    raise PlanError(
                        f"timed out after {bound:g}s waiting for plan transaction "
                        f"lock at {lock_path}; holder {holder}"
                    ) from error
                time.sleep(PLAN_TRANSACTION_LOCK_POLL_SECONDS)

        holder_payload = json.dumps(
            {
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(holder_payload)
        while view:
            view = view[os.write(descriptor, view) :]
        stat_result = os.fstat(descriptor)
        yield stat_result.st_dev, stat_result.st_ino
    finally:
        try:
            if held:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _plan_transaction_lock_holder(descriptor: int) -> str:
    """Describe the advisory holder record visible through ``descriptor``."""
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        payload = json.loads(os.read(descriptor, 4096).decode("utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return "unknown (metadata unavailable)"
    if not isinstance(payload, dict):
        return "unknown (metadata unavailable)"
    pid = payload.get("pid")
    hostname = payload.get("hostname")
    thread = payload.get("thread")
    if not isinstance(pid, int) or not isinstance(hostname, str):
        return "unknown (metadata unavailable)"
    description = f"pid {pid} on {hostname}"
    if isinstance(thread, str) and thread:
        description += f" thread {thread!r}"
    return description


def _transaction_lock_is_current(
    transaction_dir: Path, identity: tuple[int, int]
) -> bool:
    """Return whether ``identity`` still names the live marker's lock file."""
    try:
        stat_result = (transaction_dir / PLAN_TRANSACTION_LOCK_NAME).stat()
    except FileNotFoundError:
        return False
    return identity == (stat_result.st_dev, stat_result.st_ino)


def _recover_pending_plan_transaction_locked(
    canonical_dir: Path, transaction_dir: Path
) -> bool:
    """Roll forward the live transaction while its marker mutex is held."""
    if not transaction_dir.exists():
        return False
    if not transaction_dir.is_dir():
        raise PlanError(
            f"invalid plan transaction marker: {transaction_dir} is not a directory"
        )

    journal_path = transaction_dir / PLAN_TRANSACTION_JOURNAL_NAME
    if not journal_path.exists():
        raise PlanError("pending plan transaction marker has no durable journal")

    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PlanError(
            f"cannot read pending plan transaction journal: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise PlanError("pending plan transaction journal must contain a JSON object")
    if payload.get("version") != PLAN_TRANSACTION_SCHEMA_VERSION:
        raise PlanError("pending plan transaction journal uses an unsupported version")
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PlanError("pending plan transaction journal has no entries")

    entries: list[tuple[Path, bytes]] = []
    for index, raw_entry in enumerate(raw_entries, start=1):
        if not isinstance(raw_entry, dict):
            raise PlanError(f"pending plan transaction entry #{index} is malformed")
        relative_path = raw_entry.get("path")
        payload_name = raw_entry.get("payload")
        expected_sha = raw_entry.get("sha256")
        if not all(
            isinstance(value, str)
            for value in (relative_path, payload_name, expected_sha)
        ):
            raise PlanError(f"pending plan transaction entry #{index} is malformed")
        parsed_path = PurePosixPath(relative_path)
        if (
            not relative_path
            or relative_path == "."
            or parsed_path.is_absolute()
            or ".." in parsed_path.parts
            or payload_name != f"{index - 1:06d}.bin"
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
        ):
            raise PlanError(f"pending plan transaction entry #{index} is unsafe")
        destination = canonical_dir.joinpath(*parsed_path.parts)
        try:
            destination.resolve().relative_to(canonical_dir)
        except ValueError as error:
            raise PlanError(
                f"pending plan transaction entry #{index} escapes the plan directory"
            ) from error
        payload_path = transaction_dir / payload_name
        try:
            content = payload_path.read_bytes()
        except OSError as error:
            raise PlanError(
                f"cannot read pending plan transaction payload #{index}: {error}"
            ) from error
        if hashlib.sha256(content).hexdigest() != expected_sha:
            raise PlanError(
                f"pending plan transaction payload #{index} failed its checksum"
            )
        entries.append((destination, content))

    try:
        for destination, content in entries:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp_path = destination.with_name(
                f".{destination.name}.asl-transaction-recovery.tmp"
            )
            with temp_path.open("wb") as handle:
                handle.write(content)
                handle.flush()
                durability.fullsync(handle.fileno())
            os.replace(temp_path, destination)
            _fsync_directory(destination.parent)
        _retire_plan_transaction(canonical_dir, transaction_dir)
    except OSError as error:
        raise PlanError(f"cannot recover pending plan transaction: {error}") from error
    return True


def recover_pending_plan_transaction(plan_dir: str | Path) -> bool:
    """Roll forward a durable multi-file plan transaction, if one is pending.

    The transaction payloads remain immutable until every destination has been
    replaced. Replaying all entries is therefore idempotent, including after a
    second interruption during recovery. A staging directory without a journal
    predates publication and is safe to discard.
    """
    canonical_dir = canonicalize_path(plan_dir)
    transaction_dir = canonical_dir / PLAN_TRANSACTION_DIR_NAME
    while transaction_dir.exists():
        try:
            with _plan_transaction_mutex(transaction_dir) as identity:
                # The writer may have retired this marker while we waited and a
                # later transaction may already have published another one.
                # Never act on a marker protected by a different lock inode.
                if not _transaction_lock_is_current(transaction_dir, identity):
                    continue
                return _recover_pending_plan_transaction_locked(
                    canonical_dir, transaction_dir
                )
        except FileNotFoundError:
            # Retirement between the existence check and lock open means the
            # commit completed; re-check in case another marker replaced it.
            continue
        except OSError as error:
            raise PlanError(f"cannot lock pending plan transaction: {error}") from error
    return False


def plan_metadata_path(plan_dir: str | Path) -> Path:
    """Return the stable workspace-metadata path for ``plan_dir``."""
    return canonicalize_path(plan_dir) / PLAN_METADATA_FILE_NAME


def load_plan_metadata(plan_dir: str | Path) -> PlanMetadataValue | None:
    """Load workspace metadata, returning ``None`` when it has not been written."""
    recover_pending_plan_transaction(plan_dir)
    path = plan_metadata_path(plan_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        _raise_read_error(path, error)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise PlanError(
            f"{path} is not valid JSON: {error.msg} at line {error.lineno}"
        ) from error
    if not isinstance(payload, dict):
        raise PlanError(f"{path} must contain a JSON object")

    version = payload.get("version")
    if version in {
        PLAN_METADATA_V2_SCHEMA_VERSION,
        PLAN_METADATA_V3_SCHEMA_VERSION,
    }:
        return _parse_workspace_plan_metadata(path, payload)
    return _parse_legacy_plan_metadata(path, payload)


def _parse_legacy_plan_metadata(path: Path, payload: dict[str, object]) -> PlanMetadata:
    """Parse v1 metadata without changing its strict compatibility contract."""
    unknown = sorted(set(payload) - {"version", "cwd"})
    if unknown:
        raise PlanError(f"{path} contains unknown keys: {', '.join(unknown)}")
    version = payload.get("version")
    if type(version) is not int or version != PLAN_METADATA_SCHEMA_VERSION:
        raise PlanError(
            f"{path} uses unsupported version {version!r}; "
            f"expected {PLAN_METADATA_SCHEMA_VERSION}"
        )
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise PlanError(f"{path} field 'cwd' must be a non-empty string")
    if cwd != cwd.strip() or not Path(cwd).is_absolute():
        raise PlanError(f"{path} field 'cwd' must be a canonical absolute path")
    canonical_cwd = str(canonicalize_path(cwd))
    if cwd != canonical_cwd:
        raise PlanError(
            f"{path} field 'cwd' is not canonical: {cwd!r}; expected {canonical_cwd!r}"
        )
    return PlanMetadata(cwd=cwd, version=version)


def _parse_workspace_plan_metadata(
    path: Path, payload: dict[str, object]
) -> WorkspacePlanMetadata:
    version = payload.get("version")
    allowed = {"version", "workspace_root", "registry_digest"}
    if version == PLAN_METADATA_V3_SCHEMA_VERSION:
        allowed.add("workspace_inventory_excludes")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise PlanError(f"{path} contains unknown keys: {', '.join(unknown)}")
    if type(version) is not int or version not in {
        PLAN_METADATA_V2_SCHEMA_VERSION,
        PLAN_METADATA_V3_SCHEMA_VERSION,
    }:
        raise PlanError(
            f"{path} uses unsupported version {version!r}; "
            f"expected {PLAN_METADATA_V2_SCHEMA_VERSION} or "
            f"{PLAN_METADATA_V3_SCHEMA_VERSION}"
        )
    workspace_root = payload.get("workspace_root")
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        raise PlanError(f"{path} field 'workspace_root' must be a non-empty string")
    if (
        workspace_root != workspace_root.strip()
        or not Path(workspace_root).is_absolute()
    ):
        raise PlanError(
            f"{path} field 'workspace_root' must be a canonical absolute path"
        )
    canonical_root = str(canonicalize_path(workspace_root))
    if workspace_root != canonical_root:
        raise PlanError(
            f"{path} field 'workspace_root' is not canonical: "
            f"{workspace_root!r}; expected {canonical_root!r}"
        )
    registry_digest = payload.get("registry_digest")
    if (
        not isinstance(registry_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", registry_digest) is None
    ):
        raise PlanError(
            f"{path} field 'registry_digest' must be a lowercase SHA-256 digest"
        )
    # Version 3 files written before the policy was retired still carry this
    # key; accept a well-formed value and discard it rather than churn versions.
    raw_excludes = payload.get("workspace_inventory_excludes", [])
    if not isinstance(raw_excludes, list) or not all(
        isinstance(value, str) for value in raw_excludes
    ):
        raise PlanError(
            f"{path} field 'workspace_inventory_excludes' must be a string array"
        )
    return WorkspacePlanMetadata(
        workspace_root=workspace_root,
        registry_digest=registry_digest,
        version=version,
    )


def save_plan_metadata(plan_dir: str | Path, cwd: str | Path) -> Path:
    """Atomically persist the canonical workspace associated with a plan."""
    path = plan_metadata_path(plan_dir)
    canonical_cwd = canonicalize_path(cwd)
    metadata = PlanMetadata(cwd=str(canonical_cwd))
    text = json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)
    return path


def save_workspace_plan_metadata(
    plan_dir: str | Path,
    workspace_root: str | Path,
    registry_digest: str,
) -> Path:
    """Atomically persist a registered workspace binding."""
    path = plan_metadata_path(plan_dir)
    canonical_root = canonicalize_path(workspace_root)
    metadata = WorkspacePlanMetadata(
        workspace_root=str(canonical_root),
        registry_digest=registry_digest,
    )
    # Validate the digest through the same strict parser used for disk reads.
    _parse_workspace_plan_metadata(path, metadata.to_dict())
    text = json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)
    return path


save_plan_metadata_v2 = save_workspace_plan_metadata


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


def next_top_level_session_id(plan_dir: str | Path) -> SessionId:
    """Return a fresh top-level session id one greater than the max in `plan_dir`."""
    canonical_dir = canonicalize_path(plan_dir)
    sessions = discover_session_files(canonical_dir)
    if not sessions:
        return SessionId((1,))
    max_top = max(session.session_id.components[0] for session in sessions)
    return SessionId((max_top + 1,))


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
    rendered = ", ".join(f"{source} -> {target}" for source, target in suggestions[:5])
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
    if normalized == "pending":
        return "pending"
    if normalized == "complete":
        return "complete"
    return "unknown"


def _final_handoff_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return index
    return None


def _is_generated_review_session_text(text: str) -> bool:
    """Return whether ``text`` belongs to a generated final-review session."""
    return (
        _GENERATED_REVIEW_SESSION_MARKER in text
        or _GENERATED_REVIEW_SESSION_VERSION_MARKER_RE.search(text) is not None
    )


@dataclass(frozen=True)
class _ReviewArtifactProjection:
    """A handoff text with its generated review artifacts projected away.

    ``exclusive`` says every ``Files modified`` entry named a review artifact,
    which is the only shape the persisted normalization rewrites on disk.
    """

    text: str
    exclusive: bool


def _is_generated_review_artifact_entry(entry: str) -> bool:
    """Return whether one ``Files modified`` entry names a review artifact."""
    candidate = entry.strip()
    if candidate.startswith("./"):
        candidate = candidate[2:]
    return candidate in _GENERATED_REVIEW_PLAN_ARTIFACTS


def _project_generated_review_handoff_artifacts(
    text: str,
) -> _ReviewArtifactProjection:
    """Project generated review artifacts out of a completed handoff.

    ``final-review.md`` and ``final-review.json`` live beside session files;
    they are evidence produced by a generated review, never files modified in
    one of the repositories under review.  A review harness can nevertheless
    record them in its own handoff.  Drop those entries during inspection so
    registered-plan path validation remains about repository changes, and do it
    per entry: a handoff that also records a real repository change keeps that
    entry, and its ordinary fail-closed validation, without the review having
    to rewrite an otherwise valid handoff.  Every other entry stays untouched.
    """
    if not _is_generated_review_session_text(text):
        return _ReviewArtifactProjection(text, False)

    lines = text.splitlines(keepends=True)
    plain_lines = [line.rstrip("\r\n") for line in lines]
    handoff_start = _final_handoff_start(plain_lines)
    if handoff_start is None:
        return _ReviewArtifactProjection(text, False)

    section_end = len(lines)
    for index in range(handoff_start + 1, len(lines)):
        if plain_lines[index].startswith("## "):
            section_end = index
            break

    status = "empty"
    files_index: int | None = None
    for index in range(handoff_start + 1, section_end):
        line = plain_lines[index]
        if line.startswith(STATUS_PREFIX):
            status = parse_status_value(line[len(STATUS_PREFIX) :])
        elif line.startswith(HANDOFF_FILES_PREFIX):
            files_index = index

    if status != "complete" or files_index is None:
        return _ReviewArtifactProjection(text, False)

    inline = plain_lines[files_index][len(HANDOFF_FILES_PREFIX) :].strip()
    entries = tuple(part.strip() for part in inline.split(",") if part.strip())
    retained = tuple(
        entry for entry in entries if not _is_generated_review_artifact_entry(entry)
    )
    if not entries or len(retained) == len(entries):
        return _ReviewArtifactProjection(text, False)

    line_ending = "\r\n" if lines[files_index].endswith("\r\n") else "\n"
    if not lines[files_index].endswith(("\n", "\r")):
        line_ending = ""
    rendered = ", ".join(retained) if retained else "(none)"
    lines[files_index] = f"{HANDOFF_FILES_PREFIX} {rendered}{line_ending}"
    return _ReviewArtifactProjection("".join(lines), not retained)


def normalize_generated_review_handoff_artifacts(
    plan_dir: str | Path,
) -> tuple[Path, ...]:
    """Persist the bounded generated-review handoff normalization.

    This is an idempotent recovery for a completed generated review that wrote
    only review-owned plan artifacts into ``Files modified``.  It deliberately
    leaves every other handoff entry alone, including unqualified repository
    paths, so multi-repository validation continues to fail closed.  A handoff
    that mixes review artifacts with other entries is projected during
    inspection but never rewritten here, so no handoff is edited on disk to buy
    a validation that reading it already grants.
    """
    recover_pending_plan_transaction(plan_dir)
    canonical_dir = canonicalize_path(plan_dir)
    changed_paths: list[Path] = []
    for session in discover_session_files(canonical_dir):
        try:
            text = session.path.read_text(encoding="utf-8")
        except OSError as error:
            _raise_read_error(session.path, error)
        projection = _project_generated_review_handoff_artifacts(text)
        if not projection.exclusive or projection.text == text:
            continue
        _atomic_write_text(session.path, projection.text)
        changed_paths.append(session.path)
    return tuple(changed_paths)


_REPOSITORY_REGISTRY_UNSET = object()


def read_session_handoff(
    session: SessionFile,
    *,
    repository_registry: RepositoryRegistry
    | None
    | object = _REPOSITORY_REGISTRY_UNSET,
) -> SessionHandoff:
    """Read the final `## Handoff` block in a session file as a structured record.

    Body bullets (`- **Status**:`, `- **Files modified**:`, etc.) are captured
    until the first `### ` or `## ` heading. Recognized subsections
    (`### Discovered Facts`, `### Open Questions`) then accumulate top-level
    `- ` bullets — with no leading whitespace, matching the body parser — into
    their respective tuples. Subsection bullet capture stops at the next
    `### ` or `## ` heading. Subsections may appear in either order; missing
    subsections default to empty tuples; unknown `### ` subsections are
    ignored.

    Missing handoff blocks and blocks without a `- **Status**:` line both
    yield `status="empty"` and `line_number=None`, matching the prior
    behavior of `read_session_status`.
    """
    recover_pending_plan_transaction(session.path.parent)
    try:
        text = session.path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(session.path, error)
    lines = _project_generated_review_handoff_artifacts(text).text.splitlines()

    registry = (
        read_repository_registry(session.path.parent)
        if repository_registry is _REPOSITORY_REGISTRY_UNSET
        else repository_registry
    )
    if registry is not None and not isinstance(registry, RepositoryRegistry):
        raise TypeError("repository_registry must be a RepositoryRegistry or None")
    scope = (
        parse_session_scope_lines(lines, registry.aliases)
        if registry is not None
        else None
    )

    handoff_start = _final_handoff_start(lines)
    if handoff_start is None:
        return SessionHandoff(
            session=session,
            status="empty",
            raw_value="",
            line_number=None,
        )

    status: StatusValue = "empty"
    raw_value = ""
    status_line: int | None = None
    files_modified: tuple[str, ...] = ()
    key_decisions: str | None = None
    blockers: str | None = None
    notes: str | None = None
    discovered_facts: list[str] = []
    open_questions: list[str] = []
    current_subsection: str | None = None

    for index in range(handoff_start + 1, len(lines)):
        line = lines[index]
        if line.startswith("## "):
            break
        if line.startswith("### "):
            heading = line.rstrip()
            if heading == DISCOVERED_FACTS_HEADING:
                current_subsection = "facts"
            elif heading == OPEN_QUESTIONS_HEADING:
                current_subsection = "questions"
            else:
                current_subsection = "other"
            continue

        if current_subsection is None:
            if line.startswith(STATUS_PREFIX):
                raw_value = line[len(STATUS_PREFIX) :].strip()
                status = parse_status_value(raw_value)
                status_line = index + 1
            elif line.startswith(HANDOFF_FILES_PREFIX):
                inline = line[len(HANDOFF_FILES_PREFIX) :].strip()
                files_modified = (inline,) if inline else ()
            elif line.startswith(HANDOFF_DECISIONS_PREFIX):
                inline = line[len(HANDOFF_DECISIONS_PREFIX) :].strip()
                key_decisions = inline or None
            elif line.startswith(HANDOFF_BLOCKERS_PREFIX):
                inline = line[len(HANDOFF_BLOCKERS_PREFIX) :].strip()
                blockers = inline or None
            elif line.startswith(HANDOFF_NOTES_PREFIX):
                inline = line[len(HANDOFF_NOTES_PREFIX) :].strip()
                notes = inline or None
        elif current_subsection == "facts" and line.startswith("- "):
            content = line[2:].strip()
            if content:
                discovered_facts.append(content)
        elif current_subsection == "questions" and line.startswith("- "):
            content = line[2:].strip()
            if content:
                open_questions.append(content)

    handoff = SessionHandoff(
        session=session,
        status=status,
        raw_value=raw_value,
        line_number=status_line,
        files_modified=files_modified,
        key_decisions=key_decisions,
        blockers=blockers,
        notes=notes,
        discovered_facts=tuple(discovered_facts),
        open_questions=tuple(open_questions),
    )
    if registry is not None and scope is not None and handoff.status == "complete":
        validate_completed_handoff_paths(handoff, scope, registry.aliases)
    return handoff


def read_session_status(
    session: SessionFile,
    *,
    repository_registry: RepositoryRegistry
    | None
    | object = _REPOSITORY_REGISTRY_UNSET,
) -> SessionStatus:
    """Read the status marker from the final handoff block in a session file."""
    handoff = read_session_handoff(
        session,
        repository_registry=repository_registry,
    )
    return SessionStatus(
        session=handoff.session,
        status=handoff.status,
        raw_value=handoff.raw_value,
        line_number=handoff.line_number,
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


def parse_repository_registry_manifest(
    manifest_text: str,
) -> RepositoryRegistry | None:
    """Parse the exact authoritative repository table from rendered manifest text."""
    lines = manifest_text.splitlines()
    headings: list[int] = []
    fence: str | None = None
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match is not None:
            delimiter = match.group(1)[0]
            if fence is None:
                fence = delimiter
            elif fence == delimiter:
                fence = None
            continue
        if fence is None and line == "## Repositories":
            headings.append(index)
    if not headings:
        return None
    if len(headings) != 1:
        raise PlanError("manifest contains duplicate ## Repositories sections")

    index = headings[0] + 1
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines):
        return None
    header = _split_table_row(lines[index])
    if header is None:
        return None
    if tuple(cell.strip() for cell in header[1]) != (
        "Alias",
        "Path",
        "Origin",
    ):
        raise PlanError(
            "manifest repository registry columns must be exactly "
            "Alias, Path, and Origin"
        )
    if index + 1 >= len(lines):
        raise PlanError("manifest repository registry separator row is missing")
    separator = _split_table_row(lines[index + 1])
    if (
        separator is None
        or len(separator[1]) != 3
        or not _is_separator_row(separator[1])
    ):
        raise PlanError("manifest repository registry separator row is malformed")

    repositories: list[Repository] = []
    index += 2
    while index < len(lines):
        parsed = _split_table_row(lines[index])
        if parsed is None:
            break
        cells = tuple(cell.strip() for cell in parsed[1])
        if len(cells) != 3 or any(not cell for cell in cells):
            raise PlanError("manifest repository registry row is malformed")
        try:
            repositories.append(Repository(*cells))
        except WorkspaceError as error:
            raise PlanError(
                f"manifest repository registry is invalid: {error}"
            ) from error
        index += 1
    if not repositories:
        raise PlanError("manifest repository registry must contain at least one row")
    try:
        return RepositoryRegistry(tuple(repositories))
    except WorkspaceError as error:
        raise PlanError(f"manifest repository registry is invalid: {error}") from error


def read_repository_registry(plan_dir: str | Path) -> RepositoryRegistry | None:
    """Read a rendered plan's optional authoritative repository registry."""
    recover_pending_plan_transaction(plan_dir)
    manifest_path = canonicalize_path(plan_dir) / "manifest.md"
    if not _is_file(manifest_path):
        return None
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    return parse_repository_registry_manifest(text)


def validate_completed_handoff_paths(
    handoff: SessionHandoff,
    scope: SessionScope,
    registry_aliases: Iterable[str],
) -> tuple[QualifiedPath, ...]:
    """Reject completed multi-repo handoffs whose file entries are not qualified."""
    if handoff.status != "complete":
        return ()
    inline = handoff.files_modified[0].strip() if handoff.files_modified else ""
    if not inline:
        if not scope.writes:
            # A session that declares no writable repository had nothing it was
            # allowed to change, so an omitted bullet asserts what the ``(none)``
            # in a generated repair child asserts explicitly.
            return ()
        raise PlanError(
            f"{handoff.session.filename} completed handoff is missing Files modified"
        )
    if inline == "(none)":
        return ()
    if inline == "(none yet)":
        raise PlanError(
            f"{handoff.session.filename} completed handoff retains the pending "
            "Files modified placeholder"
        )

    qualified: list[QualifiedPath] = []
    for entry in (part.strip() for part in inline.split(",")):
        try:
            parsed = parse_qualified_path(entry, registry_aliases)
        except ValueError as error:
            raise PlanError(
                f"{handoff.session.filename} has invalid Files modified entry "
                f"{entry!r}: {error}"
            ) from error
        if parsed.alias not in scope.writes:
            raise PlanError(
                f"{handoff.session.filename} Files modified entry {entry!r} is outside "
                f"Writes repositories"
            )
        qualified.append(parsed)
    return tuple(qualified)


def completed_handoff_registry_blocker(
    handoff: SessionHandoff,
    *,
    scope: SessionScope | None = None,
    registry_aliases: Iterable[str] | None = None,
) -> str | None:
    """Say why a new repository registry would make this handoff unparsable.

    Giving a registry to a plan that already has completed sessions turns
    every legacy ``Files modified`` entry into a value
    :func:`validate_completed_handoff_paths` must accept, so a migration that
    ignored this stops the plan from parsing at all. With the migration's own
    derived ``scope`` and aliases the answer is that function's; without them
    it is syntactic, because an entry that is not already ``Alias:path``
    survives no registry.
    """
    if handoff.status != "complete":
        return None
    if scope is not None and registry_aliases is not None:
        try:
            validate_completed_handoff_paths(handoff, scope, registry_aliases)
        except PlanError as error:
            return str(error)
        return None

    inline = handoff.files_modified[0].strip() if handoff.files_modified else ""
    if not inline:
        return f"{handoff.session.filename} completed handoff is missing Files modified"
    if inline == "(none)":
        return None
    if inline == "(none yet)":
        return (
            f"{handoff.session.filename} completed handoff retains the pending "
            "Files modified placeholder"
        )
    for entry in (part.strip() for part in inline.split(",")):
        try:
            parse_qualified_path(entry)
        except ValueError as error:
            return (
                f"{handoff.session.filename} has invalid Files modified entry "
                f"{entry!r}: {error}"
            )
    return None


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
    """Hash the current plan files in deterministic relative-path order.

    Legacy `parallel-validation.md` is excluded entirely. Legacy
    `## Parallel Execution` (in `manifest.md`) and `## Parallel Metadata`
    (in `session-<id>.md`) sections are stripped before hashing so leftover
    parallel scheduling text cannot move the content hash.
    """
    canonical_dir = canonicalize_path(plan_dir)
    selected_sessions = (
        discover_session_files(canonical_dir) if sessions is None else sessions
    )
    selected_sessions = tuple(
        sorted(selected_sessions, key=lambda session: session.session_id.sort_key)
    )
    relative_names = ["context.md", "manifest.md"]

    for session in selected_sessions:
        relative_names.append(session.filename)
        expansion_name = session.expansion_filename
        if _is_file(canonical_dir / expansion_name):
            relative_names.append(expansion_name)

    digest = hashlib.sha256()
    for relative_name in relative_names:
        file_path = canonical_dir / relative_name
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as error:
            _raise_read_error(file_path, error)
        normalized = _normalize_for_content_hash(relative_name, text)
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def session_artifact_digests(
    plan_dir: str | Path,
    sessions: tuple[SessionFile, ...] | None = None,
    *,
    exclude_session_id: str | None = None,
) -> dict[str, str]:
    """Digest each session artifact on its own, keyed by plan-relative name.

    Mirrors the `plan_content_sha256` file set minus `context.md` and
    `manifest.md`, which the runner itself rewrites when it syncs derived
    status tables. Raw bytes are hashed rather than the normalized text so a
    whitespace-only edit still registers. An unreadable file maps to
    `UNREADABLE_ARTIFACT_DIGEST` instead of raising, so a file that vanishes
    mid-turn compares unequal against a readable capture rather than
    collapsing the comparison.
    """
    canonical_dir = canonicalize_path(plan_dir)
    selected_sessions = (
        discover_session_files(canonical_dir) if sessions is None else sessions
    )
    digests: dict[str, str] = {}
    for session in sorted(
        selected_sessions, key=lambda session: session.session_id.sort_key
    ):
        if exclude_session_id is not None and session.id == exclude_session_id:
            continue
        relative_names = [session.filename]
        expansion_name = session.expansion_filename
        if _is_file(canonical_dir / expansion_name):
            relative_names.append(expansion_name)
        for relative_name in relative_names:
            digests[relative_name] = _artifact_digest(canonical_dir / relative_name)
    return digests


def _artifact_digest(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return UNREADABLE_ARTIFACT_DIGEST
    return hashlib.sha256(data).hexdigest()


def plan_fingerprint(plan_dir: str | Path, content_hash: str) -> str:
    canonical_dir = canonicalize_path(plan_dir)
    digest = hashlib.sha256()
    digest.update(str(canonical_dir).encode("utf-8", "surrogateescape"))
    digest.update(b"\0")
    digest.update(content_hash.encode("ascii"))
    return digest.hexdigest()


def _strip_inline_code_ticks(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == "`" and text[-1] == "`":
        return text[1:-1].strip()
    return text


def parse_readiness_state(value: str) -> ReadinessState:
    """Parse and validate a readiness state name."""
    normalized = value.strip().lower()
    if normalized in READINESS_STATES:
        return normalized  # type: ignore[return-value]
    raise PlanError(f"invalid readiness state: {value!r}")


def _is_readiness_line(line: str) -> bool:
    stripped = line.lstrip()
    return (
        stripped.startswith(READINESS_PREFIX)
        or stripped.startswith(READINESS_SHA_PREFIX)
        or stripped.startswith(READINESS_CHECKED_PREFIX)
    )


def parse_readiness_metadata(manifest_text: str) -> ReadinessMetadata | None:
    """Parse `**Readiness**`, `**Readiness SHA-256**`, `**Readiness checked at**` "
    "lines."""
    state: ReadinessState | None = None
    sha: str | None = None
    checked_at: str | None = None

    for raw_line in manifest_text.splitlines():
        line = raw_line.strip()
        if line.startswith(READINESS_SHA_PREFIX):
            payload = _strip_inline_code_ticks(line[len(READINESS_SHA_PREFIX) :])
            if not payload:
                raise PlanError("manifest readiness SHA-256 is empty")
            sha = payload
        elif line.startswith(READINESS_CHECKED_PREFIX):
            payload = _strip_inline_code_ticks(line[len(READINESS_CHECKED_PREFIX) :])
            if not payload:
                raise PlanError("manifest readiness checked-at timestamp is empty")
            checked_at = payload
        elif line.startswith(READINESS_PREFIX):
            payload = _strip_inline_code_ticks(line[len(READINESS_PREFIX) :])
            if not payload:
                raise PlanError("manifest readiness state is empty")
            state = parse_readiness_state(payload)

    if state is None and sha is None and checked_at is None:
        return None
    if state is None:
        raise PlanError("manifest readiness state missing")
    if sha is None:
        raise PlanError("manifest readiness SHA-256 missing")
    if checked_at is None:
        raise PlanError("manifest readiness checked-at timestamp missing")
    return ReadinessMetadata(state=state, readiness_sha256=sha, checked_at=checked_at)


def read_readiness_metadata(plan_dir: str | Path) -> ReadinessMetadata | None:
    """Read readiness metadata from `manifest.md` in a plan directory."""
    recover_pending_plan_transaction(plan_dir)
    canonical_dir = canonicalize_path(plan_dir)
    manifest_path = canonical_dir / "manifest.md"
    if not _is_file(manifest_path):
        raise PlanError(f"missing manifest.md in plan directory: {canonical_dir}")
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    return parse_readiness_metadata(text)


def _render_readiness_metadata_lines(metadata: ReadinessMetadata) -> list[str]:
    return [
        f"**Readiness**: {metadata.state}",
        f"**Readiness SHA-256**: `{metadata.readiness_sha256}`",
        f"**Readiness checked at**: {metadata.checked_at}",
    ]


def _find_readiness_insertion_index(lines: list[str]) -> int:
    h2_index = next(
        (index for index, line in enumerate(lines) if line.startswith("## ")),
        len(lines),
    )
    last_metadata = -1
    for index in range(h2_index):
        if lines[index].lstrip().startswith("**"):
            last_metadata = index
    if last_metadata == -1:
        return 0
    return last_metadata + 1


def replace_readiness_metadata_in_text(text: str, metadata: ReadinessMetadata) -> str:
    """Insert or replace readiness metadata lines in manifest text."""
    new_block = _render_readiness_metadata_lines(metadata)
    lines = text.splitlines(keepends=False)
    existing = [index for index, line in enumerate(lines) if _is_readiness_line(line)]

    if existing:
        insertion_index = existing[0]
        kept = [
            line for index, line in enumerate(lines) if not _is_readiness_line(line)
        ]
        result = kept[:insertion_index] + new_block + kept[insertion_index:]
    else:
        insertion_index = _find_readiness_insertion_index(lines)
        result = lines[:insertion_index] + new_block + lines[insertion_index:]

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(result) + suffix


def write_readiness_metadata(plan_dir: str | Path, metadata: ReadinessMetadata) -> bool:
    """Write readiness metadata to `manifest.md`. Returns True if the file changed."""
    canonical_dir = canonicalize_path(plan_dir)
    manifest_path = canonical_dir / "manifest.md"
    if not _is_file(manifest_path):
        raise PlanError(f"missing manifest.md in plan directory: {canonical_dir}")
    try:
        original = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    updated = replace_readiness_metadata_in_text(original, metadata)
    if updated == original:
        return False
    _write_text(manifest_path, updated)
    return True


def _strip_final_handoff_block(text: str) -> str:
    lines = text.splitlines(keepends=False)
    handoff_start = _final_handoff_start(lines)
    if handoff_start is None:
        return text
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(lines[:handoff_start]) + suffix


def _strip_readiness_lines(text: str) -> str:
    lines = text.splitlines(keepends=False)
    kept = [line for line in lines if not _is_readiness_line(line)]
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(kept) + suffix


def _clear_status_cells_block(block: list[str]) -> list[str]:
    if len(block) < 2:
        return block

    header = _split_table_row(block[0])
    separator = _split_table_row(block[1])
    if header is None or separator is None or not _is_separator_row(separator[1]):
        return block

    headers = [_normalize_table_header(cell) for cell in header[1]]
    try:
        status_index = headers.index("status")
    except ValueError:
        return block

    session_index = next(
        (
            index
            for index, name in enumerate(headers)
            if name in SESSION_STATUS_TABLE_HEADERS
        ),
        None,
    )
    if session_index is None:
        return block

    updated = list(block)
    for row_index, row in enumerate(block[2:], start=2):
        parsed = _split_table_row(row)
        if parsed is None:
            continue
        leading, cells = parsed
        if max(session_index, status_index) >= len(cells):
            continue
        if _session_id_from_cell(cells[session_index]) is None:
            continue
        cleared = _replace_cell_content(cells[status_index], "")
        if cells[status_index] != cleared:
            cells[status_index] = cleared
            updated[row_index] = _join_table_row(leading, cells)
    return updated


def _clear_status_cells_in_text(text: str) -> str:
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    index = 0

    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            out.append(lines[index])
            index += 1
            continue

        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        out.extend(_clear_status_cells_block(block))

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + suffix


LEGACY_PARALLEL_MANIFEST_SECTIONS = frozenset({"Parallel Execution"})
LEGACY_PARALLEL_SESSION_SECTIONS = frozenset({"Parallel Metadata"})


def _strip_markdown_sections(text: str, headings: frozenset[str]) -> str:
    """Remove whole H2 sections whose contents are derived snapshots."""
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        if line.startswith("## ") and line[3:].strip() in headings:
            if out and out[-1] == "":
                out.pop()
            index += 1
            while index < len(lines) and not lines[index].startswith("## "):
                index += 1
            while index < len(lines) and lines[index] == "":
                index += 1
            if index < len(lines) and out and out[-1] != "":
                out.append("")
            continue

        out.append(line)
        index += 1

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + suffix


def _normalize_for_content_hash(filename: str, text: str) -> str:
    if SESSION_FILE_RE.fullmatch(filename):
        return _strip_markdown_sections(text, LEGACY_PARALLEL_SESSION_SECTIONS)
    if filename == "manifest.md":
        # Readiness lines are runner bookkeeping rewritten with a fresh
        # `checked_at` on every turn; hashing them lets the runner move plan
        # identity by observing it. Status cells are *not* stripped here, unlike
        # `_normalize_for_readiness`: a session completing must still move the
        # content hash.
        return _strip_markdown_sections(
            _strip_readiness_lines(text), LEGACY_PARALLEL_MANIFEST_SECTIONS
        )
    return text


def _normalize_for_readiness(filename: str, text: str) -> str:
    if SESSION_FILE_RE.fullmatch(filename):
        return _strip_markdown_sections(
            _strip_final_handoff_block(text),
            LEGACY_PARALLEL_SESSION_SECTIONS,
        )
    if filename == "manifest.md":
        return _strip_markdown_sections(
            _clear_status_cells_in_text(_strip_readiness_lines(text)),
            LEGACY_PARALLEL_MANIFEST_SECTIONS,
        )
    if filename == "context.md":
        return _clear_status_cells_in_text(text)
    return text


def stable_readiness_sha256(
    plan_dir: str | Path,
    sessions: tuple[SessionFile, ...] | None = None,
) -> str:
    """Hash structural plan content, ignoring final handoffs and status bookkeeping.

    Legacy `parallel-validation.md` is excluded; legacy `## Parallel Execution`
    and `## Parallel Metadata` sections are stripped via `_normalize_for_readiness`.
    """
    canonical_dir = canonicalize_path(plan_dir)
    selected_sessions = (
        discover_session_files(canonical_dir) if sessions is None else sessions
    )
    selected_sessions = tuple(
        sorted(selected_sessions, key=lambda session: session.session_id.sort_key)
    )
    relative_names = ["context.md", "manifest.md"]

    for session in selected_sessions:
        relative_names.append(session.filename)
        expansion_name = session.expansion_filename
        if _is_file(canonical_dir / expansion_name):
            relative_names.append(expansion_name)

    digest = hashlib.sha256()
    for relative_name in relative_names:
        file_path = canonical_dir / relative_name
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError as error:
            _raise_read_error(file_path, error)
        normalized = _normalize_for_readiness(relative_name, text)
        digest.update(relative_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def inspect_plan(
    plan_dir: str | Path,
    *,
    repository_registry: RepositoryRegistry
    | None
    | object = _REPOSITORY_REGISTRY_UNSET,
) -> PlanSnapshot:
    """Inspect a split plan directory and return durable session state."""
    recover_pending_plan_transaction(plan_dir)
    canonical_dir = canonicalize_path(plan_dir)
    if not canonical_dir.is_dir():
        raise PlanError(f"plan directory does not exist: {canonical_dir}")

    context_path = canonical_dir / "context.md"
    manifest_path = canonical_dir / "manifest.md"
    if not _is_file(context_path):
        raise PlanError(f"missing context.md in plan directory: {canonical_dir}")
    if not _is_file(manifest_path):
        raise PlanError(f"missing manifest.md in plan directory: {canonical_dir}")

    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(manifest_path, error)
    plan_title = (
        plan_title_from_markdown(manifest_text)
        or canonical_dir.name
        or str(canonical_dir)
    )

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

    registry = (
        read_repository_registry(canonical_dir)
        if repository_registry is _REPOSITORY_REGISTRY_UNSET
        else repository_registry
    )
    if registry is not None and not isinstance(registry, RepositoryRegistry):
        raise TypeError("repository_registry must be a RepositoryRegistry or None")
    statuses: list[SessionStatus] = []
    for session in sessions:
        try:
            status = read_session_status(
                session,
                repository_registry=registry,
            )
        except SessionScopeError as error:
            raise SessionScopeError(
                error.reason,
                line_number=error.line_number,
                source_line=error.source_line,
                session_id=session.id,
                path=session.path,
            ) from error
        statuses.append(status)
    content_hash = plan_content_sha256(canonical_dir, sessions)

    return PlanSnapshot(
        plan_dir=canonical_dir,
        context_path=context_path,
        manifest_path=manifest_path,
        plan_title=plan_title,
        sessions=tuple(statuses),
        content_sha256=content_hash,
        path_hash=path_sha256(canonical_dir),
        fingerprint=plan_fingerprint(canonical_dir, content_hash),
    )


def _plan_file_stamps(canonical_dir: Path) -> dict[str, tuple[int, int, int]] | None:
    """Stamp every file `plan_content_sha256` hashes, or `None` if unreadable.

    Mirrors `plan_content_sha256`'s file-set enumeration so an added or removed
    session file *or* expansion note changes the stamp keys, not just the values.
    """
    try:
        recover_pending_plan_transaction(canonical_dir)
        sessions = discover_session_files(canonical_dir)
    except PlanError, OSError:
        return None

    relative_names = ["context.md", "manifest.md"]
    for session in sorted(sessions, key=lambda session: session.session_id.sort_key):
        relative_names.append(session.filename)
        expansion_name = session.expansion_filename
        try:
            if (canonical_dir / expansion_name).is_file():
                relative_names.append(expansion_name)
        except OSError:
            return None

    stamps: dict[str, tuple[int, int, int]] = {}
    for relative_name in relative_names:
        try:
            info = (canonical_dir / relative_name).stat()
        except OSError:
            return None
        stamps[relative_name] = (info.st_mtime_ns, info.st_size, info.st_ctime_ns)
    return stamps


class PlanSnapshotCache:
    """Reuse an `inspect_plan()` result while every hashed plan file is untouched.

    Instances are owned by their caller, so a one-shot CLI invocation keeps the
    uncached behaviour. `get()` is fail-closed: anything it cannot positively
    prove unchanged -- a different plan directory, a different file set, a
    changed `(mtime_ns, size, ctime_ns)`, or an `OSError` -- forces a full
    re-read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._plan_dir: Path | None = None
        self._snapshot: PlanSnapshot | None = None
        self._stamps: dict[str, tuple[int, int, int]] | None = None

    def get(self, plan_dir: str | Path) -> PlanSnapshot:
        """Return a validated cached snapshot, re-reading the plan on any doubt."""
        canonical_dir = canonicalize_path(plan_dir)
        with self._lock:
            if (
                self._snapshot is not None
                and self._plan_dir == canonical_dir
                and self._stamps is not None
                and self._stamps == _plan_file_stamps(canonical_dir)
            ):
                return self._snapshot
            return self._reread(canonical_dir)

    def get_fresh(self, plan_dir: str | Path) -> PlanSnapshot:
        """Re-read the plan unconditionally and re-record it as the cached state."""
        canonical_dir = canonicalize_path(plan_dir)
        with self._lock:
            return self._reread(canonical_dir)

    def invalidate(self) -> None:
        """Drop the cached snapshot so the next `get()` re-reads."""
        with self._lock:
            self._plan_dir = None
            self._snapshot = None
            self._stamps = None

    def _reread(self, canonical_dir: Path) -> PlanSnapshot:
        # Stamp before reading: a write racing the read then leaves a stamp that
        # no longer matches the file, which costs a re-read instead of serving a
        # snapshot that never existed on disk.
        stamps = _plan_file_stamps(canonical_dir)
        snapshot = inspect_plan(canonical_dir)
        self._plan_dir = canonical_dir
        self._snapshot = snapshot
        self._stamps = stamps
        return snapshot


_PROGRESS_VERSION_RE = re.compile(
    r"<!--\s*agent-session-loop progress-journal v(\d+)\s*-->"
)
_PROGRESS_HEADING_RE = re.compile(
    rf"^## Session ({SESSION_ID_PATTERN}) — (\S+) \((\S+)\)\s*$"
)
_PROGRESS_FILES_PREFIX = "- Files touched:"
_PROGRESS_DECISIONS_PREFIX = "- Decisions:"
_PROGRESS_FACTS_PREFIX = "- Discovered facts:"
_PROGRESS_QUESTIONS_PREFIX = "- Open questions:"
_PROGRESS_BLOCKERS_PREFIX = "- Blockers:"


def progress_path(plan_dir: str | Path) -> Path:
    """Return the canonical `progress.md` path for a plan directory."""
    return canonicalize_path(plan_dir) / PROGRESS_FILE_NAME


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via temp-file + `os.replace` so the destination is never "
    "half-written."""
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
    except OSError as error:
        _raise_read_error(path, error)
    try:
        os.replace(tmp_path, path)
    except OSError as error:
        with suppress(OSError):
            tmp_path.unlink()
        _raise_read_error(path, error)


def _validate_progress_version(path: Path, text: str) -> None:
    match = _PROGRESS_VERSION_RE.search(text)
    if match is None:
        raise PlanError(
            f"progress journal at {path} is missing schema version comment "
            f"({PROGRESS_SCHEMA_COMMENT!r})"
        )
    version = int(match.group(1))
    if version != PROGRESS_SCHEMA_VERSION:
        raise PlanError(
            f"progress journal at {path} uses schema v{version}; "
            f"this version of agent-session-loop only understands "
            f"v{PROGRESS_SCHEMA_VERSION}. Migration required."
        )


def _format_progress_csv(values: tuple[str, ...]) -> str:
    if not values:
        return "none"
    return ", ".join(values)


def _parse_progress_csv(value: str) -> tuple[str, ...]:
    text = value.strip()
    if not text or text == "none":
        return ()
    return tuple(part.strip() for part in text.split(",") if part.strip())


def render_progress_entry(entry: ProgressEntry) -> str:
    """Render a single progress entry block; always ends with a newline."""
    lines: list[str] = [
        f"## Session {entry.session_id.value} — {entry.status} ({entry.completed_at})",
        f"{_PROGRESS_FILES_PREFIX} {_format_progress_csv(entry.files_modified)}",
        f"{_PROGRESS_DECISIONS_PREFIX} {entry.key_decisions or 'none'}",
    ]
    if entry.discovered_facts:
        lines.append(_PROGRESS_FACTS_PREFIX)
        for fact in entry.discovered_facts:
            lines.append(f"  - {fact}")
    else:
        lines.append(f"{_PROGRESS_FACTS_PREFIX} none")
    if entry.open_questions:
        lines.append(_PROGRESS_QUESTIONS_PREFIX)
        for question in entry.open_questions:
            lines.append(f"  - {question}")
    else:
        lines.append(f"{_PROGRESS_QUESTIONS_PREFIX} none")
    lines.append(f"{_PROGRESS_BLOCKERS_PREFIX} {entry.blockers or 'none'}")
    return "\n".join(lines) + "\n"


def render_progress_journal(entries: tuple[ProgressEntry, ...]) -> str:
    """Render a complete progress journal with header and all entries."""
    if not entries:
        return PROGRESS_HEADER
    text = PROGRESS_HEADER + "\n" + render_progress_entry(entries[0])
    for entry in entries[1:]:
        text = text.rstrip("\n") + "\n\n" + render_progress_entry(entry)
    return text


def _consume_indented_bullets(lines: list[str], start: int) -> tuple[list[str], int]:
    bullets: list[str] = []
    index = start
    while index < len(lines) and lines[index].startswith("  - "):
        bullets.append(lines[index][4:].strip())
        index += 1
    return bullets, index


def _parse_progress_entries(text: str) -> list[ProgressEntry]:
    lines = text.splitlines()
    entries: list[ProgressEntry] = []
    index = 0
    while index < len(lines):
        match = _PROGRESS_HEADING_RE.match(lines[index])
        if match is None:
            index += 1
            continue
        session_id = parse_session_id(match.group(1))
        status = parse_status_value(match.group(2))
        completed_at = match.group(3)
        index += 1

        files_modified: tuple[str, ...] = ()
        key_decisions: str | None = None
        discovered_facts: list[str] = []
        open_questions: list[str] = []
        blockers: str | None = None

        while index < len(lines):
            line = lines[index]
            if line.startswith("## "):
                break
            if line.startswith(_PROGRESS_FILES_PREFIX):
                value = line[len(_PROGRESS_FILES_PREFIX) :].strip()
                files_modified = _parse_progress_csv(value)
                index += 1
            elif line.startswith(_PROGRESS_DECISIONS_PREFIX):
                value = line[len(_PROGRESS_DECISIONS_PREFIX) :].strip()
                key_decisions = None if value in {"", "none"} else value
                index += 1
            elif line.startswith(_PROGRESS_FACTS_PREFIX):
                value = line[len(_PROGRESS_FACTS_PREFIX) :].strip()
                index += 1
                if value and value != "none":
                    discovered_facts.append(value)
                else:
                    bullets, index = _consume_indented_bullets(lines, index)
                    discovered_facts.extend(bullets)
            elif line.startswith(_PROGRESS_QUESTIONS_PREFIX):
                value = line[len(_PROGRESS_QUESTIONS_PREFIX) :].strip()
                index += 1
                if value and value != "none":
                    open_questions.append(value)
                else:
                    bullets, index = _consume_indented_bullets(lines, index)
                    open_questions.extend(bullets)
            elif line.startswith(_PROGRESS_BLOCKERS_PREFIX):
                value = line[len(_PROGRESS_BLOCKERS_PREFIX) :].strip()
                blockers = None if value in {"", "none"} else value
                index += 1
            else:
                index += 1

        entries.append(
            ProgressEntry(
                session_id=session_id,
                status=status,
                completed_at=completed_at,
                files_modified=files_modified,
                key_decisions=key_decisions,
                discovered_facts=tuple(discovered_facts),
                open_questions=tuple(open_questions),
                blockers=blockers,
            )
        )
    return entries


def read_progress_entries(plan_dir: str | Path) -> tuple[ProgressEntry, ...]:
    """Read parsed progress entries from `<plan_dir>/progress.md`, or `()` if absent."""
    path = progress_path(plan_dir)
    if not _is_file(path):
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        _raise_read_error(path, error)
    _validate_progress_version(path, text)
    return tuple(_parse_progress_entries(text))


def append_progress_entry(plan_dir: str | Path, entry: ProgressEntry) -> Path:
    """Append `entry` to `<plan_dir>/progress.md`, adding a header if absent.

    Idempotent on retries: if the most recent entry already matches
    `(session_id, completed_at)`, the file is left untouched. Writes go
    through an atomic temp-file + `os.replace` so a crash mid-append never
    leaves a half-written file behind.
    """
    path = progress_path(plan_dir)
    rendered_entry = render_progress_entry(entry)

    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as error:
            _raise_read_error(path, error)
        _validate_progress_version(path, existing)
        existing_entries = _parse_progress_entries(existing)
        if existing_entries:
            last = existing_entries[-1]
            if (
                last.session_id == entry.session_id
                and last.completed_at == entry.completed_at
            ):
                return path
        new_content = existing.rstrip("\n") + "\n\n" + rendered_entry
    else:
        new_content = PROGRESS_HEADER + "\n" + rendered_entry

    _atomic_write_text(path, new_content)
    return path


def write_progress_journal(
    plan_dir: str | Path,
    entries: tuple[ProgressEntry, ...],
) -> Path:
    """Replace `<plan_dir>/progress.md` atomically with a full rendered journal."""
    path = progress_path(plan_dir)
    text = render_progress_journal(entries)
    _atomic_write_text(path, text)
    return path
