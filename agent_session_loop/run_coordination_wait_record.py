"""The durable waiting-for-reply record and the lease evidence of a failing wait."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import (
    clock,
    mailbox_models,
    plans_payloads,
    plans_session_ids,
)


@dataclass(frozen=True)
class WaitingForReply:
    """The compact persisted identity and deadline record for a live wait.

    Frozen because the two deadlines are the only mutable part and they move
    only through :meth:`rebased`, which returns a new record: a wait that
    re-anchors its clock produces a new value rather than editing the one a
    concurrent status reader may be rendering.
    """

    question_id: str
    session_id: str
    turn_index: int
    started_at: datetime
    base_deadline: datetime
    unsupervised_deadline_at: datetime
    unsupervised_ceiling_seconds: float
    last_lease_activity_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question_id, str) or not self.question_id:
            raise ValueError("question_id must be a non-empty string")
        plans_session_ids.parse_session_id(self.session_id)
        if (
            isinstance(self.turn_index, bool)
            or not isinstance(self.turn_index, int)
            or self.turn_index < 1
        ):
            raise ValueError("turn_index must be a positive integer")
        moments: tuple[tuple[str, datetime], ...] = (
            ("started_at", self.started_at),
            ("base_deadline", self.base_deadline),
            ("unsupervised_deadline_at", self.unsupervised_deadline_at),
        )
        if self.last_lease_activity_at is not None:
            moments += (("last_lease_activity_at", self.last_lease_activity_at),)
        for name, moment in moments:
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.base_deadline < self.started_at:
            raise ValueError("base_deadline must not precede started_at")
        ceiling = self.unsupervised_ceiling_seconds
        if (
            isinstance(ceiling, bool)
            or not isinstance(ceiling, (int, float))
            or not math.isfinite(ceiling)
            or ceiling <= 0
        ):
            raise ValueError(
                "unsupervised_ceiling_seconds must be a finite positive number"
            )

    def to_payload(self) -> dict[str, Any]:
        """Render the durable mapping ``payload["waiting_for_reply"]`` holds.

        The two deadlines no longer share an anchor: the base ratchets off the
        last lease probe while the ceiling rolls off the last *fresh* one, so a
        supervised wait legitimately holds a base beyond the current ceiling.
        ``unsupervised_deadline_at`` is the ceiling as of the last anchor the
        runner persisted; a reader rolls it forward from
        ``last_lease_activity_at`` and the ceiling span rather than treating it
        as terminal.
        """
        return {
            "question_id": self.question_id,
            **plans_payloads.session_identity_payload(self.session_id),
            "turn_index": self.turn_index,
            "started_at": clock.isoformat(self.started_at),
            "base_deadline": clock.isoformat(self.base_deadline),
            "unsupervised_deadline_at": clock.isoformat(self.unsupervised_deadline_at),
            "unsupervised_ceiling_seconds": float(self.unsupervised_ceiling_seconds),
            "last_lease_activity_at": (
                clock.isoformat(self.last_lease_activity_at)
                if self.last_lease_activity_at is not None
                else None
            ),
        }

    def rebased(
        self, *, base_deadline: datetime, unsupervised_deadline_at: datetime
    ) -> WaitingForReply:
        """Return the same wait re-anchored to new wall-clock deadlines."""
        return WaitingForReply(
            question_id=self.question_id,
            session_id=self.session_id,
            turn_index=self.turn_index,
            started_at=self.started_at,
            base_deadline=base_deadline,
            unsupervised_deadline_at=unsupervised_deadline_at,
            unsupervised_ceiling_seconds=self.unsupervised_ceiling_seconds,
            last_lease_activity_at=self.last_lease_activity_at,
        )


def waiting_for_reply_payload(
    *,
    question_id: str,
    session_id: str,
    turn_index: int,
    started_at: datetime,
    base_deadline: datetime,
    unsupervised_deadline_at: datetime,
    unsupervised_ceiling_seconds: float,
    last_lease_activity_at: datetime | None,
) -> dict[str, Any]:
    """Build the compact persisted identity and deadline record for a live wait."""
    return WaitingForReply(
        question_id=question_id,
        session_id=session_id,
        turn_index=turn_index,
        started_at=started_at,
        base_deadline=base_deadline,
        unsupervised_deadline_at=unsupervised_deadline_at,
        unsupervised_ceiling_seconds=unsupervised_ceiling_seconds,
        last_lease_activity_at=last_lease_activity_at,
    ).to_payload()


@dataclass(frozen=True)
class LeaseEvidence:
    """The lease and clock skew a failing wait saw, so the exit is attributable.

    A frozen record rather than the loose mapping this used to be: every field
    here reaches a durable ``wait-cleared`` event, and a typo in one key made a
    supervisor-loss exit indistinguishable from an unattributed one.
    """

    #: Whether a lease was read at all. The three lease fields below are all
    #: independently optional on a lease that *was* read, so their emptiness
    #: cannot stand in for "no lease": a probe that returned an unparseable
    #: lease is evidence, and an absent probe is not.
    lease_observed: bool = False
    lease_updated_at: str | None = None
    lease_reason: str | None = None
    lease_supervisor_id: str | None = None
    suspend_skew_seconds: float | None = None
    suspend_lease_grace_seconds: float | None = None
    unsupervised_seconds: float | None = None

    @classmethod
    def observed(
        cls,
        lease: mailbox_models.MailboxLeaseView | None,
        *,
        suspend_skew_seconds: float = 0.0,
        suspend_lease_grace_seconds: float = 0.0,
        unsupervised_seconds: float | None = None,
    ) -> LeaseEvidence:
        """Capture what one wait saw, dropping the quantities it never had.

        A zero suspend skew or grace is the absence of a suspend rather than a
        measurement of one, so both are normalized to ``None`` here — once,
        where the meaning is known, instead of at each of the six terminal
        exits that report them.
        """
        return cls(
            lease_observed=lease is not None,
            lease_updated_at=None if lease is None else lease.updated_at,
            lease_reason=None if lease is None else lease.reason,
            lease_supervisor_id=None if lease is None else lease.supervisor_id,
            suspend_skew_seconds=(
                suspend_skew_seconds if suspend_skew_seconds > 0.0 else None
            ),
            suspend_lease_grace_seconds=(
                suspend_lease_grace_seconds
                if suspend_lease_grace_seconds > 0.0
                else None
            ),
            unsupervised_seconds=unsupervised_seconds,
        )

    def to_payload(self) -> dict[str, str | float | None]:
        """Render only the fields that were actually observed."""
        evidence: dict[str, str | float | None] = {}
        if self.lease_observed:
            evidence["lease_updated_at"] = self.lease_updated_at
            evidence["lease_reason"] = self.lease_reason
            evidence["lease_supervisor_id"] = self.lease_supervisor_id
        for key, value in (
            ("suspend_skew_seconds", self.suspend_skew_seconds),
            ("suspend_lease_grace_seconds", self.suspend_lease_grace_seconds),
            ("unsupervised_seconds", self.unsupervised_seconds),
        ):
            if value is not None:
                evidence[key] = value
        return evidence
