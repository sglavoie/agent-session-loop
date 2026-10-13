"""Normalize and durably append one mailbox record under the lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import durability
from .mailbox import _read_locked
from .mailbox_constants import (
    ERROR_MAILBOX_FULL,
    ERROR_MAILBOX_WRITE,
    MAILBOX_CLOSURE_RESERVE_BYTES,
    MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES,
    MAILBOX_MAX_BYTES,
    MAILBOX_RECEIPT_RESERVE_BYTES,
    MAILBOX_SCHEMA_VERSION,
    MAILBOX_WAIT_END_RESERVE_BYTES,
    MAILBOX_WAIT_SCHEMA_VERSION,
    REJECT_CLOSURE_ALREADY_CONSUMED,
    REJECT_CLOSURE_NOT_FOUND,
    REJECT_DUPLICATE_ID,
    REJECT_GUIDANCE_ALREADY_DELIVERED,
    REJECT_GUIDANCE_NOT_FOUND,
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
    REJECT_QUESTION_ALREADY_ANSWERED,
    REJECT_QUESTION_ALREADY_RESOLVED,
    REJECT_QUESTION_ALREADY_WAITING,
    REJECT_QUESTION_NOT_FOUND,
    REJECT_QUESTION_NOT_OPEN,
    REJECT_QUESTION_NOT_WAITING,
    REJECT_UNKNOWN_KIND,
)
from .mailbox_cursors import mailbox_path_for_plan
from .mailbox_models import MailboxError, MailboxMessage
from .mailbox_parsing import _parse_mailbox_bytes
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
    DELIVERY_MODE_WAIT,
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
    KIND_WAIT_START,
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


def _append_record(
    plan_dir: str | Path,
    *,
    kind: MessageKind,
    state_dir: str | Path | None,
    text: str | None = None,
    target: str | None = None,
    upto: int | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
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
    commit_fields: Callable[[], Mapping[str, Any]] | None = None,
) -> MailboxMessage:
    fields: dict[str, Any] = {
        "kind": kind,
        "text": text,
        "target": target,
        "upto": upto,
        "message_id": message_id,
        "created_at": created_at,
        "run_id": run_id,
        "session_id": session_id,
        "turn_index": turn_index,
        "waiter_origin": waiter_origin,
        "action": action,
        "note": note,
        "delivery_mode": delivery_mode,
        "wait_id": wait_id,
        "wait_end_reason": wait_end_reason,
        "closure_id": closure_id,
        "supervision_outcome": supervision_outcome,
        "supervision_id": supervision_id,
        "delivery_state": delivery_state,
        "delivery_reason": delivery_reason,
        "channel": channel,
        "harness": harness,
    }
    record = _normalize_append_record(**fields)
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        mailbox_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            mailbox_path,
            os.O_RDWR | os.O_CREAT | os.O_APPEND,
            0o600,
        )
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE,
            str(error),
            path=mailbox_path,
        ) from error

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        existing = _read_locked(fd)
        current_view = _parse_mailbox_bytes(existing, mailbox_path)
        if any(message.id == record["id"] for message in current_view.messages):
            raise MailboxError(
                REJECT_DUPLICATE_ID,
                str(record["id"]),
                path=mailbox_path,
            )

        seq = current_view.last_seq + 1
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
                raise MailboxError(
                    REJECT_QUESTION_NOT_OPEN, question_id, path=mailbox_path
                )
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
            if strict and (
                active_wait is None or active_wait.id != record.get("wait_id")
            ):
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

        separator = b"\n" if existing and not existing.endswith(b"\n") else b""
        original_size = os.fstat(fd).st_size
        if kind == KIND_RECEIPT:
            effective_max_bytes = MAILBOX_MAX_BYTES
        elif kind == KIND_SUPERVISION_END:
            # Refusing the terminal signal on a full mailbox would strand the
            # supervising agent in the dead wait this record exists to end.
            effective_max_bytes = MAILBOX_MAX_BYTES
        elif kind == KIND_WAIT_END:
            effective_max_bytes = MAILBOX_MAX_BYTES - MAILBOX_RECEIPT_RESERVE_BYTES
        elif kind in CLOSURE_KINDS and strict_wait_closure:
            effective_max_bytes = (
                MAILBOX_MAX_BYTES
                - MAILBOX_WAIT_END_RESERVE_BYTES
                - MAILBOX_RECEIPT_RESERVE_BYTES
            )
        elif kind in CLOSURE_KINDS:
            effective_max_bytes = MAILBOX_MAX_BYTES
        elif kind == KIND_GUIDANCE_DELIVERY:
            effective_max_bytes = MAILBOX_MAX_BYTES - MAILBOX_CLOSURE_RESERVE_BYTES
        else:
            effective_max_bytes = (
                MAILBOX_MAX_BYTES
                - MAILBOX_CLOSURE_RESERVE_BYTES
                - MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
            )
        # One correction is enough for a resolver that settles once, which is
        # what the terminal supervision record needs: it flips to `error` and
        # stays there. The bound keeps a resolver that never settles from
        # spinning while it holds the exclusive lock.
        corrections_left = 1
        committed: dict[str, Any] = {}
        while True:
            if commit_fields is not None:
                # Resolved with the lock held and every validation passed, so a
                # caller whose record can still change while the append is in
                # flight commits what was true at the write rather than at the
                # call. The identity and timestamp stay pinned to the first
                # normalization: only the resolved fields move.
                committed = dict(commit_fields())
                fields.update(committed)
                fields["message_id"] = record["id"]
                fields["created_at"] = record["created_at"]
                record = _normalize_append_record(**fields)
            encoded_record = (
                json.dumps(record, separators=(",", ":"), sort_keys=True).encode(
                    "utf-8"
                )
                + b"\n"
            )
            append_bytes = separator + encoded_record
            if original_size + len(append_bytes) > effective_max_bytes:
                raise MailboxError(
                    ERROR_MAILBOX_FULL,
                    f"append would exceed {effective_max_bytes} bytes",
                    path=mailbox_path,
                )
            try:
                written = os.write(fd, append_bytes)
                if written != len(append_bytes):
                    os.ftruncate(fd, original_size)
                    raise OSError(
                        f"short mailbox append: wrote {written} of "
                        f"{len(append_bytes)} bytes"
                    )
                durability.fullsync(fd)
                if (
                    commit_fields is not None
                    and corrections_left
                    and dict(commit_fields()) != committed
                ):
                    # The resolution moved during the write or the durability
                    # barrier, the windows no decision taken before them can
                    # see. The line is rolled back and rewritten while the
                    # exclusive lock is still held, and the rewrite is synced
                    # in turn, so the superseded value never becomes the
                    # durable record and is never readable: every reader takes
                    # a shared lock on this same file.
                    corrections_left -= 1
                    os.ftruncate(fd, original_size)
                    continue
            except OSError:
                # A regular-file write should be all-or-error, but restoring
                # the original length also fails safe on an observable short
                # write.
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, original_size)
                raise
            break
    except MailboxError:
        raise
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE,
            str(error),
            path=mailbox_path,
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    return MailboxMessage(
        seq=seq,
        id=record["id"],
        kind=kind,
        created_at=record["created_at"],
        text=record.get("text"),
        to=record.get("to"),
        upto=record.get("upto"),
        run_id=record.get("run_id"),
        session_id=record.get("session_id"),
        turn_index=record.get("turn_index"),
        waiter_origin=record.get("waiter_origin"),
        action=record.get("action"),
        note=record.get("note"),
        delivery_mode=record.get("delivery_mode"),
        wait_id=record.get("wait_id"),
        wait_end_reason=record.get("reason"),
        closure_id=record.get("closure_id"),
        supervision_outcome=record.get("outcome"),
        supervision_id=record.get("supervision_id"),
        delivery_state=record.get("delivery_state"),
        delivery_reason=record.get("delivery_reason"),
        channel=record.get("channel"),
        harness=record.get("harness"),
        version=record["version"],
    )
