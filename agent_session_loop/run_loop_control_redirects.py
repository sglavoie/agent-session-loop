"""Run-control actions, turn teardown and redirect restart handling."""

from __future__ import annotations

import argparse

from . import (
    events,
    plans_errors,
    plans_models,
    plans_payloads,
    redirect_continuation,
    redirect_recovery,
    redirect_restart,
    redirect_store_models,
    run_control,
    run_coordination_control,
    run_coordination_mailbox_poll,
    run_reporting,
    run_termination,
)
from .run_loop_context import RunContext


def _handle_run_control_action(
    ctx: RunContext, action: run_control.RunControlAction
) -> None:
    if action.action in ("consumed", "abort-consumed"):
        # Both consumptions are recorded by the site that acts on them, and
        # an abort's consumption has three distinct outcomes this callback
        # cannot tell apart.
        return
    if action.action == "tail-disabled":
        ctx.inline_tail.disable()
    with ctx.persistence.lock:
        context = (
            None if ctx.run_control_context is None else dict(ctx.run_control_context)
        )
        run_coordination_control.record_run_control_action(
            event_path=ctx.event_path,
            payload=ctx.payload,
            control_payload=ctx.control_payload,
            action=action,
            context=context,
            persistence=ctx.persistence,
        )
    if action.action == "tail-enabled":
        ctx.inline_tail.enable()


def _handle_run_control_status(
    ctx: RunContext, action: run_control.RunControlAction
) -> None:
    with ctx.persistence.lock:
        context = (
            None if ctx.run_control_context is None else dict(ctx.run_control_context)
        )
        status_control = dict(ctx.control_payload)
        status_control["stop_after_current"] = action.stop_after_current
        status_control["tail_enabled"] = action.tail_enabled
        status_control["next_harness"] = action.next_harness
        status_payload = dict(ctx.payload)
        active_turn = ctx.payload.get("active_turn")
        if isinstance(active_turn, dict):
            status_payload["active_turn"] = dict(active_turn)
        elif context is not None:
            status_payload["active_turn"] = dict(context)
        turns = ctx.payload.get("turns")
        if isinstance(turns, list):
            status_payload["turns"] = [
                dict(turn) if isinstance(turn, dict) else turn for turn in turns
            ]

    try:
        status_snapshot = ctx.plan_cache.get(ctx.plan_dir)
        plan_error = None
    except plans_errors.PlanError as error:
        status_snapshot = None
        plan_error = str(error)

    ctx.progress.status_snapshot(
        run_reporting._compact_status_lines(
            payload=status_payload,
            control=status_control,
            snapshot=status_snapshot,
            plan_error=plan_error,
        )
    )


def _abandon_turn(
    ctx: RunContext,
    redirect: redirect_continuation.TurnRedirect | None = None,
) -> None:
    """Drop this run's interactive surfaces when a turn cannot be launched.

    The redirect gate is closed here for the same reason the run controls are
    stopped: it holds a watch thread and an escalation clock aimed at a process
    that never became this turn's worker, and a launch that died leaves nobody
    else to take either of them down.
    """
    if redirect is not None:
        redirect.close()
    if ctx.run_controls is not None:
        ctx.run_controls.stop()
    ctx.inline_tail.stop()


def _close_turn_controls(
    redirect: redirect_continuation.TurnRedirect,
    mailbox_poll: run_coordination_mailbox_poll.MailboxPoll,
) -> None:
    """Disarm everything the turn was handed, in the order that proves it.

    Settling the redirect gate first is what serializes completion against
    acceptance: from this line on a correction is refused rather than accepted
    against a turn whose process is already gone. Closing it second is what
    releases the escalation, and it happens here rather than later because the
    harness has by now waited on its child -- which is the only evidence that
    lets the escalation's report claim the predecessor was collected rather
    than merely killed.
    """
    redirect.close()
    run_termination._detach_turn_controls(mailbox_poll)


def _redirected_turn_harness(
    ctx: RunContext,
    *,
    pending: redirect_continuation.PendingRedirect | None,
    selected: str,
    session_id: str,
    turn_index: int,
) -> str:
    """Pin a redirected attempt to the harness its predecessor ran on.

    A future-harness selection queued mid-run is a statement about the *next*
    session, not about finishing the one an operator just corrected. Letting it
    apply here would either strand the conversation the correction was written
    against or fail the resume outright on ``SELECTION_INCOMPATIBLE``, so the
    switch is deferred and recorded rather than silently taken.
    """
    if pending is None or pending.session_id != session_id:
        return selected
    if pending.harness is None or pending.harness == selected:
        return selected
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans_payloads.session_identity_payload(session_id),
            "outcome": "harness-deferred",
            "redirect": pending.to_payload(),
            "deferred_harness": selected,
        },
    )
    return pending.harness


def _pause_for_redirect_restart(
    ctx: RunContext, *, args: argparse.Namespace, snapshot: plans_models.PlanSnapshot
) -> bool:
    if args.dry_run:
        return False
    try:
        blocked = redirect_restart.restart_blocker(
            snapshot.plan_dir,
            pending_session_ids=tuple(
                s.id for s in snapshot.sessions if s.status == "pending"
            ),
            state_dir=args.state_dir,
        )
    except (OSError, redirect_store_models.RedirectStoreError) as error:
        blocked = {"reason": "redirect-successor-blocked", "error": str(error)}
    if blocked is None:
        return False
    ctx.record_final_snapshot(snapshot)
    ctx.pause(
        reason=blocked["reason"],
        message=(
            "Redirect recovery requires predecessor termination proof and "
            "an explicit resolution of any uncertain native submission."
        ),
        details={"redirect_recovery": blocked},
    )
    return True


def _reconcile_stranded_redirects(
    ctx: RunContext,
    *,
    args: argparse.Namespace,
    snapshot: plans_models.PlanSnapshot,
) -> None:
    """Settle what a crashed predecessor left in the redirect journal.

    Runs once, under this run's own lock, before the first session is selected
    — the same placement and the same reason as the repair-publication
    rollback above it. A correction the dead run accepted and never delivered is
    not something a turn can discover later: by the time a prompt is being built
    the loop is already deciding what that prompt says, and a journal still
    claiming an unanswered correction for a run that no longer exists would make
    that decision on a lie.

    The restart gate runs before reconciliation: no release, commit, or worker
    is allowed until predecessor termination and submission ambiguity have
    been checked. This function then settles only provably unsent carries.
    """
    if args.dry_run:
        # A dry run promises to write nothing durable, and settling a request is
        # durable. The corrections stay stranded for the real run to reconcile.
        return
    pending_sessions = tuple(
        session.id for session in snapshot.sessions if session.status == "pending"
    )
    try:
        report = redirect_recovery.reconcile_startup(
            snapshot.plan_dir,
            run_id=ctx.run_id,
            pending_session_ids=pending_sessions,
            state_dir=args.state_dir,
        )
    except (redirect_store_models.RedirectStoreError, OSError) as error:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "outcome": "recovery-failed",
                "error": str(error),
            },
        )
        return
    ctx.redirect_recovery_report = report
    notable = (
        report.blocked
        or report.expired
        or report.settled
        or report.stranded
        or report.recoverable
        or report.uncertain
        or report.torn_tail
        or report.unreadable_lines
    )
    if not notable:
        # Nothing was stranded and nothing is in doubt: the overwhelmingly
        # common case, and an event for it would be noise in every run's log.
        return
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "outcome": "recovery-blocked" if report.blocked else "recovered",
            "recovery": report.to_payload(),
        },
    )
