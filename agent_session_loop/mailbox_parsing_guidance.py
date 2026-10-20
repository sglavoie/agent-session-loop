"""Validate guidance-delivery records against the recorded guidance."""

from __future__ import annotations

from typing import Any

from .mailbox_constants import (
    REJECT_GUIDANCE_ALREADY_DELIVERED,
    REJECT_GUIDANCE_NOT_FOUND,
    REJECT_INCOMPLETE_TURN_IDENTITY,
    REJECT_INVALID_DELIVERY_CHANNEL,
    REJECT_INVALID_DELIVERY_REASON,
    REJECT_INVALID_DELIVERY_STATE,
    REJECT_INVALID_HARNESS,
    REJECT_INVALID_RUN_ID,
    REJECT_INVALID_SESSION_ID,
    REJECT_INVALID_TARGET,
    REJECT_INVALID_TURN_INDEX,
    REJECT_UNEXPECTED_TEXT,
)
from .mailbox_parsing_models import ParseState, RecordFields, RecordRejected
from .mailbox_validation import (
    _is_uuid,
    _valid_harness,
    _valid_run_id,
    _valid_session_id,
    _valid_turn_index,
)
from .mailbox_vocabulary import (
    GUIDANCE_DEFERRAL_REASONS,
    GUIDANCE_DELIVERED_REASONS,
    GUIDANCE_DELIVERED_STATE_CHANNEL,
    GUIDANCE_DELIVERED_STATES,
    GUIDANCE_DELIVERY_CHANNELS,
    GUIDANCE_DELIVERY_STATES,
    GUIDANCE_STATE_DEFERRED,
)


def _parse_turn_identity(payload: dict[str, Any], fields: RecordFields) -> None:
    identity_keys = ("run_id", "session_id", "turn_index")
    present = tuple(key for key in identity_keys if key in payload)
    if present not in ((), identity_keys):
        # A partial turn identity cannot key the one-terminal-attempt
        # rule, so it is refused rather than silently widened to "no
        # turn" — which would let a second terminal attempt through.
        raise RecordRejected(REJECT_INCOMPLETE_TURN_IDENTITY, ",".join(present))
    if not present:
        return
    if not _valid_run_id(payload.get("run_id")):
        raise RecordRejected(REJECT_INVALID_RUN_ID)
    if not _valid_session_id(payload.get("session_id")):
        raise RecordRejected(REJECT_INVALID_SESSION_ID)
    if not _valid_turn_index(payload.get("turn_index")):
        raise RecordRejected(REJECT_INVALID_TURN_INDEX)
    fields.run_id = payload["run_id"]
    fields.session_id = payload["session_id"]
    fields.turn_index = payload["turn_index"]


def parse_guidance_delivery(
    payload: dict[str, Any], state: ParseState, fields: RecordFields
) -> None:
    if "text" in payload:
        raise RecordRejected(REJECT_UNEXPECTED_TEXT)
    target_value = payload.get("to")
    if not _is_uuid(target_value):
        raise RecordRejected(REJECT_INVALID_TARGET)
    assert isinstance(target_value, str)
    target = target_value
    fields.target = target
    if target not in state.guidance_records:
        raise RecordRejected(REJECT_GUIDANCE_NOT_FOUND, target)
    state_value = payload.get("delivery_state")
    if state_value not in GUIDANCE_DELIVERY_STATES:
        raise RecordRejected(REJECT_INVALID_DELIVERY_STATE, str(state_value))
    channel_value = payload.get("channel")
    if channel_value not in GUIDANCE_DELIVERY_CHANNELS or (
        state_value in GUIDANCE_DELIVERED_STATES
        and channel_value != GUIDANCE_DELIVERED_STATE_CHANNEL[state_value]
    ):
        raise RecordRejected(REJECT_INVALID_DELIVERY_CHANNEL, str(channel_value))
    reason_value = payload.get("delivery_reason")
    allowed_reasons = (
        GUIDANCE_DEFERRAL_REASONS
        if state_value == GUIDANCE_STATE_DEFERRED
        else GUIDANCE_DELIVERED_REASONS
    )
    if reason_value not in allowed_reasons:
        raise RecordRejected(REJECT_INVALID_DELIVERY_REASON, str(reason_value))
    _parse_turn_identity(payload, fields)
    if "harness" in payload and not _valid_harness(payload.get("harness")):
        raise RecordRejected(REJECT_INVALID_HARNESS)
    identity = (target, fields.run_id, fields.session_id, fields.turn_index)
    if (
        state_value in GUIDANCE_DELIVERED_STATES
        and identity in state.terminal_deliveries
    ):
        raise RecordRejected(REJECT_GUIDANCE_ALREADY_DELIVERED, target)
    fields.delivery_state = state_value
    fields.delivery_reason = reason_value
    fields.channel = channel_value
    fields.harness = payload.get("harness")
