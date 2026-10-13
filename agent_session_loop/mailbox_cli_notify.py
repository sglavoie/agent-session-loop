"""The ``notify`` command."""

from __future__ import annotations

import argparse

from . import (
    mailbox_appenders,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_cli_projections,
    mailbox_cli_status,
    mailbox_models,
    responses,
)


def notify_from_args(args: argparse.Namespace) -> int:
    """Publish a nonblocking notice owned by the exact active worker turn."""
    text = mailbox_cli_args.body_from_args(
        args,
        command_name=mailbox_cli_constants.COMMAND_NOTIFY,
        text_label="notify",
        inline_source="TEXT",
    )
    if text is None:
        return 1
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_NOTIFY,
        text_label="notify",
    )
    if context is None:
        return 1
    ownership = mailbox_cli_status.active_turn_ownership(context)
    if ownership is None:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_NOTIFY,
            text_label="notify",
            reason=mailbox_cli_constants.ERROR_NO_ACTIVE_RUN,
            error="no active worker turn can publish a notice",
        )
        return 1
    run_id, session_id, turn_index = ownership
    try:
        message = mailbox_appenders.append_notice(
            context.snapshot.plan_dir,
            text,
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
            state_dir=args.state_dir,
        )
    except mailbox_models.MailboxError as error:
        mailbox_cli_output.print_mailbox_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_NOTIFY,
            text_label="notify",
            error=error,
        )
        return 1

    mailbox_cli_projections.record_notice_published_event(context, message)

    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_NOTIFY,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_cli_args.plan_mailbox_path(context, args)),
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        message=mailbox_cli_output.message_payload(message),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Published notice {message.id} for session {session_id} "
            f"turn {turn_index} (seq {message.seq})."
        )
    return 0
