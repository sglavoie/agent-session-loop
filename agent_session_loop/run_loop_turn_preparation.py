"""Loop pass: build the turn prompt and stage the launch."""

from __future__ import annotations

import functools
from pathlib import Path

from . import (
    events,
    guards,
    guidance_delivery,
    harnesses,
    plans_content_hash,
    redirect_continuation,
    redirect_store,
    run_commit,
    run_selection,
    state,
    turn_execution,
    turn_preflight,
    turn_preparation,
)
from .run_loop_carried_corrections import (
    _carried_corrections,
    _launch_carried_corrections,
    _release_carried_corrections,
    _TurnDelivery,
)
from .run_loop_frame import LoopSignal, RunFrame, RunResult


def _prepare_turn_prompt(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Build the prompt, continuation and active-turn record for the turn."""
    frame.turn_reply = frame.pending_reply
    frame.turn_resolution = frame.pending_resolution
    frame.use_reply_prompt = frame.turn_reply is not None
    frame.use_resolution_prompt = frame.turn_resolution is not None
    # Immediately before the prompt is built, because the boundary the
    # operator experiences is "did it make it into the prompt".
    frame.turn_corrections = _carried_corrections(
        frame.ctx,
        plan_dir=frame.before.plan_dir,
        state_dir_arg=frame.args.state_dir,
        pending=frame.pending_redirect,
        session_id=frame.session_id,
        turn_index=frame.turn_index,
        harness=frame.turn_harness,
        recovery=frame.ctx.redirect_recovery_report,
    )
    turn_prompt = turn_preparation.prepare_turn_prompt(
        frame.ctx,
        snapshot=frame.before,
        classification=frame.classification,
        inputs=frame.packet_inputs,
        args=frame.args,
        cwd=frame.cwd,
        harness=frame.turn_harness,
        session_id=frame.session_id,
        session_label=frame.session_label,
        session_details=frame.session_details,
        session_scope=frame.session_scope,
        workspace=frame.workspace,
        mailbox_enabled=frame.mailbox_enabled,
        reply=frame.turn_reply,
        resolution=frame.turn_resolution,
        corrections=frame.turn_corrections,
    )
    if turn_prompt.paused:
        _release_carried_corrections(
            frame.ctx,
            plan_dir=frame.before.plan_dir,
            state_dir_arg=frame.args.state_dir,
            carried=frame.turn_corrections,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            harness=frame.turn_harness,
        )
        return LoopSignal.BREAK
    frame.continuation = turn_preparation.prepare_turn_continuation(
        frame.ctx,
        snapshot=frame.before,
        args=frame.args,
        cwd=frame.cwd,
        harness=frame.turn_harness,
        session_id=frame.session_id,
        session_details=frame.session_details,
        session_scope=frame.session_scope,
        workspace=frame.workspace,
        authorization=frame.fresh_conversation,
        # The conversation the correction was written against is the
        # one this attempt continues, and the resumed worker is told
        # which operations were cut short by identifier. The words stay
        # in the redirect journal: this is the handle, not the feedback.
        interrupted_operations=(
            frame.pending_redirect.resumable_operation_ids()
            if frame.pending_redirect is not None
            and frame.pending_redirect.session_id == frame.session_id
            else ()
        ),
    )
    if frame.continuation.paused:
        _release_carried_corrections(
            frame.ctx,
            plan_dir=frame.before.plan_dir,
            state_dir_arg=frame.args.state_dir,
            carried=frame.turn_corrections,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            harness=frame.turn_harness,
        )
        return LoopSignal.BREAK
    frame.conversation_capture = frame.continuation.capture(
        state.plan_state_dir(frame.before.plan_dir, frame.args.state_dir)
    )
    frame.prompt = turn_prompt.prompt
    frame.reply_source_meta = turn_prompt.reply_source
    frame.resolution_source_meta = turn_prompt.resolution_source
    turn_record = turn_preflight.build_active_turn(
        frame.ctx,
        snapshot=frame.before,
        classification=frame.classification,
        prompt=turn_prompt,
        session_identity=frame.session_identity,
        turn_index=frame.turn_index,
        paths=frame.paths,
        harness=frame.turn_harness,
        model_effort=frame.turn_model_effort,
        spark_route=frame.spark_route,
        cwd=frame.cwd,
    )
    frame.active_turn = turn_record.payload
    frame.total_sessions = turn_record.total_sessions
    log_stream_callback = turn_execution.prepare_stream_logs(frame.paths)
    frame.mailbox_poll.set_context(frame.active_turn)
    # Handed to the turn before it starts even though nothing has
    # requested an abort yet: the wait loop can only be cancellable if
    # the signal object already exists when the harness is launched.
    frame.abort_signal = harnesses.AbortSignal()
    frame.mailbox_poll.set_abort(frame.abort_signal)
    # The turn's redirect gate, bound to the same identity a producer
    # reads out of `active_turn` when it submits a correction, and
    # sharing the abort signal above rather than owning a second one:
    # the cooperative cancellation a redirect initiates and the one an
    # operator abort initiates are the same request to the same turn,
    # and the signal is also the only proof the escalation has that the
    # worker was reaped. It watches its own event, not the control
    # wakeup, so a correction never queues behind the control poll.
    frame.turn_redirect = redirect_continuation.TurnRedirect(
        plan_dir=frame.before.plan_dir,
        target=redirect_store.RedirectTarget(
            run_id=frame.ctx.run_id,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            harness=frame.turn_harness,
        ),
        state_dir=frame.args.state_dir,
        wakeup=frame.redirect_wakeup,
        cancellation=frame.abort_signal,
    )
    # The other turn-scoped control input beside the abort signal, and
    # bound to the same identity the delivery ledger records. It stays
    # inert until the harness opens it around the live process.
    frame.delivery_endpoint = guidance_delivery.LiveDeliveryEndpoint(
        guidance_delivery.TurnIdentity(
            run_id=frame.ctx.run_id,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            harness=frame.turn_harness,
        )
    )
    frame.mailbox_poll.set_live_delivery(frame.delivery_endpoint)

    frame.live_turn = run_commit._LiveTurn(
        payload=frame.ctx.payload,
        event_path=frame.ctx.event_path,
        progress=frame.ctx.progress,
        active_turn=frame.active_turn,
        persistence=frame.ctx.persistence,
        input_wait_probe=functools.partial(
            turn_execution.worker_input_wait_probe,
            plan_dir=frame.before.plan_dir,
            run_id=frame.ctx.run_id,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            state_dir=frame.args.state_dir,
        ),
        poll_callback=frame.mailbox_poll,
        poll_interval=turn_execution.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
        wakeup=frame.wakeup,
    )
    # One executor per turn: the identity and the artifact paths are
    # what make this turn this turn, so they are bound once here
    # rather than repeated at each of the three transitions below.
    frame.turn_executor = turn_execution.TurnExecutor(
        identity=turn_execution.TurnIdentity(
            run_id=frame.ctx.run_id,
            turn_index=frame.turn_index,
            session_id=frame.session_id,
        ),
        paths=frame.paths,
        event_path=frame.ctx.event_path,
        persistence=frame.ctx.persistence,
        progress=frame.ctx.progress,
        plan_cache=frame.ctx.plan_cache,
    )

    frame.stream_callback = turn_execution.live_stream_callback(
        log_stream_callback, frame.live_turn
    )

    with frame.run_control_lock:
        frame.ctx.run_control_context = dict(frame.active_turn)
    baselines = turn_preflight.capture_turn_baselines(
        frame.ctx,
        snapshot=frame.before,
        state_dir_arg=frame.args.state_dir,
        cwd=frame.cwd,
        classification=frame.classification,
        workspace=frame.workspace,
        session_scope=frame.session_scope,
        review_base=frame.prepared_review_workspace_snapshot,
        retained_scope_repair=frame.retained_scope_repair,
        regenerated_write_scope=frame.regenerated_write_scope,
        session_id=frame.session_id,
        session_label=frame.session_label,
        session_details=frame.session_details,
        review_guard_excludes=frame.review_guard_excludes,
        review_guard_include_ignored=frame.review_guard_include_ignored,
        active_turn=frame.active_turn,
        live_turn=frame.live_turn,
        paths=frame.paths,
        authorized_external_roots=tuple(
            Path(value)
            for value in frame.ctx.payload["run_config"].get("external_write_root", ())
        ),
    )
    if baselines.paused:
        _release_carried_corrections(
            frame.ctx,
            plan_dir=frame.before.plan_dir,
            state_dir_arg=frame.args.state_dir,
            carried=frame.turn_corrections,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            harness=frame.turn_harness,
        )
        return LoopSignal.BREAK
    frame.workspace_snapshot_before = baselines.workspace_snapshot
    frame.legacy_snapshot_before = baselines.legacy_snapshot
    frame.review_fingerprint_before = baselines.review_fingerprint
    frame.external_digests_before = baselines.external_digests
    frame.turn_started_ns = baselines.turn_started_ns
    frame.pending_exception_guard = baselines.pending_exception_guard
    frame.protected_path_baseline = baselines.protected_path_baseline
    frame.plan_artifacts_before = plans_content_hash.session_artifact_digests(
        frame.before.plan_dir, exclude_session_id=frame.session_id
    )
    return None


def _stage_turn_launch(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Capture baselines and stage redirects, replies and guidance before launch."""
    if frame.pending_redirect is not None:
        # Consumed at the last point before the turn starts, and only
        # against the session it was accepted for. Selection can move on
        # while a correction is in flight -- a repair child, the
        # generated review -- and a correction spent on a session the
        # operator was not talking about is worse than one dropped, so
        # the mismatch is recorded and the slot cleared either way.
        frame.consumed_redirect = frame.pending_redirect
        frame.pending_redirect = None
        frame.ctx.persistence.append_event(
            frame.ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": frame.ctx.run_id,
                "turn_index": frame.turn_index,
                **frame.session_identity,
                "outcome": (
                    "resumed"
                    if frame.consumed_redirect.session_id == frame.session_id
                    else "dropped"
                ),
                "redirect": frame.consumed_redirect.to_payload(),
                "harness": frame.turn_harness,
                "conversation": frame.continuation.resume_conversation_id,
                "interrupted_operations": list(
                    frame.consumed_redirect.resumable_operation_ids()
                ),
            },
        )
        if frame.consumed_redirect.session_id == frame.session_id:
            with frame.ctx.persistence.lock:
                frame.active_turn["redirect_resumed"] = (
                    frame.consumed_redirect.to_payload()
                )

    if frame.use_reply_prompt:
        assert frame.reply_source_meta is not None and frame.turn_reply is not None
        # Consumption is explicit rather than positional: a mailbox
        # continuation can begin on any later turn index, and no reply
        # may leak into a subsequent session prompt.
        frame.pending_reply = None
        if frame.turn_reply.process_start:
            frame.ctx.reply_state_preserver.mark_consumed()
        frame.reply_event_payload = {
            "run_id": frame.ctx.run_id,
            "turn_index": frame.turn_index,
            "session_id": frame.session_id,
            "reply_length": len(frame.turn_reply.text),
            "source": frame.reply_source_meta,
        }
        if frame.turn_reply.question_id is not None:
            frame.reply_event_payload.update(
                {
                    "origin": frame.turn_reply.origin,
                    "question_id": frame.turn_reply.question_id,
                }
            )
        frame.ctx.persistence.append_event(
            frame.ctx.event_path,
            events.REPLY_PROVIDED_EVENT,
            frame.reply_event_payload,
        )
    elif frame.use_resolution_prompt:
        assert (
            frame.resolution_source_meta is not None
            and frame.turn_resolution is not None
        )
        # Clear the pending object before recording turn start. A
        # successful start therefore consumes this exact resolution
        # once, even when the same session needs another turn later.
        frame.pending_resolution = None
        frame.ctx.persistence.append_event(
            frame.ctx.event_path,
            events.RESOLUTION_CONSUMED_EVENT,
            {
                "run_id": frame.ctx.run_id,
                "turn_index": frame.turn_index,
                "session_id": frame.session_id,
                "question_id": frame.turn_resolution.question_id,
                "resolution_message_id": (frame.turn_resolution.resolution_message_id),
                "resolution_seq": frame.turn_resolution.resolution_seq,
                "action": frame.turn_resolution.action,
                "note_length": frame.resolution_source_meta["note_length"],
                "source": frame.resolution_source_meta,
            },
        )

    # Materialized per turn, not once per run: the guard-bin directory
    # must exist before the child's PATH names it, and a guard added
    # during the previous turn has to be armed for this one. The
    # workspace binding is refreshed here too, because it is what turns
    # the absolute path a tool call carries into the `alias:path` a
    # rule is written in, and a run can change harness mid-flight.
    with frame.run_control_lock:
        frame.ctx.guard_controller.set_workspace_aliases(
            {
                repository.alias: str(repository.root)
                for repository in frame.workspace.repositories
            }
            if frame.workspace is not None
            else {guards.LEGACY_PATH_QUALIFIER: str(frame.cwd)}
        )
        frame.ctx.guard_controller.mark_turn_snapshot(frame.protected_path_baseline)
        # The worker may read an externally changed alias it cannot
        # write, but its own edit tools are refused there: the
        # postflight comparison no longer convicts that alias, so this
        # lane is what still tells the worker it is out of scope.
        frame.ctx.guard_controller.set_external_read_only_aliases(
            frame.external_change_aliases
            - frozenset(
                frame.session_scope.writes if frame.session_scope is not None else ()
            )
        )
        frame.turn_env = frame.ctx.guard_controller.prepare_turn(
            harness=frame.turn_harness,
            pretool_armed=harnesses.get_adapter(
                frame.turn_harness
            ).enforces_guard_pretool,
        )

    frame.record_guidance_delivery = (
        functools.partial(
            run_selection._record_next_turn_guidance_delivery,
            plan_dir=frame.before.plan_dir,
            state_dir_arg=frame.args.state_dir,
            event_path=frame.ctx.event_path,
            append_event=frame.ctx.persistence.append_event,
            pending_guidance=frame.packet_inputs.pending_guidance,
            harness=frame.turn_harness,
            run_id=frame.ctx.run_id,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
        )
        if (
            frame.packet_inputs.pending_guidance
            and harnesses.delivers_guidance_next_turn(frame.turn_harness)
        )
        else None
    )

    # The launch side of the coalescing boundary, and this attempt's
    # alone: it settles the carry as delivered when the harness reports
    # the prompt submitted, and hands it back below if the turn ended
    # without ever getting that far.
    frame.turn_delivery = _TurnDelivery(
        frame.ctx,
        plan_dir=frame.before.plan_dir,
        state_dir_arg=frame.args.state_dir,
        carried=frame.turn_corrections,
        session_id=frame.session_id,
        turn_index=frame.turn_index,
        harness=frame.turn_harness,
    )

    # The last durable statement made while it is still true that the
    # words have not gone out. After this line the journal can no longer
    # prove this attempt did not submit, which is the entire reason a
    # crash from here on reads as a question instead of an invitation to
    # deliver the same correction again.
    if not _launch_carried_corrections(
        frame.ctx,
        plan_dir=frame.before.plan_dir,
        state_dir_arg=frame.args.state_dir,
        carried=frame.turn_corrections,
        session_id=frame.session_id,
        turn_index=frame.turn_index,
        harness=frame.turn_harness,
    ):
        # Undeclarable, so unsendable. The carry goes back by the same
        # route every other prelaunch refusal uses -- a best effort,
        # since the journal that just refused one line may refuse this
        # one too, and a carry left standing is reported as uncertain
        # rather than handed out again.
        _release_carried_corrections(
            frame.ctx,
            plan_dir=frame.before.plan_dir,
            state_dir_arg=frame.args.state_dir,
            carried=frame.turn_corrections,
            session_id=frame.session_id,
            turn_index=frame.turn_index,
            harness=frame.turn_harness,
        )
        frame.ctx.record_final_snapshot(frame.before)
        frame.ctx.pause(
            reason=events.PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE,
            message=(
                "the redirect journal would not record that this turn's "
                "prompt was about to be submitted, so the turn was not "
                "started"
            ),
            details={
                "session_id": frame.session_id,
                "turn_index": frame.turn_index,
                "harness": frame.turn_harness,
                "operation_ids": list(frame.turn_corrections.operation_ids())
                if frame.turn_corrections is not None
                else [],
            },
        )
        return LoopSignal.BREAK
    return None
