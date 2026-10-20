"""Collect packet inputs and build the prompt subject to prelaunch gates."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from . import (
    events,
    plans_models,
    plans_progress,
    plans_session_scope,
    preflight_footprint,
    redirect_ack_carry,
    review_delta,
    review_transitions,
    run_inputs,
    run_prompts,
    run_selection,
    task_packet,
    turn_execution_argv,
    workspaces,
)
from .turn_preparation_models import (
    PacketInputs,
    RunContext,
    SessionClassification,
    TurnPrompt,
)


def collect_packet_inputs(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    classification: SessionClassification,
    args: argparse.Namespace,
    session_id: str,
    mailbox_enabled: bool,
) -> PacketInputs:
    """Read what the selected session's prompt will need, whatever its shape.

    Autonomous turns default to Python-built task packets (packet mode);
    ``ASL_WORKER_MODE=skill`` restores the legacy ``session-run`` skill
    invocation as a temporary rollback. Either way the manual ``$session-run``
    path is unchanged.
    """
    repair_references: tuple[str, ...] = tuple(
        review_delta._verification_repair_state(ctx.event_path, session_id)[
            "references"
        ]
    )
    if classification.is_review_session:
        repair_references = repair_references + (
            review_transitions.review_verdict_repair_references(
                ctx.event_path, session_id
            )
        )
    return PacketInputs(
        worker_mode=task_packet._worker_mode(),
        predecessor_id=task_packet._predecessor_session_id(snapshot, session_id),
        has_expansion=task_packet._session_has_expansion(snapshot.plan_dir, session_id),
        has_progress=(snapshot.plan_dir / plans_progress.PROGRESS_FILE_NAME).is_file(),
        repair_references=repair_references,
        pending_guidance=run_selection._pending_guidance_snapshot(
            enabled=mailbox_enabled,
            plan_dir=snapshot.plan_dir,
            state_dir_arg=args.state_dir,
        ),
    )


def prepare_turn_prompt(
    ctx: RunContext,
    *,
    snapshot: plans_models.PlanSnapshot,
    classification: SessionClassification,
    inputs: PacketInputs,
    args: argparse.Namespace,
    cwd: Path,
    harness: str,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    session_scope: plans_session_scope.SessionScope | None,
    workspace: workspaces.ResolvedWorkspace | None,
    mailbox_enabled: bool,
    reply: run_prompts._PendingReply | None,
    resolution: run_prompts._PendingResolution | None,
    corrections: redirect_ack_carry.CarriedCorrections | None = None,
) -> TurnPrompt:
    """Render the prompt this turn carries, then judge what it reaches.

    Three prompt shapes, each in two modes: a reply continuation, a
    resolution continuation, or a fresh session start, rendered as a task
    packet by default and through the legacy ``session-run`` skill under
    ``ASL_WORKER_MODE=skill``. ``corrections`` cuts across all three: an
    operator may correct a turn that was itself resuming, so the correction
    block is rendered ahead of whichever shape this turn has rather than being
    a fourth one. The repository-intent gate runs last because it judges the
    rendered text rather than the plan the text came from, and only for a
    legacy run — a registered workspace states its repositories in the registry
    instead.
    """
    if reply is not None and resolution is not None:
        raise RuntimeError("reply and resolution continuations are mutually exclusive")
    if reply is not None:
        reply_context = reply.context
        reply_text = reply.text
        if inputs.worker_mode == task_packet.WORKER_MODE_SKILL:
            prompt = run_prompts.build_reply_prompt(
                harness,
                snapshot.plan_dir,
                reply=reply_text,
                context=reply_context,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
                corrections=corrections,
            )
        else:
            prompt = task_packet.build_task_packet(
                harness,
                snapshot.plan_dir,
                session_id=session_id,
                session_kind=classification.session_kind,
                predecessor_id=inputs.predecessor_id,
                has_expansion=inputs.has_expansion,
                has_progress=inputs.has_progress,
                reply=reply_text,
                reply_context=reply_context,
                repair_references=inputs.repair_references,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
                corrections=corrections,
            ).render()
        prompt_kind = "reply"
        # Built as a plain mapping and only then narrowed to the optional the
        # result carries, so the three conditional keys are assigned into a
        # dict rather than into something that could be None.
        reply_identity: dict[str, Any] = {
            "run_id": reply_context.run_id,
            "session_id": reply_context.session_id,
            "turn_index": reply_context.turn_index,
            "last_message_path": str(reply_context.last_message_path),
            "reply_length": len(reply_text),
            "origin": reply.origin,
        }
        if reply.question_id is not None:
            reply_identity["question_id"] = reply.question_id
        if reply.reply_message_id is not None:
            reply_identity["reply_message_id"] = reply.reply_message_id
        if reply.reply_seq is not None:
            reply_identity["reply_seq"] = reply.reply_seq
        reply_source: dict[str, Any] | None = reply_identity
        persisted_prompt = turn_execution_argv.reply_argv_placeholder(len(reply_text))
        resolution_source: dict[str, Any] | None = None
    elif resolution is not None:
        resolution_context = resolution.context
        if inputs.worker_mode == task_packet.WORKER_MODE_SKILL:
            prompt = run_prompts.build_resolution_prompt(
                harness,
                snapshot.plan_dir,
                action=resolution.action,
                note=resolution.note,
                context=resolution_context,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
                corrections=corrections,
            )
        else:
            prompt = task_packet.build_task_packet(
                harness,
                snapshot.plan_dir,
                session_id=session_id,
                session_kind=classification.session_kind,
                predecessor_id=inputs.predecessor_id,
                has_expansion=inputs.has_expansion,
                has_progress=inputs.has_progress,
                resolution_action=resolution.action,
                resolution_note=resolution.note,
                resolution_context=resolution_context,
                repair_references=inputs.repair_references,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
                corrections=corrections,
            ).render()
        prompt_kind = "resolution"
        reply_source = None
        resolution_source = task_packet._resolution_source_metadata(resolution)
        persisted_prompt = turn_execution_argv.resolution_argv_placeholder(
            resolution.action,
            resolution_source["note_length"],
        )
    else:
        if inputs.worker_mode == task_packet.WORKER_MODE_SKILL:
            prompt = run_prompts.build_session_prompt(
                harness,
                snapshot.plan_dir,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
                corrections=corrections,
            )
        else:
            prompt = task_packet.build_task_packet(
                harness,
                snapshot.plan_dir,
                session_id=session_id,
                session_kind=classification.session_kind,
                predecessor_id=inputs.predecessor_id,
                has_expansion=inputs.has_expansion,
                has_progress=inputs.has_progress,
                repair_references=inputs.repair_references,
                mailbox_enabled=mailbox_enabled,
                state_dir=args.state_dir,
                pending_guidance=inputs.pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
                corrections=corrections,
            ).render()
        prompt_kind = "session"
        reply_source = None
        resolution_source = None
        persisted_prompt = prompt
    packet_intent_report = (
        None
        if workspace is not None
        else run_inputs._task_packet_repository_intent_report(
            prompt,
            root=cwd,
            artifact_paths=run_inputs._task_packet_artifact_paths(
                snapshot.plan_dir, session_id, inputs.predecessor_id
            ),
            exempt_roots=run_inputs._runner_owned_roots(
                snapshot.plan_dir, args.state_dir, run_dir=ctx.run_dir
            ),
            provenance_roots=preflight_footprint.plan_provenance_exempt_roots(
                snapshot.plan_dir
            ),
        )
    )
    if packet_intent_report is not None:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            message=run_inputs._repository_intent_message(
                packet_intent_report,
                subject=f"task packet for {session_label}",
            ),
            details={
                **session_details,
                "repository_intent": packet_intent_report,
                "repository_intent_source": "task-packet",
            },
        )
        return TurnPrompt(
            prompt=prompt,
            prompt_kind=prompt_kind,
            persisted_prompt=persisted_prompt,
            reply_source=reply_source,
            resolution_source=resolution_source,
            paused=True,
        )
    state_root_report = run_inputs._session_plan_store_state_root_report(
        snapshot.plan_dir,
        session_id,
        state_dir_arg=args.state_dir,
        run_dir=ctx.run_dir,
    )
    if state_root_report is not None:
        ctx.record_final_snapshot(snapshot)
        required = ", ".join(state_root_report["required_state_roots"])
        ctx.pause(
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            message=(
                f"{session_label} requires plan-store writes with --state-dir "
                f"{required}, outside this run's effective guard roots; "
                "no worker was launched. Inspect the requirement and relaunch "
                "with the explicit state-root configuration, or repair the "
                "completion contract before retrying"
            ),
            details={**session_details, "plan_store_state_root": state_root_report},
        )
        return TurnPrompt(
            prompt=prompt,
            prompt_kind=prompt_kind,
            persisted_prompt=persisted_prompt,
            reply_source=reply_source,
            resolution_source=resolution_source,
            paused=True,
        )
    return TurnPrompt(
        prompt=prompt,
        prompt_kind=prompt_kind,
        persisted_prompt=persisted_prompt,
        reply_source=reply_source,
        resolution_source=resolution_source,
        paused=False,
    )
