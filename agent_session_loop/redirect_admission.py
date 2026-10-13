"""Admission control between a redirect producer and the runner that serves it.

Submitting a redirect and having one accepted are different events, and this
module exists to keep them different. :mod:`agent_session_loop.redirect_store`
makes a correction durable; only the process holding the live turn can accept
one. A producer that conflated the two would report success for a correction no
runner ever saw — which is the specific way an urgent channel fails an operator,
because the operator stops watching once they are told the message landed.

So a submission has four possible answers and only one of them is good news:

``accepted``
    The runner recorded acceptance in the journal within the wait window. The
    correction is bound to a turn that was live when the runner took it.
``rejected``
    A definite no. Either the pre-checks below refused before anything was
    persisted, or the runner settled the request against.
``unavailable``
    The urgent channel could not carry it. The submission is durably settled as
    ``urgent-channel-unavailable``, so no later runner can pick it up and act on
    a correction the operator was told did not land.
``uncertain``
    The wait elapsed with the request still open. Nothing is settled, because
    settling here would race the runner's own acceptance — the operation id
    comes back so the caller can look, and the journal's expiry is what
    eventually closes it if nobody does.

The pre-checks refuse *before* persisting, and that ordering is deliberate: a
correction refused before submission is still in the producer's hands, whereas
one persisted and then abandoned is the loss this whole subsystem is built to
prevent. Nothing here writes when it means to refuse.

The refusals themselves:

- **No live run, or a paused one.** There is no turn to interrupt. A paused run
  is answered through the mailbox, which is a durable queue built for exactly
  that and does not need interrupting.
- **A stale or mismatched target.** A caller that names a turn re-states which
  turn it meant; if the live turn has moved on, the correction is refused
  rather than re-aimed. Advice written for one turn is not advice for its
  successor, and the runner re-checks this at acceptance for the same reason.
- **A terminal audit.** A ``review`` turn is the run's own verdict on work
  already done. Redirecting it would replay the audit against feedback the
  audited sessions never saw, and would spend review-retry budget on an
  attempt the operator interrupted.
- **A remotely owned or stale lock.** The urgent channel is a local FIFO, so a
  run owned by another host has no reachable endpoint, and a stale lock has no
  live reader behind whatever it names.
- **No urgent endpoint.** A runner that published no redirect FIFO does not
  serve this channel. Falling back to the control queue's drain checkpoint is
  refused rather than taken: a checkpoint is exactly the latency an urgent
  correction cannot spend, so pretending the queue is a substitute would report
  acceptance for a delivery that has not happened.

Every read is non-mutating. Deciding whether an action is permitted must never
be the thing that changes what it is deciding about.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import (
    control_signal,
    intervention,
    plans_files,
    plans_payloads,
    redirect_store,
    state_locks,
)

#: The urgent window, and the single number both halves of it are derived from.
#: It bounds what a producer waits for an answer *and* what the runner's own
#: escalation waits before it force-stops the worker
#: (:data:`agent_session_loop.redirect_control.ESCALATION_WINDOW_SECONDS`),
#: because those are two halves of one promise: a correction reaches a stopped
#: worker within this long, or its author is told it did not. Maintained
#: separately the two could drift into a window where the producer gives up
#: before the runner escalates, which would report "uncertain" for corrections
#: that were about to land.
URGENT_WINDOW_SECONDS = 2.0

#: The longest a producer waits for the runner's answer. Short on purpose: the
#: urgent wakeup reaches a live runner in milliseconds, so a wait that has not
#: resolved by now is not slow, it is unanswered — and telling an operator
#: "uncertain, here is the id" quickly beats telling them "accepted" slowly.
ACCEPTANCE_WAIT_SECONDS = URGENT_WINDOW_SECONDS
ACCEPTANCE_POLL_INTERVAL_SECONDS = 0.05
MAX_ACCEPTANCE_WAIT_SECONDS = 30.0

Outcome = Literal["accepted", "rejected", "unavailable", "uncertain"]
OUTCOME_ACCEPTED: Outcome = "accepted"
OUTCOME_REJECTED: Outcome = "rejected"
OUTCOME_UNAVAILABLE: Outcome = "unavailable"
OUTCOME_UNCERTAIN: Outcome = "uncertain"
OUTCOMES: tuple[Outcome, ...] = (
    OUTCOME_ACCEPTED,
    OUTCOME_REJECTED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_UNCERTAIN,
)

REFUSE_INVALID_PLAN = "invalid-plan"
REFUSE_NO_ACTIVE_RUN = "no-active-run"
REFUSE_RUN_PAUSED = "run-paused"
REFUSE_NO_ACTIVE_TURN = "no-active-turn"
REFUSE_STALE_TARGET = "stale-target"
REFUSE_TERMINAL_AUDIT = "terminal-audit"
REFUSE_REMOTE_OWNER = "remote-owner"
REFUSE_STALE_LOCK = "stale-lock"
REFUSE_URGENT_CHANNEL_UNAVAILABLE = "urgent-channel-unavailable"
REFUSAL_REASONS: tuple[str, ...] = (
    REFUSE_INVALID_PLAN,
    REFUSE_NO_ACTIVE_RUN,
    REFUSE_RUN_PAUSED,
    REFUSE_NO_ACTIVE_TURN,
    REFUSE_STALE_TARGET,
    REFUSE_TERMINAL_AUDIT,
    REFUSE_REMOTE_OWNER,
    REFUSE_STALE_LOCK,
    REFUSE_URGENT_CHANNEL_UNAVAILABLE,
)

#: Refusals that mean "not here, not now" rather than "never". They are
#: reported as ``unavailable`` so a caller can sensibly retry; the rest are
#: ``rejected``, which a caller should not retry unchanged.
UNAVAILABLE_REASONS: tuple[str, ...] = (
    REFUSE_URGENT_CHANNEL_UNAVAILABLE,
    REFUSE_REMOTE_OWNER,
    REFUSE_STALE_LOCK,
)

#: Session kinds whose turn is the run's own audit and may not be redirected.
TERMINAL_AUDIT_SESSION_KINDS: tuple[str, ...] = (plans_payloads.SESSION_KIND_REVIEW,)


class RedirectAdmissionError(Exception):
    """A submission that was malformed rather than merely refused.

    Separate from a refusal outcome on purpose: a refusal is a fact about the
    run, which may be different a second later, while this is a fact about the
    call, which will not be.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TargetFacts:
    """One non-mutating read of whether a run can accept a redirect right now."""

    plan_dir: Path
    target: redirect_store.RedirectTarget | None = None
    session_kind: str | None = None
    run_active: bool = False
    paused: bool = False
    lock_active: bool = False
    urgent_endpoint: str | None = None
    refusal: str | None = None

    @property
    def admissible(self) -> bool:
        return self.refusal is None and self.target is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": str(self.plan_dir),
            "target": self.target.to_payload() if self.target is not None else None,
            "session_kind": self.session_kind,
            "run_active": self.run_active,
            "paused": self.paused,
            "lock_active": self.lock_active,
            "urgent_endpoint": self.urgent_endpoint,
            "refusal": self.refusal,
            "admissible": self.admissible,
        }


@dataclass(frozen=True)
class SubmissionOutcome:
    """What a producer learned, and the identity it can ask about later.

    ``operation_id`` is present whenever anything was persisted, including
    every outcome that is not good news. That is the point: an ``unavailable``
    or ``uncertain`` answer without an identity would leave the operator with
    no way to find out what became of their words.
    """

    outcome: Outcome
    plan_dir: Path
    reason: str | None = None
    operation_id: str | None = None
    request: redirect_store.RedirectRequest | None = None
    target: redirect_store.RedirectTarget | None = None
    delivery: control_signal.NotifyResult | None = field(default=None, repr=False)
    waited_seconds: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.outcome == OUTCOME_ACCEPTED

    @property
    def persisted(self) -> bool:
        return self.operation_id is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "plan_dir": str(self.plan_dir),
            "reason": self.reason,
            "operation_id": self.operation_id,
            "request": self.request.to_payload() if self.request else None,
            "target": self.target.to_payload() if self.target else None,
            "delivery": self.delivery.to_payload() if self.delivery else None,
            "waited_seconds": round(self.waited_seconds, 3),
        }


def _canonical_plan_dir(plan_dir: str | Path) -> Path:
    try:
        return plans_files.canonicalize_path(plan_dir)
    except (OSError, ValueError) as error:
        raise RedirectAdmissionError(
            REFUSE_INVALID_PLAN, f"invalid plan directory {plan_dir!r}: {error}"
        ) from error


def _active_turn_target(
    runner_state: Mapping[str, Any],
    run_id: str,
) -> tuple[redirect_store.RedirectTarget, str | None] | None:
    """Read the live turn's identity, or ``None`` if there is not one.

    Every field is re-validated rather than trusted: this payload is written by
    another process, and a redirect bound to a half-formed target would be
    accepted against whatever the runner happens to be doing.
    """
    active_turn = runner_state.get("active_turn")
    if not isinstance(active_turn, Mapping):
        return None
    session_id = active_turn.get("session_id")
    turn_index = active_turn.get("turn_index")
    harness = active_turn.get("harness")
    if (
        active_turn.get("run_id") != run_id
        or not isinstance(session_id, str)
        or not session_id
        or isinstance(turn_index, bool)
        or not isinstance(turn_index, int)
        or turn_index < 1
    ):
        return None
    session_kind = active_turn.get("session_kind")
    target = redirect_store.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness if isinstance(harness, str) and harness else None,
    )
    try:
        redirect_store.validate_target(target)
    except redirect_store.RedirectStoreError:
        return None
    return target, session_kind if isinstance(session_kind, str) else None


def inspect_target(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    expected_target: redirect_store.RedirectTarget | None = None,
) -> TargetFacts:
    """Read whether ``plan_dir`` can accept a redirect, writing nothing.

    Run state is read first and the lock second. A lock that disappears between
    the two reads makes this refuse, while the reverse order could report a
    live channel on the strength of a lock that outlived the run state proving
    the run was over. Refusing on a run that is in fact healthy costs a retry;
    admitting a correction into a dead run loses it.
    """
    canonical = _canonical_plan_dir(plan_dir)
    facts = intervention.inspect_run(canonical, state_dir=state_dir)

    if not facts.run_active or facts.run_id is None:
        return TargetFacts(
            plan_dir=canonical,
            run_active=False,
            paused=facts.paused,
            lock_active=facts.lock_active,
            refusal=REFUSE_RUN_PAUSED if facts.paused else REFUSE_NO_ACTIVE_RUN,
        )

    resolved = _active_turn_target(facts.runner_state, facts.run_id)
    if resolved is None:
        return TargetFacts(
            plan_dir=canonical,
            run_active=True,
            lock_active=facts.lock_active,
            refusal=REFUSE_NO_ACTIVE_TURN,
        )
    target, session_kind = resolved

    partial = TargetFacts(
        plan_dir=canonical,
        target=target,
        session_kind=session_kind,
        run_active=True,
        lock_active=facts.lock_active,
    )
    if expected_target is not None and not target.identifies_same_turn(expected_target):
        return _refused(partial, REFUSE_STALE_TARGET)
    if session_kind in TERMINAL_AUDIT_SESSION_KINDS:
        return _refused(partial, REFUSE_TERMINAL_AUDIT)

    lock = state_locks.inspect_lock_info(canonical, state_dir)
    if lock is None:
        return _refused(partial, REFUSE_NO_ACTIVE_RUN)
    # Ordered most specific first. `LockInfo.active` is defined as `not stale`
    # and both conditions below set `stale`, so testing activity first would
    # collapse every one of them into `no-active-run` and leave the operator
    # unable to tell "the runner is on another host" from "there is no run".
    if lock.foreign_host:
        return _refused(partial, REFUSE_REMOTE_OWNER)
    if lock.stale:
        return _refused(partial, REFUSE_STALE_LOCK)
    if lock.redirect_endpoint is None:
        return _refused(partial, REFUSE_URGENT_CHANNEL_UNAVAILABLE)

    return TargetFacts(
        plan_dir=canonical,
        target=target,
        session_kind=session_kind,
        run_active=True,
        lock_active=True,
        urgent_endpoint=lock.redirect_endpoint,
    )


def _refused(facts: TargetFacts, refusal: str) -> TargetFacts:
    return TargetFacts(
        plan_dir=facts.plan_dir,
        target=facts.target,
        session_kind=facts.session_kind,
        run_active=facts.run_active,
        paused=facts.paused,
        lock_active=facts.lock_active,
        urgent_endpoint=facts.urgent_endpoint,
        refusal=refusal,
    )


def _validate_wait(wait_seconds: float) -> float:
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int | float):
        raise RedirectAdmissionError("invalid-wait", "wait_seconds must be a number")
    if not 0.0 <= wait_seconds <= MAX_ACCEPTANCE_WAIT_SECONDS:
        raise RedirectAdmissionError(
            "invalid-wait",
            f"wait_seconds must be between 0 and {MAX_ACCEPTANCE_WAIT_SECONDS}",
        )
    return float(wait_seconds)


def submit_redirect(
    plan_dir: str | Path,
    feedback: str,
    *,
    state_dir: str | Path | None = None,
    operation_id: str | None = None,
    ttl_seconds: int | None = None,
    expected_target: redirect_store.RedirectTarget | None = None,
    wait_seconds: float = ACCEPTANCE_WAIT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SubmissionOutcome:
    """Submit one correction and report what became of it within the wait.

    The sequence is fixed by what each step can still take back:

    1. Refuse on the pre-checks. Nothing has been written, so a refusal here
       leaves the correction with its author.
    2. Persist. From this line on the feedback cannot be lost, whatever
       happens to this process.
    3. Ring the urgent channel. A failed ring is *durably* settled as
       ``urgent-channel-unavailable`` before returning, so the "cannot falsely
       report acceptance" guarantee holds in both directions: the caller is not
       told it landed, and no runner reading the journal later can decide it
       did.
    4. Wait, briefly, for the runner's own record of acceptance. Acceptance is
       read from the journal rather than inferred from a delivered wakeup,
       because a delivered byte proves a reader existed, not that it took the
       request.

    ``monotonic`` and ``sleep`` are injectable so the wait can be driven
    deterministically. The clock is monotonic rather than wall-clock: a wait
    bound that a clock adjustment can lengthen is not a bound.
    """
    wait = _validate_wait(wait_seconds)
    redirect_store.validate_feedback(feedback)
    facts = inspect_target(
        plan_dir, state_dir=state_dir, expected_target=expected_target
    )
    if facts.refusal is not None or facts.target is None:
        refusal = facts.refusal or REFUSE_NO_ACTIVE_TURN
        return SubmissionOutcome(
            outcome=(
                OUTCOME_UNAVAILABLE
                if refusal in UNAVAILABLE_REASONS
                else OUTCOME_REJECTED
            ),
            plan_dir=facts.plan_dir,
            reason=refusal,
            target=facts.target,
        )

    target = facts.target
    request = redirect_store.submit_request(
        facts.plan_dir,
        target=target,
        feedback=feedback,
        state_dir=state_dir,
        operation_id=operation_id,
        ttl_seconds=ttl_seconds,
    )

    delivery = control_signal.notify_redirect(facts.plan_dir, state_dir=state_dir)
    if not delivery.delivered:
        return SubmissionOutcome(
            outcome=OUTCOME_UNAVAILABLE,
            plan_dir=facts.plan_dir,
            reason=redirect_store.REASON_URGENT_CHANNEL_UNAVAILABLE,
            operation_id=request.operation_id,
            request=_settle_undeliverable(facts.plan_dir, request, state_dir=state_dir),
            target=target,
            delivery=delivery,
        )

    return _await_settlement(
        facts.plan_dir,
        request,
        target=target,
        state_dir=state_dir,
        delivery=delivery,
        wait=wait,
        monotonic=monotonic,
        sleep=sleep,
    )


def _settle_undeliverable(
    plan_dir: Path,
    request: redirect_store.RedirectRequest,
    *,
    state_dir: str | Path | None,
) -> redirect_store.RedirectRequest:
    """Close a request the urgent channel could not carry.

    Best-effort by necessity — the journal may be unwritable for the same
    reason the channel is gone — but never silently downgraded to acceptance:
    if the settlement cannot be written, the request stays ``submitted`` and
    its own expiry closes it, and the caller was already told ``unavailable``.
    """
    try:
        return redirect_store.reject_request(
            plan_dir,
            request.operation_id,
            reason=redirect_store.REASON_URGENT_CHANNEL_UNAVAILABLE,
            state_dir=state_dir,
        )
    except redirect_store.RedirectStoreError:
        return request


def _await_settlement(
    plan_dir: Path,
    request: redirect_store.RedirectRequest,
    *,
    target: redirect_store.RedirectTarget,
    state_dir: str | Path | None,
    delivery: control_signal.NotifyResult,
    wait: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> SubmissionOutcome:
    started = monotonic()
    while True:
        current = _current_request(plan_dir, request, state_dir=state_dir)
        elapsed = monotonic() - started
        if current.terminal:
            return SubmissionOutcome(
                outcome=(OUTCOME_ACCEPTED if current.accepted else OUTCOME_REJECTED),
                plan_dir=plan_dir,
                reason=current.reason,
                operation_id=current.operation_id,
                request=current,
                target=target,
                delivery=delivery,
                waited_seconds=elapsed,
            )
        if elapsed >= wait:
            # Deliberately not settled. The request is still inside its TTL and
            # the runner may yet take it; writing a rejection here would race
            # that acceptance and could retract a correction already in effect.
            return SubmissionOutcome(
                outcome=OUTCOME_UNCERTAIN,
                plan_dir=plan_dir,
                reason=redirect_store.STATE_SUBMITTED,
                operation_id=current.operation_id,
                request=current,
                target=target,
                delivery=delivery,
                waited_seconds=elapsed,
            )
        sleep(min(ACCEPTANCE_POLL_INTERVAL_SECONDS, max(0.0, wait - elapsed)))


def _current_request(
    plan_dir: Path,
    request: redirect_store.RedirectRequest,
    *,
    state_dir: str | Path | None,
) -> redirect_store.RedirectRequest:
    """Re-read one request, falling back to the last known copy.

    An unreadable journal is reported as "still submitted" rather than as a
    settlement, so a transient read failure can only extend the wait — never
    manufacture an outcome the journal does not contain.
    """
    try:
        view = redirect_store.read_journal(plan_dir, state_dir=state_dir)
    except redirect_store.RedirectStoreError:
        return request
    return view.request(request.operation_id) or request


def pending_redirects(
    plan_dir: str | Path,
    *,
    target: redirect_store.RedirectTarget,
    state_dir: str | Path | None = None,
) -> tuple[redirect_store.RedirectRequest, ...]:
    """Return unsettled requests aimed at ``target``, oldest first.

    The runner's read side. Filtered by target rather than returning everything
    open, because a request naming a turn that has already finished is not this
    turn's to answer and must not be swept into it.
    """
    canonical = _canonical_plan_dir(plan_dir)
    view = redirect_store.read_journal(canonical, state_dir=state_dir)
    return view.pending_for(target)


def accept_redirect(
    plan_dir: str | Path,
    operation_id: str,
    *,
    target: redirect_store.RedirectTarget,
    state_dir: str | Path | None = None,
) -> redirect_store.RedirectRequest:
    """Record the live runner's acceptance of one request.

    A thin pass-through to :func:`redirect_store.accept_request`, and thin on
    purpose: acceptance is one durable, target-checked, expiry-checked append
    under the journal's lock, and any pre-check added here would be a second
    opinion formed outside that lock. ``target`` must be the turn the caller is
    currently executing; the store refuses anything else.
    """
    return redirect_store.accept_request(
        _canonical_plan_dir(plan_dir),
        operation_id,
        target=target,
        state_dir=state_dir,
    )
