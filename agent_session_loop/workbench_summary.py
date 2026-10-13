"""Workbench summary construction from plan directories."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from . import (
    assurance_report,
    events,
    plans_errors,
    plans_snapshot,
    qna,
    review_continuation,
    state,
    state_abandonment,
    state_locks,
    state_payloads,
    workbench_recovery_build,
    workbench_recovery_state,
    workbench_workspace,
)
from .workbench_registry_models import DiscoveredPlan
from .workbench_summary_helpers import (
    _build_invalid_summary,
    _final_review_path_if_exists,
    _first_activity,
    _last_progress_entry_at,
    _paused_activity,
    _searchable_text,
    _truncate_preview,
    _verification_position,
)
from .workbench_summary_model import (
    ACTIVITY_LABEL_BY_STATE,
    REVIEW_CONTINUATION_ACTIVITY_LABEL,
    URGENCY_GROUP_BY_STATE,
    URGENCY_RANK_BY_STATE,
    LastActivitySource,
    PrimaryState,
    RecommendedAction,
    WorkbenchSummary,
)


def build_workbench_summary(
    plan: DiscoveredPlan,
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> WorkbenchSummary:
    """Aggregate one discovered plan into a Workbench queue summary.

    Reads plan files and runner state without rewriting them, but the shared
    lock reader reclaims a lock this host has proven dead. ``reclaim=False``
    suppresses that hygiene so an observational caller — the stdio bridge —
    projects a function of the corpus rather than of its own read order.
    Classification priority, highest first, is:

    1. ``invalid`` - plan directory missing or unparseable.
    2. ``verifying`` - active lock and ``active_verification`` set.
    3. ``running`` - active lock and ``active_turn`` set, or a live mailbox
       reply wait is holding the run between harness turns.
    4. ``paused-for-reply`` - runner state ``paused=True`` or an expired
       nonterminal reply wait can be resumed with an operator answer.
    5. ``failed`` - finished run with ``ok=False``.
    6. ``stale`` - stale lock or stale-unfinished runner state.
    7. ``recent`` - review continuation is ready despite zero pending sessions.
    8. ``complete`` - no pending sessions or eligible continuation remain.
    9. ``recent`` - pending sessions remain after a finished run.
    10. ``ready`` - pending sessions remain and no run state exists.
    """
    plan_dir = plan.plan_dir
    if not plan.exists:
        return _build_invalid_summary(
            plan,
            detail=f"plan directory does not exist: {plan_dir}",
            exists=False,
            state_dir=state_dir,
            reclaim=reclaim,
        )

    try:
        snapshot = plans_snapshot.inspect_plan(plan_dir)
    except plans_errors.PlanError as error:
        state_paths = workbench_recovery_state._summary_base_paths(plan_dir, state_dir)
        return _build_invalid_summary(
            plan,
            detail=str(error),
            exists=True,
            state_paths=state_paths,
            state_dir=state_dir,
            reclaim=reclaim,
        )

    state_path, events_path, progress_path = (
        workbench_recovery_state._summary_base_paths(snapshot.plan_dir, state_dir)
    )
    review_resume = review_continuation.evaluate_review_continuation(
        snapshot, events_path
    ).to_payload()
    lock_path = state.lock_path_for_plan(snapshot.plan_dir, state_dir)
    lock_info = state_locks._read_lock_path(
        lock_path, snapshot.plan_dir, reclaim=reclaim
    )
    runner_state = state_payloads._runner_state_payload(
        state_path,
        lock_info=lock_info,
        include_turn_activity=True,
    )
    try:
        workspace = workbench_workspace._workspace_projection(
            snapshot.plan_dir, runtime=runner_state
        )
    except (plans_errors.PlanError, OSError, ValueError) as error:
        return _build_invalid_summary(
            replace(plan, plan_dir=snapshot.plan_dir, exists=True),
            detail=f"workspace metadata unreadable: {error}",
            exists=True,
            state_paths=(state_path, events_path, progress_path),
            state_dir=state_dir,
            reclaim=reclaim,
        )
    summary_cwd = (
        runner_state.get("cwd")
        or workspace.get("workspace_root")
        or (plan.profile.cwd if plan.profile is not None else None)
    )
    expired_wait_context = workbench_recovery_state._expired_wait_qna_context(
        snapshot,
        runner_state,
        state_dir,
        reclaim=reclaim,
    )
    stale_unfinished, stale_reason = state_abandonment._stale_runner_state(
        runner_state, lock_info
    )
    recovery = workbench_recovery_build.build_workbench_recovery(
        replace(plan, plan_dir=snapshot.plan_dir, exists=True),
        state_dir=state_dir,
        reclaim=reclaim,
    ).compact_payload()

    counts = {"pending": 0, "complete": 0, "empty": 0, "unknown": 0}
    for session in snapshot.sessions:
        counts[session.status] += 1
    next_pending = snapshot.next_pending
    final_review_path = _final_review_path_if_exists(snapshot.plan_dir)

    active_turn = runner_state.get("active_turn")
    active_verification = runner_state.get("active_verification")
    base_run_id = runner_state.get("run_id")
    base_harness = runner_state.get("harness")

    artifact_dir = None
    stdout_path = None
    stderr_path = None
    last_message_path = None
    result_path = None
    if isinstance(active_turn, dict):
        artifact_dir = workbench_recovery_state._coerce_path_value(
            active_turn.get("artifact_dir")
        )
        stdout_path = workbench_recovery_state._coerce_path_value(
            active_turn.get("stdout_path")
        )
        stderr_path = workbench_recovery_state._coerce_path_value(
            active_turn.get("stderr_path")
        )
        last_message_path = workbench_recovery_state._coerce_path_value(
            active_turn.get("last_message_path")
        )
        result_path = workbench_recovery_state._coerce_path_value(
            active_turn.get("result_path")
        )

    def base_kwargs(
        *,
        primary_state: PrimaryState,
        recommended_action: RecommendedAction,
        next_action_hint: str,
        detail: str | None,
        warnings: tuple[str, ...] = (),
        run_id: str | None = base_run_id,
        harness: str | None = base_harness,
        artifact_dir: Path | None = artifact_dir,
        stdout_path: Path | None = stdout_path,
        stderr_path: Path | None = stderr_path,
        last_message_path: Path | None = last_message_path,
        result_path: Path | None = result_path,
        pause_reason: str | None = None,
        pause_message: str | None = None,
        last_message_preview: str | None = None,
        last_activity_at: str | None = None,
        last_activity_source: LastActivitySource | None = None,
        completed_at: str | None = None,
        concise_reason: str | None = None,
        activity_label: str | None = None,
    ) -> WorkbenchSummary:
        urgency_group = URGENCY_GROUP_BY_STATE[primary_state]
        resolved_activity_label = (
            activity_label or ACTIVITY_LABEL_BY_STATE[primary_state]
        )
        resolved_reason = concise_reason or detail or next_action_hint
        next_pending_session_id = next_pending.id if next_pending is not None else None
        searchable_text = _searchable_text(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            primary_state=primary_state,
            recommended_action=recommended_action,
            urgency_group=urgency_group,
            activity_label=resolved_activity_label,
            concise_reason=resolved_reason,
            detail=detail,
            next_action_hint=next_action_hint,
            next_pending_session_id=next_pending_session_id,
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            run_id=run_id,
            harness=harness,
            sources=plan.sources,
        )
        return WorkbenchSummary(
            plan_dir=snapshot.plan_dir,
            label=plan.label,
            profile=plan.profile,
            sources=plan.sources,
            exists=True,
            primary_state=primary_state,
            recommended_action=recommended_action,
            next_action_hint=next_action_hint,
            detail=detail,
            next_pending_session_id=next_pending_session_id,
            next_pending_session_path=(
                next_pending.path if next_pending is not None else None
            ),
            counts=counts,
            state_path=state_path,
            events_path=events_path,
            progress_path=progress_path,
            final_review_path=final_review_path,
            artifact_dir=artifact_dir,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            last_message_path=last_message_path,
            result_path=result_path,
            run_id=run_id,
            harness=harness,
            cwd=str(summary_cwd) if summary_cwd is not None else None,
            workspace_root=workspace.get("workspace_root"),
            registry_digest=workspace.get("registry_digest"),
            repositories=workbench_workspace._repository_rows(
                workspace.get("repositories")
            ),
            repository_count=int(workspace.get("repository_count") or 0),
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=last_message_preview,
            urgency_rank=URGENCY_RANK_BY_STATE[primary_state],
            urgency_group=urgency_group,
            last_activity_at=last_activity_at,
            last_activity_source=last_activity_source,
            completed_at=completed_at,
            activity_label=resolved_activity_label,
            concise_reason=resolved_reason,
            searchable_text=searchable_text,
            recovery=recovery,
            review_resume=review_resume,
            assurance=assurance_report.bounded_summary(runner_state.get("assurance")),
            warnings=warnings,
        )

    if (
        lock_info is not None
        and lock_info.active
        and isinstance(active_verification, dict)
    ):
        session_label = (
            workbench_recovery_state._session_identifier_from_payload(
                active_verification
            )
            or "?"
        )
        position = _verification_position(active_verification)
        check_name = active_verification.get("current_check_name") or "pending"
        verifying_activity, verifying_source = _first_activity(
            (
                (
                    active_verification.get("current_check_started_at"),
                    "active-verification",
                ),
                (active_verification.get("started_at"), "active-verification"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="verifying",
            recommended_action="watch",
            next_action_hint=(
                f"Verifying session {session_label} check {position} ({check_name})"
            ),
            detail=f"Active verification on session {session_label}",
            last_activity_at=verifying_activity,
            last_activity_source=verifying_source,
            concise_reason=(
                f"On check {position} ({check_name}) of session {session_label}"
            ),
        )

    if lock_info is not None and lock_info.active and isinstance(active_turn, dict):
        session_label = (
            workbench_recovery_state._session_identifier_from_payload(active_turn)
            or "?"
        )
        turn_index = active_turn.get("turn_index")
        running_activity, running_source = _first_activity(
            (
                (active_turn.get("last_activity_at"), "active-turn"),
                (active_turn.get("started_at"), "active-turn"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=(f"Running session {session_label} turn {turn_index}"),
            detail=f"Active turn on session {session_label}",
            last_activity_at=running_activity,
            last_activity_source=running_source,
            concise_reason=(f"Session {session_label}, turn {turn_index}"),
        )

    waiting_for_reply = runner_state.get("waiting_for_reply")
    if (
        lock_info is not None
        and lock_info.active
        and isinstance(waiting_for_reply, dict)
        and runner_state.get("waiting_for_reply_status")
        == state_payloads.WAITING_FOR_REPLY_STATUS_LIVE
    ):
        session_label = (
            workbench_recovery_state._session_identifier_from_payload(waiting_for_reply)
            or "?"
        )
        waiting_activity, waiting_source = _first_activity(
            ((runner_state.get("run_started_at"), "run-started"),)
        )
        return base_kwargs(
            primary_state="running",
            recommended_action="watch",
            next_action_hint=f"Awaiting reply for session {session_label}",
            detail=f"Live mailbox reply wait on session {session_label}",
            last_activity_at=waiting_activity,
            last_activity_source=waiting_source,
            concise_reason=f"Awaiting reply for session {session_label}",
        )

    if runner_state.get("paused") is True or expired_wait_context is not None:
        pause_reason = (
            runner_state.get("pause_reason") or runner_state.get("reason") or None
        )
        if (
            pause_reason == events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR
            and active_turn is None
            and recovery.get("kind") == "review-continuation"
        ):
            failed_activity, failed_source = _first_activity(
                (
                    (runner_state.get("run_finished_at"), "run-finished"),
                    (runner_state.get("run_started_at"), "run-started"),
                )
            )
            if (
                recovery.get("severity") == "info"
                and review_resume.get("eligible") is True
                and review_resume.get("action")
                in {"materialize-repairs", "run-repairs", "rereview"}
            ):
                return base_kwargs(
                    primary_state="recent",
                    recommended_action="run",
                    next_action_hint=recovery["title"],
                    detail=recovery["summary"],
                    pause_reason=pause_reason,
                    pause_message=runner_state.get("pause_message") or None,
                    last_activity_at=failed_activity,
                    last_activity_source=failed_source,
                    concise_reason=recovery["summary"],
                    activity_label=(
                        REVIEW_CONTINUATION_ACTIVITY_LABEL
                        if next_pending is None
                        else None
                    ),
                )
            return base_kwargs(
                primary_state="failed",
                recommended_action="inspect-failure",
                next_action_hint=recovery["title"],
                detail=recovery["summary"],
                pause_reason=pause_reason,
                pause_message=runner_state.get("pause_message") or None,
                last_activity_at=failed_activity,
                last_activity_source=failed_source,
                concise_reason=recovery["summary"],
            )

        warnings: list[str] = []
        pause_message = None
        preview = None
        qna_last_message_path = last_message_path
        paused_activity, paused_source = _paused_activity(runner_state)
        try:
            context = expired_wait_context or qna.load_qna_context(
                snapshot.plan_dir,
                state_dir=state_dir,
                reclaim=reclaim,
            )
        except qna.QnAError as error:
            # Not every pause is a question. A run that stopped on, say, an
            # unready review repair has no Q&A context, and recovery has
            # already named what it needs; offering "reply" there sends the
            # operator to a form that can only refuse them.
            if recovery.get("kind") not in {"none", "reply-needed"} and recovery.get(
                "title"
            ):
                return base_kwargs(
                    primary_state="failed",
                    recommended_action="inspect-failure",
                    next_action_hint=recovery["title"],
                    detail=recovery.get("summary") or recovery["title"],
                    pause_reason=pause_reason,
                    pause_message=runner_state.get("pause_message") or None,
                    last_activity_at=paused_activity,
                    last_activity_source=paused_source,
                    concise_reason=recovery.get("summary") or recovery["title"],
                )
            warnings.append(
                f"paused-q&a context unreadable ({error.reason}): {error.error}"
            )
            hint = (
                f"Reply needed for session {next_pending.id}"
                if next_pending is not None
                else "Reply needed"
            )
            detail = "Paused without readable last-message context"
        else:
            pause_reason = context.pause_reason or pause_reason
            pause_message = context.pause_message
            preview = _truncate_preview(context.last_message)
            qna_last_message_path = context.last_message_path
            if context.stdout_path is not None:
                stdout_path = context.stdout_path
            if context.stderr_path is not None:
                stderr_path = context.stderr_path
            if context.artifact_dir is not None:
                artifact_dir = context.artifact_dir
            if context.result_path is not None:
                result_path = context.result_path
            base_run_id_local = context.run_id or base_run_id
            base_harness_local = context.harness or base_harness
            session_label = context.session_id
            hint = f"Reply needed for session {session_label}"
            detail = pause_message or pause_reason or "Paused awaiting reply"
            return base_kwargs(
                primary_state="paused-for-reply",
                recommended_action="reply",
                next_action_hint=hint,
                detail=detail,
                warnings=tuple(warnings),
                run_id=base_run_id_local,
                harness=base_harness_local,
                artifact_dir=artifact_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                last_message_path=qna_last_message_path,
                result_path=result_path,
                pause_reason=pause_reason,
                pause_message=pause_message,
                last_message_preview=preview,
                last_activity_at=paused_activity,
                last_activity_source=paused_source,
                concise_reason=(
                    pause_message or pause_reason or "Awaiting operator reply"
                ),
            )

        return base_kwargs(
            primary_state="paused-for-reply",
            recommended_action="reply",
            next_action_hint=hint,
            detail=detail,
            warnings=tuple(warnings),
            last_message_path=qna_last_message_path,
            pause_reason=pause_reason,
            pause_message=pause_message,
            last_message_preview=preview,
            last_activity_at=paused_activity,
            last_activity_source=paused_source,
            concise_reason=(pause_message or pause_reason or "Awaiting operator reply"),
        )

    if stale_unfinished or (lock_info is not None and lock_info.stale):
        if stale_unfinished:
            reason_text = stale_reason or "Runner state has not finished"
        else:
            reason_text = lock_info.reason if lock_info is not None else "stale lock"
        lock_created_at = (
            lock_info.created_at
            if lock_info is not None and lock_info.created_at
            else None
        )
        stale_activity, stale_source = _first_activity(
            (
                (lock_created_at, "lock-created"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="stale",
            recommended_action="unlock",
            next_action_hint=f"Stale state detected: {reason_text}",
            detail=reason_text,
            last_activity_at=stale_activity,
            last_activity_source=stale_source,
            concise_reason=reason_text,
        )

    if (
        runner_state.get("present")
        and runner_state.get("run_finished_at")
        and runner_state.get("ok") is False
    ):
        reason_text = runner_state.get("reason") or "Last run finished with errors"
        failed_activity, failed_source = _first_activity(
            (
                (runner_state.get("run_finished_at"), "run-finished"),
                (runner_state.get("run_started_at"), "run-started"),
            )
        )
        return base_kwargs(
            primary_state="failed",
            recommended_action="inspect-failure",
            next_action_hint=f"Last run failed: {reason_text}",
            detail=reason_text,
            last_activity_at=failed_activity,
            last_activity_source=failed_source,
            concise_reason=reason_text,
        )

    if next_pending is None:
        complete_activity, complete_source = _first_activity(
            (
                (_last_progress_entry_at(snapshot.plan_dir), "progress-journal"),
                (runner_state.get("run_finished_at"), "run-finished"),
            )
        )
        review_action = review_resume.get("action")
        if review_resume.get("eligible") and review_action in {
            "materialize-repairs",
            "rereview",
        }:
            if review_action == "materialize-repairs":
                hint = (
                    "Resume review repairs from session "
                    f"{review_resume.get('review_session_id')}"
                )
                detail = "Review repair materialization is ready"
            else:
                hint = "Rerun review after completed repair sessions"
                detail = "Completed review repairs are awaiting rereview"
            return base_kwargs(
                primary_state="recent",
                recommended_action="run",
                next_action_hint=hint,
                detail=detail,
                last_activity_at=complete_activity,
                last_activity_source=complete_source,
                concise_reason=detail,
                activity_label=REVIEW_CONTINUATION_ACTIVITY_LABEL,
            )
        if review_resume.get("blocker") == "higher-cap-required":
            minimum = review_resume.get("minimum_required_cap")
            selected_cap = review_resume.get("selected_cap")
            if selected_cap is None:
                hint = f"Supply a review-fix cap of {minimum} or higher"
                detail = "Review repairs require a review-fix cap to resume"
            else:
                hint = f"Increase to a review-fix cap of {minimum} or higher"
                detail = (
                    "Review repair budget is exhausted at the selected cap "
                    f"of {selected_cap}"
                )
        elif review_resume.get("blocker") == "artifact-invalid":
            hint = "Correct the structured review artifact before rerunning"
            detail = review_resume.get("artifact_error") or (
                "Review artifact validation failed"
            )
        elif (
            review_action == "terminal"
            and review_resume.get("blocker")
            not in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
        ):
            hint = recovery.get("title") or ("Inspect inconsistent review continuation")
            detail = recovery.get("summary") or (
                "Recorded review repair history cannot be resumed safely: "
                f"{review_resume.get('blocker') or 'unknown blocker'}."
            )
        else:
            hint = "All sessions complete"
            detail = "No pending sessions remain"
        return base_kwargs(
            primary_state="complete",
            recommended_action="view",
            next_action_hint=hint,
            detail=detail,
            last_activity_at=complete_activity,
            last_activity_source=complete_source,
            completed_at=complete_activity,
            concise_reason=detail,
        )

    if runner_state.get("present") and runner_state.get("run_finished_at"):
        recent_activity, recent_source = _first_activity(
            ((runner_state.get("run_finished_at"), "run-finished"),)
        )
        review_hint = (
            f"Resume review repairs with session {next_pending.id}"
            if review_resume.get("action") == "run-repairs"
            else f"Resume with session {next_pending.id}"
        )
        return base_kwargs(
            primary_state="recent",
            recommended_action="run",
            next_action_hint=review_hint,
            detail=f"Last run finished at {runner_state.get('run_finished_at')}",
            last_activity_at=recent_activity,
            last_activity_source=recent_source,
            concise_reason=(
                f"Last run finished at {runner_state.get('run_finished_at')}"
            ),
        )

    ready_activity, ready_source = _first_activity(
        ((_last_progress_entry_at(snapshot.plan_dir), "progress-journal"),)
    )
    ready_hint = (
        f"Start review repairs with session {next_pending.id}"
        if review_resume.get("action") == "run-repairs"
        else f"Start session {next_pending.id}"
    )
    return base_kwargs(
        primary_state="ready",
        recommended_action="run",
        next_action_hint=ready_hint,
        detail="Ready to run; no prior runner state",
        last_activity_at=ready_activity,
        last_activity_source=ready_source,
        concise_reason=f"Next: session {next_pending.id}",
    )


def build_workbench_summaries(
    discovered: Iterable[DiscoveredPlan],
    *,
    state_dir: str | Path | None = None,
    reclaim: bool = True,
) -> tuple[WorkbenchSummary, ...]:
    """Build a Workbench summary per discovered plan in iteration order."""
    return tuple(
        build_workbench_summary(plan, state_dir=state_dir, reclaim=reclaim)
        for plan in discovered
    )
