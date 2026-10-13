"""What happens to a run after a review session hands back a verdict.

A review is not an ending. Its verdict either settles the run or opens a
repair, and the repair opens another review, and the whole sequence has to be
reconstructible afterwards from the event log alone. This module owns that
sequence: the durable lineage of every reopen attempt, the continuation
projection a resumed run reads to find out where it left off, the verdict
attestation that keeps an executed review attestable even when the
bookkeeping around it failed, and :func:`_advance_review_cycle`, which turns
one verdict into one of three answers -- stop, keep going, or pause.

The policy itself is not here. Classifying a verdict against the previous
cycle's fingerprints, deciding whether a cap is exhausted, and naming the
resulting disposition are all pure decisions owned by ``review_transitions``
and ``review_continuation``. What lives here is the part that touches the
world: reading the prior evidence those decisions need, recording what was
decided, materializing the repair children a ``repairs`` disposition implies,
and re-gating the plan those children grew.

Durable records this boundary writes:

* ``payload["review_reopen"]`` and one append-only
  ``review-repair-reopened`` event per reopen attempt, written together under
  the persistence lock by :func:`_record_review_reopen`. A refused attempt is
  recorded exactly as loudly as a successful one; the lineage is what an
  operator reads to find out why a repair never appeared.
* ``payload["review_resume"]``, the continuation projection refreshed by
  :func:`_set_review_resume` at every point the plan could have moved.
* ``payload["review_cycles"]`` and ``payload["review_fix_disposition"]``,
  plus one ``review-cycle`` event, written together by
  :func:`_record_review_cycle`.
* ``payload["review_verdict"]``, ``review_output_sha256`` and
  ``review_artifact_sha256``, projected from what a review session left on
  disk by :func:`_set_existing_review_verdict` and
  :func:`_attest_executed_review`.
* ``payload["final_snapshot"]`` and the ``readiness_state_*`` trio, refreshed
  by :func:`_advance_review_cycle` as it hands the run back to the loop.

The plan directory itself is mutated on one path only: a ``repairs``
disposition materializes repair children through ``review_repair`` and then
marks the plan ``running`` through ``readiness_gate``. Nothing here opens a
file of its own.

Nothing here imports the runner. The event path, the payload, the argparse
namespace, the plan snapshot, the snapshot cache and the persistence handle
all enter as parameters. The two things it needs from elsewhere enter as
seams, each satisfied by the module that owns the work: the review verdict
reader, through the :class:`review_delta.ReviewVerdictReader` Protocol
Session 17 declared for exactly this second reader and satisfied by
``handoff_guards._read_review_verdict``, and the post-mutation plan re-read,
through :data:`PlanMutationReinspector`, satisfied by
``session_verification._reinspect_after_plan_mutation``.

Invariants that survive the move:

* **A cycle is recorded before every exit.** Every route out of
  :func:`_advance_review_cycle` -- settled, paused, or continuing -- appends
  its ``review_cycle`` record first. A run that ends without one would leave
  the next run's continuation projection reading a history with a hole in it.
* **Every projection after a plan mutation is built from the re-read.**
  Materialization writes pending children into the durable plan even on the
  paths that go on to pause, so the snapshot taken before the mutation
  describes a plan that no longer exists and is never what gets published.
* **Reopen lineage is append-only and counts the source once.** A recovered
  crash-window backfill reconstructs the pre-materialization count rather than
  counting children the evaluator has already reconciled, so the historical
  materialization total stays the number of times repairs were actually
  opened.
* **A reopen names the tree its source review read.** The record copies the
  attributed cycle's ``repo_head``, ``worktree_dirty``, ``dirty_paths`` and
  registered base vector, because the reopen supersedes that cycle as the
  newest repair evidence a later rereview scopes itself against. Attribution is
  proof, not preference: the identity and the artifact digests both have to
  agree, and a reopen that can attribute nothing names no baseline at all
  rather than borrowing a convenient earlier cycle's.
* **A missing cap is not a zero cap.** :func:`_prior_review_cap` returns
  ``None`` when the source cycle recorded no budget, including for legacy
  events that predate the field, rather than reporting a cap of zero that
  would read as a deliberate refusal to repair.
* **The terminal reason is the adjudicator's, not this module's.**
  :func:`_terminal_review_reason` asks the same cleared-review predicate the
  terminal blocker asks and only records the answer, so the two cannot
  disagree about whether a review cleared.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    clock,
    events,
    plans_errors,
    plans_models,
    plans_payloads,
    plans_snapshot,
    readiness_gate,
    review,
    review_continuation,
    review_delta,
    review_repair,
    review_transitions,
    run_inputs,
    run_persistence,
    run_termination,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------

#: Re-reads a plan this boundary itself just rewrote, returning the fallback
#: snapshot unchanged when the rewritten plan cannot be parsed at all. The
#: concrete implementation is still a runner symbol and does not move until
#: Session 23; it is the same primitive ``run_termination.SnapshotReinspector``
#: describes for the revert path, narrowed here to the two arguments the
#: materialization path has to hand.
PlanMutationReinspector = Callable[
    [plans_snapshot.PlanSnapshotCache, plans_models.PlanSnapshot],
    plans_models.PlanSnapshot,
]


def existing_review_verdict_recorder(
    read_review_verdict: review_delta.ReviewVerdictReader,
) -> run_termination.ExistingReviewVerdictRecorder:
    """Bind the verdict reader into the recorder ``run_termination`` expects.

    ``ExistingReviewVerdictRecorder`` is a two-positional-argument callable, so
    a recorder that needs its own reader seam cannot be passed directly. This
    is the one place that binding is made, mirroring
    ``run_termination.readiness_finalizer``, so the reader is threaded in a
    single spot rather than at every stop that reports the verdict it has.
    """

    def record(payload: dict[str, Any], plan_dir: Path) -> None:
        _set_existing_review_verdict(
            payload, plan_dir, read_review_verdict=read_review_verdict
        )

    return record


# ---------------------------------------------------------------------------
# Reopen lineage
# ---------------------------------------------------------------------------


def _prior_review_cap(
    event_path: Path,
    resume: review_continuation.ReviewResume,
) -> int | None:
    """Return the cap recorded by the source cycle, including legacy events."""

    for cycle in reversed(review_delta._read_prior_review_cycles(event_path)):
        cycle_number = cycle.get("cycle")
        cycle_session = cycle.get("review_session_id")
        if resume.source_cycle is not None and cycle_number != resume.source_cycle:
            continue
        if (
            resume.review_session_id is not None
            and isinstance(cycle_session, str)
            and cycle_session != resume.review_session_id
        ):
            continue
        value = cycle.get("max_review_fix_cycles")
        if type(value) is int and value >= 0:
            return value
    return None


def _review_reopen_record(
    *,
    event_path: Path,
    payload: dict[str, Any],
    resume: review_continuation.ReviewResume,
    max_review_fix_cycles: int,
    materialized: bool,
    repair_session_ids: Sequence[str] = (),
    result: str,
    error: str | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    """Build the durable lineage record for one reopen attempt/result."""

    before_count = resume.historical_materializations
    if recovered and materialized:
        # Existing children are already part of the evaluator's reconciled
        # count. Reconstruct the pre-materialization value for the crash-window
        # backfill record instead of counting the source twice.
        before_count = max(0, before_count - 1)
    after_count = before_count + (1 if materialized else 0)
    record = {
        "run_id": payload.get("run_id"),
        "source_cycle": resume.source_cycle,
        "source_review_session_id": resume.review_session_id,
        "source_disposition": resume.source_disposition,
        "previous_fingerprints": (
            list(resume.source_fingerprints)
            if resume.source_fingerprints is not None
            else None
        ),
        "current_fingerprints": (
            list(resume.current_fingerprints)
            if resume.current_fingerprints is not None
            else None
        ),
        "current_repeat_keys": (
            list(resume.current_repeat_keys)
            if resume.current_repeat_keys is not None
            else None
        ),
        "source_review_output_sha256": resume.source_review_output_sha256,
        "source_review_artifact_sha256": (resume.source_review_artifact_sha256),
        "current_review_output_sha256": resume.current_review_output_sha256,
        "current_review_artifact_sha256": (resume.current_review_artifact_sha256),
        "repair_session_ids": list(repair_session_ids),
        "materialized": materialized,
        "recovered": recovered,
        "result": result,
        "error": error,
        "historical_materializations_before": before_count,
        "historical_materializations_after": after_count,
        "minimum_required_cap": resume.minimum_required_cap,
        "before_max_review_fix_cycles": _prior_review_cap(event_path, resume),
        "after_max_review_fix_cycles": max_review_fix_cycles,
    }
    if resume.source_findings is not None:
        record["source_findings"] = [dict(item) for item in resume.source_findings]
    if resume.current_findings is not None:
        record["current_findings"] = [dict(item) for item in resume.current_findings]
    if resume.source_repositories is not None:
        record["source_repositories"] = [
            dict(item) for item in resume.source_repositories
        ]
    if resume.current_repositories is not None:
        record["current_repositories"] = [
            dict(item) for item in resume.current_repositories
        ]
    if resume.repair_groups is not None:
        record["repair_groups"] = [dict(item) for item in resume.repair_groups]
    # The reopen record becomes the newest repair evidence a later rereview
    # scopes itself against, so it has to carry the tree the *source* review
    # read. Without it the reopen supersedes a cycle record that named a base
    # and names none itself, and every review after a reopen re-reads the whole
    # tree. The baseline is copied from the attributed cycle only: when nothing
    # can be attributed the fields stay absent, and full scope is the answer.
    source = review_continuation.find_source_review_cycle(
        event_path,
        source_cycle=resume.source_cycle,
        review_session_id=resume.review_session_id,
        review_output_sha256=resume.source_review_output_sha256,
        review_artifact_sha256=resume.source_review_artifact_sha256,
    )
    if source is not None:
        record.update(review_continuation.review_baseline_evidence(source))
    return record


def _record_review_reopen(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    record: dict[str, Any],
) -> None:
    """Persist an append-only reopen attempt and expose it in run JSON."""

    with persistence.lock:
        payload["review_reopen"] = record
        persistence.append_event(
            event_path, events.REVIEW_REPAIR_REOPENED_EVENT, record
        )
        persistence.write_state(payload)


def _has_successful_review_reopen(
    event_path: Path, source_review_session_id: str | None
) -> bool:
    """Return whether durable reopen success already exists for a source."""

    if source_review_session_id is None:
        return False
    for record in review_continuation.read_review_events(event_path):
        if record.name != events.REVIEW_REPAIR_REOPENED_EVENT:
            continue
        source_id = record.payload.get("source_review_session_id")
        if (
            source_id == source_review_session_id
            and record.payload.get("materialized") is True
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Continuation projection
# ---------------------------------------------------------------------------


def _set_review_resume(
    *,
    snapshot: plans_models.PlanSnapshot,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    max_repair_group_attempts: int,
    max_review_fix_cycles: int,
    review_requested: bool,
) -> review_continuation.ReviewResume:
    """Refresh the shared continuation projection in state and run JSON."""

    resume = review_continuation.evaluate_review_continuation(
        snapshot,
        event_path,
        max_repair_group_attempts=max_repair_group_attempts,
        max_review_fix_cycles=max_review_fix_cycles,
        review_harness_requested=review_requested,
    )
    with persistence.lock:
        payload["review_resume"] = resume.to_payload()
        persistence.write_state(payload)
    return resume


# ---------------------------------------------------------------------------
# Verdict attestation
# ---------------------------------------------------------------------------


def _terminal_review_reason(payload: dict[str, Any]) -> str:
    """Apply the review lane's terminal verdict to this run's payload.

    The decision is the extracted adjudicator's, which asks the same
    cleared-review predicate the terminal blocker asks; all that happens here is
    recording it, so the two cannot disagree about whether a review cleared.
    """
    outcome = review_transitions.adjudicate_terminal_review(payload)
    if not outcome.cleared:
        payload["ok"] = False
        payload["action_required"] = outcome.action_required
    return outcome.reason


def _set_existing_review_verdict(
    payload: dict[str, Any],
    plan_dir: Path,
    *,
    require_artifact: bool = False,
    read_review_verdict: review_delta.ReviewVerdictReader,
) -> None:
    """Project the persisted verdict when a continuation stops before a turn."""

    verdict = read_review_verdict(plan_dir, require_artifact=require_artifact)
    if not verdict.get("ok"):
        return
    payload["review_verdict"] = {
        key: verdict.get(key)
        for key in (
            "status_line",
            "outcome",
            "total_findings",
            "findings_by_severity",
        )
    }


def _attest_executed_review(
    payload: dict[str, Any],
    plan_dir: Path,
    *,
    require_artifact: bool,
    read_review_verdict: review_delta.ReviewVerdictReader,
) -> None:
    """Attest a review that ran but never reached the commit that records it.

    ``review_verdict`` is stamped on the commit path, so a review session whose
    handoff fails plan inspection leaves the run reporting that no review
    happened at all — while the artifact that session wrote sits on disk.
    Reading it back here keeps the run's only independent review attestable from
    its own output rather than from bookkeeping that never got to run.

    `require_artifact` mirrors what the run itself enforced, so a verdict the
    run refused as invalid is never re-projected here as if it stood. The
    digests are recorded either way: they describe what the session left on
    disk, which stays honest evidence even when the verdict does not hold.
    """

    if payload.get("review_verdict") is None:
        _set_existing_review_verdict(
            payload,
            plan_dir,
            require_artifact=require_artifact,
            read_review_verdict=read_review_verdict,
        )
    output_sha256, artifact_sha256 = review_delta._review_artifact_digests(plan_dir)
    payload["review_output_sha256"] = output_sha256
    payload["review_artifact_sha256"] = artifact_sha256


# ---------------------------------------------------------------------------
# Cycle advance
# ---------------------------------------------------------------------------


def _record_review_cycle(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    cycle_record: dict[str, Any],
    disposition: str,
) -> None:
    """Persist one review-cycle record into state and the event log."""
    cycle_record["disposition"] = disposition
    with persistence.lock:
        payload.setdefault("review_cycles", []).append(cycle_record)
        payload["review_fix_disposition"] = disposition
        persistence.append_event(event_path, events.REVIEW_CYCLE_EVENT, cycle_record)
        persistence.write_state(payload)


def _advance_review_cycle(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    args: argparse.Namespace,
    cwd: Path,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    snapshot: plans_models.PlanSnapshot,
    turn_payload: Mapping[str, Any],
    max_review_fix_cycles: int,
    plan_cache: plans_snapshot.PlanSnapshotCache,
    reinspect: PlanMutationReinspector,
) -> tuple[str, plans_models.PlanSnapshot]:
    """Advance the repair/rereview loop after a review session completed.

    Classifies the just-enforced review verdict against the previous cycle's
    finding fingerprints, records one ``review_cycle`` state/event entry
    (cycle number, introduced/closed/repeated findings, repair session ids,
    disposition), and decides how the run continues:

    - ``("complete", snapshot)`` — stop successfully (all-clear, cycles
      disabled, cap exhausted, repeated findings, or unstructured findings).
    - ``("continue", snapshot)`` — repair children were materialized; the
      caller re-enters the loop to execute them serially and then append the
      next review session.
    - ``("paused", snapshot)`` — the run was paused (malformed repair output
      or a failed executability gate); ``run_termination._pause_run`` has already fired.
    """
    cursor = review_transitions.REVIEW_CYCLE_MACHINE.start()
    verdict = turn_payload.get("review_verdict")
    verdict = verdict if isinstance(verdict, dict) else {}
    outcome = verdict.get("outcome")
    fingerprints = review.finding_fingerprints(verdict.get("findings"))
    cycle_state = review_delta._review_cycle_state(event_path, snapshot)
    delta = review_transitions.diff_review_findings(
        fingerprints=fingerprints,
        repeat_keys=review.finding_repeat_keys(verdict.get("findings")),
        previous_fingerprints=cycle_state["previous_fingerprints"],
        previous_repeat_keys=cycle_state["previous_repeat_keys"],
    )
    cycle_record: dict[str, Any] = {
        "run_id": payload.get("run_id"),
        "cycle": cycle_state["cycle"],
        "review_session_id": session_id,
        "outcome": outcome,
        "total_findings": verdict.get("total_findings"),
        "fingerprints": list(delta.current),
        "repeat_keys": list(delta.current_keys),
        "introduced_findings": list(delta.introduced),
        "closed_findings": list(delta.closed),
        "repeated_findings": list(delta.repeated),
        "repeated_finding_keys": list(delta.repeated_keys),
        "repair_session_ids": [],
        "max_repair_group_attempts": run_inputs._max_repair_group_attempts(args),
        "max_review_fix_cycles": max_review_fix_cycles,
    }
    (
        cycle_record["review_output_sha256"],
        cycle_record["review_artifact_sha256"],
    ) = review_delta._review_artifact_digests(snapshot.plan_dir)
    repo_state = turn_payload.get("review_repo_state")
    repo_state = repo_state if isinstance(repo_state, dict) else {}
    cycle_record["repo_head"] = repo_state.get("head")
    cycle_record["worktree_dirty"] = repo_state.get("worktree_dirty")
    cycle_record["dirty_paths"] = repo_state.get("dirty_paths")
    repository_bases = turn_payload.get("review_repository_bases")
    if isinstance(repository_bases, list):
        cycle_record["repositories"] = repository_bases
        findings = verdict.get("findings")
        if isinstance(findings, list):
            cycle_record["findings"] = findings

    def record_and_complete(
        transition: review_transitions.ReviewCycleTransition,
    ) -> tuple[str, plans_models.PlanSnapshot]:
        _record_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            cycle_record=cycle_record,
            disposition=str(transition.disposition),
        )
        return "complete", snapshot

    classified = review_transitions.classify_review_cycle(
        outcome=outcome,
        structured=fingerprints is not None,
        delta=delta,
        max_review_fix_cycles=max_review_fix_cycles,
        repairs_materialized=cycle_state["repairs_materialized"],
    )
    cursor.advance(str(classified.event))
    if classified.settles:
        return record_and_complete(classified)

    planning = review_repair.plan_review_repair_sessions(
        snapshot.plan_dir,
        review_session_id=session_id,
    )
    if planning.ok and planning.reason == review_repair.REVIEW_REPAIR_REASON_OK:
        historical_attempts = review_continuation.repair_group_attempts(
            event_path, snapshot
        )
        cycle_record["repair_groups"] = [
            {
                "repair_group": group.repair_group,
                "repair_session_id": group.proposed_child_id.value,
                "finding_ids": list(group.finding_ids),
                **(
                    {
                        "read_repositories": list(group.read_repositories),
                        "write_repositories": list(group.write_repositories),
                        "write_repository_sources": dict(
                            group.write_repository_sources
                        ),
                    }
                    if "repositories" in cycle_record
                    else {}
                ),
            }
            for group in planning.repair_groups
        ]
        exhausted_group = review_transitions.first_exhausted_repair_group(
            repair_groups=planning.repair_groups,
            historical_attempts=historical_attempts,
            max_repair_group_attempts=cycle_record["max_repair_group_attempts"],
        )
        if exhausted_group is not None:
            cycle_record["exhausted_repair_group"] = exhausted_group.repair_group
            cycle_record["exhausted_repair_group_attempts"] = historical_attempts[
                exhausted_group.repair_group
            ]
            settled = review_transitions.settle_group_exhausted()
            cursor.advance(str(settled.event))
            return record_and_complete(settled)

    repair_result = review_repair.materialize_review_repair_sessions(
        snapshot.plan_dir,
        review_session_id=session_id,
        cwd=cwd,
        state_dir=args.state_dir,
        hold_lock=False,
        allow_not_loop_ready=readiness_gate._allow_not_loop_ready(args),
    )
    materialized_repair_groups = [
        {
            "repair_group": child.repair_group,
            "repair_session_id": child.session_id.value,
            "finding_ids": list(child.finding_ids),
            **(
                {
                    "read_repositories": list(child.read_repositories),
                    "write_repositories": list(child.write_repositories),
                }
                if "repositories" in cycle_record
                else {}
            ),
        }
        for child in repair_result.children
    ]
    if not materialized_repair_groups:
        # ASI-147: a cycle that planned repairs and materialized none is not a
        # cycle that planned none. The pre-authoring gate refused the rendered
        # children, or the executability gate withdrew them after authoring
        # (ASI-136); either way the empty materialized list would overwrite the
        # planned one recorded above and leave the pause detail as the only
        # durable trace of repairs this cycle actually specified.
        planned_repair_groups = cycle_record.get("repair_groups")
        if planned_repair_groups:
            cycle_record["withdrawn_repair_groups"] = planned_repair_groups
            cycle_record["withdrawn_repair_session_ids"] = [
                str(group["repair_session_id"]) for group in planned_repair_groups
            ]
            cycle_record["repair_children_rolled_back"] = repair_result.rolled_back
    cycle_record["repair_session_ids"] = [
        child.session_id.value for child in repair_result.children
    ]
    cycle_record["repair_groups"] = materialized_repair_groups
    # Materialization writes pending repair sessions into the durable plan
    # even on the paths that go on to pause, so every projection from here
    # down is built from the re-read rather than from the snapshot taken
    # before the runner mutated the plan.
    materialized = reinspect(plan_cache, snapshot)

    settlement = review_transitions.classify_materialization(
        ok=repair_result.ok,
        reason=repair_result.reason,
        non_bypassable_blockers=repair_result.non_bypassable_blockers,
        allow_not_loop_ready=readiness_gate._allow_not_loop_ready(args),
    )
    cursor.advance(str(settlement.event))

    if settlement.disposition == review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED:
        _record_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            cycle_record=cycle_record,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED,
        )
        with persistence.lock:
            payload["final_snapshot"] = plans_payloads.snapshot_payload(materialized)
        run_termination._pause_run(
            event_path,
            payload,
            reason=str(settlement.pause_reason),
            message=(
                "review repair materialization failed after "
                f"{session_label}: {repair_result.error}"
            ),
            details={
                **session_details,
                "error": repair_result.error,
                "detail": repair_result.detail,
                "cycle": cycle_record["cycle"],
            },
            persistence=persistence,
        )
        return "paused", materialized

    if settlement.settles:
        return record_and_complete(settlement)

    if (
        settlement.disposition
        == review_continuation.REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY
    ):
        _record_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            cycle_record=cycle_record,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
        )
        with persistence.lock:
            payload["final_snapshot"] = plans_payloads.snapshot_payload(materialized)
        run_termination._pause_run(
            event_path,
            payload,
            reason=str(settlement.pause_reason),
            message=(
                (
                    "repair sessions were materialized after "
                    f"{session_label} but the plan could not be marked "
                    "loop-ready"
                )
                if cycle_record["repair_session_ids"]
                else (
                    "no review repair children survived materialization "
                    f"after {session_label}: {repair_result.error}"
                )
            ),
            details={
                **session_details,
                "blockers": list(repair_result.blockers),
                "blocker": list(repair_result.non_bypassable_blockers),
                "repair_session_ids": cycle_record["repair_session_ids"],
                "cycle": cycle_record["cycle"],
                # ASI-136: children that failed the gate are withdrawn, so the
                # pause has to say which plan an operator is looking at.
                "repair_children_rolled_back": repair_result.rolled_back,
                **(
                    {"repair_rollback": repair_result.rollback}
                    if repair_result.rollback is not None
                    else {}
                ),
            },
            persistence=persistence,
        )
        return "paused", materialized

    _record_review_cycle(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        cycle_record=cycle_record,
        disposition=str(settlement.disposition),
    )
    try:
        refreshed = plan_cache.get_fresh(snapshot.plan_dir)
    except plans_errors.PlanError as error:
        with persistence.lock:
            payload["final_snapshot"] = plans_payloads.snapshot_payload(materialized)
        run_termination._pause_run(
            event_path,
            payload,
            reason="malformed-plan",
            message=(
                "plan could not be inspected after materializing repair "
                f"sessions: {error}"
            ),
            details={**session_details, "error": str(error)},
            persistence=persistence,
        )
        return "paused", materialized
    if not readiness_gate._regate_reopened_plan(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        args=args,
        snapshot=refreshed,
        repair_session_ids=cycle_record["repair_session_ids"],
        details={**session_details, "cycle": cycle_record["cycle"]},
    ):
        return "paused", refreshed
    running_success, running_error = readiness_gate.write_readiness_state(
        refreshed.plan_dir, "running"
    )
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(refreshed)
        if running_success:
            payload["readiness_state_active"] = True
            payload["readiness_state_current"] = "running"
            payload["readiness_state_refreshed_at"] = clock.isoformat(clock.utc_now())
            payload["readiness_state_refresh_error"] = None
        else:
            payload["readiness_state_refresh_error"] = running_error
        persistence.write_state(payload)
    return "continue", refreshed
