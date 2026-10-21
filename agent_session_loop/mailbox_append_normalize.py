"""Normalize one mailbox record before it is appended."""

from __future__ import annotations

from typing import Any

from .mailbox_constants import (
    MAILBOX_SCHEMA_VERSION,
    REJECT_INCOMPLETE_TURN_IDENTITY,
    REJECT_INVALID_ACKNOWLEDGEMENT,
    REJECT_INVALID_ACTION,
    REJECT_INVALID_CLOSURE_ID,
    REJECT_INVALID_CREATED_AT,
    REJECT_INVALID_DELIVERY_CHANNEL,
    REJECT_INVALID_DELIVERY_MODE,
    REJECT_INVALID_DELIVERY_REASON,
    REJECT_INVALID_DELIVERY_STATE,
    REJECT_INVALID_HARNESS,
    REJECT_INVALID_ID,
    REJECT_INVALID_NOTE,
    REJECT_INVALID_RUN_ID,
    REJECT_INVALID_SESSION_ID,
    REJECT_INVALID_SUPERVISION_ID,
    REJECT_INVALID_SUPERVISION_OUTCOME,
    REJECT_INVALID_TARGET,
    REJECT_INVALID_TURN_INDEX,
    REJECT_INVALID_WAIT_END_REASON,
    REJECT_INVALID_WAIT_ID,
    REJECT_INVALID_WAITER_ORIGIN,
    REJECT_MISSING_TEXT,
    REJECT_UNKNOWN_KIND,
)
from .mailbox_models import MailboxError
from .mailbox_validation import (
    _is_uuid,
    _new_id,
    _parse_timestamp,
    _utc_now_iso,
    _valid_harness,
    _valid_run_id,
    _valid_session_id,
    _valid_turn_index,
    _validate_sequence,
)
from .mailbox_vocabulary import (
    CLOSURE_KINDS,
    DELIVERY_MODES,
    GUIDANCE_DEFERRAL_REASONS,
    GUIDANCE_DELIVERED_REASONS,
    GUIDANCE_DELIVERED_STATE_CHANNEL,
    GUIDANCE_DELIVERED_STATES,
    GUIDANCE_DELIVERY_CHANNELS,
    GUIDANCE_DELIVERY_STATES,
    GUIDANCE_STATE_DEFERRED,
    KIND_ACK,
    KIND_GUIDANCE,
    KIND_GUIDANCE_DELIVERY,
    KIND_NOTICE,
    KIND_QUESTION,
    KIND_RECEIPT,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_SUPERVISION_END,
    KIND_WAIT_END,
    LIFECYCLE_KINDS,
    MESSAGE_KINDS,
    RESOLUTION_ACTIONS,
    SUPERVISION_OUTCOMES,
    WAIT_END_REASON_CLOSURE,
    WAIT_END_REASONS,
    WAITER_ORIGINS,
    DeliveryMode,
    GuidanceDeliveryChannel,
    GuidanceDeliveryReason,
    GuidanceDeliveryState,
    MessageKind,
    ResolutionAction,
    SupervisionOutcome,
    WaitEndReason,
    WaiterOrigin,
)


def _normalize_append_record(
    *,
    kind: MessageKind,
    text: str | None,
    target: str | None,
    upto: int | None,
    message_id: str | None,
    created_at: str | None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
    action: ResolutionAction | str | None = None,
    note: str | None = None,
    delivery_mode: DeliveryMode | str | None = None,
    wait_id: str | None = None,
    wait_end_reason: WaitEndReason | str | None = None,
    closure_id: str | None = None,
    supervision_outcome: SupervisionOutcome | str | None = None,
    supervision_id: str | None = None,
    delivery_state: GuidanceDeliveryState | str | None = None,
    delivery_reason: GuidanceDeliveryReason | str | None = None,
    channel: GuidanceDeliveryChannel | str | None = None,
    harness: str | None = None,
) -> dict[str, Any]:
    if kind not in MESSAGE_KINDS:
        raise MailboxError(REJECT_UNKNOWN_KIND, str(kind))
    resolved_id = _new_id() if message_id is None else message_id
    if not _is_uuid(resolved_id):
        raise MailboxError(REJECT_INVALID_ID)
    resolved_created_at = _utc_now_iso() if created_at is None else created_at
    if _parse_timestamp(resolved_created_at) is None:
        raise MailboxError(REJECT_INVALID_CREATED_AT)

    record: dict[str, Any] = {
        "created_at": resolved_created_at,
        "id": resolved_id,
        "kind": kind,
        "version": MAILBOX_SCHEMA_VERSION,
    }
    if kind in (
        KIND_GUIDANCE,
        KIND_QUESTION,
        KIND_REPLY,
        KIND_NOTICE,
        KIND_SUPERVISION_END,
    ):
        if not isinstance(text, str) or text.strip() == "":
            raise MailboxError(REJECT_MISSING_TEXT)
        record["text"] = text

    if kind == KIND_QUESTION:
        if delivery_mode not in DELIVERY_MODES:
            raise MailboxError(REJECT_INVALID_DELIVERY_MODE, str(delivery_mode))
        record["delivery_mode"] = delivery_mode
        if run_id is not None:
            if not _valid_run_id(run_id):
                raise MailboxError(REJECT_INVALID_RUN_ID)
            record["run_id"] = run_id
        if session_id is not None:
            if not _valid_session_id(session_id):
                raise MailboxError(REJECT_INVALID_SESSION_ID)
            record["session_id"] = session_id
        if turn_index is not None:
            if not _valid_turn_index(turn_index):
                raise MailboxError(REJECT_INVALID_TURN_INDEX)
            record["turn_index"] = turn_index
        if waiter_origin is not None:
            if waiter_origin not in WAITER_ORIGINS:
                raise MailboxError(
                    REJECT_INVALID_WAITER_ORIGIN,
                    str(waiter_origin),
                )
            record["waiter_origin"] = waiter_origin

    if kind == KIND_NOTICE:
        if not _valid_run_id(run_id):
            raise MailboxError(REJECT_INVALID_RUN_ID)
        if not _valid_session_id(session_id):
            raise MailboxError(REJECT_INVALID_SESSION_ID)
        if not _valid_turn_index(turn_index):
            raise MailboxError(REJECT_INVALID_TURN_INDEX)
        record["run_id"] = run_id
        record["session_id"] = session_id
        record["turn_index"] = turn_index

    if kind == KIND_SUPERVISION_END:
        if supervision_outcome not in SUPERVISION_OUTCOMES:
            raise MailboxError(
                REJECT_INVALID_SUPERVISION_OUTCOME,
                str(supervision_outcome),
            )
        record["outcome"] = supervision_outcome
        if run_id is not None:
            if not _valid_run_id(run_id):
                raise MailboxError(REJECT_INVALID_RUN_ID)
            record["run_id"] = run_id
        if supervision_id is not None:
            if not _is_uuid(supervision_id):
                raise MailboxError(REJECT_INVALID_SUPERVISION_ID)
            record["supervision_id"] = supervision_id

    if kind in CLOSURE_KINDS:
        if not _is_uuid(target):
            raise MailboxError(REJECT_INVALID_TARGET)
        record["to"] = target

    if kind in LIFECYCLE_KINDS:
        if not _is_uuid(target):
            raise MailboxError(REJECT_INVALID_TARGET)
        record["to"] = target

    if kind == KIND_WAIT_END:
        if not _is_uuid(wait_id):
            raise MailboxError(REJECT_INVALID_WAIT_ID)
        record["wait_id"] = wait_id

    if kind == KIND_RECEIPT and wait_id is not None:
        if not _is_uuid(wait_id):
            raise MailboxError(REJECT_INVALID_WAIT_ID)
        record["wait_id"] = wait_id

    if kind == KIND_WAIT_END:
        if wait_end_reason not in WAIT_END_REASONS:
            raise MailboxError(REJECT_INVALID_WAIT_END_REASON, str(wait_end_reason))
        record["reason"] = wait_end_reason
        if wait_end_reason == WAIT_END_REASON_CLOSURE:
            if not _is_uuid(closure_id):
                raise MailboxError(REJECT_INVALID_CLOSURE_ID)
            record["closure_id"] = closure_id
        elif closure_id is not None:
            raise MailboxError(REJECT_INVALID_CLOSURE_ID)

    if kind == KIND_RECEIPT:
        if not _is_uuid(closure_id):
            raise MailboxError(REJECT_INVALID_CLOSURE_ID)
        record["closure_id"] = closure_id

    if kind == KIND_RESOLUTION:
        if not _valid_run_id(run_id):
            raise MailboxError(REJECT_INVALID_RUN_ID)
        if action not in RESOLUTION_ACTIONS:
            raise MailboxError(REJECT_INVALID_ACTION, str(action))
        if note is not None and not isinstance(note, str):
            raise MailboxError(REJECT_INVALID_NOTE)
        record["run_id"] = run_id
        record["action"] = action
        if note is not None:
            record["note"] = note

    if kind == KIND_GUIDANCE_DELIVERY:
        if not _is_uuid(target):
            raise MailboxError(REJECT_INVALID_TARGET)
        record["to"] = target
        if delivery_state not in GUIDANCE_DELIVERY_STATES:
            raise MailboxError(REJECT_INVALID_DELIVERY_STATE, str(delivery_state))
        record["delivery_state"] = delivery_state
        if channel not in GUIDANCE_DELIVERY_CHANNELS or (
            delivery_state in GUIDANCE_DELIVERED_STATES
            and channel != GUIDANCE_DELIVERED_STATE_CHANNEL[delivery_state]
        ):
            raise MailboxError(REJECT_INVALID_DELIVERY_CHANNEL, str(channel))
        record["channel"] = channel
        allowed_reasons = (
            GUIDANCE_DEFERRAL_REASONS
            if delivery_state == GUIDANCE_STATE_DEFERRED
            else GUIDANCE_DELIVERED_REASONS
        )
        if delivery_reason not in allowed_reasons:
            raise MailboxError(REJECT_INVALID_DELIVERY_REASON, str(delivery_reason))
        record["delivery_reason"] = delivery_reason
        identity = (run_id, session_id, turn_index)
        if any(value is not None for value in identity) and not all(
            value is not None for value in identity
        ):
            raise MailboxError(REJECT_INCOMPLETE_TURN_IDENTITY)
        if run_id is not None:
            if not _valid_run_id(run_id):
                raise MailboxError(REJECT_INVALID_RUN_ID)
            if not _valid_session_id(session_id):
                raise MailboxError(REJECT_INVALID_SESSION_ID)
            if not _valid_turn_index(turn_index):
                raise MailboxError(REJECT_INVALID_TURN_INDEX)
            record["run_id"] = run_id
            record["session_id"] = session_id
            record["turn_index"] = turn_index
        if harness is not None:
            if not _valid_harness(harness):
                raise MailboxError(REJECT_INVALID_HARNESS)
            record["harness"] = harness

    if kind == KIND_ACK:
        try:
            record["upto"] = _validate_sequence(
                upto,
                field_name="upto",
            )
        except ValueError as error:
            raise MailboxError(
                REJECT_INVALID_ACKNOWLEDGEMENT,
                str(error),
            ) from error
    return record
