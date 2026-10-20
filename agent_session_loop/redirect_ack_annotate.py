"""Settling what became of the corrections an attempt carried.

Finalizing a prompt is not the same as running one, and the gap between them is
where an attempt can still refuse or die. :func:`deliver_corrections` closes
that gap on the success side -- the worker was launched holding these words --
and :func:`release_corrections` closes it on the other, handing a carry back to
the next attempt when this one never got as far as a process. Without the pair,
a refusal after the coalescing boundary reads exactly like a delivery nobody
receipted, and the operator's words are spent on a turn that never happened.

Neither of those two is written by an attempt that *stopped existing* in the
gap, which is why :func:`launch_corrections` sits between them. It is appended
while the words demonstrably have not gone out, and it is the only reason a
later reader can tell "this attempt died before submitting" from "this attempt
died just after submitting". Before it, silence is safe to act on; after it,
silence is a question for an operator. A caller that cannot append it has to
abandon the prompt rather than send it, because a submission it could not
declare is one nothing can ever account for.

The carry itself is made by :func:`redirect_ack_carry.carry_corrections`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import (
    redirect_ack_attempt,
    redirect_ack_carry,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_validation,
    redirect_store_vocab,
)


class _CarryAnnotator(Protocol):
    """The shape :func:`redirect_store_delivery.deliver_request` and kin share."""

    def __call__(
        self,
        plan_dir: str | Path,
        operation_id: str,
        *,
        continuation: redirect_store_models.RedirectTarget,
        state_dir: str | Path | None = None,
        now: datetime | None = None,
    ) -> redirect_store_models.RedirectRequest: ...


@dataclass(frozen=True)
class CarryAnnotations:
    """The outcome of settling what became of one attempt's carries.

    Several facts share this shape because they are the successive answers to
    the same question -- did the attempt that bound these corrections into a
    prompt actually put them in front of a worker -- and a caller reporting any
    of them wants the same three things: which attempt, which ids moved, and
    which the journal would not move.
    """

    continuation: redirect_store_models.RedirectTarget
    kind: str
    operation_ids: tuple[str, ...] = ()
    refused: tuple[redirect_ack_carry.RefusedCorrection, ...] = ()

    @property
    def attempt(self) -> str:
        return redirect_ack_attempt.attempt_token(self.continuation)

    def __bool__(self) -> bool:
        return bool(self.operation_ids)

    def to_payload(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "continuation": self.continuation.to_payload(),
            "kind": self.kind,
            "operation_ids": list(self.operation_ids),
            "count": len(self.operation_ids),
            "refused": [item.to_payload() for item in self.refused],
        }


def _carry_annotation_targets(
    plan_dir: str | Path,
    *,
    continuation: redirect_store_models.RedirectTarget,
    operation_ids: Sequence[str] | None,
    state_dir: str | Path | None,
) -> tuple[str, ...]:
    """Resolve which ids an annotation pass should attempt, oldest first.

    ``None`` means "whatever the journal says this attempt is holding", which is
    what a recovery path has to ask: the process that knew the ids is the one
    that died. An explicit sequence is kept verbatim, including ids the journal
    disagrees about, so the per-id refusal reaches the caller instead of the id
    silently vanishing from a list they supplied.
    """
    if operation_ids is not None:
        return tuple(operation_ids)
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    return tuple(request.operation_id for request in view.carried_for(continuation))


def launch_corrections(
    plan_dir: str | Path,
    *,
    continuation: redirect_store_models.RedirectTarget,
    operation_ids: Sequence[str] | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> CarryAnnotations:
    """Declare that ``continuation`` is about to submit a prompt holding these.

    The last durable statement an attempt can make while it is still true that
    the words have not gone out. Everything after it is the window a crash makes
    unreadable, and the declaration is what tells a later reader that the window
    was entered at all.

    Unlike its siblings this one's refusals are *not* advisory. A caller that
    cannot get these lines down must not submit the prompt: doing so would put
    the correction in a worker's hands with nothing on disk to stop the next run
    from handing it over a second time. Refusals are still collected rather than
    raised, because the caller has to decide what to do about a partial pass --
    some ids declared, some not -- and an exception would hide which is which.
    """
    return _annotate_carries(
        plan_dir,
        continuation=continuation,
        operation_ids=operation_ids,
        kind=redirect_store_vocab.KIND_LAUNCHING,
        annotate=redirect_store_carry.launch_request,
        state_dir=state_dir,
        now=now,
    )


def deliver_corrections(
    plan_dir: str | Path,
    *,
    continuation: redirect_store_models.RedirectTarget,
    operation_ids: Sequence[str] | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> CarryAnnotations:
    """Record that ``continuation`` launched a worker holding these corrections.

    Called on the launch side of the window
    :func:`redirect_ack_carry.carry_corrections` opens. Up to here the journal
    only knows an attempt *undertook* to deliver the words; from here it knows a
    process was started with them, which is what makes stranding them afterwards
    a duplicate rather than a rescue.

    Per-id refusals are collected rather than raised, for the same reason they
    are in :func:`redirect_ack_carry.carry_corrections`: the worker is already
    running, and an exception thrown at that point would abandon a turn over a
    bookkeeping line.
    """
    return _annotate_carries(
        plan_dir,
        continuation=continuation,
        operation_ids=operation_ids,
        kind=redirect_store_vocab.KIND_DELIVERED,
        annotate=redirect_store_delivery.deliver_request,
        state_dir=state_dir,
        now=now,
    )


def release_corrections(
    plan_dir: str | Path,
    *,
    continuation: redirect_store_models.RedirectTarget,
    operation_ids: Sequence[str] | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
) -> CarryAnnotations:
    """Give back the corrections ``continuation`` bound but never launched with.

    The call an attempt owes the operator when it gives up between finalizing a
    prompt and starting a worker -- a prompt build that refuses, a conversation
    it cannot prove is usable, an authorized pause. Released corrections are
    accepted and uncarried again, so the next attempt coalesces them normally
    and a reader after a crash finds them recoverable rather than spent.

    Refusals are collected, and a correction the journal has already delivered
    or seen receipted is one of them: it is no longer this attempt's to give
    back.
    """
    return _annotate_carries(
        plan_dir,
        continuation=continuation,
        operation_ids=operation_ids,
        kind=redirect_store_vocab.KIND_RELEASED,
        annotate=redirect_store_delivery.release_request,
        state_dir=state_dir,
        now=now,
    )


def _annotate_carries(
    plan_dir: str | Path,
    *,
    continuation: redirect_store_models.RedirectTarget,
    operation_ids: Sequence[str] | None,
    kind: str,
    annotate: _CarryAnnotator,
    state_dir: str | Path | None,
    now: datetime | None,
) -> CarryAnnotations:
    redirect_store_validation.validate_target(continuation)
    annotated: list[str] = []
    refused: list[redirect_ack_carry.RefusedCorrection] = []
    for operation_id in _carry_annotation_targets(
        plan_dir,
        continuation=continuation,
        operation_ids=operation_ids,
        state_dir=state_dir,
    ):
        try:
            annotate(
                plan_dir,
                operation_id,
                continuation=continuation,
                state_dir=state_dir,
                now=now,
            )
        except redirect_store_models.RedirectStoreError as error:
            refused.append(
                redirect_ack_carry.RefusedCorrection(
                    operation_id=operation_id,
                    reason=error.reason,
                    message=str(error),
                )
            )
            continue
        annotated.append(operation_id)
    return CarryAnnotations(
        continuation=continuation,
        kind=kind,
        operation_ids=tuple(annotated),
        refused=tuple(refused),
    )
