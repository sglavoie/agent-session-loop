"""How a plan with nothing left to run reaches its ending.

Every other boundary in this package answers a question about *a session*.
This one owns the pass where there is no session left to answer for: the run
loop found no pending work, and something still has to decide whether that is
an ending, and which ending it is. Three things can be true of a plan with no
next session, and this module is the ordered decision between them.

* **The plan is inconsistent.** No session is pending and not every session is
  complete, so selection and status disagree. Nothing here can repair that, so
  the run pauses on ``no-pending-but-incomplete`` with the snapshot attached.
* **The plan has work left that is not a session.** Under
  ``--commit-mode end`` the run still owes one commit, and under
  ``--review-harness`` it still owes a final review — and a review that finds
  something owes repair children, which are sessions, which puts pending work
  back on a plan that had none. Each of those grows the run rather than ending
  it, which is why this boundary can hand the loop back a "go round again"
  instead of only a verdict.
* **The plan is finished.** Nothing is owed, so ``reason`` becomes
  ``all-complete`` and the loop stops; terminal adjudication downstream decides
  whether that success stands.

The loop's ``break`` and ``continue`` were the only statement of that decision
before this move, and neither survives a boundary. :class:`CompletionOutcome`
is what replaces them: every path out of :func:`complete_plan` returns one, it
carries the plan image the pass ended on, and its two actions are exactly the
two the loop had. The branch never fell through, so the outcome is total —
there is no third answer for the caller to invent.

Durable records this boundary writes, all through collaborators that already
own them: the **end-of-run commit group** (``commit_settlement``), the
**generated review session file** and its two events (``review_session_body``),
the **review repair children** and the reopen record that accounts for them
(``review_repair`` and ``review_cycle``), the plan's **readiness state**
(``readiness_gate``), and the run state and event stream every one of those
writes through :attr:`RunContext.persistence`. It creates no record of its own.

Nothing here imports the runner, and since session 37 it does not import the
loop that owns ``RunContext`` either: the context enters as the structural
:class:`RunContext` Protocol below — the ``CoordinationPersistence``/
``LockedRun`` pattern applied
to the loop's own context object. The two collaborators the context does not
carry enter beside it: the run-control listener, which is non-optional here
because a completion pass only happens after the loop bound it, and
``soft_fail_commit``, which stays in ``run_loop`` because the owed-group
recovery above the loop shares it. Everything else is read at its true owner.

Five invariants survive the move:

* **The end commit is attempted once.** ``commit_at_end_attempted`` is set
  before the attempt, not after it, so a commit that fails still marks the
  attempt spent and the next pass moves on to the review instead of retrying
  here. The retry belongs to the owed-group settlement at the top of the loop.
* **A commit failure never ends the run.** The end-of-run commit soft-fails
  through the same recorder every other commit boundary uses, and the pass
  returns "go round again" rather than a verdict, so an unresolved group is
  reported by terminal adjudication rather than by an exception.
* **A stop is observed before review work begins.** The external control queue
  is drained and ``stop_after_current`` is read *before* the review resume is
  computed, because the review and its repair children are work that starts
  after the session the operator asked to stop after.
* **Plan growth is re-gated before it runs.** Both materialized repair children
  and a freshly appended review session reach this point under the readiness
  report that said no turns were required, so each re-gates the grown plan and
  re-writes ``running`` before returning to the loop. A *reused* pending review
  session is not re-gated: it was authorized when it was created.
* **The image the loop keeps is the one this pass ended on.** Every mutation
  that grows the plan is followed by a fresh inspection, and the resulting
  snapshot travels back on the outcome, so the next selection never runs
  against the image the pass started from.
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
    events,
    guards,
    handoff_guards,
    plans_errors,
    plans_models,
    plans_payloads,
    plans_snapshot,
    readiness_gate,
    repository_evidence,
    review_continuation,
    review_cycle,
    review_delta,
    review_repair,
    review_session_body,
    review_session_contract,
    run_commit,
    run_control,
    run_coordination,
    run_inputs,
    run_persistence,
    run_reporting,
    run_selection,
    run_termination,
    verification,
    workspace_evidence,
)
from .run_errors import RunError

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as the completion pass needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members a completion
    pass actually reaches are named, which is what makes this a boundary rather
    than a second copy of the run's shape: everything the loop carries for its
    turns — the counters, the idle baseline, the per-turn details — is absent
    here because nothing on this path may touch it.

    ``commit_at_end_attempted`` and ``review_appended`` are declared mutable
    because this is the boundary that spends them: each marks a piece of
    end-of-run work as owed once and never again, and the loop reads them
    nowhere else.
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
    def plan_cache(self) -> plans_snapshot.PlanSnapshotCache: ...

    @property
    def verification_context(self) -> verification.VerificationContext: ...

    @property
    def commit_generation(self) -> run_commit._CommitGeneration: ...

    @property
    def control_payload(self) -> dict[str, Any]: ...

    @property
    def seen_external_command_ids(self) -> set[str]: ...

    commit_at_end_attempted: bool
    review_appended: bool

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None: ...

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
    the top of the loop shares it with the end-of-run commit here, and because
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

#: The two moves a completion pass can hand back, spelled as the loop
#: statements they replace. Each constant is annotated with its own literal
#: rather than the union so a comparison against it narrows at the call site.
CompletionAction = Literal["continue", "break"]

#: Go round the loop again: this pass grew the plan rather than ending it.
ACTION_CONTINUE: Literal["continue"] = "continue"

#: Stop the loop: this pass either settled the run or paused it.
ACTION_BREAK: Literal["break"] = "break"


@dataclasses.dataclass(frozen=True)
class CompletionOutcome:
    """The loop's next move, paired with the plan image it makes it against.

    The pass rebinds its snapshot every time it grows the plan, so returning
    the action alone would leave the caller selecting out of a stale image.
    Both constructors take the snapshot for that reason: there is no way to
    state an outcome here without stating which plan it is an outcome for.
    """

    action: CompletionAction
    snapshot: plans_models.PlanSnapshot

    @classmethod
    def again(cls, snapshot: plans_models.PlanSnapshot) -> CompletionOutcome:
        """Grow the run: the loop takes another pass over this plan."""
        return cls(action=ACTION_CONTINUE, snapshot=snapshot)

    @classmethod
    def stop(cls, snapshot: plans_models.PlanSnapshot) -> CompletionOutcome:
        """End the run: the loop leaves and terminalization takes over."""
        return cls(action=ACTION_BREAK, snapshot=snapshot)


# ---------------------------------------------------------------------------
# The completion pass
# ---------------------------------------------------------------------------


def complete_plan(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    args: argparse.Namespace,
    cwd: Path,
    commit_mode: str | None,
    review_requested: bool,
    max_review_fix_cycles: int,
    allow_not_loop_ready: bool,
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
    run_controls: run_control.RunControlListener,
    run_control_lock: AbstractContextManager[Any],
    wakeup: threading.Event | None,
    soft_fail_commit: SoftFailCommit,
) -> CompletionOutcome:
    """Decide what a plan with no pending session owes, and settle it.

    Called once per loop pass that selects nothing, in the order the plan's
    remaining obligations must be discharged: the consistency check first
    because nothing below is meaningful on a plan whose status and selection
    disagree, then the end-of-run commit because a review must read committed
    work, then the review and the repair children it may reopen, and only then
    the ``all-complete`` ending.

    Returns the loop's next move rather than performing it. A returned
    :data:`ACTION_CONTINUE` always means the plan grew — a commit was made, a
    review session was appended, or repair children were materialized — so the
    caller re-selects; :data:`ACTION_BREAK` means this run is over, whether it
    settled or paused.
    """
    if not run_inputs._all_sessions_complete(snapshot):
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="no-pending-but-incomplete",
            message="no pending session was found, but not every session is complete",
            details={"snapshot": plans_payloads.snapshot_payload(snapshot)},
        )
        return CompletionOutcome.stop(snapshot)

    if commit_mode == run_selection.COMMIT_MODE_END and not ctx.commit_at_end_attempted:
        ctx.commit_at_end_attempted = True
        ctx.record_final_snapshot(snapshot)
        try:
            end_targets = commit_settlement._registered_end_commit_targets(
                ctx.verification_context
            )
            commit_group, failed_commit = commit_settlement._run_commit_group(
                event_path=ctx.event_path,
                payload=ctx.payload,
                progress=ctx.progress,
                verification_context=ctx.verification_context,
                run_dir=ctx.run_dir,
                run_id=ctx.run_id,
                session_id=None,
                targets=end_targets,
                generation=ctx.commit_generation,
                persistence=ctx.persistence,
            )
        except RunError as error:
            commit_group = None
            failed_commit = None
            commit_error = str(error)
        else:
            commit_error = None
        if failed_commit is not None or commit_error is not None:
            soft_fail_commit(
                scope="end",
                session_id=None,
                group=commit_group,
                commit=failed_commit,
                error=commit_error,
                message=("final auto-commit failed after all sessions completed"),
            )
        return CompletionOutcome.again(snapshot)

    if review_requested and not ctx.review_appended:
        # The review and its repair children are work that starts
        # *after* the session the operator asked to stop after, so
        # the request has to be visible before either begins.
        run_coordination.drain_external_control_queue(
            plan_dir=snapshot.plan_dir,
            state_dir_arg=args.state_dir,
            control_state=run_controls.state,
            seen_ids=ctx.seen_external_command_ids,
            payload=ctx.payload,
            event_path=ctx.event_path,
            control_payload=ctx.control_payload,
            context=None,
            persistence=ctx.persistence,
            run_control_lock=run_control_lock,
            wakeup=wakeup,
            guard_controller=ctx.guard_controller,
        )
        with run_control_lock:
            stop_before_review = run_controls.state.stop_after_current
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

        if review_resume.action == "terminal":
            terminal_disposition = (
                review_resume.blocker
                in review_continuation.TERMINAL_REVIEW_DISPOSITIONS
            )
            with ctx.persistence.lock:
                review_cycle._set_existing_review_verdict(
                    ctx.payload,
                    snapshot.plan_dir,
                    read_review_verdict=handoff_guards._read_review_verdict,
                )
                ctx.payload["review_fix_disposition"] = (
                    review_resume.source_disposition if terminal_disposition else None
                )
                ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(
                    snapshot
                )
            if terminal_disposition:
                with ctx.persistence.lock:
                    ctx.payload["reason"] = review_cycle._terminal_review_reason(
                        ctx.payload
                    )
            else:
                blocker = review_resume.blocker or "unknown blocker"
                ctx.pause(
                    reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                    message="Recorded review repair history cannot be "
                    f"resumed safely: {blocker}.",
                    details={"review_resume": review_resume.to_payload()},
                )
            return CompletionOutcome.stop(snapshot)

        if review_resume.action == "materialize-repairs":
            if not review_resume.eligible:
                with ctx.persistence.lock:
                    review_cycle._set_existing_review_verdict(
                        ctx.payload,
                        snapshot.plan_dir,
                        read_review_verdict=handoff_guards._read_review_verdict,
                    )
                    ctx.payload["review_fix_disposition"] = (
                        review_resume.source_disposition
                    )
                    ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(
                        snapshot
                    )
                if review_resume.blocker == "higher-cap-required":
                    with ctx.persistence.lock:
                        ctx.payload["reason"] = review_cycle._terminal_review_reason(
                            ctx.payload
                        )
                    return CompletionOutcome.stop(snapshot)

                reopen_error = (
                    review_resume.artifact_error
                    or "review repair artifact is not materializable"
                )
                failed_record = review_cycle._review_reopen_record(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    resume=review_resume,
                    max_review_fix_cycles=max_review_fix_cycles,
                    materialized=False,
                    result=(review_resume.blocker or "reopen-ineligible"),
                    error=reopen_error,
                )
                review_cycle._record_review_reopen(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    record=failed_record,
                )
                ctx.pause(
                    reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                    message="review repair reopening failed for "
                    f"session {review_resume.review_session_id}: "
                    f"{reopen_error}",
                    details={
                        "review_resume": review_resume.to_payload(),
                        "error": reopen_error,
                    },
                )
                return CompletionOutcome.stop(snapshot)

            if stop_before_review:
                run_termination._stop_before_review_work(
                    run_controls,
                    snapshot=snapshot,
                    payload=ctx.payload,
                    event_path=ctx.event_path,
                    control_payload=ctx.control_payload,
                    record_existing_review_verdict=(
                        review_cycle.existing_review_verdict_recorder(
                            handoff_guards._read_review_verdict
                        )
                    ),
                    persistence=ctx.persistence,
                )
                return CompletionOutcome.stop(snapshot)

            repair_result = review_repair.materialize_review_repair_sessions(
                snapshot.plan_dir,
                review_session_id=(review_resume.review_session_id),
                cwd=cwd,
                state_dir=args.state_dir,
                hold_lock=False,
                allow_not_loop_ready=allow_not_loop_ready,
            )
            repair_session_ids = tuple(
                child.session_id.value for child in repair_result.children
            )
            materialized = bool(repair_result.ok and repair_session_ids)
            reopen_record = review_cycle._review_reopen_record(
                event_path=ctx.event_path,
                payload=ctx.payload,
                resume=review_resume,
                max_review_fix_cycles=max_review_fix_cycles,
                materialized=materialized,
                repair_session_ids=repair_session_ids,
                result=repair_result.reason,
                error=repair_result.error,
            )
            review_cycle._record_review_reopen(
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                record=reopen_record,
            )

            if (
                not repair_session_ids
                and repair_result.reason
                == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
            ):
                # No child survived materialization: the pre-authoring
                # gate refused them, or the loop-ready gate withdrew
                # them (ASI-136). Either way the plan is the one this
                # cycle started from, so there is nothing to proceed
                # into even with --allow-not-loop-ready.
                ctx.record_final_snapshot(snapshot)
                ctx.pause(
                    reason="not-loop-ready",
                    message="no review repair children survived "
                    "materialization for session "
                    f"{repair_result.review_session_id}: "
                    f"{repair_result.error}",
                    details={
                        "review_resume": review_resume.to_payload(),
                        "blockers": list(repair_result.blockers),
                        "blocker": list(repair_result.non_bypassable_blockers),
                        "repair_children_rolled_back": (repair_result.rolled_back),
                        **(
                            {"repair_rollback": repair_result.rollback}
                            if repair_result.rollback is not None
                            else {}
                        ),
                    },
                )
                return CompletionOutcome.stop(snapshot)

            if not repair_result.ok or not repair_session_ids:
                reopen_error = (
                    repair_result.error
                    or "review repair reopening produced no children"
                )
                ctx.record_final_snapshot(snapshot)
                ctx.pause(
                    reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
                    message="review repair reopening failed for "
                    f"session {review_resume.review_session_id}: "
                    f"{reopen_error}",
                    details={
                        "review_resume": review_resume.to_payload(),
                        "error": reopen_error,
                        "detail": repair_result.detail,
                    },
                )
                return CompletionOutcome.stop(snapshot)

            try:
                snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
            except plans_errors.PlanError as error:
                ctx.pause(
                    reason="malformed-plan",
                    message="plan could not be inspected after "
                    f"reopening review repairs: {error}",
                    details={"error": str(error)},
                )
                return CompletionOutcome.stop(snapshot)

            review_resume = review_cycle._set_review_resume(
                snapshot=snapshot,
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                max_repair_group_attempts=(run_inputs._max_repair_group_attempts(args)),
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=True,
            )
            with ctx.persistence.lock:
                ctx.payload["review_fix_disposition"] = (
                    review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                )
                ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(
                    snapshot
                )

            if (
                repair_result.reason
                == review_repair.REVIEW_REPAIR_REASON_NOT_LOOP_READY
                and not allow_not_loop_ready
            ):
                ctx.pause(
                    reason="not-loop-ready",
                    message="repair sessions were reopened from review "
                    f"session {repair_result.review_session_id} "
                    "but the plan could not be marked loop-ready",
                    details={
                        "review_resume": review_resume.to_payload(),
                        "blockers": list(repair_result.blockers),
                        "repair_session_ids": list(repair_session_ids),
                    },
                )
                return CompletionOutcome.stop(snapshot)

            if not readiness_gate._regate_reopened_plan(
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                args=args,
                snapshot=snapshot,
                repair_session_ids=repair_session_ids,
                details={"review_resume": review_resume.to_payload()},
            ):
                return CompletionOutcome.stop(snapshot)

            running_success, running_error = readiness_gate.write_readiness_state(
                snapshot.plan_dir, "running"
            )
            with ctx.persistence.lock:
                if running_success:
                    ctx.payload["readiness_state_active"] = True
                    ctx.payload["readiness_state_current"] = "running"
                    ctx.payload["readiness_state_refreshed_at"] = clock.isoformat(
                        clock.utc_now()
                    )
                    ctx.payload["readiness_state_refresh_error"] = None
                else:
                    ctx.payload["readiness_state_refresh_error"] = running_error
                ctx.persistence.write_state(ctx.payload)
            # Execute the exact children just materialized. Once
            # they complete, the next all-complete pass evaluates
            # to ``rereview`` and creates a fresh review.
            return CompletionOutcome.again(snapshot)

        if stop_before_review:
            run_termination._stop_before_review_work(
                run_controls,
                snapshot=snapshot,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                record_existing_review_verdict=(
                    review_cycle.existing_review_verdict_recorder(
                        handoff_guards._read_review_verdict
                    )
                ),
                persistence=ctx.persistence,
            )
            return CompletionOutcome.stop(snapshot)

        review_workspace = ctx.verification_context.workspace
        review_bases: list[dict[str, Any]] = []
        if review_workspace is None:
            review_scope_delta, review_scope_fallback = (
                review_delta._compute_review_delta_context(
                    event_path=ctx.event_path,
                    plan_dir=snapshot.plan_dir,
                    cwd=cwd,
                )
            )
        else:
            try:
                review_base_snapshot = (
                    handoff_guards._capture_workspace_write_guard_snapshot(
                        workspace=review_workspace,
                        plan_dir=snapshot.plan_dir,
                        exclude=review_guard_excludes,
                        include_ignored=(review_guard_include_ignored),
                    )
                )
            except repository_evidence.RepositoryEvidenceError as error:
                unavailable = {
                    ".": {
                        "reason": "workspace-inventory-failed",
                        "error": str(error),
                    }
                }
            else:
                unavailable = workspace_evidence._workspace_snapshot_unavailable(
                    review_base_snapshot,
                    required_repository_paths=tuple(
                        repository.repository.path
                        for repository in review_workspace.repositories
                    ),
                )
            if unavailable:
                unavailable_summary = workspace_evidence._workspace_unavailable_summary(
                    unavailable,
                    workspace=review_workspace,
                )
                ctx.record_final_snapshot(snapshot)
                ctx.pause(
                    reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                    message="combined workspace review base could not "
                    "be captured safely"
                    f"{unavailable_summary}",
                    details={
                        "workspace_root": str(review_workspace.root),
                        "unavailable": unavailable,
                    },
                )
                return CompletionOutcome.stop(snapshot)
            try:
                review_bases = workspace_evidence._workspace_review_bases(
                    review_workspace, review_base_snapshot
                )
            except RunError as error:
                ctx.record_final_snapshot(snapshot)
                ctx.pause(
                    reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                    message=str(error),
                    details={"error": str(error)},
                )
                return CompletionOutcome.stop(snapshot)
            review_scope_delta, review_scope_fallback = (
                review_delta._compute_registered_review_delta_context(
                    event_path=ctx.event_path,
                    plan_dir=snapshot.plan_dir,
                    workspace=review_workspace,
                )
            )
        review_scope = (
            review_scope_delta["mode"]
            if review_scope_delta is not None
            else review_session_body.REVIEW_SCOPE_FULL
        )
        review_base_sha = (
            review_scope_delta.get("base_sha")
            if review_scope_delta is not None
            else None
        )
        try:
            review_file, review_created = (
                review_session_body.ensure_generated_review_session(
                    snapshot.plan_dir,
                    review_harness=args.review_harness,
                    delta=review_scope_delta,
                    workspace=review_workspace,
                    review_bases=review_bases,
                )
            )
        except (plans_errors.PlanError, OSError) as error:
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason="malformed-plan",
                message="could not create or reuse a generated review "
                f"session: {error}",
                details={"error": str(error)},
            )
            return CompletionOutcome.stop(snapshot)
        ctx.review_appended = True
        with ctx.persistence.lock:
            ctx.payload["review_session_id"] = review_file.session_id.value
        if review_created:
            ctx.persistence.append_event(
                ctx.event_path,
                events.REVIEW_SCOPE_EVENT,
                {
                    "run_id": ctx.run_id,
                    "session_id": review_file.session_id.value,
                    "scope": review_scope,
                    "base_sha": review_base_sha,
                    **(
                        {"repositories": review_scope_delta.get("repositories")}
                        if review_scope_delta is not None
                        and "repositories" in review_scope_delta
                        else {}
                    ),
                    "fallback_reason": review_scope_fallback,
                    "refreshed": False,
                },
            )
            ctx.persistence.append_event(
                ctx.event_path,
                events.REVIEW_SESSION_CREATED_EVENT,
                {
                    "run_id": ctx.run_id,
                    "session_id": review_file.session_id.value,
                    "session": review_file.session_id.value,
                    "review_harness": args.review_harness,
                    "review_session_path": str(review_file.path),
                    "review_output_path": (
                        ctx.payload.get("review_output_path")
                        or str(
                            review_session_contract.final_review_output_path(
                                snapshot.plan_dir
                            )
                        )
                    ),
                    "review_scope": review_scope,
                    "review_base_sha": review_base_sha,
                    **(
                        {"repositories": review_bases}
                        if review_workspace is not None
                        else {}
                    ),
                },
            )
        try:
            snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
        except plans_errors.PlanError as error:
            ctx.pause(
                reason="malformed-plan",
                message="plan could not be inspected after appending the "
                f"review session: {error}",
                details={"error": str(error)},
            )
            return CompletionOutcome.stop(snapshot)
        ctx.record_final_snapshot(snapshot)
        # Appending the review session grows the plan exactly the way
        # materializing repair children does, and reaches here under
        # the same reused readiness report -- the one that said every
        # session was complete and no turns were required. Re-gate the
        # grown plan before its turn launches, so the authority the
        # review runs under describes the plan that now exists rather
        # than the one the startup gate saw. A reused pending review
        # session was already authorized when it was created, so only
        # a fresh append is re-gated.
        if review_created and not readiness_gate._regate_reopened_plan(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            args=args,
            snapshot=snapshot,
            repair_session_ids=[],
            details={
                "review_session_id": review_file.session_id.value,
                "review_session_path": str(review_file.path),
            },
            trigger=events.REVIEW_SESSION_CREATED_EVENT,
            growth=(
                "a generated review session "
                f"({review_file.session_id.value}) was appended"
            ),
        ):
            return CompletionOutcome.stop(snapshot)
        running_success, running_error = readiness_gate.write_readiness_state(
            snapshot.plan_dir, "running"
        )
        with ctx.persistence.lock:
            if running_success:
                ctx.payload["readiness_state_active"] = True
                ctx.payload["readiness_state_current"] = "running"
                ctx.payload["readiness_state_refreshed_at"] = clock.isoformat(
                    clock.utc_now()
                )
                ctx.payload["readiness_state_refresh_error"] = None
            else:
                ctx.payload["readiness_state_refresh_error"] = running_error
            ctx.persistence.write_state(ctx.payload)
        return CompletionOutcome.again(snapshot)

    with ctx.persistence.lock:
        ctx.payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot)
        ctx.payload["reason"] = "all-complete"
    return CompletionOutcome.stop(snapshot)
