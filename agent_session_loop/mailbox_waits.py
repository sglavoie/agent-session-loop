"""Reply-wait records: start, finish, and closure consumption."""

from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Any

from .mailbox import _read_locked, _write_locked_records, read_mailbox
from .mailbox_append_normalize import _normalize_append_record
from .mailbox_append_record import _append_record
from .mailbox_constants import (
    ERROR_MAILBOX_WRITE,
    MAILBOX_CLOSURE_RESERVE_BYTES,
    MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES,
    MAILBOX_MAX_BYTES,
    MAILBOX_RECEIPT_RESERVE_BYTES,
    REJECT_DUPLICATE_ID,
    REJECT_INVALID_TARGET,
    REJECT_INVALID_WAIT_END_REASON,
    REJECT_INVALID_WAIT_ID,
    REJECT_QUESTION_ALREADY_ANSWERED,
    REJECT_QUESTION_ALREADY_RESOLVED,
    REJECT_QUESTION_NOT_FOUND,
    REJECT_QUESTION_NOT_WAITING,
)
from .mailbox_cursors import mailbox_path_for_plan
from .mailbox_models import MailboxError, MailboxMessage, WaitCompletion
from .mailbox_parsing import _parse_mailbox_bytes
from .mailbox_validation import _is_uuid
from .mailbox_vocabulary import (
    DELIVERY_MODE_WAIT,
    KIND_QUESTION,
    KIND_RECEIPT,
    KIND_REPLY,
    KIND_WAIT_END,
    KIND_WAIT_START,
    WAIT_END_REASON_CLOSURE,
    WAIT_END_REASON_ERROR,
    WAIT_END_REASON_TIMEOUT,
    WaitEndReason,
    WaiterOrigin,
)


def append_wait_question(
    plan_dir: str | Path,
    text: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    wait_id: str | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    session_id: str | None = None,
    turn_index: int | None = None,
    waiter_origin: WaiterOrigin | None = None,
) -> tuple[MailboxMessage, MailboxMessage]:
    """Atomically create a wait-delivered question and its first active wait."""
    question_record = _normalize_append_record(
        kind=KIND_QUESTION,
        text=text,
        target=None,
        upto=None,
        message_id=message_id,
        created_at=created_at,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        waiter_origin=waiter_origin,
        delivery_mode=DELIVERY_MODE_WAIT,
    )
    wait_record = _normalize_append_record(
        kind=KIND_WAIT_START,
        text=None,
        target=question_record["id"],
        upto=None,
        message_id=wait_id,
        created_at=created_at,
    )
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        mailbox_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(mailbox_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE, str(error), path=mailbox_path
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        existing = _read_locked(fd)
        view = _parse_mailbox_bytes(existing, mailbox_path)
        existing_ids = {message.id for message in view.messages}
        for record in (question_record, wait_record):
            if record["id"] in existing_ids:
                raise MailboxError(REJECT_DUPLICATE_ID, record["id"], path=mailbox_path)
            existing_ids.add(record["id"])
        messages = _write_locked_records(
            fd,
            existing=existing,
            records=[question_record, wait_record],
            effective_max_bytes=(
                MAILBOX_MAX_BYTES
                - MAILBOX_CLOSURE_RESERVE_BYTES
                - MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES
            ),
            mailbox_path=mailbox_path,
        )
    except MailboxError:
        raise
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE, str(error), path=mailbox_path
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return messages[0], messages[1]


def append_wait_start(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
    message_id: str | None = None,
    created_at: str | None = None,
) -> MailboxMessage:
    """Start a resumed wait for an open v3 wait-delivered question."""
    return _append_record(
        plan_dir,
        kind=KIND_WAIT_START,
        state_dir=state_dir,
        target=question_id,
        message_id=message_id,
        created_at=created_at,
    )


def finish_wait(
    plan_dir: str | Path,
    question_id: str,
    wait_id: str,
    *,
    reason: WaitEndReason | str,
    state_dir: str | Path | None = None,
    created_at: str | None = None,
    consume_closure: bool = True,
) -> WaitCompletion:
    """Atomically end a wait, giving any committed closure priority.

    The mailbox lock is the serialization point for timeout-versus-closure
    races.  If a closure won first, this transaction records its receipt and a
    closure wait-end.  Otherwise it records the requested timeout/error end;
    any later strict wait-mode closure is rejected as ``question-not-waiting``.

    ``consume_closure=False`` is for a waiter that is going away without being
    able to act on an answer: a closure found under the lock is then refused
    rather than receipted, so the answer is never reported consumed by a run
    that did not deliver it.
    """
    if reason not in (WAIT_END_REASON_TIMEOUT, WAIT_END_REASON_ERROR):
        raise MailboxError(REJECT_INVALID_WAIT_END_REASON, str(reason))
    if not _is_uuid(question_id):
        raise MailboxError(REJECT_INVALID_TARGET)
    if not _is_uuid(wait_id):
        raise MailboxError(REJECT_INVALID_WAIT_ID)
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        fd = os.open(mailbox_path, os.O_RDWR | os.O_APPEND)
    except FileNotFoundError as error:
        raise MailboxError(
            REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
        ) from error
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE, str(error), path=mailbox_path
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_locked(fd)
        view = _parse_mailbox_bytes(existing, mailbox_path)
        question = next(
            (
                message
                for message in view.messages
                if message.kind == KIND_QUESTION and message.id == question_id
            ),
            None,
        )
        if question is None:
            raise MailboxError(
                REJECT_QUESTION_NOT_FOUND, question_id, path=mailbox_path
            )
        wait_start = view.active_wait_for(question_id)
        if wait_start is None or wait_start.id != wait_id:
            raise MailboxError(
                REJECT_QUESTION_NOT_WAITING, question_id, path=mailbox_path
            )
        closure = view.closure_for(question_id)
        if closure is not None and not consume_closure:
            raise MailboxError(
                REJECT_QUESTION_ALREADY_ANSWERED
                if closure.kind == KIND_REPLY
                else REJECT_QUESTION_ALREADY_RESOLVED,
                question_id,
                path=mailbox_path,
            )
        records: list[dict[str, Any]] = []
        receipt: MailboxMessage | None = None
        if closure is not None:
            existing_receipt = view.receipt_for_closure(closure.id)
            if existing_receipt is None:
                records.append(
                    _normalize_append_record(
                        kind=KIND_RECEIPT,
                        text=None,
                        target=question_id,
                        upto=None,
                        message_id=None,
                        created_at=created_at,
                        wait_id=wait_id,
                        closure_id=closure.id,
                    )
                )
            else:
                receipt = existing_receipt
            end_reason: WaitEndReason = WAIT_END_REASON_CLOSURE
        else:
            end_reason = reason
        records.append(
            _normalize_append_record(
                kind=KIND_WAIT_END,
                text=None,
                target=question_id,
                upto=None,
                message_id=None,
                created_at=created_at,
                wait_id=wait_id,
                wait_end_reason=end_reason,
                closure_id=closure.id if closure is not None else None,
            )
        )
        written = _write_locked_records(
            fd,
            existing=existing,
            records=records,
            effective_max_bytes=(
                MAILBOX_MAX_BYTES
                if closure is not None
                else MAILBOX_MAX_BYTES - MAILBOX_RECEIPT_RESERVE_BYTES
            ),
            mailbox_path=mailbox_path,
        )
        if closure is not None and receipt is None:
            receipt = written[0]
        wait_end = written[-1]
    except MailboxError:
        raise
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_WRITE, str(error), path=mailbox_path
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return WaitCompletion(
        question=question,
        wait_start=wait_start,
        wait_end=wait_end,
        closure=closure,
        receipt=receipt,
    )


def consume_wait_closure(
    plan_dir: str | Path,
    question_id: str,
    wait_id: str,
    *,
    state_dir: str | Path | None = None,
) -> WaitCompletion | None:
    """Consume a closure if present without ending an otherwise active wait."""
    view = read_mailbox(plan_dir, state_dir=state_dir)
    if view.closure_for(question_id) is None:
        return None
    return finish_wait(
        plan_dir,
        question_id,
        wait_id,
        reason=WAIT_END_REASON_ERROR,
        state_dir=state_dir,
    )
