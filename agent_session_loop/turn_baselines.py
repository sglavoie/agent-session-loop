"""What the turn will be measured against, and the guard armed before it runs.

Third step of the turn-preflight boundary described in ``turn_preflight``:
:func:`capture_turn_baselines` captures every before-image the guards compare
to -- the workspace write-guard snapshot or the legacy single-repository one,
the review fingerprint and the control-plane digests -- checks each for
usability, spends a retained repair's one chance to clear itself, and arms the
exception guard that lets a crashed turn still reach a verdict. Every refusal
is a ``ctx.pause`` immediately followed by the return that carries it.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    events,
    external_write_evidence,
    handoff_guards,
    plans_models,
    plans_session_scope,
    repository_evidence,
    review_guard,
    scope_recovery,
    turn_execution_live,
    turn_preparation,
    workspace_evidence,
    workspaces,
)
from .turn_preflight import RunContext


@dataclasses.dataclass(frozen=True)
class TurnBaselines:
    """Every before-image this turn will be judged against.

    Exactly one of ``workspace_snapshot``, ``legacy_snapshot`` and
    ``review_fingerprint`` is present on a turn that was not refused, and which
    one it is decides the guard mode the crash path and the finalization path
    both dispatch on. ``pending_exception_guard`` is the armed form of that
    choice; it is ``None`` on a refusal so the caller's own guard, if it holds
    one from a previous turn, is left exactly as it was.
    """

    workspace_snapshot: repository_evidence.WorkspaceSnapshot | None
    legacy_snapshot: repository_evidence.WorkspaceSnapshot | None
    review_fingerprint: dict[str, Any] | None
    external_digests: dict[str, dict[str, Any]]
    turn_started_ns: int
    pending_exception_guard: dict[str, Any] | None
    protected_path_baseline: bool
    paused: bool


def capture_turn_baselines(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    state_dir_arg: str | None,
    cwd: Path,
    classification: turn_preparation.SessionClassification,
    workspace: workspaces.ResolvedWorkspace | None,
    session_scope: plans_session_scope.SessionScope | None,
    review_base: repository_evidence.WorkspaceSnapshot | None,
    retained_scope_repair: dict[str, Any] | None,
    regenerated_write_scope: dict[str, Any] | None,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
    active_turn: dict[str, Any],
    live_turn: turn_execution_live.LiveTurn,
    paths: dict[str, Path],
    authorized_external_roots: tuple[Path, ...] = (),
) -> TurnBaselines:
    """Capture the turn's before-images and arm the crash-path guard.

    Captured before the reply is consumed and before ``turn_started``: an
    unusable guard has to stop the run without spending the turn.
    """
    workspace_snapshot_before: repository_evidence.WorkspaceSnapshot | None = None
    legacy_snapshot_before: repository_evidence.WorkspaceSnapshot | None = None
    review_fingerprint_before: dict[str, Any] | None = None
    # The control plane is digested for both modes and unconditionally: the
    # repository snapshot is silent about it by construction, so a permitted
    # mutation there has no other evidence.
    turn_started_ns = time.time_ns()
    external_digests_before = external_write_evidence._capture_external_write_digests(
        (
            *external_write_evidence._control_plane_digest_roots(
                snapshot.plan_dir, state_dir_arg, run_dir=ctx.run_dir
            ),
            *authorized_external_roots,
        ),
        shallow_roots=(
            external_write_evidence._control_plane_shallow_digest_roots(state_dir_arg)
        ),
    )

    def refused() -> TurnBaselines:
        """The turn was refused; the loop breaks and spends nothing."""
        return TurnBaselines(
            workspace_snapshot=workspace_snapshot_before,
            legacy_snapshot=legacy_snapshot_before,
            review_fingerprint=review_fingerprint_before,
            external_digests=external_digests_before,
            turn_started_ns=turn_started_ns,
            pending_exception_guard=None,
            protected_path_baseline=False,
            paused=True,
        )

    if workspace is not None:
        assert session_scope is not None
        if classification.is_review_session and review_base is not None:
            workspace_snapshot_before = review_base
            unavailable = workspace_evidence._workspace_snapshot_unavailable(
                workspace_snapshot_before,
                required_repository_paths=tuple(
                    repository.repository.path for repository in workspace.repositories
                ),
            )
        else:
            try:
                workspace_snapshot_before = (
                    handoff_guards._capture_workspace_write_guard_snapshot(
                        workspace=workspace,
                        plan_dir=snapshot.plan_dir,
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
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
                    workspace_snapshot_before,
                    required_repository_paths=tuple(
                        repository.repository.path
                        for repository in workspace.repositories
                    ),
                )
        if unavailable:
            unavailable_message = workspace_evidence._workspace_unavailable_message(
                unavailable,
                workspace=workspace,
            )
            guard_record = {
                "passed": False,
                "reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                "phase": "before",
                "unavailable": unavailable,
                "read_aliases": list(session_scope.reads),
                "write_aliases": list(session_scope.writes),
                "excluded": list(review_guard_excludes),
                "include_ignored": review_guard_include_ignored,
            }
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                message="workspace write guard unavailable before "
                f"{session_label}; {unavailable_message}",
                details={
                    **session_details,
                    "workspace_write_guard": guard_record,
                },
            )
            return refused()
        # The only path that leaves the snapshot unset is the inventory failure
        # above, which sets `unavailable` and has already returned.
        assert workspace_snapshot_before is not None
        if retained_scope_repair is not None:
            # The durable file was already widened before preflight and the task
            # packet were built, so the guard below judges the retained changes
            # against exactly the scope preflight, expansion, the contract and
            # the prompt already read.
            prior_snapshot = retained_scope_repair.get("snapshot_before")
            if isinstance(prior_snapshot, Mapping):
                repair_comparison = repository_evidence.compare_workspace_snapshots(
                    prior_snapshot, workspace_snapshot_before
                )
                (
                    repair_comparison,
                    repair_registry_delta,
                ) = workspace_evidence._workspace_scope_repair_comparison(
                    repair_comparison,
                    frozenset(
                        repository.repository.path
                        for repository in workspace.repositories
                    ),
                )
                repair_evidence = workspace_evidence._workspace_write_guard_evidence(
                    workspace=workspace,
                    scope=session_scope,
                    comparison=repair_comparison,
                    exclude=review_guard_excludes,
                    include_ignored=review_guard_include_ignored,
                )
                repair_evidence["repair_registry_delta"] = repair_registry_delta
            else:
                repair_evidence = {
                    "passed": False,
                    "unavailable": {".": {"reason": "repair-baseline-missing"}},
                    "violations": [],
                }
            if regenerated_write_scope is not None:
                repair_evidence["write_scope_regeneration"] = regenerated_write_scope
            current_expansion = workspace_evidence._session_expansion_evidence(
                snapshot.plan_dir, session_id
            )
            prior_expansion = retained_scope_repair.get("expansion_before_repair")
            restored = workspace_evidence._workspace_scope_repair_restored(
                retained_scope_repair,
                workspace_snapshot_before,
                workspace=workspace,
                scope=session_scope,
                exclude=review_guard_excludes,
                include_ignored=review_guard_include_ignored,
            )
            expansion_fresh = isinstance(
                prior_expansion, Mapping
            ) and workspace_evidence._workspace_scope_repair_is_fresh(
                prior_expansion, current_expansion
            )
            if not repair_evidence["passed"] or not (expansion_fresh or restored):
                repair_record = {
                    **retained_scope_repair,
                    "phase": "repair-check",
                    "current_scope_evidence": repair_evidence,
                    "current_expansion": current_expansion,
                    "expansion_fresh": expansion_fresh,
                    "restored": restored,
                }
                ctx.record_final_snapshot(snapshot)
                ctx.pause(
                    reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                    message=f"{session_label} still has retained changes "
                    "outside its durable write scope or lacks a "
                    "fresh expansion; restore the unauthorized changes to the "
                    "recorded pre-turn state, or explicitly authorize a scope "
                    "change and rerun expansion/preflight before continuing",
                    details={
                        **session_details,
                        "workspace_write_guard": repair_record,
                        "external_change_recovery": scope_recovery.recovery_diagnostics(
                            snapshot.plan_dir,
                            session_id,
                            repair_record,
                        ),
                    },
                )
                return refused()
            ctx.persistence.append_event(
                ctx.event_path,
                events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                {
                    "run_id": ctx.run_id,
                    "details": {
                        **session_details,
                        "session_id": session_id,
                        "workspace_write_guard": repair_evidence,
                        "expansion": current_expansion,
                        "restored": restored,
                    },
                },
            )
    elif not classification.is_review_session:
        legacy_snapshot_before = handoff_guards._capture_legacy_implementation_snapshot(
            cwd=cwd,
            plan_dir=snapshot.plan_dir,
            exclude=review_guard_excludes,
            include_ignored=review_guard_include_ignored,
        )
        unavailable = workspace_evidence._workspace_snapshot_unavailable(
            legacy_snapshot_before,
            required_repository_paths=(".",),
        )
        if unavailable:
            guard_record = {
                "passed": False,
                "scope_mode": "legacy-single-repository",
                "reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                "phase": "before",
                "repository_root": str(cwd),
                "unavailable": unavailable,
                "read_aliases": ["legacy"],
                "write_aliases": ["legacy"],
                "excluded": list(review_guard_excludes),
                "include_ignored": review_guard_include_ignored,
            }
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                message="implementation write guard unavailable before "
                f"{session_label}; the legacy Git repository scope "
                "could not be established safely",
                details={
                    **session_details,
                    "workspace_write_guard": guard_record,
                },
            )
            return refused()
    # Captured before the exception-guard context below, because a legacy review
    # turn has no write-guard snapshot and this fingerprint is the only
    # turn-start comparison it will ever hold.
    review_fingerprint_before = (
        review_guard._capture_review_repo_fingerprint(
            cwd,
            snapshot.plan_dir,
            exclude=review_guard_excludes,
            include_ignored=review_guard_include_ignored,
        )
        if classification.is_review_session and workspace is None
        else None
    )
    if review_fingerprint_before is not None:
        review_unavailable = review_guard._review_guard_unavailable_detail(
            review_fingerprint_before
        )
        if review_unavailable is not None:
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                message="review mutation guard unavailable before "
                f"{session_label}: {review_unavailable['reason']}",
                details={
                    **session_details,
                    "review_mutation_guard": {
                        "passed": False,
                        "reason": events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                        "phase": "before",
                        "unavailable": review_unavailable,
                        "excluded": list(review_guard_excludes),
                        "include_ignored": review_guard_include_ignored,
                    },
                },
            )
            return refused()
    pending_exception_guard: dict[str, Any] | None = None
    if workspace is not None:
        assert session_scope is not None
        assert workspace_snapshot_before is not None
        pending_exception_guard = {
            "mode": "workspace",
            "workspace": workspace,
            "scope": session_scope,
            "before": snapshot,
            "after": None,
            "snapshot_before": workspace_snapshot_before,
            "session_id": session_id,
            "session_label": session_label,
            "session_details": session_details,
            "turn_payload": None,
            "active_turn": active_turn,
            "live_turn": live_turn,
            "paths": paths,
            "external_digests_before": external_digests_before,
            "turn_started_ns": turn_started_ns,
        }
    elif legacy_snapshot_before is not None:
        pending_exception_guard = {
            "mode": "legacy",
            "cwd": cwd,
            "before": snapshot,
            "after": None,
            "snapshot_before": legacy_snapshot_before,
            "session_id": session_id,
            "session_label": session_label,
            "session_details": session_details,
            "turn_payload": None,
            "active_turn": active_turn,
            "live_turn": live_turn,
            "paths": paths,
            "external_digests_before": external_digests_before,
            "turn_started_ns": turn_started_ns,
        }
    elif review_fingerprint_before is not None:
        # A legacy review turn captures no write-guard snapshot, so without this
        # branch it enters the harness with no exception context at all and the
        # outer handler skips protected-path adjudication entirely -- in the one
        # moment the run's guards are still alive. The fingerprint is its
        # turn-start comparison, so the crash path can still reach a verdict.
        pending_exception_guard = {
            "mode": "review",
            "cwd": cwd,
            "before": snapshot,
            "after": None,
            "review_fingerprint_before": review_fingerprint_before,
            "session_id": session_id,
            "session_label": session_label,
            "session_details": session_details,
            "turn_payload": None,
            "active_turn": active_turn,
            "live_turn": live_turn,
            "paths": paths,
            "external_digests_before": external_digests_before,
            "turn_started_ns": turn_started_ns,
        }
    return TurnBaselines(
        workspace_snapshot=workspace_snapshot_before,
        legacy_snapshot=legacy_snapshot_before,
        review_fingerprint=review_fingerprint_before,
        external_digests=external_digests_before,
        turn_started_ns=turn_started_ns,
        pending_exception_guard=pending_exception_guard,
        # Whether this turn has something a protected-path guard can be compared
        # against. The review fingerprint counts: a legacy review turn captures
        # no write-guard snapshot, but its mutation guard proves the whole
        # worktree is unchanged, which clears every path rule at once rather
        # than leaving them unproven.
        protected_path_baseline=(
            workspace_snapshot_before is not None
            or legacy_snapshot_before is not None
            or review_fingerprint_before is not None
        ),
        paused=False,
    )
