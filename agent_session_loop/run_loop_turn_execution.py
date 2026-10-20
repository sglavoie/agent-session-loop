"""Loop pass: launch the turn and enforce the post-turn guards."""

from __future__ import annotations

import functools

from . import (
    conversation_resume,
    events,
    plans_handoff,
    plans_payloads,
    redirect_continuation,
    review,
    run_commit_invocation,
    run_persistence,
    run_prompts,
    run_selection,
    run_termination,
    state,
    turn_execution_artifacts,
    turn_execution_executor,
    turn_postflight,
)
from .run_loop_control_redirects import _abandon_turn, _close_turn_controls
from .run_loop_frame import LoopSignal, RunFrame, RunResult


def _execute_turn(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Launch the harness turn, finalize it and enforce the post-turn guards."""
    # Started before the launch, not after it: a correction submitted
    # against this turn's identity can land the instant that identity is
    # published, and the gate's own bind wait is what holds such an
    # acceptance until there is a worker to interrupt.
    frame.turn_redirect.start()
    execution = frame.turn_executor.launch(
        turn_execution_executor.TurnLaunch(
            env=frame.turn_env,
            harness=frame.turn_harness,
            cwd=frame.cwd,
            plan_dir=frame.before.plan_dir,
            prompt=frame.prompt,
            unsafe_bypass=run_selection._unsafe_bypass(frame.args),
            timeout=frame.timeout,
            model=frame.requested_model,
            effort=frame.requested_effort,
            abort=frame.abort_signal,
            live_delivery=frame.delivery_endpoint,
            stream_callback=frame.stream_callback,
            on_prompt_submitted=frame.turn_delivery.note_prompt_submitted,
            # The guidance ledger stays on the spawn it has always been
            # recorded at. It answers a different question than the
            # redirect journal -- which pending guidance this turn's
            # prompt was built from -- and nothing reads it back as
            # proof that a worker received anything.
            on_spawned=frame.record_guidance_delivery,
            build_invocation=functools.partial(
                run_prompts.build_harness_invocation,
                frame.turn_harness,
                cwd=frame.cwd,
                plan_dir=frame.before.plan_dir,
                unsafe_bypass=run_selection._unsafe_bypass(frame.args),
                last_message_path=frame.paths["last_message"],
                model=frame.requested_model,
                effort=frame.requested_effort,
            ),
            resume_conversation_id=frame.continuation.resume_conversation_id,
            on_conversation=frame.conversation_capture,
            on_worker=frame.turn_redirect.bind_worker,
        ),
        live_turn=frame.live_turn,
        active_turn=frame.active_turn,
        payload=frame.ctx.payload,
        after_stop=functools.partial(
            _close_turn_controls, frame.turn_redirect, frame.mailbox_poll
        ),
        on_failure=functools.partial(_abandon_turn, frame.ctx, frame.turn_redirect),
    )
    frame.result = execution.result
    frame.live_turn_payload = execution.live_turn
    # Immediately on the way out of the turn, ahead of every guard that
    # can end the run: a turn that can show it never submitted its
    # prompt is holding words that belong back in the journal, and the
    # pause it is about to take is exactly when the operator would look
    # for them. The result is what carries that showing; a turn that
    # cannot make it leaves the carry where it is.
    frame.turn_delivery.settle_unsubmitted(frame.result)
    with frame.run_control_lock:
        frame.ctx.run_control_context = None
    if frame.conversation_capture is not None:
        # Ahead of every post-turn guard, because confirmation is what
        # makes the conversation resumable and every branch below can
        # end the run. A guard that refuses the turn still leaves work
        # in the conversation the next attempt has to continue.
        frame.outcome_record = conversation_resume.record_turn_outcome(
            state.plan_state_dir(frame.before.plan_dir, frame.args.state_dir),
            capture=frame.conversation_capture,
            result=frame.result,
            run_id=frame.ctx.run_id,
            turn=frame.turn_index,
        )
        if frame.outcome_record.error is not None:
            frame.ctx.persistence.append_event(
                frame.ctx.event_path,
                events.CONVERSATION_CONTINUATION_EVENT,
                {
                    "run_id": frame.ctx.run_id,
                    **plans_payloads.session_identity_payload(frame.session_id),
                    "disposition": frame.continuation.disposition,
                    "conversation": frame.outcome_record.conversation,
                    "confirmed": frame.outcome_record.confirmed,
                    "detail": frame.outcome_record.error,
                },
            )
    frame.plan_artifacts_written_by_runner = ()
    if frame.is_review_session:
        # Stamped from the turn's own completion rather than from the
        # commit, because every guard and terminating branch below can
        # end the run before the commit that would otherwise be the
        # only record that an independent review happened at all.
        frame.executed_review_requires_artifact = (
            frame.review_session_version is not None
            and frame.review_session_version >= review.STRUCTURED_REVIEW_MIN_VERSION
        )
        with frame.ctx.persistence.lock:
            frame.ctx.payload["review_session_executed"] = frame.session_id
        frame.normalized_after_review = (
            plans_handoff.normalize_generated_review_handoff_artifacts(
                frame.before.plan_dir
            )
        )
        if frame.normalized_after_review:
            frame.normalized_review_handoffs.extend(frame.normalized_after_review)
            frame.plan_artifacts_written_by_runner = tuple(
                path.name for path in frame.normalized_after_review
            )
            with frame.ctx.persistence.lock:
                frame.ctx.payload["normalized_review_handoffs"] = [
                    str(path) for path in frame.normalized_review_handoffs
                ]
    finalization = frame.turn_executor.finalize(
        execution,
        before=frame.before,
        unredacted_prompt=(
            frame.prompt
            if frame.use_reply_prompt or frame.use_resolution_prompt
            else None
        ),
        pending_exception_guard=frame.pending_exception_guard,
    )
    frame.after = finalization.after
    frame.plan_error = finalization.plan_error
    frame.turn_payload = finalization.turn_payload
    if frame.turn_redirect.redirected or frame.turn_redirect.to_payload()["refused"]:
        # Recorded ahead of every guard because this is what names the
        # turn's disposition: `_terminating_result_disposition` reads
        # the acceptance back out of here rather than off the harness
        # result, which cannot distinguish a redirect from an abort. A
        # turn that only ever refused corrections records them too --
        # an operator whose correction arrived a moment too late is
        # owed the evidence of where it went.
        frame.turn_payload[redirect_continuation.TURN_PAYLOAD_KEY] = (
            frame.turn_redirect.to_payload()
        )
    # The turn enters run state as executed-but-unaccepted. Every guard
    # below may still amend or refuse it; `turn_execution_commit.commit_turn`
    # is what replaces this record with the published one.
    with frame.ctx.persistence.lock:
        frame.ctx.payload["turns"].append(frame.turn_payload)
        # A harness that reports no model leaves the last observation
        # standing rather than erasing it.
        if frame.turn_payload.get("observed_model") is not None:
            frame.ctx.payload.update(
                turn_execution_artifacts.observed_model_payload(
                    frame.turn_payload["observed_model"]
                )
            )
        frame.ctx.payload["model_projection"] = (
            run_commit_invocation._merged_model_projection(
                frame.ctx.payload.get("model_projection"), frame.turn_payload
            )
        )
        frame.turn_identity = {
            "run_id": frame.ctx.run_id,
            "turn_index": frame.turn_index,
            **plans_payloads.session_identity_payload(frame.session_id),
        }
        run_persistence._append_process_sweep_event(
            event_path=frame.ctx.event_path,
            persistence=frame.ctx.persistence,
            report=frame.result.process_sweep,
            phase="turn",
            identity=frame.turn_identity,
        )
        run_persistence._append_harness_degradation_event(
            event_path=frame.ctx.event_path,
            persistence=frame.ctx.persistence,
            degradation=frame.turn_payload.get("harness_degradation"),
            identity=frame.turn_identity,
        )
        frame.ctx.persistence.write_state(frame.ctx.payload)

    if frame.abort_signal.armed and not frame.result.aborted:
        # The worker ended the turn inside the grace window, so nothing
        # was killed and whatever it recorded stands: the pause or
        # completion path below owns the outcome from here.
        with frame.run_control_lock:
            run_termination._record_abort_satisfied_by_worker(
                frame.ctx.run_controls,
                payload=frame.ctx.payload,
                event_path=frame.ctx.event_path,
                control_payload=frame.ctx.control_payload,
                context=frame.turn_payload,
                persistence=frame.ctx.persistence,
            )

    frame.postflight = turn_postflight.enforce_post_turn_guards(
        frame.ctx,
        before=frame.before,
        after=frame.after,
        result=frame.result,
        turn_payload=frame.turn_payload,
        workspace=frame.workspace,
        session_scope=frame.session_scope,
        workspace_snapshot_before=frame.workspace_snapshot_before,
        legacy_snapshot_before=frame.legacy_snapshot_before,
        review_fingerprint_before=frame.review_fingerprint_before,
        external_digests_before=frame.external_digests_before,
        turn_started_ns=frame.turn_started_ns,
        plan_artifacts_before=frame.plan_artifacts_before,
        plan_artifacts_written_by_runner=frame.plan_artifacts_written_by_runner,
        is_review_session=frame.is_review_session,
        cwd=frame.cwd,
        session_id=frame.session_id,
        session_label=frame.session_label,
        session_details=frame.session_details,
        review_guard_excludes=frame.review_guard_excludes,
        review_guard_include_ignored=frame.review_guard_include_ignored,
        paths=frame.paths,
        state_dir_arg=frame.args.state_dir,
        disarm_exception_guard=frame.disarm_exception_guard,
        external_change_aliases=frame.external_change_aliases,
    )
    return None
