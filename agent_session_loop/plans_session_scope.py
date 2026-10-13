"""Session repository scope metadata and qualified-path parsing."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from .plans_errors import (
    PlanError,
)
from .plans_models import (
    PlanSnapshot,
)
from .workspaces import (
    RepositoryRegistry,
    WorkspaceError,
    validate_repository_alias,
)

READS_REPOSITORIES_PREFIX = "**Reads repositories**:"


WRITES_REPOSITORIES_PREFIX = "**Writes repositories**:"


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


def _next_session_scope(
    snapshot: PlanSnapshot, registry: RepositoryRegistry
) -> SessionScope | None:
    session = snapshot.next_pending
    if session is None:
        return None
    return parse_session_scope(
        session.path.read_text(encoding="utf-8"), registry.aliases
    )


def needed_repository_aliases(
    snapshot: PlanSnapshot, registry: RepositoryRegistry
) -> frozenset[str]:
    """Fail closed on the next session's scope; no pending session needs no tree."""
    scope = _next_session_scope(snapshot, registry)
    if scope is None:
        return frozenset()
    return frozenset((*scope.reads, *scope.writes))


def commit_repository_aliases(
    snapshot: PlanSnapshot, registry: RepositoryRegistry
) -> frozenset[str]:
    """The next session's write aliases: the only trees its commit can stage.

    A per-session commit stages write-guard evidence for declared write aliases
    only, so a read-only tree's pre-existing dirt can never enter it, and the
    write guard compares turn-start and turn-end snapshots rather than assuming a
    clean start. Only these trees must therefore be clean before the turn.
    """
    scope = _next_session_scope(snapshot, registry)
    if scope is None:
        return frozenset()
    return frozenset(scope.writes)


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
