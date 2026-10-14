"""Loop pass: pick the next pending session and the harness that runs it."""

from __future__ import annotations

import functools
from collections.abc import Mapping

from . import (
    clock,
    model_availability,
    plans_payloads,
    review_guard,
    review_repair,
    run_completion,
    run_coordination_control_drain,
    run_prompts,
    run_selection,
    run_termination,
    spark_status,
    state,
    turn_execution_artifacts,
    turn_preflight,
    turn_preparation,
)
from .run_errors import RunError
from .run_loop_commit_settlement import (
    _commit_preflight_refusal,
    _pause_for_unsettled_commit_group,
    _settle_owed_commit_group,
    _soft_fail_commit,
    _spend_owed_commit_group_budget,
)
from .run_loop_control_redirects import (
    _pause_for_redirect_restart,
    _redirected_turn_harness,
)
from .run_loop_frame import LoopSignal, RunFrame, RunResult


def _select_pending_session(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Pause or finish the run, or pick the next pending session."""
    if frame.ctx.idle_baseline_pinned:
        # A path that reached here after deliberately establishing its
        # own baseline opts out by pinning it. Refreshing would
        # overwrite that earlier instant with this one and charge the
        # gap it was measuring to the runner.
        frame.ctx.idle_baseline_pinned = False
    elif not frame.ctx.first_loop_pass:
        # Every path back to the top got here by finishing
        # runner-owned work — a turn and its verification and
        # finalization, or the all-complete branch's final commit,
        # repair materialization, re-gating, readiness writes and
        # review creation. Refreshing the baseline in this one place
        # covers every path, including ones added later.
        frame.ctx.runner_work_finished_at = clock.utc_now()
    if _pause_for_redirect_restart(frame.ctx, args=frame.args, snapshot=frame.snapshot):
        return LoopSignal.BREAK
    entering_first_pass = frame.ctx.first_loop_pass
    frame.ctx.first_loop_pass = False
    if not entering_first_pass:
        # One place covers both boundaries a still-owed group must be
        # settled at: ahead of the next worker, and ahead of the
        # end-of-run commit the all-complete branch below runs. The
        # first pass is excluded because the recovery above the loop
        # is that pass's boundary and has just run.
        _settle_owed_commit_group(frame.ctx)
    if _pause_for_unsettled_commit_group(frame.ctx, snapshot=frame.snapshot):
        return LoopSignal.BREAK
    frame.pending_session = frame.snapshot.next_pending

    if frame.pending_session is None:
        # No session is selectable, which is not yet an ending: the
        # plan may still owe a commit, a review, or the repair
        # children a review reopens. The completion boundary decides
        # which, grows the plan when it must, and hands back the
        # image it ended on.
        frame.completion = run_completion.complete_plan(
            frame.ctx,
            snapshot=frame.snapshot,
            args=frame.args,
            cwd=frame.cwd,
            commit_mode=frame.commit_mode,
            review_requested=frame.review_requested,
            max_review_fix_cycles=frame.max_review_fix_cycles,
            allow_not_loop_ready=frame.allow_not_loop_ready,
            review_guard_excludes=frame.review_guard_excludes,
            review_guard_include_ignored=frame.review_guard_include_ignored,
            run_controls=frame.ctx.run_controls,
            run_control_lock=frame.run_control_lock,
            wakeup=frame.wakeup,
            soft_fail_commit=functools.partial(_soft_fail_commit, frame.ctx),
        )
        frame.snapshot = frame.completion.snapshot
        if frame.completion.action == run_completion.ACTION_CONTINUE:
            return LoopSignal.CONTINUE
        return LoopSignal.BREAK

    frame.classification = turn_preparation.classify_session(frame.pending_session.path)
    frame.is_review_session = frame.classification.is_review_session
    frame.is_repair_session = frame.classification.is_repair_session
    review_preparation = turn_preparation.prepare_review_session(
        frame.ctx,
        session=frame.pending_session,
        snapshot=frame.snapshot,
        classification=frame.classification,
        cwd=frame.cwd,
        review_guard_excludes=frame.review_guard_excludes,
        review_guard_include_ignored=frame.review_guard_include_ignored,
    )
    frame.snapshot = review_preparation.snapshot
    if review_preparation.paused:
        return LoopSignal.BREAK
    frame.prepared_review_workspace_snapshot = review_preparation.workspace_snapshot
    frame.review_session_version = review_preparation.session_version

    if (
        not frame.is_review_session
        and frame.max_sessions is not None
        and frame.ctx.completed_count >= frame.max_sessions
    ):
        with frame.ctx.persistence.lock:
            frame.ctx.payload["reason"] = "max-sessions"
        return LoopSignal.BREAK
    return None


def _select_turn_harness(frame: RunFrame) -> RunResult | LoopSignal | None:
    """Identify the session, drain controls and choose the harness, model and scope."""
    frame.before = frame.snapshot
    frame.session_id = frame.pending_session.id
    frame.session_label = plans_payloads.session_label(frame.session_id)
    frame.session_identity = plans_payloads.session_identity_payload(frame.session_id)
    frame.session_details = plans_payloads.session_details_payload(frame.session_id)
    frame.ctx.last_session_details = frame.session_details

    if frame.is_review_session:
        with frame.ctx.persistence.lock:
            if frame.ctx.payload.get("review_session_id") != frame.session_id:
                frame.ctx.payload["review_session_id"] = frame.session_id

    with frame.ctx.persistence.lock:
        frame.turn_index = len(frame.ctx.payload["turns"]) + 1
        frame.turns_already_run = bool(frame.ctx.payload["turns"])
    frame.paths = turn_execution_artifacts.turn_paths(
        frame.ctx.run_dir,
        turn_index=frame.turn_index,
        session_id=frame.session_id,
    )
    implementation_overrides: Mapping[str, Mapping[str, str]] = {}
    pre_turn_control_context = {
        **frame.session_identity,
        "turn_index": frame.turn_index,
        "total_sessions": sum(plans_payloads.status_counts(frame.before).values()),
    }
    run_coordination_control_drain.drain_external_control_queue(
        plan_dir=frame.before.plan_dir,
        state_dir_arg=frame.args.state_dir,
        control_state=frame.ctx.run_controls.state,
        seen_ids=frame.ctx.seen_external_command_ids,
        payload=frame.ctx.payload,
        event_path=frame.ctx.event_path,
        control_payload=frame.ctx.control_payload,
        context=pre_turn_control_context,
        persistence=frame.ctx.persistence,
        run_control_lock=frame.run_control_lock,
        wakeup=frame.wakeup,
        guard_controller=frame.ctx.guard_controller,
    )
    with frame.run_control_lock:
        # A stop observed here must never let another turn start. Only
        # a stop that lands after a turn already ran in this process
        # qualifies: one queued before the run began still means "run
        # the first session, then stop". A pending reply or resolution
        # continuation belongs to the session already in flight, so it
        # runs before the stop is honored.
        frame.stop_before_turn = (
            frame.turns_already_run
            and frame.pending_reply is None
            and frame.pending_resolution is None
            and frame.ctx.run_controls.state.stop_after_current
        )
        if frame.stop_before_turn:
            run_termination._honor_stop_after_current(
                frame.ctx.run_controls,
                snapshot=frame.before,
                payload=frame.ctx.payload,
                event_path=frame.ctx.event_path,
                control_payload=frame.ctx.control_payload,
                context=pre_turn_control_context,
                persistence=frame.ctx.persistence,
            )
    if frame.stop_before_turn:
        return LoopSignal.BREAK
    if frame.is_review_session:
        frame.turn_harness = frame.args.review_harness
    else:
        with frame.run_control_lock:
            frame.turn_harness = run_selection._selected_future_harness(
                frame.args, frame.ctx.control_payload
            )
            implementation_overrides = (
                frame.ctx.run_controls.state.implementation_overrides
            )
    if frame.turn_harness is None:
        raise RunError("--review-harness is required for a review session")
    frame.turn_harness = _redirected_turn_harness(
        frame.ctx,
        pending=frame.pending_redirect,
        selected=frame.turn_harness,
        session_id=frame.session_id,
        turn_index=frame.turn_index,
    )
    spark_eligible = (
        getattr(frame.args, "spark", False)
        and frame.spark_run_suppression is None
        and frame.spark_fallback_reason is None
        and frame.turn_harness == "codex"
        and not frame.is_review_session
        and not implementation_overrides.get(frame.turn_harness)
        and not review_repair.is_generated_review_repair_session_file(
            frame.pending_session.path
        )
        and run_selection._session_spark_suitability(
            frame.before.plan_dir, frame.session_id
        )[0]
    )
    if spark_eligible:
        try:
            frame.refreshed_availability = model_availability.reconcile_live(
                state.state_root(frame.args.state_dir),
                run_selection.SPARK_MODEL,
                spark_status.read_status(),
                run_id=frame.ctx.run_id,
            )
        except OSError as error:
            frame.refreshed_availability = model_availability.Availability(
                False,
                "ledger-unreadable",
                warning=f"model availability ledger is unreadable: {error}",
            )
        with frame.ctx.persistence.lock:
            frame.ctx.payload["spark"].update(frame.refreshed_availability.to_dict())
    # The immediate retry after a Spark failure stays on the baseline.
    # Later sessions may use a fresh live read, including an early reset.
    frame.requested_model, frame.turn_model_effort, frame.spark_route = (
        run_selection._spark_turn_route(
            frame.args,
            plan_dir=frame.before.plan_dir,
            session_path=frame.pending_session.path,
            session_id=frame.session_id,
            harness=frame.turn_harness,
            is_review_turn=frame.is_review_session,
            implementation_overrides=implementation_overrides,
            spark_state=frame.ctx.payload["spark"],
            run_suppression=(
                frame.spark_run_suppression
                or (
                    f"{frame.spark_fallback_reason}-immediate-fallback"
                    if frame.spark_fallback_reason is not None
                    else None
                )
            ),
            fallback_reason=frame.spark_fallback_reason,
        )
    )
    frame.spark_fallback_reason = None
    with frame.ctx.persistence.lock:
        frame.ctx.payload["spark"]["active_route"] = frame.spark_route
    frame.requested_effort = run_selection._requested_effort(
        frame.args,
        harness=frame.turn_harness,
        is_review_turn=frame.is_review_session,
        implementation_overrides=implementation_overrides,
    )
    preflight_failure = run_selection._harness_preflight_failure(
        frame.turn_harness,
        session_id=frame.session_id,
    )
    if preflight_failure is not None:
        frame.message, frame.details = preflight_failure
        frame.ctx.record_final_snapshot(frame.before)
        frame.ctx.pause(
            reason="harness-unavailable",
            message=(
                f"harness unavailable before {frame.session_label}: {frame.message}"
            ),
            details=frame.details,
        )
        return LoopSignal.BREAK
    frame.packet_inputs = turn_preparation.collect_packet_inputs(
        frame.ctx,
        snapshot=frame.before,
        classification=frame.classification,
        args=frame.args,
        session_id=frame.session_id,
        mailbox_enabled=frame.mailbox_enabled,
    )
    frame.workspace = frame.ctx.verification_context.workspace
    frame.session_scope = (
        run_prompts._session_scope(frame.pending_session, frame.workspace)
        if frame.workspace is not None
        else None
    )
    frame.retained_scope_repair = turn_preflight.resolve_retained_scope_repair(
        event_path=frame.ctx.event_path,
        session_id=frame.session_id,
        workspace=frame.workspace,
    )
    repair_resumption = turn_preparation.resume_repair_session(
        frame.ctx,
        session=frame.pending_session,
        snapshot=frame.before,
        classification=frame.classification,
        args=frame.args,
        cwd=frame.cwd,
        workspace=frame.workspace,
        session_scope=frame.session_scope,
        session_label=frame.session_label,
        session_identity=frame.session_identity,
        session_details=frame.session_details,
        retained_scope_repair=frame.retained_scope_repair,
        max_review_fix_cycles=frame.max_review_fix_cycles,
        review_requested=frame.review_requested,
    )
    frame.session_scope = repair_resumption.session_scope
    frame.regenerated_write_scope = repair_resumption.regenerated_write_scope
    if repair_resumption.paused:
        return LoopSignal.BREAK
    if (
        frame.commit_mode == run_selection.COMMIT_MODE_PER_SESSION
        and frame.workspace is not None
    ):
        frame.refusal = _commit_preflight_refusal(
            frame.ctx, cwd=frame.cwd, session_scope=frame.session_scope
        )
        if frame.refusal is not None and frame.ctx.owed_commit_group is not None:
            # The dirt this gate is refusing may be the run's own: a
            # commit group that failed part-way leaves the repositories
            # it never reached exactly as unexplained as an operator's
            # stray edit, and this gate cannot tell the two apart. So
            # the owed group spends its budget first. If it settles, the
            # dirt is gone and the gate has nothing left to say; if it
            # exhausts its retries, the honest reason is
            # ``commit-failed`` -- a commit this run attempted and could
            # not make -- and not ``commit-preflight``, which would
            # report a repairable commit failure as a worktree for a
            # human to clean up, one process earlier than the run had to
            # stop.
            _spend_owed_commit_group_budget(frame.ctx)
            if _pause_for_unsettled_commit_group(frame.ctx, snapshot=frame.before):
                return LoopSignal.BREAK
            frame.refusal = _commit_preflight_refusal(
                frame.ctx, cwd=frame.cwd, session_scope=frame.session_scope
            )
        if frame.refusal is not None:
            frame.ctx.record_final_snapshot(frame.before)
            frame.ctx.pause(
                reason="commit-preflight",
                message=str(frame.refusal),
                details={
                    **frame.session_details,
                    **review_guard.commit_preflight_pause_details(
                        frame.ctx.verification_context
                    ),
                    "commit_preflight_phase": "continuation",
                },
            )
            return LoopSignal.BREAK
    return None
