"""Bounded-wait settings, result records, and the supervision cursor."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import (
    mailbox_models,
    qna,
    run_coordination_constants,
    run_coordination_wait_record,
)


def finite_positive_env(
    name: str,
    default: float,
    *,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Read a positive finite float override, falling back on anything else."""
    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        return default
    try:
        value = float(raw.strip())
    except AttributeError, ValueError:
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


@dataclass(frozen=True)
class MailboxWaitSettings:
    base_seconds: float
    lease_extension_seconds: float
    unsupervised_ceiling_seconds: float
    poll_seconds: float
    io_grace_seconds: float


@dataclass(frozen=True)
class PreparedMailboxWait:
    context: qna.QnAContext
    question: mailbox_models.MailboxMessage
    wait_start: mailbox_models.MailboxMessage
    waiting_for_reply: dict[str, Any]
    pending_outbox: int
    settings: MailboxWaitSettings
    started_monotonic: float


@dataclass(frozen=True)
class MailboxWaitResult:
    reason: str
    pending_outbox: int | None
    reply: mailbox_models.MailboxMessage | None = None
    resolution: mailbox_models.MailboxMessage | None = None
    lease_updated_at: str | None = None
    lease_reason: str | None = None
    lease_supervisor_id: str | None = None
    suspend_skew_seconds: float | None = None
    suspend_lease_grace_seconds: float | None = None
    unsupervised_seconds: float | None = None

    @classmethod
    def unclosed(
        cls,
        reason: str,
        *,
        pending_outbox: int | None,
        evidence: run_coordination_wait_record.LeaseEvidence,
    ) -> MailboxWaitResult:
        """Build the result of a wait that ended without a closure.

        Every such ending is attributable, so the lease and clock evidence is
        threaded through as a typed record rather than splatted from a loose
        mapping: the durable ``wait-cleared`` event is built out of these
        fields, and a mistyped one there is invisible until an operator is
        reading a supervisor-loss exit that names no supervisor.
        """
        return cls(
            reason=reason,
            pending_outbox=pending_outbox,
            lease_updated_at=evidence.lease_updated_at,
            lease_reason=evidence.lease_reason,
            lease_supervisor_id=evidence.lease_supervisor_id,
            suspend_skew_seconds=evidence.suspend_skew_seconds,
            suspend_lease_grace_seconds=evidence.suspend_lease_grace_seconds,
            unsupervised_seconds=evidence.unsupervised_seconds,
        )

    @property
    def closure(self) -> mailbox_models.MailboxMessage | None:
        """The message that ended the wait, whichever kind it was."""
        return self.reply if self.reply is not None else self.resolution

    @property
    def closure_id(self) -> str | None:
        closure = self.closure
        return None if closure is None else closure.id


class MailboxWaitUnavailable(Exception):
    """Internal control flow for a wait that must degrade to a real pause."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class SupervisionCursor:
    """The lease and clock ratchet one bounded wait advances.

    The wait's four coupled quantities used to be loose locals in a 200-line
    loop, where the ordering between them was whatever the branch structure
    happened to impose. They are declared together here because the ordering is
    the invariant:

    * ``last_fresh_lease`` is the only anchor of the unsupervised ceiling, so a
      fresh lease rolls the ceiling forward and a stale one never does.
    * ``active_deadline`` ratchets forward only on a *fresh* lease; a stale
      lease inside the post-suspend grace proves nothing and moves nothing.
    * A detected suspend restarts ``last_fresh_lease`` at the wake instant,
      because nobody could have proven liveness while the machine slept.
    * The grace bounds only how long a stale lease is tolerated. It never
      extends the ceiling.
    """

    settings: MailboxWaitSettings
    started: float
    monotonic: Callable[[], float]
    wall_clock: Callable[[], datetime]
    active_deadline: float = field(init=False)
    last_fresh_lease: float = field(init=False)
    lease: mailbox_models.MailboxLeaseView | None = None
    lease_grace_until: float | None = None
    suspend_skew_seconds: float = 0.0
    suspend_lease_grace_seconds: float = 0.0
    _last_skew_monotonic: float = field(init=False)
    _last_skew_wall: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.active_deadline = self.started + self.settings.base_seconds
        # The wait begins where supervision was last known, so a wait that never
        # probes a lease still measures its unsupervised stretch from a real
        # instant rather than from nothing.
        self.last_fresh_lease = self.started
        self._last_skew_monotonic = self.monotonic()
        self._last_skew_wall = self.wall_clock()

    def unsupervised_elapsed(self) -> float:
        return max(0.0, self.monotonic() - self.last_fresh_lease)

    def unsupervised_deadline(self) -> float:
        """The instant an *unwatched* wait ends.

        The ceiling bounds unsupervised time, so it rolls forward with every
        lease that proves a supervisor alive: a watched wait has no terminal
        instant, while an unwatched one ends a full ceiling after the last
        proof.
        """
        return self.last_fresh_lease + self.settings.unsupervised_ceiling_seconds

    def observe_clock(self) -> tuple[datetime, datetime] | None:
        """Detect a suspend and, if there was one, return the rebased deadlines.

        The monotonic clock freezes across system sleep while the wall clock
        keeps running, so wall time gaining more than the skew threshold on
        monotonic time since the previous iteration means the process was
        suspended for ~skew seconds. The caller re-expresses the persisted
        wall-clock deadlines around the surviving monotonic budget so status
        tooling keeps reporting this wait live; the active deadline itself is
        deliberately not extended.
        """
        skew_monotonic = self.monotonic()
        skew_wall = self.wall_clock()
        skew = (skew_wall - self._last_skew_wall).total_seconds() - (
            skew_monotonic - self._last_skew_monotonic
        )
        self._last_skew_monotonic = skew_monotonic
        self._last_skew_wall = skew_wall
        if skew <= run_coordination_constants.MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS:
            return None
        self.suspend_skew_seconds += skew
        self.suspend_lease_grace_seconds = (
            run_coordination_constants.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS
        )
        self.lease_grace_until = (
            skew_monotonic
            + run_coordination_constants.MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS
        )
        # Suspended time is not supervision loss: nobody could have proven
        # liveness while the machine slept, so the unsupervised stretch
        # restarts at the wake instant.
        self.last_fresh_lease = skew_monotonic
        return (
            skew_wall
            + timedelta(seconds=max(0.0, self.active_deadline - skew_monotonic)),
            skew_wall
            + timedelta(
                seconds=max(0.0, self.unsupervised_deadline() - skew_monotonic)
            ),
        )

    def observe_lease(self, lease: mailbox_models.MailboxLeaseView, now: float) -> str:
        """Fold one lease probe in and name what it proved.

        Returns ``"fresh"`` when supervision was re-proven, ``"lost"`` when a
        stale lease outside any grace ends the wait, and ``"grace"`` when a
        stale lease landed inside the post-suspend window and so proves nothing
        either way.
        """
        self.lease = lease
        if lease.fresh:
            # A fresh lease is proof of supervision, so it moves the ceiling
            # with it; clamping the extension against a bound that just rolled
            # away would be meaningless.
            self.last_fresh_lease = now
            self.active_deadline = now + self.settings.lease_extension_seconds
            return "fresh"
        if self.lease_grace_until is None or now >= self.lease_grace_until:
            return "lost"
        # A non-fresh lease inside the post-suspend grace neither ends the
        # wait nor ratchets the active deadline: it proves nothing, so the
        # wait keeps advancing toward the ceiling while the supervisor
        # re-proves liveness.
        return "grace"

    def evidence(self) -> run_coordination_wait_record.LeaseEvidence:
        """Capture the lease and skew evidence a terminal wait reason carries."""
        return run_coordination_wait_record.LeaseEvidence.observed(
            self.lease,
            suspend_skew_seconds=self.suspend_skew_seconds,
            suspend_lease_grace_seconds=self.suspend_lease_grace_seconds,
            unsupervised_seconds=self.unsupervised_elapsed(),
        )


def mailbox_reply_wait_settings(
    environ: Mapping[str, str] | None = None,
) -> MailboxWaitSettings:
    """Resolve bounded wait timings, falling back safely on bad overrides."""
    base_seconds = finite_positive_env(
        run_coordination_constants.MAILBOX_REPLY_WAIT_BASE_ENV_VAR,
        run_coordination_constants.MAILBOX_REPLY_WAIT_BASE_SECONDS,
        environ=environ,
    )
    lease_extension_seconds = finite_positive_env(
        run_coordination_constants.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR,
        run_coordination_constants.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS,
        environ=environ,
    )
    unsupervised_ceiling_seconds = finite_positive_env(
        run_coordination_constants.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR,
        run_coordination_constants.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS,
        environ=environ,
    )
    poll_seconds = finite_positive_env(
        run_coordination_constants.MAILBOX_REPLY_WAIT_POLL_ENV_VAR,
        run_coordination_constants.MAILBOX_REPLY_WAIT_POLL_SECONDS,
        environ=environ,
    )
    io_grace_seconds = finite_positive_env(
        run_coordination_constants.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR,
        run_coordination_constants.MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS,
        environ=environ,
    )
    # A malformed cross-field override must not move the base deadline beyond
    # the unsupervised ceiling. Clamp the base instead of raising the ceiling
    # so an unwatched wait remains bounded under every environment value.
    base_seconds = min(base_seconds, unsupervised_ceiling_seconds)
    return MailboxWaitSettings(
        base_seconds=base_seconds,
        lease_extension_seconds=lease_extension_seconds,
        unsupervised_ceiling_seconds=unsupervised_ceiling_seconds,
        poll_seconds=poll_seconds,
        io_grace_seconds=io_grace_seconds,
    )
