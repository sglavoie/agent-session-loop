"""Command-line producers and consumers for the per-plan mailbox."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
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
    events,
    mailbox,
    mailbox_rendering,
    responses,
)


COMMAND_INBOX_GUIDANCE = "inbox-guidance"
COMMAND_INBOX_REPLY = "inbox-reply"
COMMAND_INBOX_RESOLVE = "inbox-resolve"
COMMAND_INBOX_READ = "inbox-read"
COMMAND_ASK = "ask"
COMMAND_NOTIFY = "notify"
COMMAND_OUTBOX_WAIT = "outbox-wait"

OUTBOX_INCLUDE_QUESTIONS = "questions"
OUTBOX_INCLUDE_NOTICES = "notices"
OUTBOX_INCLUDE_ALL = "all"
OUTBOX_INCLUDE_MODES = (
    OUTBOX_INCLUDE_QUESTIONS,
    OUTBOX_INCLUDE_NOTICES,
    OUTBOX_INCLUDE_ALL,
)

DEFAULT_ASK_WAIT_TIMEOUT_SECONDS = 90.0
DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS = 300.0
MAX_WAIT_TIMEOUT_SECONDS = 30.0 * 60.0
LEASE_RENEW_INTERVAL_SECONDS = 30.0
ASK_WAIT_POLL_INTERVAL_SECONDS = 2.0
OUTBOX_WAIT_POLL_INTERVAL_SECONDS = 5.0
ERROR_STDIN_READ = "stdin-read-error"
ERROR_INVALID_TEXT_SOURCE = "invalid-text-source"
ERROR_INVALID_NOTE_SOURCE = "invalid-note-source"
ERROR_NO_ACTIVE_RUN = "no-active-run"
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
    notices: tuple[mailbox.MailboxMessage, ...]
    include: str
    supervisor_id: str
    next_since: int
    timed_out: bool
    lease_supervision: _LeaseSupervision


@dataclass(frozen=True)
class _MailboxReplyProjection:
    """Authoritative pre-reply evidence for one supervisor question."""

    run_id: str
    projection_seq: int
    question_id: str
    lease_predates_question: bool = False


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


def _outbox_question_text_lines(
    questions: tuple[mailbox.MailboxMessage, ...],
) -> list[str]:
    """Render question bodies under the notice count and byte bounds."""

    def question_section(
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
                        f"Question {message.id} (seq {message.seq}):",
                        MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        MAILBOX_MESSAGE_END,
                    )
                )
        else:
            lines.append(
                "No selected question records fit within the "
                f"{mailbox_rendering.MAX_RENDERED_NOTICE_BYTES:,}-byte "
                "text rendering budget."
            )

        omission_notices: list[str] = []
        if record_cap_omitted:
            omission_notices.append(
                f"{len(record_cap_omitted)} older selected question records "
                f"were omitted by the "
                f"{mailbox_rendering.MAX_RENDERED_NOTICES}-record cap: "
                f"{_message_references(record_cap_omitted)}."
            )
        if individually_oversized:
            omission_notices.append(
                f"{len(individually_oversized)} selected question record(s) "
                "exceeded the text byte budget individually and were omitted "
                f"in full: {_message_references(individually_oversized)}."
            )
        if aggregate_budget_omitted:
            omission_notices.append(
                f"{len(aggregate_budget_omitted)} oldest remaining selected "
                "question record(s) were omitted in full to keep the rendered "
                "section within the text byte budget: "
                f"{_message_references(aggregate_budget_omitted)}."
            )
        if omission_notices:
            lines.extend(
                (
                    "",
                    " ".join(omission_notices),
                    "The physical cursor advances through every mailbox "
                    "record, including records filtered out or omitted from "
                    "text rendering.",
                )
            )
        return lines

    bounded = mailbox_rendering.render_bounded_messages(
        questions,
        question_section,
        max_rendered=mailbox_rendering.MAX_RENDERED_NOTICES,
        max_bytes=mailbox_rendering.MAX_RENDERED_NOTICE_BYTES,
    )
    return list(bounded.lines)


def _outbox_notice_text_lines(
    notices: tuple[mailbox.MailboxMessage, ...],
) -> list[str]:
    """Render notice bodies under explicit count and complete-byte bounds."""

    def notice_section(
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
                        f"Notice {message.id} (seq {message.seq}):",
                        MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        MAILBOX_MESSAGE_END,
                    )
                )
        else:
            lines.append(
                "No selected notice records fit within the "
                f"{mailbox_rendering.MAX_RENDERED_NOTICE_BYTES:,}-byte "
                "text rendering budget."
            )

        omission_notices: list[str] = []
        if record_cap_omitted:
            omission_notices.append(
                f"{len(record_cap_omitted)} older selected notice records "
                f"were omitted by the "
                f"{mailbox_rendering.MAX_RENDERED_NOTICES}-record cap: "
                f"{_message_references(record_cap_omitted)}."
            )
        if individually_oversized:
            omission_notices.append(
                f"{len(individually_oversized)} selected notice record(s) "
                "exceeded the text byte budget individually and were omitted "
                f"in full: {_message_references(individually_oversized)}."
            )
        if aggregate_budget_omitted:
            omission_notices.append(
                f"{len(aggregate_budget_omitted)} oldest remaining selected "
                "notice record(s) were omitted in full to keep the rendered "
                "section within the text byte budget: "
                f"{_message_references(aggregate_budget_omitted)}."
            )
        if omission_notices:
            lines.extend(
                (
                    "",
                    " ".join(omission_notices),
                    "The physical cursor advances through every mailbox "
                    "record, including records filtered out or omitted from "
                    "text rendering.",
                )
            )
        return lines

    bounded = mailbox_rendering.render_bounded_messages(
        notices,
        notice_section,
        max_rendered=mailbox_rendering.MAX_RENDERED_NOTICES,
        max_bytes=mailbox_rendering.MAX_RENDERED_NOTICE_BYTES,
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
    inline_source: str = "--text",
) -> str | None:
    inline = getattr(args, "text", None)
    from_stdin = bool(getattr(args, "text_stdin", False))
    if inline is None and not from_stdin:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=mailbox.REJECT_MISSING_TEXT,
            error=(
                f"exactly one of {inline_source} and --text-stdin is required"
            ),
        )
        return None
    if inline is not None and from_stdin:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=ERROR_INVALID_TEXT_SOURCE,
            error=(
                f"exactly one of {inline_source} and --text-stdin is required"
            ),
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


def _optional_note_from_args(
    args: argparse.Namespace,
) -> tuple[bool, str | None]:
    """Return an optional resolution note while preserving an empty note."""
    inline = getattr(args, "note", None)
    from_stdin = bool(getattr(args, "note_stdin", False))
    if inline is not None and from_stdin:
        _print_input_error(
            args,
            command_name=COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=ERROR_INVALID_NOTE_SOURCE,
            error="at most one of --note and --note-stdin may be supplied",
        )
        return False, None
    if not from_stdin:
        if inline is not None and not isinstance(inline, str):
            _print_input_error(
                args,
                command_name=COMMAND_INBOX_RESOLVE,
                text_label="inbox resolve",
                reason=mailbox.REJECT_INVALID_NOTE,
                error="resolution note must be text",
            )
            return False, None
        return True, inline
    try:
        if sys.stdin is None:
            raise ValueError("standard input is unavailable")
        stdin_stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw_note = stdin_stream.read()
        if isinstance(raw_note, bytes):
            note = raw_note.decode("utf-8", errors="strict")
        elif isinstance(raw_note, str):
            raw_note.encode("utf-8", errors="strict")
            note = raw_note
        else:
            raise TypeError("standard input did not return text or bytes")
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=ERROR_STDIN_READ,
            error=f"cannot read resolution note from stdin: {error}",
        )
        return False, None
    return True, note


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


def _validated_include(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> str | None:
    value = getattr(args, "include", OUTBOX_INCLUDE_QUESTIONS)
    if value not in OUTBOX_INCLUDE_MODES:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-include",
            error="--include must be one of questions, notices, or all",
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
    supervisor_id: str | None = None,
) -> mailbox.MailboxLease:
    """Heartbeat a live supervisor's lease, or take over when none is alive.

    A fresh, current-version lease that carries a ``run_id`` proves a live
    supervisor is projecting this plan, so re-emit its ``supervisor_id`` and
    question projection verbatim with only ``updated_at`` refreshed — a
    heartbeat, not a takeover. A missing, stale, v1, or unprojected lease
    means no supervisor is running, so the caller legitimately becomes one:
    write ``supervisor_id`` (minted fresh when omitted) with no projection.
    A lease read failure degrades to the takeover path (fail-open, as the
    blind write always was).
    """
    lease: mailbox.MailboxLease | None
    try:
        view = mailbox.read_lease(plan_dir, state_dir=state_dir)
    except (OSError, mailbox.MailboxError):
        lease = None
    else:
        lease = view.lease if view.fresh else None
    if (
        lease is not None
        and lease.version == mailbox.MAILBOX_LEASE_VERSION
        and isinstance(lease.run_id, str)
        and lease.run_id.strip()
    ):
        return mailbox.write_lease(
            plan_dir,
            lease.supervisor_id,
            state_dir=state_dir,
            run_id=lease.run_id,
            projection_seq=lease.projection_seq,
            unanswered_questions=lease.unanswered_questions,
            latest_unanswered_question_id=(
                lease.latest_unanswered_question_id
            ),
            latest_unanswered_question_created_at=(
                lease.latest_unanswered_question_created_at
            ),
        )
    return mailbox.write_lease(
        plan_dir,
        str(uuid.uuid4()) if supervisor_id is None else supervisor_id,
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
    try:
        # Establish the lease first.  If the state root is unwritable, this
        # avoids appending a command the caller may retry after seeing failure.
        # The reported supervisor_id comes from the lease that actually landed
        # on disk: the live supervisor's on a heartbeat, ours on a takeover.
        lease = _write_supervisor_lease(
            context.snapshot.plan_dir,
            args.state_dir,
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
    return context, message, lease.supervisor_id


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


def _status_question_ownership(
    context: cli_common.PlanCommandContext,
) -> tuple[
    str | None,
    str | None,
    int | None,
    mailbox.WaiterOrigin | None,
]:
    """Return exact live-turn ownership when a worker ask can prove it.

    Historical callers could append a question with only the best-effort run
    identity. Preserve that compatibility fallback, but stamp the complete
    ownership tuple whenever the status snapshot exposes the active harness
    turn. The runner relies on the complete tuple to correlate a worker's
    answerless pause with the turn that subsequently emits ``RUNNER_PAUSE``.
    """
    run_id = _status_run_id(context)
    runner_state = context.status.get("runner_state")
    if run_id is None or not isinstance(runner_state, dict):
        return run_id, None, None, None
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        return run_id, None, None, None
    session_id = active_turn.get("session_id")
    turn_index = active_turn.get("turn_index")
    if (
        active_turn.get("run_id") != run_id
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(turn_index, bool)
        or not isinstance(turn_index, int)
        or turn_index < 1
    ):
        return run_id, None, None, None
    return (
        run_id,
        session_id,
        turn_index,
        mailbox.WAITER_ORIGIN_WORKER,
    )


def _active_status_run_id(
    context: cli_common.PlanCommandContext,
) -> str | None:
    """Return the authoritative live run id from one status projection."""
    runner_state = context.status.get("runner_state")
    lock = context.status.get("lock")
    if not isinstance(runner_state, dict) or not isinstance(lock, dict):
        return None
    run_id = runner_state.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    if (
        not runner_state.get("present")
        or runner_state.get("error")
        or runner_state.get("paused")
        or runner_state.get("run_finished_at")
        or not lock.get("active")
    ):
        return None
    return run_id


def _active_turn_ownership(
    context: cli_common.PlanCommandContext,
) -> tuple[str, str, int] | None:
    """Return exact ownership for the active worker turn, if authoritative."""
    run_id = _active_status_run_id(context)
    runner_state = context.status.get("runner_state")
    if run_id is None or not isinstance(runner_state, dict):
        return None
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        return None
    session_id = active_turn.get("session_id")
    turn_index = active_turn.get("turn_index")
    if (
        active_turn.get("run_id") != run_id
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(turn_index, bool)
        or not isinstance(turn_index, int)
        or turn_index < 1
    ):
        return None
    return run_id, session_id, turn_index


def _mailbox_reply_projection(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    question_id: str,
) -> _MailboxReplyProjection | None:
    """Capture fresh v2 lease evidence for a reply before renewing the lease.

    The lease supplies the owning ``run_id`` and supervisor liveness; it is
    not required to already know the question. Accept it when its projection
    names ``question_id`` as the latest unanswered question, or when it
    demonstrably predates the question — its ``projection_seq`` is behind the
    question's mailbox sequence — and attributes the same run the question
    names. A ``projection_seq`` of 0 is a valid projection of a then-empty
    mailbox and predates the mailbox's first question.
    """
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
        or lease.projection_seq < 0
    ):
        return None
    if lease.latest_unanswered_question_id == question_id:
        return _MailboxReplyProjection(
            run_id=lease.run_id,
            projection_seq=lease.projection_seq,
            question_id=question_id,
        )
    try:
        snapshot = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except (OSError, mailbox.MailboxError):
        return None
    question = next(
        (
            message
            for message in snapshot.messages
            if message.kind == mailbox.KIND_QUESTION
            and message.id == question_id
        ),
        None,
    )
    if (
        question is None
        or lease.projection_seq >= question.seq
        or question.run_id != lease.run_id
    ):
        return None
    return _MailboxReplyProjection(
        run_id=lease.run_id,
        projection_seq=lease.projection_seq,
        question_id=question_id,
        lease_predates_question=True,
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
                # A lease that predates the question carries a stale
                # generation; the reply's seq is monotonic in the same file,
                # so it substitutes as a valid generation counter.
                "mailbox_projection_sequence": str(
                    message.seq
                    if projection.lease_predates_question
                    else projection.projection_seq
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


def _notify_agent_mux_mailbox_resolution(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    message: mailbox.MailboxMessage,
    projection: _MailboxReplyProjection | None,
    *,
    active_run_id: str,
) -> dict[str, Any] | None:
    """Best-effort fast clear after an authoritative answerless closure."""
    if (
        projection is None
        or projection.run_id != active_run_id
        or message.to != projection.question_id
        or message.kind != mailbox.KIND_RESOLUTION
        or message.action not in mailbox.RESOLUTION_ACTIONS
    ):
        return None
    try:
        post_resolution = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
        if (
            post_resolution.last_seq < message.seq
            or post_resolution.open_questions(projection.run_id)
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
            agent_mux_delivery.MAILBOX_RESOLUTION_EVENT,
            payload,
            reason=agent_mux_delivery.MAILBOX_RESOLUTION_REASON,
            active_turn=None,
            extra={
                "question_id": projection.question_id,
                "resolution_action": message.action,
                "mailbox_projection_sequence": str(
                    message.seq
                    if projection.lease_predates_question
                    else projection.projection_seq
                ),
                "source_event_id": (
                    f"{projection.run_id}:mailbox-resolution:{message.seq}"
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
    except Exception:  # noqa: BLE001 - resolutions never depend on AgentMux
        return None


def _record_notice_published_event(
    context: cli_common.PlanCommandContext,
    message: mailbox.MailboxMessage,
) -> None:
    """Best-effort body-free tail evidence for a published worker notice."""
    events_path = context.status.get("events_path")
    if not isinstance(events_path, str) or not events_path:
        return
    text = message.text
    if not isinstance(text, str):
        return
    try:
        events.append_event(
            events_path,
            events.Event(
                name=events.MAILBOX_EVENT,
                payload={
                    "action": events.MAILBOX_ACTION_NOTICE_PUBLISHED,
                    "run_id": message.run_id,
                    "session_id": message.session_id,
                    "turn_index": message.turn_index,
                    "message_id": message.id,
                    "seq": message.seq,
                    "length": len(text),
                    "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                },
            ),
        )
    except Exception:  # noqa: BLE001 - notice publication stays authoritative
        pass


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


def inbox_resolve_from_args(args: argparse.Namespace) -> int:
    """Atomically append the first answerless closure for a live question."""
    action = getattr(args, "action", None)
    if action not in mailbox.RESOLUTION_ACTIONS:
        _print_input_error(
            args,
            command_name=COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=mailbox.REJECT_INVALID_ACTION,
            error="--action must be either continue or pause",
        )
        return 1
    note_ok, note = _optional_note_from_args(args)
    if not note_ok:
        return 1
    context = _resolve(
        args,
        command_name=COMMAND_INBOX_RESOLVE,
        text_label="inbox resolve",
    )
    if context is None:
        return 1
    run_id = _active_status_run_id(context)
    if run_id is None:
        _print_input_error(
            args,
            command_name=COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            reason=ERROR_NO_ACTIVE_RUN,
            error="no active run can consume a question resolution",
        )
        return 1
    projection = _mailbox_reply_projection(context, args, args.to)
    try:
        lease = _write_supervisor_lease(
            context.snapshot.plan_dir,
            args.state_dir,
        )
        message = mailbox.append_resolution(
            context.snapshot.plan_dir,
            args.to,
            action,
            expected_run_id=run_id,
            note=note,
            state_dir=args.state_dir,
        )
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=COMMAND_INBOX_RESOLVE,
            text_label="inbox resolve",
            error=error,
        )
        return 1

    _notify_agent_mux_mailbox_resolution(
        context,
        args,
        message,
        projection,
        active_run_id=run_id,
    )

    payload = responses.success_payload(
        COMMAND_INBOX_RESOLVE,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        lease_path=str(
            mailbox.lease_path_for_plan(
                context.snapshot.plan_dir,
                args.state_dir,
            )
        ),
        supervisor_id=lease.supervisor_id,
        run_id=run_id,
        message=_message_payload(message),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Resolved question {args.to} with action {action} "
            f"(resolution {message.id}, seq {message.seq})"
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


def notify_from_args(args: argparse.Namespace) -> int:
    """Publish a nonblocking notice owned by the exact active worker turn."""
    text = _body_from_args(
        args,
        command_name=COMMAND_NOTIFY,
        text_label="notify",
        inline_source="TEXT",
    )
    if text is None:
        return 1
    context = _resolve(
        args,
        command_name=COMMAND_NOTIFY,
        text_label="notify",
    )
    if context is None:
        return 1
    ownership = _active_turn_ownership(context)
    if ownership is None:
        _print_input_error(
            args,
            command_name=COMMAND_NOTIFY,
            text_label="notify",
            reason=ERROR_NO_ACTIVE_RUN,
            error="no active worker turn can publish a notice",
        )
        return 1
    run_id, session_id, turn_index = ownership
    try:
        message = mailbox.append_notice(
            context.snapshot.plan_dir,
            text,
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
            state_dir=args.state_dir,
        )
    except mailbox.MailboxError as error:
        _print_mailbox_error(
            args,
            command_name=COMMAND_NOTIFY,
            text_label="notify",
            error=error,
        )
        return 1

    _record_notice_published_event(context, message)

    payload = responses.success_payload(
        COMMAND_NOTIFY,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        message=_message_payload(message),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Published notice {message.id} for session {session_id} "
            f"turn {turn_index} (seq {message.seq})."
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


def _wait_for_closure(
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
                closure = view.closure_for(question_id)
                if closure is not None:
                    return closure, False

        now = time.monotonic()
        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            return None, True
        time.sleep(min(ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now))


def _wait_for_reply(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
    initial_view: mailbox.MailboxView | None = None,
) -> tuple[mailbox.MailboxMessage | None, bool]:
    """Compatibility wrapper for callers of the former reply-only wait."""
    return _wait_for_closure(
        plan_dir,
        question_id,
        state_dir=state_dir,
        timeout=timeout,
        initial_view=initial_view,
    )


def ask_from_args(args: argparse.Namespace) -> int:
    """Append or resume a worker question, optionally waiting for closure."""
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
            (
                run_id,
                session_id,
                turn_index,
                waiter_origin,
            ) = _status_question_ownership(context)
            question = mailbox.append_question(
                context.snapshot.plan_dir,
                text,
                state_dir=args.state_dir,
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
                waiter_origin=waiter_origin,
            )
        else:
            initial_view = mailbox.read_mailbox(
                context.snapshot.plan_dir,
                state_dir=args.state_dir,
            )
            question = _question_from_view(initial_view, question_id)

        closure = (
            initial_view.closure_for(question.id)
            if initial_view is not None
            else None
        )
        timed_out = False
        if args.wait and closure is None:
            closure, timed_out = _wait_for_closure(
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

    reply = (
        closure if closure is not None and closure.kind == mailbox.KIND_REPLY
        else None
    )
    resolution = (
        closure
        if closure is not None and closure.kind == mailbox.KIND_RESOLUTION
        else None
    )
    pause_required = bool(
        resolution is not None
        and resolution.action == mailbox.RESOLUTION_ACTION_PAUSE
    )

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
        resolved=resolution is not None,
        pause_required=pause_required,
        timed_out=timed_out,
        reply=None if reply is None else _message_payload(reply),
        resolution=(
            None if resolution is None else _message_payload(resolution)
        ),
    )
    if args.json:
        responses.print_json(payload)
    elif reply is not None:
        print(f"Reply to question {question.id}:")
        _print_framed_message(reply)
    elif resolution is not None:
        print(f"Resolution for question {question.id}:")
        _print_framed_message(resolution)
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


def _wait_for_questions(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None,
    since: int,
    timeout: float,
    supervisor_id: str,
    include: str = OUTBOX_INCLUDE_QUESTIONS,
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
    kinds = {
        OUTBOX_INCLUDE_QUESTIONS: (mailbox.KIND_QUESTION,),
        OUTBOX_INCLUDE_NOTICES: (mailbox.KIND_NOTICE,),
        OUTBOX_INCLUDE_ALL: (mailbox.KIND_QUESTION, mailbox.KIND_NOTICE),
    }[include]

    # Initial establishment remains fail-closed: without it the caller has no
    # signal that supervision ever started. Only later renewals may degrade.
    landed_lease = _write_supervisor_lease(
        plan_dir,
        state_dir,
        supervisor_id,
    )
    landed_supervisor_id = landed_lease.supervisor_id
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
                selected = view.messages_since(since, kinds=kinds)
                if selected:
                    return _OutboxWaitResult(
                        questions=tuple(
                            message
                            for message in selected
                            if message.kind == mailbox.KIND_QUESTION
                        ),
                        notices=tuple(
                            message
                            for message in selected
                            if message.kind == mailbox.KIND_NOTICE
                        ),
                        include=include,
                        supervisor_id=landed_supervisor_id,
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
                notices=(),
                include=include,
                supervisor_id=landed_supervisor_id,
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
                landed_lease = _write_supervisor_lease(
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
                landed_supervisor_id = landed_lease.supervisor_id
            next_lease_at = now + LEASE_RENEW_INTERVAL_SECONDS
        sleep_for = min(
            OUTBOX_WAIT_POLL_INTERVAL_SECONDS,
            deadline - now,
            max(0.0, next_lease_at - now),
        )
        if sleep_for > 0:
            time.sleep(sleep_for)


def outbox_wait_from_args(args: argparse.Namespace) -> int:
    """Long-poll selected worker records after a stateless physical cursor."""
    since = _validated_since(
        args,
        command_name=COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if since is None:
        return 1
    include = _validated_include(
        args,
        command_name=COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if include is None:
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
            include=include,
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
        supervisor_id=wait_result.supervisor_id,
        since=since,
        next_since=wait_result.next_since,
        timeout=timeout,
        timed_out=wait_result.timed_out,
        include=wait_result.include,
        questions=[
            _message_payload(question)
            for question in wait_result.questions
        ],
        notices=[
            _message_payload(notice)
            for notice in wait_result.notices
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
        if not wait_result.questions and not wait_result.notices:
            selected_label = {
                OUTBOX_INCLUDE_QUESTIONS: "questions",
                OUTBOX_INCLUDE_NOTICES: "notices",
                OUTBOX_INCLUDE_ALL: "questions or notices",
            }[wait_result.include]
            print(f"No {selected_label} after seq {since}; timed out.")
        else:
            if wait_result.questions:
                print(
                    "\n".join(
                        _outbox_question_text_lines(wait_result.questions)
                    )
                )
            if wait_result.notices:
                print("\n".join(_outbox_notice_text_lines(wait_result.notices)))
            print(f"Next cursor: {wait_result.next_since}")
    return 0
