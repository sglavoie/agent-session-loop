"""Evidence projections and event records emitted by mailbox commands."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
from collections.abc import Sequence
from typing import Any

from . import (
    cli_common,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_cli_models,
    mailbox_cli_output,
    mailbox_cli_status,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
)


def record_guidance_read_deliveries(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    guidance: Sequence[mailbox_models.MailboxMessage],
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
    ownership = mailbox_cli_status.active_turn_ownership(context)
    if ownership is None:
        # Nothing durable can be said about which turn read this. The
        # acknowledgement still stands, and ``guidance_delivery_state`` still
        # reports ``delivered-by-read`` from it.
        return []
    run_id, session_id, turn_index = ownership
    harness = mailbox_cli_status.active_turn_harness(context)
    receipts: list[dict[str, Any]] = []
    for message in guidance:
        try:
            record = mailbox_appenders.append_guidance_delivery(
                context.snapshot.plan_dir,
                message.id,
                delivery_state=mailbox_vocabulary.GUIDANCE_STATE_DELIVERED_BY_READ,
                delivery_reason=mailbox_vocabulary.GUIDANCE_REASON_DELIVERED,
                channel=mailbox_vocabulary.GUIDANCE_CHANNEL_READ,
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
                harness=harness,
                state_dir=args.state_dir,
            )
        except OSError, mailbox_models.MailboxError:
            break
        receipts.append(mailbox_cli_output.message_payload(record))
    return receipts


def mailbox_reply_projection(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    question_id: str,
) -> mailbox_cli_models.MailboxReplyProjection | None:
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
        view = mailbox_leases.read_lease(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except OSError, mailbox_models.MailboxError:
        return None
    lease = view.lease
    if (
        not view.fresh
        or lease is None
        or lease.version != mailbox_constants.MAILBOX_LEASE_VERSION
        or not isinstance(lease.run_id, str)
        or not lease.run_id.strip()
        or isinstance(lease.projection_seq, bool)
        or not isinstance(lease.projection_seq, int)
        or lease.projection_seq < 0
    ):
        return None
    if lease.latest_unanswered_question_id == question_id:
        return mailbox_cli_models.MailboxReplyProjection(
            run_id=lease.run_id,
            projection_seq=lease.projection_seq,
            question_id=question_id,
        )
    try:
        snapshot = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except OSError, mailbox_models.MailboxError:
        return None
    question = next(
        (
            message
            for message in snapshot.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
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
    return mailbox_cli_models.MailboxReplyProjection(
        run_id=lease.run_id,
        projection_seq=lease.projection_seq,
        question_id=question_id,
        lease_predates_question=True,
    )


def outbox_question_projection(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    wait_result: mailbox_cli_models.OutboxWaitResult,
) -> mailbox_cli_models.MailboxQuestionProjection | None:
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
    active_run_id = mailbox_cli_status.active_status_run_id(context)

    projected_run_id: str | None = None
    try:
        lease_view = mailbox_leases.read_lease(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
    except OSError, mailbox_models.MailboxError:
        lease_view = None
    if (
        lease_view is not None
        and lease_view.fresh
        and lease_view.lease is not None
        and lease_view.lease.version == mailbox_constants.MAILBOX_LEASE_VERSION
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
    with contextlib.suppress(OSError, mailbox_models.MailboxError):
        mailbox_leases.write_lease(
            context.snapshot.plan_dir,
            wait_result.supervisor_id,
            state_dir=args.state_dir,
            **projection_kwargs,
        )

    if not actionable or not open_questions:
        return None
    latest_question = max(open_questions, key=lambda question: question.seq)
    return mailbox_cli_models.MailboxQuestionProjection(
        run_id=run_id,
        projection_seq=view.last_seq,
        open_questions=open_questions,
        latest_question=latest_question,
    )


def record_notice_published_event(
    context: cli_common.PlanCommandContext,
    message: mailbox_models.MailboxMessage,
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
