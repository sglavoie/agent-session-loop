"""Shared helpers for plan-scoped command-line producers.

Plan-scoped commands accept either a split-plan directory or the compact label
shown by ``asl sessions``.  Keeping resolution and failure reporting here
prevents producer surfaces such as ``asl control`` and the mailbox commands
from drifting into subtly different JSON envelopes.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any

from . import plans, responses, state, state_plan_refs, state_status


@dataclass(frozen=True)
class PlanCommandContext:
    """A validated plan plus its current status projection."""

    snapshot: plans.PlanSnapshot
    status: dict[str, Any]


def read_stdin_text() -> str:
    """Read all of standard input as strict UTF-8 text.

    Shared by every producer that accepts a body on stdin so the awkward cases
    are decided once: an absent stream, a text-backed stream with no binary
    buffer (test and embedding harnesses), and undecodable bytes. Strict
    decoding in both branches is the point -- a surrogateescaped body would be
    persisted as text nothing can round-trip.

    Raises whatever the read or decode raises (``OSError``, ``TypeError``,
    ``UnicodeError``, ``ValueError``); each caller reports it under its own
    command's reason vocabulary rather than sharing one message.
    """
    if sys.stdin is None:
        raise ValueError("standard input is unavailable")
    stdin_stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stdin_stream.read()
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="strict")
    if isinstance(raw, str):
        raw.encode("utf-8", errors="strict")
        return raw
    raise TypeError("standard input did not return text or bytes")


def print_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    reason: str,
    error: str,
    details: dict[str, Any] | None = None,
    hint: str | None = None,
) -> None:
    """Print one deterministic error using the common command envelope.

    ``plan_reference`` is always the unresolved token supplied by the caller;
    successful command payloads reserve ``plan_dir`` for the canonical path.
    """
    if args.json:
        responses.print_json(
            responses.error_payload(
                command_name,
                reason=reason,
                error=error,
                paused=False,
                details=details,
                plan_reference=str(args.plan_dir),
            )
        )
        return
    message = f"agent-session-loop {text_label}: {error}"
    if hint is not None:
        message = f"{message}; {hint}"
    print(message, file=sys.stderr)


def resolve_plan_context(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> PlanCommandContext | None:
    """Resolve and inspect ``args.plan_dir``, reporting failures in place."""
    try:
        resolved_plan_dir = state_plan_refs.resolve_plan_reference(
            args.plan_dir, state.state_root(args.state_dir)
        )
        snapshot = plans.inspect_plan(resolved_plan_dir)
        status = state_status.status_payload(snapshot, args.state_dir)
    except state.AmbiguousPlanError as error:
        print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="ambiguous-plan",
            error=str(error),
            details={
                "state_root": str(error.state_root),
                "candidates": [str(candidate) for candidate in error.candidates],
            },
            hint="pass a full plan directory path to disambiguate",
        )
        return None
    except (plans.PlanError, state.StateError) as error:
        print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=(
                "malformed-plan"
                if isinstance(error, plans.PlanError)
                else "state-error"
            ),
            error=str(error),
        )
        return None
    except OSError as error:
        print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="os-error",
            error=str(error),
        )
        return None
    return PlanCommandContext(snapshot=snapshot, status=status)
