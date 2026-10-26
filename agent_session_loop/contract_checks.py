"""Validation and serialization of individual completion-contract checks."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import fields
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .contract_models import (
    BASELINE_EXPECTATIONS,
    DEFAULT_BASELINE_EXPECTATION,
    DEFAULT_COMMAND_TIMEOUT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    BaselineExpectation,
    CommandCheck,
    FileExistsCheck,
    RegexInFileCheck,
    VerificationCheck,
)
from .literals import as_literal
from .plans_errors import PlanError
from .plans_session_ids import SessionId
from .workspaces import RepositoryRegistry, WorkspaceError, validate_repository_alias


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
    try:
        numeric = float(value)
    except OverflowError:
        # An integer too large for a float is still a positive number; the
        # caller's upper bound refuses it by name.
        numeric = float("inf") if value > 0 else float("-inf")
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
    return as_literal(
        raw,
        BASELINE_EXPECTATIONS,
        f"session {session_id.value} contract check #{index + 1} "
        "'baseline_expectation'",
        error=PlanError,
    )


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
    if not isinstance(raw_type, str) or raw_type not in CHECK_PARSERS:
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
