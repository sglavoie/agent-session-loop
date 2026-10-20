"""Whether a finished turn is allowed to stand, and what happens when it is not.

Every function that judges evidence lives elsewhere. This module owns the layer
above them: it takes a verdict the evidence modules produced, decides whether the
turn survives it, and — when it does not — performs the identical five-step
refusal that makes the refusal durable. A guard that only computed a verdict
would leave the run free to publish the turn anyway; a guard that computed one
inline in the loop would make each refusal a fresh transcription of the same five
steps. Both are why this is a boundary rather than a stretch of ``run_locked``.

It owns the workspace write guard, :func:`_enforce_workspace_write_guard`, which
refuses a registered implementation turn that changed a repository outside its
declared write aliases. Its siblings own the rest of the layer and share its
five-step refusal and invariants: ``write_guards_legacy`` (the legacy
single-repository guard), ``write_guards_protected`` (the protected-path guard)
and ``write_guards_repair`` (the repair-continuation gate).

Durable records the layer writes: the ``workspace_write_guard`` and
``protected_path_guard`` verdicts written to the turn result under the run's
mutex, the ``final_snapshot`` refreshed from the re-read plan after every revert,
and one ``turn-rejected`` plus one ``run-paused`` event per refusal. It rewrites
no session file itself — the revert is delegated to
``handoff_guards._revert_session_status_to_pending`` and the scope widening to
``workspace_evidence._regenerated_repair_child_write_scope``, both of which own
that write at their own boundary. ``tests/test_asl_write_guards.py`` pins the
result-write sites per function, so a fourth writer appearing here is a test
failure rather than a review catch.

Nothing here imports the runner. The event path, the payload, the persistence
handle, the plan snapshots, the snapshot cache, the resolved workspace, the guard
controller and the turn-start evidence all enter as parameters, and the two
collaborators the caller supplies enter as required keyword-only
seams: the handoff-note appender, through the
:class:`handoff_guards.HandoffNoteAppender` Protocol
``handoff_correction._append_handoff_note`` satisfies, and
the post-revert plan re-read, through the
:data:`run_termination.SnapshotReinspector` alias
``session_verification._reinspect_after_revert`` satisfies.
Neither is ever defaulted, so no refusal path can silently skip the note or the
re-read.

Invariants that survive the move:

* **A refusal is five steps, always in this order.** Revert the session to
  ``pending`` with a note naming the guard, re-read the plan, write the guard
  record and the refreshed ``final_snapshot`` to the turn result under the lock,
  record ``turn-rejected``, then pause the run. A refusal that stopped short of
  any of them would leave a turn the run refused looking like a turn it accepted.
* **Filesystem changes are always retained.** The run cannot know whether the
  change was the valuable part of the turn, so a violation withdraws the
  *claim* — the session status — and never the work. Retention is not the same
  question as where the work is kept, and elsewhere the run now answers the
  second one: under an active commit mode a *recoverable* pause commits the
  changes its turn retained, so the relaunch starts on a clean worktree. No
  refusal made here is recoverable, so none of them is ever settled that way.
  The dirt a write-scope violation or a protected-path change leaves behind is
  the evidence these gates exist to preserve, and it stays in the worktree,
  uncommitted, for the human the pause is already waiting on. Nothing rolls
  back on either path.
* **A guard that cannot see is not a guard that saw nothing.** An unreadable
  comparison, an unavailable snapshot lane, and ``changed_paths`` of ``None`` all
  refuse the turn through the guard's own unavailable reason rather than clearing
  it.
* **One capture answers both questions.** The protected-path guard reads the
  write guard's recorded evidence instead of recapturing, because a second
  snapshot taken at a different instant could convict a path the first cleared.
* **Adjudicate-only never displaces a decided outcome.** A turn already ending
  for another reason still reaches and records the protected-path verdict, and
  amends the live pause details in place, but performs no second revert and
  publishes no pause that would overwrite the first one's reason.
* **The scope-stale rewrite is widening-only and revalidated.** Regeneration is
  plan-wide so a single stale sibling cannot deadlock the continuation, and the
  gate reprojects afterwards rather than assuming the gap closed.
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
    plans_session_scope,
    plans_snapshot,
    repository_evidence,
    run_inputs,
    run_persistence,
    run_termination,
    scope_recovery,
    turn_execution_commit,
    workspace_evidence,
    workspaces,
)


def _enforce_workspace_write_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans_session_scope.SessionScope,
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
    state_dir_arg: str | None = None,
    run_dir: Path | None = None,
    external_digests_before: Mapping[str, Mapping[str, Any]] | None = None,
    turn_started_ns: int | None = None,
    external_change_aliases: frozenset[str] = frozenset(),
) -> bool:
    """Fail closed when a registered implementation turn exceeds its writes."""
    snapshot_after: repository_evidence.WorkspaceSnapshot | None = None
    try:
        snapshot_after = handoff_guards._capture_workspace_write_guard_snapshot(
            workspace=workspace,
            plan_dir=before.plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    except repository_evidence.RepositoryEvidenceError as error:
        comparison: dict[str, Any] = {
            "changed": {},
            "added": [],
            "removed": [],
            "unavailable": {
                ".": {
                    "after": {
                        "reason": "workspace-inventory-failed",
                        "error": str(error),
                    }
                }
            },
        }
    else:
        comparison = repository_evidence.compare_workspace_snapshots(
            snapshot_before, snapshot_after
        )
        workspace_evidence._annotate_history_transitions(
            comparison,
            repository_roots={
                repository.repository.path: repository.root
                for repository in workspace.repositories
            },
        )

    evidence = workspace_evidence._workspace_write_guard_evidence(
        workspace=workspace,
        scope=scope,
        comparison=comparison,
        exclude=exclude,
        include_ignored=include_ignored,
        external_writes=external_write_evidence._turn_external_write_evidence(
            plan_dir=before.plan_dir,
            artifact_paths=(
                *run_inputs._task_packet_artifact_paths(
                    before.plan_dir, session_id, None
                ),
                paths["last_message"],
            ),
            root=workspace.root,
            workspace_root=workspace.root,
            repository_roots=[repository.root for repository in workspace.repositories],
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
        external_change_aliases=external_change_aliases,
    )
    if evidence["passed"]:
        with persistence.lock:
            turn_payload["workspace_write_guard"] = evidence
            durability.write_json_file(paths["result"], turn_payload)
        return False

    if evidence["unavailable"]:
        unavailable_message = workspace_evidence._workspace_unavailable_message(
            evidence["unavailable"], workspace=workspace
        )
        reason = events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
        failure_note = f"workspace write guard unavailable; {unavailable_message}"
        message = (
            f"workspace write guard unavailable after {session_label}; "
            f"{unavailable_message}"
        )
    else:
        reason = events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        summary = workspace_evidence._workspace_guard_violation_summary(evidence)
        failure_note = (
            "workspace write scope violation retained"
            f"{summary}; repair the durable repository scope and rerun "
            "expansion/preflight before continuing"
        )
        # The guard compares snapshots; it cannot tell the session's writes
        # from a concurrent process's, so the message must not claim either
        # (ASI-140).
        message = (
            f"registered repositories changed during {session_label} "
            f"(writer unknown) outside its write aliases{summary}; "
            "changes were retained"
        )

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
        "repair_required": workspace_evidence._workspace_repair_required(
            evidence, reason
        ),
    }
    if reason == events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION:
        guard_record["snapshot_before"] = snapshot_before
        # The retained changes outlive this process, so what the turn left
        # behind has to outlive it too: the after-vector is the only durable
        # description of the *content* of every retained path. Without it a
        # later resume can prove that a dirty path bears a name the turn wrote
        # but never that it still holds the bytes the turn wrote.
        if snapshot_after is not None:
            guard_record["snapshot_after"] = snapshot_after
        guard_record["expansion_before_repair"] = (
            workspace_evidence._session_expansion_evidence(before.plan_dir, session_id)
        )
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
        guard="workspace-write-guard",
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
            "workspace_write_guard": guard_record,
            "external_change_recovery": scope_recovery.recovery_diagnostics(
                before.plan_dir,
                session_id,
                guard_record,
                state_dir=state_dir_arg,
            ),
        },
        persistence=persistence,
    )
    return True
