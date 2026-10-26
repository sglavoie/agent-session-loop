"""Fenced-code-aware source plan parsing and repository registry extraction."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from . import (
    plans_registry,
    plans_session_scope,
)
from .authoring_source_models import (
    _ATX_CLOSING_MARKER_RE,
    _REPOSITORIES_HEADING,
    _REPOSITORIES_HEADING_CANDIDATE_RE,
    _REPOSITORY_HEADERS,
    _SESSION_HEADING_CANDIDATE_RE,
    _SESSION_HEADING_PREFIX_RE,
    _SOURCE_SESSION_ID_RE,
    _TITLE_SEPARATORS,
    LINT_DANGLING_TITLE_SEPARATOR,
    LINT_DUPLICATE_REPOSITORIES,
    LINT_DUPLICATE_SESSION_ID,
    LINT_INVALID_REPOSITORY,
    LINT_INVALID_SESSION_ID,
    LINT_INVALID_SESSION_SCOPE,
    LINT_MALFORMED_HEADING,
    LINT_MALFORMED_REPOSITORIES,
    LINT_MALFORMED_TITLE,
    LINT_MISSING_ID_WHITESPACE,
    LINT_MISSING_SESSION_ID,
    LINT_NON_NUMERIC_SESSION_ID,
    LINT_PUNCTUATION_FREE_TITLE,
    LINT_REPOSITORIES_PLACEMENT,
    LINT_SESSION_ID_SUFFIX,
    LINT_UNSUPPORTED_TITLE_SEPARATOR,
    LINT_ZERO_ID_COMPONENT,
    SESSION_HEADING_RE,
    ParsedPlan,
    ParsedSession,
    SourceHeadingError,
    SourceRegistryError,
    SourceScopeError,
)
from .contract_models import fenced_line_flags
from .plans_session_ids import SessionId
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
    canonicalize_declared_origin,
)


def _source_heading_error(
    line_number: int, reason: str, line: str, *, category: str
) -> SourceHeadingError:
    """Build a line-specific malformed source-heading error."""
    return SourceHeadingError(
        line_number=line_number, category=category, source_line=line, reason=reason
    )


def _canonicalize_source_session_id(
    raw_id: str, *, line_number: int, line: str
) -> SessionId:
    """Canonicalize a source-only, optionally padded dotted session ID."""
    if _SOURCE_SESSION_ID_RE.fullmatch(raw_id) is None:
        raise _source_heading_error(
            line_number,
            f"invalid session id {raw_id!r}; expected dotted decimal components",
            line,
            category=LINT_INVALID_SESSION_ID,
        )

    components = tuple(int(component) for component in raw_id.split("."))
    if any(component == 0 for component in components):
        raise _source_heading_error(
            line_number,
            f"zero-valued session id component in {raw_id!r}; components must be "
            f"positive",
            line,
            category=LINT_ZERO_ID_COMPONENT,
        )
    return SessionId(components)


def _raise_malformed_session_heading(line: str, line_number: int) -> None:
    """Raise the most specific diagnostic for a detected malformed candidate."""
    without_closing_marker = _ATX_CLOSING_MARKER_RE.sub("", line)
    prefix = _SESSION_HEADING_PREFIX_RE.match(without_closing_marker)
    if prefix is None:  # Defensive: callers first use the candidate detector.
        raise _source_heading_error(
            line_number,
            "malformed session heading",
            line,
            category=LINT_MALFORMED_HEADING,
        )

    after_token = prefix.group("rest")
    if not after_token or after_token[0] not in " \t":
        raise _source_heading_error(
            line_number,
            "invalid session id; expected whitespace and a positive dotted decimal id",
            line,
            category=LINT_MISSING_ID_WHITESPACE,
        )

    payload = after_token.lstrip(" \t")
    if not payload:
        raise _source_heading_error(
            line_number,
            "invalid session id; session id is missing",
            line,
            category=LINT_MISSING_SESSION_ID,
        )

    numeric_prefix = re.match(r"[0-9.]+", payload)
    if numeric_prefix is None:
        raw_id = re.split(r"[ \t:–—-]", payload, maxsplit=1)[0]
        raise _source_heading_error(
            line_number,
            f"invalid session id {raw_id!r}; expected dotted decimal components",
            line,
            category=LINT_NON_NUMERIC_SESSION_ID,
        )

    raw_id = numeric_prefix.group(0)
    suffix = payload[numeric_prefix.end() :]
    if suffix and suffix[0] not in " \t" and suffix[0].isalnum():
        malformed_token = re.split(r"[ \t:–—-]", payload, maxsplit=1)[0]
        raise _source_heading_error(
            line_number,
            f"invalid session id {malformed_token!r}; expected dotted decimal "
            f"components",
            line,
            category=LINT_SESSION_ID_SUFFIX,
        )

    _canonicalize_source_session_id(raw_id, line_number=line_number, line=line)
    suffix = suffix.lstrip(" \t")
    if not suffix:
        # A valid bare ID would have matched. This path is only reachable for
        # defensive completeness if the valid matcher and diagnostic parser
        # diverge.
        raise _source_heading_error(
            line_number,
            "malformed session heading after session id",
            line,
            category=LINT_MALFORMED_HEADING,
        )

    separator = suffix[0]
    if separator in _TITLE_SEPARATORS:
        if not suffix[1:].strip(" \t"):
            raise _source_heading_error(
                line_number,
                f"dangling title separator {separator!r}; a non-empty title is "
                f"required",
                line,
                category=LINT_DANGLING_TITLE_SEPARATOR,
            )
        raise _source_heading_error(
            line_number,
            "malformed session title",
            line,
            category=LINT_MALFORMED_TITLE,
        )

    if separator.isalnum():
        raise _source_heading_error(
            line_number,
            "punctuation-free title suffix; use ':', '-', '–', or '—' before the title",
            line,
            category=LINT_PUNCTUATION_FREE_TITLE,
        )
    raise _source_heading_error(
        line_number,
        f"unsupported title separator {separator!r}; use ':', '-', '–', or '—'",
        line,
        category=LINT_UNSUPPORTED_TITLE_SEPARATOR,
    )


def _parse_source_session_heading(
    line: str, line_number: int
) -> tuple[SessionId, str | None, int] | None:
    """Return canonical ID, title, and level for a source session heading."""
    if _SESSION_HEADING_CANDIDATE_RE.match(line) is None:
        return None

    without_closing_marker = _ATX_CLOSING_MARKER_RE.sub("", line)
    heading = SESSION_HEADING_RE.fullmatch(without_closing_marker)
    if heading is None:
        _raise_malformed_session_heading(line, line_number)
        raise AssertionError("malformed session heading did not raise")

    raw_id = heading.group("session_id")
    session_id = _canonicalize_source_session_id(
        raw_id, line_number=line_number, line=line
    )
    separator = heading.group("separator")
    raw_title = heading.group("title")
    if separator is not None and not (raw_title or "").strip(" \t"):
        raise _source_heading_error(
            line_number,
            f"dangling title separator {separator!r}; a non-empty title is required",
            line,
            category=LINT_DANGLING_TITLE_SEPARATOR,
        )
    title = (raw_title or "").strip(" \t") or None
    return session_id, title, len(heading.group("hashes"))


def _iter_structural_lines(
    lines: Sequence[str],
) -> Iterator[tuple[int, str, bool, bool]]:
    """Yield source lines with fence-aware structural-candidate flags.

    Sole owner of fenced-code bookkeeping for source plans, so the fail-fast
    parser and the accumulating linter can never disagree about which lines are
    structure and which are content.
    """
    fenced = fenced_line_flags(lines)
    for index, line in enumerate(lines):
        is_candidate = (
            not fenced[index] and _SESSION_HEADING_CANDIDATE_RE.match(line) is not None
        )
        is_repositories = (
            not fenced[index]
            and _REPOSITORIES_HEADING_CANDIDATE_RE.fullmatch(line) is not None
        )
        yield index + 1, line, is_candidate, is_repositories


def _source_registry_error(
    line_number: int, reason: str, line: str, *, category: str
) -> SourceRegistryError:
    return SourceRegistryError(
        line_number=line_number,
        category=category,
        source_line=line,
        reason=reason,
    )


def _redact_repository_row(_line: str) -> str:
    """Return an opaque diagnostic marker for a repository table line.

    Malformed tables cannot establish which cell is the Origin column, so no
    cell is safe to echo: credential-bearing URLs may appear in the header,
    separator, alias, or path position.  Keep the parameter to make every
    call site explicitly identify the source line being redacted.
    """
    return "[repository row redacted]"


def _parse_repository_registry(
    lines: Sequence[str],
    structural_lines: Sequence[tuple[int, str, bool, bool]],
) -> tuple[RepositoryRegistry | None, frozenset[int]]:
    """Parse and locate the optional source registry.

    Returned indices identify only the authoritative registry block. Callers
    remove those lines from shared context while preserving every other source
    line verbatim.
    """
    first_session_line = next(
        (line_number for line_number, _, candidate, _ in structural_lines if candidate),
        None,
    )
    headings = [
        (line_number, line)
        for line_number, line, _, candidate in structural_lines
        if candidate
    ]
    if not headings:
        return None, frozenset()

    first_heading_line, first_heading = headings[0]
    if first_session_line is not None and first_heading_line > first_session_line:
        raise _source_registry_error(
            first_heading_line,
            "the top-level ## Repositories table must appear before the first session",
            first_heading,
            category=LINT_REPOSITORIES_PLACEMENT,
        )
    if first_heading != _REPOSITORIES_HEADING:
        raise _source_registry_error(
            first_heading_line,
            "repository registry heading must be exactly '## Repositories'",
            first_heading,
            category=LINT_REPOSITORIES_PLACEMENT,
        )
    if len(headings) > 1:
        duplicate_line, duplicate_heading = headings[1]
        raise _source_registry_error(
            duplicate_line,
            f"duplicate repository registry; first defined on line "
            f"{first_heading_line}",
            duplicate_heading,
            category=LINT_DUPLICATE_REPOSITORIES,
        )

    heading_index = first_heading_line - 1
    table_index = heading_index + 1
    while table_index < len(lines) and not lines[table_index].strip():
        table_index += 1
    if table_index >= len(lines):
        raise _source_registry_error(
            first_heading_line,
            "repository registry table is missing",
            first_heading,
            category=LINT_MALFORMED_REPOSITORIES,
        )

    header = plans_registry._split_table_row(lines[table_index])
    if (
        header is None
        or tuple(cell.strip() for cell in header[1]) != _REPOSITORY_HEADERS
    ):
        raise _source_registry_error(
            table_index + 1,
            "repository registry columns must be exactly Alias, Path, and Origin",
            _redact_repository_row(lines[table_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )

    separator_index = table_index + 1
    if separator_index >= len(lines):
        raise _source_registry_error(
            table_index + 1,
            "repository registry separator row is missing",
            _redact_repository_row(lines[table_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )
    separator = plans_registry._split_table_row(lines[separator_index])
    if (
        separator is None
        or len(separator[1]) != len(_REPOSITORY_HEADERS)
        or not plans_registry._is_separator_row(separator[1])
    ):
        raise _source_registry_error(
            separator_index + 1,
            "repository registry requires a three-column Markdown separator row",
            _redact_repository_row(lines[separator_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )

    repositories: list[Repository] = []
    row_index = separator_index + 1
    while row_index < len(lines):
        parsed_row = plans_registry._split_table_row(lines[row_index])
        if parsed_row is None:
            break
        safe_source_line = _redact_repository_row(lines[row_index])
        cells = tuple(cell.strip() for cell in parsed_row[1])
        if len(cells) != len(_REPOSITORY_HEADERS):
            raise _source_registry_error(
                row_index + 1,
                "repository registry rows must contain exactly three cells",
                safe_source_line,
                category=LINT_MALFORMED_REPOSITORIES,
            )
        if any(not cell for cell in cells):
            raise _source_registry_error(
                row_index + 1,
                "repository registry Alias, Path, and Origin values must not be blank",
                safe_source_line,
                category=LINT_INVALID_REPOSITORY,
            )
        try:
            origin = canonicalize_declared_origin(cells[2])
            repository = Repository(alias=cells[0], path=cells[1], origin=origin)
            RepositoryRegistry(tuple((*repositories, repository)))
        except WorkspaceError as error:
            raise _source_registry_error(
                row_index + 1,
                str(error),
                safe_source_line,
                category=LINT_INVALID_REPOSITORY,
            ) from error
        repositories.append(repository)
        row_index += 1

    if not repositories:
        raise _source_registry_error(
            separator_index + 1,
            "repository registry must contain at least one repository row",
            _redact_repository_row(lines[separator_index]),
            category=LINT_MALFORMED_REPOSITORIES,
        )

    registry = RepositoryRegistry(tuple(repositories))
    excluded = frozenset(range(heading_index, row_index))
    return registry, excluded


def parse_plan_document(text: str) -> ParsedPlan:
    """Split a plan into shared context and per-session blocks (fence-aware).

    Case-insensitive H2--H6 ATX ``Session`` headings found inside a fenced code
    block are treated as content, never as structure. Malformed candidates and
    duplicate canonical session IDs raise :class:`PlanError` with source-line
    diagnostics.
    """
    lines = text.splitlines()
    context: list[str] = []
    sessions: list[ParsedSession] = []
    structural_lines = tuple(_iter_structural_lines(lines))
    registry, registry_line_indices = _parse_repository_registry(
        lines, structural_lines
    )

    current: dict[str, Any] | None = None
    first_id_lines: dict[str, int] = {}

    def _flush(end: int) -> None:
        if current is None:
            return
        body = lines[current["body_start"] : end]
        scope: plans_session_scope.SessionScope | None = None
        if registry is not None:
            try:
                scope = plans_session_scope.parse_session_scope_lines(
                    body,
                    registry.aliases,
                    start_line=current["body_start"] + 1,
                )
            except plans_session_scope.SessionScopeError as error:
                raise SourceScopeError(
                    line_number=error.line_number,
                    category=LINT_INVALID_SESSION_SCOPE,
                    source_line=error.source_line,
                    reason=error.reason,
                ) from error
        sessions.append(
            ParsedSession(
                session_id=current["session_id"],
                title=current["title"],
                heading_level=current["heading_level"],
                lines=tuple(body),
                scope=scope,
            )
        )

    for line_number, line, is_candidate, _ in structural_lines:
        index = line_number - 1
        heading = (
            _parse_source_session_heading(line, line_number) if is_candidate else None
        )
        if heading is None:
            if current is None and index not in registry_line_indices:
                context.append(line)
            continue

        session_id, title, heading_level = heading
        first_line = first_id_lines.get(session_id.value)
        if first_line is not None:
            raise _source_heading_error(
                line_number,
                f"duplicate canonical session id {session_id.value!r}; "
                f"first defined on line {first_line}",
                line,
                category=LINT_DUPLICATE_SESSION_ID,
            )
        first_id_lines[session_id.value] = line_number

        _flush(index)
        current = {
            "session_id": session_id,
            "title": title,
            "heading_level": heading_level,
            "body_start": line_number,
        }

    _flush(len(lines))

    return ParsedPlan(
        context_lines=tuple(context),
        sessions=tuple(sessions),
        registry=registry,
    )
