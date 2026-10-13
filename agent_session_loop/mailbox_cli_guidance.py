"""The ``inbox guidance`` command."""

from __future__ import annotations

import argparse

from . import (
    cli_common,
    mailbox_appenders,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_cli_receipts,
    mailbox_cli_status,
    mailbox_cursors,
    mailbox_models,
    mailbox_vocabulary,
    responses,
)


def inbox_guidance_from_args(args: argparse.Namespace) -> int:
    """Append supervisor guidance and renew the plan's supervisor lease.

    The append and the delivery are separate facts and are reported as such.
    A durable append is a success even when nothing has delivered it yet, so
    the exit code follows the append alone and ``delivered`` carries the rest.
    """
    text = mailbox_cli_args.body_from_args(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
    )
    if text is None:
        return 1
    wait_delivered = bool(getattr(args, "wait_delivered", False))
    delivery_timeout = mailbox_cli_receipts.delivery_wait_timeout(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
    )
    if delivery_timeout is None:
        return 1
    active_turn: tuple[str, str, int] | None = None

    def capture_active_turn(context: cli_common.PlanCommandContext) -> None:
        nonlocal active_turn
        active_turn = mailbox_cli_status.active_turn_ownership(context)

    result = mailbox_cli_status.supervisor_write(
        args,
        command_name=mailbox_cli_constants.COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
        append=lambda plan_dir: mailbox_appenders.append_guidance(
            plan_dir,
            text,
            state_dir=args.state_dir,
        ),
        before_lease=capture_active_turn,
    )
    if result is None:
        return 1
    context, message, supervisor_id = result
    mailbox_path = mailbox_cli_args.plan_mailbox_path(context, args)
    delivery: mailbox_models.MailboxMessage | None = None
    if wait_delivered:
        try:
            delivery = mailbox_cli_receipts.wait_for_guidance_delivery(
                context.snapshot.plan_dir,
                message.id,
                state_dir=args.state_dir,
                timeout=delivery_timeout,
                active_turn=active_turn,
            )
        except mailbox_models.MailboxError:
            # The guidance append is authoritative. A later observation failure
            # must stay a truthful successful append with delivered=false.
            delivery = None
    delivery_state: mailbox_vocabulary.GuidanceDeliveryState
    if delivery is None:
        # Without a delivery record the guidance is queued by definition: it is
        # durable and nothing has reported acting on it.
        delivery_state = mailbox_vocabulary.GUIDANCE_STATE_QUEUED
    else:
        # Every record `_wait_for_guidance_delivery` can return is a delivery
        # attempt -- successful or deferred -- so it carries a state.
        assert delivery.delivery_state is not None
        delivery_state = delivery.delivery_state
    delivered = delivery_state in mailbox_vocabulary.GUIDANCE_DELIVERED_STATES
    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_INBOX_GUIDANCE,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_path),
        lease_path=str(
            mailbox_cursors.lease_path_for_plan(
                context.snapshot.plan_dir, args.state_dir
            )
        ),
        supervisor_id=supervisor_id,
        message=mailbox_cli_output.message_payload(message),
        appended=True,
        waited=wait_delivered,
        delivered=delivered,
        delivery_state=delivery_state,
        delivery_reason=None if delivery is None else delivery.delivery_reason,
        delivery=None
        if delivery is None
        else mailbox_cli_output.message_payload(delivery),
    )
    if args.json:
        responses.print_json(payload)
    elif wait_delivered:
        # The state, not the append, leads the line. Without `--wait-delivered`
        # nothing was observed and the honest report is that the record is
        # durable; with it, this says which of the two actually happened.
        summary = mailbox_vocabulary.describe_guidance_delivery(
            delivery_state,
            delivery_reason=None if delivery is None else delivery.delivery_reason,
            harness=None if delivery is None else delivery.harness,
        )
        print(
            f"Guidance {summary}: {message.id} for {context.snapshot.plan_dir} "
            f"(seq {message.seq}, mailbox {mailbox_path}, "
            f"state {delivery_state})"
        )
    else:
        queued = mailbox_vocabulary.describe_guidance_delivery(
            mailbox_vocabulary.GUIDANCE_STATE_QUEUED
        )
        print(
            f"Guidance {queued} (no delivery wait requested): {message.id} for "
            f"{context.snapshot.plan_dir} (seq {message.seq}, "
            f"mailbox {mailbox_path})"
        )
    return 0
