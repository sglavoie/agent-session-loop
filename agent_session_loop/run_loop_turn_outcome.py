"""Loop pass: act on the turn's disposition and settle it."""

from __future__ import annotations

import functools

from . import (
    commit_settlement,
    continuation_guards,
    handoff_correction,
    handoff_guards,
    redirect_continuation,
    run_coordination_control_drain,
    run_coordination_mailbox_wait,
    run_inputs,
    run_termination,
    session_verification,
    state,
    turn_dispositions,
    turn_execution_commit,
    turn_settlement,
)
from .run_loop_commit_settlement import _soft_fail_commit
from .run_loop_frame import LoopSignal, RunFrame, RunResult


def _settle_turn_outcome(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Handle terminating dispositions, settle the turn and decide whether to stop."""
    if frame.postflight.stopped:
        if frame.result.aborted:
            with frame.run_control_lock:
                run_termination._record_aborted_turn_exit(
                    frame.ctx.run_controls,
                    payload=frame.ctx.payload,
                    event_path=frame.ctx.event_path,
                    control_payload=frame.ctx.control_payload,
                    context=frame.turn_payload,
                    persistence=frame.ctx.persistence,
                )
        return LoopSignal.BREAK
    # The one arming site, and it sits here rather than at any pause
    # because of what is above it: every post-turn write guard has run
    # and reported. A write-scope violation or a protected-path change
    # stops inside `enforce_post_turn_guards` and leaves the loop on
    # the line above, so those pauses structurally never arm this and
    # the dirt they retain for a human to inspect is never a candidate
    # for a commit. That guarantee is the control flow, not a blacklist
    # of reasons the settlement below has to keep in step with.
    frame.ctx.retained_work = commit_settlement.RetainedWorkContext(
        session_id=frame.session_id,
        session_label=frame.session_label,
        turn_payload=frame.turn_payload,
        result_path=frame.paths["result"],
        artifact_dir=frame.paths["artifact_dir"],
        is_review_session=frame.is_review_session,
        is_repair_session=frame.is_repair_session,
    )
    terminating_disposition = frame.postflight.terminating_disposition
    # The same site, for the same reason: an attempt only joins the
    # session's accumulated evidence once every post-turn write guard
    # has cleared it. A refused turn left the loop above, so its paths
    # can never be carried into a later attempt's commit scope -- the
    # control flow guarantees it rather than a list of reasons the merge
    # would have to keep in step with. The record is the guard's own
    # evidence as it now stands, which is already cumulative, so the
    # next attempt merges against one record instead of a history.
    frame.ctx.session_attempts.record(
        session_id=frame.session_id,
        turn_index=frame.turn_index,
        disposition=terminating_disposition,
        guard=frame.turn_payload.get("workspace_write_guard"),
    )
    with frame.ctx.persistence.lock:
        frame.ctx.payload[continuation_guards.SESSION_ATTEMPTS_PAYLOAD_KEY] = (
            frame.ctx.session_attempts.to_payload()
        )
    frame.ctx.persistence.write_state(frame.ctx.payload)

    if terminating_disposition == "interrupted":
        turn_dispositions.pause_interrupted_run(
            frame.ctx,
            before=frame.before,
            after=frame.after,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            result=frame.result,
            turn_payload=frame.turn_payload,
            paths=frame.paths,
            session_details=frame.session_details,
        )
        return LoopSignal.BREAK

    if terminating_disposition == redirect_continuation.DISPOSITION_REDIRECTED:
        frame.redirected = turn_dispositions.settle_redirected_turn(
            frame.ctx,
            frame.ctx.run_controls,
            before=frame.before,
            after=frame.after,
            redirect=frame.turn_redirect,
            harness=frame.turn_harness,
            conversation_id=(
                frame.result.conversation_id
                or frame.continuation.resume_conversation_id
            ),
            session_id=frame.session_id,
            session_label=frame.session_label,
            turn_index=frame.turn_index,
            result=frame.result,
            turn_payload=frame.turn_payload,
            paths=frame.paths,
            session_details=frame.session_details,
            pending=frame.pending_redirect,
        )
        if frame.redirected is None:
            return LoopSignal.BREAK
        frame.pending_redirect = frame.redirected.pending
        if frame.redirected.snapshot is not None:
            frame.snapshot = frame.redirected.snapshot
            return LoopSignal.CONTINUE
        # The plan could not be inspected after the forced stop. The
        # correction is already held above, so it rides along into
        # whatever attempt the repair below produces rather than being
        # spent on a turn that has no plan image to run against.
        terminating_disposition = "malformed-plan"

    if terminating_disposition == "timeout":
        turn_dispositions.pause_timed_out_turn(
            frame.ctx,
            before=frame.before,
            after=frame.after,
            session_id=frame.session_id,
            session_label=frame.session_label,
            turn_index=frame.turn_index,
            result=frame.result,
            turn_payload=frame.turn_payload,
            paths=frame.paths,
            session_details=frame.session_details,
        )
        return LoopSignal.BREAK

    if terminating_disposition == "aborted":
        turn_dispositions.record_aborted_turn(
            frame.ctx,
            frame.ctx.run_controls,
            before=frame.before,
            after=frame.after,
            session_id=frame.session_id,
            turn_payload=frame.turn_payload,
        )
        return LoopSignal.BREAK
    if terminating_disposition == "restore-refused":
        turn_dispositions.settle_restore_refused(
            frame.ctx,
            plan_state=state.plan_state_dir(
                frame.before.plan_dir, frame.args.state_dir
            ),
            before=frame.before,
            after=frame.after,
            result=frame.result,
            continuation=frame.continuation,
            session_id=frame.session_id,
            session_label=frame.session_label,
            turn_index=frame.turn_index,
            turn_payload=frame.turn_payload,
            session_details=frame.session_details,
            paths=frame.paths,
        )
        return LoopSignal.BREAK

    if terminating_disposition == "malformed-plan":
        frame.corrected = turn_dispositions.settle_malformed_plan(
            frame.ctx,
            before=frame.before,
            workspace=frame.workspace,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            plan_error=frame.plan_error,
            turn_payload=frame.turn_payload,
            paths=frame.paths,
            session_details=frame.session_details,
        )
        if frame.corrected is not None:
            frame.snapshot = frame.corrected
            return LoopSignal.CONTINUE
        return LoopSignal.BREAK

    # Every disposition that survives the branches above names a turn
    # the plan could be inspected after, so the malformed-plan branch
    # owns the only `after is None` turn that reaches here.
    assert frame.after is not None
    frame.after, paused_status_repair = (
        handoff_correction._repair_paused_handoff_status(
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            persistence=frame.ctx.persistence,
            plan_cache=frame.ctx.plan_cache,
            after=frame.after,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            runner_pause=frame.turn_payload.get("runner_pause"),
        )
    )
    frame.invalid_pause = run_inputs._invalid_status_pause(frame.after)
    if frame.invalid_pause is not None:
        frame.reason, frame.message, frame.details = frame.invalid_pause
        frame.ctx.record_final_snapshot(frame.after)
        turn_execution_commit.record_turn_rejected(
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            persistence=frame.ctx.persistence,
            turn_payload=frame.turn_payload,
            session_id=frame.session_id,
            guard="plan-status-validity",
            reason=frame.reason,
            detail=frame.details,
        )
        frame.ctx.pause(
            reason=frame.reason,
            message=frame.message,
            details=frame.details | frame.session_details,
        )
        return LoopSignal.BREAK

    selected_after = next(
        (item for item in frame.after.sessions if item.id == frame.session_id),
        None,
    )
    if (
        frame.mailbox_enabled
        and selected_after is not None
        and selected_after.status == "complete"
        and handoff_guards._enforce_mailbox_completion_guard(
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            persistence=frame.ctx.persistence,
            after=frame.after,
            session_id=frame.session_id,
            session_label=frame.session_label,
            session_details=frame.session_details,
            turn_index=frame.turn_index,
            state_dir_arg=frame.args.state_dir,
            turn_payload=frame.turn_payload,
            paths=frame.paths,
            plan_cache=frame.ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
        )
    ):
        return LoopSignal.BREAK

    if terminating_disposition == "runner-pause":
        frame.continuation = turn_dispositions.settle_runner_pause(
            frame.ctx,
            after=frame.after,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            result=frame.result,
            turn_payload=frame.turn_payload,
            session_details=frame.session_details,
            paused_status_repair=paused_status_repair,
            mailbox_enabled=frame.mailbox_enabled,
            mailbox_poll=frame.mailbox_poll,
            plan_state=frame.state_dir,
            state_path=frame.state_path,
            state_dir_arg=frame.args.state_dir,
            # Resolved here rather than inside the boundary so this
            # module keeps owning all three names: callers patch them
            # on the runner to assert a wait was or was not entered.
            prepare_wait=run_coordination_mailbox_wait.prepare_mailbox_reply_wait,
            wait_for_reply=run_coordination_mailbox_wait.wait_for_mailbox_reply,
            finish_lifecycle=run_coordination_mailbox_wait.finish_mailbox_reply_wait_lifecycle,
        )
        if frame.continuation is not None:
            frame.snapshot = frame.continuation.snapshot
            frame.pending_reply = frame.continuation.pending_reply
            frame.pending_resolution = frame.continuation.pending_resolution
            return LoopSignal.CONTINUE
        return LoopSignal.BREAK
    if terminating_disposition == "nonzero-exit":
        frame.retry = turn_dispositions.settle_nonzero_exit(
            frame.ctx,
            before=frame.before,
            after=frame.after,
            result=frame.result,
            spark_route=frame.spark_route,
            session_id=frame.session_id,
            session_label=frame.session_label,
            turn_index=frame.turn_index,
            turn_payload=frame.turn_payload,
            session_details=frame.session_details,
            paths=frame.paths,
            state_dir_arg=frame.args.state_dir,
            spark_run_suppression=frame.spark_run_suppression,
        )
        if frame.retry is not None:
            frame.snapshot = frame.retry.snapshot
            frame.spark_fallback_reason = frame.retry.fallback_reason
            frame.spark_run_suppression = frame.retry.run_suppression
            return LoopSignal.CONTINUE
        return LoopSignal.BREAK

    settlement = turn_settlement.settle_turn(
        frame.ctx,
        args=frame.args,
        cwd=frame.cwd,
        before=frame.before,
        after=frame.after,
        snapshot=frame.snapshot,
        session_id=frame.session_id,
        session_label=frame.session_label,
        session_details=frame.session_details,
        turn_index=frame.turn_index,
        total_sessions=frame.total_sessions,
        turn_payload=frame.turn_payload,
        paths=frame.paths,
        plan_artifacts_before=frame.plan_artifacts_before,
        plan_artifacts_written_by_runner=frame.plan_artifacts_written_by_runner,
        is_review_session=frame.is_review_session,
        is_repair_session=frame.is_repair_session,
        review_session_version=frame.review_session_version,
        allow_not_loop_ready=frame.allow_not_loop_ready,
        commit_mode=frame.commit_mode,
        max_review_fix_cycles=frame.max_review_fix_cycles,
        review_requested=frame.review_requested,
        turn_executor=frame.turn_executor,
        live_turn_payload=frame.live_turn_payload,
        run_controls=frame.ctx.run_controls,
        run_control_lock=frame.run_control_lock,
        wakeup=frame.wakeup,
        soft_fail_commit=functools.partial(_soft_fail_commit, frame.ctx),
    )
    frame.snapshot = settlement.snapshot
    if settlement.action == turn_settlement.ACTION_STOP:
        return LoopSignal.BREAK
    if settlement.action == turn_settlement.ACTION_RETRY:
        return LoopSignal.CONTINUE

    run_coordination_control_drain.drain_external_control_queue(
        plan_dir=frame.snapshot.plan_dir,
        state_dir_arg=frame.args.state_dir,
        control_state=frame.ctx.run_controls.state,
        seen_ids=frame.ctx.seen_external_command_ids,
        payload=frame.ctx.payload,
        event_path=frame.ctx.event_path,
        control_payload=frame.ctx.control_payload,
        context=settlement.turn_record,
        persistence=frame.ctx.persistence,
        run_control_lock=frame.run_control_lock,
        wakeup=frame.wakeup,
        guard_controller=frame.ctx.guard_controller,
    )
    with frame.run_control_lock:
        # An unpending request is left set on purpose: the all-complete
        # branch at the top of the loop owns it, so a stop is never
        # swallowed by a run that still has a review to append.
        frame.stop_now = (
            frame.ctx.run_controls.state.stop_after_current
            and frame.snapshot.next_pending is not None
        )
        if frame.stop_now:
            run_termination._honor_stop_after_current(
                frame.ctx.run_controls,
                snapshot=frame.snapshot,
                payload=frame.ctx.payload,
                event_path=frame.ctx.event_path,
                control_payload=frame.ctx.control_payload,
                context=settlement.turn_record,
                persistence=frame.ctx.persistence,
            )
    if frame.stop_now:
        return LoopSignal.BREAK
    return None
