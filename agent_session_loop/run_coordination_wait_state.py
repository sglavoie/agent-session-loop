"""Sole writer of ``payload["waiting_for_reply"]`` and its mailbox events."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    events,
    mailbox_vocabulary,
    plans_payloads,
    run_coordination_constants,
    run_coordination_seams,
    run_coordination_wait_models,
)


class WaitStateStore:
    """Sole writer of ``payload["waiting_for_reply"]`` and its mailbox events.

    Every mutation below is a compare-and-set against the ``question_id`` that
    opened the wait, taken under ``persistence.lock``. That is what makes the
    late writer harmless: a lease probe or deadline rebase whose wait has
    already been cleared finds no matching slot and returns, instead of
    resurrecting a wait the loop has moved past.
    """

    def __init__(
        self,
        *,
        event_path: Path,
        payload: dict[str, Any],
        persistence: run_coordination_seams.CoordinationPersistence,
    ) -> None:
        self._event_path = event_path
        self._payload = payload
        self._persistence = persistence

    @property
    def payload(self) -> dict[str, Any]:
        """The run payload this store writes into."""
        return self._payload

    def current(self) -> dict[str, Any] | None:
        """Return a copy of the live wait record, or ``None`` when idle."""
        with self._persistence.lock:
            current = self._payload.get("waiting_for_reply")
            return dict(current) if isinstance(current, dict) else None

    def begin(
        self,
        waiting_for_reply: Mapping[str, Any],
        *,
        pending_outbox: int,
    ) -> dict[str, Any]:
        """Persist a live wait and its nonterminal mailbox event atomically."""
        return set_waiting_for_reply(
            self._event_path,
            self._payload,
            waiting_for_reply,
            pending_outbox=pending_outbox,
            persistence=self._persistence,
        )

    def record_lease_activity(self, *, question_id: str, updated_at: str) -> None:
        """Persist a newly observed lease timestamp without emitting an event."""
        record_waiting_lease_activity(
            self._payload,
            question_id=question_id,
            updated_at=updated_at,
            persistence=self._persistence,
        )

    def rebase_deadlines(
        self,
        *,
        question_id: str,
        base_deadline: datetime,
        unsupervised_deadline_at: datetime,
    ) -> None:
        """Re-anchor persisted wall-clock deadlines without emitting an event."""
        rebase_waiting_deadlines(
            self._payload,
            question_id=question_id,
            base_deadline=base_deadline,
            unsupervised_deadline_at=unsupervised_deadline_at,
            persistence=self._persistence,
        )

    def clear(
        self, result: run_coordination_wait_models.MailboxWaitResult | None
    ) -> str:
        """Remove the live wait and publish the outcome it reached.

        ``None`` means the wait's own bookkeeping raised before it produced a
        result: the slot is still cleared, under the reason
        :data:`WAIT_REASON_ERROR`, because leaving a wait record behind would
        make a finished run read as still waiting.
        """
        reason = (
            run_coordination_constants.WAIT_REASON_ERROR
            if result is None
            else result.reason
        )
        resolution = None if result is None else result.resolution
        clear_waiting_for_reply(
            self._event_path,
            self._payload,
            reason=reason,
            pending_outbox=None if result is None else result.pending_outbox,
            lease_updated_at=None if result is None else result.lease_updated_at,
            lease_reason=None if result is None else result.lease_reason,
            lease_supervisor_id=(
                None if result is None else result.lease_supervisor_id
            ),
            suspend_skew_seconds=(
                None if result is None else result.suspend_skew_seconds
            ),
            suspend_lease_grace_seconds=(
                None if result is None else result.suspend_lease_grace_seconds
            ),
            unsupervised_seconds=(
                None if result is None else result.unsupervised_seconds
            ),
            resolution_action=None if resolution is None else resolution.action,
            resolution_message_id=None if resolution is None else resolution.id,
            resolution_seq=None if resolution is None else resolution.seq,
            persistence=self._persistence,
        )
        return reason

    def record_preparation_failed(
        self, *, session_id: str, turn_index: int, reason: str
    ) -> None:
        """Record why a reply-capable pause could not enter a live wait."""
        record_mailbox_reply_wait_preparation_failed(
            self._event_path,
            self._payload,
            session_id=session_id,
            turn_index=turn_index,
            reason=reason,
            persistence=self._persistence,
        )


def set_waiting_for_reply(
    event_path: Path,
    payload: dict[str, Any],
    waiting_for_reply: Mapping[str, Any],
    *,
    pending_outbox: int,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> dict[str, Any]:
    """Persist a live wait and its nonterminal mailbox event atomically."""
    if (
        isinstance(pending_outbox, bool)
        or not isinstance(pending_outbox, int)
        or pending_outbox < 0
    ):
        raise ValueError("pending_outbox must be a non-negative integer")
    waiting = dict(waiting_for_reply)
    with persistence.lock:
        payload["waiting_for_reply"] = waiting
        persistence.append_event(
            event_path,
            events.MAILBOX_EVENT,
            {
                "run_id": payload.get("run_id"),
                **waiting,
                "pending_outbox": pending_outbox,
                "action": events.MAILBOX_ACTION_WAIT_STARTED,
            },
        )
        persistence.write_state(payload)
    return waiting


def clear_waiting_for_reply(
    event_path: Path,
    payload: dict[str, Any],
    *,
    reason: str,
    pending_outbox: int | None,
    lease_updated_at: str | None = None,
    lease_reason: str | None = None,
    lease_supervisor_id: str | None = None,
    suspend_skew_seconds: float | None = None,
    suspend_lease_grace_seconds: float | None = None,
    unsupervised_seconds: float | None = None,
    resolution_action: mailbox_vocabulary.ResolutionAction | None = None,
    resolution_message_id: str | None = None,
    resolution_seq: int | None = None,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Remove live-wait state and make its outcome observable to event readers."""
    if not isinstance(reason, str) or not reason:
        raise ValueError("reason must be a non-empty string")
    if pending_outbox is not None and (
        isinstance(pending_outbox, bool)
        or not isinstance(pending_outbox, int)
        or pending_outbox < 0
    ):
        raise ValueError("pending_outbox must be a non-negative integer or None")
    resolution_values = (
        resolution_action,
        resolution_message_id,
        resolution_seq,
    )
    if any(value is not None for value in resolution_values):
        if reason != run_coordination_constants.WAIT_REASON_RESOLUTION or any(
            value is None for value in resolution_values
        ):
            raise ValueError(
                "resolution metadata must be complete and use reason resolution"
            )
        if resolution_action not in mailbox_vocabulary.RESOLUTION_ACTIONS:
            raise ValueError("resolution_action must be continue or pause")
        if not isinstance(resolution_message_id, str) or not resolution_message_id:
            raise ValueError("resolution_message_id must be a non-empty string")
        if (
            isinstance(resolution_seq, bool)
            or not isinstance(resolution_seq, int)
            or resolution_seq < 1
        ):
            raise ValueError("resolution_seq must be a positive integer")
    elif reason == run_coordination_constants.WAIT_REASON_RESOLUTION:
        raise ValueError("reason resolution requires resolution metadata")
    with persistence.lock:
        waiting = payload.pop("waiting_for_reply", None)
        if isinstance(waiting, dict):
            event_payload = {
                "run_id": payload.get("run_id"),
                **waiting,
                "reason": reason,
                "action": events.MAILBOX_ACTION_WAIT_CLEARED,
            }
            if pending_outbox is not None:
                event_payload["pending_outbox"] = pending_outbox
            for key, value in (
                ("lease_updated_at", lease_updated_at),
                ("lease_reason", lease_reason),
                ("lease_supervisor_id", lease_supervisor_id),
                ("suspend_skew_seconds", suspend_skew_seconds),
                ("suspend_lease_grace_seconds", suspend_lease_grace_seconds),
                ("unsupervised_seconds", unsupervised_seconds),
                ("resolution_action", resolution_action),
                ("resolution_message_id", resolution_message_id),
                ("resolution_seq", resolution_seq),
            ):
                if value is not None:
                    event_payload[key] = value
            persistence.append_event(
                event_path,
                events.MAILBOX_EVENT,
                event_payload,
            )
        persistence.write_state(payload)


def record_mailbox_reply_wait_preparation_failed(
    event_path: Path,
    payload: Mapping[str, Any],
    *,
    session_id: str,
    turn_index: int,
    reason: str,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Record why a reply-capable pause could not enter a live mailbox wait."""
    if not isinstance(reason, str) or not reason:
        raise ValueError("reason must be a non-empty string")
    persistence.append_event(
        event_path,
        events.MAILBOX_EVENT,
        {
            "run_id": payload.get("run_id"),
            **plans_payloads.session_identity_payload(session_id),
            "turn_index": turn_index,
            "reason": reason,
            "action": events.MAILBOX_ACTION_WAIT_PREPARATION_FAILED,
        },
    )


def record_waiting_lease_activity(
    payload: dict[str, Any],
    *,
    question_id: str,
    updated_at: str,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Persist a newly observed lease timestamp without emitting a new event."""
    with persistence.lock:
        current = payload.get("waiting_for_reply")
        if not isinstance(current, dict):
            return
        if current.get("question_id") != question_id:
            return
        if current.get("last_lease_activity_at") == updated_at:
            return
        waiting = dict(current)
        waiting["last_lease_activity_at"] = updated_at
        payload["waiting_for_reply"] = waiting
        persistence.write_state(payload)


def rebase_waiting_deadlines(
    payload: dict[str, Any],
    *,
    question_id: str,
    base_deadline: datetime,
    unsupervised_deadline_at: datetime,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Re-anchor persisted wall-clock deadlines without emitting a new event."""
    for name, moment in (
        ("base_deadline", base_deadline),
        ("unsupervised_deadline_at", unsupervised_deadline_at),
    ):
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
    with persistence.lock:
        current = payload.get("waiting_for_reply")
        if not isinstance(current, dict):
            return
        if current.get("question_id") != question_id:
            return
        rebased = {
            "base_deadline": clock.isoformat(base_deadline),
            "unsupervised_deadline_at": clock.isoformat(unsupervised_deadline_at),
        }
        if all(current.get(key) == value for key, value in rebased.items()):
            return
        waiting = dict(current)
        waiting.update(rebased)
        payload["waiting_for_reply"] = waiting
        persistence.write_state(payload)
