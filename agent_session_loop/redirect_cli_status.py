# ruff: noqa: F403, F405
from .redirect_cli_projection import *
from .redirect_cli_projection import (
    _live_target,
    _store_error_details,
    _validated_timeout,
)
from .redirect_cli_vocab import *


def control_redirect_status_from_args(args: argparse.Namespace) -> int:
    """Report what became of this plan's corrections, optionally waiting."""
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_REDIRECT_STATUS,
        text_label=TEXT_LABEL_REDIRECT_STATUS,
    )
    if context is None:
        return EXIT_REFUSED
    plan_dir = context.snapshot.plan_dir
    state_dir = getattr(args, "state_dir", None)
    wanted = tuple(dict.fromkeys(getattr(args, "redirect_id", None) or ()))
    wait_requested = bool(getattr(args, "wait_acknowledged", False))
    timeout_seconds = _validated_timeout(
        args,
        command_name=COMMAND_REDIRECT_STATUS,
        text_label=TEXT_LABEL_REDIRECT_STATUS,
        default_seconds=redirect_ack.DEFAULT_ACK_WAIT_SECONDS,
        maximum_seconds=redirect_ack.MAX_ACK_WAIT_SECONDS,
        requires_flag="--wait-acknowledged",
        flag_enabled=wait_requested,
    )
    if timeout_seconds is None:
        return EXIT_REFUSED

    report = _read_report(args, plan_dir=plan_dir, state_dir=state_dir)
    if report is None:
        return EXIT_REFUSED
    known = {operation.operation_id for operation in report.operations}
    unknown = tuple(
        operation_id for operation_id in wanted if operation_id not in known
    )
    if unknown:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT_STATUS,
            text_label=TEXT_LABEL_REDIRECT_STATUS,
            reason=redirect_store_vocab.ERROR_UNKNOWN_REQUEST,
            error=(f"the journal holds no record of {', '.join(unknown)}"),
            details={
                "plan_dir": str(plan_dir),
                "unknown_operation_ids": list(unknown),
                "journal_path": report.journal_path,
                "complete": report.complete,
            },
            hint=(
                "an incomplete journal may still be missing a record"
                if not report.complete
                else None
            ),
        )
        return EXIT_REFUSED

    wait_payload: dict[str, Any] | None = None
    if wait_requested:
        attempt = _live_target(plan_dir, state_dir)
        if attempt is None:
            cli_common.print_error(
                args,
                command_name=COMMAND_REDIRECT_STATUS,
                text_label=TEXT_LABEL_REDIRECT_STATUS,
                reason=redirect_ack_attempt.ERROR_NO_LIVE_ATTEMPT,
                error=(
                    "no live attempt is running, so there is no receipt to wait for"
                ),
                details={"plan_dir": str(plan_dir)},
                hint="drop --wait-acknowledged to read the journal as it stands",
            )
            return EXIT_REFUSED
        try:
            wait = redirect_ack.wait_for_acknowledgment(
                plan_dir,
                attempt=attempt,
                operation_ids=wanted,
                timeout_seconds=timeout_seconds,
                state_dir=state_dir,
            )
        except redirect_store_models.RedirectStoreError as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_REDIRECT_STATUS,
                text_label=TEXT_LABEL_REDIRECT_STATUS,
                reason=error.reason,
                error=error.message,
                details=_store_error_details(error),
            )
            return EXIT_REFUSED
        wait_payload = wait.to_payload()
        # Re-read after the wait so the reported stages are the ones the
        # receipts produced rather than the ones that motivated waiting.
        report = _read_report(args, plan_dir=plan_dir, state_dir=state_dir)
        if report is None:
            return EXIT_REFUSED

    live_target = _live_target(plan_dir, state_dir)
    selected = [
        operation
        for operation in report.operations
        if not wanted or operation.operation_id in wanted
    ]
    operations = [
        operation_payload(operation, live_target=live_target) for operation in selected
    ]
    journal = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    for operation in operations:
        request = journal.request(operation["operation_id"])
        operation["operator_resolution"] = (
            redirect_restart.resolution_for(plan_dir, request, state_dir)
            if request is not None
            else None
        )
    unresolved = [
        operation["operation_id"]
        for operation in operations
        if operation["stage"] == STAGE_UNCERTAIN
        and not operation["operator_resolution"]
    ]
    outstanding = list(wait_payload["outstanding"]) if wait_payload else []
    exit_code = EXIT_UNRESOLVED if unresolved or outstanding else EXIT_OK

    payload = responses.success_payload(
        COMMAND_REDIRECT_STATUS,
        plan_dir=str(plan_dir),
        journal_path=report.journal_path,
        owner=report.owner.to_payload(),
        conversations=report.conversations.to_payload(),
        live_target=live_target.to_payload() if live_target is not None else None,
        requested_operation_ids=list(wanted),
        operations=operations,
        uncertain_operation_ids=unresolved,
        complete=report.complete,
        torn_tail=report.torn_tail,
        unreadable_lines=report.unreadable_lines,
        checked_at=report.checked_at,
        acknowledgment_wait=wait_payload,
        exit_code=exit_code,
    )
    if args.json:
        responses.print_json(payload)
    else:
        _print_status_text(
            plan_dir=plan_dir,
            report=report,
            operations=operations,
            wait_payload=wait_payload,
            unresolved=unresolved,
        )
    return exit_code


def _read_report(
    args: argparse.Namespace,
    *,
    plan_dir: Path,
    state_dir: str | None,
) -> redirect_recovery.RecoveryReport | None:
    try:
        return redirect_recovery.inspect_plan_recovery(plan_dir, state_dir=state_dir)
    except redirect_store_models.RedirectStoreError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT_STATUS,
            text_label=TEXT_LABEL_REDIRECT_STATUS,
            reason=error.reason,
            error=error.message,
            details=_store_error_details(error),
        )
        return None
    except OSError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT_STATUS,
            text_label=TEXT_LABEL_REDIRECT_STATUS,
            reason="os-error",
            error=str(error),
        )
        return None


def _print_status_text(
    *,
    plan_dir: Path,
    report: redirect_recovery.RecoveryReport,
    operations: list[dict[str, Any]],
    wait_payload: dict[str, Any] | None,
    unresolved: list[str],
) -> None:
    if not operations:
        print(f"No redirect operations recorded for {plan_dir}")
    else:
        print(f"Redirect operations for {plan_dir} (owner {report.owner.liveness}):")
        for operation in operations:
            print(
                f"  {operation['operation_id']}  {operation['stage']}  "
                f"(status {operation['status']}) "
                f"run {operation['target']['run_id']} "
                f"session {operation['target']['session_id']} "
                f"turn {operation['target']['turn_index']}"
            )
            decision = operation.get("operator_resolution")
            if decision:
                print(
                    f"    operator decision: {decision['outcome']} "
                    f"({decision['reason']})"
                )
    if not report.complete:
        print(
            "  note: the journal is incomplete, so a record may be missing "
            f"(torn_tail={report.torn_tail}, "
            f"unreadable_lines={report.unreadable_lines})"
        )
    if wait_payload is not None and wait_payload["outstanding"]:
        print(
            "  note: still unreceipted after "
            f"{wait_payload['waited_seconds']:g}s: "
            f"{', '.join(wait_payload['outstanding'])}; the wait retracted "
            "nothing and the worker still holds them"
        )
    if unresolved:
        print(f"  note: {', '.join(unresolved)} — {RECONCILE_HINT}")


# ---------------------------------------------------------------------------
# asl control recover-conversation
# ---------------------------------------------------------------------------
