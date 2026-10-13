"""The crash handler and the settlement that ends every run."""

from __future__ import annotations

from . import (
    clock,
    handoff_correction,
    handoff_guards,
    harnesses,
    plans,
    review_cycle,
    review_transitions,
    run_inputs,
    session_verification,
    turn_execution,
    write_guards,
)
from .run_loop_commit_settlement import _settle_paused_retained_work
from .run_loop_frame import RunFrame, RunResult
from .run_loop_terminal import _expire_run_guards, _terminalize


def _handle_run_crash(frame: RunFrame, error: BaseException) -> RunResult | None:
    """Settle or re-raise a crash or interrupt that escaped the run loop."""
    guard_context = frame.pending_exception_guard
    frame.pending_exception_guard = None
    if guard_context is not None:
        frame.live_turn = guard_context["live_turn"]
        frame.live_turn.stop()
        frame.finished_at = clock.utc_now()
        frame.live_turn_payload = frame.live_turn.finish(frame.finished_at)
        with frame.run_control_lock:
            frame.ctx.run_control_context = None

        frame.after = guard_context.get("after")
        if frame.after is None:
            try:
                frame.after = frame.ctx.plan_cache.get_fresh(frame.snapshot.plan_dir)
            except Exception:
                frame.after = None
        frame.turn_payload = guard_context.get("turn_payload")
        if not isinstance(frame.turn_payload, dict):
            frame.turn_payload = {
                **frame.live_turn_payload,
                "run_id": frame.ctx.run_id,
                "turn_index": frame.live_turn_payload.get("turn_index"),
                **plans.session_identity_payload(str(guard_context["session_id"])),
                "status_result": "exception",
                "exception": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
                "before": plans.snapshot_payload(guard_context["before"]),
                "after": (
                    plans.snapshot_payload(frame.after)
                    if frame.after is not None
                    else None
                ),
            }
        frame.guard_mode = guard_context.get("mode")
        # ``review`` matches neither branch on purpose: a legacy review turn
        # declares no write scope and captured no write-guard snapshot, so
        # there is no write guard to enforce for it. Its comparison is the
        # review fingerprint, which the protected-path step below reads.
        try:
            if frame.guard_mode == "legacy":
                write_guards._enforce_legacy_implementation_write_guard(
                    event_path=frame.ctx.event_path,
                    payload=frame.ctx.payload,
                    persistence=frame.ctx.persistence,
                    cwd=guard_context["cwd"],
                    before=guard_context["before"],
                    after=frame.after,
                    snapshot_before=guard_context["snapshot_before"],
                    exclude=frame.review_guard_excludes,
                    include_ignored=frame.review_guard_include_ignored,
                    session_id=str(guard_context["session_id"]),
                    session_label=str(guard_context["session_label"]),
                    session_details=guard_context["session_details"],
                    turn_payload=frame.turn_payload,
                    paths=guard_context["paths"],
                    plan_cache=frame.ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                    state_dir_arg=frame.args.state_dir,
                    run_dir=frame.ctx.run_dir,
                    external_digests_before=guard_context.get(
                        "external_digests_before"
                    ),
                    turn_started_ns=guard_context.get("turn_started_ns"),
                )
            elif frame.guard_mode == "workspace":
                write_guards._enforce_workspace_write_guard(
                    external_change_aliases=frame.external_change_aliases,
                    event_path=frame.ctx.event_path,
                    payload=frame.ctx.payload,
                    persistence=frame.ctx.persistence,
                    workspace=guard_context["workspace"],
                    scope=guard_context["scope"],
                    before=guard_context["before"],
                    after=frame.after,
                    snapshot_before=guard_context["snapshot_before"],
                    exclude=frame.review_guard_excludes,
                    include_ignored=frame.review_guard_include_ignored,
                    session_id=str(guard_context["session_id"]),
                    session_label=str(guard_context["session_label"]),
                    session_details=guard_context["session_details"],
                    turn_payload=frame.turn_payload,
                    paths=guard_context["paths"],
                    plan_cache=frame.ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                    state_dir_arg=frame.args.state_dir,
                    run_dir=frame.ctx.run_dir,
                    external_digests_before=guard_context.get(
                        "external_digests_before"
                    ),
                    turn_started_ns=guard_context.get("turn_started_ns"),
                )
        except Exception as guard_error:
            error.add_note(
                "post-turn workspace write guard failed while handling "
                f"the exception: {guard_error}"
            )
        # The run's guards are expired a few lines below, so this is the
        # last moment an operator's standing instruction can be answered
        # at all. It is answered in adjudicate-only mode because the
        # exception, not the guard, is what ended this turn: the verdict
        # is recorded and the failure it belongs to is re-raised intact.
        try:
            write_guards._enforce_protected_path_guard(
                event_path=frame.ctx.event_path,
                payload=frame.ctx.payload,
                persistence=frame.ctx.persistence,
                guard_controller=frame.ctx.guard_controller,
                before=guard_context["before"],
                after=frame.after,
                session_id=str(guard_context["session_id"]),
                session_label=str(guard_context["session_label"]),
                session_details=guard_context["session_details"],
                turn_payload=frame.turn_payload,
                paths=guard_context["paths"],
                plan_cache=frame.ctx.plan_cache,
                append_handoff_note=handoff_correction._append_handoff_note,
                reinspect=session_verification._reinspect_after_revert,
                # A write guard that never recorded its comparison — it
                # raised, or the inventory failed — leaves this guard
                # blind, and blind is recorded as unavailable rather than
                # read as clean. A legacy review turn has no write-guard
                # comparison by construction, so it is answered from its
                # own turn-start fingerprint instead.
                changed_paths=(
                    write_guards._protected_path_review_changed_paths(
                        guard_context,
                        exclude=frame.review_guard_excludes,
                        include_ignored=frame.review_guard_include_ignored,
                    )
                    if frame.guard_mode == "review"
                    else write_guards._protected_path_changed_paths(
                        frame.turn_payload.get("workspace_write_guard")
                    )
                ),
                adjudicate_only=True,
            )
        except Exception as guard_error:
            error.add_note(
                "post-turn protected-path guard failed while handling "
                f"the exception: {guard_error}"
            )
    if isinstance(error, KeyboardInterrupt):
        if not frame.ctx.payload.get("paused"):
            frame.ctx.pause(
                reason="interrupted",
                message="run interrupted by user",
                details=dict(frame.ctx.last_session_details),
            )
    else:
        _expire_run_guards(frame.ctx)
        frame.ctx.run_controls.stop()
        frame.ctx.inline_tail.stop()
        raise
    return None


def _finalize_run(frame: RunFrame) -> RunResult | None:
    """Settle retained work, publish the review state and terminalize the run."""
    # The one call site, at the point every one of the loop's breaks converges
    # on. Placing it here rather than at each pause is what keeps the ordering
    # the settlement depends on -- the revert, the plan re-read, `turn_rejected`
    # and `run_paused` have all happened by now -- and it is still ahead of
    # `_terminalize`, which snapshots the unresolved commit groups a soft-failed
    # settlement would have to appear in.
    _settle_paused_retained_work(frame.ctx, commit_mode=frame.commit_mode)

    if frame.review_requested:
        # An ordinary repair child can change pending/completed membership
        # without traversing a review-cycle branch. Re-inspect here so every
        # loop exit (including max-sessions, stop-after-current, and pauses)
        # publishes the plan's final continuation state.
        try:
            frame.final_review_snapshot = frame.ctx.plan_cache.get(
                frame.snapshot.plan_dir
            )
        except plans.PlanError:
            # Preserve the run's existing malformed-plan pause and its last
            # safely computed projection when the final plan is unreadable.
            pass
        else:
            frame.review_resume = review_cycle._set_review_resume(
                snapshot=frame.final_review_snapshot,
                event_path=frame.ctx.event_path,
                payload=frame.ctx.payload,
                persistence=frame.ctx.persistence,
                max_repair_group_attempts=run_inputs._max_repair_group_attempts(
                    frame.args
                ),
                max_review_fix_cycles=frame.max_review_fix_cycles,
                review_requested=True,
            )

    with frame.ctx.persistence.lock:
        if frame.ctx.payload["ok"] and frame.ctx.payload["reason"] == "":
            frame.ctx.payload["reason"] = "stopped"
        frame.review_executed = (
            frame.ctx.payload.get("review_session_executed") is not None
        )
        if (
            frame.review_requested
            and frame.review_executed
            and not frame.ctx.payload.get("review_completed")
        ):
            review_cycle._attest_executed_review(
                frame.ctx.payload,
                frame.snapshot.plan_dir,
                require_artifact=frame.executed_review_requires_artifact,
                read_review_verdict=handoff_guards._read_review_verdict,
            )
        if (
            frame.review_requested
            and not frame.review_executed
            and not frame.ctx.payload.get("review_completed")
            and frame.ctx.payload.get("review_skipped_reason") is None
        ):
            if frame.ctx.payload.get("paused"):
                frame.ctx.payload["review_skipped_reason"] = (
                    frame.ctx.payload.get("pause_reason") or "paused"
                )
            elif frame.ctx.payload["reason"] not in (
                "",
                "all-complete",
                review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            ):
                frame.ctx.payload["review_skipped_reason"] = frame.ctx.payload["reason"]

    with frame.ctx.persistence.lock:
        frame.exit_code = 0 if frame.ctx.payload["ok"] else 1
        if frame.ctx.payload.get("pause_reason") == "interrupted":
            frame.exit_code = turn_execution.INTERRUPTED_EXIT
    # The controls outlive the loop so a supervisor can still reach a run that
    # is finalizing: the terminal audit is a harness turn of its own, and an
    # abort arriving during it has to find a live listener and a live drain.
    finalization_abort = harnesses.AbortSignal()
    frame.mailbox_poll.set_context(None)
    frame.mailbox_poll.set_abort(finalization_abort, finalization=True)
    try:
        return _terminalize(
            frame.ctx,
            frame.exit_code,
            cancel=finalization_abort,
            poll=frame.mailbox_poll,
        )
    finally:
        frame.mailbox_poll.set_abort(None)
        _expire_run_guards(frame.ctx)
        frame.ctx.run_controls.stop()
        frame.ctx.inline_tail.stop()
