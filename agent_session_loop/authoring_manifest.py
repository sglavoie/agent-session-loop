"""Manifest rendering and manifest/status table mutation helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import (
    plans_models,
    plans_registry,
    plans_status_tables,
    repository_scope,
)
from .authoring_render import previous_session_reference_line
from .plans_errors import PlanError
from .plans_models import ReadinessMetadata
from .plans_readiness import SPLIT_FROM_PREFIX
from .plans_session_ids import SessionId, parse_session_id
from .workspaces import (
    RepositoryRegistry,
)

# ---------------------------------------------------------------------------
# Manifest rendering and table updates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestSession:
    """One row in a manifest ``## Sessions`` table."""

    session_id: SessionId
    title: str
    status: plans_models.StatusValue = "pending"

    @property
    def filename(self) -> str:
        return self.session_id.filename


def render_manifest_session_row(session: ManifestSession) -> str:
    return (
        f"| {session.session_id.value} | {session.title} | "
        f"{session.status} | {session.filename} |"
    )


def render_manifest_sessions_table(sessions: tuple[ManifestSession, ...]) -> str:
    lines = [
        "| # | Title | Status | File |",
        "| --- | --- | --- | --- |",
    ]
    lines.extend(render_manifest_session_row(session) for session in sessions)
    return "\n".join(lines) + "\n"


def render_repository_registry_table(registry: RepositoryRegistry) -> str:
    """Render the authoritative ordered repository registry as Markdown."""
    return "\n".join(repository_scope.registry_table_lines(registry)) + "\n"


@dataclass(frozen=True)
class ManifestSpec:
    """Inputs for deterministic ``manifest.md`` rendering."""

    plan_title: str
    source_path: str
    output_dir: str
    plan_id: str
    source_fingerprint: str
    content_sha256: str
    sessions: tuple[ManifestSession, ...]
    split_date: str
    readiness: ReadinessMetadata
    skill_prefix: str = "/"
    execution_note: str | None = None
    registry: RepositoryRegistry | None = None


def render_manifest(spec: ManifestSpec) -> str:
    """Render a serial-only ``manifest.md`` including readiness metadata lines."""
    prefix = spec.skill_prefix
    run_command = f"{prefix}session-run {spec.output_dir}/"
    lines: list[str] = [
        f"# {spec.plan_title}",
        "",
        f"{SPLIT_FROM_PREFIX} `{spec.source_path}`",
        f"**Output directory**: `{spec.output_dir}`",
        f"**Plan ID**: `{spec.plan_id}`",
        f"**Source fingerprint**: `{spec.source_fingerprint}`",
        f"**Content SHA-256**: `{spec.content_sha256}`",
        f"**Sessions**: {len(spec.sessions)}",
        f"**Split date**: {spec.split_date}",
        f"**Readiness**: {spec.readiness.state}",
        f"**Readiness SHA-256**: `{spec.readiness.readiness_sha256}`",
        f"**Readiness checked at**: {spec.readiness.checked_at}",
    ]
    if spec.registry is not None:
        lines.extend(
            [
                "",
                "## Repositories",
                "",
                render_repository_registry_table(spec.registry).rstrip("\n"),
            ]
        )
    lines.extend(
        [
            "",
            "## Sessions",
            "",
            render_manifest_sessions_table(spec.sessions).rstrip("\n"),
            "",
            "## Execution",
            "",
            (
                f"Run `{prefix}session-run` once per session, preferably in a fresh "
                "harness conversation for each:"
            ),
            "",
            "```text",
            run_command,
            "```",
            "",
            f"Example rendered command: `{run_command}`",
            "",
            (
                f"`{prefix}session-run` auto-advances through pending sessions in "
                "dotted numeric order. Session handoff statuses are authoritative; "
                "this manifest table is derived bookkeeping."
            ),
        ]
    )
    if spec.execution_note:
        lines.extend(["", spec.execution_note.rstrip("\n")])
    return "\n".join(lines) + "\n"


def replace_manifest_sessions_count(manifest_text: str, total_sessions: int) -> str:
    """Rewrite the manifest's ``**Sessions**: N`` metadata line, if present."""
    return re.sub(
        r"(?m)^(\*\*Sessions\*\*:[ \t]*)\d+[ \t]*$",
        rf"\g<1>{total_sessions}",
        manifest_text,
    )


def _find_header(headers: list[str], names: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in names:
            return index
    return None


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _replace_status_table_rows(
    block: list[str],
    session_id: SessionId,
    replacements: tuple[ManifestSession, ...],
) -> tuple[list[str], bool]:
    parsed_header = plans_registry._split_table_row(block[0])
    assert parsed_header is not None
    headers = [
        plans_registry._normalize_table_header(cell) for cell in parsed_header[1]
    ]
    session_index = _find_header(
        headers, plans_status_tables.SESSION_STATUS_TABLE_HEADERS
    )
    status_index = _find_header(headers, {"status"})
    title_index = _find_header(headers, {"title"})
    file_index = _find_header(headers, {"file"})
    if session_index is None or status_index is None:
        return block, False

    changed = False
    updated = block[:2]
    for row in block[2:]:
        parsed = plans_registry._split_table_row(row)
        if parsed is None:
            updated.append(row)
            continue
        leading, cells = parsed
        if max(session_index, status_index) >= len(cells):
            updated.append(row)
            continue
        if (
            plans_status_tables._session_id_from_cell(cells[session_index])
            != session_id
        ):
            updated.append(row)
            continue

        for replacement in replacements:
            new_cells = list(cells)
            new_cells[session_index] = _replace_cell_content(
                new_cells[session_index], replacement.session_id.value
            )
            new_cells[status_index] = _replace_cell_content(
                new_cells[status_index], replacement.status
            )
            if title_index is not None and title_index < len(new_cells):
                new_cells[title_index] = _replace_cell_content(
                    new_cells[title_index], replacement.title
                )
            if file_index is not None and file_index < len(new_cells):
                new_cells[file_index] = _replace_cell_content(
                    new_cells[file_index], replacement.filename
                )
            updated.append(_join_table_row(leading, new_cells))
        changed = True
    return updated, changed


def replace_session_status_rows(
    text: str,
    session_id: int | str | SessionId,
    replacements: tuple[ManifestSession, ...] | list[ManifestSession],
) -> tuple[str, bool]:
    """Replace one session's row in every recognized session-status table.

    Each matching row is replaced with one row per ``replacements`` entry.
    Unrelated cells keep their padding; only the session-id, status, and (when
    present) title/file columns are rewritten. Tables without both a session-id
    column and a ``Status`` column are left untouched, as are legacy
    dependency-wave and ownership tables.
    """
    canonical_id = parse_session_id(session_id)
    replacement_rows = tuple(replacements)
    lines = text.splitlines()
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
        parsed_header = (
            plans_registry._split_table_row(block[0]) if len(block) >= 2 else None
        )
        parsed_separator = (
            plans_registry._split_table_row(block[1]) if len(block) >= 2 else None
        )
        if (
            parsed_header is None
            or parsed_separator is None
            or not plans_registry._is_separator_row(parsed_separator[1])
        ):
            updated.extend(block)
            continue

        new_block, block_changed = _replace_status_table_rows(
            block, canonical_id, replacement_rows
        )
        updated.extend(new_block)
        changed = changed or block_changed

    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(updated) + suffix, changed


def replace_previous_session_reference(
    text: str,
    old_session_id: int | str | SessionId,
    new_session_id: int | str | SessionId,
) -> tuple[str, bool]:
    """Repoint a ``> **Previous session**`` reference line at a new session file."""
    old_line = previous_session_reference_line(old_session_id)
    new_line = previous_session_reference_line(new_session_id)
    pattern = re.compile(rf"(?m)^{re.escape(old_line)}$")
    updated = pattern.sub(lambda _match: new_line, text)
    return updated, updated != text


def add_manifest_session_row(manifest_text: str, session: ManifestSession) -> str:
    """Append a session row to the manifest's ``## Sessions`` status table.

    Reuses :mod:`plans_status_tables` table primitives so the inserted row matches the
    existing header/separator alignment. Raises :class:`PlanError` when no
    session-status table is present.
    """
    lines = manifest_text.splitlines(keepends=False)
    index = 0
    while index < len(lines):
        if not lines[index].lstrip().startswith("|"):
            index += 1
            continue
        block_start = index
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            index += 1
        block = lines[block_start:index]
        if _is_session_status_table(block):
            new_row = render_manifest_session_row(session)
            leading = block[0][: len(block[0]) - len(block[0].lstrip())]
            updated = lines[:index] + [f"{leading}{new_row.lstrip()}"] + lines[index:]
            suffix = "\n" if manifest_text.endswith("\n") else ""
            return "\n".join(updated) + suffix

    raise PlanError("manifest has no session-status table to update")


def _is_session_status_table(block: list[str]) -> bool:
    if len(block) < 2:
        return False
    header = plans_registry._split_table_row(block[0])
    separator = plans_registry._split_table_row(block[1])
    if header is None or separator is None:
        return False
    if not plans_registry._is_separator_row(separator[1]):
        return False
    headers = [plans_registry._normalize_table_header(cell) for cell in header[1]]
    if "status" not in headers:
        return False
    return any(
        name in plans_status_tables.SESSION_STATUS_TABLE_HEADERS for name in headers
    )
