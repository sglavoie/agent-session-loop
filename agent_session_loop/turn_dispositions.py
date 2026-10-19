"""What the run does with a turn that ended in something other than progress.

``turn_postflight`` names the disposition; this boundary is what that name
selects. It owns the eight terminating branches of the session loop -- the ways
a turn can end that are not "the session advanced" -- and the retry paths
hidden among them.

Eight branches, in the order the loop tests them, which is the whole of what
this module owns:

* **interrupted** and **timeout** end a turn the plan could not be re-inspected
  after. Both publish the last plan image the run actually holds -- the
  after-image when one was read, the before-image otherwise -- and pause.
* **redirected** sits between them and is the only branch that continues the
  *same* session on purpose: an operator's correction was accepted against the
  live turn, so that attempt cannot earn a completion and the next one carries
  the correction forward. It runs ahead of ``timeout`` and ``aborted`` because
  a redirect arms the same abort signal an operator stop does and the result
  flags cannot tell them apart; only the acceptance can.
* **aborted** sits beside them and is deliberately *not* a pause: an operator
  asked for the stop, so it is recorded as one.
* **restore-refused** runs next, ahead of the plan snapshot and the exit code,
  because it is the only branch whose fact outlives the turn: the provider said
  the conversation this turn was told to continue no longer exists. It retires
  that ledger entry, which is what makes the refusal recoverable instead of
  merely repeated, and pauses for ``conversation-recovery-required``.
* **malformed-plan** is the only branch that may retry. It is the sole owner of
  the ``after is None`` turn, which is why it runs ahead of the status guards
  the rest of the loop depends on an after-image for.
* **runner-pause** carries the reply wait: a worker that paused with a question
  can be answered mid-run, and an answer turns the pause back into a turn.
* **nonzero-exit** carries the spark retry: a Spark turn that died on an
  exhausted quota or a rejected model falls back to the baseline model and
  retries the same session, provided the turn left the plan untouched. When it
  pauses instead, it first reconciles the session's status: this branch ends
  the turn ahead of verification and the handoff guards, so a session left
  ``complete`` holds a claim nothing adjudicated, and the claim is taken back
  before the final snapshot is published.

Durable records this boundary writes: the run's final snapshot on every path
that ends the run, the rejection event for a refused turn, the spark fallback
event and re-written result artifact for a retry, the reopen event and
``completion_reconciliation`` result block for a withdrawn completion claim,
the redirect continuation event and its result block, and the mailbox wait's
own lifecycle. Every pause is recorded by
``run_termination`` through ``ctx.pause``, and every write goes through the
run's persistence seam.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol in ``turn_disposition_models``,
naming only the members these branches reach. The three mailbox wait
functions enter as callbacks rather than being called on
``run_coordination_mailbox_wait``
directly, for the reason ``MailboxWaitCoordinator`` already states: callers
patch them to assert a wait was or was not entered. Session 37 repointed those
patches from the runner's retired aliases to
``run_coordination_mailbox_wait`` itself, which
is where the loop now resolves them; a branch that imported them here instead
would bind them once and silently ignore the patch.

Each of the four branches the loop can survive returns ``None`` when it ended
the run and a continuation object when it did not, rather than performing the
loop's ``break``: a boundary cannot break a loop it does not own, and "no
continuation" is the more honest name for a run that stopped than an action
enum whose other half would have to carry a snapshot that is never read.

Four invariants survive the move:

* **A session advances to ``complete`` only through a settled turn.** Neither a
  nonzero exit nor an accepted redirect is a settlement, so the completion
  claim in either after-image is withdrawn through the same reverter every
  guard refusal uses, leaving the work on disk untouched and only the claim
  taken back.
* **The loop still names its own branch.** The eight ``if
  terminating_disposition == ...`` tests stay in the loop, each delegating its
  body here and then taking its own edge. The dispatch is the loop's contract
  with the disposition; only the work behind it moved.
* **A retried turn re-enters the loop with the plan image the retry was decided
  against.** Both retry paths hand back the snapshot the next pass must start
  from, because a pass that re-read the plan instead could adopt a change made
  after the decision.
* **The reply wait pins the idle baseline itself.** The wait is genuine idle
  time, and the pin is this path's explicit opt-out from the structural refresh
  at the top of the loop -- so both the stamp and the pin have to happen here,
  where the wait is entered, not at the call site.

Implementations are split by responsibility: ``turn_disposition_models`` holds
the shared context and continuation values, ``turn_disposition_redirect`` owns
redirect settlement, ``turn_disposition_mailbox`` owns reply waits, and
``turn_disposition_failures`` owns failure reconciliation and retry. This
module retains the entry points and the uninspected-plan branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    handoff_correction,
    handoff_guards,
    plans_models,
    plans_payloads,
    run_control,
    run_termination,
    session_verification,
    turn_disposition_models,
    turn_execution_commit,
    workspaces,
)
from .turn_disposition_failures import (
    _reconcile_unsettled_completion,
    _record_spark_fallback,
    settle_nonzero_exit,
    settle_restore_refused,
)
from .turn_disposition_mailbox import (
    _enter_mailbox_reply_wait,
    _mailbox_pause_resolution_details,
    settle_runner_pause,
)
from .turn_disposition_models import (
    MailboxContinuation,
    RedirectContinuation,
    RunContext,
    SparkRetry,
)
from .turn_disposition_redirect import (
    REDIRECT_COMPLETION_NOTE,
    REDIRECT_COMPLETION_REASON,
    REDIRECT_OUTCOME_BLOCKED,
    REDIRECT_OUTCOME_CONTINUED,
    REDIRECT_OUTCOME_STOPPED,
    _record_redirect_continuation,
    settle_redirected_turn,
)

__all__ = [
    "pause_interrupted_run",
    "pause_timed_out_turn",
    "record_aborted_turn",
    "settle_malformed_plan",
    "_reconcile_unsettled_completion",
    "_record_spark_fallback",
    "settle_nonzero_exit",
    "settle_restore_refused",
    "_enter_mailbox_reply_wait",
    "_mailbox_pause_resolution_details",
    "settle_runner_pause",
    "MailboxContinuation",
    "RedirectContinuation",
    "RunContext",
    "SparkRetry",
    "REDIRECT_COMPLETION_NOTE",
    "REDIRECT_COMPLETION_REASON",
    "REDIRECT_OUTCOME_BLOCKED",
    "REDIRECT_OUTCOME_CONTINUED",
    "REDIRECT_OUTCOME_STOPPED",
    "_record_redirect_continuation",
    "settle_redirected_turn",
]


def _record_uninspected_final_snapshot(
    ctx: turn_disposition_models.RunContext,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
) -> None:
    """Publish the newest plan image this run actually holds.

    The after-image when the turn produced one and the before-image otherwise:
    both branches below can be reached by a turn that never got re-inspected,
    and a run that reported no final snapshot at all would be indistinguishable
    from one that never started a turn.
    """
    with ctx.persistence.lock:
        ctx.payload["final_snapshot"] = (
            plans_payloads.snapshot_payload(after)
            if after is not None
            else plans_payloads.snapshot_payload(before)
        )


def pause_interrupted_run(
    ctx: turn_disposition_models.RunContext,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    session_details: dict[str, Any],
) -> None:
    """Stop a run whose turn was interrupted by the operator's own terminal."""
    _record_uninspected_final_snapshot(ctx, before=before, after=after)
    ctx.pause(
        reason="interrupted",
        message="run interrupted by user",
        details=session_details,
    )


def pause_timed_out_turn(
    ctx: turn_disposition_models.RunContext,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    session_label: str,
    session_details: dict[str, Any],
) -> None:
    """Stop a run whose harness turn outlived its wall-clock budget."""
    _record_uninspected_final_snapshot(ctx, before=before, after=after)
    ctx.pause(
        reason="timeout",
        message=f"harness turn timed out for {session_label}",
        details=session_details,
    )


def record_aborted_turn(
    ctx: turn_disposition_models.RunContext,
    listener: run_control.RunControlListener,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    session_id: str,
    turn_payload: dict[str, Any],
) -> None:
    """Record the clean early stop that follows an operator-killed turn.

    Beside the timeout branch, but not a pause: the operator asked for this
    stop, so it is recorded as one. The control lock is taken here because the
    abort is consumed from the same listener state the poller writes.
    """
    with ctx.persistence.lock:
        run_termination._honor_abort_turn(
            listener,
            snapshot=after if after is not None else before,
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


def settle_malformed_plan(
    ctx: turn_disposition_models.RunContext,
    *,
    before: plans_models.PlanSnapshot,
    workspace: workspaces.ResolvedWorkspace | None,
    session_id: str,
    turn_index: int,
    plan_error: str | None,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    session_details: dict[str, Any],
) -> plans_models.PlanSnapshot | None:
    """Try to make the plan readable again, and pause when it cannot be.

    Returns the corrected snapshot the next pass retries from, or ``None`` when
    the plan stayed uninspectable and the run paused. The two repairs are
    ordered and not interchangeable: a correction rewrites the handoff the
    worker malformed, and only once correcting is exhausted does the reopen
    hand the session back as pending so a later run can try again.
    """
    corrected, correction_evidence = handoff_correction._correct_malformed_handoff(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        plan_cache=ctx.plan_cache,
        before=before,
        workspace=workspace,
        run_id=ctx.run_id,
        turn_index=turn_index,
        session_id=session_id,
        plan_error=plan_error,
        turn_payload=turn_payload,
        paths=paths,
    )
    if corrected is not None:
        return corrected
    malformed_details: dict[str, Any] = {
        "error": plan_error,
        **session_details,
    }
    if correction_evidence is not None:
        malformed_details["handoff_correction"] = correction_evidence
    reopened_snapshot, reopen_evidence = (
        handoff_correction._reopen_uninspectable_handoff(
            plan_cache=ctx.plan_cache,
            before=before,
            session_id=session_id,
            plan_error=plan_error,
            correction_exhausted=bool(
                correction_evidence is not None and correction_evidence.get("exhausted")
            ),
        )
    )
    if reopen_evidence is not None:
        malformed_details["handoff_reopen"] = reopen_evidence
    ctx.record_final_snapshot(
        before if reopened_snapshot is None else reopened_snapshot
    )
    turn_execution_commit.record_turn_rejected(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="plan-inspection",
        reason="malformed-plan",
        reopened=bool(reopen_evidence is not None and reopen_evidence["reopened"]),
        detail={"error": plan_error},
    )
    ctx.pause(
        reason="malformed-plan",
        message="plan could not be inspected after the harness turn",
        details=malformed_details,
    )
    return None
