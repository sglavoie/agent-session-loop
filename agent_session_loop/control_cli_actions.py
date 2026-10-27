"""Stop, abort, harness, and model control handlers."""

from __future__ import annotations

import argparse
from typing import Any

from . import cli_common, control_queue, events, harnesses
from .cli_parser_support import flag_refusal
from .control_cli_enqueue import _enqueue_and_report
from .control_cli_status import (
    _pending_after_current,
    _review_continuation_pending,
    _run_active,
    _status_finalizing,
    _turn_active,
)
from .control_cli_vocab import (
    ABORT_CANCELS_FINALIZATION_NOTE,
    ABORT_DEGRADES_NOTE,
    ABORT_WILL_APPLY_NOTE,
    COMMAND_ABORT,
    COMMAND_SELECT_HARNESS,
    COMMAND_SET_MODEL,
    COMMAND_STOP_AFTER_CURRENT,
    MODEL_APPLIES_TO,
    NO_ACTIVE_RUN_NOTE,
    STOP_BEFORE_REVIEW_NOTE,
    STOP_NOOP_NOTE,
    STOP_WILL_APPLY_NOTE,
)


def control_stop_after_current_from_args(args: argparse.Namespace) -> int:
    """Request a graceful stop after the current session finishes."""

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        run_active = _run_active(status)
        pending_after_current = _pending_after_current(status)
        review_continuation_pending = run_active and _review_continuation_pending(
            status
        )
        if not run_active:
            note = NO_ACTIVE_RUN_NOTE
        elif pending_after_current > 0:
            note = STOP_WILL_APPLY_NOTE
        elif review_continuation_pending:
            note = STOP_BEFORE_REVIEW_NOTE
        else:
            note = STOP_NOOP_NOTE
        fields = {
            "run_active": run_active,
            "pending_after_current": pending_after_current,
            "review_continuation_pending": review_continuation_pending,
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


def control_abort_from_args(args: argparse.Namespace) -> int:
    """Abort the in-flight turn after a bounded grace window."""
    grace_seconds = getattr(args, "grace", None)
    reason = getattr(args, "reason", None)
    if grace_seconds is None:
        grace_seconds = control_queue.DEFAULT_ABORT_GRACE_SECONDS

    # Defense in depth: `control_queue.append_command` rejects the same values,
    # but a direct handler caller gets the stable reason instead of the generic
    # `invalid-command` the enqueue failure path reports.
    if (
        isinstance(grace_seconds, bool)
        or not isinstance(grace_seconds, int)
        or not (
            control_queue.MIN_ABORT_GRACE_SECONDS
            <= grace_seconds
            <= control_queue.MAX_ABORT_GRACE_SECONDS
        )
    ):
        cli_common.print_error(
            args,
            command_name=COMMAND_ABORT,
            text_label="control abort",
            reason=control_queue.REJECT_INVALID_GRACE,
            error=flag_refusal(
                "--grace",
                "must be a whole number of seconds between "
                f"{control_queue.MIN_ABORT_GRACE_SECONDS} and "
                f"{control_queue.MAX_ABORT_GRACE_SECONDS}",
                grace_seconds,
            ),
            details={"grace_seconds": repr(grace_seconds)},
        )
        return 1
    if reason is not None and (not isinstance(reason, str) or reason == ""):
        cli_common.print_error(
            args,
            command_name=COMMAND_ABORT,
            text_label="control abort",
            reason=control_queue.REJECT_INVALID_REASON,
            error="--reason must be non-empty text when provided",
            details={"reason": repr(reason)},
        )
        return 1

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        run_active = _run_active(status)
        turn_active = run_active and _turn_active(status)
        if not run_active:
            note = NO_ACTIVE_RUN_NOTE
        elif turn_active:
            note = ABORT_WILL_APPLY_NOTE
        elif _status_finalizing(status):
            note = ABORT_CANCELS_FINALIZATION_NOTE
        else:
            note = ABORT_DEGRADES_NOTE
        fields = {
            "run_active": run_active,
            "turn_active": turn_active,
            "grace_seconds": queued.grace_seconds,
            "reason": queued.reason,
            "note": note,
        }
        summary = f"Queued abort for {status['plan_dir']} (command {queued.id}); {note}"
        return fields, summary

    return _enqueue_and_report(
        args,
        command_name=COMMAND_ABORT,
        text_label="control abort",
        queue_command=control_queue.COMMAND_REQUEST_ABORT_TURN,
        grace_seconds=grace_seconds,
        reason=reason,
        terminal_actions=(
            events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            # The turn timeout or an interrupt ended the turn first; that is an
            # outcome, but not the one asked for, so it fails the wait below.
            events.RUN_CONTROL_ACTION_ABORT_TURN_PREEMPTED,
            # Sent between turns there is nothing to kill: the abort becomes
            # stop-after-current, and the wait then follows the run to its end.
            events.RUN_CONTROL_ACTION_ABORT_TURN_DEGRADED,
            # An abort sent while the run is already finalizing has no turn to
            # end, so this is the only outcome its `--wait` can be answered by.
            events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
        ),
        failure_actions=(events.RUN_CONTROL_ACTION_ABORT_TURN_PREEMPTED,),
        report=report,
    )


def control_select_harness_from_args(args: argparse.Namespace) -> int:
    """Select the harness future sessions of a run should use."""
    harness = args.harness
    # Defense in depth: argparse `choices` and `control_queue.append_command`
    # both reject unknown harnesses, but direct handler callers get the same
    # stable `invalid-harness` reason the Workbench API returns.
    if harness not in harnesses.HARNESS_NAMES:
        cli_common.print_error(
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


def control_set_model_from_args(args: argparse.Namespace) -> int:
    """Override or clear model/effort for a future implementation turn."""
    harness = getattr(args, "harness", None)
    harness_model = getattr(args, "harness_model", None)
    clear_harness_model = bool(getattr(args, "clear_harness_model", False))
    effort = getattr(args, "effort", None)
    clear_effort = bool(getattr(args, "clear_effort", False))

    if harness is not None and harness not in harnesses.HARNESS_NAMES:
        cli_common.print_error(
            args,
            command_name=COMMAND_SET_MODEL,
            text_label="control set-model",
            reason="invalid-harness",
            error=f"unsupported harness {harness!r}",
            details={"harness": harness},
        )
        return 1
    if (
        harness_model is None
        and not clear_harness_model
        and effort is None
        and not clear_effort
    ):
        cli_common.print_error(
            args,
            command_name=COMMAND_SET_MODEL,
            text_label="control set-model",
            reason=control_queue.REJECT_MISSING_MODEL_CONTROL_OPERATION,
            error=(
                "set-model requires at least one model or effort set/clear operation"
            ),
        )
        return 1
    # The queue rejects an empty value too, but only with its bare code; refuse
    # here so the reader gets a sentence and `reason` keeps that code.
    for flag, value, code, clear_flag in (
        (
            "--harness-model",
            harness_model,
            control_queue.REJECT_INVALID_MODEL,
            "--clear-harness-model",
        ),
        ("--effort", effort, control_queue.REJECT_INVALID_EFFORT, "--clear-effort"),
    ):
        if value is not None and not value.strip():
            cli_common.print_error(
                args,
                command_name=COMMAND_SET_MODEL,
                text_label="control set-model",
                reason=code,
                error=(
                    f"{flag} needs a non-empty value; pass {clear_flag} to "
                    "drop the override instead"
                ),
            )
            return 1

    def report(
        status: dict[str, Any], queued: control_queue.ControlCommand
    ) -> tuple[dict[str, Any], str]:
        run_active = _run_active(status)
        target = harness or "the selected implementation harness"
        note = (
            f"will apply to the next implementation turn for {target}"
            if run_active
            else NO_ACTIVE_RUN_NOTE
        )
        fields = {
            "harness": harness,
            "harness_model": harness_model,
            "clear_harness_model": clear_harness_model,
            "effort": effort,
            "clear_effort": clear_effort,
            "run_active": run_active,
            "applies_to": MODEL_APPLIES_TO,
            "note": note,
        }
        summary = (
            f"Queued set-model for {status['plan_dir']} (command {queued.id}); {note}"
        )
        return fields, summary

    return _enqueue_and_report(
        args,
        command_name=COMMAND_SET_MODEL,
        text_label="control set-model",
        queue_command=control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
        harness=harness,
        harness_model=harness_model,
        clear_harness_model=clear_harness_model,
        effort=effort,
        clear_effort=clear_effort,
        report=report,
    )
