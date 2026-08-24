"""Diff capture, prompt construction, generation, and the ``asl commit`` handler.

The diff half was ported from the retired
``skills/gitsummary/scripts/gitsummary-prepare.py`` (itself a stdlib port of the
retired spec CLI's diff service).  The budget arithmetic came across verbatim,
so the truncated diff this module inlines into a prompt stays byte-for-byte
comparable with what that script produced.  The message half was ported from the
retired ``skills/gitsummary/scripts/format-commit.py``, plus the API-error guard
that copy never had.  The ``gitsummary`` skill now calls this module through
``asl commit`` rather than carrying its own copy.

The git surface is module-local on purpose: ``asl commit`` runs standalone,
outside a loop, so it must not pull the runner import graph in behind it.  The
same rule governs the ASL imports the generation half needs — ``harnesses``,
``config``, ``harness_failures``, ``retry_hints``, and ``responses`` are
imported inside the functions that use them, so importing this module still
costs nothing but the standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from . import harnesses

MAX_TOTAL_DIFF_SIZE = 150 * 1024
MAX_FILE_DIFF_SIZE = 30 * 1024

# Per-binary-file allowance withheld from the text budget so the one-line
# summaries always fit alongside the text diffs.
BINARY_SUMMARY_OVERHEAD = 50

# Bytes kept free inside a per-file budget so the truncation marker fits.
_TRUNCATION_RESERVE = 100

# Names listed inline before a truncation notice collapses to a remainder count.
_NOTICE_NAME_LIMIT = 3

# The same cap for the prompt's truncation note, which has room for more names
# than the one-line header notice does.
_PROMPT_NAME_LIMIT = 5

_FILE_HEADER_PATTERN = re.compile(r"^diff --git \w/(.+?) \w/(.+?)$", re.MULTILINE)
_PLUS_PATH_PATTERN = re.compile(r"^\+\+\+ \w/(.+)$", re.MULTILINE)
_BINARY_FILES_PATTERN = re.compile(r"^Binary files .+ and .+ differ$", re.MULTILINE)
_GIT_BINARY_PATCH_PATTERN = re.compile(r"^GIT binary patch$", re.MULTILINE)
_HUNK_PATTERN = re.compile(r"^@@.*@@", re.MULTILINE)

COMMIT_TITLE_PATTERN = re.compile(r"^[a-z]+(\(.+\))?!?:\s+.+")

_BULLET_PATTERN = re.compile(r"^(\s*)([-*•]|\d+\.)\s+")
_LEADING_INDENT_PATTERN = re.compile(r"^(\s*)")

# Trailer lines a generated message must never carry.  Matched case-insensitively
# against the start of the stripped line, the way git recognizes a trailer.
_DISALLOWED_TRAILERS = ("co-authored-by:", "signed-off-by:")

# Machine-readable failure codes the ``--json`` payload reports.  They are this
# command's own vocabulary, not the runner's pause reasons.
REASON_FAILED = "commit-message-failed"
REASON_INVALID_ARGUMENTS = "invalid-arguments"
REASON_NOT_A_WORKTREE = "not-a-worktree"
REASON_NO_CHANGES = "no-changes"
REASON_NO_STAGED_CHANGES = "no-staged-changes"
REASON_STAGE_FAILED = "stage-failed"
REASON_DIFF_FAILED = "diff-failed"
REASON_COMMIT_NOT_FOUND = "commit-not-found"
REASON_EMPTY_DIFF = "empty-diff"
REASON_ALL_BINARY = "all-binary"
REASON_NO_HARNESS = "no-harness"
REASON_GENERATION_FAILED = "generation-failed"
REASON_GENERATION_TIMEOUT = "generation-timeout"
REASON_EMPTY_MESSAGE = "empty-message"
REASON_API_ERROR = "api-error"
REASON_COMMIT_FAILED = "commit-failed"
REASON_CANCELLED = "cancelled"

COMMIT_COMMAND = "commit"

#: Wall-clock bound on the single harness turn that writes a commit message.
#: Generation is one read-only turn over a diff that is already capped at
#: 150 KB, so a turn still running after this long is stalled rather than
#: working; a real stall of exactly that shape was observed twice while this
#: command was being built. Unbounded is not an option a caller can reach by
#: omission: :func:`generate_commit_message` defaults to this value and the
#: ``--timeout`` flag only moves the bound, never removes it.
DEFAULT_GENERATION_TIMEOUT_SECONDS = 900

# The two ``mode`` values the ``--prepare-only`` header carries, kept identical
# to what the retired ``gitsummary-prepare.py`` emitted so the skill's parser is
# unchanged.
PREPARE_MODE_COMMIT = "commit"
PREPARE_MODE_FROM_ID = "from-id"


class CommitMessageError(RuntimeError):
    """Raised when a commit message cannot be produced, or cannot be committed.

    ``reason`` is the machine-readable code the ``--json`` payload reports and
    ``details`` is whatever structured evidence the failure had; a raise that
    supplies neither still reports the generic code rather than nothing.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = REASON_FAILED,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details: dict[str, Any] = dict(details or {})


@dataclass
class FileDiff:
    """One file's section of a unified diff, before or after truncation."""

    path: str
    content: str
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False
    is_truncated: bool = False
    original_size: int = 0


@dataclass
class DiffResult:
    """A diff reduced to fit the prompt budget, plus what reduction cost."""

    diff: str
    files_included: list[str] = field(default_factory=list)
    omitted_files: list[str] = field(default_factory=list)
    binary_files: list[str] = field(default_factory=list)
    truncated_files: list[str] = field(default_factory=list)
    total_size: int = 0
    is_truncated: bool = False
    is_empty: bool = False


# ---------------------------------------------------------------------------
# Diff parsing & truncation (ported from the retired gitsummary-prepare.py)
# ---------------------------------------------------------------------------


def _parse_diff_into_files(diff: str) -> list[FileDiff]:
    if not diff.strip():
        return []

    diff = diff.replace("\r\n", "\n").replace("\r", "\n")

    matches = list(_FILE_HEADER_PATTERN.finditer(diff))
    if not matches:
        return []

    files: list[FileDiff] = []
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
        content = diff[start:end]

        plus_match = _PLUS_PATH_PATTERN.search(content)
        path = plus_match.group(1) if plus_match else match.group(2)

        is_binary = bool(
            _BINARY_FILES_PATTERN.search(content)
            or _GIT_BINARY_PATCH_PATTERN.search(content)
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
            FileDiff(
                path=path,
                content=content,
                additions=additions,
                deletions=deletions,
                is_binary=is_binary,
                original_size=len(content.encode("utf-8")),
            )
        )

    return files


def _truncate_file_diff(file_diff: FileDiff, max_size: int) -> FileDiff:
    content = file_diff.content
    if len(content.encode("utf-8")) <= max_size:
        return file_diff

    hunks = list(_HUNK_PATTERN.finditer(content))

    if not hunks:
        truncated = content[:max_size]
        return FileDiff(
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

        if current_size + hunk_size + _TRUNCATION_RESERVE > max_size:
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

    return FileDiff(
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


def _priority_score(file_diff: FileDiff) -> int:
    score = file_diff.additions + file_diff.deletions
    if file_diff.path.endswith((".toml", ".json", ".yaml", ".yml")):
        score += 50
    if "test" in file_diff.path.lower():
        score = max(1, score - 10)
    return score


def _create_stat_line(path: str, additions: int, deletions: int) -> str:
    return f"{path} | +{additions} -{deletions}"


def _compute_file_budgets(files: list[FileDiff]) -> dict[str, int]:
    text_files = [f for f in files if not f.is_binary]
    if not text_files:
        return {}

    binary_count = sum(1 for f in files if f.is_binary)
    binary_overhead = binary_count * BINARY_SUMMARY_OVERHEAD
    text_budget = max(0, MAX_TOTAL_DIFF_SIZE - binary_overhead)

    fair_share = text_budget // len(text_files)
    cap = min(MAX_FILE_DIFF_SIZE, fair_share)

    budgets: dict[str, int] = {}
    large_files: list[FileDiff] = []
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
            budgets[f.path] = min(MAX_FILE_DIFF_SIZE, cap + extra)

    return budgets


def process_raw_diff(raw_diff: str) -> DiffResult:
    """Reduce a raw unified diff to a budgeted, prompt-safe ``DiffResult``."""
    file_diffs = _parse_diff_into_files(raw_diff)
    if not file_diffs:
        return DiffResult(diff="", is_empty=True)

    binary_files: list[str] = []
    text_files: list[FileDiff] = []
    for file_diff in file_diffs:
        if file_diff.is_binary:
            binary_files.append(file_diff.path)
        else:
            text_files.append(file_diff)

    budgets = _compute_file_budgets(list(file_diffs))

    text_files.sort(key=_priority_score, reverse=True)

    truncated_files: list[str] = []
    processed_text: list[FileDiff] = []
    for file_diff in text_files:
        budget = budgets.get(file_diff.path, MAX_FILE_DIFF_SIZE)
        if file_diff.original_size > budget:
            file_diff = _truncate_file_diff(file_diff, budget)
            truncated_files.append(file_diff.path)
        processed_text.append(file_diff)

    # Greedy assembly with stat-only demotion for whatever no longer fits.
    binary_summaries = [_create_binary_summary(path) for path in binary_files]
    binary_summary_size = sum(len(summary.encode()) for summary in binary_summaries)

    stat_only_items: list[tuple[str, int, int]] = []
    included: list[FileDiff] = []
    running_total = 0

    for file_diff in processed_text:
        content_size = len(file_diff.content.encode())
        if running_total + content_size + binary_summary_size <= MAX_TOTAL_DIFF_SIZE:
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

    return DiffResult(
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


def build_truncation_notice(result: DiffResult) -> str:
    """Summarize what budgeting removed; empty when nothing was removed."""
    notes: list[str] = []
    if result.binary_files:
        count = len(result.binary_files)
        notes.append(
            f"{count} binary file(s) summarized: "
            f"{_capped_file_list(result.binary_files, _NOTICE_NAME_LIMIT)}"
        )
    if result.truncated_files:
        count = len(result.truncated_files)
        notes.append(
            f"{count} large file(s) truncated: "
            f"{_capped_file_list(result.truncated_files, _NOTICE_NAME_LIMIT)}"
        )
    if result.omitted_files:
        count = len(result.omitted_files)
        notes.append(
            f"{count} file(s) reduced to stat summary: "
            f"{_capped_file_list(result.omitted_files, _NOTICE_NAME_LIMIT)}"
        )
    return "; ".join(notes)


# ---------------------------------------------------------------------------
# Git surface
# ---------------------------------------------------------------------------


def _git(
    cwd: Path,
    args: Sequence[str],
    *,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git without raising, mirroring the runner's subprocess pattern."""
    argv = ["git", *args]
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            input=stdin_text,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr=str(error))
    except OSError as error:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=str(error))


def find_git_root(start: Path) -> Path | None:
    """Walk upward from ``start`` for the directory holding ``.git``."""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def has_changes(cwd: Path) -> bool:
    """True when the worktree has any staged, unstaged, or untracked change."""
    result = _git(cwd, ["status", "--porcelain"])
    return result.returncode == 0 and bool(result.stdout.strip())


def _pathspec_args(pathspec: Sequence[str] | None) -> list[str]:
    """The ``-- <paths>`` tail that narrows a git command, or nothing."""
    if not pathspec:
        return []
    return ["--", *pathspec]


def has_staged_changes(cwd: Path, pathspec: Sequence[str] | None = None) -> bool:
    """True when the index differs from HEAD, within ``pathspec`` when given.

    ``git diff --cached --quiet`` exits 1 for a difference and 0 for none;
    anything else is a git failure and must not read as staged content.
    """
    argv = ["diff", "--cached", "--quiet", *_pathspec_args(pathspec)]
    return _git(cwd, argv).returncode == 1


def stage_all(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Stage every change in the worktree."""
    return _git(cwd, ["add", "-A"])


def stage_paths(cwd: Path, paths: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Stage only ``paths``, leaving every other change unstaged."""
    if not paths:
        return subprocess.CompletedProcess(
            ["git", "add", "--"], 0, stdout="", stderr=""
        )
    return _git(cwd, ["add", "--", *paths])


def dequalify_paths(qualified: Sequence[str]) -> list[str]:
    """Strip the ``"<repository>:"`` prefix repository evidence attaches.

    ``repository_evidence._qualified_paths`` emits ``f"{repository}:{path}"``,
    where the repository is an alias or the legacy ``"."`` single-repository
    key.  Splitting on the first colon keeps colons inside a path intact.
    """
    paths: list[str] = []
    for entry in qualified:
        _, separator, remainder = entry.partition(":")
        path = remainder if separator else entry
        if path:
            paths.append(path)
    return list(dict.fromkeys(paths))


def capture_staged_diff(cwd: Path, pathspec: Sequence[str] | None = None) -> DiffResult:
    """Capture the staged diff, reduced to the prompt budget.

    ``pathspec`` narrows the capture to the paths this invocation owns, so an
    unrelated entry staged before the run never reaches the prompt.
    """
    result = _git(cwd, ["diff", "--cached", "--no-ext-diff", *_pathspec_args(pathspec)])
    if result.returncode != 0:
        raise CommitMessageError(
            f"failed to capture staged diff: {result.stderr.strip()}",
            reason=REASON_DIFF_FAILED,
        )
    return process_raw_diff(result.stdout)


def capture_commit_diff(sha: str, cwd: Path) -> DiffResult:
    """Capture an existing commit's diff, reduced to the prompt budget."""
    resolved = _git(cwd, ["rev-parse", "--verify", f"{sha}^{{commit}}"])
    if resolved.returncode != 0:
        raise CommitMessageError(
            f"commit not found: {sha}", reason=REASON_COMMIT_NOT_FOUND
        )
    full_sha = resolved.stdout.strip()

    result = _git(cwd, ["show", full_sha, "--format=", "--no-ext-diff"])
    if result.returncode != 0:
        raise CommitMessageError(
            f"failed to capture diff for commit {sha}: {result.stderr.strip()}",
            reason=REASON_DIFF_FAILED,
        )
    return process_raw_diff(result.stdout)


def changes_are_all_binary(
    cwd: Path,
    *,
    from_id: str | None = None,
    pathspec: Sequence[str] | None = None,
) -> bool:
    """True when every changed file is binary, so no text diff can exist.

    ``git --numstat`` reports a binary file as ``-\t-\t<path>``, which is how
    an empty diff over real changes is told apart from an empty diff over no
    changes at all.  A failed or silent probe answers ``False``: the caller
    then reports the ordinary empty-diff error rather than a specific cause it
    could not actually establish.
    """
    if from_id is not None:
        args = ["show", from_id, "--numstat", "--format=", "--no-renames"]
    else:
        args = [
            "diff",
            "--cached",
            "--numstat",
            "--no-renames",
            *_pathspec_args(pathspec),
        ]
    result = _git(cwd, args)
    if result.returncode != 0 or not result.stdout.strip():
        return False
    return all(
        line.startswith("-\t-\t")
        for line in result.stdout.strip().splitlines()
        if line.strip()
    )


def commit_staged(
    cwd: Path,
    message: str,
    *,
    verify: bool = False,
    pathspec: Sequence[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Commit the staged index, handing the message to git on stdin.

    ``git commit -F -`` avoids both the argv length limit a long body would hit
    and the quoting hazards of a heredoc, so no message shape can change what
    git is asked to do.

    With a ``pathspec``, ``--only`` commits those paths from the index and
    disregards everything else staged, so an entry staged before the run is
    neither committed nor unstaged by it.
    """
    argv = ["commit", "-F", "-"]
    if not verify:
        argv.append("--no-verify")
    if pathspec:
        argv.append("--only")
        argv.extend(_pathspec_args(pathspec))
    return _git(cwd, argv, stdin_text=message)


def head_sha(cwd: Path) -> str:
    """The current HEAD commit, or an empty string when there is none."""
    result = _git(cwd, ["rev-parse", "HEAD"])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Message extraction & formatting (ported from format-commit.py)
# ---------------------------------------------------------------------------


def looks_like_api_error(response: str) -> bool:
    """True when the harness returned an error body instead of a message.

    Catches both ``API Error: 500 {"type":"error",...}`` and a bare error
    object forwarded as if it were message text.  The guard matters far more
    headless than interactive: nobody is watching to notice that the "commit
    message" is a stack of JSON.
    """
    stripped = response.strip()
    if stripped.startswith("API Error:"):
        return True
    brace_pos = stripped.find("{")
    if brace_pos != -1:
        try:
            parsed = json.loads(stripped[brace_pos:])
        except ValueError:
            return False
        return isinstance(parsed, dict) and parsed.get("type") == "error"
    return False


def _is_disallowed_trailer(line: str) -> bool:
    return line.strip().lower().startswith(_DISALLOWED_TRAILERS)


def strip_disallowed_trailers(text: str) -> str:
    """Drop attribution trailer lines and the blank lines they left behind.

    Some harnesses ignore the no-trailer prompt rule and append
    ``Co-authored-by:`` or ``Signed-off-by:`` anyway.  The match is anchored
    to the start of the line rather than run as a substring search, so a body
    sentence *about* trailers survives while a real trailer does not.
    """
    lines = [line for line in text.split("\n") if not _is_disallowed_trailer(line)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def extract_commit_message(text: str) -> str:
    """Return the message from its first conventional-commit title onward.

    Preamble such as "I've read the diff..." is dropped.  Text with no title
    line at all falls back to the whole response; either way disallowed
    trailers are stripped.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if COMMIT_TITLE_PATTERN.match(line.strip()):
            return strip_disallowed_trailers("\n".join(lines[index:]))
    return strip_disallowed_trailers(text)


def detect_line_type(line: str) -> tuple[str, str | tuple[str, str, str]]:
    """Classify a line as ``blank``, ``bullet``, or ``paragraph``.

    A bullet decomposes into its indent, its marker, and the text after the
    marker; everything else is returned unchanged.
    """
    if not line.strip():
        return "blank", line

    bullet_match = _BULLET_PATTERN.match(line)
    if bullet_match:
        indent = bullet_match.group(1)
        bullet = bullet_match.group(2)
        return "bullet", (indent, bullet, line[bullet_match.end() :])

    return "paragraph", line


def format_bullet(indent: str, bullet: str, text: str, width: int = 80) -> str:
    """Wrap one bullet, hanging continuation lines under its text column."""
    first_line_prefix = f"{indent}{bullet} "
    return textwrap.fill(
        text,
        width=width,
        initial_indent=first_line_prefix,
        subsequent_indent=" " * len(first_line_prefix),
        break_long_words=False,
        break_on_hyphens=False,
    )


def format_paragraph(
    text: str,
    width: int = 80,
    initial_indent: str = "",
    subsequent_indent: str = "",
) -> str:
    """Wrap a paragraph, inheriting its own leading indent when none is given."""
    if not initial_indent:
        match = _LEADING_INDENT_PATTERN.match(text)
        if match:
            initial_indent = match.group(1)
            subsequent_indent = initial_indent

    return textwrap.fill(
        text.strip(),
        width=width,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def format_commit_message(text: str, width: int = 80) -> str:
    """Normalize a generated message into committable form.

    The first non-blank line is the title and is kept verbatim, however long
    it is; paragraphs and bullets below it are re-wrapped at ``width`` after
    consecutive lines are joined, and blank lines are preserved as written.
    """
    text = extract_commit_message(text)
    lines = text.split("\n")
    result: list[str] = []

    seen_title = False

    index = 0
    while index < len(lines):
        line = lines[index]
        line_type, content = detect_line_type(line)

        if line_type == "blank":
            result.append("")
            index += 1
            continue

        if not seen_title:
            result.append(line.rstrip())
            seen_title = True
            index += 1
            continue

        if line_type == "bullet":
            indent, bullet, bullet_text = cast(tuple[str, str, str], content)
            # Unbroken paragraph lines below a bullet are its continuation.
            text_parts = [bullet_text.strip()]
            while index + 1 < len(lines):
                next_type, _ = detect_line_type(lines[index + 1])
                if next_type != "paragraph":
                    break
                index += 1
                text_parts.append(lines[index].strip())
            result.append(format_bullet(indent, bullet, " ".join(text_parts), width))
            index += 1
            continue

        paragraph_lines = [line]
        while index + 1 < len(lines):
            next_type, _ = detect_line_type(lines[index + 1])
            if next_type != "paragraph":
                break
            index += 1
            paragraph_lines.append(lines[index])
        combined = " ".join(entry.strip() for entry in paragraph_lines)
        result.append(format_paragraph(combined, width))
        index += 1

    return "\n".join(result)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_truncation_note(diff_result: DiffResult) -> str:
    """Tell the model what the budget removed; empty when nothing was removed.

    This is the prompt's prose counterpart to ``build_truncation_notice``,
    which is the terse notice the ``--prepare-only`` header carries.
    """
    if not diff_result.is_truncated:
        return ""

    parts: list[str] = []
    if diff_result.binary_files:
        parts.append(f"{len(diff_result.binary_files)} binary file(s)")
    if diff_result.truncated_files:
        parts.append(f"{len(diff_result.truncated_files)} file(s) partially shown")
    if diff_result.omitted_files:
        listed = _capped_file_list(diff_result.omitted_files, _PROMPT_NAME_LIMIT)
        parts.append(f"{len(diff_result.omitted_files)} file(s) omitted: {listed}")
    summary = "; ".join(parts)

    return f"""
Note: This diff has been truncated to fit within size limits. {summary}.
Generate a commit message based on the changes shown. If the changes seem incomplete,
focus on what IS visible rather than speculating about omitted content.
"""


def build_commit_prompt(diff_result: DiffResult, guidance: str | None = None) -> str:
    """Build the Conventional Commits prompt around an already-budgeted diff.

    The diff is always inlined under ``## Git Diff`` rather than written to a
    file for the harness to read: reading it back requires granting a Read
    tool, and Copilot's streaming filter drops the final assistant message
    once tool requests interleave.  The 150 KB budget exists so that inlining
    is always affordable.
    """
    guidance_note = f"\n## User Guidance\n\n{guidance}\n" if guidance else ""
    truncation_note = build_truncation_note(diff_result)

    return f"""\
You are a commit message generator. Analyze the following git diff and generate
a properly formatted commit message following the Conventional Commits specification.

## Format Requirements

Use this format:
<type>: <short description (max 72 chars)>

<body paragraph explaining WHAT changed and WHY>

<optional bullet points for multi-aspect commits>

## Type Conventions

- feat: New feature or capability
- fix: Bug fix
- docs: Documentation changes only
- refactor: Code restructuring without behavior change
- test: Test additions or modifications
- chore: Maintenance tasks (dependencies, configs)
- perf: Performance improvements
- style: Code style/formatting changes

## Content Rules

- Use PRESENT TENSE ("Add" not "Added")
- Focus on WHAT changed and WHY it matters
- Do NOT describe HOW the code works (implementation details)
- No spec-specific language (no "phase 1", "task 2", "spec section")
- No markdown syntax (no **bold**, _italic_, or `code`)
- No author attribution (no "Co-authored-by:", "Signed-off-by:", or similar trailers)
- Keep title under 72 characters
- Wrap body at 80 characters
- For multi-aspect commits, use bullet points with 2-space indent

## Output Format

Return ONLY the commit message text. No code blocks, no explanations, no
surrounding text. Title on first line, blank line, then body paragraphs.
{guidance_note}{truncation_note}
## Git Diff

{diff_result.diff}

Generate the commit message:"""


# ---------------------------------------------------------------------------
# Harness-backed generation
# ---------------------------------------------------------------------------


def resolve_harness(requested: str | None = None) -> str:
    """Pick the harness generation runs on: the flag first, else what is usable.

    A harness named in ``harnesses.json`` wins over one that is merely
    installed, because naming an executable there is the strongest statement of
    intent available; within each group the declared harness order decides.
    Nothing is selected that is not actually on PATH, so a missing binary is
    reported here instead of surfacing later as a launch failure.
    """
    from . import config, harnesses

    if requested is not None:
        return requested

    try:
        configured = config.load_harness_config()
    except config.ConfigError as error:
        raise CommitMessageError(str(error), reason=REASON_NO_HARNESS) from error

    ordered = [name for name in harnesses.HARNESS_NAMES if name in configured]
    ordered += [name for name in harnesses.HARNESS_NAMES if name not in configured]
    for name in ordered:
        try:
            adapter = harnesses.get_adapter(name)
        except harnesses.HarnessError:
            continue
        if adapter.resolve_executable() is not None:
            return name

    raise CommitMessageError(
        "no harness executable found on PATH; pass --harness or configure one "
        f"in {config.default_config_path()}",
        reason=REASON_NO_HARNESS,
    )


def generation_settings(
    harness: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict[str, str | None]:
    """Resolve and describe the harness, model, and effort a turn will use.

    Only the flags are ever handed to the harness: the model and effort
    payloads are reporting metadata, and ``native_model_payload`` deliberately
    names the *unresolved* default rather than a model string that could be
    passed as ``--model``.  Session 4's runner passes its own resolved values
    in as flags, so standalone and in-loop invocations share this one path.
    """
    from . import config

    name = resolve_harness(harness)
    try:
        executable = config.configured_harness_executable(name)
    except config.ConfigError as error:
        raise CommitMessageError(str(error), reason=REASON_NO_HARNESS) from error

    settings: dict[str, str | None] = {
        "harness": name,
        "harness_executable": executable,
    }
    settings.update(config.selected_model_payload(name, model))
    settings.update(config.selected_effort_payload(effort))
    return settings


def _classified_harness_error(
    harness: str,
    result: harnesses.HarnessResult,
) -> CommitMessageError:
    """Turn a nonzero harness turn into the shared failure vocabulary.

    ``classify_harness_failure`` already distinguishes quota, authentication,
    invalid-model, transport, and interruption failures from an unrecognized
    exit, and ``retry_hints`` renders whatever reset hint the provider gave as
    an absolute instant, so neither is reinvented here.
    """
    from . import harness_failures, retry_hints

    classification = harness_failures.classify_harness_failure(result)
    detail = f"harness {harness!r} failed ({classification.reason})"
    if classification.evidence:
        detail += f": {classification.evidence}"
    retry_at = retry_hints.normalized_retry_at(classification.retry_at)
    if retry_at is not None:
        detail += f"; retry at {retry_at}"
    return CommitMessageError(
        detail,
        reason=REASON_GENERATION_FAILED,
        details={"classification": classification.to_dict(), "retry_at": retry_at},
    )


def _generation_timeout_error(
    harness: str,
    result: harnesses.HarnessResult,
    timeout: int | None,
) -> CommitMessageError:
    """Turn an expired commit-message turn into the shared failure vocabulary.

    A stall is reported under its own reason rather than folded into
    ``generation-failed``: the two ask for different responses, and only this
    one says the harness never answered at all. The classification is still
    attached, because ``run_harness`` may well have captured a transport error
    before the turn wedged, and ``process_sweep`` travels with it as the
    evidence that the turn's process tree was actually taken down.
    """
    from . import harness_failures

    classification = harness_failures.classify_harness_failure(result)
    bound = "its bound" if timeout is None else f"{timeout}s"
    return CommitMessageError(
        f"harness {harness!r} did not produce a commit message within {bound}",
        reason=REASON_GENERATION_TIMEOUT,
        details={
            "classification": classification.to_dict(),
            "timeout_seconds": timeout,
            "process_sweep": result.process_sweep,
        },
    )


def generate_commit_message(
    diff_result: DiffResult,
    *,
    repo_root: Path,
    harness: str,
    guidance: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    timeout: int | None = DEFAULT_GENERATION_TIMEOUT_SECONDS,
) -> str:
    """Run one read-only harness turn and return the message it produced.

    The turn runs under ``audit_read_only_access((repo_root,))``: writing a
    commit message only ever needs to read a diff the prompt already carries,
    so no adapter is handed a writable root and every adapter is hardened by
    the profile rather than by per-harness branching here.  ``plan_dir`` is
    ``None`` for the same reason — it is the worker's writable grant, and this
    turn has no plan.

    A nonzero exit is classified rather than reported as a bare exit code, and
    a response that is empty or is an error body is rejected instead of being
    committed: headless, nobody is watching to notice that the "commit message"
    is a stack of JSON.

    ``timeout`` bounds the turn by default. ``run_harness`` kills the turn's
    whole process group and sweeps whatever escaped it before returning, so a
    stalled harness that fanned out into helpers cannot outlive the bound; the
    expiry then leaves here as an ordinary :class:`CommitMessageError`, which
    is what puts a stall on the same soft-failure path as a refused commit.
    Passing ``None`` is an explicit request for an unbounded turn and no
    ``asl commit`` invocation can produce it.
    """
    from . import harnesses

    prompt = build_commit_prompt(diff_result, guidance)
    with tempfile.TemporaryDirectory(prefix="asl-commit-") as tmp:
        try:
            result = harnesses.run_harness(
                harness,
                cwd=repo_root,
                plan_dir=None,
                prompt=prompt,
                unsafe_bypass=False,
                last_message_path=Path(tmp) / "commit-last-message.txt",
                timeout=timeout,
                model=model,
                effort=effort,
                access=harnesses.audit_read_only_access((repo_root,)),
            )
        except harnesses.HarnessError as error:
            raise CommitMessageError(
                f"harness {harness!r} failed to launch: {error}",
                reason=REASON_GENERATION_FAILED,
            ) from error

    if result.timed_out:
        raise _generation_timeout_error(harness, result, timeout)
    if result.returncode != 0:
        raise _classified_harness_error(harness, result)

    response = result.last_message.strip()
    if not response:
        raise CommitMessageError(
            f"harness {harness!r} returned an empty commit message",
            reason=REASON_EMPTY_MESSAGE,
        )
    if looks_like_api_error(response):
        first_line = response.splitlines()[0]
        raise CommitMessageError(
            f"harness {harness!r} returned an API error instead of a commit "
            f"message: {first_line[:200]}",
            reason=REASON_API_ERROR,
        )
    return response


# ---------------------------------------------------------------------------
# `asl commit` handler
# ---------------------------------------------------------------------------


def _reject_conflicting_modes(args: argparse.Namespace) -> None:
    """Refuse flag combinations that ask for two different commands at once."""
    conflicts = (
        (args.from_id and args.staged, "--from-id and --staged"),
        (args.from_id and args.pathspec, "--from-id and --pathspec"),
        (args.from_id and args.message_file, "--from-id and --message-file"),
        (args.prepare_only and args.message_file, "--prepare-only and --message-file"),
        (args.staged and args.pathspec, "--staged and --pathspec"),
    )
    for conflicting, names in conflicts:
        if conflicting:
            raise CommitMessageError(
                f"{names} are mutually exclusive",
                reason=REASON_INVALID_ARGUMENTS,
            )


def _resolve_repo_root(path: str | None) -> Path:
    """Resolve the worktree ``asl commit`` operates on, refusing anything else."""
    start = Path(path).expanduser() if path else Path.cwd()
    if not start.exists():
        raise CommitMessageError(
            f"path does not exist: {start}", reason=REASON_INVALID_ARGUMENTS
        )
    root = find_git_root(start)
    if root is None:
        raise CommitMessageError(
            f"not a git worktree: {start}", reason=REASON_NOT_A_WORKTREE
        )
    return root


def _stage_for_commit(args: argparse.Namespace, repo_root: Path) -> None:
    """Bring the index to what this invocation intends to commit.

    ``--staged`` promises the index is already right and refuses to touch it;
    ``--pathspec`` stages exactly what it names and leaves every other change
    alone; the default stages everything, exactly as the skill does today.
    """
    if args.staged:
        if not has_staged_changes(repo_root):
            raise CommitMessageError(
                "no staged changes; stage something or drop --staged",
                reason=REASON_NO_STAGED_CHANGES,
            )
        return

    if args.pathspec:
        staged = stage_paths(repo_root, args.pathspec)
    else:
        if not has_changes(repo_root):
            raise CommitMessageError("no changes to commit", reason=REASON_NO_CHANGES)
        staged = stage_all(repo_root)

    if staged.returncode != 0:
        raise CommitMessageError(
            f"git add failed: {staged.stderr.strip()}", reason=REASON_STAGE_FAILED
        )


def _reject_empty_diff(
    diff_result: DiffResult,
    repo_root: Path,
    *,
    from_id: str | None = None,
    pathspec: Sequence[str] | None = None,
) -> None:
    """Refuse a diff with nothing to summarize, saying which kind of nothing."""
    if not diff_result.is_empty:
        return
    if changes_are_all_binary(repo_root, from_id=from_id, pathspec=pathspec):
        raise CommitMessageError(
            "all changed files are binary - no text diff to summarize",
            reason=REASON_ALL_BINARY,
        )
    raise CommitMessageError("diff is empty", reason=REASON_EMPTY_DIFF)


def _read_message_text(source: str) -> str:
    """Read ``--message-file``, treating ``-`` as stdin."""
    if source == "-":
        return sys.stdin.read()
    path = Path(source).expanduser()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise CommitMessageError(
            f"cannot read message file {path}: {error}",
            reason=REASON_INVALID_ARGUMENTS,
        ) from error


def _is_tty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except OSError, ValueError:
        return False


def _confirm(args: argparse.Namespace, question: str) -> bool:
    """Ask before committing, when there is a person there to ask.

    A piped or redirected invocation is a deliberate one and proceeds
    unprompted, which is what keeps the documented command shape scriptable
    and lets the runner drive it without a terminal.
    """
    if args.yes or args.json:
        return True
    if not (_is_tty(sys.stdin) and _is_tty(sys.stdout)):
        return True
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def _prepare_header(
    args: argparse.Namespace, diff_result: DiffResult
) -> dict[str, Any]:
    """Build the ``--prepare-only`` header ``gitsummary-prepare.py`` used to emit.

    The key set and its order are the skill's parsing contract, and
    ``build_truncation_notice`` is the notice that contract carries — the
    prompt's looser ``build_truncation_note`` is a different string.
    """
    return {
        "mode": PREPARE_MODE_FROM_ID if args.from_id else PREPARE_MODE_COMMIT,
        "yes": bool(args.yes),
        "no_verify": not args.verify,
        "guidance": args.guidance,
        "is_truncated": diff_result.is_truncated,
        "truncation_notes": build_truncation_notice(diff_result),
        "from_id": args.from_id,
    }


def _emit_prepare_only(
    args: argparse.Namespace,
    diff_result: DiffResult,
) -> int:
    """Emit a single-line JSON header, a blank line, and the budgeted diff."""
    from . import responses

    header = _prepare_header(args, diff_result)
    if args.json:
        responses.print_json(
            responses.success_payload(
                COMMIT_COMMAND,
                mode="prepare-only",
                committed=False,
                header=header,
                diff=diff_result.diff,
            )
        )
        return 0
    sys.stdout.write(json.dumps(header) + "\n\n")
    sys.stdout.write(diff_result.diff)
    return 0


def _warn_if_truncated(args: argparse.Namespace, diff_result: DiffResult) -> None:
    if args.json or not diff_result.is_truncated:
        return
    notice = build_truncation_notice(diff_result)
    if notice:
        print(
            f"asl commit: diff reduced to fit the prompt budget: {notice}",
            file=sys.stderr,
        )


def _emit_message_only(
    args: argparse.Namespace,
    message: str,
    diff_result: DiffResult,
    settings: dict[str, str | None],
) -> int:
    """Print a generated message without committing, as ``--from-id`` promises."""
    from . import responses

    if args.json:
        responses.print_json(
            responses.success_payload(
                COMMIT_COMMAND,
                mode=PREPARE_MODE_FROM_ID,
                committed=False,
                from_id=args.from_id,
                message=message,
                is_truncated=diff_result.is_truncated,
                truncation_notes=build_truncation_notice(diff_result),
                **settings,
            )
        )
    else:
        print(message)
    return 0


def _commit_pathspec(args: argparse.Namespace) -> list[str] | None:
    """The paths this invocation owns, or ``None`` when it owns the index.

    ``--pathspec`` is the only mode that scopes the commit: ``--staged`` is
    mutually exclusive with it and promises the whole index is intended.
    """
    return list(args.pathspec) if args.pathspec else None


def _generation_timeout(args: argparse.Namespace) -> int:
    """The bound this invocation puts on its one message-generation turn.

    ``getattr`` rather than ``args.timeout`` so an in-process caller building
    its own namespace still gets the bound instead of an ``AttributeError``;
    the flag itself already defaults to it. A non-positive value is refused
    rather than quietly read as "no bound": ``run_harness`` would treat 0 as an
    immediate expiry and a negative number is not a bound at all.
    """
    timeout = getattr(args, "timeout", None)
    if timeout is None:
        return DEFAULT_GENERATION_TIMEOUT_SECONDS
    timeout = int(timeout)
    if timeout <= 0:
        raise CommitMessageError(
            "--timeout must be a positive number of seconds",
            reason=REASON_INVALID_ARGUMENTS,
        )
    return timeout


def _commit_and_report(
    args: argparse.Namespace,
    repo_root: Path,
    message: str,
    *,
    mode: str,
    extra: dict[str, Any],
) -> int:
    """Print the message, then commit it unless the caller stopped short.

    Stdout carries the message and nothing else, so ``asl commit --dry-run >
    msg.txt`` is a usable command; progress and outcome go to stderr.
    """
    from . import responses

    if not args.json:
        print(message)

    if args.dry_run:
        if args.json:
            responses.print_json(
                responses.success_payload(
                    COMMIT_COMMAND,
                    mode=mode,
                    committed=False,
                    dry_run=True,
                    repo_root=str(repo_root),
                    message=message,
                    **extra,
                )
            )
        return 0

    if not _confirm(args, "Commit with this message?"):
        raise CommitMessageError("commit declined", reason=REASON_CANCELLED)

    committed = commit_staged(
        repo_root, message, verify=args.verify, pathspec=_commit_pathspec(args)
    )
    if committed.returncode != 0:
        raise CommitMessageError(
            f"git commit failed: {(committed.stderr or committed.stdout).strip()}",
            reason=REASON_COMMIT_FAILED,
            details={"returncode": committed.returncode},
        )

    sha = head_sha(repo_root)
    if args.json:
        responses.print_json(
            responses.success_payload(
                COMMIT_COMMAND,
                mode=mode,
                committed=True,
                dry_run=False,
                repo_root=str(repo_root),
                commit=sha,
                message=message,
                **extra,
            )
        )
    else:
        print(f"asl commit: committed {sha[:12]}", file=sys.stderr)
    return 0


def _run_from_id(args: argparse.Namespace, repo_root: Path) -> int:
    """Describe an existing commit; never stage and never commit."""
    diff_result = capture_commit_diff(args.from_id, repo_root)
    _reject_empty_diff(diff_result, repo_root, from_id=args.from_id)
    if args.prepare_only:
        return _emit_prepare_only(args, diff_result)

    _warn_if_truncated(args, diff_result)
    settings = generation_settings(args.harness, args.model, args.effort)
    message = format_commit_message(
        generate_commit_message(
            diff_result,
            repo_root=repo_root,
            harness=cast(str, settings["harness"]),
            guidance=args.guidance,
            model=args.model,
            effort=args.effort,
            timeout=_generation_timeout(args),
        )
    )
    return _emit_message_only(args, message, diff_result, settings)


def _run_message_file(args: argparse.Namespace, repo_root: Path) -> int:
    """Commit an operator-supplied message; the one mode that needs no harness."""
    text = _read_message_text(args.message_file)
    if not text.strip():
        raise CommitMessageError(
            "the supplied commit message is empty", reason=REASON_EMPTY_MESSAGE
        )
    if not has_staged_changes(repo_root, _commit_pathspec(args)):
        raise CommitMessageError(
            "no staged changes to commit", reason=REASON_NO_STAGED_CHANGES
        )
    message = format_commit_message(text)
    if not message.strip():
        raise CommitMessageError(
            "the supplied commit message is empty after formatting",
            reason=REASON_EMPTY_MESSAGE,
        )
    return _commit_and_report(
        args,
        repo_root,
        message,
        mode="message-file",
        extra={"message_file": args.message_file, "generated": False},
    )


def _run_default(args: argparse.Namespace, repo_root: Path) -> int:
    """Capture, generate, format, and commit — the mode every flag defaults to."""
    pathspec = _commit_pathspec(args)
    diff_result = capture_staged_diff(repo_root, pathspec)
    _reject_empty_diff(diff_result, repo_root, pathspec=pathspec)
    if args.prepare_only:
        return _emit_prepare_only(args, diff_result)

    _warn_if_truncated(args, diff_result)
    settings = generation_settings(args.harness, args.model, args.effort)
    message = format_commit_message(
        generate_commit_message(
            diff_result,
            repo_root=repo_root,
            harness=cast(str, settings["harness"]),
            guidance=args.guidance,
            model=args.model,
            effort=args.effort,
            timeout=_generation_timeout(args),
        )
    )
    if not message.strip():
        raise CommitMessageError(
            "the generated commit message is empty after formatting",
            reason=REASON_EMPTY_MESSAGE,
        )
    return _commit_and_report(
        args,
        repo_root,
        message,
        mode=PREPARE_MODE_COMMIT,
        extra={
            "generated": True,
            "is_truncated": diff_result.is_truncated,
            "truncation_notes": build_truncation_notice(diff_result),
            **settings,
        },
    )


def _dispatch_commit(args: argparse.Namespace) -> int:
    _reject_conflicting_modes(args)
    # Ahead of staging, so an unusable bound is refused before the invocation
    # has touched the index rather than after.
    _generation_timeout(args)
    repo_root = _resolve_repo_root(args.path)

    if args.from_id:
        return _run_from_id(args, repo_root)

    _stage_for_commit(args, repo_root)
    if args.message_file:
        return _run_message_file(args, repo_root)
    return _run_default(args, repo_root)


def commit_from_args(args: argparse.Namespace) -> int:
    """Run the ``asl commit`` subcommand from CLI arguments."""
    from . import responses

    try:
        return _dispatch_commit(args)
    except CommitMessageError as error:
        if args.json:
            responses.print_json(
                responses.error_payload(
                    COMMIT_COMMAND,
                    reason=error.reason,
                    error=str(error),
                    details=error.details,
                )
            )
        else:
            print(f"asl commit: {error}", file=sys.stderr)
        return 1
