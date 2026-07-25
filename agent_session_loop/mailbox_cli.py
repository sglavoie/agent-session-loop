"""Command-line producers and consumers for the per-plan mailbox."""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable
import uuid

from . import cli_common, mailbox, responses


COMMAND_INBOX_GUIDANCE = "inbox-guidance"
COMMAND_INBOX_REPLY = "inbox-reply"
COMMAND_INBOX_READ = "inbox-read"
COMMAND_ASK = "ask"
COMMAND_OUTBOX_WAIT = "outbox-wait"

DEFAULT_ASK_WAIT_TIMEOUT_SECONDS = 90.0
DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS = 300.0
MAX_WAIT_TIMEOUT_SECONDS = 15.0 * 60.0
LEASE_RENEW_INTERVAL_SECONDS = 30.0
WAIT_POLL_INTERVAL_SECONDS = 0.25

def _message_payload(message: mailbox.MailboxMessage) -> dict[str, Any]:
    return message.to_payload()


def _mailbox_error_details(error: mailbox.MailboxError) -> dict[str, Any]:
    return {} if error.path is None else {"path": str(error.path)}


def _print_mailbox_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    error: mailbox.MailboxError,
) -> None:
    cli_common.print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason=error.reason,
        error=str(error),
        details=_mailbox_error_details(error),
    )


def _print_input_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    reason: str,
    error: str,
) -> None:
    cli_common.print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason=reason,
        error=error,
    )


def _body_from_args(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> str | None:
    inline = getattr(args, "text", None)
    from_stdin = bool(getattr(args, "text_stdin", False))
    if (inline is None) == (not from_stdin):
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox.REJECT_MISSING_TEXT,
            error="exactly one of --text and --text-stdin is required",
        )
        return None
    if from_stdin:
        try:
            body = sys.stdin.read()
        except OSError as error:
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason="os-error",
                error=f"cannot read message text from stdin: {error}",
            )
            return None
    else:
        body = inline
    if not isinstance(body, str) or body.strip() == "":
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox.REJECT_MISSING_TEXT,
            error="message text must not be blank",
        )
        return None
    return body


def _validated_timeout(
    args: argparse.Namespace,
    *,
    value: object,
    option: str,
    command_name: str,
    text_label: str,
) -> float | None:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = math.nan
    if (
        not math.isfinite(timeout)
        or timeout < 0
        or timeout > MAX_WAIT_TIMEOUT_SECONDS
    ):
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error=(
                f"{option} must be between 0 and "
                f"{int(MAX_WAIT_TIMEOUT_SECONDS)} seconds"
            ),
        )
        return None
    return timeout


def _validated_since(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> int | None:
    value = getattr(args, "since", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-since",
            error="--since must be a non-negative integer",
        )
        return None
    return value


def _resolve(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> cli_common.PlanCommandContext | None:
    return cli_common.resolve_plan_context(
        args,
        command_name=command_name,
        text_label=text_label,
    )


def _write_supervisor_lease(
    plan_dir: str | Path,
    state_dir: str | Path | None,
    supervisor_id: str,
) -> mailbox.MailboxLease:
    return mailbox.write_lease(
        plan_dir,
        supervisor_id,
        state_dir=state_dir,
    )


def _supervisor_write(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    append: Callable[[Path], mailbox.MailboxMessage],
) -> tuple[cli_common.PlanCommandContext, mailbox.MailboxMessage, str] | None:
    context = _resolve(
        args,
        command_name=command_name,
        text_label=text_label,
    )
    if context is None:
        return None
    supervisor_id = str(uuid.uuid4())
    try:
        # Establish the lease first.  If the state root is unwritable, this
        # avoids appending a command the caller may retry after seeing failure.
        _write_supervisor_lease(
            context.snapshot.plan_dir,
            args.state_dir,
            supervisor_id,
        )
        message = append(context.snapshot.plan_dir)
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=command_name,
            text_label=text_label,
            error=error,
        )
        return None
    return context, message, supervisor_id


def inbox_guidance_from_args(args: argparse.Namespace) -> int:
    """Append supervisor guidance and renew the plan's supervisor lease."""
    text = _body_from_args(
        args,
        command_name=COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
    )
    if text is None:
        return 1
    result = _supervisor_write(
        args,
        command_name=COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
        append=lambda plan_dir: mailbox.append_guidance(
            plan_dir,
            text,
            state_dir=args.state_dir,
        ),
    )
    if result is None:
        return 1
    context, message, supervisor_id = result
    mailbox_path = _mailbox_path(context, args)
    payload = responses.success_payload(
        COMMAND_INBOX_GUIDANCE,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_path),
        lease_path=str(
            mailbox.lease_path_for_plan(context.snapshot.plan_dir, args.state_dir)
        ),
        supervisor_id=supervisor_id,
        message=_message_payload(message),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Queued guidance {message.id} for {context.snapshot.plan_dir} "
            f"(seq {message.seq}, mailbox {mailbox_path})"
        )
    return 0


def _mailbox_path(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
) -> Path:
    return mailbox.mailbox_path_for_plan(
        context.snapshot.plan_dir, args.state_dir
    )


def inbox_reply_from_args(args: argparse.Namespace) -> int:
    """Atomically append the first supervisor reply to a question."""
    text = _body_from_args(
        args,
        command_name=COMMAND_INBOX_REPLY,
        text_label="inbox reply",
    )
    if text is None:
        return 1
    result = _supervisor_write(
        args,
        command_name=COMMAND_INBOX_REPLY,
        text_label="inbox reply",
        append=lambda plan_dir: mailbox.append_reply(
            plan_dir,
            args.to,
            text,
            state_dir=args.state_dir,
        ),
    )
    if result is None:
        return 1
    context, message, supervisor_id = result
    payload = responses.success_payload(
        COMMAND_INBOX_REPLY,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        lease_path=str(
            mailbox.lease_path_for_plan(context.snapshot.plan_dir, args.state_dir)
        ),
        supervisor_id=supervisor_id,
        message=_message_payload(message),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Queued reply {message.id} to question {args.to} "
            f"(seq {message.seq})"
        )
    return 0


def inbox_read_from_args(args: argparse.Namespace) -> int:
    """Read pending guidance and optionally acknowledge exactly that snapshot."""
    context = _resolve(
        args,
        command_name=COMMAND_INBOX_READ,
        text_label="inbox read",
    )
    if context is None:
        return 1
    try:
        view = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
        guidance = view.pending_guidance
        acknowledgement = None
        if guidance and not args.peek:
            acknowledgement = mailbox.append_acknowledgement(
                context.snapshot.plan_dir,
                guidance[-1].seq,
                state_dir=args.state_dir,
            )
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=COMMAND_INBOX_READ,
            text_label="inbox read",
            error=error,
        )
        return 1

    payload = responses.success_payload(
        COMMAND_INBOX_READ,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(view.mailbox_path),
        peek=bool(args.peek),
        guidance=[_message_payload(message) for message in guidance],
        acknowledged=acknowledgement is not None,
        acknowledgement=(
            None
            if acknowledgement is None
            else _message_payload(acknowledgement)
        ),
    )
    if args.json:
        responses.print_json(payload)
    elif not guidance:
        print("No pending guidance.")
    else:
        for message in guidance:
            print(f"Guidance {message.id} (seq {message.seq}):")
            print(message.text)
        if acknowledgement is not None:
            print(f"Acknowledged through seq {acknowledgement.upto}.")
    return 0


def _question_from_view(
    view: mailbox.MailboxView,
    question_id: str,
) -> mailbox.MailboxMessage:
    question = next(
        (
            message
            for message in view.messages
            if message.kind == mailbox.KIND_QUESTION
            and message.id == question_id
        ),
        None,
    )
    if question is None:
        raise mailbox.MailboxError(
            mailbox.REJECT_QUESTION_NOT_FOUND,
            question_id,
            path=view.mailbox_path,
        )
    return question


def _wait_for_reply(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
    initial_view: mailbox.MailboxView | None = None,
) -> tuple[mailbox.MailboxMessage | None, bool]:
    deadline = time.monotonic() + timeout
    last_successful_view = initial_view
    last_read_error: mailbox.MailboxError | None = None
    while True:
        try:
            view = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            )
        except mailbox.MailboxError as error:
            if error.reason != mailbox.ERROR_MAILBOX_READ:
                raise
            last_read_error = error
        else:
            last_successful_view = view
            reply = view.reply_for(question_id)
            if reply is not None:
                return reply, False

        now = time.monotonic()
        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            return None, True
        time.sleep(min(WAIT_POLL_INTERVAL_SECONDS, deadline - now))


def ask_from_args(args: argparse.Namespace) -> int:
    """Append or resume a worker question, optionally waiting for its reply."""
    text = getattr(args, "text", None)
    question_id = getattr(args, "question_id", None)
    if (text is None) == (question_id is None):
        _print_input_error(
            args,
            command_name=COMMAND_ASK,
            text_label="ask",
            reason="invalid-question-source",
            error="exactly one of question text and --question-id is required",
        )
        return 1
    if text is not None and (
        not isinstance(text, str) or text.strip() == ""
    ):
        _print_input_error(
            args,
            command_name=COMMAND_ASK,
            text_label="ask",
            reason=mailbox.REJECT_MISSING_TEXT,
            error="question text must not be blank",
        )
        return 1
    wait_timeout = _validated_timeout(
        args,
        value=args.wait_timeout,
        option="--wait-timeout",
        command_name=COMMAND_ASK,
        text_label="ask",
    )
    if wait_timeout is None:
        return 1
    context = _resolve(
        args,
        command_name=COMMAND_ASK,
        text_label="ask",
    )
    if context is None:
        return 1

    initial_view: mailbox.MailboxView | None = None
    created = text is not None
    try:
        if text is not None:
            question = mailbox.append_question(
                context.snapshot.plan_dir,
                text,
                state_dir=args.state_dir,
            )
        else:
            initial_view = mailbox.read_mailbox(
                context.snapshot.plan_dir,
                state_dir=args.state_dir,
            )
            question = _question_from_view(initial_view, question_id)

        reply = (
            initial_view.reply_for(question.id)
            if initial_view is not None
            else None
        )
        timed_out = False
        if args.wait and reply is None:
            reply, timed_out = _wait_for_reply(
                context.snapshot.plan_dir,
                question.id,
                state_dir=args.state_dir,
                timeout=wait_timeout,
                initial_view=initial_view,
            )
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=COMMAND_ASK,
            text_label="ask",
            error=error,
        )
        return 1

    payload = responses.success_payload(
        COMMAND_ASK,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        question_id=question.id,
        question=_message_payload(question),
        created=created,
        waited=bool(args.wait),
        wait_timeout=wait_timeout,
        answered=reply is not None,
        timed_out=timed_out,
        reply=None if reply is None else _message_payload(reply),
    )
    if args.json:
        responses.print_json(payload)
    elif reply is not None:
        print(f"Reply to question {question.id}:")
        print(reply.text)
    elif timed_out:
        print(f"Timed out waiting for a reply to question {question.id}.")
    else:
        action = "Queued" if created else "Resumed"
        print(f"{action} question {question.id} (seq {question.seq}).")
    return 0


def _wait_for_questions(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None,
    since: int,
    timeout: float,
    supervisor_id: str,
) -> tuple[tuple[mailbox.MailboxMessage, ...], int, bool]:
    started = time.monotonic()
    deadline = started + timeout
    next_lease_at = started + LEASE_RENEW_INTERVAL_SECONDS
    last_successful_view: mailbox.MailboxView | None = None
    last_read_error: mailbox.MailboxError | None = None

    _write_supervisor_lease(plan_dir, state_dir, supervisor_id)
    while True:
        try:
            view = mailbox.read_mailbox(
                plan_dir,
                state_dir=state_dir,
            )
        except mailbox.MailboxError as error:
            if error.reason != mailbox.ERROR_MAILBOX_READ:
                raise
            last_read_error = error
        else:
            last_successful_view = view
            questions = view.messages_since(
                since,
                kinds=(mailbox.KIND_QUESTION,),
            )
            if questions:
                return questions, max(since, view.last_seq), False

        now = time.monotonic()
        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            next_since = max(
                since,
                0
                if last_successful_view is None
                else last_successful_view.last_seq,
            )
            return (), next_since, True

        if now >= next_lease_at:
            _write_supervisor_lease(plan_dir, state_dir, supervisor_id)
            next_lease_at = now + LEASE_RENEW_INTERVAL_SECONDS
        sleep_for = min(
            WAIT_POLL_INTERVAL_SECONDS,
            deadline - now,
            max(0.0, next_lease_at - now),
        )
        if sleep_for > 0:
            time.sleep(sleep_for)


def outbox_wait_from_args(args: argparse.Namespace) -> int:
    """Long-poll worker questions after a stateless physical cursor."""
    since = _validated_since(
        args,
        command_name=COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if since is None:
        return 1
    timeout = _validated_timeout(
        args,
        value=args.timeout,
        option="--timeout",
        command_name=COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if timeout is None:
        return 1
    context = _resolve(
        args,
        command_name=COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if context is None:
        return 1
    supervisor_id = str(uuid.uuid4())
    try:
        questions, next_since, timed_out = _wait_for_questions(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
            since=since,
            timeout=timeout,
            supervisor_id=supervisor_id,
        )
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=COMMAND_OUTBOX_WAIT,
            text_label="outbox wait",
            error=error,
        )
        return 1

    payload = responses.success_payload(
        COMMAND_OUTBOX_WAIT,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        lease_path=str(
            mailbox.lease_path_for_plan(context.snapshot.plan_dir, args.state_dir)
        ),
        supervisor_id=supervisor_id,
        since=since,
        next_since=next_since,
        timeout=timeout,
        timed_out=timed_out,
        questions=[_message_payload(question) for question in questions],
    )
    if args.json:
        responses.print_json(payload)
    elif not questions:
        print(f"No questions after seq {since}; timed out.")
    else:
        for question in questions:
            print(f"Question {question.id} (seq {question.seq}):")
            print(question.text)
        print(f"Next cursor: {next_since}")
    return 0
