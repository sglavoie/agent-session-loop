"""Fence-aware extraction of completion-contract Markdown sections."""

from __future__ import annotations

from .contract_models import _FENCE_RE, COMPLETION_CONTRACT_HEADING
from .plans_errors import PlanError
from .plans_handoff import HANDOFF_HEADING
from .plans_session_ids import SessionId


def _final_handoff_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return index
    return None


def _heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    level = 0
    while level < len(stripped) and stripped[level] == "#":
        level += 1
    if level == 0 or level == len(stripped) or stripped[level] != " ":
        return 0
    return level


def _find_heading_index(
    lines: list[str], heading: str, start: int, end: int
) -> int | None:
    for index in range(start, end):
        if lines[index].strip() == heading:
            return index
    return None


def fenced_line_flags(lines: list[str]) -> list[bool]:
    """Return, per line, whether it belongs to a fenced code block.

    A fence opens on a run of at least three backticks or tildes and closes only
    on a run of the same character, at least as long as the opener, followed by
    nothing but whitespace. A shorter run, a different character, or an info
    string is content, so a nested example never closes the block quoting it.
    """
    flags = [False] * len(lines)
    fence_char: str | None = None
    fence_length = 0
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence_char is None:
            if match is not None:
                run = match.group("run")
                fence_char = run[0]
                fence_length = len(run)
                flags[index] = True
            continue
        flags[index] = True
        if match is None:
            continue
        run = match.group("run")
        if (
            run[0] == fence_char
            and len(run) >= fence_length
            and match.group("rest").strip() == ""
        ):
            fence_char = None
            fence_length = 0
    return flags


def completion_contract_heading_indices(
    lines: list[str], end: int | None = None
) -> list[int]:
    """Return the indices of ``## Completion Contract`` headings in ``lines``.

    Headings inside fenced code blocks are quoted text, not sections, and are
    never reported.
    """
    limit = len(lines) if end is None else end
    fenced = fenced_line_flags(lines)
    return [
        index
        for index in range(limit)
        if not fenced[index] and lines[index].strip() == COMPLETION_CONTRACT_HEADING
    ]


def strip_completion_contract_sections(lines: list[str]) -> tuple[list[str], int]:
    """Return ``lines`` minus every Completion Contract section, and the count cut."""
    starts = completion_contract_heading_indices(lines)
    if not starts:
        return list(lines), 0
    fenced = fenced_line_flags(lines)
    keep = [True] * len(lines)
    for start in starts:
        stop = _section_end(lines, start, max_level=2, end=len(lines), fenced=fenced)
        for index in range(start, stop):
            keep[index] = False
    return [line for index, line in enumerate(lines) if keep[index]], len(starts)


def _section_end(
    lines: list[str],
    start: int,
    max_level: int,
    end: int,
    fenced: list[bool] | None = None,
) -> int:
    flags = fenced_line_flags(lines) if fenced is None else fenced
    for index in range(start + 1, end):
        if flags[index]:
            continue
        level = _heading_level(lines[index])
        if 0 < level <= max_level:
            return index
    return end


def _extract_bullets(lines: list[str]) -> list[str]:
    bullets: list[str] = []
    current: list[str] | None = None
    for raw_line in lines:
        stripped = raw_line.lstrip()
        leading = raw_line[: len(raw_line) - len(stripped)]
        if stripped.startswith(("- ", "* ", "+ ")) and len(leading) == 0:
            if current is not None:
                bullets.append(" ".join(current).strip())
            current = [stripped[2:].strip()]
        elif current is not None and stripped == "":
            bullets.append(" ".join(current).strip())
            current = None
        elif current is not None and leading.startswith((" ", "\t")):
            current.append(stripped)
        elif current is not None:
            bullets.append(" ".join(current).strip())
            current = None
    if current is not None:
        bullets.append(" ".join(current).strip())
    return [bullet for bullet in bullets if bullet]


def _extract_json_block(lines: list[str], session_id: SessionId) -> str:
    fence_start: int | None = None
    fence_language: str | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        if fence_start is None:
            fence_language = stripped[3:].strip().lower()
            fence_start = index + 1
            continue
        if fence_language not in {"", "json"}:
            raise PlanError(
                f"session {session_id.value} Verify block must be a ```json``` "
                f"fenced code block; found language {fence_language!r}"
            )
        return "\n".join(lines[fence_start:index])

    raise PlanError(
        f"session {session_id.value} Verify block is missing a ```json``` "
        "fenced code block"
    )
