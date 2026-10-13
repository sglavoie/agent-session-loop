"""Session identity and snapshot payload helpers."""

from __future__ import annotations

from typing import Any

from .plans_models import (
    PlanSnapshot,
    SessionStatus,
    StatusValue,
)
from .plans_session_ids import (
    parse_session_id,
)

#: The kinds of work a session file can describe. A session either implements
#: the plan's next increment or independently reviews what earlier sessions
#: produced; nothing else is a session.
SESSION_KIND_IMPLEMENTATION = "implementation"


SESSION_KIND_REVIEW = "review"


SESSION_KINDS: tuple[str, ...] = (
    SESSION_KIND_IMPLEMENTATION,
    SESSION_KIND_REVIEW,
)


def session_number_alias(session_id: str) -> int | None:
    """The integer form of ``session_id``, or ``None`` for a split child."""
    parsed = parse_session_id(session_id)
    return parsed.as_int() if parsed.is_integer else None


def session_compat_value(session_id: str) -> int | str:
    """The ``session`` field's value: an int where one exists, else the id.

    Records written before split ids existed carried an integer, and consumers
    still read that field, so a whole-numbered session keeps publishing one.
    """
    number = session_number_alias(session_id)
    return number if number is not None else session_id


def session_label(session_id: str) -> str:
    return f"session {session_id}"


def session_path_label(session_id: str) -> str:
    return f"session-{parse_session_id(session_id).value}"


def session_identity_payload(session_id: str) -> dict[str, Any]:
    """The identity block every session-scoped record carries."""
    payload: dict[str, Any] = {"session": session_compat_value(session_id)}
    payload["session_id"] = session_id
    number = session_number_alias(session_id)
    if number is not None:
        payload["session_number"] = number
    return payload


def session_details_payload(session_id: str) -> dict[str, Any]:
    number = session_number_alias(session_id)
    if number is not None:
        return {"session": number}
    return {"session": session_id, "session_id": session_id}


def session_status_change_payload(
    session_id: str,
    *,
    before: StatusValue | None,
    after: StatusValue | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "before": before,
        "after": after,
    }
    number = session_number_alias(session_id)
    if number is None:
        payload["id"] = session_id
    else:
        payload["number"] = number
    return payload


def status_map(snapshot: PlanSnapshot) -> dict[str, StatusValue]:
    return snapshot.status_by_id


def status_counts(snapshot: PlanSnapshot) -> dict[str, int]:
    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    return counts


def session_payload(session: SessionStatus) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session.id,
        "status": session.status,
        "path": str(session.path),
        "line_number": session.line_number,
        "raw_value": session.raw_value,
    }
    number = session_number_alias(session.id)
    if number is not None:
        payload["number"] = number
    return payload


def sessions_with_status(
    snapshot: PlanSnapshot,
    status: StatusValue,
) -> list[dict[str, Any]]:
    return [
        session_payload(session)
        for session in snapshot.sessions
        if session.status == status
    ]


def snapshot_payload(snapshot: PlanSnapshot) -> dict[str, Any]:
    """The serialized projection of a plan snapshot a record publishes."""
    next_pending = snapshot.next_pending
    next_pending_id = next_pending.id if next_pending is not None else None
    return {
        "plan_dir": str(snapshot.plan_dir),
        "plan_hash": snapshot.plan_hash,
        "content_sha256": snapshot.content_sha256,
        "path_hash": snapshot.path_hash,
        "counts": status_counts(snapshot),
        "next_pending": (
            session_compat_value(next_pending_id)
            if next_pending_id is not None
            else None
        ),
        "next_pending_id": next_pending_id,
        "sessions": [session_payload(session) for session in snapshot.sessions],
    }


def status_changes(
    before: PlanSnapshot,
    after: PlanSnapshot,
) -> tuple[dict[str, Any], ...]:
    """Every session whose handoff status differs between two snapshots."""
    before_statuses = status_map(before)
    after_statuses = status_map(after)
    changes: list[dict[str, Any]] = []
    for session_id in sorted(
        set(before_statuses) | set(after_statuses),
        key=lambda value: parse_session_id(value).sort_key,
    ):
        before_value = before_statuses.get(session_id)
        after_value = after_statuses.get(session_id)
        if before_value != after_value:
            changes.append(
                session_status_change_payload(
                    session_id,
                    before=before_value,
                    after=after_value,
                )
            )
    return tuple(changes)


def pending_completed(
    before: PlanSnapshot,
    after: PlanSnapshot,
) -> tuple[str, ...]:
    """The sessions that moved from pending to complete between snapshots."""
    before_statuses = status_map(before)
    after_statuses = status_map(after)
    return tuple(
        session_id
        for session_id in sorted(
            before_statuses,
            key=lambda value: parse_session_id(value).sort_key,
        )
        if before_statuses[session_id] == "pending"
        and after_statuses.get(session_id) == "complete"
    )
