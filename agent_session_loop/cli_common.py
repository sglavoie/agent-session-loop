"""Shared helpers for plan-scoped command-line producers.

Plan-scoped commands accept either a split-plan directory or the compact label
shown by ``asl sessions``.  Keeping resolution and failure reporting here
prevents producer surfaces such as ``asl control`` and the mailbox commands
from drifting into subtly different JSON envelopes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
from typing import Any

from . import plans, responses, state


@dataclass(frozen=True)
class PlanCommandContext:
    """A validated plan plus its current status projection."""

    snapshot: plans.PlanSnapshot
    status: dict[str, Any]


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
    """Print one deterministic error using the common command envelope."""
    if args.json:
        responses.print_json(
            responses.error_payload(
                command_name,
                reason=reason,
                error=error,
                paused=False,
                details=details,
                plan_dir=str(args.plan_dir),
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
        resolved_plan_dir = state.resolve_plan_reference(
            args.plan_dir, state.state_root(args.state_dir)
        )
        snapshot = plans.inspect_plan(resolved_plan_dir)
        status = state.status_payload(snapshot, args.state_dir)
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
