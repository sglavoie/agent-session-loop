"""Text and framed rendering plus error printing for mailbox commands."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    cli_common,
    mailbox_cli_constants,
    mailbox_constants,
    mailbox_models,
    mailbox_rendering,
)


def message_payload(message: mailbox_models.MailboxMessage) -> dict[str, Any]:
    return message.to_payload()


def print_framed_message(message: mailbox_models.MailboxMessage) -> None:
    """Render untrusted-shaped message text as one delimited ASCII JSON line."""
    print(mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN)
    print(mailbox_rendering.message_json(message))
    print(mailbox_cli_constants.MAILBOX_MESSAGE_END)


def _message_references(
    messages: Sequence[mailbox_models.MailboxMessage],
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
    references = [f"seq {message.seq} (id {message.id})" for message in selected]
    if omitted_count:
        references.insert(
            len(selected) // 2,
            f"{omitted_count} additional references not listed",
        )
    return ", ".join(references)


def inbox_read_text_lines(
    guidance: tuple[mailbox_models.MailboxMessage, ...],
    *,
    peek: bool,
    acknowledgement: mailbox_models.MailboxMessage | None,
    acknowledgement_error: dict[str, Any] | None,
) -> list[str]:
    """Render one bounded human-readable guidance snapshot."""

    def guidance_section(
        records: Sequence[mailbox_rendering.RenderedMessage],
        record_cap_omitted: Sequence[mailbox_models.MailboxMessage],
        individually_oversized: Sequence[mailbox_models.MailboxMessage],
        aggregate_budget_omitted: Sequence[mailbox_models.MailboxMessage],
    ) -> list[str]:
        lines: list[str] = []
        if records:
            for message, record_json in records:
                lines.extend(
                    (
                        f"Guidance {message.id} (seq {message.seq}):",
                        mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        mailbox_cli_constants.MAILBOX_MESSAGE_END,
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


def outbox_question_text_lines(
    questions: tuple[mailbox_models.MailboxMessage, ...],
) -> list[str]:
    """Render question bodies under the notice count and byte bounds."""

    def question_section(
        records: Sequence[mailbox_rendering.RenderedMessage],
        record_cap_omitted: Sequence[mailbox_models.MailboxMessage],
        individually_oversized: Sequence[mailbox_models.MailboxMessage],
        aggregate_budget_omitted: Sequence[mailbox_models.MailboxMessage],
    ) -> list[str]:
        lines: list[str] = []
        if records:
            for message, record_json in records:
                lines.extend(
                    (
                        f"Question {message.id} (seq {message.seq}):",
                        mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        mailbox_cli_constants.MAILBOX_MESSAGE_END,
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


def outbox_notice_text_lines(
    notices: tuple[mailbox_models.MailboxMessage, ...],
) -> list[str]:
    """Render notice bodies under explicit count and complete-byte bounds."""

    def notice_section(
        records: Sequence[mailbox_rendering.RenderedMessage],
        record_cap_omitted: Sequence[mailbox_models.MailboxMessage],
        individually_oversized: Sequence[mailbox_models.MailboxMessage],
        aggregate_budget_omitted: Sequence[mailbox_models.MailboxMessage],
    ) -> list[str]:
        lines: list[str] = []
        if records:
            for message, record_json in records:
                lines.extend(
                    (
                        f"Notice {message.id} (seq {message.seq}):",
                        mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        mailbox_cli_constants.MAILBOX_MESSAGE_END,
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


def outbox_supervision_end_text_lines(
    supervision_ends: tuple[mailbox_models.MailboxMessage, ...],
) -> list[str]:
    """Render terminal supervision records under the notice bounds."""

    def supervision_end_section(
        records: Sequence[mailbox_rendering.RenderedMessage],
        record_cap_omitted: Sequence[mailbox_models.MailboxMessage],
        individually_oversized: Sequence[mailbox_models.MailboxMessage],
        aggregate_budget_omitted: Sequence[mailbox_models.MailboxMessage],
    ) -> list[str]:
        lines: list[str] = []
        if records:
            for message, record_json in records:
                lines.extend(
                    (
                        f"Supervision end {message.id} (seq {message.seq}):",
                        mailbox_cli_constants.MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        mailbox_cli_constants.MAILBOX_MESSAGE_END,
                    )
                )
        else:
            lines.append(
                "No selected supervision end records fit within the "
                f"{mailbox_rendering.MAX_RENDERED_NOTICE_BYTES:,}-byte "
                "text rendering budget."
            )

        omission_notices: list[str] = []
        if record_cap_omitted:
            omission_notices.append(
                f"{len(record_cap_omitted)} older selected supervision end "
                f"records were omitted by the "
                f"{mailbox_rendering.MAX_RENDERED_NOTICES}-record cap: "
                f"{_message_references(record_cap_omitted)}."
            )
        if individually_oversized:
            omission_notices.append(
                f"{len(individually_oversized)} selected supervision end "
                "record(s) exceeded the text byte budget individually and "
                f"were omitted in full: "
                f"{_message_references(individually_oversized)}."
            )
        if aggregate_budget_omitted:
            omission_notices.append(
                f"{len(aggregate_budget_omitted)} oldest remaining selected "
                "supervision end record(s) were omitted in full to keep the "
                "rendered section within the text byte budget: "
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
        supervision_ends,
        supervision_end_section,
        max_rendered=mailbox_rendering.MAX_RENDERED_NOTICES,
        max_bytes=mailbox_rendering.MAX_RENDERED_NOTICE_BYTES,
    )
    return list(bounded.lines)


def outbox_lost_text_lines(
    plan_dir: str | Path,
    reason: str,
    evidence: dict[str, Any] | None,
) -> list[str]:
    """Render a dead supervision with the run evidence that decides what to do.

    A helper killed under a live run needs a relaunched supervision; a helper
    that never started under no run needs the whole launch redone. The lock and
    session counts are what separate the two.
    """
    lines = [
        {
            mailbox_cli_constants.SUPERVISE_LOST_HELPER_EXITED: (
                "The supervising helper for this launch recorded its process "
                "identity and that process is gone; it never wrote a "
                "supervision end record."
            ),
            mailbox_cli_constants.SUPERVISE_LOST_HELPER_NEVER_STARTED: (
                "The supervising helper for this launch never recorded a "
                "process identity, and no run is holding this plan."
            ),
        }[reason]
    ]
    if evidence is None:
        lines.append("Run evidence: unavailable; the plan status could not be read.")
    else:
        active_turn = evidence["active_turn"]
        lock = (
            f"active (pid {evidence['lock_pid']})"
            if evidence["lock_active"]
            else "not active"
        )
        runner_state = {
            mailbox_cli_constants.RUNNER_STATE_FRESH: "fresh",
            mailbox_cli_constants.RUNNER_STATE_ABSENT: "absent",
            mailbox_cli_constants.RUNNER_STATE_UNREADABLE: "unreadable",
            mailbox_cli_constants.RUNNER_STATE_FINISHED: "finished",
            mailbox_cli_constants.RUNNER_STATE_STALE: (
                "stale (unfinished, its runner is gone)"
            ),
        }.get(str(evidence.get("runner_state")), "unknown")
        lines.extend(
            (
                f"Run evidence: lock {lock}, "
                f"run_id {evidence['run_id'] or 'none'}, "
                f"runner state {runner_state}, "
                f"sessions {evidence['sessions_complete']}/"
                f"{evidence['sessions_total']} complete.",
                "Active turn: none."
                if active_turn is None
                else (
                    f"Active turn: session {active_turn['session_id']}, "
                    f"idle {active_turn['idle_seconds']}s."
                ),
            )
        )
    lines.append(f"Next step: read `asl status {plan_dir} --json`.")
    return lines


def mailbox_error_details(error: mailbox_models.MailboxError) -> dict[str, Any]:
    return {} if error.path is None else {"path": str(error.path)}


#: Sentences for the mailbox rejections a CLI user can cause. ``{detail!r}`` is
#: the id the rejection names. Every other reason keeps ``str(error)``; the
#: machine code stays in ``reason`` either way.
_MAILBOX_ERROR_SENTENCES: dict[str, str] = {
    mailbox_constants.REJECT_INVALID_TARGET: "the question id must be a UUID",
    mailbox_constants.REJECT_MISSING_TEXT: "the message text must not be empty",
    mailbox_constants.REJECT_QUESTION_NOT_FOUND: (
        "no question with id {detail!r} in this plan's mailbox"
    ),
    mailbox_constants.REJECT_QUESTION_NOT_OPEN: "question {detail!r} is no longer open",
    mailbox_constants.REJECT_QUESTION_ALREADY_ANSWERED: (
        "question {detail!r} has already been answered"
    ),
    mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED: (
        "question {detail!r} has already been resolved"
    ),
    mailbox_constants.REJECT_QUESTION_NOT_WAITING: (
        "nothing is waiting on question {detail!r}"
    ),
    mailbox_constants.REJECT_QUESTION_ALREADY_WAITING: (
        "question {detail!r} already has a waiter"
    ),
    mailbox_constants.REJECT_GUIDANCE_NOT_FOUND: (
        "no guidance with id {detail!r} in this plan's mailbox"
    ),
}


def mailbox_error_message(error: mailbox_models.MailboxError) -> str:
    """Describe ``error`` as a sentence rather than its bare reason code."""
    template = _MAILBOX_ERROR_SENTENCES.get(error.reason)
    if template is None:
        return str(error)
    needs_detail = "{detail" in template
    if needs_detail and error.detail is None:
        return str(error)
    return template.format(detail=error.detail)


def print_mailbox_error(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
    error: mailbox_models.MailboxError,
) -> None:
    cli_common.print_error(
        args,
        command_name=command_name,
        text_label=text_label,
        reason=error.reason,
        error=mailbox_error_message(error),
        details=mailbox_error_details(error),
    )


def print_input_error(
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
