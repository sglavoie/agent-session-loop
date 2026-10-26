"""Mailbox question identity, text, and creation for a reply-capable pause."""

from __future__ import annotations

import uuid
from pathlib import Path

from . import (
    harnesses,
    mailbox,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    mailbox_waits,
    pause_policy,
    qna,
    run_coordination_constants,
    run_coordination_wait_models,
)


def mailbox_question_id(
    *,
    plan_dir: Path,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> str:
    """Return the retry-stable UUID5 identity for one completed turn."""
    identity = (
        f"asl-mailbox-question:{plan_dir.resolve()}:{run_id}:{session_id}:{turn_index}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def runner_pause_can_wait_for_mailbox_reply(
    *,
    mailbox_enabled: bool,
    pause_reason: str,
    result: harnesses.HarnessResult,
) -> bool:
    """Return whether an operator answer can usefully clear this pause."""
    if not mailbox_enabled or result.returncode != 0:
        return False
    policy = pause_policy.classify_pause(pause_reason)
    return bool(policy["user_action"] == pause_policy.USER_ACTION_REPLY)


def worker_pause_resolution_for_turn(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> mailbox_models.MailboxMessage | None:
    """Return this worker turn's committed pause resolution, if any.

    This lookup is deliberately fail-open: a mailbox read failure or any
    incomplete/stale ownership tuple leaves the ordinary runner-owned question
    path intact. Only a first-close-wins resolution targeting a question owned
    by this exact worker turn can suppress that second question.
    """
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except OSError, mailbox_models.MailboxError:
        return None

    worker_question_ids = {
        message.id
        for message in view.messages
        if message.kind == mailbox_vocabulary.KIND_QUESTION
        and message.run_id == run_id
        and message.session_id == session_id
        and message.turn_index == turn_index
        and message.waiter_origin == mailbox_vocabulary.WAITER_ORIGIN_WORKER
    }
    for message in view.messages:
        if (
            message.kind == mailbox_vocabulary.KIND_RESOLUTION
            and message.action == mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
            and message.run_id == run_id
            and message.to in worker_question_ids
        ):
            return message
    return None


def mailbox_question_text(context: qna.QnAContext) -> str:
    """Return the question body, bounded and byte-stable across retries."""
    text = context.last_message
    if len(text) <= run_coordination_constants.MAILBOX_QUESTION_TEXT_MAX_CHARS:
        return text
    return (
        text[: run_coordination_constants.MAILBOX_QUESTION_TEXT_MAX_CHARS]
        + "\n\n[question truncated to the first "
        + str(run_coordination_constants.MAILBOX_QUESTION_TEXT_MAX_CHARS)
        + f" of {len(text)} characters; full text: "
        + str(context.last_message_path)
        + "]"
    )


def ensure_mailbox_question(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    question_id: str,
    text: str,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> mailbox_models.MailboxMessage:
    """Append one deterministic question or reuse the identical existing one."""

    def existing_question(
        view: mailbox_snapshot.MailboxView,
    ) -> mailbox_models.MailboxMessage | None:
        existing = next(
            (message for message in view.messages if message.id == question_id),
            None,
        )
        if existing is None:
            return None
        closed = view.closure_for(existing.id)
        if (
            existing.kind != mailbox_vocabulary.KIND_QUESTION
            or existing.text != text
            or existing.run_id != run_id
            or existing.session_id != session_id
            or existing.turn_index != turn_index
            or existing.waiter_origin != mailbox_vocabulary.WAITER_ORIGIN_RUNNER
            or (
                existing.delivery_mode != mailbox_vocabulary.DELIVERY_MODE_WAIT
                and closed is None
            )
        ):
            raise run_coordination_wait_models.MailboxWaitUnavailable(
                qna.REASON_MISSING_QUESTION_CONTEXT
            )
        return existing

    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_MAILBOX_READ
        )
        raise run_coordination_wait_models.MailboxWaitUnavailable(reason) from error
    existing = existing_question(view)
    if existing is not None:
        if (
            existing.delivery_mode == mailbox_vocabulary.DELIVERY_MODE_WAIT
            and view.active_wait_for(existing.id) is None
            and view.closure_for(existing.id) is None
        ):
            # A refused wait start is a wait that cannot begin, which the
            # caller already turns into an ordinary pause; raising past it
            # would crash the run over a full or unwritable mailbox.
            try:
                mailbox_waits.append_wait_start(
                    plan_dir, existing.id, state_dir=state_dir_arg
                )
            except (OSError, mailbox_models.MailboxError) as error:
                reason = (
                    error.reason
                    if isinstance(error, mailbox_models.MailboxError)
                    else mailbox_constants.ERROR_MAILBOX_WRITE
                )
                raise run_coordination_wait_models.MailboxWaitUnavailable(
                    reason
                ) from error
        return existing
    # A question append is refused at the reduced question cap, not the file
    # cap; the wait loop's own guard is ``view.accepts_wait_closures`` because
    # there the question is whether the *reply* can still land.
    if not view.accepts_questions:
        raise run_coordination_wait_models.MailboxWaitUnavailable(
            mailbox_constants.ERROR_MAILBOX_FULL
        )

    try:
        question, _wait_start = mailbox_waits.append_wait_question(
            plan_dir,
            text,
            state_dir=state_dir_arg,
            message_id=question_id,
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
        )
        return question
    except mailbox_models.MailboxError as error:
        if error.reason != mailbox_constants.REJECT_DUPLICATE_ID:
            raise run_coordination_wait_models.MailboxWaitUnavailable(
                error.reason
            ) from error

    # Another process can win the deterministic append race. Re-read and
    # accept only the exact question this turn intended to publish.
    try:
        raced_view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_MAILBOX_READ
        )
        raise run_coordination_wait_models.MailboxWaitUnavailable(reason) from error
    raced = existing_question(raced_view)
    if raced is None:
        raise run_coordination_wait_models.MailboxWaitUnavailable(
            qna.REASON_MISSING_QUESTION_CONTEXT
        )
    return raced
