"""Idempotent mailbox posting and reconciliation after uncertain appends."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from . import (
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_dispatch,
)
from .overseer_mailbox_authority import (
    _check_authority,
    _check_question,
    _decision_id,
    _same_decision,
)
from .overseer_mailbox_models import (
    _ID_NAMESPACE,
    _REFUSAL_FOR_REASON,
    MAIL_OPERATIONS,
    Operation,
    Posting,
    PostingUncertain,
    Refusal,
    State,
)


def message_id(
    *,
    plan_dir: str,
    incident_id: int | None,
    operation: str,
    target: str,
) -> str:
    """Return the mailbox id one posting always has.

    Derived rather than generated, from the four fields an action and a ledger
    row both carry, so appending twice is refused by the mailbox instead of
    producing two messages and so a crashed process's record is findable by a
    process that never saw it.

    ``incident_id`` is optional because the ledger's is:
    :class:`~agent_session_loop.overseer_store_models.ActionRecord` carries a nullable
    incident, and the derivation has to accept whatever a row actually holds or
    a reconciliation could not look up the row it is reconciling. An absent
    incident derives its own stable id, which is what keeps such a row findable
    by the same rule as any other rather than unaddressable.
    """
    name = "\x00".join((plan_dir, str(incident_id), operation, target))
    return str(uuid.uuid5(_ID_NAMESPACE, name))


def inspect_posting(
    view: mailbox_snapshot.MailboxView,
    *,
    plan_dir: str,
    incident_id: int | None,
    operation: str,
    target: str,
) -> Posting | None:
    """Project one posting's state from a mailbox snapshot, or ``None``.

    ``None`` means no record with this posting's derived id is in the mailbox:
    nothing was appended, or an append did not land. That is deliberately not a
    state, because "not posted" is the absence of a posting rather than a sixth
    thing one can be in.
    """
    identity = message_id(
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation=operation,
        target=target,
    )
    record = _message(view, identity)
    if record is None:
        return None
    if operation == "reply":
        mailbox_state = view.delivery_state_for(target)
        state: State = "consumed" if mailbox_state == "consumed" else "accepted"
    else:
        mailbox_state = view.guidance_delivery_state(identity)
        state = (
            "delivered"
            if mailbox_state in mailbox_vocabulary.GUIDANCE_DELIVERED_STATES
            else "queued"
        )
    return Posting(
        operation=_operation(operation),
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=target,
        message_id=identity,
        state=state,
        mailbox_state=mailbox_state,
        seq=record.seq,
        created_at=record.created_at,
    )


def post_reply(
    *,
    plan_dir: str,
    incident_id: int,
    question_id: str,
    text: str,
    ruling: overseer_decisions.Ruling | None,
    run_id: str,
    authority: Callable[[], overseer_decisions.Ruling | None] | None = None,
    state_dir: str | Path | None = None,
) -> Posting:
    """Answer one open question, if current user authority says it applies.

    The order is authority, then read, then revalidate the question, then
    authority again, then append -- and the second authority check is the one
    that counts, because the read happened in between and a decision that was in
    force before a read is not evidence about the moment after it.

    ``authority`` re-establishes the ruling immediately before the append.
    Omitting it means the caller asserts the ruling it passed is still current;
    supplying it is how a withdrawal made during the read is caught.
    """
    identity = message_id(
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="reply",
        target=question_id,
    )
    refused = _refused_posting(
        operation="reply",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=question_id,
        identity=identity,
    )
    granted = _check_authority(
        ruling,
        operation="reply",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except (mailbox_models.MailboxError, OSError) as error:
        return refused("mailbox_unreadable", str(error))
    landed = inspect_posting(
        view,
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="reply",
        target=question_id,
    )
    if landed is not None:
        return landed
    stale = _check_question(view, question_id=question_id, run_id=run_id)
    if stale is not None:
        return refused(*stale)
    current = ruling if authority is None else authority()
    granted = _check_authority(
        current,
        operation="reply",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    if not _same_decision(ruling, current):
        return refused(
            "authority_superseded",
            f"{_decision_id(ruling)} was replaced by {_decision_id(current)}",
        )
    try:
        message = mailbox_appenders.append_reply(
            plan_dir,
            question_id,
            text,
            state_dir=state_dir,
            message_id=identity,
        )
    except mailbox_models.MailboxError as error:
        return _after_failed_append(
            error,
            plan_dir=plan_dir,
            incident_id=incident_id,
            operation="reply",
            target=question_id,
            identity=identity,
            state_dir=state_dir,
            refused=refused,
        )
    except OSError as error:
        raise PostingUncertain(str(error), message_id=identity) from error
    return Posting(
        operation="reply",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=question_id,
        message_id=identity,
        state="accepted",
        mailbox_state="accepted",
        seq=message.seq,
        created_at=message.created_at,
    )


def post_guidance(
    *,
    plan_dir: str,
    incident_id: int,
    text: str,
    ruling: overseer_decisions.Ruling | None,
    run_id: str,
    authority: Callable[[], overseer_decisions.Ruling | None] | None = None,
    state_dir: str | Path | None = None,
) -> Posting:
    """Queue one notice for a run, if current user authority says it applies.

    A guidance record that lands is ``queued``, never ``delivered``: this module
    appends the guidance and nothing else, and the delivery ledger the harness
    seam writes is what later moves it. Reporting an append as delivery would be
    the one lie the state vocabulary exists to prevent.
    """
    target = overseer_dispatch.Guidance(text=text).target()
    identity = message_id(
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="guidance",
        target=target,
    )
    refused = _refused_posting(
        operation="guidance",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=target,
        identity=identity,
    )
    granted = _check_authority(
        ruling,
        operation="guidance",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except (mailbox_models.MailboxError, OSError) as error:
        return refused("mailbox_unreadable", str(error))
    landed = inspect_posting(
        view,
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="guidance",
        target=target,
    )
    if landed is not None:
        return landed
    current = ruling if authority is None else authority()
    granted = _check_authority(
        current,
        operation="guidance",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    if not _same_decision(ruling, current):
        return refused(
            "authority_superseded",
            f"{_decision_id(ruling)} was replaced by {_decision_id(current)}",
        )
    try:
        message = mailbox_appenders.append_guidance(
            plan_dir,
            text,
            state_dir=state_dir,
            message_id=identity,
        )
    except mailbox_models.MailboxError as error:
        return _after_failed_append(
            error,
            plan_dir=plan_dir,
            incident_id=incident_id,
            operation="guidance",
            target=target,
            identity=identity,
            state_dir=state_dir,
            refused=refused,
        )
    except OSError as error:
        raise PostingUncertain(str(error), message_id=identity) from error
    return Posting(
        operation="guidance",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=target,
        message_id=identity,
        state="queued",
        mailbox_state=mailbox_vocabulary.GUIDANCE_STATE_QUEUED,
        seq=message.seq,
        created_at=message.created_at,
    )


# -- internals -------------------------------------------------------------


def _operation(value: str) -> Operation:
    """Narrow a stored operation string to the literal type."""
    for known in MAIL_OPERATIONS:
        if known == value:
            return known
    raise ValueError(f"{value!r} is not a mailbox operation")


def _message(
    view: mailbox_snapshot.MailboxView,
    identity: str,
) -> mailbox_models.MailboxMessage | None:
    for message in view.messages:
        if message.id == identity:
            return message
    return None


def _refused_posting(
    *,
    operation: str,
    plan_dir: str,
    incident_id: int,
    target: str,
    identity: str,
) -> Callable[[Refusal, str], Posting]:
    """Return a builder for this posting's refusals, so none can omit a reason."""

    def build(refusal: Refusal, detail: str = "") -> Posting:
        return Posting(
            operation=_operation(operation),
            plan_dir=plan_dir,
            incident_id=incident_id,
            target=target,
            message_id=identity,
            state="rejected",
            refusal=refusal,
            detail=detail,
        )

    return build


def _after_failed_append(
    error: mailbox_models.MailboxError,
    *,
    plan_dir: str,
    incident_id: int,
    operation: str,
    target: str,
    identity: str,
    state_dir: str | Path | None,
    refused: Callable[[Refusal, str], Posting],
) -> Posting:
    """Decide what a refused append means, re-reading before concluding anything.

    ``duplicate-id`` is the retry case and is proof our own record is already
    there; a write error is the case nobody can conclude from, so both re-read
    and only a re-read that also fails is uncertain.
    """
    if error.reason in (
        mailbox_constants.REJECT_DUPLICATE_ID,
        mailbox_constants.ERROR_MAILBOX_WRITE,
    ):
        try:
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
        except (mailbox_models.MailboxError, OSError) as failure:
            raise PostingUncertain(
                f"{error}; the mailbox could not be re-read: {failure}",
                message_id=identity,
            ) from failure
        landed = inspect_posting(
            view,
            plan_dir=plan_dir,
            incident_id=incident_id,
            operation=operation,
            target=target,
        )
        if landed is not None:
            return landed
        if error.reason == mailbox_constants.REJECT_DUPLICATE_ID:
            return refused("record_rejected", str(error))
        raise PostingUncertain(str(error), message_id=identity)
    return refused(
        _REFUSAL_FOR_REASON.get(error.reason, "record_rejected"),
        str(error),
    )
