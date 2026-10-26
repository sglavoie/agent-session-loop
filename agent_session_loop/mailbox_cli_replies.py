"""The ``inbox reply`` and ``inbox resolve`` commands."""

from __future__ import annotations

import argparse

from . import (
    cli_common,
    mailbox,
    mailbox_appenders,
    mailbox_cli_agent_mux,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_models,
    mailbox_cli_output,
    mailbox_cli_projections,
    mailbox_cli_receipts,
    mailbox_cli_status,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_vocabulary,
    responses,
)


def inbox_reply_from_args(args: argparse.Namespace) -> int:
    """Atomically append the first supervisor reply to a question."""
    text = mailbox_cli_args.body_from_args(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_REPLY,
        text_label="inbox reply",
    )
    if text is None:
        return 1
    consumption_timeout = mailbox_cli_receipts.consumption_wait_timeout(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_REPLY,
        text_label="inbox reply",
    )
    if consumption_timeout is None:
        return 1
    projection: mailbox_cli_models.MailboxReplyProjection | None = None

    def capture_projection(context: cli_common.PlanCommandContext) -> None:
        nonlocal projection
        projection = mailbox_cli_projections.mailbox_reply_projection(
            context, args, args.to
        )

    result = mailbox_cli_status.supervisor_write(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_REPLY,
        text_label="inbox reply",
        append=lambda plan_dir: mailbox_appenders.append_reply(
            plan_dir,
            args.to,
            text,
            state_dir=args.state_dir,
        ),
        before_lease=capture_projection,
    )
    if result is None:
        return 1
    context, message, supervisor_id = result
    mailbox_cli_agent_mux.notify_agent_mux_mailbox_reply(
        context, args, message, projection
    )
    unconsumable = _unconsumable_reason(context, args)
    receipt, waited, timed_out = mailbox_cli_receipts.await_consumption(
        args,
        context.snapshot.plan_dir,
        message.id,
        timeout=consumption_timeout,
        skip=unconsumable is not None,
    )
    consumed = receipt is not None
    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_INBOX_REPLY,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_cli_args.plan_mailbox_path(context, args)),
        lease_path=str(
            mailbox_cursors.lease_path_for_plan(
                context.snapshot.plan_dir, args.state_dir
            )
        ),
        supervisor_id=supervisor_id,
        message=mailbox_cli_output.message_payload(message),
        delivery_state="consumed" if consumed else "accepted",
        consumed=consumed,
        waited=waited,
        timed_out=timed_out,
        unconsumable_reason=unconsumable,
        receipt=None
        if receipt is None
        else mailbox_cli_output.message_payload(receipt),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Accepted reply {message.id} to question {args.to} "
            f"(seq {message.seq}, consumed: {'yes' if consumed else 'no'})"
        )
        if unconsumable is not None and getattr(args, "wait_consumed", False):
            print(
                "Did not wait for consumption: no active run owns this question; "
                "the reply stays in the mailbox for a later turn to collect."
            )
    return 0


def _unconsumable_reason(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
) -> str | None:
    """Say why no receipt can follow this reply now, or ``None``.

    A reply, unlike a resolution, is accepted with no active run: an async
    answer stays in the mailbox for a later turn to collect, and operators may
    close historical questions. Only the run that owns the question writes the
    receipt, though, so without that run a consumption wait can only time out.
    """
    run_id = mailbox_cli_status.active_status_run_id(context)
    if run_id is None:
        return mailbox_cli_constants.ERROR_NO_ACTIVE_RUN
    try:
        view = mailbox.read_mailbox(context.snapshot.plan_dir, state_dir=args.state_dir)
    except OSError, mailbox_models.MailboxError:
        return None
    question = next(
        (
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
            and message.id == args.to
        ),
        None,
    )
    if question is not None and question.run_id != run_id:
        return mailbox_constants.REJECT_QUESTION_NOT_OPEN
    return None


def inbox_resolve_from_args(args: argparse.Namespace) -> int:
    """Atomically append the first answerless closure for a live question."""
    action = getattr(args, "action", None)
    if action not in mailbox_vocabulary.RESOLUTION_ACTIONS:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=mailbox_constants.REJECT_INVALID_ACTION,
            error="--action must be either continue or pause",
        )
        return 1
    note_ok, note = mailbox_cli_args.optional_note_from_args(args)
    if not note_ok:
        return 1
    consumption_timeout = mailbox_cli_receipts.consumption_wait_timeout(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
        text_label="inbox resolve",
    )
    if consumption_timeout is None:
        return 1
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
        text_label="inbox resolve",
    )
    if context is None:
        return 1
    run_id = mailbox_cli_status.active_status_run_id(context)
    if run_id is None:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=mailbox_cli_constants.ERROR_NO_ACTIVE_RUN,
            error="no active run can consume a question resolution",
        )
        return 1
    projection = mailbox_cli_projections.mailbox_reply_projection(
        context, args, args.to
    )
    try:
        lease = mailbox_cli_status.write_supervisor_lease(
            context.snapshot.plan_dir,
            args.state_dir,
        )
        message = mailbox_appenders.append_resolution(
            context.snapshot.plan_dir,
            args.to,
            action,
            expected_run_id=run_id,
            note=note,
            state_dir=args.state_dir,
        )
    except mailbox_models.MailboxError as error:
        mailbox_cli_output.print_mailbox_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            error=error,
        )
        return 1

    mailbox_cli_agent_mux.notify_agent_mux_mailbox_resolution(
        context,
        args,
        message,
        projection,
        active_run_id=run_id,
    )
    receipt, waited, timed_out = mailbox_cli_receipts.await_consumption(
        args,
        context.snapshot.plan_dir,
        message.id,
        timeout=consumption_timeout,
    )
    consumed = receipt is not None

    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_INBOX_RESOLVE,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_cli_args.plan_mailbox_path(context, args)),
        lease_path=str(
            mailbox_cursors.lease_path_for_plan(
                context.snapshot.plan_dir,
                args.state_dir,
            )
        ),
        supervisor_id=lease.supervisor_id,
        run_id=run_id,
        message=mailbox_cli_output.message_payload(message),
        delivery_state="consumed" if consumed else "accepted",
        consumed=consumed,
        waited=waited,
        timed_out=timed_out,
        receipt=None
        if receipt is None
        else mailbox_cli_output.message_payload(receipt),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Accepted resolution for question {args.to} with action {action} "
            f"(resolution {message.id}, seq {message.seq}, "
            f"consumed: {'yes' if consumed else 'no'})"
        )
    return 0
