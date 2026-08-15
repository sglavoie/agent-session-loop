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

``--wait`` turns the fire-and-forget append into a receipt: the handler tails
``events.jsonl`` for the ``control-command-observed`` record carrying the ID it
just enqueued, the same way ``asl outbox wait`` tails the mailbox. Without it
the surface behaves exactly as it always has.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    cli_common,
    control_queue,
    control_signal,
    events,
    harnesses,
    responses,
    state,
)

# Command names used in both success and error payloads, so a supervisor can
# branch on `command` without inspecting argv.
COMMAND_STOP_AFTER_CURRENT = "control-stop-after-current"
COMMAND_ABORT = "control-abort"
COMMAND_SELECT_HARNESS = "control-select-harness"
COMMAND_SET_MODEL = "control-set-model"

# Shared note for both subcommands: enqueueing always succeeds, but nothing
# consumes the queue until some runner drains it.
NO_ACTIVE_RUN_NOTE = (
    "no active run; command applies when a runner next drains the queue"
)
STOP_WILL_APPLY_NOTE = "will stop after current session"
STOP_BEFORE_REVIEW_NOTE = (
    "current session is the last; stop also prevents the pending review"
)
STOP_NOOP_NOTE = "current session is the last; stop is a no-op"
ABORT_WILL_APPLY_NOTE = "a turn is active; abort will end it"
ABORT_DEGRADES_NOTE = "no turn active; abort degrades to stop-after-current"
ABORT_CANCELS_FINALIZATION_NOTE = (
    "no turn active; the run is finalizing, so abort cancels its terminal audit"
)
MODEL_APPLIES_TO = "next-implementation-turn"

# A wakeup usually makes the runner drain within milliseconds, so the receipt
# tail polls far more often than the mailbox waits do.
CONTROL_WAIT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS = 60.0
MAX_CONTROL_WAIT_TIMEOUT_SECONDS = 30.0 * 60.0

_EVENTS_TOKEN_UNSEEN = object()
_EVENTS_TOKEN_UNAVAILABLE = object()


FINALIZING_NOTE = (
    "the turn ended, but the run is still finalizing its terminal audit and "
    "holds the plan lock; repeat `asl control abort` to cancel the audit"
)


@dataclass(frozen=True)
class _ReceiptWait:
    """Outcome of one bounded wait for a queued command's receipt."""

    observed: bool
    terminal_action: str | None
    satisfied: bool
    timeout_seconds: float
    elapsed_seconds: float
    run_finished: bool = False
    finalizing: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "observed": self.observed,
            "terminal_action": self.terminal_action,
            "timeout_seconds": self.timeout_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "run_finished": self.run_finished,
        }
        if not self.run_finished and self.terminal_action is not None:
            payload["finalizing"] = self.finalizing
            payload["note"] = FINALIZING_NOTE
        return payload


def _events_change_token(events_path: Path) -> object:
    """Return cheap append-log metadata used to skip unchanged full reads."""
    try:
        stat_result = events_path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        # The probe is only an optimization; an unreadable stat must still lead
        # to a full read attempt rather than a silent "nothing changed".
        return _EVENTS_TOKEN_UNAVAILABLE
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


_WAIT_RECORD_NAMES = frozenset({events.RUN_CONTROL_EVENT, events.RUN_FINISHED_EVENT})


def _read_wait_records(events_path: Path) -> list[dict[str, Any]]:
    """Read the records a receipt wait reads, tolerating a torn tail.

    Both names matter: the run-control records carry the receipt and the
    turn-level outcome, and ``run_finished`` is the only proof that the run
    those records belong to has actually released the lock.
    """
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A line being appended right now is read whole on the next poll.
            continue
        if isinstance(record, dict) and record.get("name") in _WAIT_RECORD_NAMES:
            records.append(record)
    return records


@dataclass(frozen=True)
class _ReceiptScan:
    """What one pass over the event log found after the command's receipt."""

    observed: bool = False
    terminal_action: str | None = None
    run_finished: bool = False


def _scan_for_receipt(
    events_path: Path,
    *,
    command_id: str,
    terminal_actions: tuple[str, ...],
) -> _ReceiptScan:
    observed = False
    terminal_action: str | None = None
    for record in _read_wait_records(events_path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("name") == events.RUN_FINISHED_EVENT:
            if observed:
                return _ReceiptScan(
                    observed=True,
                    terminal_action=terminal_action,
                    run_finished=True,
                )
            continue
        action = payload.get("action")
        if not observed:
            if (
                action == events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
                and payload.get("command_id") == command_id
            ):
                observed = True
            continue
        # A terminal action carries no command ID, so only one recorded after
        # this command's receipt can be its outcome. The scan continues past it
        # because the turn ending is not the run ending.
        if terminal_action is None and action in terminal_actions:
            terminal_action = action
    return _ReceiptScan(observed=observed, terminal_action=terminal_action)


def _active_finalization(
    plan_dir: Path, state_dir: str | None
) -> dict[str, Any] | None:
    """Read the finalization phase a run is publishing, if it is in one."""
    state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    active = raw.get("active_finalization")
    return active if isinstance(active, dict) else None


def _wait_for_receipt(
    plan_dir: Path,
    *,
    state_dir: str | None,
    command_id: str,
    timeout: float,
    terminal_actions: tuple[str, ...],
) -> _ReceiptWait:
    """Tail ``events.jsonl`` until the command is observed, or the deadline.

    ``terminal_actions`` extends the wait past delivery: for an abort the
    supervisor wants the real outcome, so the wait continues until the runner
    records one of them after the receipt — and then past that, until the run
    itself finishes. The turn-level action only proves the turn ended; the run
    goes on to audit itself and keeps the plan lock while it does, so reporting
    the abort as done there is how a finalizing run gets mistaken for a hang.
    """
    started = time.monotonic()
    deadline = started + timeout
    events_path = state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
    last_read_token: object = _EVENTS_TOKEN_UNSEEN
    scan = _ReceiptScan()
    while True:
        current_token = _events_change_token(events_path)
        if (
            last_read_token is _EVENTS_TOKEN_UNSEEN
            or current_token is _EVENTS_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
            scan = _scan_for_receipt(
                events_path,
                command_id=command_id,
                terminal_actions=terminal_actions,
            )
            last_read_token = current_token
            if scan.observed and (
                not terminal_actions
                or (scan.terminal_action is not None and scan.run_finished)
            ):
                return _ReceiptWait(
                    observed=True,
                    terminal_action=scan.terminal_action,
                    satisfied=True,
                    timeout_seconds=timeout,
                    elapsed_seconds=time.monotonic() - started,
                    run_finished=scan.run_finished,
                )

        now = time.monotonic()
        if now >= deadline:
            return _ReceiptWait(
                observed=scan.observed,
                terminal_action=scan.terminal_action,
                satisfied=False,
                timeout_seconds=timeout,
                elapsed_seconds=now - started,
                run_finished=scan.run_finished,
                finalizing=(
                    _active_finalization(plan_dir, state_dir)
                    if scan.terminal_action is not None
                    else None
                ),
            )
        time.sleep(min(CONTROL_WAIT_POLL_INTERVAL_SECONDS, deadline - now))


def _validated_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> float | None:
    """Resolve ``--timeout``, reporting a rejection in place.

    Returns ``None`` when the value is unusable, which the caller turns into
    exit 1 before anything is enqueued.
    """
    requested = getattr(args, "wait_timeout", None)
    if not getattr(args, "wait", False):
        if requested is not None:
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason="invalid-timeout",
                error="--timeout requires --wait",
            )
            return None
        return DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS
    try:
        timeout = (
            DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS
            if requested is None
            else float(requested)
        )
    except TypeError, ValueError:
        timeout = math.nan
    if (
        not math.isfinite(timeout)
        or timeout < 0
        or timeout > MAX_CONTROL_WAIT_TIMEOUT_SECONDS
    ):
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error=(
                "--timeout must be between 0 and "
                f"{int(MAX_CONTROL_WAIT_TIMEOUT_SECONDS)} seconds"
            ),
        )
        return None
    return timeout


def _report_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    command_id: str,
    queue_path: Path,
    signal: control_signal.NotifyResult,
    run_active: bool,
    wait: _ReceiptWait,
) -> None:
    """Explain a receipt wait that ran out, distinguishing why.

    The command is durable either way, so the failure is about observation: no
    runner was listening, a listening runner did not drain in time, it drained
    but the abort it started has not resolved yet, or the abort ended the turn
    and the run is still finalizing. Only the last of those describes a run
    that is doing bounded work rather than one that may be stuck.
    """
    details = {
        "queue_path": str(queue_path),
        "command_id": command_id,
        "enqueued": True,
        "signal": signal.to_payload(),
        "wait": wait.to_payload(),
    }
    window = f"{wait.timeout_seconds:g}s"
    if not wait.observed and (
        not run_active or signal.reason == control_signal.REASON_NO_RUNNER
    ):
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="no-runner",
            error=(
                f"no runner is listening, so command {command_id} was not "
                f"observed within {window}"
            ),
            details=details,
            hint="the command stays queued for the next runner that drains it",
        )
        return
    if not wait.observed:
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="receipt-timeout",
            error=(
                f"the runner holding the plan lock did not drain command "
                f"{command_id} within {window}"
            ),
            details=details,
            hint="the command stays queued; retry with a longer --timeout",
        )
        return
    if wait.terminal_action is None:
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="abort-timeout",
            error=(
                f"the runner observed command {command_id} but recorded no abort "
                f"outcome within {window}"
            ),
            details=details,
            hint="the grace window may outlast --timeout; the abort is still armed",
        )
        return
    cli_common.print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason="finalization-timeout",
        error=(
            f"the runner recorded {wait.terminal_action} for command "
            f"{command_id} but the run did not finish within {window}"
        ),
        details=details,
        hint=FINALIZING_NOTE,
    )


def _run_active(status: dict[str, Any]) -> bool:
    lock = status["lock"]
    return bool(lock["present"] and lock["active"])


def _turn_active(status: dict[str, Any]) -> bool:
    return status.get("active_turn") is not None


def _status_finalizing(status: dict[str, Any]) -> bool:
    runner_state = status.get("runner_state")
    if not isinstance(runner_state, dict):
        return False
    return isinstance(runner_state.get("active_finalization"), dict)


def _pending_after_current(status: dict[str, Any]) -> int:
    """Count sessions still pending once the executing one finishes.

    A session that is mid-flight is still recorded as ``pending`` in its handoff
    (the runner only marks it complete afterwards), so subtract it when a turn
    is active.
    """
    pending = int(status["counts"]["pending"])
    if status.get("active_turn") is not None:
        pending -= 1
    return max(0, pending)


def _review_continuation_pending(status: dict[str, Any]) -> bool:
    """Would review work still start once the executing session finishes?

    Only a run carrying a review harness can append a review or materialize its
    repair children, and only while the shared ``review_resume`` projection asks
    for one — a ``terminal`` projection means the plan is done. This is what
    keeps a stop on the last session from being reported as a no-op when the
    runner would in fact stop before the review.
    """
    runner_state = status.get("runner_state")
    if not isinstance(runner_state, dict):
        return False
    if not runner_state.get("review_harness"):
        return False
    review_resume = status.get("review_resume")
    if not isinstance(review_resume, dict):
        return False
    return review_resume.get("action") != "terminal"


def _enqueue_and_report(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    queue_command: control_queue.CommandName,
    harness: str | None = None,
    harness_model: str | None = None,
    clear_harness_model: bool = False,
    effort: str | None = None,
    clear_effort: bool = False,
    grace_seconds: int | None = None,
    reason: str | None = None,
    terminal_actions: tuple[str, ...] = (),
    report: Callable[
        [dict[str, Any], control_queue.ControlCommand],
        tuple[dict[str, Any], str],
    ],
) -> int:
    """Resolve the plan, enqueue one command, then report whether it applies.

    ``report`` receives the pre-enqueue ``status_payload`` and the queued
    command and returns the subcommand-specific payload fields plus the one-line
    human summary.

    Under ``--wait`` the report is delayed until the runner's receipt lands;
    ``terminal_actions`` names the run-control actions whose arrival, rather
    than mere delivery, the caller is actually waiting for.
    """
    wait_timeout = _validated_wait_timeout(
        args,
        command_name=command_name,
        text_label=text_label,
    )
    if wait_timeout is None:
        return 1
    context = cli_common.resolve_plan_context(
        args,
        command_name=command_name,
        text_label=text_label,
    )
    if context is None:
        return 1
    snapshot = context.snapshot
    status = context.status

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
            harness_model=harness_model,
            clear_harness_model=clear_harness_model,
            effort=effort,
            clear_effort=clear_effort,
            grace_seconds=grace_seconds,
            reason=reason,
        )
    except control_queue.ControlQueueError as error:
        cli_common.print_error(
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
        cli_common.print_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="os-error",
            error=f"cannot write control queue {queue_path}: {error}",
            details={"queue_path": str(queue_path)},
            hint="check that the state directory is writable",
        )
        return 1

    # Best-effort wakeup after the durable append: the runner would drain
    # within its poll interval anyway, so the reported outcome is latency
    # information, never a delivery guarantee for the command itself.
    notified = control_signal.notify_runner(snapshot.plan_dir, state_dir=args.state_dir)

    wait: _ReceiptWait | None = None
    if getattr(args, "wait", False):
        wait = _wait_for_receipt(
            snapshot.plan_dir,
            state_dir=args.state_dir,
            command_id=queued.id,
            timeout=wait_timeout,
            terminal_actions=terminal_actions,
        )
        if not wait.satisfied:
            _report_wait_timeout(
                args,
                command_name=command_name,
                text_label=text_label,
                command_id=queued.id,
                queue_path=queue_path,
                signal=notified,
                run_active=_run_active(status),
                wait=wait,
            )
            return 1

    fields, summary = report(status, queued)
    payload = responses.success_payload(
        command_name,
        plan_dir=str(snapshot.plan_dir),
        queue_path=str(queue_path),
        enqueued=True,
        command_id=queued.id,
        signal=notified.to_payload(),
        **fields,
    )
    if wait is not None:
        payload["wait"] = wait.to_payload()
        summary = (
            f"{summary}; observed by the runner"
            if wait.terminal_action is None
            else f"{summary}; observed by the runner, {wait.terminal_action}"
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
            error=(
                "--grace must be a whole number of seconds between "
                f"{control_queue.MIN_ABORT_GRACE_SECONDS} and "
                f"{control_queue.MAX_ABORT_GRACE_SECONDS}"
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
            # An abort sent while the run is already finalizing has no turn to
            # end, so this is the only outcome its `--wait` can be answered by.
            events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
        ),
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
