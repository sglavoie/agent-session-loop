"""What the run does with a turn that produced work, and how it settles it.

``turn_dispositions`` owns the ways a turn can end that are not progress. This
boundary owns the other half: the turn that ran, changed a session's status,
and now has to be judged, recorded, committed and — on the review lane —
advanced to whatever the verdict asks for next.

The sequence, in the one order it may run in:

* **Status-change validation.** Exactly one session moved, and it moved from
  ``pending`` to ``complete``. Three refusals live here: no change at all, more
  than one, and the wrong one. They run behind the terminating dispositions on
  purpose — a turn that timed out has a sharper diagnosis than "the status did
  not move", and this is what is left once those are ruled out. Each refusal
  still runs the plan-artifact guard in adjudicate-only mode after it pauses,
  so a packet the turn rewrote gets a verdict beside that pause (ASI-119).
* **The plan-artifact guard, unconditionally.** ``turn_postflight`` already ran
  it gated on a terminating disposition; this is the same guard on the surviving
  turn, and it catches the tampering the status guards cannot see: an edit to
  another session's packet that moves no status at all.
* **Session verification.** The completion contract's own checks, the repair
  budget they spend, and — on a review session — the artifact the review had to
  produce.
* **The review verdict.** Review sessions only. The external control queue is
  drained first so an ``asl control stop`` is observed before a verdict retry
  short-circuits the rest of this pass, and the verdict may send the same review
  session round again.
* **The progress journal, then the derived ledgers.** In that order and no
  other: only verified work reaches ``progress.md``, and the journal row lands
  before the ledgers it describes are re-synced.
* **The per-session commit.** Under ``--commit per-session``, or owed by a
  repair child that owns its own commit. A commit that fails does not end the
  run: it is recorded on the turn and soft-failed onto the run's owed group.
* **The turn commit.** The publication that turns the executed turn into an
  accepted one, plus the run-level completion counters and the review verdict
  projection.
* **The review cycle advance.** Review sessions only, and last: the verdict
  decides whether the run is finished, paused, or owes repair children the loop
  must go round again for.

Durable records this boundary writes: the rejection event and final snapshot of
every refusal, the turn result artifact at each amendment, the ``progress.md``
row, the derived status ledgers, the readiness state refresh, the per-session
commit group, the published turn record, and the review cycle's own events.
Every pause goes through ``ctx.pause`` and every state write through the run's
persistence seam.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol below, naming only the members this
pass reaches. ``run_controls`` is a
parameter rather than a context member for the reason ``run_completion`` already
states: it is ``RunControlListener | None`` on the dataclass and mypy narrows it
inside the loop, and that narrowing does not travel across a call. The commit
soft-fail enters as the :class:`SoftFailCommit` seam because ``run_loop``
shares that implementation with the owed-group settlement above the loop.

Three invariants survive the move:

* **Nothing is committed before every guard that could refuse it has run.** The
  order above is the guarantee, and it is a single function precisely so the
  order is readable as one sequence rather than reconstructed across call sites.
* **Every refusal ends the pass.** A guard that pauses returns immediately with
  :data:`ACTION_STOP`; there is no path where a refusal is recorded and the
  turn is then committed anyway. Accepting a turn and committing the bytes it
  retained are two different acts, and only the first one is claimed here: a
  refusal that pauses recoverably may still have its changes committed
  afterwards, by the run loop's paused-work settlement at the loop's exit,
  under a message that says the work is unverified and the session is back to
  ``pending``. That commit is not this pass reversing itself. ``turn_finished``
  is still never emitted, the session is still not ``complete``, and the
  per-session commit below still runs only on the accepted path — what the
  settlement adds is that the retained dirt does not have to outlive the
  process to stay inspectable.
* **The image the loop keeps is the one this pass ended on.** Both retry and
  completion hand back the snapshot the next pass must start from, so the loop
  never re-reads the plan behind a decision already made against it.
"""

from __future__ import annotations

import argparse
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from . import (
    clock,
    continuation_guards,
    conversation_resume,
    durability,
    events,
    handoff_correction,
    handoff_guards,
    plans_errors,
    plans_models,
    plans_payloads,
    plans_status_tables,
    readiness_gate,
    review,
    review_cycle,
    run_control,
    run_coordination_control_drain,
    run_inputs,
    session_verification,
    state,
    turn_execution_commit,
    turn_execution_executor,
    turn_settlement_session_commit,
    turn_settlement_status,
)
from .turn_settlement_types import ACTION_PROCEED, ACTION_RETRY, ACTION_STOP
from .turn_settlement_types import RunContext as RunContext
from .turn_settlement_types import SettlementAction as SettlementAction
from .turn_settlement_types import SoftFailCommit as SoftFailCommit
from .turn_settlement_types import TurnSettlement as TurnSettlement


def settle_turn(
    ctx: RunContext,
    *,
    args: argparse.Namespace,
    cwd: Path,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot,
    snapshot: plans_models.PlanSnapshot,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_index: int,
    total_sessions: int,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_artifacts_before: Mapping[str, str],
    plan_artifacts_written_by_runner: tuple[str, ...],
    is_review_session: bool,
    is_repair_session: bool,
    review_session_version: int | None,
    allow_not_loop_ready: bool,
    commit_mode: str | None,
    max_review_fix_cycles: int,
    review_requested: bool,
    turn_executor: turn_execution_executor.TurnExecutor,
    live_turn_payload: dict[str, Any],
    run_controls: run_control.RunControlListener,
    run_control_lock: AbstractContextManager[Any],
    wakeup: threading.Event | None,
    soft_fail_commit: SoftFailCommit,
) -> TurnSettlement:
    """Judge, record and publish one turn that changed a session's status."""
    refusal = turn_settlement_status.validate_status_change(
        ctx,
        before=before,
        after=after,
        snapshot=snapshot,
        session_id=session_id,
        session_label=session_label,
        session_details=session_details,
        turn_payload=turn_payload,
        paths=paths,
        plan_artifacts_before=plan_artifacts_before,
        plan_artifacts_written_by_runner=plan_artifacts_written_by_runner,
    )
    if refusal is not None:
        return refusal

    # The turn survived every result branch, so nothing terminated it
    # early and the comparison above was deferred to here. Behind the
    # status guards, a turn that completed the wrong session keeps
    # their sharper diagnosis; what is left is the tampering they
    # cannot see, an edit that moves no status at all.
    if handoff_guards._enforce_plan_artifact_guard(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        before=before,
        after=after,
        digests_before=plan_artifacts_before,
        runner_written=plan_artifacts_written_by_runner,
        session_id=session_id,
        session_label=session_label,
        session_details=session_details,
        turn_payload=turn_payload,
        paths=paths,
        plan_cache=ctx.plan_cache,
        append_handoff_note=handoff_correction._append_handoff_note,
        reinspect=session_verification._reinspect_after_revert,
    ):
        return TurnSettlement(ACTION_STOP, snapshot, None)

    if session_verification._enforce_session_verification(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        progress=ctx.progress,
        before=before,
        after=after,
        cwd=cwd,
        session_id=session_id,
        session_label=session_label,
        session_details=session_details,
        allow_not_loop_ready=allow_not_loop_ready,
        turn_payload=turn_payload,
        paths=paths,
        plan_cache=ctx.plan_cache,
        append_handoff_note=handoff_correction._append_handoff_note,
        run_id=ctx.run_id,
        turn_index=turn_index,
        total_sessions=total_sessions,
        verification_context=ctx.verification_context,
        review_broad_failure_deferral=is_review_session,
        require_review_artifact=(
            review_session_version is not None
            and review_session_version >= review.STRUCTURED_REVIEW_MIN_VERSION
        ),
        generated_review_session=is_review_session,
    ):
        return TurnSettlement(ACTION_STOP, snapshot, None)

    if is_review_session:
        # A retry short-circuits the loop tail, so the operator's stop
        # request has to be visible *here* or it would be honored a
        # whole review turn late. The queue drain is what makes an
        # `asl control stop` observable at all on a review turn; the
        # read below is non-consuming, like every other checkpoint —
        # only `run_termination._honor_stop_after_current` consumes,
        # at the moment the loop actually stops.
        run_coordination_control_drain.drain_external_control_queue(
            plan_dir=before.plan_dir,
            state_dir_arg=args.state_dir,
            control_state=run_controls.state,
            seen_ids=ctx.seen_external_command_ids,
            payload=ctx.payload,
            event_path=ctx.event_path,
            control_payload=ctx.control_payload,
            context=turn_payload,
            persistence=ctx.persistence,
            run_control_lock=run_control_lock,
            wakeup=wakeup,
            guard_controller=ctx.guard_controller,
        )
        with run_control_lock:
            stop_after_current_pending = run_controls.state.stop_after_current
        verdict_action, verdict_snapshot = session_verification._enforce_review_verdict(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            before=before,
            after=after,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            require_artifact=(
                review_session_version is not None
                and review_session_version >= review.STRUCTURED_REVIEW_MIN_VERSION
            ),
            stop_after_current_pending=stop_after_current_pending,
        )
        if verdict_action == "pause":
            return TurnSettlement(ACTION_STOP, snapshot, None)
        if verdict_action == "retry":
            assert verdict_snapshot is not None
            return TurnSettlement(ACTION_RETRY, verdict_snapshot, None)

    completed_at = clock.utc_iso_now()
    progress_error: str | None = None
    try:
        progress_appended = handoff_guards._append_progress_entry(
            after, session_id, completed_at
        )
    except Exception as error:
        progress_appended = False
        progress_error = str(error)
        ctx.persistence.append_event(
            ctx.event_path,
            "progress-journal-append-failed",
            {
                "run_id": ctx.payload.get("run_id"),
                "session": session_id,
                "session_label": session_label,
                "completed_at": completed_at,
                "error": progress_error,
            },
        )
    with ctx.persistence.lock:
        turn_payload["progress_appended"] = progress_appended
        if progress_error is not None:
            turn_payload["progress_error"] = progress_error
        durability.write_json_file(paths["result"], turn_payload)

    try:
        ledger_updates = plans_status_tables.sync_derived_status_ledgers(after)
    except plans_errors.PlanError as error:
        ctx.record_final_snapshot(after)
        turn_execution_commit.record_turn_rejected(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="ledger-sync",
            reason="malformed-plan",
            detail={"error": str(error)},
        )
        ctx.pause(
            reason="malformed-plan",
            message="derived status ledgers could not be synced",
            details={
                **session_details,
                "error": str(error),
            },
        )
        return TurnSettlement(ACTION_STOP, snapshot, None)
    if ledger_updates:
        try:
            after = ctx.plan_cache.get_fresh(before.plan_dir)
        except plans_errors.PlanError as error:
            ctx.record_final_snapshot(after)
            turn_execution_commit.record_turn_rejected(
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                turn_payload=turn_payload,
                session_id=session_id,
                guard="ledger-sync",
                reason="malformed-plan",
                detail={"error": str(error)},
            )
            ctx.pause(
                reason="malformed-plan",
                message="plan could not be inspected after syncing ledgers",
                details={
                    **session_details,
                    "error": str(error),
                },
            )
            return TurnSettlement(ACTION_STOP, snapshot, None)
        with ctx.persistence.lock:
            turn_payload["ledger_updates"] = [str(path) for path in ledger_updates]
            turn_payload["after"] = plans_payloads.snapshot_payload(after)
            durability.write_json_file(paths["result"], turn_payload)

    readiness_gate._refresh_active_readiness_state(
        ctx.payload, after.plan_dir, ctx.persistence
    )

    turn_settlement_session_commit.commit_session_if_owed(
        ctx,
        session_id=session_id,
        turn_payload=turn_payload,
        paths=paths,
        is_review_session=is_review_session,
        is_repair_session=is_repair_session,
        commit_mode=commit_mode,
        soft_fail_commit=soft_fail_commit,
    )

    try:
        commit = turn_executor.commit(
            payload=ctx.payload,
            turn_payload=turn_payload,
            live_turn=live_turn_payload,
            accepted=after,
        )
    except turn_execution_commit.TurnCommitRejected as rejection:
        ctx.record_final_snapshot(after)
        turn_execution_commit.record_turn_rejected(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="ledger-sync",
            reason=rejection.reason,
            detail=rejection.detail,
        )
        ctx.pause(
            reason=rejection.reason,
            message=rejection.message,
            details={**session_details, **rejection.detail},
        )
        return TurnSettlement(ACTION_STOP, snapshot, None)

    # A completed session has no turn left to continue, so its conversation
    # stops being resumable here. Leaving it live would let a later attempt at
    # the same session id -- a re-run, a reopened repair group -- continue work
    # that already finished and already committed. Retirement is an append, so
    # the history stays readable; it is deliberately *not* raised on failure,
    # because the session is complete on disk and a ledger write must not undo
    # that. A retirement that did not land is caught on the next attempt, which
    # reads the conversation as still live and stops for recovery.
    retired, retire_error = conversation_resume.retire_session_conversations(
        state.plan_state_dir(after.plan_dir, args.state_dir), session_id=session_id
    )
    if retired or retire_error is not None:
        ctx.persistence.append_event(
            ctx.event_path,
            events.CONVERSATION_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                **plans_payloads.session_identity_payload(session_id),
                "disposition": conversation_resume.CONTINUATION_RETIRED,
                "retired": list(retired),
                "detail": retire_error or "session completed",
            },
        )
    # The session's accumulated write evidence retires beside its conversation
    # and for the same reason: the work it describes is committed and published,
    # so a later attempt at this id must be scoped by what it can observe rather
    # than inherit a path list from turns that are already settled.
    with ctx.persistence.lock:
        ctx.session_attempts.retire(session_id)
        ctx.payload[continuation_guards.SESSION_ATTEMPTS_PAYLOAD_KEY] = (
            ctx.session_attempts.to_payload()
        )
        ctx.payload["completed_sessions"].append(
            plans_payloads.session_compat_value(session_id)
        )
        ctx.payload.setdefault("completed_session_ids", []).append(session_id)
        if is_review_session:
            ctx.payload["review_completed"] = True
            review_verdict = commit.record.get("review_verdict")
            if isinstance(review_verdict, dict):
                ctx.payload["review_verdict"] = {
                    "status_line": review_verdict["status_line"],
                    "outcome": review_verdict["outcome"],
                    "total_findings": review_verdict["total_findings"],
                    "findings_by_severity": review_verdict["findings_by_severity"],
                }
    if not is_review_session:
        ctx.completed_count += 1
    snapshot = after
    ctx.record_final_snapshot(commit.published)

    if is_review_session:
        cycle_action, snapshot = review_cycle._advance_review_cycle(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            args=args,
            cwd=cwd,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            snapshot=snapshot,
            turn_payload=commit.record,
            max_review_fix_cycles=max_review_fix_cycles,
            plan_cache=ctx.plan_cache,
            reinspect=session_verification._reinspect_after_plan_mutation,
        )
        review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=review_requested,
        )
        if cycle_action == "paused":
            return TurnSettlement(ACTION_STOP, snapshot, None)
        if cycle_action == "complete":
            with ctx.persistence.lock:
                ctx.payload["reason"] = review_cycle._terminal_review_reason(
                    ctx.payload
                )
            return TurnSettlement(ACTION_STOP, snapshot, None)
        # Repair children were materialized: clear the appended flag
        # so the loop appends the next review session once the
        # children complete, then fall through to the control tail
        # so operator stop requests are honored between cycles.
        ctx.review_appended = False

    return TurnSettlement(ACTION_PROCEED, snapshot, commit.record)
