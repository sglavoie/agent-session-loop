"""Queue one control command and render its outcome."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any

from . import cli_common, control_queue, intervention, responses
from .control_cli_status import _run_active
from .control_cli_wait import (
    _ReceiptWait,
    _report_wait_timeout,
    _validated_wait_timeout,
    _wait_for_receipt,
)


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
    deny_commands: Sequence[str] | None = None,
    protect_paths: Sequence[str] | None = None,
    guard_id: str | None = None,
    expected_run_id: str | None = None,
    terminal_actions: tuple[str, ...] = (),
    terminal_requires_command_id: bool = False,
    terminal_is_final: bool = False,
    failure_actions: tuple[str, ...] = (),
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
        dispatch = intervention.dispatch_control_command(
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
            deny_commands=deny_commands,
            protect_paths=protect_paths,
            guard_id=guard_id,
            expected_run_id=expected_run_id,
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

    # The wakeup rides along inside the service: the runner would drain within
    # its poll interval anyway, so the reported outcome is latency information,
    # never a delivery guarantee for the command itself.
    queued = dispatch.command
    notified = dispatch.signal

    wait: _ReceiptWait | None = None
    if getattr(args, "wait", False):
        wait = _wait_for_receipt(
            snapshot.plan_dir,
            state_dir=args.state_dir,
            command_id=queued.id,
            timeout=wait_timeout,
            terminal_actions=terminal_actions,
            terminal_requires_command_id=terminal_requires_command_id,
            terminal_is_final=terminal_is_final,
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
        if wait.terminal_action in failure_actions:
            # The runner reached the command and refused it, or something else
            # ended the turn first. Reporting that as a success because the
            # receipt arrived is exactly the mistake ``--wait`` exists to
            # prevent.
            outcome = wait.terminal_payload or {}
            refusal = outcome.get("reason")
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason=str(refusal or wait.terminal_action),
                error=(
                    f"the runner refused command {queued.id}: {refusal}"
                    if refusal
                    else (
                        f"command {queued.id} did not take effect: "
                        f"{outcome.get('message') or wait.terminal_action}"
                    )
                ),
                details={
                    "queue_path": str(queue_path),
                    "command_id": queued.id,
                    "enqueued": True,
                    "detail": outcome.get("detail"),
                    "wait": wait.to_payload(),
                },
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
