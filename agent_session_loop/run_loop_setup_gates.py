"""Setup phase: review resume, plan validity and the readiness gate."""

from __future__ import annotations

from typing import Any

from . import (
    events,
    plans_payloads,
    plans_session_scope,
    preflight,
    readiness_gate,
    review_continuation,
    review_cycle,
    review_guard,
    review_session_body,
    run_inputs,
    run_payloads,
    run_selection,
    scope_recovery,
    turn_preflight,
    workspace_evidence,
)
from .run_errors import RunError
from .run_loop_commit_settlement import _settle_owed_commit_group_at_startup
from .run_loop_context import MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED
from .run_loop_frame import RunFrame, RunResult
from .run_loop_terminal import _current_session_scope_or_none, _terminalize


def _prepare_review_and_resume(frame: RunFrame) -> RunResult | None:
    """Pause on invalid plans and unresolved commit groups; settle review resume."""
    frame.invalid_pause = run_inputs._invalid_status_pause(frame.snapshot)
    if frame.invalid_pause is not None:
        frame.reason, frame.message, frame.details = frame.invalid_pause
        frame.ctx.record_final_snapshot(frame.snapshot)
        frame.ctx.pause(
            reason=frame.reason,
            message=frame.message,
            details=frame.details,
        )
        return _terminalize(frame.ctx, 1)

    if frame.commit_group_load_error is not None:
        frame.ctx.record_final_snapshot(frame.snapshot)
        frame.ctx.pause(
            reason="commit-failed",
            message="durable commit-group evidence could not be reconstructed",
            details={"error": frame.commit_group_load_error},
        )
        return _terminalize(frame.ctx, 1)

    if frame.unresolved_commit_group is not None and frame.args.dry_run:
        with frame.ctx.persistence.lock:
            frame.ctx.payload["commit_group_recovery"] = {
                "group_id": frame.unresolved_commit_group.get("group_id"),
                "success": False,
                "pending": True,
                "error": None,
                "group": frame.unresolved_commit_group,
            }
    elif frame.unresolved_commit_group is not None:
        # The restart half of the invariant: a group another run left owed is
        # completed here, before this run launches a worker of its own. A
        # second failure soft-fails like any other; a paused-work group spends
        # the retry that arms right here, because the startup commit gate below
        # would otherwise refuse the dirt it left before the loop boundary that
        # consumes it is ever reached.
        _settle_owed_commit_group_at_startup(
            frame.ctx, group=frame.unresolved_commit_group
        )

    frame.review_requested = getattr(frame.args, "review_harness", None) is not None
    frame.max_review_fix_cycles = run_inputs._max_review_fix_cycles(frame.args)
    frame.review_resume = None
    if frame.review_requested:
        frame.review_resume = review_cycle._set_review_resume(
            snapshot=frame.snapshot,
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            persistence=frame.ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(frame.args),
            max_review_fix_cycles=frame.max_review_fix_cycles,
            review_requested=True,
        )

    if frame.snapshot.next_pending is None:
        if not run_inputs._all_sessions_complete(frame.snapshot):
            frame.ctx.pause(
                reason="no-pending-but-incomplete",
                message="no pending session was found, but not every session is "
                "complete",
                details={"snapshot": plans_payloads.snapshot_payload(frame.snapshot)},
            )
            return _terminalize(frame.ctx, 1)
        if not frame.review_requested:
            with frame.ctx.persistence.lock:
                frame.ctx.payload["reason"] = "all-complete"
            return _terminalize(frame.ctx, 0)

    if not frame.review_requested:
        frame.pending_review = (
            review_session_body.find_pending_generated_review_session(
                frame.snapshot.plan_dir
            )
        )
        if frame.pending_review is not None:
            frame.ctx.record_final_snapshot(frame.snapshot)
            frame.ctx.pause(
                reason="review-harness-required",
                message="a pending generated review session is present; "
                "rerun with --review-harness to execute it",
                details={
                    "session_id": frame.pending_review.session_id.value,
                    "session_path": str(frame.pending_review.path),
                },
            )
            return _terminalize(frame.ctx, 1)

    if frame.args.dry_run:
        with frame.ctx.persistence.lock:
            frame.ctx.payload["reason"] = "dry-run"
            frame.ctx.payload["planned"] = run_payloads._dry_run_payload(
                args=frame.args,
                snapshot=frame.snapshot,
                cwd=frame.cwd,
                run_dir=frame.ctx.run_dir,
                mailbox_activation=frame.mailbox_activation,
                reply_text=frame.reply_text,
                reply_context=frame.reply_context,
                reply_origin=frame.reply_origin,
                verification_context=frame.ctx.verification_context,
            )
        return _terminalize(frame.ctx, 0)

    if (
        frame.ctx.verification_context.workspace is not None
        and not MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED
    ):
        frame.ctx.record_final_snapshot(frame.snapshot)
        frame.ctx.pause(
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            message="multi-repository harness turns are disabled until the "
            "authoritative workspace write guard is installed",
            details={
                "workspace_root": str(frame.ctx.verification_context.root),
                "registry_digest": frame.ctx.verification_context.registry_digest,
            },
        )
        return _terminalize(frame.ctx, 1)

    if (
        frame.review_resume is not None
        and frame.review_resume.action in ("run-repairs", "rereview")
        and (
            frame.review_resume.source_disposition is None
            or frame.review_resume.source_disposition
            in (
                *review_continuation.BUDGET_REVIEW_DISPOSITIONS,
                review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            )
        )
        and frame.review_resume.repair_session_ids
        and not review_cycle._has_successful_review_reopen(
            frame.ctx.event_path, frame.review_resume.review_session_id
        )
    ):
        # The atomic child write can win a crash race against event append.
        # Generated children are authoritative.  This also recovers an
        # eventless findings review whose cycle record was lost, preserving
        # its fingerprints before the next review overwrites the artifact.
        frame.recovered_record = review_cycle._review_reopen_record(
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            resume=frame.review_resume,
            max_review_fix_cycles=frame.max_review_fix_cycles,
            materialized=True,
            repair_session_ids=frame.review_resume.repair_session_ids,
            result="recovered-existing-children",
            recovered=True,
        )
        review_cycle._record_review_reopen(
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            persistence=frame.ctx.persistence,
            record=frame.recovered_record,
        )
        frame.review_resume = review_cycle._set_review_resume(
            snapshot=frame.snapshot,
            event_path=frame.ctx.event_path,
            payload=frame.ctx.payload,
            persistence=frame.ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(frame.args),
            max_review_fix_cycles=frame.max_review_fix_cycles,
            review_requested=True,
        )
    return None


def _pass_readiness_gate(frame: RunFrame) -> RunResult | None:
    """Run the readiness gate and apply its pause or refusal outcomes."""
    frame.allow_not_loop_ready = readiness_gate._allow_not_loop_ready(frame.args)
    max_pending_sessions = getattr(frame.args, "max_pending_sessions", None)
    preflight_kwargs: dict[str, Any] = {
        "cwd": frame.cwd,
        "harness": frame.args.harness,
        "commit_mode": frame.commit_mode,
        "state_dir": frame.args.state_dir,
        "max_pending_sessions": max_pending_sessions,
    }
    resume_report = readiness_gate._reduced_preflight_report(
        frame.snapshot.plan_dir,
        snapshot=frame.snapshot,
        lock_info=frame.lock_info,
        preflight_kwargs=preflight_kwargs,
    )
    frame.ctx.core_skipped = resume_report is not None
    if frame.ctx.core_skipped:
        frame.preflight_report = resume_report
        frame.ctx.persistence.append_event(
            frame.ctx.event_path,
            events.PREFLIGHT_SKIPPED_EVENT,
            {
                "readiness_state": frame.preflight_report.readiness.state,
                "readiness_sha256": frame.preflight_report.expected_readiness_sha256,
            },
        )
    else:
        frame.preflight_report = readiness_gate._strip_own_lock_findings(
            preflight.run_preflight(frame.snapshot.plan_dir, **preflight_kwargs),
            lock_info=frame.lock_info,
        )
    frame.ctx.gate_payload = readiness_gate._readiness_gate_payload(
        frame.preflight_report, cwd=frame.cwd
    )
    if frame.ctx.core_skipped:
        frame.ctx.gate_payload["core_skipped"] = True
    frame.ctx.gate_passed = frame.ctx.gate_payload["ok"]
    review_only_complete_start = readiness_gate._review_only_complete_start(
        frame.preflight_report,
        review_requested=frame.review_requested,
        snapshot=frame.snapshot,
    )
    with frame.ctx.persistence.lock:
        frame.ctx.payload["readiness_gate"] = frame.ctx.gate_payload
        if frame.preflight_report.readiness is not None:
            frame.ctx.payload["readiness_state_initial"] = (
                frame.preflight_report.readiness.state
            )

    initial_workspace_scope_repair = (
        workspace_evidence._latest_workspace_scope_repair_requirement(
            frame.ctx.event_path, frame.snapshot.next_pending.id
        )
        if frame.ctx.verification_context.workspace is not None
        and frame.snapshot.next_pending is not None
        else None
    )
    initial_scope_restored = (
        turn_preflight.retained_scope_restored(
            record=initial_workspace_scope_repair,
            workspace=frame.ctx.verification_context.workspace,
            plan_dir=frame.snapshot.plan_dir,
            scope=_current_session_scope_or_none(
                frame.ctx, frame.snapshot.next_pending
            ),
            exclude=frame.review_guard_excludes,
            include_ignored=frame.review_guard_include_ignored,
        )
        if initial_workspace_scope_repair is not None and not frame.ctx.gate_passed
        else False
    )
    if (
        initial_workspace_scope_repair is not None
        and not frame.ctx.gate_passed
        and not initial_scope_restored
        and workspace_evidence._workspace_scope_repair_still_unauthorized(
            initial_workspace_scope_repair,
            _current_session_scope_or_none(frame.ctx, frame.snapshot.next_pending),
        )
    ):
        frame.session_id = frame.snapshot.next_pending.id
        frame.details = {
            **plans_payloads.session_details_payload(frame.session_id),
            "readiness_gate": frame.ctx.gate_payload,
            "workspace_write_guard": initial_workspace_scope_repair,
        }
        frame.details["external_change_recovery"] = scope_recovery.recovery_diagnostics(
            frame.snapshot.plan_dir,
            frame.session_id,
            initial_workspace_scope_repair,
            state_dir=frame.args.state_dir,
        )
        frame.ctx.record_final_snapshot(frame.snapshot)
        frame.ctx.pause(
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            message=f"{plans_payloads.session_label(frame.session_id)} "
            "has retained changes to registered repositories outside its write "
            "aliases; restore the "
            "unauthorized changes to the recorded pre-turn state, or explicitly "
            "authorize a durable scope change and rerun expansion plus "
            "preflight before continuing",
            details=frame.details,
        )
        return _terminalize(frame.ctx, 1)

    non_bypassable = readiness_gate._non_bypassable_blockers(frame.preflight_report)
    if non_bypassable:
        frame.ctx.record_final_snapshot(frame.snapshot)
        frame.ctx.pause(
            reason="not-loop-ready",
            message=readiness_gate._readiness_gate_failure_message(
                frame.preflight_report
            ),
            details={**frame.ctx.gate_payload, "blocker": list(non_bypassable)},
        )
        return _terminalize(frame.ctx, 1)

    if (
        not frame.ctx.gate_passed
        and not frame.allow_not_loop_ready
        and not review_only_complete_start
    ):
        frame.preflight_report, frame.repair_payload = (
            readiness_gate._repair_readiness_gate(
                frame.preflight_report,
                plan_dir=frame.snapshot.plan_dir,
                lock_info=frame.lock_info,
                cwd=frame.cwd,
                harness=frame.args.harness,
                commit_mode=frame.commit_mode,
                state_dir=frame.args.state_dir,
                max_pending_sessions=max_pending_sessions,
                event_path=frame.ctx.event_path,
                persistence=frame.ctx.persistence,
                snapshot=frame.snapshot,
                review_requested=frame.review_requested,
                core_skipped=frame.ctx.core_skipped,
            )
        )
        frame.ctx.core_skipped = bool(frame.repair_payload["core_skipped"])
        frame.ctx.gate_payload = readiness_gate._readiness_gate_payload(
            frame.preflight_report, cwd=frame.cwd
        )
        if frame.ctx.core_skipped:
            frame.ctx.gate_payload["core_skipped"] = True
        frame.ctx.gate_passed = frame.ctx.gate_payload["ok"]
        review_only_complete_start = readiness_gate._review_only_complete_start(
            frame.preflight_report,
            review_requested=frame.review_requested,
            snapshot=frame.snapshot,
        )
        with frame.ctx.persistence.lock:
            frame.ctx.payload["readiness_gate"] = frame.ctx.gate_payload
            frame.ctx.payload["readiness_gate_repair"] = frame.repair_payload

        if not frame.ctx.gate_passed and not review_only_complete_start:
            frame.gate_message = readiness_gate._readiness_gate_failure_message(
                frame.preflight_report
            )
            frame.ctx.record_final_snapshot(frame.snapshot)
            frame.ctx.pause(
                reason="not-loop-ready",
                message=frame.gate_message,
                details=frame.ctx.gate_payload,
            )
            return _terminalize(frame.ctx, 1)

    try:
        if frame.commit_mode == run_selection.COMMIT_MODE_END:
            review_guard._validate_commit_at_end_scope(
                snapshot=frame.snapshot,
                max_sessions=frame.max_sessions,
            )
        if frame.commit_mode is not None:
            frame.per_session_registered = (
                frame.commit_mode == run_selection.COMMIT_MODE_PER_SESSION
                and frame.ctx.verification_context.workspace is not None
            )
            frame.commit_preflight = review_guard._validate_commit_preflight(
                frame.cwd,
                verification_context=frame.ctx.verification_context,
                needed_aliases=(
                    plans_session_scope.needed_repository_aliases(
                        frame.snapshot,
                        frame.ctx.verification_context.workspace.registry,
                    )
                    if frame.per_session_registered
                    else None
                ),
                clean_aliases=(
                    plans_session_scope.commit_repository_aliases(
                        frame.snapshot,
                        frame.ctx.verification_context.workspace.registry,
                    )
                    if frame.per_session_registered
                    else None
                ),
            )
            with frame.ctx.persistence.lock:
                frame.ctx.payload["commit_preflight"] = frame.commit_preflight
    except RunError as error:
        frame.ctx.record_final_snapshot(frame.snapshot)
        frame.ctx.pause(
            reason="commit-preflight",
            message=str(error),
            details={
                "commit_mode": frame.commit_mode,
                "max_sessions": frame.max_sessions,
                "pending_sessions": plans_payloads.status_counts(frame.snapshot)[
                    "pending"
                ],
            },
        )
        return _terminalize(frame.ctx, 1)

    should_transition = frame.ctx.gate_passed or (
        frame.allow_not_loop_ready and frame.preflight_report.readiness is not None
    )
    if should_transition:
        frame.running_success, frame.running_error = (
            readiness_gate.write_readiness_state(
                frame.snapshot.plan_dir,
                "running",
                expected_sha256=frame.preflight_report.expected_readiness_sha256,
            )
        )
        with frame.ctx.persistence.lock:
            if frame.running_success:
                frame.ctx.payload["readiness_state_active"] = True
                frame.ctx.payload["readiness_state_current"] = "running"
            else:
                frame.ctx.payload["readiness_state_active"] = False
                frame.ctx.payload["readiness_state_write_error"] = frame.running_error
    return None
