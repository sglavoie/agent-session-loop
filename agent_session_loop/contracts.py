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

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal, Union

from .plans import (
    HANDOFF_HEADING,
    PlanError,
    SessionId,
    canonicalize_path,
    parse_session_id,
    session_id_to_filename,
)


COMPLETION_CONTRACT_HEADING = "## Completion Contract"
DONE_WHEN_HEADING = "### Done when"
VERIFY_HEADING = "### Verify"

CommandKind = Literal["command"]
FileExistsKind = Literal["file_exists"]
RegexInFileKind = Literal["regex_in_file"]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0
MAX_COMMAND_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True)
class CommandCheck:
    """A check that runs a command (no shell) and asserts it exits 0."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float
    cwd: str | None = None
    kind: CommandKind = "command"


@dataclass(frozen=True)
class FileExistsCheck:
    """A check that asserts a path exists relative to the repo root."""

    name: str
    path: str
    kind: FileExistsKind = "file_exists"


@dataclass(frozen=True)
class RegexInFileCheck:
    """A check that asserts a regex matches somewhere in a file."""

    name: str
    path: str
    pattern: str
    kind: RegexInFileKind = "regex_in_file"


VerificationCheck = Union[CommandCheck, FileExistsCheck, RegexInFileCheck]


@dataclass(frozen=True)
class VerifyRecipe:
    """Ordered tuple of verification checks for a session."""

    checks: tuple[VerificationCheck, ...]


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


def _section_end(lines: list[str], start: int, max_level: int, end: int) -> int:
    for index in range(start + 1, end):
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


def _check_name(raw_name: object, *, index: int, session_id: SessionId) -> str:
    if raw_name is None:
        return f"check-{index + 1}"
    if not isinstance(raw_name, str) or raw_name.strip() == "":
        raise PlanError(
            f"session {session_id.value} contract check #{index + 1} "
            "'name' must be a non-empty string when provided"
        )
    return raw_name.strip()


def _parse_command_check(
    payload: dict, *, index: int, session_id: SessionId
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

    name = _check_name(payload.get("name"), index=index, session_id=session_id)
    return CommandCheck(
        name=name,
        argv=tuple(argv),
        timeout_seconds=timeout,
        cwd=raw_cwd,
    )


def _parse_file_exists_check(
    payload: dict, *, index: int, session_id: SessionId
) -> FileExistsCheck:
    path = _require_str(payload.get("path"), label="path", session_id=session_id)
    name = _check_name(payload.get("name"), index=index, session_id=session_id)
    return FileExistsCheck(name=name, path=path)


def _parse_regex_in_file_check(
    payload: dict, *, index: int, session_id: SessionId
) -> RegexInFileCheck:
    path = _require_str(payload.get("path"), label="path", session_id=session_id)
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
    return RegexInFileCheck(name=name, path=path, pattern=pattern)


CHECK_PARSERS = {
    "command": _parse_command_check,
    "file_exists": _parse_file_exists_check,
    "regex_in_file": _parse_regex_in_file_check,
}


def _parse_check(
    payload: object, *, index: int, session_id: SessionId
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
    return CHECK_PARSERS[raw_type](payload, index=index, session_id=session_id)


def _parse_verify_payload(payload: object, *, session_id: SessionId) -> VerifyRecipe:
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

    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or len(raw_checks) == 0:
        raise PlanError(
            f"session {session_id.value} Verify JSON 'checks' must be a "
            "non-empty list"
        )

    checks: list[VerificationCheck] = []
    seen_names: set[str] = set()
    for index, raw_check in enumerate(raw_checks):
        check = _parse_check(raw_check, index=index, session_id=session_id)
        if check.name in seen_names:
            raise PlanError(
                f"session {session_id.value} contract check name "
                f"{check.name!r} is duplicated"
            )
        seen_names.add(check.name)
        checks.append(check)

    return VerifyRecipe(checks=tuple(checks))


def parse_completion_contract(
    text: str, *, session_id: int | str | SessionId
) -> CompletionContract | None:
    """Parse a session file body and return its completion contract, if any.

    Returns ``None`` if the session text contains no ``## Completion Contract``
    section. Raises :class:`PlanError` for any malformed contract.
    """
    canonical_id = parse_session_id(session_id)
    lines = text.splitlines(keepends=False)

    handoff_start = _final_handoff_start(lines)
    end = handoff_start if handoff_start is not None else len(lines)

    contract_start = _find_heading_index(lines, COMPLETION_CONTRACT_HEADING, 0, end)
    if contract_start is None:
        return None

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

    recipe = _parse_verify_payload(payload, session_id=canonical_id)
    return CompletionContract(
        session_id=canonical_id,
        done_when=done_when,
        verify=recipe,
    )


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
    return parse_completion_contract(text, session_id=canonical_id)
