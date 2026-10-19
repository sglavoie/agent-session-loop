"""Detect repository mutation during a read-only review turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    plans_models,
    plans_payloads,
    plans_snapshot,
    repository_evidence,
    review,
    review_guard,
    run_persistence,
    run_termination,
    turn_execution_commit,
)
from .handoff_guard_sessions import (
    HandoffNoteAppender,
    _reopened,
    _revert_session_status_to_pending,
)


def _read_review_verdict(
    plan_dir: Path, *, require_artifact: bool = False
) -> dict[str, Any]:
    return review.load_review_verdict(plan_dir, require_artifact=require_artifact)


def _enforce_review_mutation_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    cwd: Path,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    fingerprint_before: dict[str, Any],
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
) -> bool:
    """Compare the post-turn fingerprint against `fingerprint_before`.

    Runs ahead of every result branch, so the turn's own outcome never
    suppresses the verdict: a retryable pause such as ``timeout`` would
    otherwise relaunch against the mutated tree and adopt it as the new
    baseline. `after` is ``None`` when the post-turn plan could not be
    inspected; the revert then targets the pre-turn snapshot, which names the
    same session file. Returns ``True`` when the run must pause (the caller
    must ``break``).
    """
    fingerprint_after = review_guard._capture_review_repo_fingerprint(
        cwd,
        before.plan_dir,
        exclude=exclude,
        include_ignored=include_ignored,
    )
    excluded = list(exclude)
    unavailable = review_guard._review_guard_unavailable_detail(fingerprint_after)
    # A remote-tracking ref move (a background push or fetch, or the session
    # publishing a commit it already made) changes no content this guard
    # protects, so it is recorded as drift and kept out of the comparison
    # (ASI-161).
    drift = (
        repository_evidence.remote_ref_drift(fingerprint_before, fingerprint_after)
        if unavailable is None
        else []
    )
    if unavailable is not None:
        reason = events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE
        failure_note = f"review mutation guard unavailable: {unavailable['reason']}"
        message = (
            "review mutation guard unavailable after "
            f"{session_label}: {unavailable['reason']}"
        )
        evidence: dict[str, Any] = {
            "phase": "after",
            "unavailable": unavailable,
            "excluded": excluded,
            "include_ignored": include_ignored,
        }
    elif repository_evidence.without_remote_tracking_refs(
        fingerprint_after
    ) != repository_evidence.without_remote_tracking_refs(fingerprint_before):
        reason = "review-mutated-worktree"
        changed_paths = repository_evidence.split_remote_ref_changes(
            review_guard._review_guard_changed_paths(
                fingerprint_before, fingerprint_after
            )
        )
        failure_note = (
            "review mutation guard failed: repository content changed "
            "outside the plan directory"
        )
        message = (
            f"review session {session_label} mutated repository content "
            "outside the plan directory"
            f"{review_guard._review_guard_changed_summary(changed_paths)}"
        )
        evidence = {
            "fingerprint_before": fingerprint_before,
            "fingerprint_after": fingerprint_after,
            "changed_paths": changed_paths,
            "excluded": excluded,
            "include_ignored": include_ignored,
        }
        if drift:
            evidence["remote_ref_drift"] = drift
    else:
        with persistence.lock:
            turn_payload["review_mutation_guard"] = {
                "passed": True,
                "excluded": excluded,
                "include_ignored": include_ignored,
                **({"remote_ref_drift": drift} if drift else {}),
            }
            repo_state = review_guard._review_repo_state(fingerprint_after)
            if repo_state is not None:
                turn_payload["review_repo_state"] = repo_state
            durability.write_json_file(paths["result"], turn_payload)
        return False

    # Leaving the review complete would make an unguarded review permanent
    # across a resume, so a failed guard always puts it back to pending.
    revert_target = after if after is not None else before
    reverted = _revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=append_handoff_note,
    )
    snapshot_after = reinspect(before.plan_dir, revert_target, reverted, plan_cache)
    guard_record = {
        "passed": False,
        "reason": reason,
        "reverted_to_pending": _reopened(reverted),
        **evidence,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(snapshot_after)
        turn_payload["review_mutation_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="review-mutation-guard",
        reason=reason,
        revert=reverted,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={
            **session_details,
            **evidence,
            "review_mutation_guard": guard_record,
        },
        persistence=persistence,
    )
    return True
