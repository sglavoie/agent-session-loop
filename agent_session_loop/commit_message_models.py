"""Commit message models."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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


#: A flag combination or value the command refuses before doing any work;
#: spelled like every other command's input mistakes.
REASON_INVALID_ARGUMENTS = "input-error"


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


REASON_SNAPSHOT_DRIFTED = "snapshot-drifted"


COMMIT_COMMAND = "commit"


#: Wall-clock bound on the single harness turn that writes a commit message.
#: Generation is one read-only turn over a diff that is already capped at
#: 150 KB, so a turn still running after this long is stalled rather than
#: working; a real stall of exactly that shape was observed twice while this
#: command was being built. Unbounded is not an option a caller can reach by
#: omission: :func:`generate_commit_message` defaults to this value and the
#: ``--timeout`` flag only moves the bound, never removes it.
DEFAULT_GENERATION_TIMEOUT_SECONDS = 900


# Length of the ``--prepare-only`` snapshot token: long enough that no two
# distinct index states collide in practice, short enough to travel through a
# skill's shell command without wrapping.
SNAPSHOT_TOKEN_LENGTH = 16


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
