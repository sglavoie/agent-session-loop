"""Public appenders for guidance, questions, replies, receipts, and notices."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .mailbox import read_mailbox
from .mailbox_append_record import _append_record
from .mailbox_models import MailboxMessage
from .mailbox_vocabulary import (
    DELIVERY_MODE_ASYNC,
    KIND_ACK,
    KIND_GUIDANCE,
    KIND_GUIDANCE_DELIVERY,
    KIND_NOTICE,
    KIND_QUESTION,
    KIND_RECEIPT,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_SUPERVISION_END,
    DeliveryMode,
    GuidanceDeliveryChannel,
    GuidanceDeliveryReason,
    GuidanceDeliveryState,
    ResolutionAction,
    SupervisionOutcome,
    WaiterOrigin,
)


def append_guidance(
    plan_dir: str | Path,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    return _append_record(
        plan_dir,
        kind=KIND_GUIDANCE,
        state_dir=state_dir,
        text=text,
        message_id=message_id,
        created_at=created_at,
    )


def append_guidance_delivery(
    plan_dir: str | Path,
    guidance_id: str,
    *,
    delivery_state: GuidanceDeliveryState | str,
    delivery_reason: GuidanceDeliveryReason | str,
    channel: GuidanceDeliveryChannel | str,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    harness: str | None = None,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Record what one delivery attempt did with an existing guidance record.

    The record names the guidance, never quotes it. ``run_id``/``session_id``/
    ``turn_index`` are supplied together or not at all: together they are the
    turn identity that the one-terminal-attempt-per-turn rule keys on, so a
    partial identity is refused rather than collapsed into "no turn".
    """
    return _append_record(
        plan_dir,
        kind=KIND_GUIDANCE_DELIVERY,
        state_dir=state_dir,
        target=guidance_id,
        delivery_state=delivery_state,
        delivery_reason=delivery_reason,
        channel=channel,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        message_id=message_id,
        created_at=created_at,
    )


def guidance_delivery_event_payload(message: MailboxMessage) -> dict[str, Any]:
    """Return the body-free event payload for one delivery attempt.

    Every field here is metadata the supervisor already knows or can look up;
    nothing derived from the guidance body — not its text, not its length, not
    a digest — appears, because a delivery event is evidence about transport,
    not about content.
    """
    if message.kind != KIND_GUIDANCE_DELIVERY:
        raise ValueError("message must be a guidance-delivery record")
    return {
        "guidance_id": message.to,
        "delivery_id": message.id,
        "seq": message.seq,
        "delivery_state": message.delivery_state,
        "delivery_reason": message.delivery_reason,
        "channel": message.channel,
        "harness": message.harness,
        "run_id": message.run_id,
        "session_id": message.session_id,
        "turn_index": message.turn_index,
        "created_at": message.created_at,
    }


def guidance_deliveries(
    plan_dir: str | Path,
    guidance_id: str,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    return read_mailbox(plan_dir, state_dir=state_dir).guidance_deliveries(guidance_id)


def deliverable_guidance(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    return read_mailbox(plan_dir, state_dir=state_dir).deliverable_guidance


def append_question(
    plan_dir: str | Path,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
    delivery_mode: DeliveryMode | str = DELIVERY_MODE_ASYNC,
) -> MailboxMessage:
    return _append_record(
        plan_dir,
        kind=KIND_QUESTION,
        state_dir=state_dir,
        text=text,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        waiter_origin=waiter_origin,
        delivery_mode=delivery_mode,
    )


def append_reply(
    plan_dir: str | Path,
    question_id: str,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Atomically append the first reply to a valid question."""
    return _append_record(
        plan_dir,
        kind=KIND_REPLY,
        state_dir=state_dir,
        text=text,
        target=question_id,
        message_id=message_id,
        created_at=created_at,
    )


def append_resolution(
    plan_dir: str | Path,
    question_id: str,
    action: ResolutionAction | str,
    *,
    expected_run_id: str,
    note: str | None = None,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Atomically append the first answerless closure for an owned question."""
    return _append_record(
        plan_dir,
        kind=KIND_RESOLUTION,
        state_dir=state_dir,
        target=question_id,
        run_id=expected_run_id,
        action=action,
        note=note,
        message_id=message_id,
        created_at=created_at,
    )


def append_receipt(
    plan_dir: str | Path,
    question_id: str,
    closure_id: str,
    *,
    wait_id: str | None = None,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Record that an ask/runner consumer actually observed one closure."""
    return _append_record(
        plan_dir,
        kind=KIND_RECEIPT,
        state_dir=state_dir,
        target=question_id,
        wait_id=wait_id,
        closure_id=closure_id,
        message_id=message_id,
        created_at=created_at,
    )


def append_notice(
    plan_dir: str | Path,
    text: str,
    *,
    run_id: str,
    session_id: str,
    turn_index: int,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Append a nonblocking worker notice with exact turn ownership."""
    return _append_record(
        plan_dir,
        kind=KIND_NOTICE,
        state_dir=state_dir,
        text=text,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        message_id=message_id,
        created_at=created_at,
    )


def append_supervision_end(
    plan_dir: str | Path,
    text: str,
    *,
    outcome: SupervisionOutcome | str,
    run_id: str | None = None,
    supervision_id: str | None = None,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
    resolve_at_commit: Callable[[], tuple[SupervisionOutcome | str, str]] | None = None,
) -> MailboxMessage:
    """Append the terminal record marking the end of supervised work.

    ``supervision_id`` identifies the supervising launch that is ending, so a
    reader can tell this helper's exit from a record left by an overlapping
    helper on the same plan; physical sequence alone cannot.

    ``resolve_at_commit`` returns the ``(outcome, text)`` to persist. It is
    called immediately before the bytes are written and again once the line
    has cleared its durability barrier, and a line whose resolution moved
    across either is rolled back and rewritten — and resynced — under the
    lock. The emitting helper can be killed while this append is in flight —
    waiting on the lock, reading the log, inside the write or the sync itself
    — and only a decision taken that late reports the termination instead of
    the outcome the caller started with. It must be side-effect free and
    settle after at most one change; only its first change is honoured.
    """

    commit_fields: Callable[[], dict[str, Any]] | None = None
    if resolve_at_commit is not None:
        resolve = resolve_at_commit

        def commit_fields() -> dict[str, Any]:
            resolved_outcome, resolved_text = resolve()
            return {
                "supervision_outcome": resolved_outcome,
                "text": resolved_text,
            }

    return _append_record(
        plan_dir,
        kind=KIND_SUPERVISION_END,
        state_dir=state_dir,
        text=text,
        supervision_outcome=outcome,
        supervision_id=supervision_id,
        run_id=run_id,
        message_id=message_id,
        created_at=created_at,
        commit_fields=commit_fields,
    )


def append_acknowledgement(
    plan_dir: str | Path,
    upto: int,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Acknowledge worker-visible records through physical sequence ``upto``."""
    return _append_record(
        plan_dir,
        kind=KIND_ACK,
        state_dir=state_dir,
        upto=upto,
        message_id=message_id,
        created_at=created_at,
    )


# Short alias for call sites that already use the on-disk kind name.
append_ack = append_acknowledgement
