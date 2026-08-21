"""Completion contract parsing for session files.

Each session file may include a ``## Completion Contract`` section with a
``### Done when`` bullet list of human-readable criteria and a ``### Verify``
JSON block describing executable checks.

The package is stdlib-only, so the machine-executable recipe is JSON rather
than YAML. The parser validates schema, session IDs, command argv lists,
timeouts, file checks, and regex checks, and raises :class:`plans.PlanError`
with clear messages for any malformed contract so callers see a consistent
error surface.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from .plans import (
    HANDOFF_HEADING,
    PlanError,
    SessionId,
    canonicalize_path,
    parse_session_id,
    read_repository_registry,
    session_id_to_filename,
)
from .workspaces import (
    RepositoryRegistry,
    WorkspaceError,
    validate_repository_alias,
)

COMPLETION_CONTRACT_HEADING = "## Completion Contract"
DONE_WHEN_HEADING = "### Done when"
VERIFY_HEADING = "### Verify"

_FENCE_RE = re.compile(r"^\s*(?P<run>`{3,}|~{3,})(?P<rest>.*)$")

CommandKind = Literal["command"]
FileExistsKind = Literal["file_exists"]
RegexInFileKind = Literal["regex_in_file"]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0
MAX_COMMAND_TIMEOUT_SECONDS = 3600.0

BaselineExpectation = Literal["pass", "fail", "skip"]

#: Baseline expectations, in a stable order for error messages.
BASELINE_EXPECTATIONS: tuple[str, ...] = ("pass", "fail", "skip")

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


def _require_str(value: object, *, label: str, session_id: SessionId) -> str:
    if not isinstance(value, str) or value == "":
        raise PlanError(
            f"session {session_id.value} contract field {label!r} must be a "
            "non-empty string"
        )
    return value


def _require_positive_number(
    value: object, *, label: str, session_id: SessionId
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(
            f"session {session_id.value} contract field {label!r} must be a "
            "positive number"
        )
    numeric = float(value)
    if numeric != numeric or numeric <= 0:
        raise PlanError(
            f"session {session_id.value} contract field {label!r} must be a "
            "positive number"
        )
    return numeric


def _baseline_expectation(
    payload: dict, *, index: int, session_id: SessionId
) -> BaselineExpectation:
    raw = payload.get("baseline_expectation")
    if raw is None:
        return DEFAULT_BASELINE_EXPECTATION
    if raw not in BASELINE_EXPECTATIONS:
        allowed = ", ".join(BASELINE_EXPECTATIONS)
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'baseline_expectation' must be one of: {allowed}; got {raw!r}"
        )
    return raw  # type: ignore[return-value]


def _check_name(raw_name: object, *, index: int, session_id: SessionId) -> str:
    if raw_name is None:
        return f"check-{index + 1}"
    if not isinstance(raw_name, str) or raw_name.strip() == "":
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'name' must be a non-empty string when provided"
        )
    return raw_name.strip()


def _on_failure_of(
    payload: dict, *, index: int, session_id: SessionId
) -> tuple[str, ...]:
    raw = payload.get("on_failure_of")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'on_failure_of' must be a non-empty list of check names when provided"
        )
    names: list[str] = []
    seen: set[str] = set()
    for dependency_index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise PlanError(
                f"session {session_id.value} contract check #{index + 1} "
                f"'on_failure_of'[{dependency_index}] must be a non-empty string"
            )
        name = value.strip()
        if name in seen:
            raise PlanError(
                f"session {session_id.value} contract check #{index + 1} "
                f"'on_failure_of' duplicates check name {name!r}"
            )
        seen.add(name)
        names.append(name)
    return tuple(names)


def _review_replay(payload: dict, *, index: int, session_id: SessionId) -> bool:
    raw = payload.get("review_replay", False)
    if not isinstance(raw, bool):
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'review_replay' must be a boolean; got {raw!r}"
        )
    return raw


def _repository_alias(
    payload: dict,
    *,
    index: int,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None,
) -> str | None:
    raw = payload.get("repo")
    if raw is None:
        if repository_registry is None:
            return None
        if len(repository_registry) == 1:
            return repository_registry.aliases[0]
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'repo' is required when the repository registry has multiple entries"
        )
    if repository_registry is None:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'repo' is only valid when the plan declares a repository registry"
        )
    if not isinstance(raw, str) or not raw:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'repo' must be a non-empty repository alias when provided"
        )
    try:
        alias = validate_repository_alias(raw)
        if repository_registry is not None:
            repository_registry.get(alias)
    except WorkspaceError as error:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'repo' is invalid: {error}"
        ) from error
    return alias


def _validate_repository_relative_value(
    value: str,
    *,
    field: str,
    index: int,
    session_id: SessionId,
) -> None:
    """Reject values that could escape a registry-selected repository root."""
    if (
        "\\" in value
        or PureWindowsPath(value).is_absolute()
        or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
    ):
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'{field}' must stay beneath the selected repository and cannot "
            "be absolute or traverse upward"
        )


def _parse_command_check(
    payload: dict,
    *,
    index: int,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> CommandCheck:
    raw_argv = payload.get("argv")
    if not isinstance(raw_argv, list) or len(raw_argv) == 0:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'argv' must be a non-empty list of strings"
        )
    argv: list[str] = []
    for arg_index, arg_value in enumerate(raw_argv):
        if not isinstance(arg_value, str) or arg_value == "":
            raise PlanError(
                f"session {session_id.value} contract check #{index + 1} "
                f"'argv'[{arg_index}] must be a non-empty string"
            )
        argv.append(arg_value)

    raw_timeout = payload.get("timeout_seconds", DEFAULT_COMMAND_TIMEOUT_SECONDS)
    timeout = _require_positive_number(
        raw_timeout, label="timeout_seconds", session_id=session_id
    )
    if timeout > MAX_COMMAND_TIMEOUT_SECONDS:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'timeout_seconds' must be <= {MAX_COMMAND_TIMEOUT_SECONDS}"
        )

    raw_cwd = payload.get("cwd")
    if raw_cwd is not None and (not isinstance(raw_cwd, str) or raw_cwd == ""):
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'cwd' must be a non-empty string when provided"
        )

    repo = _repository_alias(
        payload,
        index=index,
        session_id=session_id,
        repository_registry=repository_registry,
    )
    if repository_registry is not None and raw_cwd is not None:
        _validate_repository_relative_value(
            raw_cwd, field="cwd", index=index, session_id=session_id
        )

    name = _check_name(payload.get("name"), index=index, session_id=session_id)
    baseline = _baseline_expectation(payload, index=index, session_id=session_id)
    on_failure_of = _on_failure_of(payload, index=index, session_id=session_id)
    review_replay = _review_replay(payload, index=index, session_id=session_id)
    return CommandCheck(
        name=name,
        argv=tuple(argv),
        timeout_seconds=timeout,
        cwd=raw_cwd,
        baseline_expectation=baseline,
        on_failure_of=on_failure_of,
        repo=repo,
        review_replay=review_replay,
    )


def _parse_file_exists_check(
    payload: dict,
    *,
    index: int,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> FileExistsCheck:
    path = _require_str(payload.get("path"), label="path", session_id=session_id)
    repo = _repository_alias(
        payload,
        index=index,
        session_id=session_id,
        repository_registry=repository_registry,
    )
    if repository_registry is not None:
        _validate_repository_relative_value(
            path, field="path", index=index, session_id=session_id
        )
    name = _check_name(payload.get("name"), index=index, session_id=session_id)
    baseline = _baseline_expectation(payload, index=index, session_id=session_id)
    on_failure_of = _on_failure_of(payload, index=index, session_id=session_id)
    review_replay = _review_replay(payload, index=index, session_id=session_id)
    return FileExistsCheck(
        name=name,
        path=path,
        baseline_expectation=baseline,
        on_failure_of=on_failure_of,
        repo=repo,
        review_replay=review_replay,
    )


def _parse_regex_in_file_check(
    payload: dict,
    *,
    index: int,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> RegexInFileCheck:
    path = _require_str(payload.get("path"), label="path", session_id=session_id)
    repo = _repository_alias(
        payload,
        index=index,
        session_id=session_id,
        repository_registry=repository_registry,
    )
    if repository_registry is not None:
        _validate_repository_relative_value(
            path, field="path", index=index, session_id=session_id
        )
    pattern = _require_str(
        payload.get("pattern"), label="pattern", session_id=session_id
    )
    try:
        re.compile(pattern)
    except re.error as error:
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'pattern' is not a valid regex: {error}"
        ) from error
    name = _check_name(payload.get("name"), index=index, session_id=session_id)
    baseline = _baseline_expectation(payload, index=index, session_id=session_id)
    on_failure_of = _on_failure_of(payload, index=index, session_id=session_id)
    review_replay = _review_replay(payload, index=index, session_id=session_id)
    return RegexInFileCheck(
        name=name,
        path=path,
        pattern=pattern,
        baseline_expectation=baseline,
        on_failure_of=on_failure_of,
        repo=repo,
        review_replay=review_replay,
    )


def check_payload(check: VerificationCheck) -> dict[str, Any]:
    """Serialize a parsed check back into the payload a contract declares.

    The inverse of :func:`_parse_check`, so a check read from one session can be
    re-emitted into another session's recipe without the caller reconstructing
    the payload shape by hand. Defaulted fields are omitted, which keeps a
    round trip byte-identical for the common case.
    """
    payload: dict[str, Any] = {"type": check.kind, "name": check.name}
    if isinstance(check, CommandCheck):
        payload["argv"] = list(check.argv)
        payload["timeout_seconds"] = check.timeout_seconds
        if check.cwd is not None:
            payload["cwd"] = check.cwd
    else:
        payload["path"] = check.path
        if isinstance(check, RegexInFileCheck):
            payload["pattern"] = check.pattern
    if check.baseline_expectation != DEFAULT_BASELINE_EXPECTATION:
        payload["baseline_expectation"] = check.baseline_expectation
    if check.on_failure_of:
        payload["on_failure_of"] = list(check.on_failure_of)
    if check.review_replay:
        payload["review_replay"] = True
    if check.repo is not None:
        payload["repo"] = check.repo
    return payload


CHECK_PARSERS: dict[str, Callable[..., VerificationCheck]] = {
    "command": _parse_command_check,
    "file_exists": _parse_file_exists_check,
    "regex_in_file": _parse_regex_in_file_check,
}


#: Check fields no contract may declare. ``kind`` is the parsed spelling of the
#: payload's ``type``; ``generated_gate`` is provenance the runner stamps in
#: process, and a payload that could set it would be a session file asserting
#: its own authorship.
_INTERNAL_CHECK_FIELDS = frozenset({"kind", "generated_gate"})


def _payload_keys(check_type: type[VerificationCheck]) -> frozenset[str]:
    """The payload keys that survive a parse into ``check_type``.

    Derived from the dataclass rather than listed, so a field added to a check
    and its parser is accepted without a second edit here, minus the fields no
    payload may carry.
    """
    return frozenset(
        {field.name for field in fields(check_type)} - _INTERNAL_CHECK_FIELDS
    ) | {"type"}


#: Keys each check type accepts. Anything else is rejected before dispatch: a
#: key no parser reads is dropped by :func:`check_payload` and never reaches
#: the executor, so tolerating one lets a check claim semantics it does not
#: have and run as its inverse.
_ALLOWED_CHECK_KEYS: dict[str, frozenset[str]] = {
    "command": _payload_keys(CommandCheck),
    "file_exists": _payload_keys(FileExistsCheck),
    "regex_in_file": _payload_keys(RegexInFileCheck),
}

#: What the file-reading check types actually assert, stated in the rejection
#: because the tempting unknown key is an absence flag.
CHECK_PRESENCE_SEMANTICS: dict[str, str] = {
    "file_exists": "file_exists asserts the path EXISTS and has no absence semantics",
    "regex_in_file": (
        "regex_in_file asserts the pattern is PRESENT and has no absence semantics"
    ),
}


def _parse_check(
    payload: object,
    *,
    index: int,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> VerificationCheck:
    if not isinstance(payload, dict):
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "must be a JSON object"
        )
    raw_type = payload.get("type")
    if raw_type not in CHECK_PARSERS:
        allowed = ", ".join(sorted(CHECK_PARSERS))
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            f"'type' must be one of: {allowed}; got {raw_type!r}"
        )
    allowed_keys = _ALLOWED_CHECK_KEYS[raw_type]
    unknown = sorted(key for key in payload if key not in allowed_keys)
    if unknown:
        raw_name = payload.get("name")
        named = (
            f" {raw_name.strip()!r}"
            if isinstance(raw_name, str) and raw_name.strip()
            else ""
        )
        offending = ", ".join(repr(key) for key in unknown)
        message = (
            f"session {session_id.value} contract check #{index + 1}{named} "
            f"does not support key(s): {offending}; a {raw_type} check allows "
            f"only: {', '.join(sorted(allowed_keys))}"
        )
        semantics = CHECK_PRESENCE_SEMANTICS.get(raw_type)
        if semantics is not None:
            message = f"{message}; {semantics}"
        raise PlanError(message)
    return CHECK_PARSERS[raw_type](
        payload,
        index=index,
        session_id=session_id,
        repository_registry=repository_registry,
    )


def _parse_verify_payload(
    payload: object,
    *,
    session_id: SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> VerifyRecipe:
    if not isinstance(payload, dict):
        raise PlanError(
            f"session {session_id.value} Verify JSON must be a top-level object"
        )

    raw_session = payload.get("session")
    if raw_session is not None:
        try:
            declared_id = parse_session_id(raw_session)
        except ValueError as error:
            raise PlanError(
                f"session {session_id.value} Verify JSON 'session' is not a "
                f"valid session id: {error}"
            ) from error
        if declared_id != session_id:
            raise PlanError(
                f"session {session_id.value} Verify JSON 'session' is "
                f"{declared_id.value!r}; expected {session_id.value!r}"
            )

    raw_parallel = payload.get("parallel", False)
    if not isinstance(raw_parallel, bool):
        raise PlanError(
            f"session {session_id.value} Verify JSON 'parallel' must be a "
            f"boolean; got {raw_parallel!r}"
        )

    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or len(raw_checks) == 0:
        raise PlanError(
            f"session {session_id.value} Verify JSON 'checks' must be a non-empty list"
        )

    checks: list[VerificationCheck] = []
    seen_names: set[str] = set()
    for index, raw_check in enumerate(raw_checks):
        check = _parse_check(
            raw_check,
            index=index,
            session_id=session_id,
            repository_registry=repository_registry,
        )
        if check.name in seen_names:
            raise PlanError(
                f"session {session_id.value} contract check name "
                f"{check.name!r} is duplicated"
            )
        seen_names.add(check.name)
        checks.append(check)

    checks_by_name = {check.name: check for check in checks}
    gate_names = {check.name for check in checks if not check.on_failure_of}
    if not gate_names:
        raise PlanError(
            f"session {session_id.value} Verify JSON must contain at least one "
            "unconditional gate check"
        )
    for check in checks:
        if check.review_replay and check.on_failure_of:
            raise PlanError(
                f"session {session_id.value} contract check {check.name!r} "
                "cannot set 'review_replay' to true on a diagnostic "
                "'on_failure_of' check"
            )
        for dependency in check.on_failure_of:
            if dependency == check.name:
                raise PlanError(
                    f"session {session_id.value} contract check {check.name!r} "
                    "cannot name itself in 'on_failure_of'"
                )
            target = checks_by_name.get(dependency)
            if target is None:
                raise PlanError(
                    f"session {session_id.value} contract check {check.name!r} "
                    f"'on_failure_of' references unknown check {dependency!r}"
                )
            if target.on_failure_of:
                raise PlanError(
                    f"session {session_id.value} contract check {check.name!r} "
                    f"'on_failure_of' target {dependency!r} is diagnostic; "
                    "diagnostics may depend only on unconditional gates"
                )

    return VerifyRecipe(checks=tuple(checks), parallel=raw_parallel)


def parse_completion_contract(
    text: str,
    *,
    session_id: int | str | SessionId,
    repository_registry: RepositoryRegistry | None = None,
) -> CompletionContract | None:
    """Parse a session file body and return its completion contract, if any.

    Returns ``None`` if the session text contains no ``## Completion Contract``
    section. Raises :class:`PlanError` for any malformed contract, and for
    duplicate contracts: silently selecting one would run an unintended gate.
    """
    canonical_id = parse_session_id(session_id)
    lines = text.splitlines(keepends=False)

    handoff_start = _final_handoff_start(lines)
    end = handoff_start if handoff_start is not None else len(lines)

    starts = completion_contract_heading_indices(lines, end)
    if not starts:
        return None
    if len(starts) > 1:
        locations = ", ".join(str(index + 1) for index in starts)
        raise PlanError(
            f"session {canonical_id.value} has {len(starts)} "
            f"{COMPLETION_CONTRACT_HEADING!r} sections (lines {locations}); "
            "exactly one is allowed"
        )
    contract_start = starts[0]

    contract_end = _section_end(lines, contract_start, max_level=2, end=end)
    section = lines[contract_start + 1 : contract_end]

    done_start = _find_heading_index(section, DONE_WHEN_HEADING, 0, len(section))
    if done_start is None:
        raise PlanError(
            f"session {canonical_id.value} contract missing "
            f"{DONE_WHEN_HEADING!r} subsection"
        )
    done_end = _section_end(section, done_start, max_level=3, end=len(section))
    done_lines = section[done_start + 1 : done_end]
    done_when = tuple(_extract_bullets(done_lines))
    if not done_when:
        raise PlanError(
            f"session {canonical_id.value} contract "
            f"{DONE_WHEN_HEADING!r} must list at least one bullet"
        )

    verify_start = _find_heading_index(section, VERIFY_HEADING, 0, len(section))
    if verify_start is None:
        raise PlanError(
            f"session {canonical_id.value} contract missing "
            f"{VERIFY_HEADING!r} subsection"
        )
    verify_end = _section_end(section, verify_start, max_level=3, end=len(section))
    verify_lines = section[verify_start + 1 : verify_end]

    json_text = _extract_json_block(verify_lines, canonical_id)
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise PlanError(
            f"session {canonical_id.value} Verify JSON is invalid: {error.msg} "
            f"at line {error.lineno} column {error.colno}"
        ) from error

    recipe = _parse_verify_payload(
        payload,
        session_id=canonical_id,
        repository_registry=repository_registry,
    )
    return CompletionContract(
        session_id=canonical_id,
        done_when=done_when,
        verify=recipe,
    )


def baseline_check_budget_seconds(check: VerificationCheck) -> float:
    """Return the per-check baseline execution budget, in seconds.

    Command checks are clamped to :data:`DEFAULT_BASELINE_CHECK_BUDGET_SECONDS`
    so a generous per-session timeout cannot exceed the baseline budget. File
    and regex checks have no command timeout and use the full check budget.
    """
    timeout = getattr(check, "timeout_seconds", DEFAULT_BASELINE_CHECK_BUDGET_SECONDS)
    return min(float(timeout), DEFAULT_BASELINE_CHECK_BUDGET_SECONDS)


def recipe_total_budget_seconds(recipe: VerifyRecipe) -> float:
    """Return the total baseline budget for a recipe, in seconds.

    The sum of per-check budgets is clamped to
    :data:`DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS`.
    """
    total = sum(baseline_check_budget_seconds(check) for check in recipe.checks)
    return min(float(total), DEFAULT_BASELINE_TOTAL_BUDGET_SECONDS)


def read_completion_contract(
    plan_dir: str | Path, session_id: int | str | SessionId
) -> CompletionContract | None:
    """Read and parse the completion contract for a session in a plan directory."""
    canonical_id = parse_session_id(session_id)
    canonical_dir = canonicalize_path(plan_dir)
    session_path = canonical_dir / session_id_to_filename(canonical_id)
    try:
        text = session_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise PlanError(
            f"missing session file for session {canonical_id.value}: {session_path}"
        ) from error
    except OSError as error:
        raise PlanError(f"cannot read {session_path}: {error}") from error
    registry = read_repository_registry(canonical_dir)
    return parse_completion_contract(
        text,
        session_id=canonical_id,
        repository_registry=registry,
    )
