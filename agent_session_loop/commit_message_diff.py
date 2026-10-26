"""Commit message diff."""

from __future__ import annotations

import re
from collections.abc import Sequence

from . import commit_message_models as models

# ---------------------------------------------------------------------------
# Diff parsing & truncation (ported from the retired gitsummary-prepare.py)
# ---------------------------------------------------------------------------


# git's C-quoting escapes (``quote_c_style``); anything else is a three-digit
# octal byte, which is how a non-ASCII name arrives unless ``core.quotePath``
# is off.
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    "\\": 0x5C,
    '"': 0x22,
}


_OCTAL_DIGITS = frozenset("01234567")


# The one-letter side prefix: the ``a/``/``b/`` a capture asks for, or the
# ``c/``/``i/``/``w/`` a mnemonic prefix writes into a diff handed in as text.
_SIDE_PREFIX = re.compile(r"^\w/")


_UNQUOTED_HEADER_SPLIT = re.compile(r"^(\w/.+?) (\w/.+)$")


def _read_c_quoted(text: str, start: int) -> tuple[str, int] | None:
    """Decode the C-quoted name opening at ``text[start]``.

    Returns the name and the index just past its closing quote, or ``None``
    when no well-formed quoted name starts there.
    """
    if text[start : start + 1] != '"':
        return None
    decoded = bytearray()
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == '"':
            return decoded.decode("utf-8", errors="replace"), index + 1
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogateescape"))
            index += 1
            continue
        escape = text[index + 1 : index + 2]
        if escape in _C_ESCAPES:
            decoded.append(_C_ESCAPES[escape])
            index += 2
            continue
        octal = text[index + 1 : index + 4]
        if len(octal) == 3 and set(octal) <= _OCTAL_DIGITS:
            decoded.append(int(octal, 8) & 0xFF)
            index += 4
            continue
        return None
    return None


def _strip_side_prefix(name: str) -> str:
    return _SIDE_PREFIX.sub("", name, count=1)


def _whole_name(text: str) -> str | None:
    """``text`` as one name: decoded when it is quoted, verbatim otherwise."""
    if not text.startswith('"'):
        return text or None
    parsed = _read_c_quoted(text, 0)
    if parsed is None or parsed[1] != len(text):
        return None
    return parsed[0]


def _header_names(rest: str) -> tuple[str, str] | None:
    """Split what follows ``diff --git`` into its two prefixed names.

    Either side is C-quoted when its name holds a character git escapes, and
    the two sides quote independently. Two unquoted names cannot be split on
    a space alone (``a/x y b/x y``), so the split where both sides name the
    same path wins; a rename falls back to the first `` <letter>/`` boundary.
    """
    if rest.startswith('"'):
        first = _read_c_quoted(rest, 0)
        if first is None or rest[first[1] : first[1] + 1] != " ":
            return None
        second = _whole_name(rest[first[1] + 1 :])
        return None if second is None else (first[0], second)
    if rest.endswith('"'):
        for match in re.finditer(' "', rest):
            second = _whole_name(rest[match.start() + 1 :])
            if second is not None:
                return rest[: match.start()], second
    for match in re.finditer(" ", rest):
        old, new = rest[: match.start()], rest[match.end() :]
        if _strip_side_prefix(old) == _strip_side_prefix(new):
            return old, new
    split = _UNQUOTED_HEADER_SPLIT.match(rest)
    return (split.group(1), split.group(2)) if split else None


def _plus_path(content: str) -> str | None:
    """The new-side name a section's ``+++`` line carries, if any.

    Only the extended header before the first hunk is searched, so an added
    line that happens to read ``++ x`` is never mistaken for one.
    """
    hunk = models._HUNK_PATTERN.search(content)
    header = content[: hunk.start()] if hunk else content
    match = models._PLUS_PATH_PATTERN.search(header)
    if match is None:
        return None
    # git appends a tab to a name holding a space; an unquoted name never
    # holds a tab of its own, since git would have quoted it.
    name = _whole_name(match.group(1).rstrip("\t"))
    if name is None or name == "/dev/null":
        return None
    return _strip_side_prefix(name)


def _section_path(header_rest: str, content: str) -> str:
    plus = _plus_path(content)
    if plus is not None:
        return plus
    names = _header_names(header_rest)
    if names is None:
        return header_rest
    return _strip_side_prefix(names[1])


def _parse_diff_into_files(diff: str) -> list[models.FileDiff]:
    if not diff.strip():
        return []

    # git output is decoded with ``surrogateescape``, so a name or a line that
    # is not UTF-8 arrives as lone surrogates no size measurement can encode.
    diff = diff.encode("utf-8", errors="surrogateescape").decode(
        "utf-8", errors="replace"
    )
    diff = diff.replace("\r\n", "\n").replace("\r", "\n")

    matches = list(models._FILE_HEADER_PATTERN.finditer(diff))
    if not matches:
        return []

    files: list[models.FileDiff] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        content = diff[start:end]

        path = _section_path(match.group(1), content)

        is_binary = bool(
            models._BINARY_FILES_PATTERN.search(content)
            or models._GIT_BINARY_PATCH_PATTERN.search(content)
        )

        additions = 0
        deletions = 0
        if not is_binary:
            for line in content.split("\n"):
                if line.startswith("+") and not line.startswith("+++"):
                    additions += 1
                elif line.startswith("-") and not line.startswith("---"):
                    deletions += 1

        files.append(
            models.FileDiff(
                path=path,
                content=content,
                additions=additions,
                deletions=deletions,
                is_binary=is_binary,
                original_size=len(content.encode("utf-8")),
            )
        )

    return files


def _truncate_file_diff(file_diff: models.FileDiff, max_size: int) -> models.FileDiff:
    content = file_diff.content
    if len(content.encode("utf-8")) <= max_size:
        return file_diff

    hunks = list(models._HUNK_PATTERN.finditer(content))

    if not hunks:
        truncated = content[:max_size]
        return models.FileDiff(
            path=file_diff.path,
            content=truncated + "\n[...truncated...]\n",
            additions=file_diff.additions,
            deletions=file_diff.deletions,
            is_binary=file_diff.is_binary,
            is_truncated=True,
            original_size=file_diff.original_size,
        )

    header = content[: hunks[0].start()]
    header_size = len(header.encode("utf-8"))

    truncated_parts = [header]
    current_size = header_size
    hunks_included = 0

    for i, hunk_match in enumerate(hunks):
        hunk_start = hunk_match.start()
        hunk_end = hunks[i + 1].start() if i + 1 < len(hunks) else len(content)
        hunk_content = content[hunk_start:hunk_end]
        hunk_size = len(hunk_content.encode("utf-8"))

        if current_size + hunk_size + models._TRUNCATION_RESERVE > max_size:
            break

        truncated_parts.append(hunk_content)
        current_size += hunk_size
        hunks_included += 1

    remaining_hunks = len(hunks) - hunks_included
    if remaining_hunks > 0:
        remaining_adds = file_diff.additions
        remaining_dels = file_diff.deletions

        included_content = "".join(truncated_parts)
        for line in included_content.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                remaining_adds -= 1
            elif line.startswith("-") and not line.startswith("---"):
                remaining_dels -= 1

        truncated_parts.append(
            f"\n[...{remaining_hunks} more hunks: "
            f"+{max(0, remaining_adds)}, -{max(0, remaining_dels)} lines...]\n"
        )

    return models.FileDiff(
        path=file_diff.path,
        content="".join(truncated_parts),
        additions=file_diff.additions,
        deletions=file_diff.deletions,
        is_binary=file_diff.is_binary,
        is_truncated=True,
        original_size=file_diff.original_size,
    )


def _create_binary_summary(path: str) -> str:
    return f"diff --git a/{path} b/{path}\n[Binary file]\n"


def _priority_score(file_diff: models.FileDiff) -> int:
    score = file_diff.additions + file_diff.deletions
    if file_diff.path.endswith((".toml", ".json", ".yaml", ".yml")):
        score += 50
    if "test" in file_diff.path.lower():
        score = max(1, score - 10)
    return score


def _create_stat_line(path: str, additions: int, deletions: int) -> str:
    return f"{path} | +{additions} -{deletions}"


def _compute_file_budgets(
    files: list[models.FileDiff],
) -> dict[str, int]:
    text_files = [f for f in files if not f.is_binary]
    if not text_files:
        return {}

    binary_count = sum(1 for f in files if f.is_binary)
    binary_overhead = binary_count * models.BINARY_SUMMARY_OVERHEAD
    text_budget = max(0, models.MAX_TOTAL_DIFF_SIZE - binary_overhead)

    fair_share = text_budget // len(text_files)
    cap = min(models.MAX_FILE_DIFF_SIZE, fair_share)

    budgets: dict[str, int] = {}
    large_files: list[models.FileDiff] = []
    surplus = 0

    for f in text_files:
        if f.original_size <= cap:
            budgets[f.path] = cap
            surplus += cap - f.original_size
        else:
            large_files.append(f)

    if large_files:
        extra = surplus // len(large_files)
        for f in large_files:
            budgets[f.path] = min(models.MAX_FILE_DIFF_SIZE, cap + extra)

    return budgets


def process_raw_diff(raw_diff: str) -> models.DiffResult:
    """Reduce a raw unified diff to a budgeted, prompt-safe ``DiffResult``."""
    file_diffs = _parse_diff_into_files(raw_diff)
    if not file_diffs:
        return models.DiffResult(diff="", is_empty=True)

    binary_files: list[str] = []
    text_files: list[models.FileDiff] = []
    for file_diff in file_diffs:
        if file_diff.is_binary:
            binary_files.append(file_diff.path)
        else:
            text_files.append(file_diff)

    budgets = _compute_file_budgets(list(file_diffs))

    text_files.sort(key=_priority_score, reverse=True)

    truncated_files: list[str] = []
    processed_text: list[models.FileDiff] = []
    for file_diff in text_files:
        budget = budgets.get(file_diff.path, models.MAX_FILE_DIFF_SIZE)
        if file_diff.original_size > budget:
            file_diff = _truncate_file_diff(file_diff, budget)
            truncated_files.append(file_diff.path)
        processed_text.append(file_diff)

    # Greedy assembly with stat-only demotion for whatever no longer fits.
    binary_summaries = [_create_binary_summary(path) for path in binary_files]
    binary_summary_size = sum(len(summary.encode()) for summary in binary_summaries)

    stat_only_items: list[tuple[str, int, int]] = []
    included: list[models.FileDiff] = []
    running_total = 0

    for file_diff in processed_text:
        content_size = len(file_diff.content.encode())
        if (
            running_total + content_size + binary_summary_size
            <= models.MAX_TOTAL_DIFF_SIZE
        ):
            included.append(file_diff)
            running_total += content_size
        else:
            stat_only_items.append(
                (file_diff.path, file_diff.additions, file_diff.deletions)
            )

    stat_block = ""
    if stat_only_items:
        lines = [_create_stat_line(*item) for item in stat_only_items]
        stat_block = (
            "\n# Stat-only summaries (diff too large for full content):\n"
            + "\n".join(lines)
            + "\n"
        )

    parts = [file_diff.content for file_diff in included]
    if stat_block:
        parts.append(stat_block)
    parts.extend(binary_summaries)

    final_diff = "".join(parts)
    omitted_files = [path for path, _, _ in stat_only_items]

    return models.DiffResult(
        diff=final_diff,
        files_included=[file_diff.path for file_diff in included],
        omitted_files=omitted_files,
        binary_files=binary_files,
        truncated_files=truncated_files,
        total_size=len(final_diff.encode("utf-8")),
        is_truncated=bool(binary_files or truncated_files or omitted_files),
        is_empty=not final_diff.strip(),
    )


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def format_size(size: int) -> str:
    """Render a byte count for human-facing notices."""
    if size < 1024:
        return f"{size} B"
    kilobytes = size / 1024
    if kilobytes < 1024:
        return f"{kilobytes:.1f} KB"
    return f"{kilobytes / 1024:.1f} MB"


def _capped_file_list(paths: Sequence[str], limit: int) -> str:
    """List up to ``limit`` names, collapsing the rest into a remainder count."""
    listed = ", ".join(paths[:limit])
    remainder = len(paths) - limit
    if remainder > 0:
        listed += f", ... (+{remainder} more)"
    return listed


def build_truncation_notice(result: models.DiffResult) -> str:
    """Summarize what budgeting removed; empty when nothing was removed."""
    notes: list[str] = []
    if result.binary_files:
        count = len(result.binary_files)
        notes.append(
            f"{count} binary file(s) summarized: "
            f"{_capped_file_list(result.binary_files, models._NOTICE_NAME_LIMIT)}"
        )
    if result.truncated_files:
        count = len(result.truncated_files)
        notes.append(
            f"{count} large file(s) truncated: "
            f"{_capped_file_list(result.truncated_files, models._NOTICE_NAME_LIMIT)}"
        )
    if result.omitted_files:
        count = len(result.omitted_files)
        notes.append(
            f"{count} file(s) reduced to stat summary: "
            f"{_capped_file_list(result.omitted_files, models._NOTICE_NAME_LIMIT)}"
        )
    return "; ".join(notes)
