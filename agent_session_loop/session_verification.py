"""The gate between a finished handoff and the record that it was earned.

A turn ends with a session file claiming ``complete``. Nothing about that claim
is trustworthy until this boundary has run the session's own ``Verify`` recipe —
or, for a review session, adjudicated the verdict it produced — and only a claim
that survives here is allowed to reach the progress journal, the derived status
ledgers, and the commit. That is the whole reason this is a boundary rather than
two stretches of ``run_locked``: the loop decides *when* to gate, and this
module decides *whether the turn stands* and performs the identical durable
bookkeeping either way.

It owns three things:

* **The review verdict gate.** :func:`_enforce_review_verdict` reads the review
  session's verdict (and, at or above ``STRUCTURED_REVIEW_MIN_VERSION``, its
  ``final-review.json`` artifact), hands it to
  ``review_transitions.adjudicate_review_verdict``, and returns ``"pass"``,
  ``"retry"`` — the loop re-selects and re-runs the same review session — or
  ``"pause"`` once the repair budget is spent or an operator stop must win.
* **The verification gate.** :func:`_enforce_session_verification` drives the
  ``verification_transitions.VERIFICATION_MACHINE`` states beside the effects
  each one earns: schedule the contract, open the live record, run the checks
  through an observer, grade the result, and either continue or refuse. The
  repair budget is adjudicated from the run's own prior ``verify-failed``
  events, so an identical failure cannot loop and a changed one stays eligible
  under the cap.
* **The post-mutation plan re-read.** :func:`_reinspect_after_plan_mutation`
  re-reads a plan the runner itself just rewrote, and
  :func:`_reinspect_after_revert` is the revert-aware wrapper over it that every
  guard in the package calls. Both live here because the reverts they follow are
  this boundary's own, and because a projection still built from the
  pre-mutation snapshot describes a plan that no longer exists.

Durable records this boundary writes: the ``review_verdict`` and
``verification`` blocks of the turn result, every one of them written under the
run's mutex; the ``final_snapshot`` refreshed from the re-read plan after each
revert; one ``review-verdict-retry`` event per accepted retry; one process-sweep
event per verification check; and, on refusal, one ``turn-rejected`` plus one
``run-paused``. It rewrites no session file itself — the revert is delegated to
``handoff_guards._revert_session_status_to_pending``, which owns that write at
its own boundary. ``tests/test_asl_session_verification.py`` pins the write
sites per function, so a fourth writer appearing here is a test failure rather
than a review catch.

Nothing here imports the runner. The event path, the payload, the persistence
handle, the progress reporter, the plan snapshots, the snapshot cache, the
verification context and the turn's paths all enter as parameters, and the one
collaborator the caller supplies enters as a required keyword-only seam: the
handoff-note appender, through the
:class:`handoff_guards.HandoffNoteAppender` Protocol
``handoff_correction._append_handoff_note`` satisfies. It stays a seam rather
than a direct import because ``handoff_correction`` imports *this* module for
the post-revert re-read, so the edge only runs one way. It
is never defaulted, so no refusal path can silently skip the note that tells the
next turn why its predecessor was reopened.

Invariants that survive the move:

* **Verify, then journal, then ledgers.** The loop calls
  :func:`_enforce_session_verification` strictly before
  ``handoff_guards._append_progress_entry`` and before
  ``plans_status_tables.sync_derived_status_ledgers``. Both halves fail silently —
  a journal row appended before verification looks exactly like one appended
  after it — so the ordering is asserted against the loop's source rather than assumed.
* **A refusal reverts, re-reads, records, and pauses, in that order.** The
  session goes back to ``pending`` with a note naming the failure, the plan is
  re-read so ``final_snapshot`` describes the plan as the guard left it, the
  turn result and ``turn-rejected`` record the refusal, and only then does the
  run pause. A refusal that stopped short of any of them would leave a turn the
  run refused looking like a turn it accepted.
* **The state machine runs even on the paths that refuse.** Every exit advances
  the cursor first, so an out-of-order step raises here rather than writing a
  durable record describing a lifecycle this session never had.
* **A repair budget is read before its own failure is appended.** Both gates
  read the prior retries or verify-failures for the session *before* recording
  the current one, so the attempt count describes the history the failure is
  being judged against rather than including itself.
* **Waived is not passed.** A loop-ready run that accepts a session with no
  contract records that nothing was enforced and continues; it never writes a
  verification block claiming checks ran.
* **The pre-verification snapshot is published.** ``final_snapshot`` advances to
  the post-turn view before the checks start, so operator tools see the
  in-flight phase as verifying rather than idle, while completion bookkeeping
  still waits for the pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    contracts,
    durability,
    events,
    handoff_guards,
    live_verification,
    plans_errors,
    plans_models,
    plans_payloads,
    plans_snapshot,
    review_delta,
    review_session_contract,
    review_transitions,
    run_persistence,
    run_reporting,
    run_termination,
    turn_execution_commit,
    verification_models,
    verification_run,
    verification_transitions,
)


def _enforce_review_verdict(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: handoff_guards.HandoffNoteAppender,
    require_artifact: bool = False,
    stop_after_current_pending: bool = False,
) -> tuple[str, plans_models.PlanSnapshot | None]:
    """Validate and persist the final review verdict.

    ``require_artifact=True`` (review sessions at or above
    :data:`STRUCTURED_REVIEW_MIN_VERSION`) additionally requires a valid,
    consistent ``final-review.json``; v1 review sessions stay
    Markdown-compatible.

    Returns ``("pass", None)`` for a valid verdict. An invalid verdict always
    reverts the review session to ``pending`` with the exact validation error
    as a Handoff note, then either returns ``("retry", snapshot)`` — the caller
    adopts the snapshot and ``continue``s, so the main loop re-selects and
    re-runs the same review session — or ``("pause", snapshot)`` once the
    repair is exhausted or ``stop_after_current_pending`` says an operator
    stop must win over another turn (the caller must ``break``).
    """
    review_path = review_session_contract.final_review_output_path(before.plan_dir)
    verdict = handoff_guards._read_review_verdict(
        before.plan_dir, require_artifact=require_artifact
    )
    cursor = review_transitions.REVIEW_VERDICT_MACHINE.start()
    # Fingerprint this rejection and compare against the retries already
    # recorded for this session in the current review cycle (read before the
    # new retry event is appended). An identical rejection cannot loop, and the
    # attempt cap bounds a reviewer that keeps producing *different* malformed
    # artifacts.
    graded = review_transitions.adjudicate_review_verdict(
        verdict=verdict,
        priors=review_transitions.read_prior_review_verdict_retries(
            event_path, session_id
        ),
        stop_after_current_pending=stop_after_current_pending,
    )
    cursor.advance(graded.event)
    if graded.accepted:
        with persistence.lock:
            turn_payload["review_verdict"] = {
                "passed": True,
                **{key: value for key, value in verdict.items() if key != "ok"},
            }
            durability.write_json_file(paths["result"], turn_payload)
        return ("pass", None)

    error = verdict.get("error", "invalid review verdict")
    failure_note = f"review verdict validation failed: {error}"
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=failure_note,
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = _reinspect_after_revert(
        before.plan_dir, after, reverted, plan_cache
    )

    repair_payload = graded.to_payload()
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot_after)
        payload["review_verdict"] = None
        turn_payload["review_verdict"] = {
            "passed": False,
            "reason": "invalid-review-verdict",
            **{
                key: value
                for key, value in verdict.items()
                if key not in ("ok", "reason")
            },
            "failure_reason": verdict.get("reason"),
            **repair_payload,
        }
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="review-verdict",
        reason="invalid-review-verdict",
        revert=reverted,
        detail={"failure_reason": verdict.get("reason"), "attempt": graded.attempt},
    )

    if graded.action == "retry":
        persistence.append_event(
            event_path,
            events.REVIEW_VERDICT_RETRY_EVENT,
            {
                "run_id": payload.get("run_id"),
                **plans_payloads.session_identity_payload(session_id),
                "attempt": graded.attempt,
                "max_attempts": graded.max_attempts,
                "fingerprint": graded.fingerprint,
                "failure_reason": verdict.get("reason"),
                "error": str(error),
            },
        )
        cursor.advance("resubmit")
        return ("retry", snapshot_after)

    details = {
        **session_details,
        "review_output_path": str(review_path),
        "error": str(error),
        "failure_reason": verdict.get("reason"),
        **repair_payload,
    }
    if "review_artifact_path" in verdict:
        details["review_artifact_path"] = verdict["review_artifact_path"]
    run_termination._pause_run(
        event_path,
        payload,
        reason="verify-failed",
        message=(f"review verdict validation failed for {session_label}: {error}"),
        details=details,
        persistence=persistence,
    )
    return ("pause", snapshot_after)


def _enforce_session_verification(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    progress: run_reporting._ProgressReporter,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot,
    cwd: Path,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    allow_not_loop_ready: bool,
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: handoff_guards.HandoffNoteAppender,
    run_id: str | None,
    turn_index: int,
    total_sessions: int | None,
    heartbeat_interval: float = (
        live_verification.VERIFICATION_HEARTBEAT_INTERVAL_SECONDS
    ),
    verification_context: verification_models.VerificationContext | None = None,
    review_broad_failure_deferral: bool = False,
    require_review_artifact: bool = False,
    generated_review_session: bool = False,
) -> bool:
    """Run the session's `Verify` recipe and pause on failure or contract problems.

    The lane's states — scheduled, running, and the settled passed / deferred /
    failed / unenforced / refused — are declared in
    :data:`verification_transitions.VERIFICATION_MACHINE` and driven here beside
    the effects each one earns, so an out-of-order step raises rather than
    writing a durable record describing a lifecycle this session never had.

    Returns ``True`` when the run should pause (the caller must ``break``);
    ``False`` when verification passed and the loop should continue.
    """
    cursor = verification_transitions.VERIFICATION_MACHINE.start()
    live: live_verification._LiveVerification | None = None

    def refuse(
        refusal: verification_transitions.VerificationRefusal,
    ) -> bool:
        """Apply a refusal's effects: close the live record, revert, pause."""

        if live is not None and refusal.live_finish_reason is not None:
            live.finish(
                passed=False,
                passed_count=0,
                reason=refusal.live_finish_reason,
                error=refusal.error,
            )
        reverted = handoff_guards._revert_session_status_to_pending(
            after,
            session_id,
            failure_note=refusal.failure_note,
            append_handoff_note=append_handoff_note,
        )
        snapshot_after = _reinspect_after_revert(
            before.plan_dir, after, reverted, plan_cache
        )
        with persistence.lock:
            payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot_after)
            turn_payload["verification"] = refusal.turn_verification(session_id)
            durability.write_json_file(paths["result"], turn_payload)
        turn_execution_commit.record_turn_rejected(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="session-verification",
            reason=refusal.reason,
            revert=reverted,
            detail=refusal.guard_detail,
        )
        run_termination._pause_run(
            event_path,
            payload,
            reason=refusal.reason,
            message=refusal.message,
            details=refusal.pause_details(session_details),
            persistence=persistence,
        )
        return True

    contract: contracts.CompletionContract | None
    contract_error: str | None = None
    try:
        contract = contracts.read_completion_contract(before.plan_dir, session_id)
    except plans_errors.PlanError as error:
        contract, contract_error = None, str(error)

    schedule = verification_transitions.schedule_verification(
        contract=contract,
        contract_error=contract_error,
        allow_not_loop_ready=allow_not_loop_ready,
        session_label=session_label,
    )
    cursor.advance(schedule.event)
    if not schedule.runs:
        if schedule.refusal is not None:
            return refuse(schedule.refusal)
        # Waived: a loop-ready run accepted a session with no contract, so the
        # turn records that nothing was enforced and the loop continues.
        with persistence.lock:
            turn_payload["verification"] = schedule.turn_verification(session_id)
            durability.write_json_file(paths["result"], turn_payload)
        return False

    # `schedule.runs` is `state == "contract-ready"`, which
    # `schedule_verification` returns on exactly one branch: the one guarded by
    # `contract is not None`. The invariant lives across a module boundary, so
    # it is restated here to narrow the contract every call below requires.
    assert contract is not None

    # Persist `final_snapshot` to the post-turn inspected snapshot before
    # verification starts. Completion bookkeeping (progress.md append and
    # `completed_sessions`) still waits until verification passes, but the
    # snapshot itself is the post-turn view either way so operator tools see
    # the in-flight phase as "verifying" rather than "idle".
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(after)

    if generated_review_session:
        review_workspace = (
            verification_context.workspace if verification_context is not None else None
        )
        contract = review_session_contract._authenticated_generated_review_contract(
            contract,
            plan_dir=before.plan_dir,
            repository_aliases=(
                () if review_workspace is None else review_workspace.registry.aliases
            ),
        )

    contract_checks = contract.verify.checks
    check_count = len(contract_checks)
    live = live_verification._LiveVerification(
        event_path=event_path,
        payload=payload,
        progress=progress,
        persistence=persistence,
        run_id=run_id,
        turn_index=turn_index,
        session_id=session_id,
        session_label=session_label,
        total_sessions=total_sessions,
        check_count=check_count,
        check_names=tuple(check.name for check in contract_checks),
        check_kinds=tuple(check.kind for check in contract_checks),
        check_repos=tuple(check.repo for check in contract_checks),
        parallel=contract.verify.parallel,
        heartbeat_interval=heartbeat_interval,
    )

    def _on_check_started(event: verification_models.CheckStartedEvent) -> None:
        live.check_started(
            check_index=event.check_index,
            name=event.name,
            kind=event.kind,
            argv=event.argv,
            cwd=event.cwd,
            repo=event.repo,
        )

    def _on_check_finished(event: verification_models.CheckFinishedEvent) -> None:
        live.check_finished(
            check_index=event.check_index,
            name=event.name,
            kind=event.kind,
            role=event.role,
            status=event.result.status,
            duration_seconds=event.result.duration_seconds,
            failure_reason=event.result.failure_reason,
            returncode=event.result.returncode,
            repo=event.repo,
            failure_diagnostic=event.result.failure_diagnostic,
        )

    def _on_check_skipped(event: verification_models.CheckSkippedEvent) -> None:
        live.check_skipped(
            check_index=event.check_index,
            name=event.name,
            kind=event.kind,
            on_failure_of=event.on_failure_of,
            reason=event.reason,
            repo=event.repo,
        )

    observer = verification_models.VerificationObserver(
        on_check_started=_on_check_started,
        on_check_finished=_on_check_finished,
        on_check_skipped=_on_check_skipped,
    )

    # Retain failed command stdout/stderr under the turn's artifact directory so
    # large output stays recoverable off to the side while result.json keeps
    # only bounded previews.
    verification_artifact_dir = paths["artifact_dir"] / "verification"

    cursor.advance("start")
    live.start()
    try:
        verification_kwargs: dict[str, Any] = {
            "observer": observer,
            "artifact_dir": verification_artifact_dir,
        }
        if verification_context is None:
            verification_kwargs["repo_root"] = cwd
        else:
            verification_kwargs["context"] = verification_context
        result = verification_run.run_verification(contract, **verification_kwargs)
    except KeyboardInterrupt:
        cursor.advance("run-refused")
        return refuse(
            verification_transitions.refuse_interrupted_verification(
                session_label=session_label
            )
        )
    except OSError as error:
        cursor.advance("run-refused")
        return refuse(
            verification_transitions.refuse_unavailable_verification(
                session_label=session_label, error=str(error)
            )
        )

    graded = verification_transitions.adjudicate_verification(
        result=result,
        contract=contract,
        deferral=(
            review_delta._review_broad_failure_acceptance(
                result,
                plan_dir=before.plan_dir,
                require_artifact=require_review_artifact,
                read_review_verdict=handoff_guards._read_review_verdict,
            )
            if review_broad_failure_deferral and not result.passed
            else None
        ),
    )
    cursor.advance(graded.event)
    details = graded.details
    live.finish(
        passed=graded.passed,
        passed_count=graded.passed_count,
        skipped_count=graded.skipped_count,
        executed_count=graded.executed_count,
        gate_count=graded.gate_count,
        diagnostic_count=graded.diagnostic_count,
        reason=graded.reason,
        acceptance=graded.acceptance,
    )

    with persistence.lock:
        turn_payload["verification"] = graded.turn_verification(session_id)
        durability.write_json_file(paths["result"], turn_payload)
        for check in result.checks:
            run_persistence._append_process_sweep_event(
                event_path=event_path,
                persistence=persistence,
                report=check.sweep,
                phase="verification",
                identity={
                    "run_id": run_id,
                    "turn_index": turn_index,
                    **plans_payloads.session_identity_payload(session_id),
                    "check": check.name,
                },
            )

    if graded.continues:
        return False

    summary = str(graded.summary)
    reverted = handoff_guards._revert_session_status_to_pending(
        after,
        session_id,
        failure_note=f"verification failed: {summary}",
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = _reinspect_after_revert(
        before.plan_dir, after, reverted, plan_cache
    )

    # Fingerprint this failure and compare against prior verify-failed events
    # for the same session (read before this pause is appended). The repair is
    # exhausted when the fingerprint repeats a prior failure (an identical
    # failure cannot loop) or when the total number of verify-failures exceeds
    # the attempt cap. The autopilot honors ``verification_repair_exhausted``
    # and stops relaunching; a changed fingerprint under the cap stays eligible.
    repair = verification_transitions.adjudicate_verification_repair(
        details=details,
        budget=verification_transitions.verification_repair_budget(
            event_path, session_id
        ),
    )
    repair_payload = repair.to_payload()
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot_after)
        turn_payload["verification"].update(repair_payload)
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="session-verification",
        reason="verify-failed",
        revert=reverted,
        detail={"summary": summary, "fingerprint": repair.fingerprint},
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason="verify-failed",
        message=(f"verification failed for {session_label}: {summary}"),
        details={**session_details, **details, **repair_payload},
        persistence=persistence,
    )
    return True


def _reinspect_after_revert(
    plan_dir: Path,
    fallback: plans_models.PlanSnapshot,
    reverted_path: object | None,
    plan_cache: plans_snapshot.PlanSnapshotCache,
) -> plans_models.PlanSnapshot:
    if reverted_path is None:
        return fallback
    return _reinspect_after_plan_mutation(plan_cache, fallback)


def _reinspect_after_plan_mutation(
    plan_cache: plans_snapshot.PlanSnapshotCache,
    fallback: plans_models.PlanSnapshot,
) -> plans_models.PlanSnapshot:
    """Re-read a plan the runner itself just rewrote.

    Reverting a session or materializing repair children changes the durable
    plan, so a projection still built from the pre-mutation snapshot describes
    a plan that no longer exists. The pre-mutation snapshot is kept only when
    the rewritten plan cannot be parsed at all, which is a separate failure the
    caller already reports.
    """
    try:
        return plan_cache.get_fresh(fallback.plan_dir)
    except plans_errors.PlanError:
        return fallback
