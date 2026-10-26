"""How a plan with nothing left to run reaches its ending.

Every other boundary in this package answers a question about *a session*.
This one owns the pass where there is no session left to answer for: the run
loop found no pending work, and something still has to decide whether that is
an ending, and which ending it is. Three things can be true of a plan with no
next session, and this module is the ordered decision between them.

* **The plan is inconsistent.** No session is pending and not every session is
  complete, so selection and status disagree. Nothing here can repair that, so
  the run pauses on ``no-pending-but-incomplete`` with the snapshot attached.
* **The plan has work left that is not a session.** Under
  ``--commit-at-end`` the run still owes one commit, and under
  ``--review-harness`` it still owes a final review — and a review that finds
  something owes repair children, which are sessions, which puts pending work
  back on a plan that had none. Each of those grows the run rather than ending
  it, which is why this boundary can hand the loop back a "go round again"
  instead of only a verdict.
* **The plan is finished.** Nothing is owed, so ``reason`` becomes
  ``all-complete`` and the loop stops; terminal adjudication downstream decides
  whether that success stands.

The loop's ``break`` and ``continue`` were the only statement of that decision
before this move, and neither survives a boundary.
:class:`run_completion_types.CompletionOutcome` is what replaces them: every
path out of :func:`complete_plan` returns one, it carries the plan image the
pass ended on, and its two actions are exactly the two the loop had. The branch
never fell through, so the outcome is total — there is no third answer for the
caller to invent.

This module is the ordered decision; the branches it picks between live in
siblings. ``run_completion_types`` holds ``RunContext``, ``SoftFailCommit`` and
the outcome type, ``run_completion_commit`` the end-of-run commit,
``run_completion_repairs`` the recorded review history that settles or reopens
the plan, ``run_completion_review`` the generated review session, and
``run_completion_support`` the steps those branches share.

Durable records this boundary writes, all through collaborators that already
own them: the **end-of-run commit group** (``commit_settlement``), the
**generated review session file** and its two events (``review_session_body``),
the **review repair children** and the reopen record that accounts for them
(``review_repair`` and ``review_cycle``), the plan's **readiness state**
(``readiness_gate``), and the run state and event stream every one of those
writes through ``RunContext.persistence``. It creates no record of its own.

Nothing here imports the runner, and since session 37 it does not import the
loop that owns ``RunContext`` either: the context enters as the structural
``RunContext`` Protocol — the ``CoordinationPersistence``/``LockedRun`` pattern
applied to the loop's own context object. The two collaborators the context
does not carry enter beside it: the run-control listener, which is non-optional
here because a completion pass only happens after the loop bound it, and
``soft_fail_commit``, which stays in ``run_loop`` because the owed-group
recovery above the loop shares it. Everything else is read at its true owner.

Five invariants survive the move:

* **The end commit is attempted once.** ``commit_at_end_attempted`` is set
  before the attempt, not after it, so a commit that fails still marks the
  attempt spent and the next pass moves on to the review instead of retrying
  here. The retry belongs to the owed-group settlement at the top of the loop.
* **A commit failure never ends the run.** The end-of-run commit soft-fails
  through the same recorder every other commit boundary uses, and the pass
  returns "go round again" rather than a verdict, so an unresolved group is
  reported by terminal adjudication rather than by an exception.
* **A stop is observed before review work begins.** The external control queue
  is drained and ``stop_after_current`` is read *before* the review resume is
  computed, because the review and its repair children are work that starts
  after the session the operator asked to stop after.
* **Plan growth is re-gated before it runs.** Both materialized repair children
  and a freshly appended review session reach this point under the readiness
  report that said no turns were required, so each re-gates the grown plan and
  re-writes ``running`` before returning to the loop. A *reused* pending review
  session is not re-gated: it was authorized when it was created.
* **The image the loop keeps is the one this pass ended on.** Every mutation
  that grows the plan is followed by a fresh inspection, and the resulting
  snapshot travels back on the outcome, so the next selection never runs
  against the image the pass started from.
"""

from __future__ import annotations

import argparse
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import (
    plans_models,
    plans_payloads,
    review_cycle,
    run_control,
    run_coordination_control_drain,
    run_inputs,
    run_selection,
)
from .run_completion_commit import settle_end_commit
from .run_completion_repairs import reopen_review_repairs, settle_terminal_review
from .run_completion_review import append_review_session
from .run_completion_support import stop_before_review
from .run_completion_types import CompletionOutcome, RunContext, SoftFailCommit


def complete_plan(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    args: argparse.Namespace,
    cwd: Path,
    commit_mode: str | None,
    review_requested: bool,
    max_review_fix_cycles: int,
    allow_not_loop_ready: bool,
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
    run_controls: run_control.RunControlListener,
    run_control_lock: AbstractContextManager[Any],
    wakeup: threading.Event | None,
    soft_fail_commit: SoftFailCommit,
) -> CompletionOutcome:
    """Decide what a plan with no pending session owes, and settle it.

    Called once per loop pass that selects nothing, in the order the plan's
    remaining obligations must be discharged: the consistency check first
    because nothing below is meaningful on a plan whose status and selection
    disagree, then the end-of-run commit because a review must read committed
    work, then the review and the repair children it may reopen, and only then
    the ``all-complete`` ending.

    Returns the loop's next move rather than performing it. A returned
    ``ACTION_CONTINUE`` always means the plan grew — a commit was made, a
    review session was appended, or repair children were materialized — so the
    caller re-selects; ``ACTION_BREAK`` means this run is over, whether it
    settled or paused.
    """
    if not run_inputs._all_sessions_complete(snapshot):
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="no-pending-but-incomplete",
            message="no pending session was found, but not every session is complete",
            details={"snapshot": plans_payloads.snapshot_payload(snapshot)},
        )
        return CompletionOutcome.stop(snapshot)

    if commit_mode == run_selection.COMMIT_MODE_END and not ctx.commit_at_end_attempted:
        return settle_end_commit(ctx, snapshot, soft_fail_commit)

    if review_requested and not ctx.review_appended:
        # The review and its repair children are work that starts
        # *after* the session the operator asked to stop after, so
        # the request has to be visible before either begins.
        run_coordination_control_drain.drain_external_control_queue(
            plan_dir=snapshot.plan_dir,
            state_dir_arg=args.state_dir,
            control_state=run_controls.state,
            seen_ids=ctx.seen_external_command_ids,
            payload=ctx.payload,
            event_path=ctx.event_path,
            control_payload=ctx.control_payload,
            context=None,
            persistence=ctx.persistence,
            run_control_lock=run_control_lock,
            wakeup=wakeup,
            guard_controller=ctx.guard_controller,
        )
        with run_control_lock:
            stop_requested = run_controls.state.stop_after_current
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

        if review_resume.action == "terminal":
            return settle_terminal_review(ctx, snapshot, review_resume)

        if review_resume.action == "materialize-repairs":
            return reopen_review_repairs(
                ctx,
                snapshot,
                review_resume,
                args=args,
                cwd=cwd,
                max_review_fix_cycles=max_review_fix_cycles,
                allow_not_loop_ready=allow_not_loop_ready,
                run_controls=run_controls,
                stop_requested=stop_requested,
            )

        if stop_requested:
            return stop_before_review(ctx, snapshot, run_controls)

        return append_review_session(
            ctx,
            snapshot,
            args=args,
            cwd=cwd,
            review_guard_excludes=review_guard_excludes,
            review_guard_include_ignored=review_guard_include_ignored,
        )

    with ctx.persistence.lock:
        ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
        ctx.payload["reason"] = "all-complete"
    return CompletionOutcome.stop(snapshot)
