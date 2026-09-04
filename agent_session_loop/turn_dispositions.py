"""What the run does with a turn that ended in something other than progress.

``turn_postflight`` names the disposition; this boundary is what that name
selects. It owns the six terminating branches of the session loop -- the ways a
turn can end that are not "the session advanced" -- and the one retry path
hidden among them.

Six branches, in the order the loop tests them, which is the whole of what this
module owns:

* **interrupted** and **timeout** end a turn the plan could not be re-inspected
  after. Both publish the last plan image the run actually holds -- the
  after-image when one was read, the before-image otherwise -- and pause.
* **aborted** sits beside them and is deliberately *not* a pause: an operator
  asked for the stop, so it is recorded as one.
* **malformed-plan** is the only branch that may retry. It is the sole owner of
  the ``after is None`` turn, which is why it runs ahead of the status guards
  the rest of the loop depends on an after-image for.
* **runner-pause** carries the reply wait: a worker that paused with a question
  can be answered mid-run, and an answer turns the pause back into a turn.
* **nonzero-exit** carries the spark retry: a Spark turn that died on an
  exhausted quota or a rejected model falls back to the baseline model and
  retries the same session, provided the turn left the plan untouched.

Durable records this boundary writes: the run's final snapshot on every path
that ends the run, the rejection event for a refused turn, the spark fallback
event and re-written result artifact for a retry, and the mailbox wait's own
lifecycle. Every pause is recorded by ``run_termination`` through ``ctx.pause``,
and every write goes through the run's persistence seam.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol below, naming only the members these
branches reach. The three mailbox wait
functions enter as callbacks rather than being called on ``run_coordination``
directly, for the reason ``MailboxWaitCoordinator`` already states: callers
patch them to assert a wait was or was not entered. Session 37 repointed those
patches from the runner's retired aliases to ``run_coordination`` itself, which
is where the loop now resolves them; a branch that imported them here instead
would bind them once and silently ignore the patch.

Each of the three branches the loop can survive returns ``None`` when it ended
the run and a continuation object when it did not, rather than performing the
loop's ``break``: a boundary cannot break a loop it does not own, and "no
continuation" is the more honest name for a run that stopped than an action
enum whose other half would have to carry a snapshot that is never read.

Three invariants survive the move:

* **The loop still names its own branch.** The six ``if terminating_disposition
  == ...`` tests stay in the loop, each delegating its body here and then taking
  its own edge. The dispatch is the loop's contract with the disposition; only
  the work behind it moved.
* **A retried turn re-enters the loop with the plan image the retry was decided
  against.** Both retry paths hand back the snapshot the next pass must start
  from, because a pass that re-read the plan instead could adopt a change made
  after the decision.
* **The reply wait pins the idle baseline itself.** The wait is genuine idle
  time, and the pin is this path's explicit opt-out from the structural refresh
  at the top of the loop -- so both the stamp and the pin have to happen here,
  where the wait is entered, not at the call site.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    durability,
    events,
    handoff_correction,
    handoff_guards,
    harness_failures,
    harnesses,
    mailbox,
    model_availability,
    plans,
    run_control,
    run_coordination,
    run_persistence,
    run_prompts,
    run_selection,
    run_termination,
    session_verification,
    state,
    turn_execution,
    workspaces,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as the terminating branches need it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members these six
    branches reach are named: the run payload and event log every record lands
    in, the persistence seam that serializes them, the plan cache a correction
    re-reads the plan from, the run and control identifiers the records are
    attributed to, the two idle-clock fields the reply wait owns, and the two
    ways a turn ends the run.

    The idle-clock fields are plain attributes rather than properties because
    the reply wait assigns both.
    """

    runner_work_finished_at: datetime | None
    idle_baseline_pinned: bool

    @property
    def run_id(self) -> str: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any]: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def plan_cache(self) -> plans.PlanSnapshotCache: ...

    @property
    def control_payload(self) -> dict[str, Any]: ...

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What a surviving branch hands back
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class MailboxContinuation:
    """The turn a mailbox answer turned a runner pause back into.

    Exactly one of the two continuation fields is set: a reply carries text the
    next turn resumes from, a resolution carries an operator's decision to
    continue without one. ``snapshot`` is the after-image the answer was given
    against, and the next pass starts from it rather than re-reading the plan.
    """

    snapshot: plans.PlanSnapshot
    pending_reply: run_prompts._PendingReply | None
    pending_resolution: run_prompts._PendingResolution | None


@dataclasses.dataclass(frozen=True)
class SparkRetry:
    """The same session, queued again on the baseline model.

    ``run_suppression`` is echoed rather than freshly decided: it outlives the
    turn, and a retry that could not write the shared availability ledger is
    the only thing that sets it. ``fallback_reason`` is per-turn, and the next
    pass reports it on the route it selects.
    """

    snapshot: plans.PlanSnapshot
    fallback_reason: str
    run_suppression: str | None


# ---------------------------------------------------------------------------
# Turns the plan could not be inspected after
# ---------------------------------------------------------------------------


def _record_uninspected_final_snapshot(
    ctx: RunContext,
    *,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
) -> None:
    """Publish the newest plan image this run actually holds.

    The after-image when the turn produced one and the before-image otherwise:
    both branches below can be reached by a turn that never got re-inspected,
    and a run that reported no final snapshot at all would be indistinguishable
    from one that never started a turn.
    """
    with ctx.persistence.lock:
        ctx.payload["final_snapshot"] = (
            plans.snapshot_payload(after)
            if after is not None
            else plans.snapshot_payload(before)
        )


def pause_interrupted_run(
    ctx: RunContext,
    *,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
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
    ctx: RunContext,
    *,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
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
    ctx: RunContext,
    listener: run_control.RunControlListener,
    *,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
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
    ctx: RunContext,
    *,
    before: plans.PlanSnapshot,
    workspace: workspaces.ResolvedWorkspace | None,
    session_id: str,
    turn_index: int,
    plan_error: str | None,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    session_details: dict[str, Any],
) -> plans.PlanSnapshot | None:
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
    turn_execution.record_turn_rejected(
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


# ---------------------------------------------------------------------------
# Turns the plan was inspected after
# ---------------------------------------------------------------------------


def _mailbox_pause_resolution_details(
    resolution: mailbox.MailboxMessage,
    *,
    run_id: str,
    session_id: str,
    turn_index: int,
) -> dict[str, Any]:
    """Describe the answer that arrived before the pause was even entered."""
    return {
        "run_id": run_id,
        "session_id": session_id,
        "turn_index": turn_index,
        "waiter_origin": mailbox.WAITER_ORIGIN_WORKER,
        "question_id": resolution.to,
        "resolution_message_id": resolution.id,
        "resolution_seq": resolution.seq,
        "action": resolution.action,
        "note_length": (None if resolution.note is None else len(resolution.note)),
    }


def _enter_mailbox_reply_wait(
    ctx: RunContext,
    prepared: run_coordination.PreparedMailboxWait,
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    mailbox_poll: run_coordination.MailboxPoll,
    wait_for_reply: Callable[..., run_coordination.MailboxWaitResult],
    finish_lifecycle: Callable[..., run_coordination.MailboxWaitResult],
) -> run_coordination.MailboxWaitResult:
    """Block on the question this pause asked, with the idle clock stopped.

    The wait that follows is genuine idle time: the runner has no work left for
    this turn until a human answers, so the clock starts before the wait.
    Pinning it is this path's explicit opt-out from the structural refresh at
    the top of the loop.
    """
    ctx.runner_work_finished_at = clock.utc_now()
    ctx.idle_baseline_pinned = True
    return run_coordination.MailboxWaitCoordinator(
        plan_dir=plan_dir,
        state_dir_arg=state_dir_arg,
        prepared=prepared,
        run_id=ctx.run_id,
        store=run_coordination.WaitStateStore(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
        ),
        persistence=ctx.persistence,
        notifier=run_persistence._AgentMuxWaitNotifier(ctx.payload, ctx.persistence),
        poll_controls=mailbox_poll,
        # Resolved by the caller rather than inside the coordinator so the
        # runner keeps owning both names: callers patch them on the runner to
        # assert a wait was or was not entered.
        wait_for_reply=wait_for_reply,
        finish_lifecycle=finish_lifecycle,
    ).run()


def settle_runner_pause(
    ctx: RunContext,
    *,
    after: plans.PlanSnapshot,
    session_id: str,
    turn_index: int,
    result: harnesses.HarnessResult,
    turn_payload: dict[str, Any],
    session_details: dict[str, Any],
    paused_status_repair: dict[str, Any] | None,
    mailbox_enabled: bool,
    mailbox_poll: run_coordination.MailboxPoll,
    plan_state: Path,
    state_path: Path,
    state_dir_arg: str | None,
    prepare_wait: Callable[..., run_coordination.PreparedMailboxWait],
    wait_for_reply: Callable[..., run_coordination.MailboxWaitResult],
    finish_lifecycle: Callable[..., run_coordination.MailboxWaitResult],
) -> MailboxContinuation | None:
    """Honour a worker's own pause, waiting for an answer where one can arrive.

    Returns the continuation an answer produced, or ``None`` when the run
    paused. A resolution already recorded against this turn short-circuits the
    wait entirely -- the answer beat the pause into the mailbox -- and only a
    pause the worker could still be answered out of enters the live wait.
    """
    runner_pause = turn_payload.get("runner_pause")
    pause_reason = turn_execution.runner_pause_reason(str(runner_pause))
    ctx.record_final_snapshot(after)
    pause_details: dict[str, Any] = {
        **session_details,
        "source": turn_payload.get("runner_pause_source"),
    }
    if paused_status_repair is not None:
        pause_details["handoff_status_repair"] = paused_status_repair
    worker_pause_resolution = (
        run_coordination.worker_pause_resolution_for_turn(
            plan_dir=after.plan_dir,
            state_dir_arg=state_dir_arg,
            run_id=ctx.run_id,
            session_id=session_id,
            turn_index=turn_index,
        )
        if mailbox_enabled
        else None
    )
    if worker_pause_resolution is not None:
        pause_details["mailbox_resolution"] = _mailbox_pause_resolution_details(
            worker_pause_resolution,
            run_id=ctx.run_id,
            session_id=session_id,
            turn_index=turn_index,
        )
        ctx.pause(
            reason=pause_reason,
            message=str(runner_pause),
            details=pause_details,
        )
        return None
    if run_coordination.runner_pause_can_wait_for_mailbox_reply(
        mailbox_enabled=mailbox_enabled,
        pause_reason=pause_reason,
        result=result,
    ):
        try:
            prepared_wait = prepare_wait(
                snapshot=after,
                payload=ctx.payload,
                plan_state=plan_state,
                state_path=state_path,
                state_dir_arg=state_dir_arg,
                run_id=ctx.run_id,
                session_id=session_id,
                turn_index=turn_index,
            )
        except run_coordination.MailboxWaitUnavailable as error:
            run_coordination.record_mailbox_reply_wait_preparation_failed(
                ctx.event_path,
                ctx.payload,
                session_id=session_id,
                turn_index=turn_index,
                reason=error.reason,
                persistence=ctx.persistence,
            )
            prepared_wait = None

        if prepared_wait is not None:
            wait_result = _enter_mailbox_reply_wait(
                ctx,
                prepared_wait,
                plan_dir=after.plan_dir,
                state_dir_arg=state_dir_arg,
                mailbox_poll=mailbox_poll,
                wait_for_reply=wait_for_reply,
                finish_lifecycle=finish_lifecycle,
            )

            if wait_result.reply is not None:
                reply_message = wait_result.reply
                assert reply_message.text is not None
                # The session remains pending; start its explicit reply
                # continuation before honoring any stop-after-current request.
                return MailboxContinuation(
                    snapshot=after,
                    pending_reply=run_prompts._PendingReply(
                        text=reply_message.text,
                        context=prepared_wait.context,
                        origin=run_prompts.REPLY_ORIGIN_MAILBOX,
                        process_start=False,
                        question_id=prepared_wait.question.id,
                        reply_message_id=reply_message.id,
                        reply_seq=reply_message.seq,
                    ),
                    pending_resolution=None,
                )
            if wait_result.resolution is not None:
                resolution_message = wait_result.resolution
                assert resolution_message.action is not None
                if resolution_message.action == mailbox.RESOLUTION_ACTION_CONTINUE:
                    # The session remains pending; schedule its answerless
                    # continuation before honoring any stop-after-current
                    # request.
                    return MailboxContinuation(
                        snapshot=after,
                        pending_reply=None,
                        pending_resolution=run_prompts._PendingResolution(
                            action=resolution_message.action,
                            note=resolution_message.note,
                            context=prepared_wait.context,
                            question_id=prepared_wait.question.id,
                            resolution_message_id=resolution_message.id,
                            resolution_seq=resolution_message.seq,
                        ),
                    )
                assert resolution_message.action == mailbox.RESOLUTION_ACTION_PAUSE
    ctx.pause(
        reason=pause_reason,
        message=str(runner_pause),
        details=pause_details,
    )
    return None


def _record_spark_fallback(
    ctx: RunContext,
    *,
    after: plans.PlanSnapshot,
    classification: harness_failures.FailureClassification,
    spark_route: dict[str, Any],
    spark_status_update: dict[str, Any],
    fallback_reason: str,
    session_id: str,
    turn_index: int,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
) -> None:
    """Commit the route change, the turn artifact, and the fallback event.

    All under one lock: the retry reads its model from the payload the next
    pass sees, so a reader that caught the route half-updated would launch the
    retry against the model that just failed.
    """
    with ctx.persistence.lock:
        spark_route["fallback_reason"] = fallback_reason
        turn_payload["spark_route"] = dict(spark_route)
        ctx.payload["spark"].update(spark_status_update)
        ctx.payload["spark"]["active_route"] = dict(spark_route)
        ctx.payload["final_snapshot"] = plans.snapshot_payload(after)
        durability.write_json_file(paths["result"], turn_payload)
        ctx.persistence.append_event(
            ctx.event_path,
            "spark_fallback",
            {
                "run_id": ctx.run_id,
                "session_id": session_id,
                "failed_turn_index": turn_index,
                "failed_model": run_selection.SPARK_MODEL,
                "fallback_model": ctx.payload["spark"]["baseline"],
                "failure_classification": classification.to_dict(),
                "retry_at": ctx.payload["spark"].get("retry_at"),
            },
        )
        ctx.persistence.write_state(ctx.payload)


def settle_nonzero_exit(
    ctx: RunContext,
    *,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
    result: harnesses.HarnessResult,
    spark_route: dict[str, Any],
    session_id: str,
    session_label: str,
    turn_index: int,
    turn_payload: dict[str, Any],
    session_details: dict[str, Any],
    paths: dict[str, Path],
    state_dir_arg: str | None,
    spark_run_suppression: str | None,
) -> SparkRetry | None:
    """Classify a failed harness exit, retrying off Spark where that is safe.

    Returns the retry the fallback scheduled, or ``None`` when the run paused.
    A Spark turn that died on an exhausted quota or a rejected model is only
    retryable when it left no trace: the session still pending and the plan
    byte-identical to the before-image. Anything else may have half-completed,
    so it pauses and says why.
    """
    classification = harness_failures.classify_harness_failure(result)
    spark_failure = spark_route["decision"] == "spark" and classification.reason in {
        events.PAUSE_REASON_QUOTA_EXHAUSTED,
        events.PAUSE_REASON_INVALID_MODEL,
    }
    selected_after = next(
        (item for item in after.sessions if item.id == session_id),
        None,
    )
    safely_retryable = (
        selected_after is not None
        and selected_after.status == "pending"
        and after.content_sha256 == before.content_sha256
    )
    if spark_failure and safely_retryable:
        try:
            cooldown = model_availability.suppress_until(
                state.state_root(state_dir_arg),
                run_selection.SPARK_MODEL,
                reason=classification.reason,
                retry_hint=classification.retry_at,
                run_id=ctx.run_id,
            )
        except OSError as error:
            cooldown = None
            with ctx.persistence.lock:
                ctx.payload["spark"]["warning"] = (
                    f"could not update model availability ledger: {error}"
                )
        fallback_reason = classification.reason
        if cooldown is None:
            spark_run_suppression = f"{classification.reason}-for-current-run"
            spark_status_update = {
                "available": False,
                "availability": False,
                "reason": spark_run_suppression,
                "retry_at": None,
            }
        else:
            spark_status_update = cooldown.to_dict()
        _record_spark_fallback(
            ctx,
            after=after,
            classification=classification,
            spark_route=spark_route,
            spark_status_update=spark_status_update,
            fallback_reason=fallback_reason,
            session_id=session_id,
            turn_index=turn_index,
            turn_payload=turn_payload,
            paths=paths,
        )
        return SparkRetry(
            snapshot=after,
            fallback_reason=fallback_reason,
            run_suppression=spark_run_suppression,
        )
    if classification.reason == events.PAUSE_REASON_NONZERO_EXIT:
        pause_message = (
            f"harness exited with status {result.returncode} for {session_label}"
        )
    else:
        pause_message = (
            f"{session_label} failed: {classification.reason} "
            f"(harness exit {result.returncode})"
        )
    ctx.record_final_snapshot(after)
    ctx.pause(
        reason=classification.reason,
        message=pause_message,
        details={
            **session_details,
            "returncode": result.returncode,
            "failure_classification": classification.to_dict(),
            **(
                {"spark_fallback_unsafe": True}
                if spark_failure and not safely_retryable
                else {}
            ),
        },
    )
    return None
