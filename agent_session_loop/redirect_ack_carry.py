"""Coalescing corrections into a continuation prompt.

Session 11 put an accepted correction into the *next attempt* of the same
session. It left open which corrections belong in the prompt, and this module
answers that.

An operator who interrupts a turn usually keeps typing: a second correction
lands while the predecessor is being collected, a third while the resumed prompt
is being built. Each of them was written about the same situation, so all of
them belong in the one prompt, in the order they were sent.
:func:`carry_corrections` is that boundary and it is a *moment*, not a
heuristic: everything unsettled against an earlier attempt of this session at
the instant the prompt is finalized is carried and marked ``continued`` in the
journal, and anything submitted after that instant is a redirect against the
attempt the prompt is about to start -- which is the existing session-11 path,
not a late edit to a prompt already written.

What happens to a carry afterwards lives in :mod:`redirect_ack_annotate`; the
worker's receipt lives in :mod:`redirect_ack`.

Nothing here imports the runner or ``run_loop``, and nothing here owns a loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    redirect_ack_attempt,
    redirect_store_carry,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_validation,
    redirect_store_vocab,
)


@dataclass(frozen=True)
class Correction:
    """One correction as a prompt is about to render it.

    This is the one object in the redirect subsystem that holds the operator's
    words, and it exists because a prompt has to say them. ``to_payload`` omits
    the text on purpose: an event, a state file, or a diagnostic bundle built
    from this object carries the digest and the byte count, exactly as
    :class:`redirect_store_models.RedirectRequest` does.
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

    continuation: redirect_store_models.RedirectTarget
    corrections: tuple[Correction, ...] = ()
    refused: tuple[RefusedCorrection, ...] = ()

    @property
    def attempt(self) -> str:
        return redirect_ack_attempt.attempt_token(self.continuation)

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
    view: redirect_store_models.RedirectJournalView,
    *,
    continuation: redirect_store_models.RedirectTarget,
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
    continuation: redirect_store_models.RedirectTarget,
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
    redirect_store_validation.validate_target(continuation)
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
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
            request = redirect_store_carry.carry_request(
                plan_dir,
                operation_id,
                continuation=continuation,
                state_dir=state_dir,
                now=now,
                allow_recovery=operation_id in recovered,
            )
        except redirect_store_models.RedirectStoreError as error:
            refused.append(
                RefusedCorrection(
                    operation_id=operation_id,
                    reason=error.reason,
                    message=str(error),
                )
            )
            continue
        text = redirect_store_journal.read_feedback(
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
                    reason=redirect_store_vocab.ERROR_INVALID_FEEDBACK,
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
    attempt: redirect_store_models.RedirectTarget,
    state_dir: str | Path | None = None,
) -> CarriedCorrections:
    """Re-read what ``attempt`` already carries, appending nothing.

    The idempotent read behind a re-rendered prompt: a turn that is prepared
    twice -- a retried launch, a diagnostic rebuild -- must show the same
    corrections rather than sweep up whatever arrived in between.
    """
    redirect_store_validation.validate_target(attempt)
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    carried: list[Correction] = []
    refused: list[RefusedCorrection] = []
    for request in view.carried_for(attempt):
        text = redirect_store_journal.read_feedback(
            plan_dir,
            request.operation_id,
            state_dir=state_dir,
        )
        if text is None:
            refused.append(
                RefusedCorrection(
                    operation_id=request.operation_id,
                    reason=redirect_store_vocab.ERROR_INVALID_FEEDBACK,
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
