"""Locked, non-destructive mailbox storage for supervised ASL runs.

The mailbox is a per-plan JSONL log. Supervisors append ``guidance``, ``reply``,
and ``resolution`` records; workers append ``question``, ``notice``, and
``ack`` records. Reads never truncate the log, and the physical one-based line
number is the durable sequence number. Invalid lines therefore consume
sequence numbers without preventing later valid records from being read.

The adjacent lease is deliberately separate from the append-only log. It is a
small, fixed-size record renewed by the supervisor so readers can distinguish
an actively supervised run from stale mailbox files.

This module keeps the locked read primitives and record lookups. Constants,
models, parsing, appenders, waits and leases live in the sibling
``mailbox_*`` modules; callers import each symbol from its owner.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Any

from . import durability
from .mailbox_constants import (
    ERROR_MAILBOX_FULL,
    ERROR_MAILBOX_READ,
    MAILBOX_CHANGE_TOKEN_UNAVAILABLE,
)
from .mailbox_cursors import mailbox_path_for_plan
from .mailbox_models import MailboxError, MailboxMessage
from .mailbox_parsing import _parse_mailbox_bytes
from .mailbox_snapshot import MailboxView
from .mailbox_validation import _physical_lines
from .mailbox_vocabulary import MessageKind


def _read_locked(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _message_from_record(record: dict[str, Any], seq: int) -> MailboxMessage:
    return MailboxMessage(
        seq=seq,
        id=record["id"],
        kind=record["kind"],
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


def _encoded_records(records: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
        for record in records
    )


def _write_locked_records(
    fd: int,
    *,
    existing: bytes,
    records: list[dict[str, Any]],
    effective_max_bytes: int,
    mailbox_path: Path,
) -> tuple[MailboxMessage, ...]:
    """Append a prevalidated batch as one lock-scoped lifecycle transaction."""
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    encoded = separator + _encoded_records(records)
    original_size = os.fstat(fd).st_size
    if original_size + len(encoded) > effective_max_bytes:
        raise MailboxError(
            ERROR_MAILBOX_FULL,
            f"append would exceed {effective_max_bytes} bytes",
            path=mailbox_path,
        )
    try:
        written = os.write(fd, encoded)
        if written != len(encoded):
            os.ftruncate(fd, original_size)
            raise OSError(
                f"short mailbox append: wrote {written} of {len(encoded)} bytes"
            )
        # Mailbox records carry durable decisions. Flush the completed
        # transaction before releasing its lock so a sudden shutdown cannot
        # acknowledge a reply, resolution, or receipt that never reached disk.
        durability.fullsync(fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, original_size)
        raise
    first_seq = len(_physical_lines(existing)) + 1
    return tuple(
        _message_from_record(record, first_seq + offset)
        for offset, record in enumerate(records)
    )


def mailbox_change_token(mailbox_path: str | Path) -> object:
    """Return cheap append-log metadata used to avoid unchanged full reads."""
    try:
        stat_result = Path(mailbox_path).stat()
    except FileNotFoundError:
        return None
    except OSError:
        # The metadata probe is only an optimization. Callers must perform a
        # full read so the mailbox API preserves its established error behavior.
        return MAILBOX_CHANGE_TOKEN_UNAVAILABLE
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def read_mailbox(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> MailboxView:
    """Read and validate the complete mailbox under a shared lock."""
    mailbox_path = mailbox_path_for_plan(plan_dir, state_dir)
    try:
        fd = os.open(mailbox_path, os.O_RDONLY)
    except FileNotFoundError:
        return MailboxView(mailbox_path=mailbox_path)
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_READ,
            str(error),
            path=mailbox_path,
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        data = _read_locked(fd)
    except OSError as error:
        raise MailboxError(
            ERROR_MAILBOX_READ,
            str(error),
            path=mailbox_path,
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return _parse_mailbox_bytes(data, mailbox_path)


def read_messages_since(
    plan_dir: str | Path,
    since: int,
    *,
    state_dir: str | Path | None = None,
    kinds: tuple[MessageKind, ...] | None = None,
) -> tuple[MailboxMessage, ...]:
    """Perform a stateless read and return accepted records after ``since``."""
    return read_mailbox(plan_dir, state_dir=state_dir).messages_since(
        since,
        kinds=kinds,
    )


def pending_guidance(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    return read_mailbox(plan_dir, state_dir=state_dir).pending_guidance


def unanswered_questions(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    return read_mailbox(plan_dir, state_dir=state_dir).unanswered_questions


def unanswered_questions_for_run(
    plan_dir: str | Path,
    run_id: str | None,
    *,
    state_dir: str | Path | None = None,
) -> tuple[MailboxMessage, ...]:
    """Select open questions owned by exactly this run, not its plan's history.

    Unknown ownership is not current ownership. The plan-wide mailbox API stays
    unchanged so direct operators can still inspect and close historical records.
    """
    if not isinstance(run_id, str) or not run_id:
        return ()
    return tuple(
        message
        for message in unanswered_questions(plan_dir, state_dir=state_dir)
        if message.run_id == run_id
    )


def find_reply(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
) -> MailboxMessage | None:
    return read_mailbox(plan_dir, state_dir=state_dir).reply_for(question_id)


def find_resolution(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
) -> MailboxMessage | None:
    return read_mailbox(plan_dir, state_dir=state_dir).resolution_for(question_id)


def find_closure(
    plan_dir: str | Path,
    question_id: str,
    *,
    state_dir: str | Path | None = None,
) -> MailboxMessage | None:
    return read_mailbox(plan_dir, state_dir=state_dir).closure_for(question_id)
