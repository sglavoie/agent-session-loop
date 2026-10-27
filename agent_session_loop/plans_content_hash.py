"""Content hashes and per-artifact digests that define plan identity."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .contract_models import fenced_line_flags
from .plans_discovery import (
    discover_session_files,
)
from .plans_files import (
    _is_file,
    _raise_read_error,
    canonicalize_path,
)
from .plans_handoff import (
    _final_handoff_start,
)
from .plans_models import (
    SessionFile,
)
from .plans_readiness import (
    _is_readiness_line,
)
from .plans_registry import (
    _is_separator_row,
    _normalize_table_header,
    _split_table_row,
)
from .plans_session_ids import (
    SESSION_FILE_RE,
)
from .plans_status_tables import (
    SESSION_STATUS_TABLE_HEADERS,
    _join_table_row,
    _replace_cell_content,
    _session_id_from_cell,
)

UNREADABLE_ARTIFACT_DIGEST = "unreadable"


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
    # A table quoted inside a fenced code block is an example, not a ledger.
    fenced = fenced_line_flags(lines)
    out: list[str] = []
    index = 0

    while index < len(lines):
        if fenced[index] or not lines[index].lstrip().startswith("|"):
            out.append(lines[index])
            index += 1
            continue

        block_start = index
        while (
            index < len(lines)
            and not fenced[index]
            and lines[index].lstrip().startswith("|")
        ):
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
