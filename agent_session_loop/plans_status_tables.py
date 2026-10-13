"""Derived status-table synchronization in `manifest.md` and `context.md`."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from .plans_files import (
    _raise_read_error,
    _write_text,
)
from .plans_models import (
    PlanSnapshot,
    StatusValue,
)
from .plans_registry import (
    _is_separator_row,
    _normalize_table_header,
    _split_table_row,
)
from .plans_session_ids import (
    SESSION_ID_PATTERN,
    SessionId,
    parse_session_id,
)

SESSION_STATUS_TABLE_HEADERS = {
    "#",
    "session",
    "session #",
    "session number",
    "session id",
    "id",
}


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


def _replace_cell_content(cell: str, value: str) -> str:
    match = re.fullmatch(r"(\s*)(.*?)(\s*)", cell)
    if match is None:
        return value
    return f"{match.group(1)}{value}{match.group(3)}"


def _join_table_row(leading: str, cells: list[str]) -> str:
    return f"{leading}|{'|'.join(cells)}|"


def _sync_status_table_block(
    block: list[str],
    status_by_session: Mapping[str, StatusValue],
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
        # `status_by_id` keys on `SessionFile.id`, which *is* `SessionId.value`,
        # so the canonical dotted spelling is the only lookup that can ever hit.
        status = status_by_session.get(session_id.value)
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
    status_by_session: Mapping[str, StatusValue],
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
