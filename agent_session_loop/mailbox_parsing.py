"""Parse raw mailbox bytes into a validated view, rejecting malformed lines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mailbox_constants import (
    MAILBOX_LEGACY_SCHEMA_VERSION,
    MAILBOX_PREVIOUS_SCHEMA_VERSION,
    MAILBOX_SUPPORTED_SCHEMA_VERSIONS,
    MAILBOX_WAIT_SCHEMA_VERSION,
    REJECT_DUPLICATE_ID,
    REJECT_EMPTY_LINE,
    REJECT_INVALID_CREATED_AT,
    REJECT_INVALID_ID,
    REJECT_INVALID_JSON,
    REJECT_NOT_OBJECT,
    REJECT_UNKNOWN_KIND,
    REJECT_UNSUPPORTED_VERSION,
)
from .mailbox_models import MailboxMessage, RejectedMailboxRecord
from .mailbox_parsing_guidance import parse_guidance_delivery
from .mailbox_parsing_models import ParseState, RecordFields, RecordRejected
from .mailbox_parsing_payload import (
    parse_ack,
    parse_notice,
    parse_question,
    parse_supervision_end,
    parse_text,
)
from .mailbox_parsing_relations import (
    check_wait_start,
    parse_closure,
    parse_lifecycle_target,
    parse_receipt_closure,
    parse_receipt_wait,
    parse_resolution,
    parse_wait_end_id,
    parse_wait_end_reason,
)
from .mailbox_snapshot import MailboxView
from .mailbox_validation import (
    _is_uuid,
    _parse_timestamp,
    _physical_lines,
    _reject,
)
from .mailbox_vocabulary import (
    CLOSURE_KINDS,
    GUIDANCE_DELIVERED_STATES,
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
    KIND_WAIT_START,
    LEGACY_MESSAGE_KINDS,
    LIFECYCLE_KINDS,
    MESSAGE_KINDS,
    V2_MESSAGE_KINDS,
    V3_MESSAGE_KINDS,
)


def _parse_envelope(
    raw: str, seen_ids: set[str]
) -> tuple[dict[str, Any], int, Any, str, str]:
    """Return (payload, version, kind, message_id, created_at) or raise."""
    if raw.strip() == "":
        raise RecordRejected(REJECT_EMPTY_LINE)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        # One deeply nested line must cost only itself, not every reader.
        raise RecordRejected(REJECT_INVALID_JSON, str(error)) from error
    if not isinstance(payload, dict):
        raise RecordRejected(REJECT_NOT_OBJECT)

    # Versionless records predate the v2 writer and therefore retain the
    # legacy v1 interpretation even after the current write version moves.
    version = payload.get("version", MAILBOX_LEGACY_SCHEMA_VERSION)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in MAILBOX_SUPPORTED_SCHEMA_VERSIONS
    ):
        raise RecordRejected(REJECT_UNSUPPORTED_VERSION, str(version))

    kind = payload.get("kind")
    allowed_kinds = (
        LEGACY_MESSAGE_KINDS
        if version == MAILBOX_LEGACY_SCHEMA_VERSION
        else V2_MESSAGE_KINDS
        if version == MAILBOX_PREVIOUS_SCHEMA_VERSION
        else V3_MESSAGE_KINDS
        if version == MAILBOX_WAIT_SCHEMA_VERSION
        else MESSAGE_KINDS
    )
    if kind not in allowed_kinds:
        raise RecordRejected(REJECT_UNKNOWN_KIND, str(kind))

    message_id = payload.get("id")
    if not _is_uuid(message_id):
        raise RecordRejected(REJECT_INVALID_ID)
    assert isinstance(message_id, str)
    if message_id in seen_ids:
        raise RecordRejected(REJECT_DUPLICATE_ID, message_id)

    created_at = payload.get("created_at")
    if _parse_timestamp(created_at) is None:
        raise RecordRejected(REJECT_INVALID_CREATED_AT)
    assert isinstance(created_at, str)
    return payload, version, kind, message_id, created_at


def _parse_kind_fields(
    payload: dict[str, Any],
    kind: str,
    version: int,
    seq: int,
    state: ParseState,
) -> RecordFields:
    fields = RecordFields()
    if kind in (
        KIND_GUIDANCE,
        KIND_QUESTION,
        KIND_REPLY,
        KIND_NOTICE,
        KIND_SUPERVISION_END,
    ):
        parse_text(payload, fields)
    if kind == KIND_QUESTION:
        parse_question(payload, version, fields)
    if kind == KIND_NOTICE:
        parse_notice(payload, fields)
    if kind == KIND_SUPERVISION_END:
        parse_supervision_end(payload, fields)
    if kind == KIND_GUIDANCE_DELIVERY:
        parse_guidance_delivery(payload, state, fields)
    if kind in CLOSURE_KINDS:
        parse_closure(payload, state, fields)
    if kind == KIND_RESOLUTION:
        parse_resolution(payload, state, fields)
    if kind == KIND_ACK:
        parse_ack(payload, seq, fields)
    if kind in LIFECYCLE_KINDS:
        parse_lifecycle_target(payload, state, fields)
    target = fields.target
    if kind == KIND_WAIT_START:
        assert target is not None
        check_wait_start(state, target)
    if kind == KIND_WAIT_END:
        assert target is not None
        parse_wait_end_id(payload, state, fields, target)
    if kind == KIND_RECEIPT:
        assert target is not None
        parse_receipt_wait(payload, state, fields, target)
    if kind == KIND_WAIT_END:
        assert target is not None
        parse_wait_end_reason(payload, state, fields, target)
    if kind == KIND_RECEIPT:
        assert target is not None
        parse_receipt_closure(payload, state, fields, target)
    return fields


def _record_message(state: ParseState, message: MailboxMessage) -> None:
    kind = message.kind
    target = message.to
    state.seen_ids.add(message.id)
    if kind == KIND_GUIDANCE:
        state.guidance_records[message.id] = message
    elif kind == KIND_GUIDANCE_DELIVERY and target is not None:
        if message.delivery_state in GUIDANCE_DELIVERED_STATES:
            state.terminal_deliveries.add(
                (target, message.run_id, message.session_id, message.turn_index)
            )
    elif kind == KIND_QUESTION:
        state.questions[message.id] = message
    elif kind in CLOSURE_KINDS and target is not None:
        state.closures[target] = message
    elif kind == KIND_WAIT_START and target is not None:
        state.wait_starts[message.id] = message
        state.active_waits[target] = message
    elif kind == KIND_RECEIPT and message.closure_id is not None:
        state.receipts[message.closure_id] = message
    elif kind == KIND_WAIT_END and target is not None:
        state.active_waits.pop(target, None)


def _parse_mailbox_bytes(data: bytes, mailbox_path: Path) -> MailboxView:
    messages: list[MailboxMessage] = []
    rejected: list[RejectedMailboxRecord] = []
    state = ParseState()
    physical_lines = _physical_lines(data)

    for seq, raw_bytes in enumerate(physical_lines, start=1):
        raw = raw_bytes.decode("utf-8", errors="replace")
        try:
            payload, version, kind, message_id, created_at = _parse_envelope(
                raw, state.seen_ids
            )
            fields = _parse_kind_fields(payload, kind, version, seq, state)
        except RecordRejected as error:
            _reject(
                rejected,
                seq=seq,
                reason=error.reason,
                raw=raw,
                detail=error.detail,
            )
            continue

        message = MailboxMessage(
            seq=seq,
            id=message_id,
            kind=kind,
            created_at=created_at,
            text=fields.text,
            to=fields.target,
            upto=fields.upto,
            run_id=fields.run_id,
            session_id=fields.session_id,
            turn_index=fields.turn_index,
            waiter_origin=fields.waiter_origin,
            action=fields.action,
            note=fields.note,
            delivery_mode=fields.delivery_mode,
            wait_id=fields.wait_id,
            wait_end_reason=fields.wait_end_reason,
            closure_id=fields.closure_id,
            supervision_outcome=fields.supervision_outcome,
            supervision_id=fields.supervision_id,
            delivery_state=fields.delivery_state,
            delivery_reason=fields.delivery_reason,
            channel=fields.channel,
            harness=fields.harness,
            version=version,
        )
        messages.append(message)
        _record_message(state, message)

    return MailboxView(
        mailbox_path=mailbox_path,
        messages=tuple(messages),
        rejected=tuple(rejected),
        last_seq=len(physical_lines),
        size_bytes=len(data),
    )
