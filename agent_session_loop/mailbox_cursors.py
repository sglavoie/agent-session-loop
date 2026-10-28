"""Mailbox file locations and per-run supervision cursors."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from . import durability, state
from .mailbox_constants import (
    MAILBOX_FILE_NAME,
    MAILBOX_LEASE_FILE_NAME,
    MAILBOX_MAX_INTEGER,
    MAILBOX_SUPERVISION_CURSOR_FILE_TEMPLATE,
    MAILBOX_SUPERVISION_CURSOR_GLOB,
    MAILBOX_SUPERVISION_CURSOR_MAX_AGE_SECONDS,
    MAILBOX_SUPERVISION_CURSOR_VERSION,
)
from .mailbox_validation import (
    _is_uuid,
    _parse_timestamp,
    _utc_now,
    _utc_now_iso,
    _validate_sequence,
)


def mailbox_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical mailbox log path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / MAILBOX_FILE_NAME


def lease_path_for_plan(
    plan_dir: str | Path,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the canonical supervisor lease path for ``plan_dir``."""
    return state.plan_state_dir(plan_dir, state_dir) / MAILBOX_LEASE_FILE_NAME


def supervision_cursor_path_for_plan(
    plan_dir: str | Path,
    supervision_id: str,
    state_dir: str | Path | None = None,
) -> Path:
    """Return the cursor path one supervising launch owns for ``plan_dir``.

    One file per launch identity makes cross-supervisor clobbering structurally
    impossible, so the cursor needs no lock of its own.
    """
    if not _is_uuid(supervision_id):
        raise ValueError("supervision_id must be a UUID")
    return state.plan_state_dir(plan_dir, state_dir) / (
        MAILBOX_SUPERVISION_CURSOR_FILE_TEMPLATE.format(supervision_id=supervision_id)
    )


def _read_supervision_cursor_payload(
    plan_dir: str | Path,
    supervision_id: str,
    state_dir: str | Path | None,
) -> dict[str, Any] | None:
    """Return one launch's stored cursor payload, or None when it is unusable.

    Every failure mode — missing, unreadable, unparseable, wrong version, or a
    mismatched launch — yields None. Field-level validation belongs to each
    reader, so one bad field never discards another's usable value.
    """
    try:
        cursor_path = supervision_cursor_path_for_plan(
            plan_dir,
            supervision_id,
            state_dir,
        )
        payload = json.loads(cursor_path.read_text(encoding="utf-8"))
    except OSError, ValueError, RecursionError:
        # RecursionError is not a ValueError: pathologically nested JSON raises
        # it from the decoder instead of a decode error.
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != MAILBOX_SUPERVISION_CURSOR_VERSION:
        return None
    if payload.get("supervision_id") != supervision_id:
        return None
    return payload


def _cursor_next_since(payload: dict[str, Any] | None) -> int:
    if payload is None:
        return 0
    next_since = payload.get("next_since")
    if (
        isinstance(next_since, bool)
        or not isinstance(next_since, int)
        or next_since < 0
        or next_since > MAILBOX_MAX_INTEGER
    ):
        return 0
    return next_since


def read_supervision_cursor(
    plan_dir: str | Path,
    supervision_id: str,
    *,
    state_dir: str | Path | None = None,
) -> int:
    """Return one launch's stored cursor, or 0 when it is unusable.

    Every failure mode — missing, unreadable, unparseable, wrong version,
    mismatched launch, or an out-of-range sequence — yields 0 because replaying
    bounded history is safe while skipping records is not.
    """
    return _cursor_next_since(
        _read_supervision_cursor_payload(plan_dir, supervision_id, state_dir)
    )


def read_supervision_first_seen_at(
    plan_dir: str | Path,
    supervision_id: str,
    *,
    state_dir: str | Path | None = None,
) -> datetime | None:
    """Return when this launch was first polled, or None when unrecorded.

    None means "no usable anchor on disk yet", which is also what an older
    cursor written before the field existed reports, so the caller writes one
    now. A future anchor cannot measure elapsed grace after the clock moves
    backward, and would otherwise postpone a lost-helper verdict indefinitely.
    """
    payload = _read_supervision_cursor_payload(plan_dir, supervision_id, state_dir)
    if payload is None:
        return None
    first_seen_at = _parse_timestamp(payload.get("first_seen_at"))
    if first_seen_at is not None and first_seen_at > _utc_now():
        return None
    return first_seen_at


def write_supervision_cursor(
    plan_dir: str | Path,
    supervision_id: str,
    next_since: int,
    *,
    state_dir: str | Path | None = None,
    first_seen_at: datetime | None = None,
) -> None:
    """Persist a never-regressing cursor for one supervising launch.

    ``first_seen_at`` is merged the way ``next_since`` is, but toward the
    earliest value: this file is rebuilt from scratch on every write, so an
    anchor that is not carried forward is an anchor that silently resets and
    postpones every grace window that depends on it.

    A failed write is swallowed: the worst outcome is one replayed batch.
    """
    _validate_sequence(next_since, field_name="next_since")
    stored = _read_supervision_cursor_payload(plan_dir, supervision_id, state_dir)
    stored_first_seen_at = (
        None if stored is None else _parse_timestamp(stored.get("first_seen_at"))
    )
    earliest_first_seen_at = min(
        (
            moment
            for moment in (stored_first_seen_at, first_seen_at)
            if moment is not None
        ),
        default=None,
    )
    payload: dict[str, Any] = {
        "next_since": max(next_since, _cursor_next_since(stored)),
        "supervision_id": supervision_id,
        "updated_at": _utc_now_iso(),
        "version": MAILBOX_SUPERVISION_CURSOR_VERSION,
    }
    if earliest_first_seen_at is not None:
        payload["first_seen_at"] = earliest_first_seen_at.isoformat(
            timespec="microseconds"
        )
    cursor_path = supervision_cursor_path_for_plan(
        plan_dir,
        supervision_id,
        state_dir,
    )
    try:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        # 0o600 like the mailbox and lease: every mailbox state file is private.
        durability.replace_bytes(
            cursor_path,
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n",
            mode=0o600,
            permissions=0o600,
        )
    except OSError:
        pass


def prune_supervision_cursors(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    max_age_seconds: float = MAILBOX_SUPERVISION_CURSOR_MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Best-effort removal of cursors left behind by long-finished launches."""
    cutoff = (_utc_now().timestamp() if now is None else now) - max_age_seconds
    removed = 0
    try:
        candidates = sorted(
            state.plan_state_dir(plan_dir, state_dir).glob(
                MAILBOX_SUPERVISION_CURSOR_GLOB
            )
        )
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
        except OSError:
            continue
        removed += 1
    return removed
