"""Discovery of `session-<id>.md` files in a plan directory."""

from __future__ import annotations

from pathlib import Path

from .plans_files import (
    _raise_read_error,
    canonicalize_path,
)
from .plans_models import (
    SessionFile,
)
from .plans_session_ids import (
    SessionId,
    _canonical_session_id_for_loose_filename,
    session_id_from_filename,
)


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
