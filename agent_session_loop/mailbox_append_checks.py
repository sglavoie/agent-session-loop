"""Validate an append against the current mailbox view under the lock."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .mailbox_constants import (
    MAILBOX_WAIT_SCHEMA_VERSION,
    REJECT_CLOSURE_ALREADY_CONSUMED,
    REJECT_CLOSURE_NOT_FOUND,
    REJECT_GUIDANCE_ALREADY_DELIVERED,
    REJECT_GUIDANCE_NOT_FOUND,
    REJECT_INVALID_ACKNOWLEDGEMENT,
    REJECT_INVALID_WAIT_ID,
    REJECT_QUESTION_ALREADY_ANSWERED,
    REJECT_QUESTION_ALREADY_RESOLVED,
    REJECT_QUESTION_ALREADY_WAITING,
    REJECT_QUESTION_NOT_FOUND,
    REJECT_QUESTION_NOT_OPEN,
    REJECT_QUESTION_NOT_WAITING,
)
from .mailbox_models import MailboxError
from .mailbox_snapshot import MailboxView
from .mailbox_vocabulary import (
    CLOSURE_KINDS,
    DELIVERY_MODE_WAIT,
    GUIDANCE_DELIVERED_STATES,
    KIND_ACK,
    KIND_GUIDANCE,
    KIND_GUIDANCE_DELIVERY,
    KIND_QUESTION,
    KIND_RECEIPT,
    KIND_REPLY,
    KIND_RESOLUTION,
    KIND_WAIT_END,
    KIND_WAIT_START,
    MessageKind,
)


def _check_append_preconditions(
    *,
    kind: MessageKind,
    record: dict[str, Any],
    current_view: MailboxView,
    mailbox_path: Path,
    seq: int,
) -> bool:
    strict_wait_closure = False
    if kind == KIND_ACK:
        ack_upto = record["upto"]
        if ack_upto >= seq:
            raise MailboxError(
                REJECT_INVALID_ACKNOWLEDGEMENT,
                f"upto {ack_upto} must be less than append sequence {seq}",
                path=mailbox_path,
            )
    elif kind in CLOSURE_KINDS:
        question_id = record["to"]
        question = next(
            (
                message
                for message in current_view.messages
                if message.kind == KIND_QUESTION and message.id == question_id
            ),
            None,
        )
        if question is None:
            raise MailboxError(
                REJECT_QUESTION_NOT_FOUND,
                question_id,
                path=mailbox_path,
            )
        closure = current_view.closure_for(question_id)
        if closure is not None:
            raise MailboxError(
                (
                    REJECT_QUESTION_ALREADY_ANSWERED
                    if closure.kind == KIND_REPLY
                    else REJECT_QUESTION_ALREADY_RESOLVED
                ),
                question_id,
                path=mailbox_path,
            )
        if kind == KIND_RESOLUTION and question.run_id != record["run_id"]:
            raise MailboxError(
                REJECT_QUESTION_NOT_OPEN,
                question_id,
                path=mailbox_path,
            )
        if (
            question.version >= MAILBOX_WAIT_SCHEMA_VERSION
            and question.delivery_mode == DELIVERY_MODE_WAIT
            and current_view.active_wait_for(question_id) is None
        ):
            raise MailboxError(
                REJECT_QUESTION_NOT_WAITING,
                question_id,
                path=mailbox_path,
            )
        strict_wait_closure = (
            question.version >= MAILBOX_WAIT_SCHEMA_VERSION
            and question.delivery_mode == DELIVERY_MODE_WAIT
        )
        if not strict_wait_closure:
            strict_wait_closure = any(
                candidate.kind == KIND_QUESTION
                and current_view.active_wait_for(candidate.id) is not None
                for candidate in current_view.messages
            )

    if kind == KIND_GUIDANCE_DELIVERY:
        guidance_id = record["to"]
        if not any(
            message.kind == KIND_GUIDANCE and message.id == guidance_id
            for message in current_view.messages
        ):
            raise MailboxError(
                REJECT_GUIDANCE_NOT_FOUND,
                guidance_id,
                path=mailbox_path,
            )
        if record["delivery_state"] in GUIDANCE_DELIVERED_STATES and any(
            message.kind == KIND_GUIDANCE_DELIVERY
            and message.to == guidance_id
            and message.delivery_state in GUIDANCE_DELIVERED_STATES
            and message.run_id == record.get("run_id")
            and message.session_id == record.get("session_id")
            and message.turn_index == record.get("turn_index")
            for message in current_view.messages
        ):
            raise MailboxError(
                REJECT_GUIDANCE_ALREADY_DELIVERED,
                guidance_id,
                path=mailbox_path,
            )

    if kind == KIND_WAIT_START:
        question_id = record["to"]
        question = next(
            (
                message
                for message in current_view.messages
                if message.kind == KIND_QUESTION and message.id == question_id
            ),
            None,
        )
        if question is None:
            raise MailboxError(
                REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
            )
        if current_view.closure_for(question_id) is not None:
            raise MailboxError(REJECT_QUESTION_NOT_OPEN, question_id, path=mailbox_path)
        if current_view.active_wait_for(question_id) is not None:
            raise MailboxError(
                REJECT_QUESTION_ALREADY_WAITING,
                question_id,
                path=mailbox_path,
            )

    if kind == KIND_WAIT_END:
        question_id = record["to"]
        active_wait = current_view.active_wait_for(question_id)
        if active_wait is None or active_wait.id != record["wait_id"]:
            raise MailboxError(
                REJECT_QUESTION_NOT_WAITING,
                question_id,
                path=mailbox_path,
            )

    if kind == KIND_RECEIPT:
        question_id = record["to"]
        question = next(
            (
                message
                for message in current_view.messages
                if message.kind == KIND_QUESTION and message.id == question_id
            ),
            None,
        )
        if question is None:
            raise MailboxError(
                REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
            )
        closure = current_view.closure_for(question_id)
        if closure is None or closure.id != record["closure_id"]:
            raise MailboxError(
                REJECT_CLOSURE_NOT_FOUND,
                record["closure_id"],
                path=mailbox_path,
            )
        if current_view.receipt_for_closure(closure.id) is not None:
            raise MailboxError(
                REJECT_CLOSURE_ALREADY_CONSUMED,
                closure.id,
                path=mailbox_path,
            )
        active_wait = current_view.active_wait_for(question_id)
        strict = (
            question.version >= MAILBOX_WAIT_SCHEMA_VERSION
            and question.delivery_mode == DELIVERY_MODE_WAIT
        )
        if active_wait is not None and record.get("wait_id") is None:
            raise MailboxError(
                (REJECT_QUESTION_NOT_WAITING if strict else REJECT_INVALID_WAIT_ID),
                question_id,
                path=mailbox_path,
            )
        if strict and (active_wait is None or active_wait.id != record.get("wait_id")):
            raise MailboxError(
                REJECT_QUESTION_NOT_WAITING,
                question_id,
                path=mailbox_path,
            )
        if record.get("wait_id") is not None and (
            active_wait is None or active_wait.id != record["wait_id"]
        ):
            raise MailboxError(
                REJECT_INVALID_WAIT_ID,
                record["wait_id"],
                path=mailbox_path,
            )

    return strict_wait_closure
