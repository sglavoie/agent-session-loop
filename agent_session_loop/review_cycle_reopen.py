"""Reopen lineage and continuation projection for the review cycle.

Owns the append-only ``review-repair-reopened`` records and the
``review_resume`` projection written into run state."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_models,
    review_continuation,
    review_delta,
    run_persistence,
)

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
