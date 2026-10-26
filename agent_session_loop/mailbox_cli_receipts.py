"""Bounded waits for guidance delivery and reply consumption."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import (
    mailbox,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_constants,
    mailbox_cursors,
    mailbox_models,
    mailbox_vocabulary,
)


def delivery_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> float | None:
    """Return the validated delivery wait timeout, or None on invalid input."""
    requested = getattr(args, "wait_timeout", None)
    wait_delivered = bool(getattr(args, "wait_delivered", False))
    if requested is not None and not wait_delivered:
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error="--wait-timeout requires --wait-delivered",
        )
        return None
    return mailbox_cli_args.validated_timeout(
        args,
        value=(
            mailbox_cli_constants.DEFAULT_GUIDANCE_DELIVERED_TIMEOUT_SECONDS
            if requested is None
            else requested
        ),
        option="--wait-timeout",
        command_name=command_name,
        text_label=text_label,
    )


def wait_for_guidance_delivery(
    plan_dir: str | Path,
    guidance_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
    active_turn: tuple[str, str, int] | None,
) -> mailbox_models.MailboxMessage | None:
    """Return the delivery attempt that ends the wait, or None on timeout.

    A successful attempt always ends the wait. A deferred attempt ends it only
    when it speaks about the turn that was live when the wait started: a stale
    deferral from an earlier turn says nothing about whether the run is about
    to deliver this guidance now. With no authoritative active turn, any
    deferral is the most current statement available and ends the wait.

    One deferral never ends it: ``awaiting-confirmation``. That record says the
    attempt is still running, so returning it would report "not delivered"
    about guidance that is very likely about to be. The wait keeps going until
    the same attempt resolves or the timeout does.
    """
    deadline = time.monotonic() + timeout
    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
    token: object = mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
    while True:
        current = mailbox.mailbox_change_token(mailbox_path)
        if (
            token is mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
            or current is mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current != token
        ):
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            token = current
            delivered = view.successful_guidance_delivery(guidance_id)
            if delivered is not None:
                return delivered
            for deferred in reversed(view.deferred_guidance_deliveries(guidance_id)):
                if deferred.delivery_reason == (
                    mailbox_vocabulary.GUIDANCE_REASON_AWAITING_CONFIRMATION
                ):
                    continue
                if (
                    active_turn is None
                    or (deferred.run_id, deferred.session_id, deferred.turn_index)
                    == active_turn
                ):
                    return deferred
        now = time.monotonic()
        if now >= deadline:
            return None
        time.sleep(
            min(mailbox_cli_constants.ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now)
        )


def consumption_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> float | None:
    requested = getattr(args, "wait_timeout", None)
    wait_consumed = bool(getattr(args, "wait_consumed", False))
    if requested is not None and not wait_consumed:
        mailbox_cli_output.print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error="--wait-timeout requires --wait-consumed",
        )
        return None
    return mailbox_cli_args.validated_timeout(
        args,
        value=(
            mailbox_cli_constants.DEFAULT_ASK_WAIT_TIMEOUT_SECONDS
            if requested is None
            else requested
        ),
        option="--wait-timeout",
        command_name=command_name,
        text_label=text_label,
    )


def await_consumption(
    args: argparse.Namespace,
    plan_dir: str | Path,
    closure_id: str,
    *,
    timeout: float,
    skip: bool = False,
) -> tuple[mailbox_models.MailboxMessage | None, bool, bool]:
    """Return ``(receipt, waited, timed_out)`` for one appended closure.

    Only a requested ``--wait-consumed`` waits, and ``skip`` says no consumer
    exists to wait for. The closure append is authoritative, so an observation
    failure after it stays a truthful successful append with no receipt, and is
    not a timeout either.
    """
    if skip or not getattr(args, "wait_consumed", False):
        return None, False, False
    try:
        receipt = wait_for_receipt(
            plan_dir,
            closure_id,
            state_dir=args.state_dir,
            timeout=timeout,
        )
    except mailbox_models.MailboxError:
        return None, True, False
    return receipt, True, receipt is None


def wait_for_receipt(
    plan_dir: str | Path,
    closure_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
) -> mailbox_models.MailboxMessage | None:
    deadline = time.monotonic() + timeout
    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
    token: object = mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
    while True:
        current = mailbox.mailbox_change_token(mailbox_path)
        if (
            token is mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
            or current is mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current != token
        ):
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            token = current
            receipt = view.receipt_for_closure(closure_id)
            if receipt is not None:
                return receipt
        now = time.monotonic()
        if now >= deadline:
            return None
        time.sleep(
            min(mailbox_cli_constants.ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now)
        )
