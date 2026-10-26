"""Repository registry manifest parsing and handoff path validation."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from .contract_models import fenced_line_flags
from .plans_errors import (
    PlanError,
)
from .plans_files import (
    _is_file,
    _raise_read_error,
    canonicalize_path,
)
from .plans_models import (
    SessionHandoff,
)
from .plans_session_scope import (
    QualifiedPath,
    SessionScope,
    parse_qualified_path,
)
from .plans_transaction import (
    recover_pending_plan_transaction,
)
from .workspaces import (
    Repository,
    RepositoryRegistry,
    WorkspaceError,
)

#: A cell boundary: a pipe that is not escaped as ``\|`` inside cell text.
_UNESCAPED_PIPE_RE = re.compile(r"(?<!\\)\|")


def escape_table_cell(value: str) -> str:
    r"""Escape ``|`` as ``\|`` so a value stays inside one Markdown table cell."""
    return value.replace("|", "\\|")


def _split_table_row(line: str) -> tuple[str, list[str]] | None:
    r"""Split a Markdown table row into its leading indent and raw cells.

    An escaped ``\|`` is cell text, not a boundary, and stays escaped in the
    returned cell so a row rejoined from these cells reads the same.
    """
    if not line.lstrip().startswith("|"):
        return None
    leading = line[: len(line) - len(line.lstrip())]
    body = line.strip().lstrip("|")
    while body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return leading, _UNESCAPED_PIPE_RE.split(body)


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
    fenced = fenced_line_flags(lines)
    headings = [
        index
        for index, line in enumerate(lines)
        if not fenced[index] and line == "## Repositories"
    ]
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
    except (OSError, UnicodeDecodeError) as error:
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
