"""The typed boundary between the run loop and everyone talking to it.

A run is never alone. A supervisor answers its questions through the mailbox,
an operator queues control commands from another process, and a terminal audit
grades the ending after the last turn. Each of those is a *coordination*
concern: it crosses a process boundary, it races the run loop, and it commits
its outcome to the same durable records the loop writes. This module owns all
three so the loop is left deciding *whether* to wait, stop, or audit rather
than reimplementing *how* those handshakes stay correct under concurrency.

Three durable slots move here with their owners, and each is now reached only
through the object that owns it rather than by indexing a dictionary the run
loop happens to be holding:

* ``payload["waiting_for_reply"]`` belongs to :class:`WaitStateStore`. Every
  begin, lease-activity stamp, deadline rebase, and clear goes through it, and
  each one is a compare-and-set against the question ID that opened the wait —
  a stale writer for a finished wait is dropped rather than resurrecting it.
* ``payload["control"]`` belongs to :class:`ControlPlane`. External commands,
  keyboard actions, and abort arming all land through one recorder, so an event
  and the ``control`` payload can never disagree about what was requested.
* ``payload["assurance"]`` and its report belong to
  :class:`TerminalAssuranceCoordinator`, which publishes the finalization
  phase, runs the advisory audit, and guarantees the phase is closed even when
  the audit is cancelled.

Nothing here imports the runner. The three collaborators ``run_loop`` owns --
serialized persistence, the AgentMux notification seam, and the compacted state
projection the audit reads -- enter through the :class:`CoordinationPersistence`,
:class:`WaitNotifier`, and ``state_snapshot`` seams, which is what lets this
boundary be typed without a cycle.

Four invariants survive the move and are load-bearing:

* **Lock ownership.** ``CoordinationPersistence.lock`` is the run's reentrant
  mutex and is the outermost lock every durable write below takes. It is never
  held across a mailbox read, a lease read, a sleep, or the audit subprocess.
  :class:`ControlPlane` takes the same lock for the whole
  observe-then-transition sequence of one command, so a concurrent keyboard
  action cannot interleave between a command's receipt and its transition.
* **Ordering.** A wait always emits ``wait-started`` before it polls and
  ``wait-cleared`` after it stops, even when the poll raises; a control command
  always gets its ``control-command-observed`` receipt before the transition it
  causes; the terminal audit always closes its finalization phase, cancelled or
  not.
* **Cancellation.** A cancelled audit is a cancelled *audit*: the run keeps the
  outcome and exit code it had already earned, and the cancellation is recorded
  rather than raised. An abort observed with no turn in flight degrades to
  stop-after-current instead of vanishing.
* **Recovery.** Every closure the wait can observe is checked once more under
  the mailbox's own lock in :func:`finish_mailbox_reply_wait_lifecycle`, so a
  reply that lands in the instant the wait times out is honoured rather than
  lost. A reply that never reached consumption is rolled back by
  :class:`ReplyStatePreserver`.
"""

from __future__ import annotations

import contextlib
import hashlib
import math
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from . import (
    assurance_report,
    clock,
    control_queue,
    durability,
    events,
    guards,
    guidance_delivery,
    harnesses,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_cursors,
    mailbox_leases,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    mailbox_waits,
    pause_policy,
    plans_models,
    plans_payloads,
    plans_session_ids,
    qna,
    run_control,
    supervisor_audit,
)

# ---------------------------------------------------------------------------
# Bounded-wait budgets
# ---------------------------------------------------------------------------

# A runner that receives a reply-capable pause remains live for five minutes.
# An active supervisor lease may extend that deadline one minute at a time.
# The ceiling below bounds *unsupervised* time, not wall clock: it is measured
# from the last lease that proved a supervisor alive, so a watched wait outlives
# it while an unwatched one still ends within thirty minutes. Environment
# overrides are deliberately narrow test seams; invalid, non-finite, or
# non-positive values fall back to these production defaults.
MAILBOX_REPLY_WAIT_BASE_SECONDS = 5 * 60.0
MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS = 60.0
MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS = 30 * 60.0
MAILBOX_REPLY_WAIT_POLL_SECONDS = 2.0
MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS = 10.0
# The wait paces itself on the monotonic clock, which freezes across system
# sleep on macOS, while the persisted deadlines are wall clock. Between two
# poll iterations the wall clock can only run ahead of the monotonic clock by
# scheduler jitter, so drift beyond one whole poll interval plus margin must
# be a suspend (or a clock step): the wait then re-anchors the persisted
# deadlines around its surviving monotonic budget. Suspend detection is
# exercised through the injected clock seams, so unlike the budgets above
# this threshold deliberately has no environment override.
MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS = 5.0
# For a few seconds after wake the on-disk lease is legitimately wall-clock
# ancient: the autopilot has not polled yet, and a lease probe landing in that
# window would kill a healthy wait. After a detected suspend the supervisor
# therefore gets this much *awake* (monotonic) time to re-prove liveness
# before a non-fresh lease ends the wait. It equals one full lease freshness
# window (mailbox_constants.MAILBOX_LEASE_FRESH_SECONDS, 90 seconds) — roughly eighteen
# chances at the autopilot's 5-second poll cadence, the tick that actually
# republishes the projection. Inside the grace a stale lease neither ends the
# wait nor extends the active deadline, and the grace never extends the
# unsupervised ceiling. Like the skew threshold above, this is a detection
# bound rather than an operator budget, exercised through the injected clock
# seams, so it deliberately has no environment override.
MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS = (
    mailbox_constants.MAILBOX_LEASE_FRESH_SECONDS
)
# A question body is the worker's last-message artifact, which is unbounded,
# while the mailbox log is capped at 4 MiB with no rotation — one oversized
# message could consume it. Longer bodies are truncated deterministically to
# this many characters plus a footer naming the on-disk artifact that holds
# the full text. Determinism is load-bearing: a retried wait preparation must
# recompute a byte-identical body or the question-reuse equality check in
# ensure_mailbox_question would degrade the retry to a terminal pause. Like
# the suspend bounds above this is a protocol bound rather than an operator
# budget, so it deliberately has no environment override.
MAILBOX_QUESTION_TEXT_MAX_CHARS = 16000
MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST = "supervisor-lost"
MAILBOX_REPLY_WAIT_PREPARATION_REASONS: tuple[str, ...] = (
    mailbox_constants.LEASE_MISSING,
    mailbox_constants.LEASE_INVALID_SIZE,
    mailbox_constants.LEASE_EMPTY,
    mailbox_constants.LEASE_INVALID_JSON,
    mailbox_constants.LEASE_NOT_OBJECT,
    mailbox_constants.LEASE_UNSUPPORTED_VERSION,
    mailbox_constants.LEASE_INVALID_SUPERVISOR_ID,
    mailbox_constants.LEASE_INVALID_UPDATED_AT,
    mailbox_constants.LEASE_FUTURE_UPDATED_AT,
    mailbox_constants.LEASE_EXPIRED,
    mailbox_constants.ERROR_LEASE_READ,
    mailbox_constants.ERROR_MAILBOX_FULL,
    mailbox_constants.ERROR_MAILBOX_READ,
    mailbox_constants.ERROR_MAILBOX_WRITE,
    *qna.QNA_ERROR_REASONS,
)
MAILBOX_REPLY_WAIT_BASE_ENV_VAR = "ASL_MAILBOX_REPLY_WAIT_BASE_SECONDS"
MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR = (
    "ASL_MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS"
)
MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR = (
    "ASL_MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS"
)
MAILBOX_REPLY_WAIT_POLL_ENV_VAR = "ASL_MAILBOX_REPLY_WAIT_POLL_SECONDS"
MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR = "ASL_MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS"

#: Sentinel for "this wait has never parsed the mailbox", which is distinct
#: from "the change token could not be read". The first forces a parse; the
#: second forces one too, but only because it proves nothing.
MAILBOX_TOKEN_UNSEEN = object()

#: Terminal wait reasons that are *not* an error the caller must degrade on.
WAIT_REASON_REPLY = "reply"
WAIT_REASON_RESOLUTION = "resolution"
WAIT_REASON_TIMEOUT = "timeout"
#: The reason recorded when a wait's own bookkeeping raised before it could
#: report one. It is deliberately not a mailbox reason: nothing was observed.
WAIT_REASON_ERROR = "error"

#: Reason recorded for an abort command that carried none. The value reaches the
#: worker's guidance record and the aborted session's Handoff note, so it has to
#: read as an explanation on its own.
ABORT_TURN_DEFAULT_REASON = "supervisor requested abort"

RUN_CONTROL_EVENT_ACTIONS: dict[str, str] = {
    "request": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
    "cancel": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CANCELED,
    "consumed": events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
    "abort-requested": events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
    # Consumption has two outcomes with one state transition behind them, so
    # the satisfied-by-worker case overrides this default at its call site.
    "abort-consumed": events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
    "tail-enabled": events.RUN_CONTROL_ACTION_TAIL_ENABLED,
    "tail-disabled": events.RUN_CONTROL_ACTION_TAIL_DISABLED,
    "harness-selected": events.RUN_CONTROL_ACTION_HARNESS_SELECTED,
    "implementation-model-updated": (
        events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED
    ),
}
RUN_CONTROL_EVENT_MESSAGES: dict[str, str] = {
    "request": "stop-after-current requested",
    "cancel": "stop-after-current canceled",
    "consumed": "stop-after-current consumed",
    "abort-requested": "turn abort requested",
    "abort-consumed": "turn abort honored",
    "tail-enabled": "live tail enabled",
    "tail-disabled": "live tail disabled",
    "harness-selected": "next harness selected",
    "implementation-model-updated": "implementation model controls updated",
}

#: Assurance states a terminating run still owes an audit for. ``skipped`` is a
#: settled decision to run no checks; anything else is already a verdict.
AUDITABLE_ASSURANCE_STATES = frozenset({"pending", "unavailable"})

#: Assurance states that leave the optional post-run audit unable to speak.
#: ``skipped`` is a settled decision not to audit and is not a degradation.
DEGRADED_ASSURANCE_STATES = frozenset({"unavailable", "partial"})


# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class CoordinationPersistence(Protocol):
    """The run's serialized payload writer, as coordination needs it.

    Declared structurally rather than imported so this module never depends on
    the runner that owns the concrete implementation. ``lock`` is the run's
    reentrant mutex and the outermost lock every durable write below takes;
    it is never held across a mailbox read, a lease read, a sleep, or the audit
    subprocess.
    """

    @property
    def lock(self) -> AbstractContextManager[Any]: ...

    def append_event(
        self, event_path: Path, name: str, payload: dict[str, Any]
    ) -> None: ...

    def write_state(self, payload: dict[str, Any]) -> None: ...


class WaitNotifier(Protocol):
    """The fail-open AgentMux notification seam a live wait drives.

    Both calls are made outside every coordination lock and neither may raise:
    a lifecycle notification is advisory, and losing one must never end a wait
    that a supervisor is still answering.
    """

    def wait_started(self, *, question_id: str, question_text: str | None) -> None: ...

    def wait_ended(
        self, *, question_id: str, wait_reason: str, closure_id: str | None
    ) -> None: ...


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


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


def read_optional_bytes(path: Path) -> bytes | None:
    """Return ``path``'s bytes, or ``None`` when it does not exist."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def restore_optional_bytes(path: Path, payload: bytes | None) -> None:
    """Put ``path`` back to ``payload``, removing it when that was absence."""
    if payload is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    durability.write_bytes_file(path, payload)


# ---------------------------------------------------------------------------
# Waiting-for-reply: the durable record
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Waiting-for-reply: the durable slot's owner
# ---------------------------------------------------------------------------


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
        persistence: CoordinationPersistence,
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

    def clear(self, result: MailboxWaitResult | None) -> str:
        """Remove the live wait and publish the outcome it reached.

        ``None`` means the wait's own bookkeeping raised before it produced a
        result: the slot is still cleared, under the reason
        :data:`WAIT_REASON_ERROR`, because leaving a wait record behind would
        make a finished run read as still waiting.
        """
        reason = WAIT_REASON_ERROR if result is None else result.reason
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
    persistence: CoordinationPersistence,
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
    persistence: CoordinationPersistence,
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
        if reason != WAIT_REASON_RESOLUTION or any(
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
    elif reason == WAIT_REASON_RESOLUTION:
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
    persistence: CoordinationPersistence,
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
    persistence: CoordinationPersistence,
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
    persistence: CoordinationPersistence,
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


class ReplyStatePreserver:
    """Restore paused Q&A state when a reply attempt fails before consumption.

    A reply is delivered by replaying the paused run, which rewrites both
    ``state.json`` and ``events.jsonl`` before the harness has accepted
    anything. If the attempt dies short of consumption those two files describe
    a continuation that never happened, so the pre-attempt bytes are captured up
    front and put back unless :meth:`mark_consumed` says the reply really landed.
    """

    def __init__(
        self,
        *,
        state_path: Path,
        event_path: Path,
        enabled: bool,
    ) -> None:
        self._enabled = enabled
        self._consumed = False
        self._state_path = state_path
        self._event_path = event_path
        self._state_bytes = read_optional_bytes(state_path) if enabled else None
        self._event_bytes = read_optional_bytes(event_path) if enabled else None

    @property
    def consumed(self) -> bool:
        """Whether the reply reached the harness and must not be rolled back."""
        return self._consumed

    def mark_consumed(self) -> None:
        self._consumed = True

    def restore_if_unconsumed(self) -> None:
        if not self._enabled or self._consumed:
            return
        restore_optional_bytes(self._state_path, self._state_bytes)
        restore_optional_bytes(self._event_path, self._event_bytes)


# ---------------------------------------------------------------------------
# Bounded wait: settings, records, and the supervision cursor
# ---------------------------------------------------------------------------


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
        evidence: LeaseEvidence,
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
        if skew <= MAILBOX_REPLY_WAIT_SUSPEND_SKEW_SECONDS:
            return None
        self.suspend_skew_seconds += skew
        self.suspend_lease_grace_seconds = (
            MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS
        )
        self.lease_grace_until = (
            skew_monotonic + MAILBOX_REPLY_WAIT_SUSPEND_LEASE_GRACE_SECONDS
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

    def evidence(self) -> LeaseEvidence:
        """Capture the lease and skew evidence a terminal wait reason carries."""
        return LeaseEvidence.observed(
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
        MAILBOX_REPLY_WAIT_BASE_ENV_VAR,
        MAILBOX_REPLY_WAIT_BASE_SECONDS,
        environ=environ,
    )
    lease_extension_seconds = finite_positive_env(
        MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR,
        MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS,
        environ=environ,
    )
    unsupervised_ceiling_seconds = finite_positive_env(
        MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR,
        MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_SECONDS,
        environ=environ,
    )
    poll_seconds = finite_positive_env(
        MAILBOX_REPLY_WAIT_POLL_ENV_VAR,
        MAILBOX_REPLY_WAIT_POLL_SECONDS,
        environ=environ,
    )
    io_grace_seconds = finite_positive_env(
        MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR,
        MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS,
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


def mailbox_question_id(
    *,
    plan_dir: Path,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> str:
    """Return the retry-stable UUID5 identity for one completed turn."""
    identity = (
        f"asl-mailbox-question:{plan_dir.resolve()}:{run_id}:{session_id}:{turn_index}"
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def runner_pause_can_wait_for_mailbox_reply(
    *,
    mailbox_enabled: bool,
    pause_reason: str,
    result: harnesses.HarnessResult,
) -> bool:
    """Return whether an operator answer can usefully clear this pause."""
    if not mailbox_enabled or result.returncode != 0:
        return False
    policy = pause_policy.classify_pause(pause_reason)
    return bool(policy["user_action"] == pause_policy.USER_ACTION_REPLY)


def worker_pause_resolution_for_turn(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> mailbox_models.MailboxMessage | None:
    """Return this worker turn's committed pause resolution, if any.

    This lookup is deliberately fail-open: a mailbox read failure or any
    incomplete/stale ownership tuple leaves the ordinary runner-owned question
    path intact. Only a first-close-wins resolution targeting a question owned
    by this exact worker turn can suppress that second question.
    """
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except OSError, mailbox_models.MailboxError:
        return None

    worker_question_ids = {
        message.id
        for message in view.messages
        if message.kind == mailbox_vocabulary.KIND_QUESTION
        and message.run_id == run_id
        and message.session_id == session_id
        and message.turn_index == turn_index
        and message.waiter_origin == mailbox_vocabulary.WAITER_ORIGIN_WORKER
    }
    for message in view.messages:
        if (
            message.kind == mailbox_vocabulary.KIND_RESOLUTION
            and message.action == mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
            and message.run_id == run_id
            and message.to in worker_question_ids
        ):
            return message
    return None


def mailbox_question_text(context: qna.QnAContext) -> str:
    """Return the question body, bounded and byte-stable across retries."""
    text = context.last_message
    if len(text) <= MAILBOX_QUESTION_TEXT_MAX_CHARS:
        return text
    return (
        text[:MAILBOX_QUESTION_TEXT_MAX_CHARS]
        + "\n\n[question truncated to the first "
        + str(MAILBOX_QUESTION_TEXT_MAX_CHARS)
        + f" of {len(text)} characters; full text: "
        + str(context.last_message_path)
        + "]"
    )


def ensure_mailbox_question(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    question_id: str,
    text: str,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> mailbox_models.MailboxMessage:
    """Append one deterministic question or reuse the identical existing one."""

    def existing_question(
        view: mailbox_snapshot.MailboxView,
    ) -> mailbox_models.MailboxMessage | None:
        existing = next(
            (message for message in view.messages if message.id == question_id),
            None,
        )
        if existing is None:
            return None
        closed = view.closure_for(existing.id)
        if (
            existing.kind != mailbox_vocabulary.KIND_QUESTION
            or existing.text != text
            or existing.run_id != run_id
            or existing.session_id != session_id
            or existing.turn_index != turn_index
            or existing.waiter_origin != mailbox_vocabulary.WAITER_ORIGIN_RUNNER
            or (
                existing.delivery_mode != mailbox_vocabulary.DELIVERY_MODE_WAIT
                and closed is None
            )
        ):
            raise MailboxWaitUnavailable(qna.REASON_MISSING_QUESTION_CONTEXT)
        return existing

    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_MAILBOX_READ
        )
        raise MailboxWaitUnavailable(reason) from error
    existing = existing_question(view)
    if existing is not None:
        if (
            existing.delivery_mode == mailbox_vocabulary.DELIVERY_MODE_WAIT
            and view.active_wait_for(existing.id) is None
            and view.closure_for(existing.id) is None
        ):
            mailbox_waits.append_wait_start(
                plan_dir, existing.id, state_dir=state_dir_arg
            )
        return existing
    # A question append is refused at the reduced question cap, not the file
    # cap; the wait loop's own guard stays on ``view.full`` because there the
    # question is whether the *reply* can still land.
    if not view.accepts_questions:
        raise MailboxWaitUnavailable(mailbox_constants.ERROR_MAILBOX_FULL)

    try:
        question, _wait_start = mailbox_waits.append_wait_question(
            plan_dir,
            text,
            state_dir=state_dir_arg,
            message_id=question_id,
            run_id=run_id,
            session_id=session_id,
            turn_index=turn_index,
            waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
        )
        return question
    except mailbox_models.MailboxError as error:
        if error.reason != mailbox_constants.REJECT_DUPLICATE_ID:
            raise MailboxWaitUnavailable(error.reason) from error

    # Another process can win the deterministic append race. Re-read and
    # accept only the exact question this turn intended to publish.
    try:
        raced_view = mailbox.read_mailbox(plan_dir, state_dir=state_dir_arg)
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_MAILBOX_READ
        )
        raise MailboxWaitUnavailable(reason) from error
    raced = existing_question(raced_view)
    if raced is None:
        raise MailboxWaitUnavailable(qna.REASON_MISSING_QUESTION_CONTEXT)
    return raced


def prepare_mailbox_reply_wait(
    *,
    snapshot: plans_models.PlanSnapshot,
    payload: dict[str, Any],
    plan_state: Path,
    state_path: Path,
    state_dir_arg: str | None,
    run_id: str,
    session_id: str,
    turn_index: int,
    started_at: datetime | None = None,
    environ: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> PreparedMailboxWait:
    """Validate turn context, ensure its question, and prepare persisted state."""
    try:
        lease = mailbox_leases.read_lease(
            snapshot.plan_dir,
            state_dir=state_dir_arg,
        )
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_LEASE_READ
        )
        raise MailboxWaitUnavailable(reason) from error
    if not lease.fresh:
        raise MailboxWaitUnavailable(lease.reason or mailbox_constants.ERROR_LEASE_READ)

    settings = mailbox_reply_wait_settings(environ)
    started_monotonic = monotonic()
    resolved_started_at = clock.utc_now() if started_at is None else started_at
    question_id = mailbox_question_id(
        plan_dir=snapshot.plan_dir,
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
    )
    waiting_for_reply = waiting_for_reply_payload(
        question_id=question_id,
        session_id=session_id,
        turn_index=turn_index,
        started_at=resolved_started_at,
        base_deadline=(resolved_started_at + timedelta(seconds=settings.base_seconds)),
        unsupervised_deadline_at=(
            resolved_started_at
            + timedelta(seconds=settings.unsupervised_ceiling_seconds)
        ),
        unsupervised_ceiling_seconds=settings.unsupervised_ceiling_seconds,
        last_lease_activity_at=None,
    )
    try:
        context = qna.build_qna_context(
            snapshot,
            payload,
            plan_state=plan_state,
            state_path=state_path,
            waiting_for_reply=waiting_for_reply,
        )
    except qna.QnAError as error:
        raise MailboxWaitUnavailable(error.reason) from error

    question = ensure_mailbox_question(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=state_dir_arg,
        question_id=question_id,
        text=mailbox_question_text(context),
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
    )
    try:
        prepared_view = mailbox.read_mailbox(
            snapshot.plan_dir,
            state_dir=state_dir_arg,
        )
    except (OSError, mailbox_models.MailboxError) as error:
        reason = (
            error.reason
            if isinstance(error, mailbox_models.MailboxError)
            else mailbox_constants.ERROR_MAILBOX_READ
        )
        raise MailboxWaitUnavailable(reason) from error
    wait_start = prepared_view.active_wait_for(question.id)
    if wait_start is None:
        raise MailboxWaitUnavailable(mailbox_constants.REJECT_QUESTION_NOT_WAITING)
    return PreparedMailboxWait(
        context=context,
        question=question,
        wait_start=wait_start,
        waiting_for_reply=waiting_for_reply,
        pending_outbox=len(prepared_view.open_questions(run_id)),
        settings=settings,
        started_monotonic=started_monotonic,
    )


def wait_for_mailbox_reply(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    payload: dict[str, Any],
    question_id: str,
    run_id: str,
    settings: MailboxWaitSettings,
    persistence: CoordinationPersistence,
    poll_controls: Callable[[], None] | None = None,
    started_monotonic: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall_clock: Callable[[], datetime] = clock.utc_now,
) -> MailboxWaitResult:
    """Poll one closure under monotonic base, lease-extension, and ceiling bounds."""
    started = monotonic() if started_monotonic is None else started_monotonic
    cursor = SupervisionCursor(
        settings=settings,
        started=started,
        monotonic=monotonic,
        wall_clock=wall_clock,
    )
    io_failure_started: float | None = None
    pending_outbox: int | None = None
    mailbox_path = mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir_arg)
    last_successful_view: mailbox_snapshot.MailboxView | None = None
    last_read_token: object = MAILBOX_TOKEN_UNSEEN

    while True:
        if poll_controls is not None:
            poll_controls()

        rebased = cursor.observe_clock()
        if rebased is not None:
            base_deadline, unsupervised_deadline_at = rebased
            rebase_waiting_deadlines(
                payload,
                question_id=question_id,
                base_deadline=base_deadline,
                unsupervised_deadline_at=unsupervised_deadline_at,
                persistence=persistence,
            )

        io_failure_reason: str | None = None
        current_token = mailbox.mailbox_change_token(mailbox_path)
        view = last_successful_view
        if (
            last_read_token is MAILBOX_TOKEN_UNSEEN
            or current_token is mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            or current_token != last_read_token
        ):
            try:
                view = mailbox.read_mailbox(
                    plan_dir,
                    state_dir=state_dir_arg,
                )
            except (OSError, mailbox_models.MailboxError) as error:
                view = None
                io_failure_reason = (
                    error.reason
                    if isinstance(error, mailbox_models.MailboxError)
                    else mailbox_constants.ERROR_MAILBOX_READ
                )
            else:
                last_successful_view = view
                # Retain the token captured before the locked read. An append
                # racing that read will conservatively trigger another parse.
                last_read_token = current_token

        if view is not None:
            pending_outbox = len(view.open_questions(run_id))
            closure = view.closure_for(question_id)
            if closure is not None and closure.kind == mailbox_vocabulary.KIND_REPLY:
                return MailboxWaitResult(
                    reason=WAIT_REASON_REPLY,
                    pending_outbox=pending_outbox,
                    reply=closure,
                )
            if (
                closure is not None
                and closure.kind == mailbox_vocabulary.KIND_RESOLUTION
            ):
                return MailboxWaitResult(
                    reason=WAIT_REASON_RESOLUTION,
                    pending_outbox=pending_outbox,
                    resolution=closure,
                )
            if view.full:
                return MailboxWaitResult.unclosed(
                    mailbox_constants.ERROR_MAILBOX_FULL,
                    pending_outbox=pending_outbox,
                    evidence=cursor.evidence(),
                )
            question = next(
                (
                    message
                    for message in view.messages
                    if message.id == question_id
                    and message.kind == mailbox_vocabulary.KIND_QUESTION
                ),
                None,
            )
            if question is None:
                return MailboxWaitResult.unclosed(
                    qna.REASON_MISSING_QUESTION_CONTEXT,
                    pending_outbox=pending_outbox,
                    evidence=cursor.evidence(),
                )

        now = monotonic()
        if (
            io_failure_reason is None
            and now < cursor.unsupervised_deadline()
            and now >= cursor.active_deadline
        ):
            try:
                lease = mailbox_leases.read_lease(
                    plan_dir,
                    state_dir=state_dir_arg,
                )
            except (OSError, mailbox_models.MailboxError) as error:
                io_failure_reason = (
                    error.reason
                    if isinstance(error, mailbox_models.MailboxError)
                    else mailbox_constants.ERROR_LEASE_READ
                )
            else:
                if lease.updated_at is not None:
                    record_waiting_lease_activity(
                        payload,
                        question_id=question_id,
                        updated_at=lease.updated_at,
                        persistence=persistence,
                    )
                if cursor.observe_lease(lease, now) == "lost":
                    return MailboxWaitResult.unclosed(
                        MAILBOX_REPLY_WAIT_REASON_SUPERVISOR_LOST,
                        pending_outbox=pending_outbox,
                        evidence=cursor.evidence(),
                    )

        if io_failure_reason is not None:
            if io_failure_started is None:
                io_failure_started = now
            if (
                now - io_failure_started >= settings.io_grace_seconds
                or now >= cursor.unsupervised_deadline()
            ):
                return MailboxWaitResult.unclosed(
                    io_failure_reason,
                    pending_outbox=pending_outbox,
                    evidence=cursor.evidence(),
                )
            sleep(
                max(
                    0.0,
                    min(
                        settings.poll_seconds,
                        (io_failure_started + settings.io_grace_seconds) - now,
                        cursor.unsupervised_deadline() - now,
                    ),
                )
            )
            continue
        io_failure_started = None

        if now >= cursor.unsupervised_deadline():
            return MailboxWaitResult.unclosed(
                WAIT_REASON_TIMEOUT,
                pending_outbox=pending_outbox,
                evidence=cursor.evidence(),
            )

        # During a post-suspend lease grace the active deadline stays in the
        # past, so it bounds the sleep only while it is still ahead — pacing
        # on a passed deadline would busy-spin at zero-length sleeps.
        sleep_bound = min(settings.poll_seconds, cursor.unsupervised_deadline() - now)
        if now < cursor.active_deadline:
            sleep_bound = min(sleep_bound, cursor.active_deadline - now)
        sleep(max(0.0, sleep_bound))


def finish_mailbox_reply_wait_lifecycle(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    question_id: str,
    wait_id: str,
    result: MailboxWaitResult,
) -> MailboxWaitResult:
    """Commit wait termination and let a lock-winning closure override timeout.

    This is the recovery half of the exactly-once reply guarantee: the poll
    loop reads the mailbox without its lock, so a reply appended in the instant
    the wait gave up is invisible to it. ``mailbox_waits.finish_wait`` takes the
    lock, and whatever closure it finds there wins over the reason the loop
    arrived with.
    """
    completion = mailbox_waits.finish_wait(
        plan_dir,
        question_id,
        wait_id,
        reason=(
            mailbox_vocabulary.WAIT_END_REASON_TIMEOUT
            if result.reason == WAIT_REASON_TIMEOUT
            else mailbox_vocabulary.WAIT_END_REASON_ERROR
        ),
        state_dir=state_dir_arg,
    )
    closure = completion.closure
    if closure is None:
        return result
    # The evidence the loop gathered is kept verbatim and only the outcome is
    # rewritten, so a timeout that lost the race still reports the lease it saw.
    # Both closure fields are named on both branches: a wait can end on exactly
    # one, and carrying the other over would describe two endings.
    if closure.kind == mailbox_vocabulary.KIND_REPLY:
        return dataclass_replace(
            result, reason=WAIT_REASON_REPLY, reply=closure, resolution=None
        )
    return dataclass_replace(
        result, reason=WAIT_REASON_RESOLUTION, reply=None, resolution=closure
    )


class MailboxWaitCoordinator:
    """One reply-capable pause's whole live wait, start to cleared.

    The ordering this class exists to guarantee is that the durable wait record
    and the lifecycle notification bracket the poll on *both* sides: a wait that
    starts is always announced before it can block, and a wait that stops is
    always cleared and announced even when the poll raises. Doing that inline
    meant a hundred-line ``finally`` whose every field had to repeat "if the
    result is not None"; here the absence of a result is one named case.
    """

    def __init__(
        self,
        *,
        plan_dir: Path,
        state_dir_arg: str | None,
        prepared: PreparedMailboxWait,
        run_id: str,
        store: WaitStateStore,
        persistence: CoordinationPersistence,
        notifier: WaitNotifier | None = None,
        poll_controls: Callable[[], None] | None = None,
        wait_for_reply: Callable[..., MailboxWaitResult] = wait_for_mailbox_reply,
        finish_lifecycle: Callable[..., MailboxWaitResult] = (
            finish_mailbox_reply_wait_lifecycle
        ),
    ) -> None:
        self._plan_dir = plan_dir
        self._state_dir_arg = state_dir_arg
        self._prepared = prepared
        self._run_id = run_id
        self._store = store
        self._persistence = persistence
        self._notifier = notifier
        self._poll_controls = poll_controls
        # Injected rather than called directly so a caller's patch is heard:
        # in-tree callers and tests patch ``wait_for_mailbox_reply`` on this
        # module to assert a wait was or was not entered, and a coordinator that
        # bound the function at construction would ignore the patch. The run
        # loop resolves both names here at the call site for the same reason.
        self._wait_for_reply = wait_for_reply
        self._finish_lifecycle = finish_lifecycle

    @property
    def question(self) -> mailbox_models.MailboxMessage:
        return self._prepared.question

    @property
    def context(self) -> qna.QnAContext:
        return self._prepared.context

    def run(self) -> MailboxWaitResult:
        """Announce, poll, commit, and clear — in that order, always."""
        prepared = self._prepared
        self._store.begin(
            prepared.waiting_for_reply,
            pending_outbox=prepared.pending_outbox,
        )
        if self._notifier is not None:
            self._notifier.wait_started(
                question_id=prepared.question.id,
                question_text=prepared.question.text,
            )
        result: MailboxWaitResult | None = None
        try:
            result = self._wait_for_reply(
                plan_dir=self._plan_dir,
                state_dir_arg=self._state_dir_arg,
                payload=self._store.payload,
                question_id=prepared.question.id,
                run_id=self._run_id,
                settings=prepared.settings,
                persistence=self._persistence,
                poll_controls=self._poll_controls,
                started_monotonic=prepared.started_monotonic,
            )
            result = self._finish_lifecycle(
                plan_dir=self._plan_dir,
                state_dir_arg=self._state_dir_arg,
                question_id=prepared.question.id,
                wait_id=prepared.wait_start.id,
                result=result,
            )
        finally:
            reason = self._store.clear(result)
            if self._notifier is not None:
                self._notifier.wait_ended(
                    question_id=prepared.question.id,
                    wait_reason=reason,
                    closure_id=None if result is None else result.closure_id,
                )
        return result


# ---------------------------------------------------------------------------
# External control commands
# ---------------------------------------------------------------------------


def initial_control_payload() -> dict[str, Any]:
    """The ``control`` slot as it looks before any request has been observed."""
    return {
        "stop_after_current": False,
        "tail_enabled": False,
        "abort_turn_requested": False,
        "abort_grace_seconds": None,
        "abort_reason": None,
        "abort_deadline": None,
        "next_harness": None,
        "target_harness": None,
        "changed_fields": [],
        "implementation_overrides": {},
        "guards": [],
        "consumed": False,
        "last_action": None,
    }


def apply_run_control_action(
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
    *,
    event_action: str | None = None,
) -> str:
    """Fold one action into the ``control`` slot and name its durable action."""
    if event_action is None:
        event_action = RUN_CONTROL_EVENT_ACTIONS[action.action]
    control_payload["stop_after_current"] = action.stop_after_current
    control_payload["tail_enabled"] = action.tail_enabled
    # A consumed abort still carries the grace and reason it was acted on so
    # the event stays legible, but the payload mirrors the cleared state.
    abort_pending = action.abort_requested and action.action != "abort-consumed"
    control_payload["abort_turn_requested"] = abort_pending
    control_payload["abort_grace_seconds"] = (
        action.abort_grace_seconds if abort_pending else None
    )
    control_payload["abort_reason"] = action.abort_reason if abort_pending else None
    if not abort_pending:
        # The deadline is stamped beside the request; nothing pending, nothing
        # left for a status surface to count down to.
        control_payload["abort_deadline"] = None
    control_payload["next_harness"] = action.next_harness
    control_payload["target_harness"] = action.target_harness
    control_payload["changed_fields"] = list(action.changed_fields)
    if action.implementation_overrides is not None:
        control_payload["implementation_overrides"] = {
            harness: dict(values)
            for harness, values in action.implementation_overrides.items()
        }
    if action.action == "consumed":
        control_payload["consumed"] = True
    control_payload["last_action"] = event_action
    return event_action


def record_run_control_action(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    action: run_control.RunControlAction,
    context: Mapping[str, Any] | None = None,
    persistence: CoordinationPersistence,
    event_action: str | None = None,
    message: str | None = None,
) -> None:
    """Persist one control transition and the event that explains it, atomically."""
    with persistence.lock:
        previous_harness = control_payload.get("next_harness") or payload.get("harness")
        event_action = apply_run_control_action(
            control_payload, action, event_action=event_action
        )
        payload["control"] = dict(control_payload)
        event_payload = {
            "run_id": payload.get("run_id"),
            "action": event_action,
            "stop_after_current": action.stop_after_current,
            "tail_enabled": action.tail_enabled,
            "abort_requested": action.abort_requested,
            "abort_grace_seconds": action.abort_grace_seconds,
            "abort_reason": action.abort_reason,
            "abort_deadline": control_payload.get("abort_deadline"),
            "next_harness": action.next_harness,
            "target_harness": action.target_harness,
            "changed_fields": list(action.changed_fields),
            "implementation_overrides": {
                harness: dict(values)
                for harness, values in control_payload.get(
                    "implementation_overrides", {}
                ).items()
            },
            "message": (
                RUN_CONTROL_EVENT_MESSAGES[action.action]
                if message is None
                else message
            ),
        }
        if context is not None:
            for key in ("session", "turn_index", "total_sessions"):
                value = context.get(key)
                if value is not None:
                    event_payload[key] = value
        if action.action == "harness-selected":
            current_harness = (
                context.get("harness")
                if context is not None and context.get("harness") is not None
                else previous_harness
            )
            event_payload["current_harness"] = current_harness
            event_payload["selected_harness"] = action.next_harness
        persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)
        persistence.write_state(payload)


def harness_name(value: str | None) -> run_control.HarnessName | None:
    """Narrow a drained command's harness to the name the control state takes.

    ``control_queue.drain_queue`` already rejects every other value, so this
    restates that guarantee in the type system rather than validating twice.
    It is still a guard and not a cast: the queue file is a durable artifact
    another process writes, and a name that slipped past the drain must degrade
    to "no harness" rather than reach the run's control payload as a harness
    nothing can execute.
    """
    # ``HARNESS_NAMES`` is the membership the drain checked, but mypy cannot
    # read a runtime collection as a type guard, so the literals are restated
    # and the two are asserted to agree by the module's tests.
    if value in harnesses.HARNESS_NAMES and value in ("codex", "claude", "copilot"):
        return value
    return None


def apply_external_control_command(
    command: control_queue.ControlCommand,
    control_state: run_control.RunControlState,
) -> run_control.RunControlAction | None:
    """Translate one queued command into a RunControlAction, if it changes state."""
    if command.command == control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT:
        return control_state.request_stop_after_current()
    if command.command == control_queue.COMMAND_SELECT_FUTURE_HARNESS:
        selected = harness_name(command.harness)
        assert selected is not None
        return control_state.select_harness(selected)
    if command.command == control_queue.COMMAND_SET_IMPLEMENTATION_MODEL:
        return control_state.set_implementation_model(
            # ``None`` here means "every harness", which is also what an
            # unrecognized name degrades to; the drain guarantees it cannot be
            # one in practice.
            target_harness=harness_name(command.harness),
            model=command.model_operation,
            effort=command.effort_operation,
        )
    if command.command == control_queue.COMMAND_REQUEST_ABORT_TURN:
        return control_state.request_abort_turn(
            grace_seconds=command.grace_seconds,
            reason=command.reason,
        )
    return None


def record_control_command_observed(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    command: control_queue.ControlCommand,
    context: Mapping[str, Any] | None,
    persistence: CoordinationPersistence,
) -> None:
    """Append the delivery receipt for one accepted external control command.

    Recorded before the command is translated, and for every accepted command
    rather than only the ones that change state: a duplicate, an
    already-applied request, and a command this runner has no branch for all
    leave no other trace, which is what makes them indistinguishable from a
    command that never arrived. No state transition happened, so this appends
    an event and writes no state.
    """
    event_payload: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
        "command_id": command.id,
        "queue_command": command.command,
        "stop_after_current": bool(control_payload.get("stop_after_current")),
        "message": "control command observed",
    }
    if context is not None:
        for key in ("session", "turn_index", "total_sessions"):
            value = context.get(key)
            if value is not None:
                event_payload[key] = value
    persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)


def record_guard_outcome(
    *,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    command_id: str | None,
    outcome: guards.GuardOutcome,
    active: Sequence[dict[str, Any]],
    context: Mapping[str, Any] | None,
    persistence: CoordinationPersistence,
) -> None:
    """Persist one guard mutation's terminal outcome and the active set.

    The event is what ``guard add|remove --wait`` resolves on, so it is
    appended only after enforcement is genuinely in place (or genuinely
    refused) — never on queue observation alone. The ``control`` slot carries
    the full active set afterwards so status can report guards without
    replaying events.
    """
    action = {
        guards.ACTION_APPLIED: events.RUN_CONTROL_ACTION_GUARD_APPLIED,
        guards.ACTION_REMOVED: events.RUN_CONTROL_ACTION_GUARD_REMOVED,
    }.get(outcome.action, events.RUN_CONTROL_ACTION_GUARD_REJECTED)
    with persistence.lock:
        control_payload["guards"] = [dict(entry) for entry in active]
        control_payload["last_action"] = action
        payload["control"] = dict(control_payload)
        event_payload: dict[str, Any] = {
            "run_id": payload.get("run_id"),
            "action": action,
            "command_id": command_id,
            "guard_ids": list(outcome.guard_ids),
            "guards": [dict(entry) for entry in active],
            "reason": outcome.reason,
            "detail": outcome.detail,
            "message": (
                f"guard {outcome.action}"
                if outcome.ok
                else f"guard rejected: {outcome.reason}"
            ),
        }
        if context is not None:
            for key in ("session", "turn_index", "total_sessions"):
                value = context.get(key)
                if value is not None:
                    event_payload[key] = value
        persistence.append_event(event_path, events.RUN_CONTROL_EVENT, event_payload)
        persistence.write_state(payload)


def _apply_guard_add(
    controller: guards.GuardController,
    command: control_queue.ControlCommand,
) -> guards.GuardOutcome:
    """Activate one ``add-guard`` line against the run that owns the registry.

    The queue parses a path rule's syntax; the alias half is checked here,
    because this is the first point that holds the run's own repository
    binding. An external producer writing the queue file directly cannot know
    that binding, so accepting its rule unchecked would arm a guard watching a
    repository the plan does not have.
    """
    aliases = tuple(controller.workspace_aliases()) or (guards.LEGACY_PATH_QUALIFIER,)
    for protected in command.protect_paths:
        if protected.qualifier not in aliases:
            return guards.GuardOutcome(
                action=guards.ACTION_REJECTED,
                reason=guards.REJECT_PATH_UNKNOWN_ALIAS,
                detail=(
                    f"protected-path rule {protected.rendered!r} names a "
                    f"repository this run does not hold; known qualifiers: "
                    f"{', '.join(sorted(aliases))}"
                ),
            )
    return controller.add(
        command.deny_commands,
        paths=command.protect_paths,
        reason=command.reason,
    )


def apply_guard_command(
    command: control_queue.ControlCommand,
    *,
    controller: guards.GuardController | None,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: CoordinationPersistence,
) -> None:
    """Run one queued guard mutation against the run that is actually live.

    The expected run ID is compared here rather than at append time because
    only the drain happens inside the run: a command queued against a run that
    has since ended must be refused, not silently inherited by its successor.
    """
    active: Sequence[dict[str, Any]] = (
        () if controller is None else controller.to_payload()
    )
    if controller is None or command.expected_run_id != controller.run_id:
        outcome = guards.GuardOutcome(
            action=guards.ACTION_REJECTED,
            reason=(
                guards.REJECT_NO_ACTIVE_RUN
                if controller is None
                else guards.REJECT_RUN_CHANGED
            ),
            detail=command.expected_run_id,
        )
    elif command.command == control_queue.COMMAND_REMOVE_GUARD:
        outcome = controller.remove(command.guard_id or "")
        active = controller.to_payload()
    else:
        outcome = _apply_guard_add(controller, command)
        active = controller.to_payload()
    record_guard_outcome(
        event_path=event_path,
        payload=payload,
        control_payload=control_payload,
        command_id=command.id,
        outcome=outcome,
        active=active,
        context=context,
        persistence=persistence,
    )


def expire_run_guards(
    *,
    controller: guards.GuardController | None,
    event_path: Path,
    payload: dict[str, Any],
    control_payload: dict[str, Any],
    persistence: CoordinationPersistence,
) -> None:
    """Disable this run's command gate as the run ends.

    Expiry is the reason a guard is safe to add: it cannot leak into a later
    run. It runs on every exit path, including the failing ones, and swallows
    its own errors — a run must not be reported as crashed because a policy
    file could not be unlinked. The runtime files that stay behind are inert:
    no later worker inherits this run's ``PATH``.
    """
    if controller is None:
        return
    try:
        expired = controller.expire()
    except OSError:
        return
    if not expired:
        return
    with contextlib.suppress(Exception), persistence.lock:
        control_payload["guards"] = []
        control_payload["last_action"] = events.RUN_CONTROL_ACTION_GUARDS_EXPIRED
        payload["control"] = dict(control_payload)
        persistence.append_event(
            event_path,
            events.RUN_CONTROL_EVENT,
            {
                "run_id": payload.get("run_id"),
                "action": events.RUN_CONTROL_ACTION_GUARDS_EXPIRED,
                "guard_ids": [rule.id for rule in expired],
                "message": "run-scoped command guards expired with the run",
            },
        )
        persistence.write_state(payload)


def abort_turn_guidance_text(
    *,
    reason: str,
    grace_seconds: int,
    deadline: str,
) -> str:
    return (
        "Supervisor abort requested for the turn now in flight "
        f"(reason: {reason}). Wind down immediately: make no further "
        "changes, leave this session pending, and emit `RUNNER_PAUSE: "
        f"supervisor requested abort - {reason}`. The harness process group "
        f"is killed at {deadline} ({grace_seconds}s grace) if the turn is "
        "still running then."
    )


def arm_requested_abort(
    action: run_control.RunControlAction,
    *,
    abort: harnesses.AbortSignal,
    plan_dir: Path,
    state_dir_arg: str | None,
    control_payload: dict[str, Any],
    mailbox_enabled: bool,
    finalization: bool = False,
) -> None:
    """Start the grace window for an observed abort and warn the worker.

    Called from the poll thread while the harness turn it aborts is still
    running. ``abort`` is the only state shared with that turn's wait loop,
    which owns expiry from here; the guidance record is what lets a compliant
    worker end the turn itself before the kill lands.

    ``finalization`` arms the kill immediately and warns nobody. The grace
    window exists so a worker can wind its own turn down and leave usable work
    behind; the terminal audit is advisory and read-only, there is no session
    to preserve, and the run is holding the plan lock while it waits — so the
    window would only delay the cancellation the supervisor just asked for.
    """
    grace_seconds = (
        0
        if finalization
        else (
            control_queue.DEFAULT_ABORT_GRACE_SECONDS
            if action.abort_grace_seconds is None
            else action.abort_grace_seconds
        )
    )
    reason = action.abort_reason or ABORT_TURN_DEFAULT_REASON
    deadline = clock.isoformat(clock.utc_now() + timedelta(seconds=grace_seconds))
    abort.arm(reason, grace_seconds=float(grace_seconds))
    control_payload["abort_deadline"] = deadline
    if finalization or not mailbox_enabled:
        return
    # The kill is already armed; failing to warn the worker must not
    # terminate the turn or lose the abort.
    with contextlib.suppress(OSError, mailbox_models.MailboxError):
        mailbox_appenders.append_guidance(
            plan_dir,
            abort_turn_guidance_text(
                reason=reason,
                grace_seconds=grace_seconds,
                deadline=deadline,
            ),
            state_dir=state_dir_arg,
        )


def degrade_abort_to_stop_after_current(
    action: run_control.RunControlAction,
    *,
    control_state: run_control.RunControlState,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: CoordinationPersistence,
) -> None:
    """Turn an abort observed with no turn in flight into a clean early stop.

    There is nothing to kill, and dropping the command would make
    ``asl control abort`` a silent no-op between turns, so the request becomes
    the strongest thing it can still mean: no further session starts.
    """
    consumed = control_state.consume_abort_turn()
    if consumed is not None:
        record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=consumed,
            context=context,
            persistence=persistence,
            message=("turn abort degraded to stop-after-current: no turn in flight"),
        )
    requested = control_state.request_stop_after_current()
    if requested is not None:
        record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=requested,
            context=context,
            persistence=persistence,
        )


def drain_external_control_queue(
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    control_state: run_control.RunControlState,
    seen_ids: set[str],
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None,
    persistence: CoordinationPersistence,
    run_control_lock: AbstractContextManager[Any],
    wakeup: threading.Event | None = None,
    abort: harnesses.AbortSignal | None = None,
    mailbox_enabled: bool = False,
    finalization: bool = False,
    guard_controller: guards.GuardController | None = None,
) -> control_queue.DrainResult:
    """Drain queued external control commands and record any state changes.

    Each accepted command is translated into the same ``RunControlAction``
    the keyboard listener would emit and persisted through
    ``record_run_control_action`` so events and the ``control`` payload
    look identical regardless of source. Commands whose IDs are already
    in ``seen_ids`` are rejected by the drain function itself. Accepted
    IDs are added to ``seen_ids`` so even a queue file rewritten by an
    external producer cannot replay the same command in this process.

    Every accepted command first gets a ``control-command-observed`` receipt
    carrying its ID, which is what ``asl control --wait`` tails; the state
    transition, if there is one, is recorded after it.

    ``wakeup`` is cleared *before* the read, not after: a wakeup that arrives
    while this drain runs may name a command appended after the read, so
    clearing afterwards would swallow it. Clearing first costs at most one
    redundant drain.

    ``abort`` is the in-flight turn's signal, supplied only by the poll thread
    that runs alongside a turn. Its presence is what tells an observed abort
    command there is something to kill: without it the request degrades to
    stop-after-current instead of dying quietly.

    ``guard_controller`` is the live run's command gate. Guard mutations are
    handled here instead of through ``apply_external_control_command`` because
    they act on the filesystem rather than on the run-control state machine,
    and because their acceptance depends on the run being the one the operator
    queued against.

    ``finalization`` says the signal belongs to the run's terminal work rather
    than to a turn. What changes is that the kill is armed without a grace
    window and warns nobody — there is no turn in flight to wind down — and the
    action is recorded as a finalization abort so a supervisor can tell the two
    apart.
    """
    if wakeup is not None:
        wakeup.clear()
    try:
        result = control_queue.drain_queue(
            plan_dir,
            state_dir=state_dir_arg,
            seen_ids=seen_ids,
        )
    except OSError:
        return control_queue.DrainResult(
            queue_path=control_queue.queue_path_for_plan(plan_dir, state_dir_arg)
        )

    for command in result.accepted:
        seen_ids.add(command.id)
        with run_control_lock:
            record_control_command_observed(
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                command=command,
                context=context,
                persistence=persistence,
            )
            if command.command in control_queue.GUARD_COMMAND_NAMES:
                apply_guard_command(
                    command,
                    controller=guard_controller,
                    event_path=event_path,
                    payload=payload,
                    control_payload=control_payload,
                    context=context,
                    persistence=persistence,
                )
                continue
            action = apply_external_control_command(command, control_state)
            if action is None:
                continue
            aborting = action.action == "abort-requested"
            finalization_abort = aborting and finalization and abort is not None
            if aborting and abort is not None:
                # Armed before the action is recorded so the requested event and
                # the `control` payload already carry the deadline the turn is
                # counting down to.
                arm_requested_abort(
                    action,
                    abort=abort,
                    plan_dir=plan_dir,
                    state_dir_arg=state_dir_arg,
                    control_payload=control_payload,
                    mailbox_enabled=mailbox_enabled,
                    finalization=finalization,
                )
            record_run_control_action(
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                action=action,
                context=context,
                persistence=persistence,
                event_action=(
                    events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED
                    if finalization_abort
                    else None
                ),
                message=(
                    "abort observed while finalizing: cancelling the terminal audit"
                    if finalization_abort
                    else None
                ),
            )
            if aborting and abort is None:
                degrade_abort_to_stop_after_current(
                    action,
                    control_state=control_state,
                    payload=payload,
                    event_path=event_path,
                    control_payload=control_payload,
                    context=context,
                    persistence=persistence,
                )
    return result


class MailboxPoll:
    """Observe live mailbox guidance alongside external control commands."""

    def __init__(
        self,
        *,
        plan_dir: Path,
        state_dir_arg: str | None,
        control_state: run_control.RunControlState,
        seen_control_ids: set[str],
        payload: dict[str, Any],
        event_path: Path,
        control_payload: dict[str, Any],
        persistence: CoordinationPersistence,
        run_control_lock: AbstractContextManager[Any],
        enabled: bool = True,
        wakeup: threading.Event | None = None,
        guard_controller: guards.GuardController | None = None,
    ) -> None:
        self._plan_dir = plan_dir
        self._state_dir_arg = state_dir_arg
        self._control_state = control_state
        self._seen_control_ids = seen_control_ids
        self._payload = payload
        self._event_path = event_path
        self._control_payload = control_payload
        self._persistence = persistence
        self._run_control_lock = run_control_lock
        self._enabled = enabled
        self._wakeup = wakeup
        self._guard_controller = guard_controller
        self._context: dict[str, Any] | None = None
        self._abort: harnesses.AbortSignal | None = None
        self._finalization = False
        self._evented_guidance_ids: set[str] = set()
        self._live_delivery: guidance_delivery.LiveDeliveryEndpoint | None = None
        self._settled_guidance_ids: set[str] = set()
        self._recorded_guidance_attempts: set[tuple[str, str, str]] = set()
        self._unconfirmed_guidance_ids: set[str] = set()
        self._mailbox_path = mailbox_cursors.mailbox_path_for_plan(
            plan_dir,
            state_dir_arg,
        )
        self._last_read_token: object = MAILBOX_TOKEN_UNSEEN

    def set_context(self, context: Mapping[str, Any] | None) -> None:
        """Set the active turn metadata attached to subsequent poll events."""
        self._context = None if context is None else dict(context)

    def set_abort(
        self,
        abort: harnesses.AbortSignal | None,
        *,
        finalization: bool = False,
    ) -> None:
        """Attach the signal an observed abort should arm, or detach it.

        Set while a turn is in flight and cleared once it has finished, so an
        abort observed between turns (during a mailbox reply wait, for example)
        cannot arm a signal no running turn is watching. ``finalization`` marks
        the signal as belonging to the run's terminal work instead of a turn.
        """
        self._abort = abort
        self._finalization = finalization

    def set_live_delivery(
        self,
        endpoint: guidance_delivery.LiveDeliveryEndpoint | None,
    ) -> None:
        """Attach the running turn's live-delivery endpoint, or detach it.

        Set before the turn starts and cleared after the poll thread has been
        joined, exactly like :meth:`set_abort`, so no tick can deliver into a
        turn that has finished. Both per-turn ledgers are reset with the
        endpoint: what a turn settled and what it recorded are facts about that
        turn, and a new turn is a new chance to deliver.

        Attaching also drops the change token so the first tick of the new turn
        parses the mailbox again. Without that, guidance deferred in the
        previous turn would sit undelivered until something else happened to
        write the mailbox: the unchanged-token short circuit would keep
        returning before the delivery pass ever ran.
        """
        self._live_delivery = endpoint
        self._settled_guidance_ids = set()
        self._recorded_guidance_attempts = set()
        self._unconfirmed_guidance_ids = set()
        if endpoint is not None:
            self._last_read_token = MAILBOX_TOKEN_UNSEEN

    def __call__(self) -> None:
        """Drain controls, then emit once-only metadata for pending guidance."""
        drain_external_control_queue(
            plan_dir=self._plan_dir,
            state_dir_arg=self._state_dir_arg,
            control_state=self._control_state,
            seen_ids=self._seen_control_ids,
            payload=self._payload,
            event_path=self._event_path,
            control_payload=self._control_payload,
            context=self._context,
            persistence=self._persistence,
            run_control_lock=self._run_control_lock,
            wakeup=self._wakeup,
            abort=self._abort,
            mailbox_enabled=self._enabled,
            finalization=self._finalization,
            guard_controller=self._guard_controller,
        )

        if not self._enabled:
            return

        current_token = mailbox.mailbox_change_token(self._mailbox_path)
        if (
            # An attempt that is written but unconfirmed has to be asked again,
            # and its evidence arrives on the harness's schedule rather than by
            # anything writing the mailbox. Short circuiting on an unchanged
            # token here would stop the retries after the one tick whose own
            # ledger append happened to move the token.
            not self._unconfirmed_guidance_ids
            and self._last_read_token is not MAILBOX_TOKEN_UNSEEN
            and current_token is not mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE
            and current_token == self._last_read_token
        ):
            return

        try:
            view = mailbox.read_mailbox(
                self._plan_dir,
                state_dir=self._state_dir_arg,
            )
        except OSError, mailbox_models.MailboxError:
            # Mailbox status remains authoritative through its own direct read;
            # a live observation failure must not terminate the harness turn.
            return

        # Retain the token captured before the locked read. An append racing
        # that read will conservatively trigger another parse on the next tick.
        self._last_read_token = current_token
        for message in view.pending_guidance:
            if message.id in self._evented_guidance_ids:
                continue
            text = message.text or ""
            event_payload = {
                "run_id": self._payload.get("run_id"),
                "action": events.MAILBOX_ACTION_GUIDANCE_OBSERVED,
                "message_id": message.id,
                "seq": message.seq,
                "length": len(text),
                "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
            if self._context is not None:
                for key in (
                    "session",
                    "session_id",
                    "turn_index",
                    "total_sessions",
                ):
                    value = self._context.get(key)
                    if value is not None:
                        event_payload[key] = value
            self._persistence.append_event(
                self._event_path,
                events.MAILBOX_EVENT,
                event_payload,
            )
            self._evented_guidance_ids.add(message.id)

        self._deliver_guidance(view)

    def _deliver_guidance(self, view: mailbox_snapshot.MailboxView) -> None:
        """Offer each undelivered guidance record to the turn now running.

        Nothing happens between turns: with no endpoint there is no live
        process to deliver into, and recording a deferral per tick would fill
        the ledger with history about a turn that does not exist.

        One attempt per guidance per turn, with one exception: an attempt that
        is still open. A transport that has written guidance out but cannot yet
        prove a model received it answers ``awaiting-confirmation``, and the
        only way that ever becomes a delivery is by asking again — the evidence
        arrives on the harness's own schedule, not on the poller's. Such an
        attempt is retried for the rest of the turn and needs no separate cap:
        each retry is a cheap local check, the ledger deduplicates below, and
        the endpoint's own close turns the last retry into ``stale-turn``.
        """
        endpoint = self._live_delivery
        if endpoint is None:
            return
        for message in view.deliverable_guidance:
            if message.id in self._settled_guidance_ids:
                continue
            # Marked before the submit, not after: an attempt that settles stays
            # settled even if recording the outcome fails below.
            self._settled_guidance_ids.add(message.id)
            outcome = endpoint.submit(
                guidance_delivery.GuidanceRequest(
                    guidance_id=message.id,
                    seq=message.seq,
                    text=message.text or "",
                    turn=endpoint.turn,
                )
            )
            if outcome.retriable_in_turn:
                self._settled_guidance_ids.discard(message.id)
                self._unconfirmed_guidance_ids.add(message.id)
            else:
                self._unconfirmed_guidance_ids.discard(message.id)
            self._record_delivery(message, outcome, endpoint.turn)

    def _record_delivery(
        self,
        message: mailbox_models.MailboxMessage,
        outcome: guidance_delivery.GuidanceOutcome,
        turn: guidance_delivery.TurnIdentity,
    ) -> None:
        """Append the attempt to the ledger and event what it did.

        The append happens only after the endpoint has returned an outcome, so
        the record always describes something that finished. If the append
        itself fails after a successful transport, nothing is written and the
        guidance stays deliverable: a later turn may then deliver it a second
        time. At-least-once is the honest guarantee, and a duplicate is a far
        smaller harm than a durable record claiming a delivery the ledger could
        not actually witness.

        At most one record per (guidance, state, reason) per turn. A retried
        attempt reports the same thing until something changes, and a ledger
        that appended every poll tick would say a hundred times over what it
        only knows once. The moment the answer does change — confirmed, or
        broken — the new pair is unrecorded and is written.
        """
        signature = (message.id, outcome.delivery_state, outcome.reason)
        if signature in self._recorded_guidance_attempts:
            return
        self._recorded_guidance_attempts.add(signature)
        try:
            record = mailbox_appenders.append_guidance_delivery(
                self._plan_dir,
                message.id,
                delivery_state=outcome.delivery_state,
                delivery_reason=outcome.reason,
                channel=outcome.channel,
                run_id=turn.run_id,
                session_id=turn.session_id,
                turn_index=turn.turn_index,
                harness=turn.harness,
                state_dir=self._state_dir_arg,
            )
        except OSError, mailbox_models.MailboxError:
            # Same fail-open discipline as the read above: a mailbox that
            # cannot record a delivery must not end the harness turn. The
            # signature is released so a retried attempt can still record what
            # this one failed to; dedup is for repetition, not for suppression.
            self._recorded_guidance_attempts.discard(signature)
            return
        event_payload = {
            "action": (
                events.MAILBOX_ACTION_GUIDANCE_DELIVERED
                if outcome.delivered
                else events.MAILBOX_ACTION_GUIDANCE_DELIVERY_DEFERRED
            ),
            **mailbox_appenders.guidance_delivery_event_payload(record),
        }
        self._persistence.append_event(
            self._event_path,
            events.MAILBOX_EVENT,
            event_payload,
        )


# ---------------------------------------------------------------------------
# Terminal assurance coordination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminalAuditContext:
    """Everything the terminal audit needs, resolved once per run."""

    plan: supervisor_audit.AuditPlan
    run_dir: Path
    state_dir: Path
    plan_dir: Path
    workspace_root: Path
    #: The state root holding the one shared assurance ledger, not this plan's
    #: state directory. ``None`` resolves to the default root.
    ledger_state_dir: Path | None = None
    #: Workspace-relative paths of the repositories the plan registered, which
    #: are the only ones the audit's read-only boundary check fingerprints.
    repositories: tuple[str, ...] = ()
    #: Whether those paths came from a plan registry rather than being
    #: synthesized for a legacy plan that registered none.
    registry_declared: bool = True


@dataclass(frozen=True)
class TerminalAssuranceOutcome:
    """What the advisory terminal audit did, as a value rather than a side effect."""

    #: Whether the audit was interrupted rather than allowed to render a verdict.
    cancelled: bool
    #: Whether an audit ran at all. ``False`` means the run was not auditable
    #: (ineligible, already graded, or a settled decision not to audit).
    audited: bool
    #: Whether the derived operator projection was attached beside the verdict.
    reported: bool
    #: The assurance state the run ended on, when it has one.
    state: str | None = None

    @property
    def degraded(self) -> bool:
        """Whether the audit left the run graded but unable to speak."""
        return self.state in DEGRADED_ASSURANCE_STATES


def attach_terminal_assurance(
    payload: dict[str, Any],
    context: TerminalAuditContext,
    persistence: CoordinationPersistence,
    abort: harnesses.AbortSignal | None = None,
    *,
    state_snapshot: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    """Audit the run, publish the verdict, and attach the bounded summary.

    A run that resolved no independent role starts ``unavailable`` and is still
    audited, because the deterministic half needs no agent and the reason the
    run could not be graded belongs in the shared inbox rather than only in its
    own state.

    ``state_snapshot`` is the runner's compaction of the live payload; it is
    taken under the run lock and handed to the audit as an immutable copy, so
    the audit never reads a payload another thread is still writing.

    Returns whether the audit was cancelled rather than completed.
    """
    assurance = payload.get("assurance")
    if not isinstance(assurance, Mapping) or not assurance.get("eligible"):
        return False
    if assurance.get("state") not in AUDITABLE_ASSURANCE_STATES:
        return False
    with persistence.lock:
        state_payload = state_snapshot(payload)
    cancelled = False
    try:
        summary = supervisor_audit.audit_terminal_run(
            context.plan,
            run_id=str(payload.get("run_id")),
            run_dir=context.run_dir,
            state_dir=context.state_dir,
            plan_dir=context.plan_dir,
            workspace_root=context.workspace_root,
            ledger_state_dir=context.ledger_state_dir,
            state_payload=state_payload,
            repositories=context.repositories,
            registry_declared=context.registry_declared,
            abort=abort,
        )
    except KeyboardInterrupt, supervisor_audit.AuditCancelled:
        # Cancelling an advisory audit cancels the audit alone: the outcome and
        # the exit code were decided before this ran, and the terminal record
        # they belong to has still not been written.
        summary = supervisor_audit.cancelled_summary(
            context.plan, run_id=str(payload.get("run_id"))
        )
        cancelled = True
    with persistence.lock:
        payload["assurance"] = summary.to_dict()
    return cancelled


def attach_assurance_report(
    payload: dict[str, Any],
    context: TerminalAuditContext,
    persistence: CoordinationPersistence,
) -> bool:
    """Attach the additive operator projection of this run's assurance object.

    Written beside the assurance object rather than inside it: the per-run
    object is a validated schema that rejects unknown keys, while this block is
    a derived view whose open-issue count is a fact about the whole inbox at the
    moment the run ended.
    """
    with persistence.lock:
        assurance = payload.get("assurance")
        run_dir = payload.get("run_dir")
    report = assurance_report.build_run_report(
        assurance,
        run_dir=run_dir,
        state_dir=context.ledger_state_dir,
    )
    if report is None:
        return False
    with persistence.lock:
        payload["assurance_report"] = report
    return True


def record_degraded_assurance(
    payload: dict[str, Any], *, persistence: CoordinationPersistence
) -> None:
    """Record that optional assurance could not speak, without blocking on it.

    Post-run assurance is advisory by design, so its unavailability may not
    turn a run that earned its success into a failure. What it may not do is
    disappear: a run whose audit never rendered a verdict is a *degraded*
    success, and this states that explicitly beside the evidence the audit did
    leave behind, so an operator reading a green run can tell the difference
    between "graded clean" and "not graded".
    """
    with persistence.lock:
        assurance = payload.get("assurance")
        assurance = assurance if isinstance(assurance, Mapping) else {}
        state = assurance.get("state")
        if state not in DEGRADED_ASSURANCE_STATES:
            payload.pop("assurance_degraded", None)
            return
        artifacts = assurance.get("artifacts")
        evidence = [
            item.get("locator")
            for item in (artifacts if isinstance(artifacts, list) else [])
            if isinstance(item, Mapping) and item.get("locator")
        ]
        payload["assurance_degraded"] = {
            "assurance_state": state,
            "reason": assurance.get("reason"),
            "blocking": False,
            "run_ok": bool(payload.get("ok")),
            "evidence": evidence,
            "issue_ids": list(assurance.get("issue_ids") or ()),
        }
        persistence.write_state(payload)


class FinalizationPhase(Protocol):
    """The run's published "terminal work in flight" phase, as the audit uses it.

    Structural so the runner keeps ownership of the heartbeat thread and its
    durable events while this module owns the guarantee that the phase is
    always closed.
    """

    def start(self) -> None: ...

    def finish(self, *, cancelled: bool = False) -> None: ...


class TerminalAssuranceCoordinator:
    """Runs the advisory terminal audit inside a published, closeable phase.

    The invariant is that the phase is closed exactly once whatever happens
    inside it. A cancelled audit closes it as cancelled; an audit that raises
    something the audit boundary does not convert still closes it, and the
    exception propagates to a caller that already decided this run's exit code.
    """

    def __init__(
        self,
        *,
        payload: dict[str, Any],
        context: TerminalAuditContext,
        persistence: CoordinationPersistence,
        phase: FinalizationPhase,
        state_snapshot: Callable[[dict[str, Any]], dict[str, Any]],
        abort: harnesses.AbortSignal | None = None,
    ) -> None:
        self._payload = payload
        self._context = context
        self._persistence = persistence
        self._phase = phase
        self._state_snapshot = state_snapshot
        self._abort = abort

    def run(self) -> TerminalAssuranceOutcome:
        """Publish the phase, audit, report, and always close the phase."""
        before = self._assurance_state()
        auditable = before in AUDITABLE_ASSURANCE_STATES and self._eligible()
        self._phase.start()
        cancelled = False
        reported = False
        try:
            cancelled = attach_terminal_assurance(
                self._payload,
                self._context,
                self._persistence,
                abort=self._abort,
                state_snapshot=self._state_snapshot,
            )
            reported = attach_assurance_report(
                self._payload, self._context, self._persistence
            )
        finally:
            self._phase.finish(cancelled=cancelled)
        return TerminalAssuranceOutcome(
            cancelled=cancelled,
            audited=auditable,
            reported=reported,
            state=self._assurance_state(),
        )

    def _assurance(self) -> Mapping[str, Any]:
        with self._persistence.lock:
            assurance = self._payload.get("assurance")
        return assurance if isinstance(assurance, Mapping) else {}

    def _assurance_state(self) -> str | None:
        state = self._assurance().get("state")
        return state if isinstance(state, str) else None

    def _eligible(self) -> bool:
        return bool(self._assurance().get("eligible"))
