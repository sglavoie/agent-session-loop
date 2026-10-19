"""Conversation storage."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import (
    conversation_ledger_models as models,
)
from . import (
    conversation_references as references,
)
from . import (
    conversation_validation as validation,
)
from . import (
    durability,
)
from .clock import utc_iso_now


def conversation_ledger_path(plan_state: str | Path) -> Path:
    """The conversation ledger inside one plan's state directory."""
    return Path(plan_state) / validation.CONVERSATION_LEDGER_FILE_NAME


def conversation_lock_path(plan_state: str | Path) -> Path:
    """The ``flock`` target that serializes appends to that ledger."""
    return Path(plan_state) / validation.CONVERSATION_LOCK_FILE_NAME


def _encode_record(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        + b"\n"
    )


def _physical_lines(data: bytes) -> list[bytes]:
    """Split ledger bytes into physical lines, keeping an unterminated tail.

    A crash-truncated final line has no newline. It is a line all the same -- it
    occupies a sequence and is reported as rejected -- so splitting must not
    discard it, and an empty element after a proper final newline must not invent
    one.
    """
    if not data:
        return []
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


@contextmanager
def _append_lock(plan_state: Path) -> Iterator[None]:
    """Hold the exclusive claim on writing this plan's ledger.

    A sidecar rather than the ledger file itself: :func:`relocate_records`
    replaces that path, so two callers holding it could be holding different
    files. ``flock`` is owned by the open file description, so this excludes
    threads as well as processes, and the kernel releases it if the holder dies
    mid-write.
    """
    plan_state.mkdir(parents=True, exist_ok=True)
    lock_path = conversation_lock_path(plan_state)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def _append_record(plan_state: Path, payload: Mapping[str, Any]) -> int:
    """Append one prevalidated record as a single durable transaction.

    Returns the one-based sequence the appended line occupies.
    """
    encoded = _encode_record(payload)
    if len(encoded) > validation.MAX_CONVERSATION_RECORD_BYTES:
        raise validation.ConversationError(
            validation.ERROR_RECORD_TOO_LARGE,
            f"conversation record is {len(encoded)} bytes, over the "
            f"{validation.MAX_CONVERSATION_RECORD_BYTES}-byte limit",
        )
    ledger_path = conversation_ledger_path(plan_state)
    with _append_lock(plan_state):
        try:
            existing = ledger_path.read_bytes()
        except FileNotFoundError:
            existing = b""
        except OSError as error:
            raise validation.ConversationError(
                validation.ERROR_LEDGER_WRITE,
                f"could not read {ledger_path} before appending: {error}",
                {"path": str(ledger_path)},
            ) from error
        # Terminate an interrupted append rather than cutting it back: the
        # truncated bytes stay in history as a rejected line, which is what keeps
        # a crash visible instead of silently healed.
        separator = b"\n" if existing and not existing.endswith(b"\n") else b""
        chunk = separator + encoded
        if len(existing) + len(chunk) > validation.MAX_CONVERSATION_LEDGER_BYTES:
            raise validation.ConversationError(
                validation.ERROR_LEDGER_FULL,
                f"append would exceed {validation.MAX_CONVERSATION_LEDGER_BYTES} bytes",
                {"path": str(ledger_path)},
            )
        handle = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            original_size = os.fstat(handle).st_size
            try:
                written = os.write(handle, chunk)
                if written != len(chunk):
                    os.ftruncate(handle, original_size)
                    raise OSError(
                        f"short conversation append: wrote {written} of "
                        f"{len(chunk)} bytes"
                    )
                # The next turn's decision to resume rests on this line. Flush it
                # through the device cache before the lock is released, so a
                # sudden shutdown cannot lose a reference whose conversation the
                # harness has already created.
                durability.fullsync(handle)
            except OSError as error:
                with contextlib.suppress(OSError):
                    os.ftruncate(handle, original_size)
                raise validation.ConversationError(
                    validation.ERROR_LEDGER_WRITE,
                    f"could not append to {ledger_path}: {error}",
                    {"path": str(ledger_path)},
                ) from error
        finally:
            os.close(handle)
        return len(_physical_lines(existing + chunk))


def _parse_line(
    sequence: int, raw: bytes
) -> models.ConversationRecord | models.RejectedLine:
    text = raw.decode("utf-8", errors="replace")
    preview = text[: validation.MAX_REJECTED_RAW_CHARS]
    if not text.strip():
        return models.RejectedLine(sequence, validation.REJECT_EMPTY_LINE, preview)
    if len(raw) > validation.MAX_CONVERSATION_RECORD_BYTES:
        return models.RejectedLine(
            sequence, validation.REJECT_RECORD_TOO_LARGE, preview
        )
    try:
        parsed = json.loads(text)
    except ValueError:
        return models.RejectedLine(sequence, validation.REJECT_INVALID_JSON, preview)
    if not isinstance(parsed, dict):
        return models.RejectedLine(sequence, validation.REJECT_NOT_OBJECT, preview)
    version = parsed.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in validation.SUPPORTED_CONVERSATION_SCHEMA_VERSIONS
    ):
        return models.RejectedLine(
            sequence, validation.REJECT_UNSUPPORTED_VERSION, preview
        )
    if parsed.get("kind") not in validation.CONVERSATION_KINDS:
        return models.RejectedLine(sequence, validation.REJECT_UNKNOWN_KIND, preview)
    try:
        kind = validation._narrow(
            parsed.get("kind"), validation.CONVERSATION_KINDS, "kind"
        )
        reference = references.parse_conversation_reference(parsed.get("reference"))
        raw_reason = parsed.get("retirement_reason")
        reason: validation.RetirementReason | None = None
        if kind == validation.KIND_CONVERSATION_RETIRED:
            reason = validation._narrow(
                raw_reason,
                validation.RETIREMENT_REASONS,
                "retirement_reason",
            )
        elif raw_reason is not None:
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                f"only a {validation.KIND_CONVERSATION_RETIRED} record may carry a "
                "retirement reason",
            )
        recorded_at = parsed.get("recorded_at")
        if not isinstance(recorded_at, str) or not recorded_at.strip():
            raise validation.ConversationError(
                validation.ERROR_INVALID_RECORD,
                "record requires a recorded_at timestamp",
            )
    except validation.ConversationError:
        return models.RejectedLine(sequence, validation.REJECT_INVALID_RECORD, preview)
    return models.ConversationRecord(
        sequence=sequence,
        kind=kind,
        reference=reference,
        recorded_at=recorded_at,
        retirement_reason=reason,
    )


def read_conversations(
    plan_state: str | Path,
) -> models.ConversationLedger:
    """Read one plan's conversation ledger, readable lines and rejected ones.

    A plan whose state predates this feature has no ledger at all, and that is an
    empty result rather than an exception: absent conversation state is a
    recovery decision for the caller and never a crash. A ledger file that cannot
    be *opened* is different from one that is merely absent -- it is reported as a
    rejected line, so selection fails closed instead of reading an unreadable
    ledger as an empty one.
    """
    ledger_path = conversation_ledger_path(plan_state)
    try:
        data = ledger_path.read_bytes()
    except FileNotFoundError, NotADirectoryError:
        return models.ConversationLedger(records=(), rejected=())
    except OSError as error:
        return models.ConversationLedger(
            records=(),
            rejected=(
                models.RejectedLine(1, validation.REJECT_UNREADABLE_LEDGER, f"{error}"),
            ),
        )
    records: list[models.ConversationRecord] = []
    rejected: list[models.RejectedLine] = []
    for offset, raw in enumerate(_physical_lines(data)):
        parsed = _parse_line(offset + 1, raw)
        if isinstance(parsed, models.RejectedLine):
            rejected.append(parsed)
        else:
            records.append(parsed)
    return models.ConversationLedger(records=tuple(records), rejected=tuple(rejected))


def _write_record(
    plan_state: str | Path,
    *,
    kind: validation.ConversationKind,
    reference: references.ConversationReference,
    retirement_reason: validation.RetirementReason | None = None,
) -> models.ConversationRecord:
    if not isinstance(reference, references.ConversationReference):
        raise validation.ConversationError(
            validation.ERROR_INVALID_REFERENCE,
            "reference must be a ConversationReference value",
        )
    record = models.ConversationRecord(
        sequence=0,
        kind=kind,
        reference=reference,
        recorded_at=utc_iso_now(),
        retirement_reason=retirement_reason,
    )
    sequence = _append_record(Path(plan_state), record.to_payload())
    return models.ConversationRecord(
        sequence=sequence,
        kind=record.kind,
        reference=record.reference,
        recorded_at=record.recorded_at,
        retirement_reason=record.retirement_reason,
    )
