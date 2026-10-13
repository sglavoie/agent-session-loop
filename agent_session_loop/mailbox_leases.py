"""The supervisor lease adjacent to the mailbox log."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from .mailbox import _read_locked
from .mailbox_constants import (
    ERROR_LEASE_READ,
    ERROR_LEASE_WRITE,
    LEASE_EMPTY,
    LEASE_EXPIRED,
    LEASE_FUTURE_UPDATED_AT,
    LEASE_INCOMPLETE_QUESTION_PROJECTION,
    LEASE_INVALID_JSON,
    LEASE_INVALID_LATEST_QUESTION_CREATED_AT,
    LEASE_INVALID_LATEST_QUESTION_ID,
    LEASE_INVALID_PROJECTION_SEQ,
    LEASE_INVALID_RUN_ID,
    LEASE_INVALID_SIZE,
    LEASE_INVALID_SUPERVISOR_ID,
    LEASE_INVALID_UNANSWERED_QUESTIONS,
    LEASE_INVALID_UPDATED_AT,
    LEASE_MISSING,
    LEASE_NOT_OBJECT,
    LEASE_UNSUPPORTED_VERSION,
    MAILBOX_LEASE_BYTES,
    MAILBOX_LEASE_FRESH_SECONDS,
    MAILBOX_LEASE_SUPPORTED_VERSIONS,
)
from .mailbox_cursors import lease_path_for_plan
from .mailbox_models import MailboxError, MailboxLease, MailboxLeaseView
from .mailbox_validation import _is_uuid, _parse_timestamp, _utc_now, _utc_now_iso


def _normalize_now(now: datetime | None) -> datetime:
    resolved = _utc_now() if now is None else now
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return resolved.astimezone(UTC)


def _validate_lease_age(max_age_seconds: float) -> float:
    try:
        resolved = float(max_age_seconds)
    except (TypeError, ValueError) as error:
        raise ValueError("max_age_seconds must be a non-negative number") from error
    if not math.isfinite(resolved) or resolved < 0:
        raise ValueError("max_age_seconds must be a non-negative number")
    return resolved


def _lease_bytes(lease: MailboxLease) -> bytes:
    encoded = (
        json.dumps(
            lease.to_payload(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAILBOX_LEASE_BYTES:
        raise MailboxError(
            ERROR_LEASE_WRITE,
            f"lease payload exceeds {MAILBOX_LEASE_BYTES} bytes",
        )
    return encoded.ljust(MAILBOX_LEASE_BYTES, b" ")


def write_lease(
    plan_dir: str | Path,
    supervisor_id: str,
    *,
    state_dir: str | Path | None = None,
    updated_at: str | None = None,
    run_id: str | None = None,
    projection_seq: int | None = None,
    unanswered_questions: int | None = None,
    latest_unanswered_question_id: str | None = None,
    latest_unanswered_question_created_at: str | None = None,
) -> MailboxLease:
    """Create or renew the fixed-size supervisor lease under an exclusive lock."""
    if not _is_uuid(supervisor_id):
        raise MailboxError(LEASE_INVALID_SUPERVISOR_ID)
    resolved_updated_at = _utc_now_iso() if updated_at is None else updated_at
    if _parse_timestamp(resolved_updated_at, require_utc=True) is None:
        raise MailboxError(LEASE_INVALID_UPDATED_AT)
    projection_values = (
        projection_seq,
        unanswered_questions,
        latest_unanswered_question_id,
        latest_unanswered_question_created_at,
    )
    if run_id is None:
        if any(value is not None for value in projection_values):
            raise MailboxError(LEASE_INCOMPLETE_QUESTION_PROJECTION)
    else:
        if not isinstance(run_id, str) or run_id.strip() == "":
            raise MailboxError(LEASE_INVALID_RUN_ID)
        if (
            isinstance(projection_seq, bool)
            or not isinstance(projection_seq, int)
            or projection_seq < 0
        ):
            raise MailboxError(LEASE_INVALID_PROJECTION_SEQ)
        if (
            isinstance(unanswered_questions, bool)
            or not isinstance(unanswered_questions, int)
            or unanswered_questions < 0
        ):
            raise MailboxError(LEASE_INVALID_UNANSWERED_QUESTIONS)
        if unanswered_questions == 0:
            if (
                latest_unanswered_question_id is not None
                or latest_unanswered_question_created_at is not None
            ):
                raise MailboxError(LEASE_INCOMPLETE_QUESTION_PROJECTION)
        else:
            assert isinstance(projection_seq, int)
            if projection_seq == 0:
                raise MailboxError(LEASE_INVALID_PROJECTION_SEQ)
            if not _is_uuid(latest_unanswered_question_id):
                raise MailboxError(LEASE_INVALID_LATEST_QUESTION_ID)
            if (
                _parse_timestamp(
                    latest_unanswered_question_created_at,
                    require_utc=True,
                )
                is None
            ):
                raise MailboxError(LEASE_INVALID_LATEST_QUESTION_CREATED_AT)
    lease = MailboxLease(
        supervisor_id=supervisor_id,
        updated_at=resolved_updated_at,
        run_id=run_id,
        projection_seq=projection_seq,
        unanswered_questions=unanswered_questions,
        latest_unanswered_question_id=latest_unanswered_question_id,
        latest_unanswered_question_created_at=(latest_unanswered_question_created_at),
    )
    data = _lease_bytes(lease)
    lease_path = lease_path_for_plan(plan_dir, state_dir)
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_WRITE,
            str(error),
            path=lease_path,
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
        total = 0
        while total < len(data):
            written = os.write(fd, data[total:])
            if written <= 0:
                raise OSError("short lease write")
            total += written
        os.ftruncate(fd, MAILBOX_LEASE_BYTES)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_WRITE,
            str(error),
            path=lease_path,
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return lease


def _invalid_lease_view(
    lease_path: Path,
    reason: str,
    *,
    lease: MailboxLease | None = None,
) -> MailboxLeaseView:
    return MailboxLeaseView(
        lease_path=lease_path,
        lease=lease,
        fresh=False,
        reason=reason,
    )


def read_lease(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    max_age_seconds: float = MAILBOX_LEASE_FRESH_SECONDS,
    now: datetime | None = None,
) -> MailboxLeaseView:
    """Read a supervisor lease; corruption, expiry, and future times are stale."""
    max_age = _validate_lease_age(max_age_seconds)
    resolved_now = _normalize_now(now)
    lease_path = lease_path_for_plan(plan_dir, state_dir)
    try:
        fd = os.open(lease_path, os.O_RDONLY)
    except FileNotFoundError:
        return _invalid_lease_view(lease_path, LEASE_MISSING)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_READ,
            str(error),
            path=lease_path,
        ) from error
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        data = _read_locked(fd)
    except OSError as error:
        raise MailboxError(
            ERROR_LEASE_READ,
            str(error),
            path=lease_path,
        ) from error
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    if len(data) != MAILBOX_LEASE_BYTES:
        return _invalid_lease_view(lease_path, LEASE_INVALID_SIZE)
    stripped = data.rstrip(b" \t\r\n\0")
    if not stripped:
        return _invalid_lease_view(lease_path, LEASE_EMPTY)
    try:
        payload = json.loads(stripped.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return _invalid_lease_view(lease_path, LEASE_INVALID_JSON)
    if not isinstance(payload, dict):
        return _invalid_lease_view(lease_path, LEASE_NOT_OBJECT)
    version = payload.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in MAILBOX_LEASE_SUPPORTED_VERSIONS
    ):
        return _invalid_lease_view(lease_path, LEASE_UNSUPPORTED_VERSION)
    supervisor_id = payload.get("supervisor_id")
    if not _is_uuid(supervisor_id):
        return _invalid_lease_view(lease_path, LEASE_INVALID_SUPERVISOR_ID)
    updated_at = payload.get("updated_at")
    updated = _parse_timestamp(updated_at, require_utc=True)
    if updated is None:
        return _invalid_lease_view(lease_path, LEASE_INVALID_UPDATED_AT)
    assert isinstance(supervisor_id, str)
    assert isinstance(updated_at, str)
    run_id: str | None = None
    projection_seq: int | None = None
    unanswered_questions: int | None = None
    latest_unanswered_question_id: str | None = None
    latest_unanswered_question_created_at: str | None = None
    if version >= 2 and "run_id" in payload:
        run_id_value = payload.get("run_id")
        if not isinstance(run_id_value, str) or run_id_value.strip() == "":
            return _invalid_lease_view(lease_path, LEASE_INVALID_RUN_ID)
        projection_seq_value = payload.get("projection_seq")
        if (
            isinstance(projection_seq_value, bool)
            or not isinstance(projection_seq_value, int)
            or projection_seq_value < 0
        ):
            return _invalid_lease_view(lease_path, LEASE_INVALID_PROJECTION_SEQ)
        unanswered_value = payload.get("unanswered_questions")
        if (
            isinstance(unanswered_value, bool)
            or not isinstance(unanswered_value, int)
            or unanswered_value < 0
        ):
            return _invalid_lease_view(lease_path, LEASE_INVALID_UNANSWERED_QUESTIONS)
        latest_id_value = payload.get("latest_unanswered_question_id")
        latest_created_value = payload.get("latest_unanswered_question_created_at")
        if unanswered_value == 0:
            if latest_id_value is not None or latest_created_value is not None:
                return _invalid_lease_view(
                    lease_path, LEASE_INCOMPLETE_QUESTION_PROJECTION
                )
        else:
            if projection_seq_value == 0:
                return _invalid_lease_view(lease_path, LEASE_INVALID_PROJECTION_SEQ)
            if not _is_uuid(latest_id_value):
                return _invalid_lease_view(lease_path, LEASE_INVALID_LATEST_QUESTION_ID)
            if _parse_timestamp(latest_created_value, require_utc=True) is None:
                return _invalid_lease_view(
                    lease_path,
                    LEASE_INVALID_LATEST_QUESTION_CREATED_AT,
                )
            assert isinstance(latest_id_value, str)
            assert isinstance(latest_created_value, str)
            latest_unanswered_question_id = latest_id_value
            latest_unanswered_question_created_at = latest_created_value
        run_id = run_id_value
        projection_seq = projection_seq_value
        unanswered_questions = unanswered_value
    elif version >= 2 and any(
        field in payload
        for field in (
            "projection_seq",
            "unanswered_questions",
            "latest_unanswered_question_id",
            "latest_unanswered_question_created_at",
        )
    ):
        return _invalid_lease_view(lease_path, LEASE_INCOMPLETE_QUESTION_PROJECTION)
    lease = MailboxLease(
        supervisor_id=supervisor_id,
        updated_at=updated_at,
        run_id=run_id,
        projection_seq=projection_seq,
        unanswered_questions=unanswered_questions,
        latest_unanswered_question_id=latest_unanswered_question_id,
        latest_unanswered_question_created_at=(latest_unanswered_question_created_at),
        version=version,
    )
    updated_utc = updated.astimezone(UTC)
    if updated_utc > resolved_now:
        return _invalid_lease_view(
            lease_path,
            LEASE_FUTURE_UPDATED_AT,
            lease=lease,
        )
    age_seconds = (resolved_now - updated_utc).total_seconds()
    if age_seconds > max_age:
        return _invalid_lease_view(
            lease_path,
            LEASE_EXPIRED,
            lease=lease,
        )
    return MailboxLeaseView(
        lease_path=lease_path,
        lease=lease,
        fresh=True,
    )
