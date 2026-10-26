"""The ``ask`` command and its answer waits."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import (
    cli_common,
    mailbox,
    mailbox_appenders,
    mailbox_cli_args,
    mailbox_cli_constants,
    mailbox_cli_output,
    mailbox_cli_status,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    mailbox_waits,
    responses,
)


def _question_from_view(
    view: mailbox_snapshot.MailboxView,
    question_id: str,
) -> mailbox_models.MailboxMessage:
    question = next(
        (
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
            and message.id == question_id
        ),
        None,
    )
    if question is None:
        raise mailbox_models.MailboxError(
            mailbox_constants.REJECT_QUESTION_NOT_FOUND,
            question_id,
            path=view.mailbox_path,
        )
    return question


def wait_for_closure(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
    initial_view: mailbox_snapshot.MailboxView | None = None,
    wait_id: str | None = None,
) -> tuple[mailbox_models.MailboxMessage | None, bool]:
    deadline = time.monotonic() + timeout
    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir)
    last_successful_view = initial_view
    last_read_error: mailbox_models.MailboxError | None = None
    last_read_token: object = mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
    while True:
        current_token = mailbox.mailbox_change_token(mailbox_path)
        if (
            last_read_token is mailbox_cli_constants.MAILBOX_TOKEN_UNSEEN
            or current_token is mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
            try:
                view = mailbox.read_mailbox(
                    plan_dir,
                    state_dir=state_dir,
                )
            except mailbox_models.MailboxError as error:
                if error.reason != mailbox_constants.ERROR_MAILBOX_READ:
                    raise
                last_read_error = error
            else:
                last_successful_view = view
                last_read_error = None
                # Capture the token from before the locked read. If an append
                # races the read, the next metadata probe conservatively
                # triggers one extra parse instead of missing the append.
                last_read_token = current_token
                closure = view.closure_for(question_id)
                if closure is not None:
                    if wait_id is not None:
                        try:
                            completion = mailbox_waits.consume_wait_closure(
                                plan_dir,
                                question_id,
                                wait_id,
                                state_dir=state_dir,
                            )
                        except mailbox_models.MailboxError as error:
                            if (
                                error.reason
                                != mailbox_constants.REJECT_QUESTION_NOT_WAITING
                            ):
                                raise
                            completed_view = mailbox.read_mailbox(
                                plan_dir, state_dir=state_dir
                            )
                            if completed_view.receipt_for_closure(closure.id) is None:
                                raise
                        else:
                            if completion is not None:
                                closure = completion.closure
                    elif view.receipt_for_closure(closure.id) is None:
                        try:
                            mailbox_appenders.append_receipt(
                                plan_dir,
                                question_id,
                                closure.id,
                                state_dir=state_dir,
                            )
                        except mailbox_models.MailboxError as error:
                            if (
                                error.reason
                                != mailbox_constants.REJECT_CLOSURE_ALREADY_CONSUMED
                            ):
                                raise
                    return closure, False

        now = time.monotonic()
        # The timeout bounds an unwatched question. A live supervisor may need
        # longer to collect a human answer, just as it may for a runner pause.
        # Zero remains a single poll; a missing or unreadable lease never
        # extends the deadline, and finish_wait still arbitrates reply races.
        if timeout > 0 and last_read_error is None:
            try:
                lease = mailbox_leases.read_lease(plan_dir, state_dir=state_dir)
            except OSError, mailbox_models.MailboxError:
                pass
            else:
                if lease.fresh:
                    deadline = max(deadline, now + timeout)
        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            if wait_id is not None:
                completion = mailbox_waits.finish_wait(
                    plan_dir,
                    question_id,
                    wait_id,
                    reason=mailbox_vocabulary.WAIT_END_REASON_TIMEOUT,
                    state_dir=state_dir,
                )
                if completion.closure is not None:
                    return completion.closure, False
            return None, True
        time.sleep(
            min(mailbox_cli_constants.ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now)
        )


def wait_for_reply(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
    initial_view: mailbox_snapshot.MailboxView | None = None,
) -> tuple[mailbox_models.MailboxMessage | None, bool]:
    """Compatibility wrapper for callers of the former reply-only wait."""
    return wait_for_closure(
        plan_dir,
        question_id,
        state_dir=state_dir,
        timeout=timeout,
        initial_view=initial_view,
    )


def ask_from_args(args: argparse.Namespace) -> int:
    """Append or resume a worker question, optionally waiting for closure."""
    text = getattr(args, "text", None)
    from_stdin = bool(getattr(args, "text_stdin", False))
    question_id_value = getattr(args, "question_id", None)
    # `--question-id` is a plain string option, so this is identity; spelling it
    # is what keeps the id a `str` rather than `Any` all the way to the lookup.
    question_id = None if question_id_value is None else str(question_id_value)
    if from_stdin and text is not None:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_ASK,
            text_label="ask",
            reason=mailbox_cli_constants.ERROR_INVALID_TEXT_SOURCE,
            error="exactly one of TEXT and --text-stdin is required",
        )
        return 1
    if (text is None and not from_stdin) == (question_id is None):
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_ASK,
            text_label="ask",
            reason="invalid-question-source",
            error="exactly one of TEXT, --text-stdin, or --question-id is required",
        )
        return 1
    if from_stdin:
        # Read only once the sources are known to be coherent, so a refused
        # invocation never consumes the caller's stdin.
        try:
            text = cli_common.read_stdin_text()
        except (OSError, TypeError, UnicodeError, ValueError) as error:
            cli_common.print_error(
                args,
                command_name=mailbox_cli_constants.COMMAND_ASK,
                text_label="ask",
                reason=mailbox_cli_constants.ERROR_STDIN_READ,
                error=f"cannot read question text from stdin: {error}",
            )
            return 1
    if text is not None and (not isinstance(text, str) or text.strip() == ""):
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_ASK,
            text_label="ask",
            reason=mailbox_constants.REJECT_MISSING_TEXT,
            error="question text must not be blank",
        )
        return 1
    requested_wait_timeout = getattr(args, "wait_timeout", None)
    if not args.wait and requested_wait_timeout is not None:
        mailbox_cli_output.print_input_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_ASK,
            text_label="ask",
            reason="invalid-timeout",
            error="--wait-timeout requires --wait",
        )
        return 1
    wait_timeout = mailbox_cli_args.validated_timeout(
        args,
        value=(
            mailbox_cli_constants.DEFAULT_ASK_WAIT_TIMEOUT_SECONDS
            if requested_wait_timeout is None
            else requested_wait_timeout
        ),
        option="--wait-timeout",
        command_name=mailbox_cli_constants.COMMAND_ASK,
        text_label="ask",
    )
    if wait_timeout is None:
        return 1
    context = mailbox_cli_args.resolve(
        args,
        command_name=mailbox_cli_constants.COMMAND_ASK,
        text_label="ask",
    )
    if context is None:
        return 1

    initial_view: mailbox_snapshot.MailboxView | None = None
    wait_start: mailbox_models.MailboxMessage | None = None
    created = text is not None
    try:
        if text is not None:
            (
                run_id,
                session_id,
                turn_index,
                waiter_origin,
            ) = mailbox_cli_status.status_question_ownership(context)
            if args.wait:
                question, wait_start = mailbox_waits.append_wait_question(
                    context.snapshot.plan_dir,
                    text,
                    state_dir=args.state_dir,
                    run_id=run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    waiter_origin=waiter_origin,
                )
            else:
                question = mailbox_appenders.append_question(
                    context.snapshot.plan_dir,
                    text,
                    state_dir=args.state_dir,
                    run_id=run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    waiter_origin=waiter_origin,
                    delivery_mode=mailbox_vocabulary.DELIVERY_MODE_ASYNC,
                )
        else:
            # Exactly one of text and question id is set; this is the other arm.
            assert question_id is not None
            initial_view = mailbox.read_mailbox(
                context.snapshot.plan_dir,
                state_dir=args.state_dir,
            )
            question = _question_from_view(initial_view, question_id)

            if args.wait:
                wait_start = initial_view.active_wait_for(question.id)
                if wait_start is None and initial_view.closure_for(question.id) is None:
                    wait_start = mailbox_waits.append_wait_start(
                        context.snapshot.plan_dir,
                        question.id,
                        state_dir=args.state_dir,
                    )

        closure = (
            initial_view.closure_for(question.id) if initial_view is not None else None
        )
        if (
            args.wait
            and closure is not None
            and initial_view is not None
            and initial_view.receipt_for_closure(closure.id) is None
        ):
            if wait_start is not None:
                mailbox_waits.consume_wait_closure(
                    context.snapshot.plan_dir,
                    question.id,
                    wait_start.id,
                    state_dir=args.state_dir,
                )
            else:
                mailbox_appenders.append_receipt(
                    context.snapshot.plan_dir,
                    question.id,
                    closure.id,
                    state_dir=args.state_dir,
                )
        timed_out = False
        if args.wait and closure is None:
            closure, timed_out = wait_for_closure(
                context.snapshot.plan_dir,
                question.id,
                state_dir=args.state_dir,
                timeout=wait_timeout,
                initial_view=initial_view,
                wait_id=None if wait_start is None else wait_start.id,
            )
    except mailbox_models.MailboxError as error:
        mailbox_cli_output.print_mailbox_error(
            args,
            command_name=mailbox_cli_constants.COMMAND_ASK,
            text_label="ask",
            error=error,
        )
        return 1

    reply = (
        closure
        if closure is not None and closure.kind == mailbox_vocabulary.KIND_REPLY
        else None
    )
    resolution = (
        closure
        if closure is not None and closure.kind == mailbox_vocabulary.KIND_RESOLUTION
        else None
    )
    pause_required = bool(
        resolution is not None
        and resolution.action == mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
    )
    receipt = None
    if closure is not None:
        final_view = mailbox.read_mailbox(
            context.snapshot.plan_dir, state_dir=args.state_dir
        )
        receipt = final_view.receipt_for_closure(closure.id)
    consumed = receipt is not None

    payload = responses.success_payload(
        mailbox_cli_constants.COMMAND_ASK,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_cli_args.plan_mailbox_path(context, args)),
        question_id=question.id,
        question=mailbox_cli_output.message_payload(question),
        created=created,
        waited=bool(args.wait),
        wait_timeout=wait_timeout,
        answered=reply is not None,
        resolved=resolution is not None,
        pause_required=pause_required,
        timed_out=timed_out,
        reply=None if reply is None else mailbox_cli_output.message_payload(reply),
        resolution=(
            None
            if resolution is None
            else mailbox_cli_output.message_payload(resolution)
        ),
        wait_start=(
            None
            if wait_start is None
            else mailbox_cli_output.message_payload(wait_start)
        ),
        delivery_state=(
            "consumed"
            if consumed
            else "accepted"
            if closure is not None
            else "timed-out"
            if timed_out
            else "open"
        ),
        consumed=consumed,
        receipt=None
        if receipt is None
        else mailbox_cli_output.message_payload(receipt),
    )
    if args.json:
        responses.print_json(payload)
    elif reply is not None:
        print(f"Reply to question {question.id}:")
        mailbox_cli_output.print_framed_message(reply)
    elif resolution is not None:
        print(f"Resolution for question {question.id}:")
        mailbox_cli_output.print_framed_message(resolution)
        if pause_required:
            print(
                "Directive: Stop work and emit RUNNER_PAUSE: blocked - "
                "supervisor requested a pause."
            )
        else:
            print(
                "Directive: Continue work. No operator reply was supplied; "
                "do not treat the optional resolution note as an operator "
                "reply."
            )
    elif timed_out:
        print(f"Timed out waiting for a closure to question {question.id}.")
    else:
        action = "Queued" if created else "Resumed"
        print(f"{action} question {question.id} (seq {question.seq}).")
    return 0
