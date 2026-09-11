"""Coalescing corrections into a continuation prompt, and receipting them.

Session 11 put an accepted correction into the *next attempt* of the same
session. It left two questions open, and this module answers both.

**Which corrections belong in the prompt?** An operator who interrupts a turn
usually keeps typing: a second correction lands while the predecessor is being
collected, a third while the resumed prompt is being built. Each of them was
written about the same situation, so all of them belong in the one prompt, in
the order they were sent. :func:`carry_corrections` is that boundary and it is a
*moment*, not a heuristic: everything unsettled against an earlier attempt of
this session at the instant the prompt is finalized is carried and marked
``continued`` in the journal, and anything submitted after that instant is a
redirect against the attempt the prompt is about to start -- which is the
existing session-11 path, not a late edit to a prompt already written.

**What counts as the worker having received it?** Only an explicit receipt from
the worker, naming the correction *and* the attempt it is running:
:func:`acknowledge_correction`. Everything cheaper is evidence of something
else. A successful pipe write proves a byte reached a harness. A transcript
containing the correction proves the prompt was recorded, and a replayed
transcript proves it twice for an attempt that is long gone. A harness accepting
a prompt proves it parsed. None of them distinguish "a model read the
correction and is acting on it" from "a process received some text", and the
whole point of a redirect is the former. So the receipt is doubly identified,
and both identities are checked: the token the worker presents must equal the
live attempt's own token, and the journal must already record that attempt as
the one which carried the correction. A stale worker from the interrupted turn
and a transcript echo of an older prompt both fail the first check; a worker
that invents an attempt fails the second.

Waiting for a receipt is *observational*. :func:`wait_for_acknowledgment`
polls the journal and reports what it saw; it writes nothing, cancels nothing,
and a timeout is not an escalation. A worker can be genuinely busy applying the
correction for far longer than any interesting wait, so treating silence as
refusal would interrupt exactly the work the correction asked for -- and a
second interruption is the one outcome an operator correcting a run cannot want.

Nothing here imports the runner or ``run_loop``, and nothing here owns a loop
except the observational wait above.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import clock, redirect_store

# ---------------------------------------------------------------------------
# Attempt identity
# ---------------------------------------------------------------------------

#: Separator inside an attempt token. Run ids and session ids cannot contain it
#: (the journal's own validation bounds them, and both are path-safe
#: identifiers), so a token has exactly three parts by construction.
ATTEMPT_TOKEN_SEPARATOR = "/"


def attempt_token(target: redirect_store.RedirectTarget) -> str:
    """Return the opaque identity a worker echoes back to receipt a correction.

    Built from the turn's identity triple and deliberately *not* from the
    harness: the run loop may re-select a harness between the interruption and
    the continuation, and that changes what runs the attempt rather than which
    attempt it is.

    The token is compared with string equality and never parsed back into a
    target. A comparison that took the token apart would have to decide what a
    malformed part means, and every answer to that question is a way for a
    worker to be credited with a receipt it did not earn.
    """
    return ATTEMPT_TOKEN_SEPARATOR.join(
        (
            target.run_id,
            target.session_id,
            str(target.turn_index),
        )
    )


def token_identifies(
    token: str,
    target: redirect_store.RedirectTarget,
) -> bool:
    """Whether ``token`` is exactly the token ``target`` would mint."""
    return isinstance(token, str) and token == attempt_token(target)


# ---------------------------------------------------------------------------
# Carried corrections
# ---------------------------------------------------------------------------

#: Failure reasons this module raises on its own behalf. Journal refusals keep
#: `redirect_store`'s reasons; these two are about the *worker's* claim.
ERROR_ATTEMPT_MISMATCH = "redirect-attempt-mismatch"
ERROR_NO_LIVE_ATTEMPT = "redirect-no-live-attempt"


class RedirectAckError(Exception):
    """A refused acknowledgment, carrying a stable surface-agnostic reason."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class Correction:
    """One correction as a prompt is about to render it.

    This is the one object in the redirect subsystem that holds the operator's
    words, and it exists because a prompt has to say them. ``to_payload`` omits
    the text on purpose: an event, a state file, or a diagnostic bundle built
    from this object carries the digest and the byte count, exactly as
    :class:`redirect_store.RedirectRequest` does.
    """

    operation_id: str
    submitted_at: str
    submitted_seq: int
    feedback_digest: str
    feedback_bytes: int
    text: str = field(repr=False)
    recovered: bool = False
    """Whether this correction reached the prompt across a crash.

    Recorded so an event can distinguish a correction the run accepted itself
    from one it picked up from a dead predecessor. The two are the same words to
    the worker and a very different story to whoever is reading the run back.
    """

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "submitted_at": self.submitted_at,
            "submitted_seq": self.submitted_seq,
            "feedback_digest": self.feedback_digest,
            "feedback_bytes": self.feedback_bytes,
            "recovered": self.recovered,
        }


@dataclass(frozen=True)
class RefusedCorrection:
    """One candidate the journal would not carry, and why."""

    operation_id: str
    reason: str
    message: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "reason": self.reason,
            "message": self.message,
        }


@dataclass(frozen=True)
class CarriedCorrections:
    """Everything one continuation attempt carries, in submission order."""

    continuation: redirect_store.RedirectTarget
    corrections: tuple[Correction, ...] = ()
    refused: tuple[RefusedCorrection, ...] = ()

    @property
    def attempt(self) -> str:
        return attempt_token(self.continuation)

    def __bool__(self) -> bool:
        return bool(self.corrections)

    def operation_ids(self) -> tuple[str, ...]:
        return tuple(item.operation_id for item in self.corrections)

    def to_payload(self) -> dict[str, Any]:
        """A payload safe for events and state: digests and counts, never text."""
        return {
            "attempt": self.attempt,
            "continuation": self.continuation.to_payload(),
            "carried": [item.to_payload() for item in self.corrections],
            "carried_count": len(self.corrections),
            "recovered_count": sum(1 for item in self.corrections if item.recovered),
            "feedback_bytes": sum(item.feedback_bytes for item in self.corrections),
            "refused": [item.to_payload() for item in self.refused],
        }


def coalesced_candidates(
    view: redirect_store.RedirectJournalView,
    *,
    continuation: redirect_store.RedirectTarget,
    accepted_operation_ids: Sequence[str] = (),
    recovered_operation_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return the operation ids ``continuation`` should carry, oldest first.

    Two populations merge here, and the merge is by journal sequence rather
    than by which population an id came from. ``accepted_operation_ids`` are
    what the gate accepted while the interrupted turn was alive -- the reason
    this attempt exists. The journal's own ``pending_before`` adds the
    corrections that arrived after that acceptance and before this prompt: they
    were never accepted by anyone, because the turn they named was already
    gone.

    Ordering by ``submitted_seq`` is what preserves the operator's sequence.
    They wrote a second correction knowing what the first said, so a prompt that
    reordered them would be showing a conversation that never happened.

    A correction an *earlier* continuation already carried drops out rather than
    being re-offered. A second correction against an attempt that was itself
    corrected extends the same lineage, so the runner keeps re-presenting the
    whole set of accepted ids; those already delivered are in the pinned
    conversation's own history, and re-rendering them would tell a worker its
    first correction is new while re-reporting a delivered one as refused.

    ``recovered_operation_ids`` is a third population and the only one whose
    members were never in this run at all: corrections a previous run was
    holding when it died, which :mod:`redirect_recovery` established are still
    unfinished and still belong to this session. They merge by journal sequence
    like the rest, so an operator's order survives a crash the same way it
    survives an interruption. The list is a caller's claim rather than something
    inferred here, because "the run that owned this is gone" is a fact about
    locks and processes, not about the journal.
    """
    ranked: dict[str, int] = {}
    for operation_id in (*accepted_operation_ids, *recovered_operation_ids):
        request = view.request(operation_id)
        if request is None or (request.terminal and not request.accepted):
            # An id the gate reported but the journal cannot corroborate is not
            # invented into a correction here: the journal is the authority.
            continue
        if request.carried and not request.continues_attempt(continuation):
            continue
        ranked[operation_id] = request.submitted_seq
    for request in view.pending_before(continuation):
        ranked.setdefault(request.operation_id, request.submitted_seq)
    return tuple(sorted(ranked, key=lambda key: (ranked[key], key)))


def carry_corrections(
    plan_dir: str | Path,
    *,
    continuation: redirect_store.RedirectTarget,
    accepted_operation_ids: Sequence[str] = (),
    recovered_operation_ids: Sequence[str] = (),
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> CarriedCorrections:
    """Bind every correction due to ``continuation`` and read their text.

    Called once, at the point the resumed prompt is finalized. Per-id refusals
    are collected rather than raised: one expired correction must not cost the
    attempt the three good ones beside it, and a refusal an operator can see in
    an event is strictly better than a prompt that was never built. A journal
    that cannot be read or written at all still raises, because then nothing
    about the attempt is trustworthy.

    ``recovered_operation_ids`` are the ones this attempt is picking up from a
    run that died holding them. They are the only ids carried with
    ``allow_recovery``, and the permission is granted per id rather than for the
    whole call: a correction that merely happens to name another run is still
    refused, so a caller cannot widen the rule for the batch by naming one
    genuine recovery in it.
    """
    redirect_store.validate_target(continuation)
    view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
    candidates = coalesced_candidates(
        view,
        continuation=continuation,
        accepted_operation_ids=accepted_operation_ids,
        recovered_operation_ids=recovered_operation_ids,
    )
    recovered = frozenset(recovered_operation_ids)
    carried: list[Correction] = []
    refused: list[RefusedCorrection] = []
    for operation_id in candidates:
        try:
            request = redirect_store.carry_request(
                plan_dir,
                operation_id,
                continuation=continuation,
                state_dir=state_dir,
                now=now,
                allow_recovery=operation_id in recovered,
            )
        except redirect_store.RedirectStoreError as error:
            refused.append(
                RefusedCorrection(
                    operation_id=operation_id,
                    reason=error.reason,
                    message=str(error),
                )
            )
            continue
        text = redirect_store.read_feedback(
            plan_dir,
            operation_id,
            state_dir=state_dir,
        )
        if text is None:
            # Carried but unreadable: the journal recorded delivery of words it
            # cannot produce. Reporting it as refused keeps the prompt honest
            # about what it is showing.
            refused.append(
                RefusedCorrection(
                    operation_id=operation_id,
                    reason=redirect_store.ERROR_INVALID_FEEDBACK,
                    message="carried redirect request has no readable feedback",
                )
            )
            continue
        carried.append(
            Correction(
                operation_id=operation_id,
                submitted_at=request.created_at,
                submitted_seq=request.submitted_seq,
                feedback_digest=request.feedback_digest,
                feedback_bytes=request.feedback_bytes,
                text=text,
                recovered=request.continuation_recovered,
            )
        )
    return CarriedCorrections(
        continuation=continuation,
        corrections=tuple(carried),
        refused=tuple(refused),
    )


def carried_corrections(
    plan_dir: str | Path,
    *,
    attempt: redirect_store.RedirectTarget,
    state_dir: str | Path | None = None,
) -> CarriedCorrections:
    """Re-read what ``attempt`` already carries, appending nothing.

    The idempotent read behind a re-rendered prompt: a turn that is prepared
    twice -- a retried launch, a diagnostic rebuild -- must show the same
    corrections rather than sweep up whatever arrived in between.
    """
    redirect_store.validate_target(attempt)
    view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
    carried: list[Correction] = []
    refused: list[RefusedCorrection] = []
    for request in view.carried_for(attempt):
        text = redirect_store.read_feedback(
            plan_dir,
            request.operation_id,
            state_dir=state_dir,
        )
        if text is None:
            refused.append(
                RefusedCorrection(
                    operation_id=request.operation_id,
                    reason=redirect_store.ERROR_INVALID_FEEDBACK,
                    message="carried redirect request has no readable feedback",
                )
            )
            continue
        carried.append(
            Correction(
                operation_id=request.operation_id,
                submitted_at=request.created_at,
                submitted_seq=request.submitted_seq,
                feedback_digest=request.feedback_digest,
                feedback_bytes=request.feedback_bytes,
                text=text,
                recovered=request.continuation_recovered,
            )
        )
    return CarriedCorrections(
        continuation=attempt,
        corrections=tuple(carried),
        refused=tuple(refused),
    )


# ---------------------------------------------------------------------------
# Worker receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Acknowledgment:
    """One worker receipt, as the surface that recorded it reports it."""

    operation_id: str
    attempt: str
    acknowledged_at: str
    acknowledged_seq: int
    feedback_digest: str
    repeated: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "attempt": self.attempt,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_seq": self.acknowledged_seq,
            "feedback_digest": self.feedback_digest,
            "repeated": self.repeated,
        }


def acknowledge_correction(
    plan_dir: str | Path,
    operation_id: str,
    *,
    token: str,
    live_attempt: redirect_store.RedirectTarget | None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> Acknowledgment:
    """Record a worker's receipt, checking both halves of its identity.

    ``token`` is what the worker was handed in its prompt; ``live_attempt`` is
    the attempt the run's own state says is running right now, or ``None`` when
    no run can be shown to be live. The first check is this module's: a token
    that does not name the live attempt is refused, which is what a stale worker
    from the interrupted turn and a transcript replaying an older prompt both
    present. The second check is the journal's:
    :func:`redirect_store.acknowledge_request` refuses an attempt it did not
    record as the carrier.

    A missing live attempt is a refusal rather than a pass. Without one there is
    nothing to compare the token against, and "the run state was unreadable"
    must not be the way an unverifiable receipt gets written.
    """
    if live_attempt is None:
        raise RedirectAckError(
            ERROR_NO_LIVE_ATTEMPT,
            "no live attempt is recorded, so a receipt cannot be verified",
            details={"operation_id": operation_id, "attempt": token},
        )
    if not token_identifies(token, live_attempt):
        raise RedirectAckError(
            ERROR_ATTEMPT_MISMATCH,
            "acknowledgment names an attempt that is not the live one",
            details={
                "operation_id": operation_id,
                "attempt": token,
                "live_attempt": attempt_token(live_attempt),
            },
        )
    before = redirect_store.read_journal(plan_dir, state_dir=state_dir).request(
        operation_id
    )
    already = before is not None and before.acknowledged
    request = redirect_store.acknowledge_request(
        plan_dir,
        operation_id,
        attempt=live_attempt,
        state_dir=state_dir,
        now=now,
    )
    return Acknowledgment(
        operation_id=operation_id,
        attempt=attempt_token(live_attempt),
        acknowledged_at=str(request.acknowledged_at),
        acknowledged_seq=int(request.acknowledged_seq or 0),
        feedback_digest=request.feedback_digest,
        repeated=already,
    )


# ---------------------------------------------------------------------------
# Observational wait
# ---------------------------------------------------------------------------

#: Default patience for an observer. Short, because the answer is a report
#: rather than a decision: nothing downstream of this wait changes behaviour
#: when it ends empty.
DEFAULT_ACK_WAIT_SECONDS = 30.0
MAX_ACK_WAIT_SECONDS = 15.0 * 60.0
ACK_WAIT_POLL_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class AcknowledgmentWait:
    """What an observer saw, and what it deliberately did not do."""

    attempt: str
    waited_seconds: float
    acknowledged: tuple[str, ...] = ()
    outstanding: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.outstanding

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "waited_seconds": round(self.waited_seconds, 3),
            "acknowledged": list(self.acknowledged),
            "outstanding": list(self.outstanding),
            "complete": self.complete,
            # Stated in the payload rather than left to a reader's assumption:
            # every consumer of this record has to know that an incomplete wait
            # retracted nothing and interrupted no one.
            "observational": True,
        }


def wait_for_acknowledgment(
    plan_dir: str | Path,
    *,
    attempt: redirect_store.RedirectTarget,
    operation_ids: Sequence[str] = (),
    timeout_seconds: float = DEFAULT_ACK_WAIT_SECONDS,
    state_dir: str | Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> AcknowledgmentWait:
    """Watch the journal until every named correction is receipted, or give up.

    Purely observational, in three specific senses that together are the
    session's rule: the journal is opened read-only so nothing is written; no
    cancellation is consulted or signalled, so a timeout cannot retract the
    redirect; and the result carries no failure, so no caller can mistake
    "not yet acknowledged" for "must interrupt again". The worker keeps the
    correction either way -- it is durable in the journal and already in the
    prompt.
    """
    redirect_store.validate_target(attempt)
    deadline_seconds = max(0.0, min(float(timeout_seconds), MAX_ACK_WAIT_SECONDS))
    wanted = tuple(dict.fromkeys(operation_ids))
    started = monotonic()
    while True:
        view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
        # An explicit id list is watched even when the journal has no record of
        # it: an id that never appears stays outstanding rather than quietly
        # dropping out of the report.
        requests = (
            [view.request(operation_id) for operation_id in wanted]
            if wanted
            else list(view.carried_for(attempt))
        )
        watched = wanted or tuple(
            request.operation_id for request in requests if request is not None
        )
        acknowledged = tuple(
            request.operation_id
            for request in requests
            if request is not None
            and request.acknowledged
            and request.continues_attempt(attempt)
        )
        outstanding = tuple(
            operation_id for operation_id in watched if operation_id not in acknowledged
        )
        elapsed = monotonic() - started
        if not outstanding or elapsed >= deadline_seconds:
            return AcknowledgmentWait(
                attempt=attempt_token(attempt),
                waited_seconds=max(0.0, elapsed),
                acknowledged=acknowledged,
                outstanding=outstanding,
            )
        remaining = deadline_seconds - elapsed
        sleep(min(ACK_WAIT_POLL_INTERVAL_SECONDS, remaining))


def acknowledgment_summary(
    plan_dir: str | Path,
    *,
    attempt: redirect_store.RedirectTarget,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Report one attempt's receipt state without waiting or writing."""
    view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
    carried = view.carried_for(attempt)
    return {
        "attempt": attempt_token(attempt),
        "carried": [request.operation_id for request in carried],
        "acknowledged": [
            request.operation_id for request in carried if request.acknowledged
        ],
        "outstanding": [
            request.operation_id for request in carried if not request.acknowledged
        ],
        "read_at": clock.isoformat(clock.utc_now()),
    }
