"""Value types for the redirect journal: errors, targets, requests, and views.

Split out of the original ``redirect_store`` module; the journal's design and
its guarantees are described in :mod:`agent_session_loop.redirect_store_vocab`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import clock
from .redirect_store_vocab import (
    STATE_ACCEPTED,
    STATE_SUBMITTED,
    TERMINAL_STATES,
    RequestState,
    SettlementReason,
)


class RedirectStoreError(Exception):
    """A refused journal operation carrying a stable, surface-agnostic reason."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        path: Path | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.path = path
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "path": str(self.path) if self.path is not None else None,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class RedirectTarget:
    """The exact turn a correction is bound to.

    Identity is the ``(run_id, session_id, turn_index)`` triple and nothing
    else. ``harness`` is recorded because an operator reading the journal wants
    to know what was interrupted, but it is deliberately excluded from
    :meth:`identifies_same_turn`: the run loop may re-select a future harness
    between submission and acceptance, and that changes what runs the turn
    rather than which turn it is.
    """

    run_id: str
    session_id: str
    turn_index: int
    harness: str | None = None

    def identifies_same_turn(self, other: RedirectTarget) -> bool:
        return (
            self.run_id == other.run_id
            and self.session_id == other.session_id
            and self.turn_index == other.turn_index
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "harness": self.harness,
        }


@dataclass(frozen=True)
class RedirectRequest:
    """One request as the journal's replay currently understands it.

    Carries no feedback text by construction. ``feedback_digest`` is what lets
    a projection, a receipt, or an operator prove *which* correction this is
    without any surface but :func:`read_feedback` ever holding the words.

    ``continuation`` is the attempt that bound this correction into the prompt
    it was assembling, recorded when the run finalizes that prompt. It is what
    makes a worker receipt checkable: acknowledging a correction means naming
    the attempt it was delivered in, and an attempt that never carried it -- a
    stale predecessor, a transcript echo of an older attempt -- names the wrong
    one.

    ``delivered_at`` is the strictly later fact that the attempt named by
    ``continuation`` actually launched a worker with that prompt. Between the
    two marks sits every way an attempt can die before submitting: a prompt
    build that refuses, a conversation the run cannot prove is usable, a crash.
    ``released_at`` is that window's undo -- it withdraws a carry that never
    reached a worker and puts the correction back in front of the next attempt.

    ``launching_at`` cuts that window in two. It is appended at the last instant
    before the prompt can leave the process, so a carry without it cannot have
    been submitted and a carry with it but no delivery is a question nobody can
    answer from the file alone: the writer either never got the prompt out or
    got it out and died before saying so. :attr:`launch_ambiguous` is that
    second case, and it is the difference between a correction the next attempt
    may take back and one only an operator may resolve.

    On a version-1 journal the carry and the delivery were one mark, so
    ``delivered_at`` is read off the ``continued`` line and ``delivered_legacy``
    says so. The flag changes no decision here -- a legacy delivery is a
    delivery -- and exists because an operator asking what became of their words
    deserves to know the delivery is the old format's single mark rather than an
    observed launch. ``launch_unrecorded`` is the same courtesy one version
    later: a version-2 writer had no ``launching`` line to append, so its silence
    is not evidence, and the flag is what stops that silence from being read as
    one.
    """

    operation_id: str
    target: RedirectTarget
    state: RequestState
    created_at: str
    expires_at: str
    feedback_bytes: int
    feedback_digest: str
    submitted_seq: int
    settled_at: str | None = None
    settled_seq: int | None = None
    reason: SettlementReason | None = None
    continuation: RedirectTarget | None = None
    continued_at: str | None = None
    continued_seq: int | None = None
    continuation_recovered: bool = False
    launching_at: str | None = None
    launching_seq: int | None = None
    launch_unrecorded: bool = False
    delivered_at: str | None = None
    delivered_seq: int | None = None
    delivered_legacy: bool = False
    released_at: str | None = None
    released_seq: int | None = None
    released_from: RedirectTarget | None = None
    acknowledged_at: str | None = None
    acknowledged_seq: int | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def accepted(self) -> bool:
        return self.state == STATE_ACCEPTED

    @property
    def carried(self) -> bool:
        """Whether a continuation attempt has bound this correction into a prompt.

        This is an undertaking, not an outcome. The attempt named by
        :attr:`continuation` promised to put these words in front of a worker;
        :attr:`delivered` is what says it got that far.
        """
        return self.continuation is not None

    @property
    def launching(self) -> bool:
        """Whether the carrying attempt declared the prompt about to go out.

        The mark is appended before the submission it describes, so it never
        means the worker got the words -- only that this process was past the
        last point at which it could still promise it had not sent them.
        """
        return self.launching_at is not None

    @property
    def delivered(self) -> bool:
        """Whether the carrying attempt actually launched a worker with it.

        True for a version-1 carry as well: that format recorded the carry only
        once the worker was up, so the ``continued`` line is the launch.
        """
        return self.delivered_at is not None

    @property
    def launch_ambiguous(self) -> bool:
        """Whether this carry's fate cannot be read off the journal.

        True when the words were declared about to go out and nothing since
        says whether they did, and true for a carry written before ``launching``
        existed, whose writer could not have declared it either way. Both are
        the same fact for every caller: an automatic decision here would be a
        guess, so the carry is neither released nor redelivered and is put in
        front of an operator instead.

        False once the attempt itself settled the question -- a ``delivered``
        or a ``released`` line is a statement by a live process, not a silence.
        """
        if not self.carried or self.reached_a_worker:
            return False
        return self.launching or self.launch_unrecorded

    @property
    def released(self) -> bool:
        """Whether a carry was ever withdrawn because its attempt never launched.

        Releasing clears the continuation, so ``released_from`` is the only
        trace the withdrawn carry leaves. The trace matters because "never
        carried" and "carried, then released" answer the same question for the
        next attempt but a different one for an operator asking what became of
        their words. It survives a later re-carry for the same reason.
        """
        return self.released_at is not None

    @property
    def reached_a_worker(self) -> bool:
        """Whether any worker was launched with this correction, or receipted it.

        The question behind every "is this still owed to someone" decision. A
        receipt implies delivery even on a journal written before delivery was
        recorded, which is why this is a disjunction rather than a read of
        :attr:`delivered` alone.
        """
        return self.delivered or self.acknowledged

    @property
    def recovered(self) -> bool:
        """Whether the attempt that carried this correction crossed a crash.

        A continuation of the ordinary kind is a later turn of the *same* run,
        so the marker is only ever true for a correction a new run picked up
        from a run that died holding it. It is kept as its own fact rather than
        inferred from a run-id comparison because the inference would also fire
        on a hand-written line, and the whole point of the marker is that a
        cross-run delivery must have been asked for explicitly.
        """
        return self.continuation_recovered

    @property
    def acknowledged(self) -> bool:
        """Whether a worker receipted this correction on its own attempt."""
        return self.acknowledged_at is not None

    def continues_attempt(self, attempt: RedirectTarget) -> bool:
        """Whether ``attempt`` is the continuation this correction was carried in."""
        return self.continuation is not None and self.continuation.identifies_same_turn(
            attempt
        )

    def is_expired_at(self, moment: datetime) -> bool:
        """Whether this request's acceptance window has closed by ``moment``.

        An unparsable ``expires_at`` reads as expired. The alternative is to
        treat a timestamp we cannot evaluate as open-ended, which would let a
        corrupt record redirect a turn indefinitely.
        """
        deadline = clock.parse_datetime(self.expires_at)
        if deadline is None:
            return True
        return moment >= deadline

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "feedback_bytes": self.feedback_bytes,
            "feedback_digest": self.feedback_digest,
            "submitted_seq": self.submitted_seq,
            "settled_at": self.settled_at,
            "settled_seq": self.settled_seq,
            "reason": self.reason,
            "continuation": (
                self.continuation.to_payload()
                if self.continuation is not None
                else None
            ),
            "continued_at": self.continued_at,
            "continued_seq": self.continued_seq,
            "continuation_recovered": self.continuation_recovered,
            "launching_at": self.launching_at,
            "launching_seq": self.launching_seq,
            "launch_unrecorded": self.launch_unrecorded,
            "launch_ambiguous": self.launch_ambiguous,
            "delivered_at": self.delivered_at,
            "delivered_seq": self.delivered_seq,
            "delivered_legacy": self.delivered_legacy,
            "released_at": self.released_at,
            "released_seq": self.released_seq,
            "released_from": (
                self.released_from.to_payload()
                if self.released_from is not None
                else None
            ),
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_seq": self.acknowledged_seq,
            **self.target.to_payload(),
        }


@dataclass(frozen=True)
class RejectedRecord:
    """One physical line replay could not apply.

    Unlike the mailbox's equivalent this keeps no copy of the raw line. A line
    rejected for a malformed target can still hold a complete, valid feedback
    body, and a projection that echoed it would put the operator's words in
    every surface this module exists to keep them out of.
    """

    seq: int
    reason: str
    detail: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"seq": self.seq, "reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class RedirectJournalView:
    """The replayed journal: every request, plus what could not be replayed."""

    journal_path: Path
    requests: tuple[RedirectRequest, ...] = ()
    rejected: tuple[RejectedRecord, ...] = field(default=(), repr=False)
    last_seq: int = 0
    size_bytes: int = 0
    torn_tail: bool = False
    """Whether the file's last physical line was never terminated.

    A writer that died between ``write`` and its ``fullsync`` -- or a device
    that lost the tail of a page -- leaves a final line with no newline after
    it. Replay already refuses that line, and the next append terminates it
    rather than merging with it, so nothing here is broken. What the flag adds
    is that the refusal is *attributable*: a projection built after a crash can
    say "one record may be missing from this answer" instead of presenting a
    possibly-truncated history as the whole truth. Mid-file corruption shows up
    in :attr:`rejected` instead, which is why the two are separate.
    """

    def request(self, operation_id: str) -> RedirectRequest | None:
        for request in self.requests:
            if request.operation_id == operation_id:
                return request
        return None

    def pending(self) -> tuple[RedirectRequest, ...]:
        """Requests still awaiting an answer, oldest first."""
        return tuple(
            request for request in self.requests if request.state == STATE_SUBMITTED
        )

    def pending_for(self, target: RedirectTarget) -> tuple[RedirectRequest, ...]:
        return tuple(
            request
            for request in self.pending()
            if request.target.identifies_same_turn(target)
        )

    def pending_before(
        self,
        continuation: RedirectTarget,
    ) -> tuple[RedirectRequest, ...]:
        """Unsettled requests ``continuation`` may still carry, oldest first.

        The coalescing boundary, read rather than computed by a caller: a
        correction aimed at an earlier attempt of this run's same session was
        submitted before this prompt was finalized, so it belongs in it. One
        aimed at this attempt is a redirect against a turn that is about to be
        live and is left alone.
        """
        return tuple(
            request
            for request in self.pending()
            if _precedes_continuation(request.target, continuation)
        )

    def carried_for(self, attempt: RedirectTarget) -> tuple[RedirectRequest, ...]:
        """Requests this exact continuation attempt carried, oldest first."""
        return tuple(
            request for request in self.requests if request.continues_attempt(attempt)
        )

    def strandable_by(self, run_id: str) -> tuple[RedirectRequest, ...]:
        """Requests a crash left unfinished for some *other* run, oldest first.

        "Unfinished" is exactly two states: never settled, and settled as
        accepted but never put in front of a worker. Both describe a correction
        the operator was promised something about and whose promise no process
        is keeping any more, because the run named in the target is not this
        one.

        A *delivered* request is excluded on purpose even when nobody receipted
        it. The journal records that a worker was launched holding it, and the
        whole acknowledgment protocol exists because delivery is not receipt:
        deciding here that a launched delivery never happened would be the guess
        that protocol refuses to make. :mod:`redirect_recovery` reports those as
        uncertain instead. So is a carry whose attempt declared the prompt about
        to go out and then said nothing: the declaration is there precisely so
        that silence cannot be read as "nothing ran". A carry with no such
        declaration is the different animal -- the words provably never left the
        dead process -- and stays strandable so they can be recovered.
        """
        return tuple(
            request
            for request in self.requests
            if request.target.run_id != run_id
            and not request.reached_a_worker
            and not request.launch_ambiguous
            and (not request.terminal or request.accepted)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "journal_path": str(self.journal_path),
            "requests": [request.to_payload() for request in self.requests],
            "rejected": [record.to_payload() for record in self.rejected],
            "last_seq": self.last_seq,
            "size_bytes": self.size_bytes,
            "torn_tail": self.torn_tail,
        }


def _precedes_continuation(
    target: RedirectTarget,
    continuation: RedirectTarget,
) -> bool:
    """Whether ``target`` names an earlier attempt of ``continuation``'s session.

    The turn index has to be strictly lower. A correction is carried forward
    into the attempt that *succeeds* the one it interrupted, so a continuation
    that claimed the same index as the request's own target would be describing
    the interrupted turn as its own successor.
    """
    return (
        target.run_id == continuation.run_id
        and target.session_id == continuation.session_id
        and target.turn_index < continuation.turn_index
    )


def _recovers_continuation(
    target: RedirectTarget,
    continuation: RedirectTarget,
) -> bool:
    """Whether ``continuation`` is a *different run's* retry of ``target``'s session.

    The turn index is deliberately not compared. A run numbers its turns from
    one, so a correction aimed at turn 7 of a run that died is legitimately
    carried by turn 1 of the run that resumes the same session, and requiring a
    greater index would refuse every real crash recovery there is. What still
    has to hold is the session: a correction is about the work of one session,
    and carrying it into another is delivering it to a worker it was not
    written for.

    Requiring a *different* run id is what keeps this from widening the ordinary
    rule. Within one run the strict ordering of :func:`_precedes_continuation`
    remains the only way a correction moves forward.
    """
    return (
        target.session_id == continuation.session_id
        and target.run_id != continuation.run_id
    )
