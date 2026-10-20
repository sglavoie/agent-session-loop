"""Worker receipts for carried corrections, and the observational wait.

A correction that was carried into a prompt and delivered is still only
*delivered*. What counts as the worker having received it is an explicit receipt
from the worker, naming the correction *and* the attempt it is running:
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

Attempt identity and the refusals live in :mod:`redirect_ack_attempt`, the
coalescing of corrections in :mod:`redirect_ack_carry`, and the launch, delivery
and release declarations in :mod:`redirect_ack_annotate`.

Nothing here imports the runner or ``run_loop``, and nothing here owns a loop
except the observational wait above.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    redirect_ack_attempt,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_validation,
)


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
    live_attempt: redirect_store_models.RedirectTarget | None,
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
    :func:`redirect_store_delivery.acknowledge_request` refuses an attempt it did not
    record as the carrier.

    A missing live attempt is a refusal rather than a pass. Without one there is
    nothing to compare the token against, and "the run state was unreadable"
    must not be the way an unverifiable receipt gets written.
    """
    if live_attempt is None:
        raise redirect_ack_attempt.RedirectAckError(
            redirect_ack_attempt.ERROR_NO_LIVE_ATTEMPT,
            "no live attempt is recorded, so a receipt cannot be verified",
            details={"operation_id": operation_id, "attempt": token},
        )
    if not redirect_ack_attempt.token_identifies(token, live_attempt):
        raise redirect_ack_attempt.RedirectAckError(
            redirect_ack_attempt.ERROR_ATTEMPT_MISMATCH,
            "acknowledgment names an attempt that is not the live one",
            details={
                "operation_id": operation_id,
                "attempt": token,
                "live_attempt": redirect_ack_attempt.attempt_token(live_attempt),
            },
        )
    before = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir).request(
        operation_id
    )
    already = before is not None and before.acknowledged
    request = redirect_store_delivery.acknowledge_request(
        plan_dir,
        operation_id,
        attempt=live_attempt,
        state_dir=state_dir,
        now=now,
    )
    return Acknowledgment(
        operation_id=operation_id,
        attempt=redirect_ack_attempt.attempt_token(live_attempt),
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
    attempt: redirect_store_models.RedirectTarget,
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
    redirect_store_validation.validate_target(attempt)
    deadline_seconds = max(0.0, min(float(timeout_seconds), MAX_ACK_WAIT_SECONDS))
    wanted = tuple(dict.fromkeys(operation_ids))
    started = monotonic()
    while True:
        view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
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
                attempt=redirect_ack_attempt.attempt_token(attempt),
                waited_seconds=max(0.0, elapsed),
                acknowledged=acknowledged,
                outstanding=outstanding,
            )
        remaining = deadline_seconds - elapsed
        sleep(min(ACK_WAIT_POLL_INTERVAL_SECONDS, remaining))


def acknowledgment_summary(
    plan_dir: str | Path,
    *,
    attempt: redirect_store_models.RedirectTarget,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Report one attempt's receipt state without waiting or writing."""
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    carried = view.carried_for(attempt)
    return {
        "attempt": redirect_ack_attempt.attempt_token(attempt),
        "carried": [request.operation_id for request in carried],
        "acknowledged": [
            request.operation_id for request in carried if request.acknowledged
        ],
        "outstanding": [
            request.operation_id for request in carried if not request.acknowledged
        ],
        "read_at": clock.isoformat(clock.utc_now()),
    }
