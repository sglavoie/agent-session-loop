"""The turn's own before-image, captured while the turn can still be refused.

Preparation settles what a *selected session* needs; this boundary settles what
*this turn* is and what it will be judged against, in the last window where
nothing has been spent yet. Turn preparation happens after the prompt exists
and before the harness process does, which is why a refusal at this point costs
the run nothing. :func:`retained_scope_restored` also captures startup evidence
before a historical scope violation can block readiness repair; that proof is
repeated against the turn's own baseline before the incident is cleared.

Three steps, in the order the loop takes them:

* **What was retained for this child?** A repair child resumed after a
  scope-repair pause carries a durable requirement from the turn whose changes
  were kept. :func:`resolve_retained_scope_repair` finds it, and refuses to
  match one recorded for a different session — the pause envelope's identity is
  broader than the guard record's own, so the narrower one decides.
* **What is this turn?** :func:`build_active_turn` builds the payload that *is*
  the turn for every reader downstream: the live tail, the mailbox poll's
  context, the control-status projection, the persisted turn record, and the
  crash path's identity. Its clocks are read once, here, rather than re-derived
  at each of those sites.
* **What will the turn be measured against?** :func:`capture_turn_baselines`
  captures every before-image the guards below will compare to — the workspace
  write-guard snapshot or the legacy single-repository one, the review
  fingerprint, and the control-plane digests — checks each for usability, spends
  the retained repair's one chance to clear itself, and arms the exception
  guard that lets a crashed turn still reach a verdict.

Durable records this boundary writes: exactly one, the
``workspace-write-scope-repair`` event that records a retained repair clearing
its scope check, appended through the run's own persistence. Every refusal is
recorded by ``run_termination`` through ``ctx.pause``, and every snapshot this
module captures is an in-memory value handed back to the loop. Nothing else
here reaches disk.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol below, naming only the members these
three steps reach.

Each step hands back a frozen result rather than performing the loop's
``break``: the branches this replaced ended in one, and a boundary cannot break
a loop it does not own. ``paused`` is that ``break``, and every ``ctx.pause``
here is immediately followed by the return that carries it, so a refusal can
never fall through into the turn it refused.

Six invariants survive the move:

* **A baseline that cannot be captured is a refusal, not a narrower turn.**
  Every capture is checked for usability before the harness is launched, and an
  unavailable repository, a failed inventory or an unreadable review fingerprint
  each end the run rather than letting a turn run with a guard that can only
  report "unknown".
* **The review base is captured once per turn, not twice.** A prepared review
  session already captured the base its session file names; that exact object is
  carried in and reused, so the guard measures against the base the file
  declares instead of a second capture taken moments later.
* **A retained repair is judged against the scope every other reader read.** The
  durable child file was widened before preflight, the packet and the prompt, so
  the comparison here is against that same widened scope — and it must also be
  backed by a *fresh* expansion when authority changes. Exact restoration under
  the original scope and observation policy needs no new expansion.
* **The exception guard is armed before the harness exists.** It is the only
  thing that lets a crash reach a write-guard verdict, so it is bound after the
  baselines are known to be usable and before anything can raise. A legacy
  review turn captures no write-guard snapshot and gets the review-fingerprint
  arming instead, rather than entering the harness with no context at all.
* **The control plane is digested unconditionally, for both modes.** The
  repository snapshot is silent about the plan directory, the state root and the
  run directory by construction, so a permitted mutation there has no other
  evidence and the digest is not made conditional on which guard mode applies.
* **A review fingerprint counts as a protected-path baseline.** It proves the
  whole worktree unchanged, which clears every path rule at once, so a legacy
  review turn is not reported as having no baseline merely for lacking a
  write-guard snapshot.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from . import (
    clock,
    events,
    external_write_evidence,
    handoff_guards,
    plans_models,
    plans_payloads,
    plans_session_scope,
    repository_evidence,
    review_guard,
    run_persistence,
    run_prompts,
    run_selection,
    scope_recovery,
    turn_execution,
    turn_preparation,
    verification,
    workspace_evidence,
    workspaces,
)

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as turn preflight needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members these three
    steps actually reach are named: the run's identity and the two paths its
    control plane is digested from, the write seam the repair event goes
    through, the verification context the workspace payload fields are derived
    from, the idle baseline the turn's own gap is measured against, and the two
    ways a run ends.
    """

    @property
    def run_id(self) -> str: ...

    @property
    def run_dir(self) -> Path: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def verification_context(self) -> verification.VerificationContext: ...

    @property
    def runner_work_finished_at(self) -> datetime | None: ...

    def record_final_snapshot(self, snapshot: plans_models.PlanSnapshot) -> None: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What a resumed repair child retained
# ---------------------------------------------------------------------------


def retained_scope_restored(
    *,
    record: Mapping[str, Any],
    workspace: workspaces.ResolvedWorkspace | None,
    plan_dir: Path,
    scope: plans_session_scope.SessionScope | None,
    exclude: tuple[str, ...],
    include_ignored: bool,
) -> bool:
    """Recheck restoration before a retained violation blocks readiness repair."""
    if workspace is None:
        return False
    try:
        current = handoff_guards._capture_workspace_write_guard_snapshot(
            workspace=workspace,
            plan_dir=plan_dir,
            exclude=exclude,
            include_ignored=include_ignored,
        )
    except repository_evidence.RepositoryEvidenceError:
        return False
    return workspace_evidence._workspace_scope_repair_restored(
        record,
        current,
        workspace=workspace,
        scope=scope,
        exclude=exclude,
        include_ignored=include_ignored,
    )


def resolve_retained_scope_repair(
    *,
    event_path: Path,
    session_id: str,
    workspace: workspaces.ResolvedWorkspace | None,
) -> dict[str, Any] | None:
    """Find the scope-repair requirement retained for exactly this session.

    Only a registered workspace records one: the legacy single-repository mode
    has no per-alias write scope to repair. The lookup also matches on the pause
    envelope's session, so the guard record's own identity is re-checked here —
    it is the narrower proof that this is the turn whose changes were retained
    for this child.
    """
    if workspace is None:
        return None
    retained = workspace_evidence._latest_workspace_scope_repair_requirement(
        event_path, session_id
    )
    if retained is not None and retained.get("session_id") != session_id:
        return None
    return retained


# ---------------------------------------------------------------------------
# What this turn is
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ActiveTurn:
    """The turn's own payload, and the plan size it was built against.

    ``payload`` is deliberately a mutable mapping rather than a frozen record:
    it is the live turn's own state, amended in place by the heartbeat, the
    stream callback and the finalizer as the turn runs. What is frozen is the
    fact that this one object is *the* turn — every downstream reader is handed
    this mapping rather than building a second description of the same turn.
    ``total_sessions`` travels beside it because the loop reports it again at
    verification, and re-counting there would read a plan image that has since
    moved.
    """

    payload: dict[str, Any]
    total_sessions: int


def build_active_turn(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    classification: turn_preparation.SessionClassification,
    prompt: turn_preparation.TurnPrompt,
    session_identity: dict[str, Any],
    turn_index: int,
    paths: dict[str, Path],
    harness: str,
    model_effort: dict[str, str | None],
    spark_route: dict[str, Any],
    cwd: Path,
) -> ActiveTurn:
    """Build the payload that is this turn, for every reader that needs one.

    The turn's clocks are read here and only here: the start instant, the text
    form every consumer renders, and the idle gap since the runner last finished
    work of its own. That gap is measured from ``runner_work_finished_at``
    rather than from the previous harness exit, because verification and
    finalization are runner-owned work and charging them to the gap would report
    the previous turn's own cost as idleness.
    """
    started_at = clock.utc_now()
    started_at_text = clock.isoformat(started_at)
    idle_seconds_since_previous_turn = (
        0.0
        if ctx.runner_work_finished_at is None
        else clock.duration_seconds(ctx.runner_work_finished_at, started_at)
    )
    total_sessions = sum(plans_payloads.status_counts(snapshot).values())
    payload: dict[str, Any] = {
        "run_id": ctx.run_id,
        "turn_index": turn_index,
        **session_identity,
        "session_kind": classification.session_kind,
        **(
            {"repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD}
            if classification.is_repair_session
            else {}
        ),
        "total_sessions": total_sessions,
        "artifact_dir": str(paths["artifact_dir"]),
        "prompt": prompt.persisted_prompt,
        "harness": harness,
        "model": model_effort["model"],
        "model_source": model_effort["model_source"],
        "observed_model": None,
        "observed_model_source": None,
        "effort": model_effort["effort"],
        "effort_source": model_effort["effort_source"],
        "spark_route": spark_route,
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "started_at": started_at_text,
        "finished_at": None,
        "duration_seconds": None,
        "idle_seconds_since_previous_turn": idle_seconds_since_previous_turn,
        "last_activity_at": started_at_text,
        "last_activity_stream": None,
        "last_activity_preview": "",
        "last_heartbeat_at": None,
        "elapsed_seconds": 0.0,
        "idle_seconds": 0.0,
        "heartbeat_count": 0,
        "stdout_chars": 0,
        "stderr_chars": 0,
        "action_required": None,
        "prompt_kind": prompt.prompt_kind,
        "reply_source": prompt.reply_source,
        **(
            {
                "cwd": str(cwd),
                **run_prompts._workspace_payload_fields(ctx.verification_context),
            }
            if ctx.verification_context.workspace is not None
            else {}
        ),
    }
    if prompt.resolution_source is not None:
        payload["resolution_source"] = prompt.resolution_source
    return ActiveTurn(payload=payload, total_sessions=total_sessions)


# ---------------------------------------------------------------------------
# What the turn will be measured against
# ---------------------------------------------------------------------------


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
    live_turn: turn_execution.LiveTurn,
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
