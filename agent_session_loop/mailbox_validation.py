"""Identifier, timestamp, and sequence validation helpers for mailbox records."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from .mailbox_constants import (
    MAILBOX_MAX_HARNESS_CHARS,
    MAILBOX_MAX_INTEGER,
    MAILBOX_MAX_RUN_ID_CHARS,
    MAILBOX_MAX_SESSION_ID_CHARS,
)
from .mailbox_models import RejectedMailboxRecord


def _new_id() -> str:
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="microseconds")


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    try:
        uuid.UUID(value)
    except ValueError, AttributeError:
        return False
    return True


def is_supervision_id(value: object) -> bool:
    """Return whether ``value`` can identify one supervising launch."""
    return _is_uuid(value)


def new_supervision_id() -> str:
    """Mint a correlation identity for one supervising launch."""
    return str(uuid.uuid4())


def _valid_run_id(value: object) -> bool:
    """Return whether ``value`` is a bounded, nonblank run identifier."""
    return (
        isinstance(value, str)
        and value.strip() != ""
        and len(value) <= MAILBOX_MAX_RUN_ID_CHARS
    )


def _valid_session_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) > MAILBOX_MAX_SESSION_ID_CHARS:
        return False
    components = value.split(".")
    return bool(components) and all(
        component.isascii() and component.isdigit() and component[0] != "0"
        for component in components
    )


def _valid_harness(value: object) -> bool:
    """Return whether ``value`` is a bounded, nonblank harness name."""
    return (
        isinstance(value, str)
        and value.strip() != ""
        and len(value) <= MAILBOX_MAX_HARNESS_CHARS
    )


def _valid_turn_index(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 < value <= MAILBOX_MAX_INTEGER
    )


def _parse_timestamp(value: object, *, require_utc: bool = False) -> datetime | None:
    if not isinstance(value, str) or value == "":
        return None
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    if require_utc and parsed.utcoffset() != timedelta(0):
        return None
    return parsed


def _validate_sequence(value: object, *, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAILBOX_MAX_INTEGER
    ):
        raise ValueError(
            f"{field_name} must be a non-negative integer no greater than "
            f"{MAILBOX_MAX_INTEGER}"
        )
    return value


def _physical_lines(data: bytes) -> list[bytes]:
    if not data:
        return []
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines.pop()
    return lines


def _reject(
    rejected: list[RejectedMailboxRecord],
    *,
    seq: int,
    reason: str,
    raw: str,
    detail: str | None = None,
) -> None:
    rejected.append(
        RejectedMailboxRecord(
            seq=seq,
            reason=reason,
            raw=raw,
            detail=detail,
        )
    )
