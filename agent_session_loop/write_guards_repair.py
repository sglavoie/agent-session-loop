"""Repair-continuation gate: widen stale pending repair-child scopes once.

:func:`_pending_repair_child_continuation` projects the repair resume and, when a
stale persisted scope is the only thing blocking it, calls
:func:`_regenerate_stale_repair_child_scopes` to widen every pending child the
projection named and reprojects. ``REPAIR_SCOPE_STALE_BLOCKER`` is the blocker
string that lane keys on.

* **The scope-stale rewrite is widening-only and revalidated.** Regeneration is
  plan-wide so a single stale sibling cannot deadlock the continuation, and the
  gate reprojects afterwards rather than assuming the gap closed.

The scope widening is delegated to
``workspace_evidence._regenerated_repair_child_write_scope``, which owns that
write at its own boundary. Nothing here imports the runner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    plans_errors,
    plans_models,
    readiness_gate,
    review_continuation,
    review_cycle,
    run_persistence,
    run_prompts,
    workspace_evidence,
    workspaces,
)

REPAIR_SCOPE_STALE_BLOCKER = "repair-scope-stale"


def _regenerate_stale_repair_child_scopes(
    *,
    snapshot: plans_models.PlanSnapshot,
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
        except plans_errors.PlanError:
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
    snapshot: plans_models.PlanSnapshot,
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
