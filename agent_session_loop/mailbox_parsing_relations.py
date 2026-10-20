"""Validate question closures, resolutions, waits and receipts against prior records."""

from __future__ import annotations

from typing import Any

from .mailbox_constants import (
    MAILBOX_WAIT_SCHEMA_VERSION,
    REJECT_CLOSURE_ALREADY_CONSUMED,
    REJECT_INVALID_ACTION,
    REJECT_INVALID_CLOSURE_ID,
    REJECT_INVALID_NOTE,
    REJECT_INVALID_RUN_ID,
    REJECT_INVALID_TARGET,
    REJECT_INVALID_WAIT_END_REASON,
    REJECT_INVALID_WAIT_ID,
    REJECT_QUESTION_ALREADY_ANSWERED,
    REJECT_QUESTION_ALREADY_RESOLVED,
    REJECT_QUESTION_ALREADY_WAITING,
    REJECT_QUESTION_NOT_FOUND,
    REJECT_QUESTION_NOT_OPEN,
    REJECT_QUESTION_NOT_WAITING,
)
from .mailbox_parsing_models import ParseState, RecordFields, RecordRejected
from .mailbox_validation import _is_uuid, _valid_run_id
from .mailbox_vocabulary import (
    DELIVERY_MODE_WAIT,
    KIND_REPLY,
    RESOLUTION_ACTIONS,
    WAIT_END_REASON_CLOSURE,
    WAIT_END_REASONS,
)


def _parse_target(payload: dict[str, Any], fields: RecordFields) -> str:
    target_value = payload.get("to")
    if not _is_uuid(target_value):
        raise RecordRejected(REJECT_INVALID_TARGET)
    assert isinstance(target_value, str)
    fields.target = target_value
    return target_value


def parse_closure(
    payload: dict[str, Any], state: ParseState, fields: RecordFields
) -> None:
    target = _parse_target(payload, fields)
    if target not in state.questions:
        raise RecordRejected(REJECT_QUESTION_NOT_FOUND, target)
    prior_closure = state.closures.get(target)
    if prior_closure is not None:
        raise RecordRejected(
            REJECT_QUESTION_ALREADY_ANSWERED
            if prior_closure.kind == KIND_REPLY
            else REJECT_QUESTION_ALREADY_RESOLVED,
            target,
        )
    question = state.questions[target]
    if (
        question.version >= MAILBOX_WAIT_SCHEMA_VERSION
        and question.delivery_mode == DELIVERY_MODE_WAIT
        and target not in state.active_waits
    ):
        raise RecordRejected(REJECT_QUESTION_NOT_WAITING, target)


def parse_resolution(
    payload: dict[str, Any], state: ParseState, fields: RecordFields
) -> None:
    run_value = payload.get("run_id")
    if not _valid_run_id(run_value):
        raise RecordRejected(REJECT_INVALID_RUN_ID)
    target = fields.target
    assert target is not None
    if state.questions[target].run_id != run_value:
        raise RecordRejected(REJECT_QUESTION_NOT_OPEN, target)
    action_value = payload.get("action")
    if action_value not in RESOLUTION_ACTIONS:
        raise RecordRejected(REJECT_INVALID_ACTION, str(action_value))
    if "note" in payload and not isinstance(payload.get("note"), str):
        raise RecordRejected(REJECT_INVALID_NOTE)
    fields.run_id = run_value
    fields.action = action_value
    fields.note = payload.get("note")


def parse_lifecycle_target(
    payload: dict[str, Any], state: ParseState, fields: RecordFields
) -> None:
    target = _parse_target(payload, fields)
    if state.questions.get(target) is None:
        raise RecordRejected(REJECT_QUESTION_NOT_FOUND, target)


def check_wait_start(state: ParseState, target: str) -> None:
    if target in state.closures:
        raise RecordRejected(REJECT_QUESTION_NOT_OPEN, target)
    if target in state.active_waits:
        raise RecordRejected(REJECT_QUESTION_ALREADY_WAITING, target)


def _matches_active_wait(state: ParseState, target: str, wait_value: Any) -> bool:
    active_wait = state.active_waits.get(target)
    return (
        _is_uuid(wait_value)
        and active_wait is not None
        and active_wait.id == wait_value
    )


def parse_wait_end_id(
    payload: dict[str, Any], state: ParseState, fields: RecordFields, target: str
) -> None:
    wait_value = payload.get("wait_id")
    if not _matches_active_wait(state, target, wait_value):
        raise RecordRejected(REJECT_INVALID_WAIT_ID, str(wait_value))
    fields.wait_id = wait_value


def parse_receipt_wait(
    payload: dict[str, Any], state: ParseState, fields: RecordFields, target: str
) -> None:
    wait_value = payload.get("wait_id")
    question = state.questions[target]
    strict = (
        question.version >= MAILBOX_WAIT_SCHEMA_VERSION
        and question.delivery_mode == DELIVERY_MODE_WAIT
    )
    if wait_value is not None:
        if not _matches_active_wait(state, target, wait_value):
            raise RecordRejected(REJECT_INVALID_WAIT_ID, str(wait_value))
        fields.wait_id = wait_value
    elif strict or target in state.active_waits:
        raise RecordRejected(
            REJECT_QUESTION_NOT_WAITING if strict else REJECT_INVALID_WAIT_ID,
            target,
        )


def parse_wait_end_reason(
    payload: dict[str, Any], state: ParseState, fields: RecordFields, target: str
) -> None:
    reason_value = payload.get("reason")
    if reason_value not in WAIT_END_REASONS:
        raise RecordRejected(REJECT_INVALID_WAIT_END_REASON, str(reason_value))
    fields.wait_end_reason = reason_value
    closure_value = payload.get("closure_id")
    closure = state.closures.get(target)
    if reason_value == WAIT_END_REASON_CLOSURE:
        if (
            not _is_uuid(closure_value)
            or closure is None
            or closure.id != closure_value
            or closure_value not in state.receipts
        ):
            raise RecordRejected(REJECT_INVALID_CLOSURE_ID, str(closure_value))
        fields.closure_id = closure_value
    elif closure_value is not None or closure is not None:
        raise RecordRejected(REJECT_INVALID_CLOSURE_ID, str(closure_value))


def parse_receipt_closure(
    payload: dict[str, Any], state: ParseState, fields: RecordFields, target: str
) -> None:
    closure_value = payload.get("closure_id")
    closure = state.closures.get(target)
    if not _is_uuid(closure_value) or closure is None or closure.id != closure_value:
        raise RecordRejected(REJECT_INVALID_CLOSURE_ID, str(closure_value))
    if closure_value in state.receipts:
        raise RecordRejected(REJECT_CLOSURE_ALREADY_CONSUMED, closure_value)
    fields.closure_id = closure_value
