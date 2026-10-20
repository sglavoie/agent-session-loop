"""Payload-only validators for mailbox records (no cross-record state)."""

from __future__ import annotations

from typing import Any

from .mailbox_constants import (
    MAILBOX_LEGACY_SCHEMA_VERSION,
    MAILBOX_WAIT_SCHEMA_VERSION,
    REJECT_INVALID_ACKNOWLEDGEMENT,
    REJECT_INVALID_DELIVERY_MODE,
    REJECT_INVALID_RUN_ID,
    REJECT_INVALID_SESSION_ID,
    REJECT_INVALID_SUPERVISION_ID,
    REJECT_INVALID_SUPERVISION_OUTCOME,
    REJECT_INVALID_TURN_INDEX,
    REJECT_INVALID_WAITER_ORIGIN,
    REJECT_MISSING_TEXT,
)
from .mailbox_parsing_models import RecordFields, RecordRejected
from .mailbox_validation import (
    _is_uuid,
    _valid_run_id,
    _valid_session_id,
    _valid_turn_index,
)
from .mailbox_vocabulary import (
    DELIVERY_MODES,
    SUPERVISION_OUTCOMES,
    WAITER_ORIGINS,
)


def parse_text(payload: dict[str, Any], fields: RecordFields) -> None:
    text_value = payload.get("text")
    if not isinstance(text_value, str) or text_value.strip() == "":
        raise RecordRejected(REJECT_MISSING_TEXT)
    fields.text = text_value


def parse_question(payload: dict[str, Any], version: int, fields: RecordFields) -> None:
    if version == MAILBOX_LEGACY_SCHEMA_VERSION:
        # v1 accepted ownership before it was part of the schema and
        # deliberately degraded malformed owners to unattributed.
        if _valid_run_id(payload.get("run_id")):
            fields.run_id = payload["run_id"]
        return
    if "run_id" in payload:
        if not _valid_run_id(payload.get("run_id")):
            raise RecordRejected(REJECT_INVALID_RUN_ID)
        fields.run_id = payload["run_id"]
    if "session_id" in payload:
        if not _valid_session_id(payload.get("session_id")):
            raise RecordRejected(REJECT_INVALID_SESSION_ID)
        fields.session_id = payload["session_id"]
    if "turn_index" in payload:
        if not _valid_turn_index(payload.get("turn_index")):
            raise RecordRejected(REJECT_INVALID_TURN_INDEX)
        fields.turn_index = payload["turn_index"]
    if "waiter_origin" in payload:
        waiter_value = payload.get("waiter_origin")
        if waiter_value not in WAITER_ORIGINS:
            raise RecordRejected(REJECT_INVALID_WAITER_ORIGIN, str(waiter_value))
        fields.waiter_origin = waiter_value
    if version >= MAILBOX_WAIT_SCHEMA_VERSION:
        delivery_value = payload.get("delivery_mode")
        if delivery_value not in DELIVERY_MODES:
            raise RecordRejected(REJECT_INVALID_DELIVERY_MODE, str(delivery_value))
        fields.delivery_mode = delivery_value


def parse_notice(payload: dict[str, Any], fields: RecordFields) -> None:
    run_value = payload.get("run_id")
    session_value = payload.get("session_id")
    turn_value = payload.get("turn_index")
    if not _valid_run_id(run_value):
        raise RecordRejected(REJECT_INVALID_RUN_ID)
    if not _valid_session_id(session_value):
        raise RecordRejected(REJECT_INVALID_SESSION_ID)
    if not _valid_turn_index(turn_value):
        raise RecordRejected(REJECT_INVALID_TURN_INDEX)
    fields.run_id = run_value
    fields.session_id = session_value
    fields.turn_index = turn_value


def parse_supervision_end(payload: dict[str, Any], fields: RecordFields) -> None:
    outcome_value = payload.get("outcome")
    if outcome_value not in SUPERVISION_OUTCOMES:
        raise RecordRejected(REJECT_INVALID_SUPERVISION_OUTCOME, str(outcome_value))
    if "run_id" in payload:
        if not _valid_run_id(payload.get("run_id")):
            raise RecordRejected(REJECT_INVALID_RUN_ID)
        fields.run_id = payload["run_id"]
    if "supervision_id" in payload:
        if not _is_uuid(payload.get("supervision_id")):
            raise RecordRejected(REJECT_INVALID_SUPERVISION_ID)
        fields.supervision_id = payload["supervision_id"]
    fields.supervision_outcome = outcome_value


def parse_ack(payload: dict[str, Any], seq: int, fields: RecordFields) -> None:
    upto_value = payload.get("upto")
    if (
        isinstance(upto_value, bool)
        or not isinstance(upto_value, int)
        or upto_value < 0
        or upto_value >= seq
    ):
        raise RecordRejected(REJECT_INVALID_ACKNOWLEDGEMENT, str(upto_value))
    fields.upto = upto_value
