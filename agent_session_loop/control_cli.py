"""CLI producer surface for the durable per-plan control queue.

``asl control`` is the programmatic equivalent of the interactive run-control
keys: it appends a command to the same ``control-queue.jsonl`` the Workbench
HTTP API feeds, and the active runner applies it at its next drain checkpoint.
This matters most for ``--json`` supervised runs, where the keyboard listener is
deliberately disabled (``run_control.should_enable_run_controls``) so stdout
stays machine-readable.

Handlers never touch ``state.json``: they enqueue and let the runner own the
transition. Reporting reuses ``state.status_payload`` so the "will this take
effect?" answer is derived from the same lock/pending view ``asl status`` shows.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import sys
from typing import Any

from . import control_queue, harnesses, plans, responses, state


# Command names used in both success and error payloads, so a supervisor can
# branch on `command` without inspecting argv.
COMMAND_STOP_AFTER_CURRENT = "control-stop-after-current"
COMMAND_SELECT_HARNESS = "control-select-harness"

# Shared note for both subcommands: enqueueing always succeeds, but nothing
# consumes the queue until some runner drains it.
NO_ACTIVE_RUN_NOTE = (
    "no active run; command applies when a runner next drains the queue"
)
STOP_WILL_APPLY_NOTE = "will stop after current session"
STOP_NOOP_NOTE = "current session is the last; stop is a no-op"


def _print_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    reason: str,
    error: str,
    details: dict[str, Any] | None = None,
    hint: str | None = None,
) -> None:
    """Report a failure using the ``status_from_args`` conventions."""
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


def _run_active(status: dict[str, Any]) -> bool:
    lock = status["lock"]
    return bool(lock["present"] and lock["active"])


def _pending_after_current(status: dict[str, Any]) -> int:
    """Count sessions still pending once the executing one finishes.

    A session that is mid-flight is still recorded as ``pending`` in its handoff
    (the runner only marks it complete afterwards), so subtract it when a turn
    is active. ``runner._consume_run_control_stop`` declines to stop when
    ``snapshot.next_pending`` is empty at the drain checkpoint, which is exactly
    the ``0`` case callers surface as a no-op.
    """
    pending = int(status["counts"]["pending"])
    if status.get("active_turn") is not None:
        pending -= 1
    return max(0, pending)


def _enqueue_and_report(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    queue_command: control_queue.CommandName,
    harness: str | None = None,
    report: Callable[
        [dict[str, Any], control_queue.ControlCommand],
        tuple[dict[str, Any], str],
    ],
) -> int:
    """Resolve the plan, enqueue one command, then report whether it applies.

    ``report`` receives the pre-enqueue ``status_payload`` and the queued
    command and returns the subcommand-specific payload fields plus the one-line
    human summary.
    """
    try:
        resolved_plan_dir = state.resolve_plan_reference(
            args.plan_dir, state.state_root(args.state_dir)
        )
        snapshot = plans.inspect_plan(resolved_plan_dir)
        status = state.status_payload(snapshot, args.state_dir)
    except state.AmbiguousPlanError as error:
        _print_error(
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
        return 1
    except (plans.PlanError, state.StateError) as error:
        _print_error(
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
        return 1

    # Resolved before the append so the failure path can name the file it could
    # not write. This is pure path math (`realpath` on a possibly-absent path),
    # so it cannot itself fail with the OSError handled below.
    queue_path = control_queue.queue_path_for_plan(snapshot.plan_dir, args.state_dir)

    try:
        queued = control_queue.append_command(
            snapshot.plan_dir,
            queue_command,
            state_dir=args.state_dir,
            harness=harness,
        )
    except control_queue.ControlQueueError as error:
        _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-command",
            error=str(error),
        )
        return 1
    except OSError as error:
        # Queue setup performs mkdir/open/flock/write, any of which can fail on
        # an unwritable or structurally invalid state directory. `--json` is a
        # programmatic surface, so those failures have to land as one error
        # object with a stable reason rather than a traceback on stderr.
        _print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="os-error",
            error=f"cannot write control queue {queue_path}: {error}",
            details={"queue_path": str(queue_path)},
            hint="check that the state directory is writable",
        )
        return 1

    fields, summary = report(status, queued)
    payload = responses.success_payload(
        command_name,
        plan_dir=str(snapshot.plan_dir),
        queue_path=str(queue_path),
        enqueued=True,
        command_id=queued.id,
        **fields,
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(summary)
    return 0


def control_stop_after_current_from_args(args: argparse.Namespace) -> int:
    """Request a graceful stop after the current session finishes."""

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        run_active = _run_active(status)
        pending_after_current = _pending_after_current(status)
        if not run_active:
            note = NO_ACTIVE_RUN_NOTE
        elif pending_after_current == 0:
            note = STOP_NOOP_NOTE
        else:
            note = STOP_WILL_APPLY_NOTE
        fields = {
            "run_active": run_active,
            "pending_after_current": pending_after_current,
            "note": note,
        }
        summary = (
            f"Queued stop-after-current for {status['plan_dir']} "
            f"(command {queued.id}); {note}"
        )
        return fields, summary

    return _enqueue_and_report(
        args,
        command_name=COMMAND_STOP_AFTER_CURRENT,
        text_label="control stop-after-current",
        queue_command=control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
        report=report,
    )


def control_select_harness_from_args(args: argparse.Namespace) -> int:
    """Select the harness future sessions of a run should use."""
    harness = args.harness
    # Defense in depth: argparse `choices` and `control_queue.append_command`
    # both reject unknown harnesses, but direct handler callers get the same
    # stable `invalid-harness` reason the Workbench API returns.
    if harness not in harnesses.HARNESS_NAMES:
        _print_error(
            args,
            command_name=COMMAND_SELECT_HARNESS,
            text_label="control select-harness",
            reason="invalid-harness",
            error=f"unsupported harness {harness!r}",
            details={"harness": harness},
        )
        return 1

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        run_active = _run_active(status)
        note = (
            f"future sessions will use the {harness} harness"
            if run_active
            else NO_ACTIVE_RUN_NOTE
        )
        fields = {
            "harness": harness,
            "run_active": run_active,
            "note": note,
        }
        summary = (
            f"Queued select-harness {harness} for {status['plan_dir']} "
            f"(command {queued.id}); {note}"
        )
        return fields, summary

    return _enqueue_and_report(
        args,
        command_name=COMMAND_SELECT_HARNESS,
        text_label="control select-harness",
        queue_command=control_queue.COMMAND_SELECT_FUTURE_HARNESS,
        harness=harness,
        report=report,
    )
