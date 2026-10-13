"""Runner-managed progress journal (`progress.md`) for a plan directory.

`progress.md` is part of the durable plan-directory surface: a
runner-managed, append-only journal of completed sessions seeded by
`session-split` and appended after each verified-complete session by the
runner. It is excluded from `plan_content_sha256` and
`stable_readiness_sha256` so journal writes never invalidate readiness or
the plan identity. Reads go through `read_progress_entries`; writes go
through the atomic `append_progress_entry` and `write_progress_journal`
helpers.
"""

from __future__ import annotations

import re
from pathlib import Path

from .plans_errors import (
    PlanError,
)
from .plans_files import (
    _atomic_write_text,
    _is_file,
    _raise_read_error,
    canonicalize_path,
)
from .plans_handoff import (
    parse_status_value,
)
from .plans_models import (
    ProgressEntry,
)
from .plans_session_ids import (
    SESSION_ID_PATTERN,
    parse_session_id,
)

PROGRESS_FILE_NAME = "progress.md"


PROGRESS_SCHEMA_VERSION = 1


PROGRESS_SCHEMA_COMMENT = "<!-- agent-session-loop progress-journal v1 -->"


PROGRESS_HEADER = (
    "# Progress Journal\n"
    f"{PROGRESS_SCHEMA_COMMENT}\n"
    "\n"
    "Auto-maintained by agent_session_loop. Append-only. Treat as the running\n"
    "summary of completed sessions; defer to session files for full detail.\n"
)


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
