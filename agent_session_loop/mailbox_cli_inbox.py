"""The ``inbox read`` and ``inbox acknowledge`` commands."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import (
    cli_common,
    mailbox,
    mailbox_appenders,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_cli_projections,
    mailbox_cli_status,
    mailbox_models,
    redirect_ack,
    redirect_ack_attempt,
    redirect_store_models,
    responses,
)


def inbox_read_from_args(args: argparse.Namespace) -> int:
    """Read pending guidance and optionally acknowledge exactly that snapshot."""
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_READ,
        text_label="inbox read",
    )
    if context is None:
        return 1
    try:
        view = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except mailbox_models.MailboxError as error:
        mailbox_cli_output.print_mailbox_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_READ,
            text_label="inbox read",
            error=error,
        )
        return 1

    guidance = view.pending_guidance
    acknowledgement = None
    acknowledgement_error = None
    read_receipts: list[dict[str, Any]] = []
    if guidance and not args.peek:
        # Receipts first, and they cannot starve the acknowledgement: delivery
        # records draw on their own reserve. Ordering this way keeps the
        # receipts describing the read that just happened even when the
        # acknowledgement below fails and the guidance stays pending.
        read_receipts = mailbox_cli_projections.record_guidance_read_deliveries(
            context, args, guidance
        )
        try:
            acknowledgement = mailbox_appenders.append_acknowledgement(
                context.snapshot.plan_dir,
                guidance[-1].seq,
                state_dir=args.state_dir,
            )
        except mailbox_models.MailboxError as error:
            # The immutable read already succeeded. A full or unwritable
            # append-only mailbox must not discard the guidance it delivered.
            acknowledgement_error = {
                "reason": error.reason,
                "error": str(error),
                "details": mailbox_cli_output.mailbox_error_details(error),
            }

    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_INBOX_READ,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(view.mailbox_path),
        peek=bool(args.peek),
        guidance=[mailbox_cli_output.message_payload(message) for message in guidance],
        acknowledged=acknowledgement is not None,
        acknowledgement=(
            None
            if acknowledgement is None
            else mailbox_cli_output.message_payload(acknowledgement)
        ),
        acknowledgement_error=acknowledgement_error,
        delivery_receipts=read_receipts,
    )
    if args.json:
        responses.print_json(payload)
    elif not guidance:
        print("No pending guidance.")
    else:
        print(
            "\n".join(
                mailbox_cli_output.inbox_read_text_lines(
                    guidance,
                    peek=bool(args.peek),
                    acknowledgement=acknowledgement,
                    acknowledgement_error=acknowledgement_error,
                )
            )
        )
        if acknowledgement_error is not None:
            print(
                "Warning: guidance was read but could not be acknowledged "
                f"({acknowledgement_error['reason']}); it remains pending.",
                file=sys.stderr,
            )
    return 0


def live_attempt(
    context: cli_common.PlanCommandContext,
) -> redirect_store_models.RedirectTarget | None:
    """Return the attempt the run's own state says is running right now."""
    ownership = mailbox_cli_status.active_turn_ownership(context)
    if ownership is None:
        return None
    run_id, session_id, turn_index = ownership
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=mailbox_cli_status.active_turn_harness(context),
    )


def _redirect_refusal(
    error: redirect_ack_attempt.RedirectAckError
    | redirect_store_models.RedirectStoreError,
) -> dict[str, Any]:
    payload = error.to_payload()
    return {
        "reason": payload["reason"],
        "error": str(error),
        "details": payload.get("details") or {},
    }


def inbox_acknowledge_from_args(args: argparse.Namespace) -> int:
    """Receipt the corrections one attempt carried, naming that attempt.

    The worker's own command. It writes one ``acknowledged`` record per
    correction and reports what it wrote; nothing waits on the result, and a
    refusal costs the worker nothing but the receipt -- the correction is
    already in its prompt and durable in the journal.

    Refusals are per correction, so a stale id cannot suppress the receipts
    beside it, but a mismatched ``--attempt`` refuses every one of them: the
    token is the claim being checked, and a claim that fails once fails for the
    whole command.
    """
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_ACKNOWLEDGE,
        text_label="inbox acknowledge",
    )
    if context is None:
        return 1
    attempt = live_attempt(context)
    requested: list[str] = [
        operation_id
        for operation_id in (args.redirect_id or ())
        if isinstance(operation_id, str) and operation_id
    ]
    if attempt is None and not requested:
        # Nothing to enumerate and nothing to check it against. Reporting this
        # as "no corrections are carried" would tell a worker whose run is gone
        # that its receipt was unnecessary, when the truth is that no receipt
        # could be verified at all.
        cli_common.print_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_ACKNOWLEDGE,
            text_label="inbox acknowledge",
            reason=redirect_ack_attempt.ERROR_NO_LIVE_ATTEMPT,
            error=(
                "no active turn owns this plan, so the attempt a receipt would "
                "name cannot be identified"
            ),
            details={"attempt": args.attempt},
        )
        return 1
    if (
        attempt is not None
        and not requested
        and not redirect_ack_attempt.token_identifies(args.attempt, attempt)
    ):
        # Checked before enumerating, so an attempt that carries nothing
        # cannot make a wrong token read as a successful empty receipt.
        cli_common.print_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_ACKNOWLEDGE,
            text_label="inbox acknowledge",
            reason=redirect_ack_attempt.ERROR_ATTEMPT_MISMATCH,
            error="acknowledgment names an attempt that is not the live one",
            details={
                "attempt": args.attempt,
                "live_attempt": redirect_ack_attempt.attempt_token(attempt),
            },
        )
        return 1
    if attempt is not None and not requested:
        # No explicit ids: receipt exactly what this attempt carries. A worker
        # reading its prompt always has the ids, so this is the convenience
        # path, and it can still only name corrections the journal already
        # bound to this attempt.
        try:
            summary = redirect_ack.acknowledgment_summary(
                context.snapshot.plan_dir,
                attempt=attempt,
                state_dir=args.state_dir,
            )
        except redirect_store_models.RedirectStoreError as error:
            cli_common.print_error(
                args,
                command_name=mailbox_cli_constants.COMMAND_INBOX_ACKNOWLEDGE,
                text_label="inbox acknowledge",
                reason=error.reason,
                error=str(error),
                details=_redirect_refusal(error)["details"],
            )
            return 1
        requested = list(summary["carried"])

    acknowledged: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for operation_id in dict.fromkeys(requested):
        try:
            receipt = redirect_ack.acknowledge_correction(
                context.snapshot.plan_dir,
                operation_id,
                token=args.attempt,
                live_attempt=attempt,
                state_dir=args.state_dir,
            )
        except (
            redirect_ack_attempt.RedirectAckError,
            redirect_store_models.RedirectStoreError,
        ) as error:
            refused.append(
                {"operation_id": operation_id, **_redirect_refusal(error)},
            )
            continue
        acknowledged.append(receipt.to_payload())

    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_INBOX_ACKNOWLEDGE,
        plan_dir=str(context.snapshot.plan_dir),
        attempt=args.attempt,
        live_attempt=(
            None if attempt is None else redirect_ack_attempt.attempt_token(attempt)
        ),
        requested=list(dict.fromkeys(requested)),
        acknowledged=acknowledged,
        refused=refused,
    )
    if args.json:
        responses.print_json(payload)
    elif not requested:
        print("No corrections are carried by this attempt.")
    else:
        for record in acknowledged:
            print(
                f"Acknowledged correction {record['operation_id']} on attempt "
                f"{record['attempt']}"
                + (" (already receipted)" if record["repeated"] else "")
            )
        for refusal in refused:
            print(
                f"Refused correction {refusal['operation_id']}: "
                f"{refusal['reason']} ({refusal['error']})",
                file=sys.stderr,
            )
    return 1 if refused else 0
