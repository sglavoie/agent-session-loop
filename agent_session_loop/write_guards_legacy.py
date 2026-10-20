"""The legacy single-repository implementation write guard.

:func:`_enforce_legacy_implementation_write_guard` refuses a legacy
single-repository turn whose scope or history evidence could not be captured
safely. It reads its verdict out of ``workspace_evidence`` and performs the same
five-step refusal as ``write_guards._enforce_workspace_write_guard`` (revert,
re-read, record under the lock, ``turn-rejected``, pause); see that module for
the shared invariants. The handoff-note appender and the post-revert plan re-read
enter as required keyword-only seams. Nothing here imports the runner.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    external_write_evidence,
    handoff_guards,
    plans_models,
    plans_payloads,
    plans_snapshot,
    preflight_footprint,
    repository_evidence,
    run_inputs,
    run_persistence,
    run_termination,
    turn_execution_commit,
    workspace_evidence,
)


def _enforce_legacy_implementation_write_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    cwd: Path,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    snapshot_before: repository_evidence.WorkspaceSnapshot,
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans_snapshot.PlanSnapshotCache,
    append_handoff_note: handoff_guards.HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
    state_dir_arg: str | None,
    run_dir: Path | None = None,
    external_digests_before: Mapping[str, Mapping[str, Any]] | None = None,
    turn_started_ns: int | None = None,
) -> bool:
    """Fail closed around every legacy implementation turn, in every mode."""
    snapshot_after = handoff_guards._capture_legacy_implementation_snapshot(
        cwd=cwd,
        plan_dir=before.plan_dir,
        exclude=exclude,
        include_ignored=include_ignored,
    )
    comparison = repository_evidence.compare_workspace_snapshots(
        snapshot_before, snapshot_after
    )
    workspace_evidence._annotate_history_transitions(
        comparison,
        repository_roots={".": cwd},
    )
    artifact_paths = (
        *run_inputs._task_packet_artifact_paths(before.plan_dir, session_id, None),
        paths["last_message"],
    )
    evidence = workspace_evidence._legacy_implementation_write_guard_evidence(
        cwd=cwd,
        comparison=comparison,
        exclude=exclude,
        include_ignored=include_ignored,
        unobserved=handoff_guards._legacy_unobserved_repositories(
            cwd=cwd,
            artifact_paths=artifact_paths,
            exempt_roots=run_inputs._runner_owned_roots(
                before.plan_dir, state_dir_arg, run_dir=run_dir
            ),
            provenance_roots=preflight_footprint.plan_provenance_exempt_roots(
                before.plan_dir
            ),
        ),
        external_writes=external_write_evidence._turn_external_write_evidence(
            plan_dir=before.plan_dir,
            artifact_paths=artifact_paths,
            root=cwd,
            workspace_root=cwd,
            repository_roots=[cwd],
            state_dir_arg=state_dir_arg,
            run_dir=run_dir,
            digests_before=external_digests_before,
            turn_started_ns=turn_started_ns,
            authorized_roots=tuple(
                Path(value)
                for value in payload.get("run_config", {}).get(
                    "external_write_root", ()
                )
            ),
        ),
    )
    if evidence["passed"]:
        with persistence.lock:
            turn_payload["workspace_write_guard"] = evidence
            durability.write_json_file(paths["result"], turn_payload)
        return False

    reason = events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
    explanation = workspace_evidence._legacy_write_guard_failure_message(evidence)
    failure_note = f"implementation write guard unavailable; {explanation}"
    revert_target = after if after is not None else before
    reverted = handoff_guards._revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=append_handoff_note,
    )
    inspected = reinspect(before.plan_dir, revert_target, reverted, plan_cache)
    guard_record = {
        **evidence,
        "session_id": session_id,
        "reason": reason,
        "phase": "after",
        "reverted_to_pending": handoff_guards._reopened(reverted),
    }
    with persistence.lock:
        payload["final_snapshot"] = plans_payloads.snapshot_payload(inspected)
        turn_payload["workspace_write_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution_commit.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="legacy-implementation-write-guard",
        reason=reason,
        revert=reverted,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=(
            f"implementation write guard unavailable after {session_label}; "
            f"{explanation}"
        ),
        details={
            **session_details,
            "workspace_write_guard": guard_record,
        },
        persistence=persistence,
    )
    return True
