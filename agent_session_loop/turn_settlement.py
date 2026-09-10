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
  not move", and this is what is left once those are ruled out.
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
import dataclasses
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, Protocol

from . import (
    clock,
    commit_settlement,
    conversation_resume,
    durability,
    events,
    guards,
    handoff_correction,
    handoff_guards,
    plans,
    readiness_gate,
    review,
    review_cycle,
    run_commit,
    run_control,
    run_coordination,
    run_inputs,
    run_persistence,
    run_reporting,
    run_selection,
    session_verification,
    state,
    turn_execution,
    verification,
)
from .run_errors import RunError

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as the settlement pass needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members this pass
    reaches are named: the run payload and event log every record lands in, the
    persistence seam that serializes them, the progress reporter the commit
    group writes through, the plan cache a refusal re-reads the plan from, the
    verification context and commit machinery the per-session commit needs, and
    the two ways a turn ends the run.

    ``completed_count`` and ``review_appended`` are declared mutable because
    this is the boundary that advances them: the counter is what ``--max``
    reads, and clearing the review flag is how a cycle that materialized repair
    children tells the loop another review is owed once they finish.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def run_dir(self) -> Path: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any]: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def progress(self) -> run_reporting._ProgressReporter: ...

    @property
    def guard_controller(self) -> guards.GuardController: ...

    @property
    def plan_cache(self) -> plans.PlanSnapshotCache: ...

    @property
    def verification_context(self) -> verification.VerificationContext: ...

    @property
    def commit_generation(self) -> run_commit._CommitGeneration: ...

    @property
    def control_payload(self) -> dict[str, Any]: ...

    @property
    def seen_external_command_ids(self) -> set[str]: ...

    completed_count: int
    review_appended: bool

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


class SoftFailCommit(Protocol):
    """Absorb a commit failure and keep the run going.

    The runner keeps the implementation because the owed-group settlement at
    the top of the loop shares it with the per-session commit here, and because
    the retry budget it spends lives on the run context rather than on this
    boundary. It never raises: a commit that cannot be made is a record and a
    warning, not an ending.
    """

    def __call__(
        self,
        *,
        scope: str,
        session_id: str | None,
        group: Mapping[str, Any] | None,
        commit: Mapping[str, Any] | None,
        error: str | None,
        message: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What the loop does next
# ---------------------------------------------------------------------------

#: The three moves a settlement pass can hand back. ``proceed`` is the pass
#: falling through to the loop's own control tail, ``retry`` is the loop's
#: `continue`, and ``stop`` is its `break`. Each constant is annotated with its
#: own literal rather than the union so a comparison against it narrows at the
#: call site.
SettlementAction = Literal["proceed", "retry", "stop"]
ACTION_PROCEED: Literal["proceed"] = "proceed"
ACTION_RETRY: Literal["retry"] = "retry"
ACTION_STOP: Literal["stop"] = "stop"


@dataclasses.dataclass(frozen=True)
class TurnSettlement:
    """What one settled turn leaves the loop holding.

    ``snapshot`` is the plan image the pass ended on, and it travels back on
    every action: a refusal hands back the image the loop already had, a retry
    hands back the one the retry was decided against, and a completed turn
    hands back the published one.

    ``turn_record`` is the published turn, and only :data:`ACTION_PROCEED`
    carries it: it is what the loop's control tail attributes its drain and its
    stop to, and no other action reaches that tail.
    """

    action: SettlementAction
    snapshot: plans.PlanSnapshot
    turn_record: Mapping[str, Any] | None


def settle_turn(
    ctx: RunContext,
    *,
    args: argparse.Namespace,
    cwd: Path,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot,
    snapshot: plans.PlanSnapshot,
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
    turn_executor: turn_execution.TurnExecutor,
    live_turn_payload: dict[str, Any],
    run_controls: run_control.RunControlListener,
    run_control_lock: AbstractContextManager[Any],
    wakeup: threading.Event | None,
    soft_fail_commit: SoftFailCommit,
) -> TurnSettlement:
    """Judge, record and publish one turn that changed a session's status."""
    expected_change = (
        plans.session_status_change_payload(
            session_id,
            before="pending",
            after="complete",
        ),
    )
    status_changes = plans.status_changes(before, after)
    if not status_changes:
        with ctx.persistence.lock:
            ctx.payload["expected_status_changes"] = list(expected_change)
            ctx.payload["observed_status_changes"] = []
            ctx.payload["final_snapshot"] = plans.snapshot_payload(after)
        turn_execution.record_turn_rejected(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="status-change",
            reason="no-status-change",
            detail={
                "expected_status_changes": list(expected_change),
                "observed_status_changes": [],
            },
        )
        ctx.pause(
            reason="no-status-change",
            message=f"{session_label} did not change status",
            details={
                **session_details,
                "expected_status_changes": list(expected_change),
                "observed_status_changes": [],
            },
        )
        return TurnSettlement(ACTION_STOP, snapshot, None)
    if len(status_changes) > 1:
        with ctx.persistence.lock:
            ctx.payload["expected_status_changes"] = list(expected_change)
            ctx.payload["observed_status_changes"] = list(status_changes)
            ctx.payload["final_snapshot"] = plans.snapshot_payload(after)
        turn_execution.record_turn_rejected(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="status-change",
            reason="multiple-status-changes",
            detail={
                "expected_status_changes": list(expected_change),
                "observed_status_changes": list(status_changes),
            },
        )
        ctx.pause(
            reason="multiple-status-changes",
            message="more than one session status changed during one harness turn",
            details={
                **session_details,
                "expected_status_changes": list(expected_change),
                "observed_status_changes": list(status_changes),
            },
        )
        return TurnSettlement(ACTION_STOP, snapshot, None)
    if status_changes != expected_change:
        with ctx.persistence.lock:
            ctx.payload["expected_status_changes"] = list(expected_change)
            ctx.payload["observed_status_changes"] = list(status_changes)
            ctx.payload["final_snapshot"] = plans.snapshot_payload(after)
        turn_execution.record_turn_rejected(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="status-change",
            reason="unexpected-status-change",
            detail={
                "expected_status_changes": list(expected_change),
                "observed_status_changes": list(status_changes),
            },
        )
        ctx.pause(
            reason="unexpected-status-change",
            message="the changed session status did not match the expected "
            "pending completion",
            details={
                **session_details,
                "expected_status_changes": list(expected_change),
                "observed_status_changes": list(status_changes),
            },
        )
        return TurnSettlement(ACTION_STOP, snapshot, None)

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
        run_coordination.drain_external_control_queue(
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
        ledger_updates = plans.sync_derived_status_ledgers(after)
    except plans.PlanError as error:
        ctx.record_final_snapshot(after)
        turn_execution.record_turn_rejected(
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
        except plans.PlanError as error:
            ctx.record_final_snapshot(after)
            turn_execution.record_turn_rejected(
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
            turn_payload["after"] = plans.snapshot_payload(after)
            durability.write_json_file(paths["result"], turn_payload)

    readiness_gate._refresh_active_readiness_state(
        ctx.payload, after.plan_dir, ctx.persistence
    )

    repair_owned_commit = is_repair_session and commit_mode is None
    if not is_review_session and (
        commit_mode == run_selection.COMMIT_MODE_PER_SESSION or repair_owned_commit
    ):
        session_targets: list[dict[str, Any]] = []
        try:
            session_targets = (
                commit_settlement._registered_session_commit_targets(
                    ctx.verification_context,
                    turn_payload.get("workspace_write_guard", {}),
                )
                if ctx.verification_context.workspace is not None
                else commit_settlement._dirty_commit_targets(ctx.verification_context)
            )
            if repair_owned_commit:
                session_targets = [
                    target
                    for target in session_targets
                    if not commit_settlement._commit_target_is_clean(target)
                ]
            if session_targets or not repair_owned_commit:
                commit_group, failed_commit = commit_settlement._run_commit_group(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    progress=ctx.progress,
                    verification_context=ctx.verification_context,
                    run_dir=ctx.run_dir,
                    run_id=ctx.run_id,
                    session_id=session_id,
                    targets=session_targets,
                    generation=ctx.commit_generation,
                    persistence=ctx.persistence,
                )
            else:
                commit_group = None
                failed_commit = None
        except RunError as error:
            commit_group = None
            failed_commit = None
            commit_error = str(error)
        else:
            commit_error = None
        if repair_owned_commit:
            with ctx.persistence.lock:
                turn_payload["repair_commit"] = {
                    "mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
                    "result": (
                        "no-changes"
                        if not session_targets
                        else (
                            "failed"
                            if failed_commit is not None or commit_error is not None
                            else "committed"
                        )
                    ),
                    "commit_group": commit_group,
                }
                durability.write_json_file(paths["result"], turn_payload)
        if failed_commit is not None or commit_error is not None:
            # The turn itself stands: it executed, it verified, and the
            # session it completed is still complete. Only the commit
            # of its changes failed, so the failure is recorded on the
            # turn and on the run, and the turn goes on to be committed
            # to the plan. Recording a `turn_rejected` here instead
            # would say a guard refused a turn the very next call
            # accepts.
            with ctx.persistence.lock:
                turn_payload["session_commit"] = {
                    "guard": (
                        "repair-session-commit"
                        if repair_owned_commit
                        else "session-commit"
                    ),
                    "reason": "commit-failed",
                    "error": commit_error,
                    "commit_group": commit_group,
                    "commit": failed_commit,
                }
                durability.write_json_file(paths["result"], turn_payload)
            soft_fail_commit(
                scope="session",
                session_id=session_id,
                group=commit_group,
                commit=failed_commit,
                error=commit_error,
                message=(f"auto-commit failed after session {session_id} completed"),
            )

    try:
        commit = turn_executor.commit(
            payload=ctx.payload,
            turn_payload=turn_payload,
            live_turn=live_turn_payload,
            accepted=after,
        )
    except turn_execution.TurnCommitRejected as rejection:
        ctx.record_final_snapshot(after)
        turn_execution.record_turn_rejected(
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
                **plans.session_identity_payload(session_id),
                "disposition": conversation_resume.CONTINUATION_RETIRED,
                "retired": list(retired),
                "detail": retire_error or "session completed",
            },
        )
    with ctx.persistence.lock:
        ctx.payload["completed_sessions"].append(plans.session_compat_value(session_id))
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
