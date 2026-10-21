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
from .mailbox_append_checks import _check_append_preconditions
from .mailbox_append_normalize import _normalize_append_record
from .mailbox_constants import (
    ERROR_MAILBOX_FULL,
    ERROR_MAILBOX_WRITE,
    MAILBOX_CLOSURE_RESERVE_BYTES,
    MAILBOX_GUIDANCE_DELIVERY_RESERVE_BYTES,
    MAILBOX_MAX_BYTES,
    MAILBOX_RECEIPT_RESERVE_BYTES,
    MAILBOX_WAIT_END_RESERVE_BYTES,
    REJECT_DUPLICATE_ID,
)
from .mailbox_cursors import mailbox_path_for_plan
from .mailbox_models import MailboxError, MailboxMessage
from .mailbox_parsing import _parse_mailbox_bytes
from .mailbox_vocabulary import (
    CLOSURE_KINDS,
    KIND_GUIDANCE_DELIVERY,
    KIND_RECEIPT,
    KIND_SUPERVISION_END,
    KIND_WAIT_END,
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
        strict_wait_closure = _check_append_preconditions(
            kind=kind,
            record=record,
            current_view=current_view,
            mailbox_path=mailbox_path,
            seq=seq,
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
