"""Settle an operator-corrected turn before allowing its successor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    handoff_correction,
    handoff_guards,
    harnesses,
    plans_models,
    plans_payloads,
    redirect_continuation,
    redirect_control,
    run_control,
    run_termination,
    session_verification,
    turn_disposition_failures,
    turn_disposition_models,
)

#: Why the redirected attempt's completion claim is taken back. Distinct from
#: the nonzero-exit reason because it is not a failure: the turn may have
#: exited cleanly, and what disqualifies its claim is that a correction reached
#: it before anything adjudicated one.
REDIRECT_COMPLETION_REASON = "unsettled-completion-after-redirect"

REDIRECT_COMPLETION_NOTE = (
    "an operator correction was accepted against this turn before it reached "
    "an accepted settlement; the completion claim is withdrawn and the work "
    "this session already wrote is left in place for the next attempt"
)

#: The correction becomes the next attempt of the same session.
REDIRECT_OUTCOME_CONTINUED = "continued"
#: The interrupted worker could not be shown gone, so nothing may succeed it.
REDIRECT_OUTCOME_BLOCKED = "blocked"
#: A stop or abort queued after the correction ended the run instead.
REDIRECT_OUTCOME_STOPPED = "stopped"


def _record_redirect_continuation(
    ctx: turn_disposition_models.RunContext,
    *,
    redirect: redirect_continuation.TurnRedirect,
    pending: redirect_continuation.PendingRedirect,
    session_id: str,
    turn_index: int,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Publish what this turn's correction became, before anything acts on it.

    The gate's projection is refreshed rather than left as the run loop wrote
    it: the escalation's report only exists once the turn is torn down, so the
    record written mid-turn names an acceptance whose outcome was still open.
    All three writes go under one lock, because the result artifact, the event
    and the run state are one claim about the same turn.
    """
    continuation_record: dict[str, Any] = {
        "outcome": outcome,
        "redirect": pending.to_payload(),
        **(detail or {}),
    }
    with ctx.persistence.lock:
        turn_payload[redirect_continuation.TURN_PAYLOAD_KEY] = redirect.to_payload()
        turn_payload["redirect_continuation"] = continuation_record
        durability.write_json_file(paths["result"], turn_payload, durable=True)
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **plans_payloads.session_identity_payload(session_id),
                **continuation_record,
            },
        )
        ctx.persistence.write_state(ctx.payload)


def settle_redirected_turn(
    ctx: turn_disposition_models.RunContext,
    listener: run_control.RunControlListener,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    redirect: redirect_continuation.TurnRedirect,
    harness: str,
    conversation_id: str | None,
    session_id: str,
    session_label: str,
    turn_index: int,
    result: harnesses.HarnessResult,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    session_details: dict[str, Any],
    pending: redirect_continuation.PendingRedirect | None,
) -> turn_disposition_models.RedirectContinuation | None:
    """Turn an accepted correction into the next attempt of the same session.

    Returns the continuation the next pass runs, or ``None`` when the run
    stopped. Three things happen in an order that is the contract:

    1. **The predecessor has to be gone.** A forced stop that could not be
       confirmed means a worker may still hold this plan's lock, its
       conversation and its worktree, so a successor is refused and the run
       pauses on ``redirect-successor-blocked`` rather than starting a second
       worker beside the first.
    2. **The attempt gives up its completion claim.** A redirected turn is not a
       settled one even when the harness exited zero, and the claim is withdrawn
       through the same reverter every guard refusal uses -- the work the
       session already wrote stays on disk for the next attempt to build on.
    3. **A later stop or abort outranks the correction.** An operator who asked
       to stop after telling the run what to fix meant the stop; the correction
       stays pending in the journal for a later run rather than being spent on
       an attempt nobody is waiting for.

    No commit happens on any of these paths and none is reachable from one: the
    branch never settles a turn, and the pause it can take is non-recoverable,
    which is what the automatic paused-work commit is gated on.
    """
    carried = redirect_continuation.PendingRedirect.after_turn(
        redirect.accepted,
        session_id=session_id,
        harness=harness,
        conversation_id=conversation_id,
        turn_index=turn_index,
        previous=pending,
    )
    # The disposition is reached only from a recorded acceptance, so the fold
    # above always has something to fold.
    assert carried is not None

    try:
        report = redirect.require_successor_allowed()
    except redirect_control.SuccessorBlockedError as error:
        blocked = error.to_payload()
        _record_redirect_continuation(
            ctx,
            redirect=redirect,
            pending=carried,
            session_id=session_id,
            turn_index=turn_index,
            turn_payload=turn_payload,
            paths=paths,
            outcome=REDIRECT_OUTCOME_BLOCKED,
            detail={"blocked": blocked},
        )
        ctx.record_final_snapshot(after if after is not None else before)
        ctx.pause(
            reason=events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
            message=(
                f"the redirected turn for {session_label} could not be confirmed "
                f"stopped: {error.reason}"
            ),
            details={**session_details, "redirect_continuation": blocked},
        )
        return None

    reconciled = after
    reconciliation: dict[str, Any] | None = None
    if after is not None:
        reconciled, reconciliation = (
            turn_disposition_failures._reconcile_unsettled_completion(
                ctx,
                after=after,
                selected_after=next(
                    (item for item in after.sessions if item.id == session_id),
                    None,
                ),
                session_id=session_id,
                turn_index=turn_index,
                result=result,
                turn_payload=turn_payload,
                paths=paths,
                reason=REDIRECT_COMPLETION_REASON,
                failure_note=REDIRECT_COMPLETION_NOTE,
            )
        )

    stop_snapshot = reconciled if reconciled is not None else before
    stop_detail: dict[str, Any] = {
        **({"completion_reconciliation": reconciliation} if reconciliation else {}),
        "termination": report.to_payload() if report is not None else None,
    }
    if listener.state.abort_turn_requested:
        _record_redirect_continuation(
            ctx,
            redirect=redirect,
            pending=carried,
            session_id=session_id,
            turn_index=turn_index,
            turn_payload=turn_payload,
            paths=paths,
            outcome=REDIRECT_OUTCOME_STOPPED,
            detail={**stop_detail, "stopped_by": "abort-turn"},
        )
        with ctx.persistence.lock:
            run_termination._honor_abort_turn(
                listener,
                snapshot=stop_snapshot,
                session_id=session_id,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                plan_cache=ctx.plan_cache,
                revert_session_status=handoff_guards.session_status_reverter(
                    handoff_correction._append_handoff_note
                ),
                reinspect=session_verification._reinspect_after_revert,
                context=turn_payload,
                persistence=ctx.persistence,
            )
        return None
    if listener.state.stop_after_current:
        _record_redirect_continuation(
            ctx,
            redirect=redirect,
            pending=carried,
            session_id=session_id,
            turn_index=turn_index,
            turn_payload=turn_payload,
            paths=paths,
            outcome=REDIRECT_OUTCOME_STOPPED,
            detail={**stop_detail, "stopped_by": "stop-after-current"},
        )
        run_termination._honor_stop_after_current(
            listener,
            snapshot=stop_snapshot,
            payload=ctx.payload,
            event_path=ctx.event_path,
            control_payload=ctx.control_payload,
            context=turn_payload,
            persistence=ctx.persistence,
        )
        return None

    _record_redirect_continuation(
        ctx,
        redirect=redirect,
        pending=carried,
        session_id=session_id,
        turn_index=turn_index,
        turn_payload=turn_payload,
        paths=paths,
        outcome=REDIRECT_OUTCOME_CONTINUED,
        detail=stop_detail,
    )
    return turn_disposition_models.RedirectContinuation(
        snapshot=reconciled, pending=carried
    )
