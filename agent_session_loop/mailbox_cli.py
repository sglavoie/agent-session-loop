"""Command-line producers and consumers for the per-plan mailbox."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import (
    agent_mux_delivery,
    cli_common,
    events,
    intervention,
    mailbox,
    mailbox_rendering,
    plans,
    redirect_ack,
    redirect_store,
    responses,
    state,
    supervision_liveness,
)

COMMAND_INBOX_GUIDANCE = "inbox-guidance"
COMMAND_INBOX_REPLY = "inbox-reply"
COMMAND_INBOX_RESOLVE = "inbox-resolve"
COMMAND_INBOX_READ = "inbox-read"
COMMAND_INBOX_ACKNOWLEDGE = "inbox-acknowledge"
COMMAND_ASK = "ask"
COMMAND_NOTIFY = "notify"
COMMAND_OUTBOX_WAIT = "outbox-wait"
COMMAND_OUTBOX_SUPERVISE = "outbox-supervise"

OUTBOX_INCLUDE_QUESTIONS = "questions"
OUTBOX_INCLUDE_NOTICES = "notices"
OUTBOX_INCLUDE_ALL = "all"
OUTBOX_INCLUDE_MODES = (
    OUTBOX_INCLUDE_QUESTIONS,
    OUTBOX_INCLUDE_NOTICES,
    OUTBOX_INCLUDE_ALL,
)
# Deliberately outside OUTBOX_INCLUDE_MODES: that tuple is the `wait` option
# surface, and `wait` must not gain a fourth mode.
OUTBOX_INCLUDE_SUPERVISION = "supervision"

SUPERVISE_TOKEN_IDLE = "IDLE"
SUPERVISE_TOKEN_ACTION = "ACTION"
SUPERVISE_TOKEN_DONE = "DONE"
SUPERVISE_TOKEN_LOST = "LOST"

SUPERVISE_LOST_HELPER_EXITED = "helper-exited-without-record"
SUPERVISE_LOST_HELPER_NEVER_STARTED = "helper-never-started"
# Only the launch race: a helper stamps its liveness record about a second in,
# so this is the window in which a missing record is not yet evidence of
# anything. A probe-confirmed death never waits it out.
SUPERVISE_LIVENESS_GRACE_SECONDS = 120.0

DEFAULT_ASK_WAIT_TIMEOUT_SECONDS = 90.0
# A guidance delivery wait is bounded much tighter than an answer wait: it ends
# on the run's own next delivery decision, not on a human's, and reporting
# ``queued`` promptly is more useful to a supervisor than blocking for one.
DEFAULT_GUIDANCE_DELIVERED_TIMEOUT_SECONDS = 30.0
DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS = 300.0
DEFAULT_OUTBOX_SUPERVISE_TIMEOUT_SECONDS = 550.0
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
    supervision_ends: tuple[mailbox.MailboxMessage, ...]
    include: str
    supervisor_id: str
    next_since: int
    timed_out: bool
    lease_supervision: _LeaseSupervision
    mailbox_view: mailbox.MailboxView | None = None
    lost_reason: str | None = None


@dataclass(frozen=True)
class _MailboxReplyProjection:
    """Authoritative pre-reply evidence for one supervisor question."""

    run_id: str
    projection_seq: int
    question_id: str
    lease_predates_question: bool = False


@dataclass(frozen=True)
class _MailboxQuestionProjection:
    """Body-free actionable question set surfaced by one outbox wait."""

    run_id: str
    projection_seq: int
    open_questions: tuple[mailbox.MailboxMessage, ...]
    latest_question: mailbox.MailboxMessage


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
    references = [f"seq {message.seq} (id {message.id})" for message in selected]
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


def _outbox_supervision_end_text_lines(
    supervision_ends: tuple[mailbox.MailboxMessage, ...],
) -> list[str]:
    """Render terminal supervision records under the notice bounds."""

    def supervision_end_section(
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
                        f"Supervision end {message.id} (seq {message.seq}):",
                        MAILBOX_MESSAGE_BEGIN,
                        record_json,
                        MAILBOX_MESSAGE_END,
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


def _supervision_run_evidence(
    plan_dir: str | Path,
    state_dir: str | Path | None,
) -> dict[str, Any] | None:
    """Return what the plan's own state says about a live run, or None.

    None means the question could not be answered, never that no run exists:
    a status read that fails must not be allowed to manufacture a death.
    """
    try:
        status = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
    except Exception:  # noqa: BLE001 - unreadable status is unprovable, not dead
        return None
    lock = status.get("lock")
    lock = lock if isinstance(lock, dict) else {}
    runner_state = status.get("runner_state")
    runner_state = runner_state if isinstance(runner_state, dict) else {}
    counts = status.get("counts")
    counts = counts if isinstance(counts, dict) else {}
    active_turn = status.get("active_turn")
    return {
        "lock_active": bool(lock.get("active")),
        "lock_pid": lock.get("pid"),
        "run_id": runner_state.get("run_id"),
        "runner_state_fresh": bool(
            runner_state.get("present")
            and not runner_state.get("error")
            and not runner_state.get("run_finished_at")
            and not runner_state.get("stale_unfinished")
        ),
        "active_turn": (
            {
                "session_id": active_turn.get("session_id"),
                "idle_seconds": active_turn.get("idle_seconds"),
            }
            if isinstance(active_turn, dict)
            else None
        ),
        "sessions_complete": counts.get("complete", 0),
        "sessions_total": sum(
            value for value in counts.values() if isinstance(value, int)
        ),
    }


def _run_evidence_is_live(evidence: dict[str, Any] | None) -> bool:
    if evidence is None:
        return True
    return bool(evidence["lock_active"] or evidence["runner_state_fresh"])


def _outbox_lost_text_lines(
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
            SUPERVISE_LOST_HELPER_EXITED: (
                "The supervising helper for this launch recorded its process "
                "identity and that process is gone; it never wrote a "
                "supervision end record."
            ),
            SUPERVISE_LOST_HELPER_NEVER_STARTED: (
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
        runner_state = (
            "fresh" if evidence["runner_state_fresh"] else "absent or finished"
        )
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


def _supervision_liveness_probe(
    plan_dir: str | Path,
    state_dir: str | Path | None,
    supervision_id: str,
    *,
    first_seen_at: datetime,
) -> Callable[[], str | None]:
    """Build the check that decides whether this launch's helper is gone.

    Two questions, in the only order that is safe. A record that exists is
    authoritative the moment ``ps`` disowns its process, so that verdict never
    waits out the grace window. A record that does not exist proves nothing on
    its own — a plain ``asl run`` supervised without the helper writes none —
    so it becomes a death only once the grace window has passed and the plan's
    own state shows no run holding it either.
    """

    def probe() -> str | None:
        record = supervision_liveness.read_record(
            plan_dir,
            supervision_id,
            state_dir=state_dir,
        )
        if record is not None:
            if supervision_liveness.probe(record) == supervision_liveness.DEAD:
                return SUPERVISE_LOST_HELPER_EXITED
            return None
        elapsed = (datetime.now(UTC) - first_seen_at).total_seconds()
        if elapsed < SUPERVISE_LIVENESS_GRACE_SECONDS:
            return None
        if _run_evidence_is_live(_supervision_run_evidence(plan_dir, state_dir)):
            return None
        return SUPERVISE_LOST_HELPER_NEVER_STARTED

    return probe


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
            error=(f"exactly one of {inline_source} and --text-stdin is required"),
        )
        return None
    if inline is not None and from_stdin:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason=ERROR_INVALID_TEXT_SOURCE,
            error=(f"exactly one of {inline_source} and --text-stdin is required"),
        )
        return None
    if from_stdin:
        try:
            body = cli_common.read_stdin_text()
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
        note = cli_common.read_stdin_text()
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
    # Anything that is not already a number or a numeric string is out of
    # range by definition, and the shared out-of-range message below is the
    # right report for it -- NaN is how it gets there.
    try:
        timeout = float(value) if isinstance(value, int | float | str) else math.nan
    except ValueError:
        timeout = math.nan
    if not math.isfinite(timeout) or timeout < 0 or timeout > MAX_WAIT_TIMEOUT_SECONDS:
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


def _validated_supervision_id(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> tuple[str | None, bool]:
    """Return the launch correlation filter and whether it validated."""
    value = getattr(args, "supervision_id", None)
    if value is None:
        return None, True
    if not mailbox.is_supervision_id(value):
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-supervision-id",
            error="--supervision-id must be a UUID",
        )
        return None, False
    return value, True


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
    """Heartbeat a live supervisor's lease, or take over when none is alive."""
    return intervention.write_supervisor_lease(plan_dir, state_dir, supervisor_id)


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
        # Optional evidence stays fail-open.
        with contextlib.suppress(Exception):
            before_lease(context)
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


def _active_turn_harness(
    context: cli_common.PlanCommandContext,
) -> str | None:
    """Return the harness running the active worker turn, if it is named."""
    runner_state = context.status.get("runner_state")
    if not isinstance(runner_state, dict):
        return None
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, dict):
        return None
    harness = active_turn.get("harness")
    return harness if isinstance(harness, str) and harness else None


def _record_guidance_read_deliveries(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    guidance: Sequence[mailbox.MailboxMessage],
) -> list[dict[str, Any]]:
    """Record each guidance in this snapshot as delivered by an explicit read.

    The cumulative acknowledgement has always implied this, but only in
    aggregate: it names a sequence number, so guidance the worker read for an
    unrelated reason became "read" alongside guidance it acted on, and nothing
    said which turn did the reading. A per-guidance record on the ``read``
    channel says exactly what happened — this worker, in this turn, was shown
    this record — which is what makes ``delivered-by-read`` evidence rather
    than an inference.

    Best effort by construction. The read already succeeded and the
    acknowledgement is what makes the guidance non-pending; a ledger that
    cannot take these records must not fail the command that delivered them.
    """
    ownership = _active_turn_ownership(context)
    if ownership is None:
        # Nothing durable can be said about which turn read this. The
        # acknowledgement still stands, and ``guidance_delivery_state`` still
        # reports ``delivered-by-read`` from it.
        return []
    run_id, session_id, turn_index = ownership
    harness = _active_turn_harness(context)
    receipts: list[dict[str, Any]] = []
    for message in guidance:
        try:
            record = mailbox.append_guidance_delivery(
                context.snapshot.plan_dir,
                message.id,
                delivery_state=mailbox.GUIDANCE_STATE_DELIVERED_BY_READ,
                delivery_reason=mailbox.GUIDANCE_REASON_DELIVERED,
                channel=mailbox.GUIDANCE_CHANNEL_READ,
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
                harness=harness,
                state_dir=args.state_dir,
            )
        except OSError, mailbox.MailboxError:
            break
        receipts.append(_message_payload(record))
    return receipts


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
    except OSError, mailbox.MailboxError:
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
    except OSError, mailbox.MailboxError:
        return None
    question = next(
        (
            message
            for message in snapshot.messages
            if message.kind == mailbox.KIND_QUESTION and message.id == question_id
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
        if post_reply.last_seq < message.seq or post_reply.open_questions(
            projection.run_id
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
                "delivery_state": "accepted",
                "consumed": False,
                # A lease that predates the question carries a stale
                # generation; the reply's seq is monotonic in the same file,
                # so it substitutes as a valid generation counter.
                "mailbox_projection_sequence": str(
                    message.seq
                    if projection.lease_predates_question
                    else projection.projection_seq
                ),
                "source_event_id": (f"{projection.run_id}:mailbox-reply:{message.seq}"),
            },
            enabled=agent_mux_delivery.notify_enabled(),
            timeout=agent_mux_delivery.notify_timeout(),
            allocate_sequence=lambda: message.seq,
            record_outcome=lambda _outcome: None,
            resolve_binary=shutil.which,
            run_command=subprocess.run,
            emitted_at=lambda: datetime.now(UTC),
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
        if post_resolution.last_seq < message.seq or post_resolution.open_questions(
            projection.run_id
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
                "delivery_state": "accepted",
                "consumed": False,
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
            emitted_at=lambda: datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - resolutions never depend on AgentMux
        return None


def _outbox_question_projection(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    wait_result: _OutboxWaitResult,
) -> _MailboxQuestionProjection | None:
    """Persist and return the actionable question set surfaced by the wait.

    ``outbox wait`` deliberately returns physical question records even when
    they have already closed.  AgentMux notification is narrower: at least one
    returned question must still be open for one unambiguous live run.  Once a
    lease already carries a run projection, later waits keep that projection
    current (including a zero count) without turning a notice-only wait into a
    new positive handoff.
    """
    view = wait_result.mailbox_view
    if view is None:
        return None

    selected_ids = {question.id for question in wait_result.questions}
    selected_run_ids = {
        question.run_id
        for question in wait_result.questions
        if isinstance(question.run_id, str) and question.run_id
    }
    active_run_id = _active_status_run_id(context)

    projected_run_id: str | None = None
    try:
        lease_view = mailbox.read_lease(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except OSError, mailbox.MailboxError:
        lease_view = None
    if (
        lease_view is not None
        and lease_view.fresh
        and lease_view.lease is not None
        and lease_view.lease.version == mailbox.MAILBOX_LEASE_VERSION
        and isinstance(lease_view.lease.run_id, str)
        and lease_view.lease.run_id
    ):
        projected_run_id = lease_view.lease.run_id

    candidate_run_id: str | None = None
    if active_run_id in selected_run_ids:
        candidate_run_id = active_run_id
    elif projected_run_id in selected_run_ids:
        candidate_run_id = projected_run_id
    elif len(selected_run_ids) == 1:
        candidate_run_id = next(iter(selected_run_ids))

    # An existing projected supervisor stays authoritative even when this wait
    # returns no new question; refreshing its exact open set lets a later zero
    # projection provide the compatibility clear.  An unprojected wait only
    # acquires a run identity after it actually surfaces an actionable question.
    run_id = candidate_run_id or projected_run_id
    if run_id is None:
        return None
    open_questions = view.open_questions(run_id)
    actionable = tuple(
        question for question in open_questions if question.id in selected_ids
    )
    if projected_run_id is None and not actionable:
        return None

    projection_kwargs: dict[str, Any] = {
        "run_id": run_id,
        "projection_seq": view.last_seq,
        "unanswered_questions": len(open_questions),
    }
    if open_questions:
        latest = max(open_questions, key=lambda question: question.seq)
        projection_kwargs.update(
            latest_unanswered_question_id=latest.id,
            latest_unanswered_question_created_at=latest.created_at,
        )
    # Observation and AgentMux delivery remain fail-open after the initial
    # lease establishment; the returned question is still authoritative.
    with contextlib.suppress(OSError, mailbox.MailboxError):
        mailbox.write_lease(
            context.snapshot.plan_dir,
            wait_result.supervisor_id,
            state_dir=args.state_dir,
            **projection_kwargs,
        )

    if not actionable or not open_questions:
        return None
    latest_question = max(open_questions, key=lambda question: question.seq)
    return _MailboxQuestionProjection(
        run_id=run_id,
        projection_seq=view.last_seq,
        open_questions=open_questions,
        latest_question=latest_question,
    )


def _notify_agent_mux_mailbox_question(
    context: cli_common.PlanCommandContext,
    projection: _MailboxQuestionProjection | None,
) -> dict[str, Any] | None:
    """Best-effort push notification for one surfaced actionable open set."""
    if projection is None:
        return None
    try:
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
        latest = projection.latest_question
        return agent_mux_delivery.notify(
            agent_mux_delivery.MAILBOX_QUESTION_EVENT,
            payload,
            reason=agent_mux_delivery.MAILBOX_QUESTION_REASON,
            active_turn=None,
            extra={
                "question_id": latest.id,
                "question_created_at": latest.created_at,
                "unanswered_questions": len(projection.open_questions),
                "delivery_state": "open",
                "mailbox_projection_sequence": str(projection.projection_seq),
                "source_event_id": (
                    f"{projection.run_id}:mailbox-question:{latest.seq}"
                ),
            },
            enabled=agent_mux_delivery.notify_enabled(),
            timeout=agent_mux_delivery.notify_timeout(),
            allocate_sequence=lambda: latest.seq,
            record_outcome=lambda _outcome: None,
            resolve_binary=shutil.which,
            run_command=subprocess.run,
            emitted_at=lambda: datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - outbox delivery never depends on AgentMux
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
    # Notice publication stays authoritative.
    with contextlib.suppress(Exception):
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


def _delivery_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> float | None:
    """Return the validated delivery wait timeout, or None on invalid input."""
    requested = getattr(args, "wait_timeout", None)
    wait_delivered = bool(getattr(args, "wait_delivered", False))
    if requested is not None and not wait_delivered:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error="--wait-timeout requires --wait-delivered",
        )
        return None
    return _validated_timeout(
        args,
        value=(
            DEFAULT_GUIDANCE_DELIVERED_TIMEOUT_SECONDS
            if requested is None
            else requested
        ),
        option="--wait-timeout",
        command_name=command_name,
        text_label=text_label,
    )


def _wait_for_guidance_delivery(
    plan_dir: str | Path,
    guidance_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
    active_turn: tuple[str, str, int] | None,
) -> mailbox.MailboxMessage | None:
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
    mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
    token: object = _MAILBOX_TOKEN_UNSEEN
    while True:
        current = mailbox.mailbox_change_token(mailbox_path)
        if (
            token is _MAILBOX_TOKEN_UNSEEN
            or current is mailbox.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current != token
        ):
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
            token = current
            delivered = view.successful_guidance_delivery(guidance_id)
            if delivered is not None:
                return delivered
            for deferred in reversed(view.deferred_guidance_deliveries(guidance_id)):
                if deferred.delivery_reason == (
                    mailbox.GUIDANCE_REASON_AWAITING_CONFIRMATION
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
        time.sleep(min(ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now))


def inbox_guidance_from_args(args: argparse.Namespace) -> int:
    """Append supervisor guidance and renew the plan's supervisor lease.

    The append and the delivery are separate facts and are reported as such.
    A durable append is a success even when nothing has delivered it yet, so
    the exit code follows the append alone and ``delivered`` carries the rest.
    """
    text = _body_from_args(
        args,
        command_name=COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
    )
    if text is None:
        return 1
    wait_delivered = bool(getattr(args, "wait_delivered", False))
    delivery_timeout = _delivery_wait_timeout(
        args,
        command_name=COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
    )
    if delivery_timeout is None:
        return 1
    active_turn: tuple[str, str, int] | None = None

    def capture_active_turn(context: cli_common.PlanCommandContext) -> None:
        nonlocal active_turn
        active_turn = _active_turn_ownership(context)

    result = _supervisor_write(
        args,
        command_name=COMMAND_INBOX_GUIDANCE,
        text_label="inbox guidance",
        append=lambda plan_dir: mailbox.append_guidance(
            plan_dir,
            text,
            state_dir=args.state_dir,
        ),
        before_lease=capture_active_turn,
    )
    if result is None:
        return 1
    context, message, supervisor_id = result
    mailbox_path = _mailbox_path(context, args)
    delivery: mailbox.MailboxMessage | None = None
    if wait_delivered:
        try:
            delivery = _wait_for_guidance_delivery(
                context.snapshot.plan_dir,
                message.id,
                state_dir=args.state_dir,
                timeout=delivery_timeout,
                active_turn=active_turn,
            )
        except mailbox.MailboxError:
            # The guidance append is authoritative. A later observation failure
            # must stay a truthful successful append with delivered=false.
            delivery = None
    delivery_state: mailbox.GuidanceDeliveryState
    if delivery is None:
        # Without a delivery record the guidance is queued by definition: it is
        # durable and nothing has reported acting on it.
        delivery_state = mailbox.GUIDANCE_STATE_QUEUED
    else:
        # Every record `_wait_for_guidance_delivery` can return is a delivery
        # attempt -- successful or deferred -- so it carries a state.
        assert delivery.delivery_state is not None
        delivery_state = delivery.delivery_state
    delivered = delivery_state in mailbox.GUIDANCE_DELIVERED_STATES
    payload = responses.success_payload(
        COMMAND_INBOX_GUIDANCE,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(mailbox_path),
        lease_path=str(
            mailbox.lease_path_for_plan(context.snapshot.plan_dir, args.state_dir)
        ),
        supervisor_id=supervisor_id,
        message=_message_payload(message),
        appended=True,
        waited=wait_delivered,
        delivered=delivered,
        delivery_state=delivery_state,
        delivery_reason=None if delivery is None else delivery.delivery_reason,
        delivery=None if delivery is None else _message_payload(delivery),
    )
    if args.json:
        responses.print_json(payload)
    elif wait_delivered:
        # The state, not the append, leads the line. Without `--wait-delivered`
        # nothing was observed and the honest report is that the record is
        # durable; with it, this says which of the two actually happened.
        summary = mailbox.describe_guidance_delivery(
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
        queued = mailbox.describe_guidance_delivery(mailbox.GUIDANCE_STATE_QUEUED)
        print(
            f"Guidance {queued} (no delivery wait requested): {message.id} for "
            f"{context.snapshot.plan_dir} (seq {message.seq}, "
            f"mailbox {mailbox_path})"
        )
    return 0


def _mailbox_path(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
) -> Path:
    return mailbox.mailbox_path_for_plan(context.snapshot.plan_dir, args.state_dir)


def _consumption_wait_timeout(
    args: argparse.Namespace,
    *,
    command_name: str,
    text_label: str,
) -> float | None:
    requested = getattr(args, "wait_timeout", None)
    wait_consumed = bool(getattr(args, "wait_consumed", False))
    if requested is not None and not wait_consumed:
        _print_input_error(
            args,
            command_name=command_name,
            text_label=text_label,
            reason="invalid-timeout",
            error="--wait-timeout requires --wait-consumed",
        )
        return None
    return _validated_timeout(
        args,
        value=(DEFAULT_ASK_WAIT_TIMEOUT_SECONDS if requested is None else requested),
        option="--wait-timeout",
        command_name=command_name,
        text_label=text_label,
    )


def _wait_for_receipt(
    plan_dir: str | Path,
    closure_id: str,
    *,
    state_dir: str | Path | None,
    timeout: float,
) -> mailbox.MailboxMessage | None:
    deadline = time.monotonic() + timeout
    mailbox_path = mailbox.mailbox_path_for_plan(plan_dir, state_dir)
    token: object = _MAILBOX_TOKEN_UNSEEN
    while True:
        current = mailbox.mailbox_change_token(mailbox_path)
        if (
            token is _MAILBOX_TOKEN_UNSEEN
            or current is mailbox.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
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
        time.sleep(min(ASK_WAIT_POLL_INTERVAL_SECONDS, deadline - now))


def inbox_reply_from_args(args: argparse.Namespace) -> int:
    """Atomically append the first supervisor reply to a question."""
    text = _body_from_args(
        args,
        command_name=COMMAND_INBOX_REPLY,
        text_label="inbox reply",
    )
    if text is None:
        return 1
    consumption_timeout = _consumption_wait_timeout(
        args,
        command_name=COMMAND_INBOX_REPLY,
        text_label="inbox reply",
    )
    if consumption_timeout is None:
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
    receipt = None
    if getattr(args, "wait_consumed", False):
        try:
            receipt = _wait_for_receipt(
                context.snapshot.plan_dir,
                message.id,
                state_dir=args.state_dir,
                timeout=consumption_timeout,
            )
        except mailbox.MailboxError:
            # The closure append is authoritative. A later observation failure
            # must remain a truthful successful append with consumed=false.
            receipt = None
    consumed = receipt is not None
    payload = responses.success_payload(
        COMMAND_INBOX_REPLY,
        plan_dir=str(context.snapshot.plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        lease_path=str(
            mailbox.lease_path_for_plan(context.snapshot.plan_dir, args.state_dir)
        ),
        supervisor_id=supervisor_id,
        message=_message_payload(message),
        delivery_state="consumed" if consumed else "accepted",
        consumed=consumed,
        receipt=None if receipt is None else _message_payload(receipt),
    )
    if args.json:
        responses.print_json(payload)
    else:
        print(
            f"Accepted reply {message.id} to question {args.to} "
            f"(seq {message.seq}, consumed: {'yes' if consumed else 'no'})"
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
    consumption_timeout = _consumption_wait_timeout(
        args,
        command_name=COMMAND_INBOX_RESOLVE,
        text_label="inbox resolve",
    )
    if consumption_timeout is None:
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
    receipt = None
    if getattr(args, "wait_consumed", False):
        try:
            receipt = _wait_for_receipt(
                context.snapshot.plan_dir,
                message.id,
                state_dir=args.state_dir,
                timeout=consumption_timeout,
            )
        except mailbox.MailboxError:
            receipt = None
    consumed = receipt is not None

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
        delivery_state="consumed" if consumed else "accepted",
        consumed=consumed,
        receipt=None if receipt is None else _message_payload(receipt),
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
    read_receipts: list[dict[str, Any]] = []
    if guidance and not args.peek:
        # Receipts first, and they cannot starve the acknowledgement: delivery
        # records draw on their own reserve. Ordering this way keeps the
        # receipts describing the read that just happened even when the
        # acknowledgement below fails and the guidance stays pending.
        read_receipts = _record_guidance_read_deliveries(context, args, guidance)
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
            None if acknowledgement is None else _message_payload(acknowledgement)
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


def _live_attempt(
    context: cli_common.PlanCommandContext,
) -> redirect_store.RedirectTarget | None:
    """Return the attempt the run's own state says is running right now."""
    ownership = _active_turn_ownership(context)
    if ownership is None:
        return None
    run_id, session_id, turn_index = ownership
    return redirect_store.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=_active_turn_harness(context),
    )


def _redirect_refusal(
    error: redirect_ack.RedirectAckError | redirect_store.RedirectStoreError,
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
    context = _resolve(
        args,
        command_name=COMMAND_INBOX_ACKNOWLEDGE,
        text_label="inbox acknowledge",
    )
    if context is None:
        return 1
    attempt = _live_attempt(context)
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
            command_name=COMMAND_INBOX_ACKNOWLEDGE,
            text_label="inbox acknowledge",
            reason=redirect_ack.ERROR_NO_LIVE_ATTEMPT,
            error=(
                "no active turn owns this plan, so the attempt a receipt would "
                "name cannot be identified"
            ),
            details={"attempt": args.attempt},
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
        except redirect_store.RedirectStoreError as error:
            cli_common.print_error(
                args,
                command_name=COMMAND_INBOX_ACKNOWLEDGE,
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
            redirect_ack.RedirectAckError,
            redirect_store.RedirectStoreError,
        ) as error:
            refused.append(
                {"operation_id": operation_id, **_redirect_refusal(error)},
            )
            continue
        acknowledged.append(receipt.to_payload())

    payload = responses.success_payload(
        COMMAND_INBOX_ACKNOWLEDGE,
        plan_dir=str(context.snapshot.plan_dir),
        attempt=args.attempt,
        live_attempt=(None if attempt is None else redirect_ack.attempt_token(attempt)),
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
            if message.kind == mailbox.KIND_QUESTION and message.id == question_id
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
    wait_id: str | None = None,
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
                    if wait_id is not None:
                        try:
                            completion = mailbox.consume_wait_closure(
                                plan_dir,
                                question_id,
                                wait_id,
                                state_dir=state_dir,
                            )
                        except mailbox.MailboxError as error:
                            if error.reason != mailbox.REJECT_QUESTION_NOT_WAITING:
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
                            mailbox.append_receipt(
                                plan_dir,
                                question_id,
                                closure.id,
                                state_dir=state_dir,
                            )
                        except mailbox.MailboxError as error:
                            if error.reason != mailbox.REJECT_CLOSURE_ALREADY_CONSUMED:
                                raise
                    return closure, False

        now = time.monotonic()
        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            if wait_id is not None:
                completion = mailbox.finish_wait(
                    plan_dir,
                    question_id,
                    wait_id,
                    reason=mailbox.WAIT_END_REASON_TIMEOUT,
                    state_dir=state_dir,
                )
                if completion.closure is not None:
                    return completion.closure, False
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
    question_id_value = getattr(args, "question_id", None)
    # `--question-id` is a plain string option, so this is identity; spelling it
    # is what keeps the id a `str` rather than `Any` all the way to the lookup.
    question_id = None if question_id_value is None else str(question_id_value)
    if (text is None) == (question_id is None):
        _print_input_error(
            args,
            command_name=COMMAND_ASK,
            text_label="ask",
            reason="invalid-question-source",
            error="exactly one of question text and --question-id is required",
        )
        return 1
    if text is not None and (not isinstance(text, str) or text.strip() == ""):
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
    wait_start: mailbox.MailboxMessage | None = None
    created = text is not None
    try:
        if text is not None:
            (
                run_id,
                session_id,
                turn_index,
                waiter_origin,
            ) = _status_question_ownership(context)
            if args.wait:
                question, wait_start = mailbox.append_wait_question(
                    context.snapshot.plan_dir,
                    text,
                    state_dir=args.state_dir,
                    run_id=run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    waiter_origin=waiter_origin,
                )
            else:
                question = mailbox.append_question(
                    context.snapshot.plan_dir,
                    text,
                    state_dir=args.state_dir,
                    run_id=run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    waiter_origin=waiter_origin,
                    delivery_mode=mailbox.DELIVERY_MODE_ASYNC,
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
                    wait_start = mailbox.append_wait_start(
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
                mailbox.consume_wait_closure(
                    context.snapshot.plan_dir,
                    question.id,
                    wait_start.id,
                    state_dir=args.state_dir,
                )
            else:
                mailbox.append_receipt(
                    context.snapshot.plan_dir,
                    question.id,
                    closure.id,
                    state_dir=args.state_dir,
                )
        timed_out = False
        if args.wait and closure is None:
            closure, timed_out = _wait_for_closure(
                context.snapshot.plan_dir,
                question.id,
                state_dir=args.state_dir,
                timeout=wait_timeout,
                initial_view=initial_view,
                wait_id=None if wait_start is None else wait_start.id,
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
        closure if closure is not None and closure.kind == mailbox.KIND_REPLY else None
    )
    resolution = (
        closure
        if closure is not None and closure.kind == mailbox.KIND_RESOLUTION
        else None
    )
    pause_required = bool(
        resolution is not None and resolution.action == mailbox.RESOLUTION_ACTION_PAUSE
    )
    receipt = None
    if closure is not None:
        final_view = mailbox.read_mailbox(
            context.snapshot.plan_dir, state_dir=args.state_dir
        )
        receipt = final_view.receipt_for_closure(closure.id)
    consumed = receipt is not None

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
        resolution=(None if resolution is None else _message_payload(resolution)),
        wait_start=(None if wait_start is None else _message_payload(wait_start)),
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
        receipt=None if receipt is None else _message_payload(receipt),
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
    liveness_probe: Callable[[], str | None] | None = None,
) -> _OutboxWaitResult:
    started = time.monotonic()
    deadline = started + timeout
    next_lease_at = started + LEASE_RENEW_INTERVAL_SECONDS
    # The probe forks `ps`, so it rides the lease-renewal interval rather than
    # the mailbox poll. It starts due, because a short wait that never reaches
    # the first renewal must still be able to report a helper that is gone.
    next_probe_at = started
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
        OUTBOX_INCLUDE_ALL: (
            mailbox.KIND_QUESTION,
            mailbox.KIND_NOTICE,
            mailbox.KIND_SUPERVISION_END,
        ),
        OUTBOX_INCLUDE_SUPERVISION: (
            mailbox.KIND_QUESTION,
            mailbox.KIND_SUPERVISION_END,
        ),
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
                        supervision_ends=tuple(
                            message
                            for message in selected
                            if message.kind == mailbox.KIND_SUPERVISION_END
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
                        mailbox_view=view,
                    )

        now = time.monotonic()
        # After the mailbox read, so a woken record always outranks a lost
        # helper: the reader above has already returned anything selected.
        if liveness_probe is not None and now >= next_probe_at:
            lost_reason = liveness_probe()
            next_probe_at = now + LEASE_RENEW_INTERVAL_SECONDS
            if lost_reason is not None:
                return _OutboxWaitResult(
                    questions=(),
                    notices=(),
                    supervision_ends=(),
                    include=include,
                    supervisor_id=landed_supervisor_id,
                    next_since=max(
                        since,
                        0
                        if last_successful_view is None
                        else last_successful_view.last_seq,
                    ),
                    timed_out=False,
                    lease_supervision=_LeaseSupervision(
                        degraded=lease_degraded,
                        renewal_failures=lease_renewal_failures,
                        renewal_recoveries=lease_renewal_recoveries,
                    ),
                    mailbox_view=last_successful_view,
                    lost_reason=lost_reason,
                )

        if now >= deadline:
            if last_successful_view is None and last_read_error is not None:
                raise last_read_error
            next_since = max(
                since,
                0 if last_successful_view is None else last_successful_view.last_seq,
            )
            return _OutboxWaitResult(
                questions=(),
                notices=(),
                supervision_ends=(),
                include=include,
                supervisor_id=landed_supervisor_id,
                next_since=next_since,
                timed_out=True,
                lease_supervision=_LeaseSupervision(
                    degraded=lease_degraded,
                    renewal_failures=lease_renewal_failures,
                    renewal_recoveries=lease_renewal_recoveries,
                ),
                mailbox_view=last_successful_view,
            )

        if now >= next_lease_at:
            try:
                landed_lease = _write_supervisor_lease(
                    plan_dir,
                    state_dir,
                    supervisor_id,
                )
            except OSError, mailbox.MailboxError:
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
    supervision_id, supervision_id_ok = _validated_supervision_id(
        args,
        command_name=COMMAND_OUTBOX_WAIT,
        text_label="outbox wait",
    )
    if not supervision_id_ok:
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

    question_projection = _outbox_question_projection(
        context,
        args,
        wait_result,
    )
    _notify_agent_mux_mailbox_question(context, question_projection)

    # Without a correlation filter every terminal record reads as terminal,
    # which is exactly what an overlapping helper's record must not do.
    own_supervision_ends = tuple(
        supervision_end
        for supervision_end in wait_result.supervision_ends
        if supervision_id is None or supervision_end.supervision_id == supervision_id
    )
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
        questions=[_message_payload(question) for question in wait_result.questions],
        notices=[_message_payload(notice) for notice in wait_result.notices],
        supervision_ends=[
            _message_payload(supervision_end)
            for supervision_end in wait_result.supervision_ends
        ],
        supervision_ended=bool(own_supervision_ends),
        supervision_id=supervision_id,
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
        if (
            not wait_result.questions
            and not wait_result.notices
            and not wait_result.supervision_ends
        ):
            selected_label = {
                OUTBOX_INCLUDE_QUESTIONS: "questions",
                OUTBOX_INCLUDE_NOTICES: "notices",
                OUTBOX_INCLUDE_ALL: "questions, notices, or supervision ends",
            }[wait_result.include]
            print(f"No {selected_label} after seq {since}; timed out.")
        else:
            if wait_result.questions:
                print("\n".join(_outbox_question_text_lines(wait_result.questions)))
            if wait_result.notices:
                print("\n".join(_outbox_notice_text_lines(wait_result.notices)))
            if wait_result.supervision_ends:
                print(
                    "\n".join(
                        _outbox_supervision_end_text_lines(wait_result.supervision_ends)
                    )
                )
                if supervision_id is not None and not own_supervision_ends:
                    print(
                        "No supervision end record matches supervision id "
                        f"{supervision_id}; supervision continues."
                    )
            print(f"Next cursor: {wait_result.next_since}")
    return 0


def _open_questions_in(
    wait_result: _OutboxWaitResult,
) -> tuple[mailbox.MailboxMessage, ...]:
    """Return the woken questions no closure has already answered."""
    view = wait_result.mailbox_view
    if view is None:
        return wait_result.questions
    unanswered = {question.id for question in view.unanswered_questions}
    return tuple(
        question for question in wait_result.questions if question.id in unanswered
    )


def outbox_supervise_from_args(args: argparse.Namespace) -> int:
    """Long-poll one launch's supervision cursor and print a single token.

    Every wakeup that a supervisor cannot act on — another launch's terminal
    record, or a question batch that already closed — advances the persisted
    cursor and re-enters the wait on the remaining budget instead of returning.
    Absorbing them here is what keeps a healthy run silent.
    """
    supervision_id, supervision_id_ok = _validated_supervision_id(
        args,
        command_name=COMMAND_OUTBOX_SUPERVISE,
        text_label="outbox supervise",
    )
    if not supervision_id_ok:
        return 1
    if supervision_id is None:
        _print_input_error(
            args,
            command_name=COMMAND_OUTBOX_SUPERVISE,
            text_label="outbox supervise",
            reason="invalid-supervision-id",
            error="--supervision-id is required",
        )
        return 1
    timeout = _validated_timeout(
        args,
        value=args.timeout,
        option="--timeout",
        command_name=COMMAND_OUTBOX_SUPERVISE,
        text_label="outbox supervise",
    )
    if timeout is None:
        return 1
    context = _resolve(
        args,
        command_name=COMMAND_OUTBOX_SUPERVISE,
        text_label="outbox supervise",
    )
    if context is None:
        return 1

    plan_dir = context.snapshot.plan_dir
    mailbox.prune_supervision_cursors(plan_dir, state_dir=args.state_dir)
    since = mailbox.read_supervision_cursor(
        plan_dir,
        supervision_id,
        state_dir=args.state_dir,
    )
    first_seen_at = mailbox.read_supervision_first_seen_at(
        plan_dir,
        supervision_id,
        state_dir=args.state_dir,
    )
    if first_seen_at is None:
        # Anchor the grace window before the first wait rather than after it:
        # a poll that dies mid-wait would otherwise restart the window and let
        # a mis-launch stay invisible for as long as polling continues.
        first_seen_at = datetime.now(UTC)
        mailbox.write_supervision_cursor(
            plan_dir,
            supervision_id,
            since,
            state_dir=args.state_dir,
            first_seen_at=first_seen_at,
        )
    supervisor_id = str(uuid.uuid4())
    liveness_probe = _supervision_liveness_probe(
        plan_dir,
        args.state_dir,
        supervision_id,
        first_seen_at=first_seen_at,
    )
    cursor = since
    deadline = time.monotonic() + timeout
    renewal_failures = 0
    renewal_recoveries = 0
    lease_degraded = False
    while True:
        try:
            wait_result = _wait_for_questions(
                plan_dir,
                state_dir=args.state_dir,
                since=cursor,
                timeout=max(0.0, deadline - time.monotonic()),
                supervisor_id=supervisor_id,
                include=OUTBOX_INCLUDE_SUPERVISION,
                liveness_probe=liveness_probe,
            )
        except mailbox.MailboxError as error:
            _print_mailbox_error(
                args,
                command_name=COMMAND_OUTBOX_SUPERVISE,
                text_label="outbox supervise",
                error=error,
            )
            return 1
        renewal_failures += wait_result.lease_supervision.renewal_failures
        renewal_recoveries += wait_result.lease_supervision.renewal_recoveries
        lease_degraded = wait_result.lease_supervision.degraded
        cursor = wait_result.next_since
        mailbox.write_supervision_cursor(
            plan_dir,
            supervision_id,
            cursor,
            state_dir=args.state_dir,
            first_seen_at=first_seen_at,
        )
        _notify_agent_mux_mailbox_question(
            context,
            _outbox_question_projection(context, args, wait_result),
        )
        own_supervision_ends = tuple(
            supervision_end
            for supervision_end in wait_result.supervision_ends
            if supervision_end.supervision_id == supervision_id
        )
        open_questions = _open_questions_in(wait_result)
        if own_supervision_ends or open_questions:
            break
        if wait_result.lost_reason is not None:
            break
        if time.monotonic() >= deadline:
            break

    run_evidence: dict[str, Any] | None = None
    if own_supervision_ends:
        terminal = own_supervision_ends[-1]
        state_token = SUPERVISE_TOKEN_DONE
        token = (
            f"{SUPERVISE_TOKEN_DONE} outcome="
            f"{terminal.supervision_outcome} seq={terminal.seq}"
        )
        text_lines = _outbox_supervision_end_text_lines(own_supervision_ends)
    elif open_questions:
        state_token = SUPERVISE_TOKEN_ACTION
        token = f"{SUPERVISE_TOKEN_ACTION} questions={len(open_questions)}"
        text_lines = _outbox_question_text_lines(open_questions)
    elif wait_result.lost_reason is not None:
        state_token = SUPERVISE_TOKEN_LOST
        token = f"{SUPERVISE_TOKEN_LOST} reason={wait_result.lost_reason}"
        run_evidence = _supervision_run_evidence(plan_dir, args.state_dir)
        text_lines = _outbox_lost_text_lines(
            plan_dir,
            wait_result.lost_reason,
            run_evidence,
        )
    else:
        state_token = SUPERVISE_TOKEN_IDLE
        token = SUPERVISE_TOKEN_IDLE
        text_lines = []

    payload = responses.success_payload(
        COMMAND_OUTBOX_SUPERVISE,
        plan_dir=str(plan_dir),
        mailbox_path=str(_mailbox_path(context, args)),
        lease_path=str(mailbox.lease_path_for_plan(plan_dir, args.state_dir)),
        cursor_path=str(
            mailbox.supervision_cursor_path_for_plan(
                plan_dir,
                supervision_id,
                args.state_dir,
            )
        ),
        supervisor_id=wait_result.supervisor_id,
        since=since,
        next_since=cursor,
        timeout=timeout,
        timed_out=state_token == SUPERVISE_TOKEN_IDLE,
        include=OUTBOX_INCLUDE_SUPERVISION,
        state=state_token,
        questions=[_message_payload(question) for question in open_questions],
        notices=[],
        supervision_ends=[
            _message_payload(supervision_end)
            for supervision_end in own_supervision_ends
        ],
        supervision_ended=bool(own_supervision_ends),
        supervision_id=supervision_id,
        lost_reason=wait_result.lost_reason,
        run_evidence=run_evidence,
        lease_supervision=_LeaseSupervision(
            degraded=lease_degraded,
            renewal_failures=renewal_failures,
            renewal_recoveries=renewal_recoveries,
        ).to_payload(),
    )
    if args.json:
        responses.print_json(payload)
        return 0
    if renewal_failures:
        health = (
            "the latest renewal remains degraded"
            if lease_degraded
            else "a later renewal recovered"
        )
        print(
            "Warning: supervisor lease renewal failed "
            f"{renewal_failures} time(s); {health}.",
            file=sys.stderr,
        )
    print(token)
    if text_lines:
        print("\n".join(text_lines))
    return 0
