"""Corrections carried into the next turn, and the delivery of a turn's guidance."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import (
    events,
    harnesses,
    plans_payloads,
    redirect_ack,
    redirect_continuation,
    redirect_recovery,
    redirect_store,
)
from .run_loop_context import RunContext


def _carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    pending: redirect_continuation.PendingRedirect | None,
    session_id: str,
    turn_index: int,
    harness: str,
    recovery: redirect_recovery.ReconciliationReport | None = None,
) -> redirect_ack.CarriedCorrections | None:
    """Bind every correction this attempt should carry, just before its prompt.

    This call *is* the coalescing boundary. Corrections keep arriving while the
    predecessor is collected and the resumed prompt is assembled, and all of
    them are about the same situation, so everything unsettled against an
    earlier attempt of this session is swept in here and ordered by submission.
    A correction submitted after this point names the attempt that is about to
    be live, which is the ordinary mid-turn redirect path rather than a late
    edit to a prompt already written.

    Gated on the same session match ``interrupted_operations`` uses: a
    correction is never swept into a session the operator was not talking about.
    A journal that cannot be carried at all degrades to session 11's behaviour —
    the resumed worker is still told which operations were interrupted — and
    the corrections stay accepted in the journal rather than being lost, so the
    failure is recorded instead of silently changing what the worker reads.

    A second population reaches the same boundary: corrections a *previous* run
    was holding when it died. They have no ``PendingRedirect`` — the object that
    would have carried them across attempts died with the process that built it
    — so they arrive from the startup reconciliation instead, and they are the
    reason this can run for a session that was never redirected in this run.
    """
    recovered_ids: tuple[str, ...] = ()
    if recovery is not None:
        recovered_ids = redirect_recovery.recoverable_operation_ids(
            recovery, session_id
        )
    redirected_here = pending is not None and pending.session_id == session_id
    if not redirected_here and not recovered_ids:
        return None
    redirect_payload: dict[str, Any] = (
        pending.to_payload()
        if pending is not None and redirected_here
        else {**plans_payloads.session_identity_payload(session_id), "recovered": True}
    )
    continuation = redirect_store.RedirectTarget(
        run_id=ctx.run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )
    try:
        carried = redirect_ack.carry_corrections(
            plan_dir,
            continuation=continuation,
            accepted_operation_ids=(
                pending.operation_ids if pending is not None and redirected_here else ()
            ),
            recovered_operation_ids=recovered_ids,
            state_dir=state_dir_arg,
        )
    except redirect_store.RedirectStoreError as error:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **plans_payloads.session_identity_payload(session_id),
                "outcome": "carry-failed",
                "redirect": redirect_payload,
                "harness": harness,
                "error": {"reason": error.reason, "message": str(error)},
            },
        )
        return None
    if not redirected_here and not carried.corrections:
        # The recoverable list is a fact about the crash and does not shrink as
        # this run works through it, so it still names this session on every
        # later turn. By then the journal has recorded the delivery and the
        # coalescing boundary drops the ids; saying "carried" again would claim
        # a delivery per turn for one correction handed over once.
        return None
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans_payloads.session_identity_payload(session_id),
            "outcome": "carried",
            "redirect": redirect_payload,
            "harness": harness,
            # Digests, byte counts and operation ids. The words are in the
            # journal and reach nothing but the prompt itself.
            **carried.to_payload(),
        },
    )
    return carried


def _annotate_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
    outcome: str,
    annotate: Callable[..., redirect_ack.CarryAnnotations],
) -> None:
    """Record what became of this attempt's carry, and say so in an event.

    Shared by the two sides of the launch boundary because they differ in one
    call and one word. Both are bookkeeping about a decision already made, so a
    journal that refuses them is reported and never raised: at the release site
    the run is already pausing for a reason of its own, and at the delivery site
    a worker is already running.

    Not shared with :func:`_launch_carried_corrections`, which is bookkeeping the
    decision *depends on* and therefore cannot swallow its own failure.
    """
    if carried is None or not carried.corrections:
        return
    try:
        result = annotate(
            plan_dir,
            continuation=carried.continuation,
            operation_ids=carried.operation_ids(),
            state_dir=state_dir_arg,
        )
    except redirect_store.RedirectStoreError as error:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **plans_payloads.session_identity_payload(session_id),
                "outcome": f"{outcome}-failed",
                "harness": harness,
                "error": {"reason": error.reason, "message": str(error)},
            },
        )
        return
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans_payloads.session_identity_payload(session_id),
            "outcome": outcome,
            "harness": harness,
            **result.to_payload(),
        },
    )


def _launch_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
) -> bool:
    """Declare this attempt's carry about to go out, and say whether it worked.

    The one piece of redirect bookkeeping the run loop may not shrug off. Its
    siblings report a decision already made; this one is a precondition of
    making it. Appended before the executor is given the prompt, it is what lets
    a later reader tell an attempt that died before submitting from one that
    died just after -- and the whole point of that distinction is to stop the
    second case from being replayed to a new worker as if it were the first.

    So a refusal here returns ``False`` and the caller must abandon the turn
    rather than launch it. Submitting a prompt this run could not declare would
    put the operator's words in a worker's hands with nothing on disk to say so,
    which is precisely the shape that produces a duplicate delivery later. A
    turn not run costs a pause the operator can act on; a turn run undeclared
    costs a correction delivered twice with nobody able to tell.

    ``True`` with nothing carried is the ordinary case: no carry, nothing to
    declare, nothing that can fail.
    """
    if carried is None or not carried.corrections:
        return True
    error: dict[str, Any] | None = None
    result: redirect_ack.CarryAnnotations | None = None
    try:
        result = redirect_ack.launch_corrections(
            plan_dir,
            continuation=carried.continuation,
            operation_ids=carried.operation_ids(),
            state_dir=state_dir_arg,
        )
    except redirect_store.RedirectStoreError as failure:
        error = {"reason": failure.reason, "message": str(failure)}
    else:
        if result.refused:
            # A per-id refusal is as disqualifying as a raised one. The prompt
            # holds every carried correction, so declaring only some of them
            # would leave the rest in exactly the state this call exists to
            # prevent.
            refusal = result.refused[0]
            error = {"reason": refusal.reason, "message": refusal.message}
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans_payloads.session_identity_payload(session_id),
            "outcome": "launching" if error is None else "launching-failed",
            "harness": harness,
            **(result.to_payload() if result is not None else {}),
            **({"error": error} if error is not None else {}),
        },
    )
    return error is None


def _release_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
) -> None:
    """Hand back a carry this attempt assembled a prompt for but never launched.

    Called at every way out between the coalescing boundary and the launch: a
    prompt build that pauses, a conversation this run cannot prove is usable, a
    baseline capture that refuses. Each of them ends the attempt with the
    correction bound to a turn that will never run, and without this the journal
    would go on claiming the operator's words were delivered to it -- which is
    what makes a later authorized fresh start arrive empty-handed.
    """
    _annotate_carried_corrections(
        ctx,
        plan_dir=plan_dir,
        state_dir_arg=state_dir_arg,
        carried=carried,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        outcome="released",
        annotate=redirect_ack.release_corrections,
    )


def _deliver_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
) -> None:
    """Record that a worker was handed this attempt's carry.

    Hung off the harness's submission callback, which is the earliest moment
    anything can honestly be called delivery: the prompt has left this process
    for the worker. A spawn is not that moment -- a protocol harness spawns,
    handshakes, and can still be refused with the prompt unsent -- and reading
    one as the other is what leaves an operator's words spent on a turn that
    never received them. From here the correction is the worker's, and recovery
    treats an unreceipted one as an open question rather than something to hand
    out again.
    """
    _annotate_carried_corrections(
        ctx,
        plan_dir=plan_dir,
        state_dir_arg=state_dir_arg,
        carried=carried,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        outcome="delivered",
        annotate=redirect_ack.deliver_corrections,
    )


class _TurnDelivery:
    """One attempt's carry, from the launch boundary to the turn's outcome.

    An attempt that carried corrections ends in one of three states, and only
    the harness can narrow it: the prompt reached a worker, it provably did not,
    or neither could be established. The first two are recorded here because
    they are the same decision read from opposite ends -- ``delivered`` makes
    taking the words back a duplicate, ``released`` makes handing them out again
    a rescue. The third is deliberately recorded as nothing at all: the
    ``launching`` line written before the launch already stands, and leaving it
    unanswered is what tells a later reader to ask rather than to guess.

    Settled exactly once, under a lock, because the two ends are reached from
    different threads: a protocol harness submits on its own driver thread while
    the run loop is waiting on the turn it will later settle.

    Bound per attempt rather than closed over, like the guidance delivery
    alongside it: a closure would read the loop variables as the *next* attempt
    left them.
    """

    def __init__(
        self,
        ctx: RunContext,
        *,
        plan_dir: Path,
        state_dir_arg: str | Path | None,
        carried: redirect_ack.CarriedCorrections | None,
        session_id: str,
        turn_index: int,
        harness: str,
    ) -> None:
        self._ctx = ctx
        self._plan_dir = plan_dir
        self._state_dir_arg = state_dir_arg
        self._carried = carried
        self._session_id = session_id
        self._turn_index = turn_index
        self._harness = harness
        self._lock = threading.Lock()
        self._settled = False

    def note_prompt_submitted(self) -> None:
        """The harness's submission callback: a worker now holds this prompt.

        Up to here the run had only undertaken to deliver these words; from here
        they went out. Exceptions are the harness seam's to swallow -- this is
        bookkeeping about a turn that is already running.
        """
        if not self._claim():
            return
        _deliver_carried_corrections(
            self._ctx,
            plan_dir=self._plan_dir,
            state_dir_arg=self._state_dir_arg,
            carried=self._carried,
            session_id=self._session_id,
            turn_index=self._turn_index,
            harness=self._harness,
        )

    def settle_unsubmitted(self, result: harnesses.HarnessResult) -> None:
        """Hand the carry back if the turn *proved* it submitted no prompt.

        The launch-side counterpart of the prelaunch releases: a turn that
        spawned and then refused -- a native restore the provider declined, a
        cancellation caught mid-handshake, a conversation it could not record --
        leaves the correction bound to an attempt that never delivered it.
        Without this the journal would go on claiming those words were the dead
        worker's, and the authorized fresh continuation that follows would
        arrive empty-handed.

        The proof is the result's own refusal fields, not the absence of a
        submission callback. A missing callback is a silence, and by the time
        this runs the journal already says the prompt was about to go out, so
        the silence has two readings: the turn never sent it, or it sent it and
        whatever would have reported that did not survive -- an adapter thread
        that died between the write and the notification, a delivery line the
        journal refused. Releasing on the silence is how the same correction
        reaches a second worker, so an unproven turn releases nothing and the
        carry stands as :attr:`~.redirect_store.RedirectRequest.launch_ambiguous`
        for an operator to reconcile.

        Called on every path out of the turn, including the ones that submitted:
        the settle guard is what makes it a no-op there rather than a withdrawal
        of work already done.
        """
        if not harnesses.prompt_provably_unsubmitted(result):
            return
        if not self._claim():
            return
        _release_carried_corrections(
            self._ctx,
            plan_dir=self._plan_dir,
            state_dir_arg=self._state_dir_arg,
            carried=self._carried,
            session_id=self._session_id,
            turn_index=self._turn_index,
            harness=self._harness,
        )

    def _claim(self) -> bool:
        with self._lock:
            if self._settled:
                return False
            self._settled = True
            return True
