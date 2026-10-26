"""Completion-contract records, check kinds, and default execution budgets."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .plans_session_ids import SessionId

COMPLETION_CONTRACT_HEADING = "## Completion Contract"


DONE_WHEN_HEADING = "### Done when"


VERIFY_HEADING = "### Verify"


_FENCE_RE = re.compile(r"^\s*(?P<run>`{3,}|~{3,})(?P<rest>.*)$")


def fenced_line_flags(lines: Sequence[str]) -> list[bool]:
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


CommandKind = Literal["command"]


FileExistsKind = Literal["file_exists"]


RegexInFileKind = Literal["regex_in_file"]


DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0


MAX_COMMAND_TIMEOUT_SECONDS = 3600.0


BaselineExpectation = Literal["pass", "fail", "skip"]


#: Baseline expectations, in a stable order for error messages.
BASELINE_EXPECTATIONS: tuple[BaselineExpectation, ...] = ("pass", "fail", "skip")


#: Legacy contracts (and any check that omits ``baseline_expectation``) default
#: to ``skip`` so plans authored before this field parse unchanged. Newly
#: prepared plans are expected to specify the field explicitly.
DEFAULT_BASELINE_EXPECTATION: BaselineExpectation = "skip"


#: Per-check baseline execution budget and total recipe budget, in seconds.
DEFAULT_BASELINE_CHECK_BUDGET_SECONDS = 120.0


DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS = 600.0


@dataclass(frozen=True)
class CommandCheck:
    """A check that runs a command (no shell) and asserts it exits 0.

    ``generated_gate`` is provenance, not configuration: it says the runner
    rendered this command itself rather than reading it from a contract. Every
    other field is authored text, so nothing else on this object can establish
    that. It is deliberately absent from :data:`_ALLOWED_CHECK_KEYS` and from
    :func:`check_payload`, which is what keeps it unsettable from a session
    file and unable to survive a round trip through one.
    """

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    cwd: str | None = None
    baseline_expectation: BaselineExpectation = DEFAULT_BASELINE_EXPECTATION
    kind: CommandKind = "command"
    on_failure_of: tuple[str, ...] = ()
    repo: str | None = None
    review_replay: bool = False
    generated_gate: bool = False


@dataclass(frozen=True)
class FileExistsCheck:
    """A check that asserts a path exists.

    Relative paths resolve against the repo root; absolute paths are used as-is.
    """

    name: str
    path: str
    baseline_expectation: BaselineExpectation = DEFAULT_BASELINE_EXPECTATION
    kind: FileExistsKind = "file_exists"
    on_failure_of: tuple[str, ...] = ()
    repo: str | None = None
    review_replay: bool = False


@dataclass(frozen=True)
class RegexInFileCheck:
    """A check that asserts a regex matches somewhere in a file.

    Relative paths resolve against the repo root; absolute paths are used as-is.
    """

    name: str
    path: str
    pattern: str
    baseline_expectation: BaselineExpectation = DEFAULT_BASELINE_EXPECTATION
    kind: RegexInFileKind = "regex_in_file"
    on_failure_of: tuple[str, ...] = ()
    repo: str | None = None
    review_replay: bool = False


VerificationCheck = CommandCheck | FileExistsCheck | RegexInFileCheck


@dataclass(frozen=True)
class VerifyRecipe:
    """Ordered tuple of verification checks for a session.

    Checks without ``on_failure_of`` are authoritative gates. Checks that name
    one or more gates in ``on_failure_of`` are diagnostics: they run only when
    at least one named gate fails and never determine whether verification
    passes. ``parallel`` opts the command checks within each execution phase
    into concurrent execution. Results, artifacts, and failure reporting stay
    in declared order either way, so the author is responsible for concurrently
    eligible commands being side-effect-free with respect to one another.
    """

    checks: tuple[VerificationCheck, ...]
    parallel: bool = False


@dataclass(frozen=True)
class CompletionContract:
    """A parsed `## Completion Contract` for a single session."""

    session_id: SessionId
    done_when: tuple[str, ...]
    verify: VerifyRecipe
