"""Whether a finished turn is allowed to stand, and what happens when it is not.

Every function that judges evidence lives elsewhere. This module owns the layer
above them: it takes a verdict the evidence modules produced, decides whether the
turn survives it, and — when it does not — performs the identical five-step
refusal that makes the refusal durable. A guard that only computed a verdict
would leave the run free to publish the turn anyway; a guard that computed one
inline in the loop would make each refusal a fresh transcription of the same five
steps. Both are why this is a boundary rather than a stretch of ``_run_locked``.

It owns three things:

* **The two write guards.** :func:`_enforce_workspace_write_guard` refuses a
  registered implementation turn that changed a repository outside its declared
  write aliases; :func:`_enforce_legacy_implementation_write_guard` refuses a
  legacy single-repository turn whose scope or history evidence could not be
  captured safely. Both read their verdict out of ``workspace_evidence`` and
  differ only in which snapshot they compare and which lane can convict.
* **The protected-path guard.** :func:`_enforce_protected_path_guard` refuses a
  turn that moved a path an operator froze mid-run, with
  :func:`_protected_path_changed_paths` reading the changed set out of the write
  guard's own evidence and :func:`_protected_path_review_changed_paths`
  recapturing it for a legacy review turn that crashed before producing any.
* **The repair-continuation gate.** :func:`_pending_repair_child_continuation`
  projects the repair resume and, when a stale persisted scope is the only thing
  blocking it, calls :func:`_regenerate_stale_repair_child_scopes` to widen every
  pending child the projection named and reprojects. ``REPAIR_SCOPE_STALE_BLOCKER``
  is the blocker string that lane keys on.

Durable records this boundary writes: the ``workspace_write_guard`` and
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
collaborators that have not left the runner yet enter as required keyword-only
seams: the handoff-note appender, through the
:class:`handoff_guards.HandoffNoteAppender` Protocol ``handoff_correction``
satisfies, and
the post-revert plan re-read, through the
:data:`run_termination.SnapshotReinspector` alias Session 23 will satisfy.
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
  *claim* — the session status — and never the work.
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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    durability,
    events,
    external_write_evidence,
    guards,
    handoff_guards,
    plans,
    preflight,
    readiness_gate,
    repository_evidence,
    review_continuation,
    review_cycle,
    review_guard,
    run_inputs,
    run_persistence,
    run_prompts,
    run_termination,
    turn_execution,
    workspace_evidence,
    workspaces,
)

REPAIR_SCOPE_STALE_BLOCKER = "repair-scope-stale"


def _regenerate_stale_repair_child_scopes(
    *,
    snapshot: plans.PlanSnapshot,
    workspace: workspaces.ResolvedWorkspace,
    gaps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Widen every pending child a scope-stale projection named.

    The projection is plan-wide, so a single stale sibling blocks the whole
    ``run-repairs`` continuation. Regenerating each child the projection names —
    not only the one about to run — is what keeps the gate self-clearing rather
    than a deadlock an operator has to hand-edit their way out of.
    """
    by_id = {session.id: session for session in snapshot.sessions}
    records: list[dict[str, Any]] = []
    for gap in gaps:
        session_id = gap.get("repair_session_id")
        session = by_id.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.status != "pending":
            continue
        try:
            scope = run_prompts._session_scope(session, workspace)
        except plans.PlanError:
            # Fail open exactly like the regeneration helper: a child whose
            # scope cannot be read keeps the blocker rather than being rewritten
            # from a scope nobody could parse.
            continue
        record = workspace_evidence._regenerated_repair_child_write_scope(
            session=session,
            plan_dir=snapshot.plan_dir,
            workspace=workspace,
            scope=scope,
        )
        if record is not None:
            records.append(record)
    return records


def _pending_repair_child_continuation(
    *,
    snapshot: plans.PlanSnapshot,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    workspace: workspaces.ResolvedWorkspace | None,
    max_repair_group_attempts: int,
    max_review_fix_cycles: int,
    review_requested: bool,
) -> review_continuation.ReviewResume:
    """Project the repair continuation, clearing a stale scope once if it can.

    The continuation is the only component that compares every pending child's
    persisted scope against the scope its own findings require today. Projecting
    it before the child is dispatched is what turns ``repair-scope-stale`` from
    an advisory note into a gate: the caller either launches a child the
    projection calls eligible, or stops without spending a turn.
    """

    def project() -> review_continuation.ReviewResume:
        return review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            max_repair_group_attempts=max_repair_group_attempts,
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=review_requested,
        )

    resume = project()
    if (
        workspace is None
        or resume.action != "run-repairs"
        or resume.eligible
        or resume.blocker != REPAIR_SCOPE_STALE_BLOCKER
        or not resume.repair_scope_gaps
    ):
        return resume
    if not _regenerate_stale_repair_child_scopes(
        snapshot=snapshot,
        workspace=workspace,
        gaps=resume.repair_scope_gaps,
    ):
        return resume
    readiness_gate._refresh_active_readiness_state(
        payload, snapshot.plan_dir, persistence
    )
    # Revalidate rather than assume: the rewrite is widening-only and fail-open,
    # so the second projection is the only proof the gap actually closed.
    return project()


def _enforce_workspace_write_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    workspace: workspaces.ResolvedWorkspace,
    scope: plans.SessionScope,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    snapshot_before: repository_evidence.WorkspaceSnapshot,
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    append_handoff_note: handoff_guards.HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
    state_dir_arg: str | None = None,
    run_dir: Path | None = None,
    external_digests_before: Mapping[str, Mapping[str, Any]] | None = None,
    turn_started_ns: int | None = None,
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
        ),
    )
    if evidence["passed"]:
        with persistence.lock:
            turn_payload["workspace_write_guard"] = evidence
            durability.write_json_file(paths["result"], turn_payload)
        return False

    if evidence["unavailable"]:
        unavailable_summary = workspace_evidence._workspace_unavailable_summary(
            evidence["unavailable"], workspace=workspace
        )
        reason = events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
        failure_note = (
            "workspace write guard unavailable; evidence for a registered "
            f"repository could not be captured safely{unavailable_summary}"
        )
        message = (
            f"workspace write guard unavailable after {session_label}; "
            "evidence for a registered repository could not be captured "
            f"safely{unavailable_summary}"
        )
    else:
        reason = events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION
        summary = workspace_evidence._workspace_guard_violation_summary(evidence)
        failure_note = (
            "workspace write scope violation retained"
            f"{summary}; repair the durable repository scope and rerun "
            "expansion/preflight before continuing"
        )
        message = (
            f"{session_label} changed registered repositories outside its "
            f"write aliases{summary}; changes were retained"
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
        "reverted_to_pending": reverted is not None,
        "repair_required": {
            "durable_scope": True,
            "fresh_expansion": True,
            "fresh_preflight": True,
        },
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
        payload["final_snapshot"] = plans.snapshot_payload(inspected)
        turn_payload["workspace_write_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="workspace-write-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={
            **session_details,
            "workspace_write_guard": guard_record,
        },
        persistence=persistence,
    )
    return True


def _enforce_legacy_implementation_write_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    cwd: Path,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    snapshot_before: repository_evidence.WorkspaceSnapshot,
    exclude: tuple[str, ...],
    include_ignored: bool,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
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
            provenance_roots=preflight.plan_provenance_exempt_roots(before.plan_dir),
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
        ),
    )
    if evidence["passed"]:
        with persistence.lock:
            turn_payload["workspace_write_guard"] = evidence
            durability.write_json_file(paths["result"], turn_payload)
        return False

    reason = events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE
    failure_note = (
        "implementation write guard unavailable; legacy repository scope or "
        "history evidence could not be captured safely"
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
        "reverted_to_pending": reverted is not None,
    }
    with persistence.lock:
        payload["final_snapshot"] = plans.snapshot_payload(inspected)
        turn_payload["workspace_write_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="legacy-implementation-write-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=(
            f"implementation write guard unavailable after {session_label}; "
            "legacy repository scope or history evidence could not be captured safely"
        ),
        details={
            **session_details,
            "workspace_write_guard": guard_record,
        },
        persistence=persistence,
    )
    return True


def _protected_path_changed_paths(
    evidence: Mapping[str, Any] | None,
) -> tuple[str, ...] | None:
    """Every qualified path the turn changed, as the write guard recorded it.

    Read from the guard's own evidence rather than recaptured: a second
    snapshot would be taken at a different instant, so a path that moved
    between the two comparisons could be convicted by one and cleared by the
    other. One capture, two questions.

    ``None`` means the comparison cannot answer the protected-path question at
    all. That includes a write guard that failed to inventory a repository:
    its ``changed`` map is then a partial view, and reading an empty one as
    "nothing moved" would clear a frozen path the run never actually looked
    at. This only arises on a composed finalization, because an incomplete
    inventory already fails the write guard.
    """
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("unavailable"):
        return None
    changed = evidence.get("changed")
    if not isinstance(changed, Mapping):
        return None
    qualified: list[str] = []
    for entry in changed.values():
        if not isinstance(entry, Mapping):
            continue
        paths = entry.get("qualified_paths")
        if isinstance(paths, list):
            qualified.extend(str(path) for path in paths)
    return tuple(dict.fromkeys(qualified))


def _protected_path_review_changed_paths(
    guard_context: Mapping[str, Any],
    *,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> tuple[str, ...] | None:
    """Every qualified path a crashing legacy review turn changed.

    A legacy review turn captures no write-guard comparison, so the crash path
    has nothing to read out of ``workspace_write_guard``. It recaptures the
    review fingerprint instead and compares it against the one taken at turn
    start -- the same comparison the mutation guard would have made had the
    turn finished -- so the protected-path guard has a baseline to adjudicate
    against.

    ``None`` means the recapture cannot answer the question: it raised, or the
    evidence came back unusable. That is reported as unavailable rather than
    read as "nothing moved", because a guard that cannot see is not a guard
    that saw nothing.
    """
    fingerprint_before = guard_context.get("review_fingerprint_before")
    if not isinstance(fingerprint_before, Mapping):
        return None
    try:
        fingerprint_after = review_guard._capture_review_repo_fingerprint(
            guard_context["cwd"],
            guard_context["before"].plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    except Exception:
        return None
    if review_guard._review_guard_unavailable_detail(fingerprint_after) is not None:
        return None
    if fingerprint_after == fingerprint_before:
        # Equal fingerprints prove the whole worktree is unchanged, which
        # clears every path rule at once -- a stronger clearance than an empty
        # change list, and the same one the finished-turn path grants.
        return ()
    return tuple(
        repository_evidence.qualified_repository_paths(
            guards.LEGACY_PATH_QUALIFIER,
            review_guard._review_guard_changed_paths(
                fingerprint_before, fingerprint_after
            ),
        )
    )


def _enforce_protected_path_guard(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
    guard_controller: guards.GuardController | None,
    before: plans.PlanSnapshot,
    after: plans.PlanSnapshot | None,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    turn_payload: dict[str, Any],
    paths: dict[str, Path],
    plan_cache: plans.PlanSnapshotCache,
    append_handoff_note: handoff_guards.HandoffNoteAppender,
    reinspect: run_termination.SnapshotReinspector,
    changed_paths: tuple[str, ...] | None,
    adjudicate_only: bool = False,
) -> bool:
    """Refuse to publish a turn that moved a path an operator declared frozen.

    This runs after the general workspace write guard and before every result
    branch, and it is deliberately a *separate* step: the write guard asks
    whether the session stayed inside its declared scope, which is a question
    about the plan, while this asks whether the run honoured a standing
    instruction an operator gave mid-flight, which is a question about the
    operator. A path can be perfectly in scope and still be one the operator
    said not to touch.

    The baseline is the turn-start snapshot, so a guard added mid-turn convicts
    a change the turn had already made. That is the intended reading of "this
    file must not change": an operator who sees a file they care about and
    freezes it is asking about its state now, not about the moment their
    command was drained.

    Filesystem changes are always retained. The run has no way to know whether
    the change was the valuable part of the turn, and silently reverting an
    agent's work to satisfy a guard would destroy more than it protects.

    ``changed_paths`` of ``None`` means the turn produced no comparison this
    guard can read, which is refused rather than waved through: a guard that
    cannot see is not a guard that saw nothing.

    ``adjudicate_only`` is for a turn that is already ending for another
    reason -- another finalization guard paused it, or the harness raised.
    The verdict is still reached and still recorded, but the outcome that was
    already decided is left standing: no second revert, and no pause that
    would overwrite the first one's reason and remediation with this one's.
    An operator reading the primary pause still finds the protected-path
    verdict beside it, because a combined failure that reports only half of
    itself is the one an operator repairs twice.
    """
    active = () if guard_controller is None else guard_controller.active_paths()
    if not active:
        return False

    evidence = turn_payload.get("workspace_write_guard")
    unavailable_detail: str | None = None
    if changed_paths is None:
        changed_paths = ()
        unavailable_detail = (
            "no turn-start comparison was available for this turn, so the "
            "workspace could not be compared against its baseline"
        )

    coverage_by_id = {
        entry.get("id"): entry.get("coverage")
        for entry in (
            guard_controller.to_payload() if guard_controller is not None else []
        )
    }
    matched = (
        guards.match_protected_paths(active, changed_paths)
        if unavailable_detail is None
        else {}
    )
    if unavailable_detail is None and not matched:
        with persistence.lock:
            turn_payload["protected_path_guard"] = {
                "passed": True,
                "session_id": session_id,
                "guards": [rule.to_payload() for rule in active],
                "observed_paths": list(changed_paths),
                "coverage": {rule.id: coverage_by_id.get(rule.id) for rule in active},
            }
            durability.write_json_file(paths["result"], turn_payload)
        return False

    if unavailable_detail is not None:
        reason = events.PAUSE_REASON_GUARD_PROTECTED_PATH_UNAVAILABLE
        failure_note = (
            "protected-path guard evidence unavailable; "
            f"{unavailable_detail}, so the guard could neither clear nor "
            "convict the turn"
        )
        message = (
            f"protected-path guard unavailable after {session_label}; "
            f"{unavailable_detail}"
        )
    else:
        reason = events.PAUSE_REASON_GUARD_PROTECTED_PATH_CHANGED
        named = ", ".join(
            f"{guard_id} ({', '.join(hits)})" for guard_id, hits in matched.items()
        )
        failure_note = (
            f"protected-path guard violation retained: {named}; restore the "
            "path to its turn-start state or remove the guard before rerunning"
        )
        message = (
            f"{session_label} changed protected paths: {named}; changes were retained"
        )

    matched_repositories = {
        qualified.partition(":")[0] for hits in matched.values() for qualified in hits
    }
    raw_changed = evidence.get("changed") if isinstance(evidence, Mapping) else None
    before_after = (
        {
            repository: dict(entry)
            for repository, entry in raw_changed.items()
            if isinstance(entry, Mapping)
            # A qualified path names a repository by its registered alias when
            # it has one, while the write guard keys its comparison by
            # repository path. Either name identifies the same repository, so
            # either one keeps the evidence attached; matching only the key
            # silently drops the before/after vectors for every aliased
            # workspace repository.
            and (
                repository in matched_repositories
                or entry.get("alias") in matched_repositories
            )
        }
        if isinstance(raw_changed, Mapping)
        else {}
    )
    guard_record: dict[str, Any] = {
        "passed": False,
        "session_id": session_id,
        "reason": reason,
        "phase": "after",
        "guard_ids": list(matched),
        "matched_paths": {guard_id: list(hits) for guard_id, hits in matched.items()},
        "guards": [rule.to_payload() for rule in active],
        "observed_paths": list(changed_paths),
        # The write guard's own before/after vectors for exactly the
        # repositories that hold a matched path: the operator's next decision
        # is "restore or accept", and that needs the content evidence, not just
        # the names.
        "evidence": before_after,
        "coverage": {rule.id: coverage_by_id.get(rule.id) for rule in active},
        "retained": True,
        "detail": unavailable_detail,
    }

    if adjudicate_only:
        guard_record["adjudicated_only"] = True
        # The turn is already ending, and whichever outcome got here first
        # already withdrew the session if it needed withdrawing. Reverting
        # again would append a second failure note for one turn.
        guard_record["reverted_to_pending"] = False
        primary_reason = payload.get("pause_reason")
        if payload.get("paused") and isinstance(primary_reason, str):
            guard_record["deferred_to_pause_reason"] = primary_reason
        with persistence.lock:
            turn_payload["protected_path_guard"] = guard_record
            durability.write_json_file(paths["result"], turn_payload)
            # The primary pause was published before this verdict existed, so
            # its live details are amended in place -- ``payload["details"]``
            # is the same object -- rather than republished under a reason
            # that would displace the one an operator has to act on.
            pause_details = payload.get("pause_details")
            if payload.get("paused") and isinstance(pause_details, dict):
                pause_details["protected_path_guard"] = guard_record
        turn_execution.record_turn_rejected(
            event_path=event_path,
            payload=payload,
            persistence=persistence,
            turn_payload=turn_payload,
            session_id=session_id,
            guard="protected-path-guard",
            reason=reason,
            reopened=False,
            detail={
                "adjudicated_only": True,
                "guard_ids": list(matched),
                "matched_paths": {
                    guard_id: list(hits) for guard_id, hits in matched.items()
                },
            },
        )
        return False

    revert_target = after if after is not None else before
    reverted = handoff_guards._revert_session_status_to_pending(
        revert_target,
        session_id,
        failure_note=failure_note,
        append_handoff_note=append_handoff_note,
    )
    inspected = reinspect(before.plan_dir, revert_target, reverted, plan_cache)
    guard_record["reverted_to_pending"] = reverted is not None
    with persistence.lock:
        payload["final_snapshot"] = plans.snapshot_payload(inspected)
        turn_payload["protected_path_guard"] = guard_record
        durability.write_json_file(paths["result"], turn_payload)
    turn_execution.record_turn_rejected(
        event_path=event_path,
        payload=payload,
        persistence=persistence,
        turn_payload=turn_payload,
        session_id=session_id,
        guard="protected-path-guard",
        reason=reason,
        reopened=reverted is not None,
    )
    run_termination._pause_run(
        event_path,
        payload,
        reason=reason,
        message=message,
        details={**session_details, "protected_path_guard": guard_record},
        persistence=persistence,
    )
    return True
