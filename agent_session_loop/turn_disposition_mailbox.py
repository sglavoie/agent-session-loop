"""Turn worker pauses into mailbox reply waits and continuation decisions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import (
    clock,
    harnesses,
    mailbox_models,
    mailbox_vocabulary,
    plans_models,
    run_coordination_mailbox_poll,
    run_coordination_mailbox_question,
    run_coordination_mailbox_wait,
    run_coordination_wait_models,
    run_coordination_wait_state,
    run_persistence,
    run_prompts,
    turn_disposition_models,
    turn_execution_signals,
)


def _mailbox_pause_resolution_details(
    resolution: mailbox_models.MailboxMessage,
    *,
    run_id: str,
    session_id: str,
    turn_index: int,
    waiter_origin: mailbox_vocabulary.WaiterOrigin = (
        mailbox_vocabulary.WAITER_ORIGIN_WORKER
    ),
    question_id: str | None = None,
) -> dict[str, Any]:
    """Describe the resolution that closed a worker or runner question."""
    return {
        "run_id": run_id,
        "session_id": session_id,
        "turn_index": turn_index,
        "waiter_origin": waiter_origin,
        "question_id": question_id or resolution.to,
        "resolution_message_id": resolution.id,
        "resolution_seq": resolution.seq,
        "action": resolution.action,
        "note_length": (None if resolution.note is None else len(resolution.note)),
    }


def _enter_mailbox_reply_wait(
    ctx: turn_disposition_models.RunContext,
    prepared: run_coordination_wait_models.PreparedMailboxWait,
    *,
    plan_dir: Path,
    state_dir_arg: str | None,
    mailbox_poll: run_coordination_mailbox_poll.MailboxPoll,
    wait_for_reply: Callable[..., run_coordination_wait_models.MailboxWaitResult],
    finish_lifecycle: Callable[..., run_coordination_wait_models.MailboxWaitResult],
) -> run_coordination_wait_models.MailboxWaitResult:
    """Block on the question this pause asked, with the idle clock stopped.

    The wait that follows is genuine idle time: the runner has no work left for
    this turn until a human answers, so the clock starts before the wait.
    Pinning it is this path's explicit opt-out from the structural refresh at
    the top of the loop.
    """
    ctx.runner_work_finished_at = clock.utc_now()
    ctx.idle_baseline_pinned = True
    return run_coordination_mailbox_wait.MailboxWaitCoordinator(
        plan_dir=plan_dir,
        state_dir_arg=state_dir_arg,
        prepared=prepared,
        run_id=ctx.run_id,
        store=run_coordination_wait_state.WaitStateStore(
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
    ctx: turn_disposition_models.RunContext,
    *,
    after: plans_models.PlanSnapshot,
    session_id: str,
    turn_index: int,
    result: harnesses.HarnessResult,
    turn_payload: dict[str, Any],
    session_details: dict[str, Any],
    paused_status_repair: dict[str, Any] | None,
    mailbox_enabled: bool,
    mailbox_poll: run_coordination_mailbox_poll.MailboxPoll,
    plan_state: Path,
    state_path: Path,
    state_dir_arg: str | None,
    prepare_wait: Callable[..., run_coordination_wait_models.PreparedMailboxWait],
    wait_for_reply: Callable[..., run_coordination_wait_models.MailboxWaitResult],
    finish_lifecycle: Callable[..., run_coordination_wait_models.MailboxWaitResult],
) -> turn_disposition_models.MailboxContinuation | None:
    """Honour a worker's own pause, waiting for an answer where one can arrive.

    Returns the continuation an answer produced, or ``None`` when the run
    paused. A resolution already recorded against this turn short-circuits the
    wait entirely -- the answer beat the pause into the mailbox -- and only a
    pause the worker could still be answered out of enters the live wait.
    """
    runner_pause = turn_payload.get("runner_pause")
    pause_reason = turn_execution_signals.runner_pause_reason(str(runner_pause))
    ctx.record_final_snapshot(after)
    pause_details: dict[str, Any] = {
        **session_details,
        "source": turn_payload.get("runner_pause_source"),
    }
    if paused_status_repair is not None:
        pause_details["handoff_status_repair"] = paused_status_repair
    worker_pause_resolution = (
        run_coordination_mailbox_question.worker_pause_resolution_for_turn(
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
    if run_coordination_mailbox_question.runner_pause_can_wait_for_mailbox_reply(
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
        except run_coordination_wait_models.MailboxWaitUnavailable as error:
            run_coordination_wait_state.record_mailbox_reply_wait_preparation_failed(
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
                return turn_disposition_models.MailboxContinuation(
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
                if (
                    resolution_message.action
                    == mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE
                ):
                    # The session remains pending; schedule its answerless
                    # continuation before honoring any stop-after-current
                    # request.
                    return turn_disposition_models.MailboxContinuation(
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
                assert (
                    resolution_message.action
                    == mailbox_vocabulary.RESOLUTION_ACTION_PAUSE
                )
                pause_details["mailbox_resolution"] = _mailbox_pause_resolution_details(
                    resolution_message,
                    run_id=ctx.run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    waiter_origin=mailbox_vocabulary.WAITER_ORIGIN_RUNNER,
                    question_id=prepared_wait.question.id,
                )
    ctx.pause(
        reason=pause_reason,
        message=str(runner_pause),
        details=pause_details,
    )
    return None
