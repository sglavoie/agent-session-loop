"""Command-line producers and consumers for the per-plan mailbox."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Sequence
import uuid

from . import (
    agent_mux_delivery,
    cli_common,
    mailbox,
    mailbox_rendering,
    responses,
)


COMMAND_INBOX_GUIDANCE = "inbox-guidance"
COMMAND_INBOX_REPLY = "inbox-reply"
COMMAND_INBOX_READ = "inbox-read"
COMMAND_ASK = "ask"
COMMAND_OUTBOX_WAIT = "outbox-wait"

DEFAULT_ASK_WAIT_TIMEOUT_SECONDS = 90.0
DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS = 300.0
MAX_WAIT_TIMEOUT_SECONDS = 30.0 * 60.0
LEASE_RENEW_INTERVAL_SECONDS = 30.0
ASK_WAIT_POLL_INTERVAL_SECONDS = 2.0
OUTBOX_WAIT_POLL_INTERVAL_SECONDS = 5.0
ERROR_STDIN_READ = "stdin-read-error"
ERROR_INVALID_TEXT_SOURCE = "invalid-text-source"
MAILBOX_MESSAGE_BEGIN = mailbox_rendering.MAILBOX_MESSAGE_BEGIN
MAILBOX_MESSAGE_END = mailbox_rendering.MAILBOX_MESSAGE_END

_MAILBOX_TOKEN_UNSEEN = object()


@dataclass(frozen=True)
class _LeaseSupervision:
    degraded: bool = False
    renewal_failures: int = 0
    renewal_recoveries: int = 0

    def to_payload(self) -> dict[str, bool | int]:
        return {
            "degraded": self.degraded,
            "renewal_failures": self.renewal_failures,
            "renewal_recoveries": self.renewal_recoveries,
        }


@dataclass(frozen=True)
class _OutboxWaitResult:
    questions: tuple[mailbox.MailboxMessage, ...]
    next_since: int
    timed_out: bool
    lease_supervision: _LeaseSupervision


@dataclass(frozen=True)
class _MailboxReplyProjection:
    """Authoritative pre-reply evidence for one supervisor question."""

    run_id: str
    projection_seq: int
    question_id: str


def _message_payload(message: mailbox.MailboxMessage) -> dict[str, Any]:
    return message.to_payload()


def _print_framed_message(message: mailbox.MailboxMessage) -> None:
    """Render untrusted-shaped message text as one delimited ASCII JSON line."""
    print(MAILBOX_MESSAGE_BEGIN)
    print(mailbox_rendering.message_json(message))
    print(MAILBOX_MESSAGE_END)


def _message_references(
    messages: Sequence[mailbox.MailboxMessage],
    *,
    max_rendered: int = 20,
) -> str:
    """Render bounded identity-only references for an omission notice."""
    records = tuple(messages)
    if len(records) <= max_rendered:
        selected = records
        omitted_count = 0
    else:
        boundary_count = max_rendered // 2
        selected = records[:boundary_count] + records[-boundary_count:]
        omitted_count = len(records) - len(selected)
    references = [
        f"seq {message.seq} (id {message.id})" for message in selected
    ]
    if omitted_count:
        references.insert(
            len(selected) // 2,
            f"{omitted_count} additional references not listed",
        )
    return ", ".join(references)


def _inbox_read_text_lines(
    guidance: tuple[mailbox.MailboxMessage, ...],
    *,
    peek: bool,
    acknowledgement: mailbox.MailboxMessage | None,
    acknowledgement_error: dict[str, Any] | None,
) -> list[str]:
    """Render one bounded human-readable guidance snapshot."""

    def guidance_section(
        records: Sequence[mailbox_rendering.RenderedMessage],
        record_cap_omitted: Sequence[mailbox.MailboxMessage],
        individually_oversized: Sequence[mailbox.MailboxMessage],
        aggregate_budget_omitted: Sequence[mailbox.MailboxMessage],
    ) -> list[str]:
        lines: list[str] = []
        if records:
            for message, record_json in records:
                lines.extend(
                    (
                        f"Guidance {message.id} (seq {message.seq}):",
                        MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        MAILBOX_MESSAGE_END,
                    )
                )
        else:
            lines.append(
                "No pending guidance records fit within the "
                f"{mailbox_rendering.MAX_RENDERED_GUIDANCE_BYTES:,}-byte "
                "text rendering budget."
            )

        omission_notices: list[str] = []
        if record_cap_omitted:
            omission_notices.append(
                f"{len(record_cap_omitted)} older pending guidance records "
                f"were omitted by the "
                f"{mailbox_rendering.MAX_RENDERED_GUIDANCE}-record cap: "
                f"{_message_references(record_cap_omitted)}."
            )
        if individually_oversized:
            omission_notices.append(
                f"{len(individually_oversized)} pending guidance "
                "record(s) exceeded the text byte budget individually and "
                "were omitted in full: "
                f"{_message_references(individually_oversized)}."
            )
        if aggregate_budget_omitted:
            omission_notices.append(
                f"{len(aggregate_budget_omitted)} oldest remaining pending "
                "guidance record(s) were omitted in full to keep the rendered "
                "section within the text byte budget: "
                f"{_message_references(aggregate_budget_omitted)}."
            )
        if omission_notices:
            lines.extend(("", " ".join(omission_notices)))
            if acknowledgement is not None:
                lines.append(
                    "The omitted records were part of the immutable snapshot "
                    f"acknowledged through seq {acknowledgement.upto}."
                )
            elif peek:
                lines.append(
                    "The omitted records remain pending because --peek was used."
                )
            elif acknowledgement_error is not None:
                lines.append(
                    "The omitted records remain pending because the snapshot "
                    "could not be acknowledged "
                    f"({acknowledgement_error['reason']})."
                )

        if acknowledgement is not None:
            lines.append(f"Acknowledged through seq {acknowledgement.upto}.")
        return lines

    bounded = mailbox_rendering.render_bounded_guidance(
        guidance,
        guidance_section,
    )
    return list(bounded.lines)


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
    if inline is None and not from_stdin:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox.REJECT_MISSING_TEXT,
            error="exactly one of --text and --text-stdin is required",
        )
        return None
    if inline is not None and from_stdin:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=ERROR_INVALID_TEXT_SOURCE,
            error="exactly one of --text and --text-stdin is required",
        )
        return None
    if from_stdin:
        try:
            if sys.stdin is None:
                raise ValueError("standard input is unavailable")
            stdin_stream = getattr(sys.stdin, "buffer", sys.stdin)
            raw_body = stdin_stream.read()
            if isinstance(raw_body, bytes):
                body = raw_body.decode("utf-8", errors="strict")
            elif isinstance(raw_body, str):
                # String-backed test and embedding streams have no binary
                # buffer. Reject surrogateescaped input just as the binary
                # UTF-8 path rejects undecodable bytes.
                raw_body.encode("utf-8", errors="strict")
                body = raw_body
            else:
                raise TypeError("standard input did not return text or bytes")
        except (OSError, TypeError, UnicodeError, ValueError) as error:
            cli_common.print_error(
                args,
                command_name=command_name,
                text_label=text_label,
                reason=ERROR_STDIN_READ,
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
    before_lease: Callable[[cli_common.PlanCommandContext], None] | None = None,
) -> tuple[cli_common.PlanCommandContext, mailbox.MailboxMessage, str] | None:
    context = _resolve(
        args,
        command_name=command_name,
        text_label=text_label,
    )
    if context is None:
        return None
    if before_lease is not None:
        try:
            before_lease(context)
        except Exception:  # noqa: BLE001 - optional evidence stays fail-open
            pass
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


def _status_run_id(context: cli_common.PlanCommandContext) -> str | None:
    """Return the run a worker question belongs to, or None when unattributable.

    A torn or absent state read degrades to an unattributed question, which is
    never counted as open. That is the safe direction: it under-reports a live
    block instead of resurrecting one after the run ends.
    """
    runner_state = context.status.get("runner_state")
    if not isinstance(runner_state, dict):
        return None
    run_id = runner_state.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _mailbox_reply_projection(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    question_id: str,
) -> _MailboxReplyProjection | None:
    """Capture a fresh v2 single-question projection before renewing the lease."""
    try:
        view = mailbox.read_lease(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except (OSError, mailbox.MailboxError):
        return None
    lease = view.lease
    if (
        not view.fresh
        or lease is None
        or lease.version != mailbox.MAILBOX_LEASE_VERSION
        or not isinstance(lease.run_id, str)
        or not lease.run_id.strip()
        or isinstance(lease.projection_seq, bool)
        or not isinstance(lease.projection_seq, int)
        or lease.projection_seq <= 0
        or lease.unanswered_questions != 1
        or lease.latest_unanswered_question_id != question_id
    ):
        return None
    return _MailboxReplyProjection(
        run_id=lease.run_id,
        projection_seq=lease.projection_seq,
        question_id=question_id,
    )


def _kitty_window_id() -> int | None:
    raw = os.environ.get("KITTY_WINDOW_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        return None
    return value if value > 0 else None


def _notify_agent_mux_mailbox_reply(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    message: mailbox.MailboxMessage,
    projection: _MailboxReplyProjection | None,
) -> dict[str, Any] | None:
    """Best-effort fast clear after a reply authoritatively empties the outbox."""
    if projection is None or message.to != projection.question_id:
        return None
    try:
        post_reply = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
        if (
            post_reply.last_seq < message.seq
            or post_reply.open_questions(projection.run_id)
        ):
            return None

        runner_state = context.status.get("runner_state")
        harness = (
            runner_state.get("harness")
            if isinstance(runner_state, dict)
            and isinstance(runner_state.get("harness"), str)
            else None
        )
        payload: dict[str, Any] = {
            "run_id": projection.run_id,
            "plan_dir": str(context.snapshot.plan_dir),
            "cwd": os.getcwd(),
            "state_path": context.status.get("state_path"),
            "harness": harness,
        }
        kitty_window_id = _kitty_window_id()
        if kitty_window_id is not None:
            payload["kitty_window_id"] = kitty_window_id
        return agent_mux_delivery.notify(
            agent_mux_delivery.MAILBOX_REPLY_EVENT,
            payload,
            reason=agent_mux_delivery.MAILBOX_REPLY_REASON,
            active_turn=None,
            extra={
                "question_id": projection.question_id,
                "mailbox_projection_sequence": str(
                    projection.projection_seq
                ),
                "source_event_id": (
                    f"{projection.run_id}:mailbox-reply:{message.seq}"
                ),
            },
            enabled=agent_mux_delivery.notify_enabled(),
            timeout=agent_mux_delivery.notify_timeout(),
            allocate_sequence=lambda: message.seq,
            record_outcome=lambda _outcome: None,
            resolve_binary=shutil.which,
            run_command=subprocess.run,
            emitted_at=lambda: datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - mailbox replies never depend on AgentMux
        return None


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
    projection: _MailboxReplyProjection | None = None

    def capture_projection(context: cli_common.PlanCommandContext) -> None:
        nonlocal projection
        projection = _mailbox_reply_projection(context, args, args.to)

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
        before_lease=capture_projection,
    )
    if result is None:
        return 1
    context, message, supervisor_id = result
    _notify_agent_mux_mailbox_reply(context, args, message, projection)
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
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=COMMAND_INBOX_READ,
            text_label="inbox read",
            error=error,
        )
        return 1

    guidance = view.pending_guidance
    acknowledgement = None
    acknowledgement_error = None
    if guidance and not args.peek:
        try:
            acknowledgement = mailbox.append_acknowledgement(
                context.snapshot.plan_dir,
                guidance[-1].seq,
                state_dir=args.state_dir,
            )
        except mailbox.MailboxError as error:
            # The immutable read already succeeded. A full or unwritable
            # append-only mailbox must not discard the guidance it delivered.
            acknowledgement_error = {
                "reason": error.reason,
                "error": str(error),
                "details": _mailbox_error_details(error),
            }

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
        acknowledgement_error=acknowledgement_error,
    )
    if args.json:
        responses.print_json(payload)
    elif not guidance:
        print("No pending guidance.")
    else:
        print(
            "\n".join(
                _inbox_read_text_lines(
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
    mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
    last_successful_view = initial_view
    last_read_error: mailbox.MailboxError | None = None
    last_read_token: object = _MAILBOX_TOKEN_UNSEEN
    while True:
        current_token = mailbox.mailbox_change_token(mailbox_path)
        if (
            last_read_token is _MAILBOX_TOKEN_UNSEEN
            or current_token is mailbox.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
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
                last_read_error = None
                # Capture the token from before the locked read. If an append
                # races the read, the next metadata probe conservatively
                # triggers one extra parse instead of missing the append.
                last_read_token = current_token
                reply = view.reply_for(question_id)
                if reply is not None:
                    return reply, False

        now = time.monotonic()
        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            return None, True
        time.sleep(min(ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now))


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
    requested_wait_timeout = getattr(args, "wait_timeout", None)
    if not args.wait and requested_wait_timeout is not None:
        _print_input_error(
            args,
            command_name=COMMAND_ASK,
            text_label="ask",
            reason="invalid-timeout",
            error="--wait-timeout requires --wait",
        )
        return 1
    wait_timeout = _validated_timeout(
        args,
        value=(
            DEFAULT_ASK_WAIT_TIMEOUT_SECONDS
            if requested_wait_timeout is None
            else requested_wait_timeout
        ),
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
                run_id=_status_run_id(context),
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
        _print_framed_message(reply)
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
) -> _OutboxWaitResult:
    started = time.monotonic()
    deadline = started + timeout
    next_lease_at = started + LEASE_RENEW_INTERVAL_SECONDS
    mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
    last_successful_view: mailbox.MailboxView | None = None
    last_read_error: mailbox.MailboxError | None = None
    last_read_token: object = _MAILBOX_TOKEN_UNSEEN
    lease_degraded = False
    lease_renewal_failures = 0
    lease_renewal_recoveries = 0

    # Initial establishment remains fail-closed: without it the caller has no
    # signal that supervision ever started. Only later renewals may degrade.
    _write_supervisor_lease(plan_dir, state_dir, supervisor_id)
    while True:
        current_token = mailbox.mailbox_change_token(mailbox_path)
        if (
            last_read_token is _MAILBOX_TOKEN_UNSEEN
            or current_token is mailbox.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
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
                last_read_error = None
                last_read_token = current_token
                questions = view.messages_since(
                    since,
                    kinds=(mailbox.KIND_QUESTION,),
                )
                if questions:
                    return _OutboxWaitResult(
                        questions=questions,
                        next_since=max(since, view.last_seq),
                        timed_out=False,
                        lease_supervision=_LeaseSupervision(
                            degraded=lease_degraded,
                            renewal_failures=lease_renewal_failures,
                            renewal_recoveries=lease_renewal_recoveries,
                        ),
                    )

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
            return _OutboxWaitResult(
                questions=(),
                next_since=next_since,
                timed_out=True,
                lease_supervision=_LeaseSupervision(
                    degraded=lease_degraded,
                    renewal_failures=lease_renewal_failures,
                    renewal_recoveries=lease_renewal_recoveries,
                ),
            )

        if now >= next_lease_at:
            try:
                _write_supervisor_lease(
                    plan_dir,
                    state_dir,
                    supervisor_id,
                )
            except (OSError, mailbox.MailboxError):
                lease_degraded = True
                lease_renewal_failures += 1
            else:
                if lease_degraded:
                    lease_renewal_recoveries += 1
                lease_degraded = False
            next_lease_at = now + LEASE_RENEW_INTERVAL_SECONDS
        sleep_for = min(
            OUTBOX_WAIT_POLL_INTERVAL_SECONDS,
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
        wait_result = _wait_for_questions(
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
        next_since=wait_result.next_since,
        timeout=timeout,
        timed_out=wait_result.timed_out,
        questions=[
            _message_payload(question)
            for question in wait_result.questions
        ],
        lease_supervision=wait_result.lease_supervision.to_payload(),
    )
    if args.json:
        responses.print_json(payload)
    else:
        failures = wait_result.lease_supervision.renewal_failures
        if failures:
            health = (
                "the latest renewal remains degraded"
                if wait_result.lease_supervision.degraded
                else "a later renewal recovered"
            )
            print(
                "Warning: supervisor lease renewal failed "
                f"{failures} time(s); {health}.",
                file=sys.stderr,
            )
        if not wait_result.questions:
            print(f"No questions after seq {since}; timed out.")
        else:
            for question in wait_result.questions:
                print(f"Question {question.id} (seq {question.seq}):")
                _print_framed_message(question)
            print(f"Next cursor: {wait_result.next_since}")
    return 0
