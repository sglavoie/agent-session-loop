"""Shared command JSON response helpers."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Exact string ``session-run`` prints once every session is implemented; the
# status ``next_command`` completion payload reuses it verbatim so the CLI and
# the skill agree on the wording (see ``skills/session-run/SKILL.md``).
ALL_SESSIONS_COMPLETE_MESSAGE = "All sessions implemented - nothing left to run."


def success_payload(command: str, **fields: Any) -> dict[str, Any]:
    """Build a successful command response while preserving command-specific keys."""
    payload: dict[str, Any] = {
        "ok": True,
        "command": command,
    }
    payload.update(fields)
    return payload


def error_payload(
    command: str,
    *,
    reason: str,
    error: str,
    paused: bool = False,
    details: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a failed command response with the common scriptable contract."""
    payload: dict[str, Any] = {
        "ok": False,
        "command": command,
        "reason": reason,
        "error": error,
        "paused": paused,
        "details": {} if details is None else details,
    }
    payload.update(fields)
    return payload


def invocation_step(description: str, invocations: Mapping[str, str]) -> dict[str, Any]:
    """Build one next step offered as ready-to-paste per-harness invocations.

    ``invocations`` maps a harness name to the exact command a user pastes into
    that harness (prefix already applied). The insertion order of
    ``invocations`` is preserved so callers can surface the most likely harness
    first.
    """
    return {"description": description, "invocations": dict(invocations)}


def format_command(argv: Sequence[str]) -> str:
    """Render an argv sequence as a shell-safe command line.

    Every element is quoted via :func:`shlex.join`, so any element containing
    whitespace or shell metacharacters round-trips as a single argument under
    :func:`shlex.split`. Ordinary tokens (plain paths, flags) are left
    untouched, so simple commands render exactly as a bare space-join would.
    This is the shared quoting builder every ``asl`` remediation/auto-repair
    payload uses instead of raw path interpolation, so an advertised command
    stays runnable even when a plan or repository path contains a space.
    """
    return shlex.join(argv)


def command_step(command: str) -> dict[str, Any]:
    """Build one next step offered as a single harness-agnostic command."""
    return {"command": command}


def next_steps_payload(steps: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Wrap next-step items in the shared ``{"next_steps": [...]}`` envelope.

    Each item is either an :func:`invocation_step` (``description`` +
    per-harness ``invocations``) or a :func:`command_step` (a single
    ``command``). Shared by ``asl status`` and the later session commands so
    every surface describes what to run next with one payload shape.
    """
    return {"next_steps": [dict(step) for step in steps]}


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))
