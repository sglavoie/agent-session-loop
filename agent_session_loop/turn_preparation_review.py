"""Classify selected sessions and refresh review contracts from current evidence."""

from __future__ import annotations

import json
from pathlib import Path

from . import (
    handoff_guards,
    plans_errors,
    plans_models,
    plans_payloads,
    readiness_gate,
    repository_evidence,
    review_delta,
    review_repair,
    review_session_body,
    review_session_contract,
    workspace_evidence,
)
from .turn_preparation_models import (
    ReviewPreparation,
    RunContext,
    SessionClassification,
)


def classify_session(session_path: Path) -> SessionClassification:
    """Read the selected session's file and say what kind of turn it needs."""
    is_review_session = review_session_contract.is_generated_review_session_file(
        session_path
    )
    return SessionClassification(
        is_review_session=is_review_session,
        is_repair_session=review_repair.is_generated_review_repair_session_file(
            session_path
        ),
        session_kind=(
            plans_payloads.SESSION_KIND_REVIEW
            if is_review_session
            else plans_payloads.SESSION_KIND_IMPLEMENTATION
        ),
    )


def prepare_review_session(
    ctx: RunContext,
    *,
    session: plans_models.SessionStatus,
    snapshot: plans_models.PlanSnapshot,
    classification: SessionClassification,
    cwd: Path,
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
) -> ReviewPreparation:
    """Re-derive a pending review session from today's Git evidence.

    A no-op for anything that is not a generated review session. For one that
    is, the three static claims in its file — the verification contract, the
    scope it says it read, and the per-repository bases it names — are each
    re-derived and rewritten if they moved, and any rewrite is followed by a
    readiness refresh and a fresh inspection so the caller selects against the
    file that now exists.
    """
    if not classification.is_review_session:
        return ReviewPreparation(
            snapshot=snapshot,
            workspace_snapshot=None,
            session_version=None,
            paused=False,
        )

    prepared_snapshot: repository_evidence.WorkspaceSnapshot | None = None
    try:
        review_workspace = ctx.verification_context.workspace
        review_text = session.path.read_text(encoding="utf-8")
        refreshed_text = review_session_contract.refreshed_generated_review_contract(
            review_text,
            session.session_id,
            plan_dir=snapshot.plan_dir,
            repository_aliases=(
                review_workspace.registry.aliases
                if review_workspace is not None
                else ()
            ),
        )
        contract_refreshed = refreshed_text != review_text
        if contract_refreshed:
            session.path.write_text(refreshed_text, encoding="utf-8")
        rescoped = review_delta._refresh_pending_review_scope(
            session=session,
            event_path=ctx.event_path,
            plan_dir=snapshot.plan_dir,
            cwd=cwd,
            workspace=ctx.verification_context.workspace,
            run_id=ctx.run_id,
            persistence=ctx.persistence,
        )
        base_refreshed = False
        if review_workspace is not None:
            try:
                prepared_snapshot = (
                    handoff_guards._capture_workspace_write_guard_snapshot(
                        workspace=review_workspace,
                        plan_dir=snapshot.plan_dir,
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                    )
                )
            except repository_evidence.RepositoryEvidenceError as error:
                raise plans_errors.PlanError(
                    f"combined workspace review base capture failed: {error}"
                ) from error
            unavailable = workspace_evidence._workspace_snapshot_unavailable(
                prepared_snapshot,
                required_repository_paths=tuple(
                    repository.repository.path
                    for repository in review_workspace.repositories
                ),
            )
            if unavailable:
                raise plans_errors.PlanError(
                    "combined workspace review base is unavailable: "
                    + json.dumps(unavailable, sort_keys=True)
                )
            review_bases = workspace_evidence._workspace_review_bases(
                review_workspace,
                prepared_snapshot,
            )
            review_text = session.path.read_text(encoding="utf-8")
            refreshed_text = review_session_body.refreshed_review_repository_base(
                review_text, review_workspace, review_bases
            )
            if refreshed_text != review_text:
                session.path.write_text(refreshed_text, encoding="utf-8")
                base_refreshed = True
        if contract_refreshed or rescoped or base_refreshed:
            readiness_gate._refresh_active_readiness_state(
                ctx.payload, snapshot.plan_dir, ctx.persistence
            )
            snapshot = ctx.plan_cache.get_fresh(snapshot.plan_dir)
    except (plans_errors.PlanError, OSError) as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="malformed-plan",
            message="the pending review session's scope could not be "
            f"re-derived from current Git evidence: {error}",
            details={
                **plans_payloads.session_details_payload(session.id),
                "error": str(error),
            },
        )
        return ReviewPreparation(
            snapshot=snapshot,
            workspace_snapshot=None,
            session_version=None,
            paused=True,
        )

    return ReviewPreparation(
        snapshot=snapshot,
        workspace_snapshot=prepared_snapshot,
        session_version=review_session_contract.generated_review_session_version(
            session.path
        ),
        paused=False,
    )
