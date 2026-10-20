"""Check conversation continuity and prepare eligible repair children."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import (
    conversation_resume,
    events,
    plans_models,
    plans_payloads,
    plans_session_scope,
    readiness_gate,
    review_guard,
    run_inputs,
    run_prompts,
    state,
    workspace_evidence,
    workspaces,
    write_guards_repair,
)
from .turn_preparation_models import RepairResumption, RunContext, SessionClassification

# ---------------------------------------------------------------------------
# Which conversation this turn runs on
# ---------------------------------------------------------------------------


def prepare_turn_continuation(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    args: argparse.Namespace,
    cwd: Path,
    harness: str,
    session_id: str,
    session_details: dict[str, Any],
    session_scope: plans_session_scope.SessionScope | None,
    workspace: workspaces.ResolvedWorkspace | None,
    authorization: conversation_resume.FreshConversationAuthorization,
    interrupted_operations: Sequence[str] = (),
) -> conversation_resume.TurnContinuation:
    """Decide this turn's conversation, record the decision, and pause if refused.

    Placed here rather than in the run loop for the same reason
    :func:`prepare_turn_prompt` is: it is a decision made from the plan and the
    run's durable state, before anything is launched, and the loop's job is to
    honour ``paused`` rather than to re-derive when a turn may start.

    The binding is rebuilt from *this* run's workspace, access profile and
    writable aliases every turn — never carried forward — because the whole
    point of the check is to notice when one of them stopped matching what the
    conversation was recorded under.

    ``interrupted_operations`` names what a previous attempt was cut short in
    the middle of, by identifier only. It is the redirect journal's ids on a
    turn that succeeds a corrected one and empty everywhere else, and it is
    passed rather than looked up here because which correction a turn carries is
    the loop's bookkeeping, not this decision's.
    """
    plan_state = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
    binding = conversation_resume.turn_binding(
        plan_dir=snapshot.plan_dir,
        session_id=session_id,
        harness=harness,
        cwd=cwd,
        workspace=workspace,
        # The *writable* aliases only. A session whose advisory read set
        # widened is still doing the same work in the same places; one whose
        # write set changed is not, and continuing an agent that believes it
        # may still write somewhere it no longer may is the failure this field
        # exists to catch.
        scope_aliases=() if session_scope is None else tuple(session_scope.writes),
    )
    continuation = conversation_resume.plan_turn_continuation(
        plan_state,
        binding=binding,
        authorization=authorization,
        interrupted_operations=interrupted_operations,
    )
    ctx.persistence.append_event(
        ctx.event_path,
        events.CONVERSATION_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            **plans_payloads.session_identity_payload(session_id),
            **continuation.describe(),
        },
    )
    if not continuation.paused:
        return continuation
    ctx.record_final_snapshot(snapshot)
    ctx.pause(
        reason=conversation_resume.RECOVERY_PAUSE_REASON,
        message=conversation_resume.recovery_message(continuation),
        details={
            **session_details,
            "conversation_recovery": continuation.describe(),
        },
    )
    return continuation


def resume_repair_session(
    ctx: RunContext,
    *,
    session: plans_models.SessionStatus,
    snapshot: plans_models.PlanSnapshot,
    classification: SessionClassification,
    args: argparse.Namespace,
    cwd: Path,
    workspace: workspaces.ResolvedWorkspace | None,
    session_scope: plans_session_scope.SessionScope | None,
    session_label: str,
    session_identity: dict[str, Any],
    session_details: dict[str, Any],
    retained_scope_repair: dict[str, Any] | None,
    max_review_fix_cycles: int,
    review_requested: bool,
) -> RepairResumption:
    """Settle a generated repair child's scope, eligibility and clean tree.

    A no-op for anything that is not a generated repair child. For one that
    is, three things happen in an order the child depends on: its durable
    write scope is widened first so everything downstream reads one scope, the
    continuation then decides whether the child may run at all, and the commit
    preflight last proves the writable set is clean enough to commit what the
    child produces.
    """
    if not classification.is_repair_session:
        return RepairResumption(
            session_scope=session_scope,
            regenerated_write_scope=None,
            paused=False,
        )

    regenerated_write_scope: dict[str, Any] | None = None
    if workspace is not None:
        assert session_scope is not None
        # The durable child file is the single description of what this child
        # may write. Widening it here — ahead of commit preflight, the task
        # packet, the persisted prompt and the guard — is what keeps all four
        # reading one scope instead of preflight and the packet describing the
        # pre-repair one.
        regenerated_write_scope = (
            workspace_evidence._regenerated_repair_child_write_scope(
                session=session,
                plan_dir=snapshot.plan_dir,
                workspace=workspace,
                scope=session_scope,
            )
        )
        if regenerated_write_scope is not None:
            readiness_gate._refresh_active_readiness_state(
                ctx.payload, snapshot.plan_dir, ctx.persistence
            )
            session_scope = run_prompts._session_scope(session, workspace)
    repair_resume = write_guards_repair._pending_repair_child_continuation(
        snapshot=snapshot,
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        workspace=workspace,
        max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
        max_review_fix_cycles=max_review_fix_cycles,
        review_requested=review_requested,
    )
    if repair_resume.action == "run-repairs" and not repair_resume.eligible:
        # The continuation is the only view that reconciles every pending
        # child with the artifact they were derived from. A child it refuses
        # is not launched under the scope that refusal describes.
        repair_blocker = repair_resume.blocker or "unknown blocker"
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            message="generated repair child "
            f"{session_label} cannot be run safely: "
            f"{repair_blocker}.",
            details={
                **session_details,
                "review_resume": repair_resume.to_payload(),
            },
        )
        return RepairResumption(
            session_scope=session_scope,
            regenerated_write_scope=regenerated_write_scope,
            paused=True,
        )
    try:
        repair_commit_preflight = review_guard._repair_commit_preflight(
            cwd,
            verification_context=ctx.verification_context,
            session_scope=session_scope,
            workspace_evidence=workspace_evidence._workspace_write_guard_evidence,
            plan_dir=snapshot.plan_dir,
            retained_scope_repair=retained_scope_repair,
        )
    except review_guard.RepairCommitPreflightError as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="commit-preflight",
            message=f"repair commit preflight failed before {session_label}: {error}",
            details={**session_details, **error.details},
        )
        return RepairResumption(
            session_scope=session_scope,
            regenerated_write_scope=regenerated_write_scope,
            paused=True,
        )
    with ctx.persistence.lock:
        ctx.payload.setdefault("repair_commit_preflights", []).append(
            {
                **session_identity,
                **repair_commit_preflight,
            }
        )
        ctx.persistence.append_event(
            ctx.event_path,
            events.REPAIR_COMMIT_PREFLIGHT_EVENT,
            {
                "run_id": ctx.run_id,
                **session_identity,
                **repair_commit_preflight,
            },
        )
        ctx.persistence.write_state(ctx.payload)
    return RepairResumption(
        session_scope=session_scope,
        regenerated_write_scope=regenerated_write_scope,
        paused=False,
    )
