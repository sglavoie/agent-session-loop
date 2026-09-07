"""The three payloads a run is born with, and the shape every writer inherits.

A run's durable record is not assembled at the end; it is *seeded once*, before
the first turn, and mutated in place for the rest of the run. This module owns
that seeding. Everything downstream — the turn recorder, the commit settler,
the review cycle, the guards, the outcome printer, and the resume overlay a
later `asl run --resume` builds — writes into the dictionary
:func:`_initial_payload` returns, so the key set that function declares *is* the
schema of ``state.json`` and of the ``run`` command's response. Moving it here,
after the writers themselves left the runner, finally puts the payload's owner
in the same layer as the code that mutates it.

Three payloads live at this boundary:

* :func:`_initial_payload` builds the run's state document. It is called once,
  under the run's lock, before any turn executes.
* :func:`_run_config_payload` builds the ``run_config`` block nested inside it —
  a snapshot of the resolved run flags, keyed by ``run_flags.RUN_FLAG_SPEC``
  dest so a resume can seed an ``argparse.Namespace`` by attribute name with no
  translation table in between.
* :func:`_dry_run_payload` builds the ``planned`` block for ``--dry-run``: the
  prompt, the harness invocation, and the selection the next turn *would* use,
  rendered through exactly the builders a real turn would call so the preview
  cannot drift from the thing it previews. It writes nothing.

## The payload contract

The contract :func:`_initial_payload` states for every later writer is that
**every key a writer indexes is already there, with its empty value of the right
type**. That is why the function seeds `turns`, `commits`, `commit_groups`,
`completed_sessions`, `completed_session_ids`, `review_cycles` and
`model_projection` to `[]`, `details`, `pause_details` and `control` to their
empty mappings, and roughly two dozen scalars — `active_turn`,
`active_verification`, `action_required`, `pause_reason`, `review_verdict`,
`review_resume`, `review_reopen`, `readiness_gate`, `run_finished_at`,
`duration_seconds` — to ``None`` rather than leaving them out. A writer may
therefore say ``payload["turns"].append(...)`` (``turn_execution``),
``payload["commits"].append(...)`` (``commit_settlement``) or
``payload["completed_sessions"].append(...)`` without a presence check, and a
reader may say ``if payload["turns"]:`` without a ``.get``. The four keys that
*are* reached through ``setdefault`` elsewhere in the package —
``repair_commit_preflights``, ``commit_settlement.COMMIT_FAILURES_KEY``, and the
defensive duplicates of ``commit_groups`` and ``review_cycles`` — are the
exception that proves the rule: the first two are the only run-state keys this
function deliberately does not seed, because they exist only on runs that reach
those features.

Two further parts of the contract are easy to break by tidying:

* **The two counters are seeded side by side and are not interchangeable.**
  ``agent_mux_sequence`` counts notifications; ``agent_mux_projection`` s
  generation key counts durable transitions. Neither is a stand-in for the
  other, which is why they are seeded as two statements rather than one.
* **``event_path`` and ``events_path`` are both written, to the same value.**
  The pair is a compatibility surface for readers of either spelling; dropping
  either one is an observable change to ``state.json``.

Durable records this boundary writes: **none**. All three functions are pure
builders that return a dictionary; the write of ``state.json`` belongs to
``run_persistence``, and the caller performs it. ``tests/test_asl_run_payloads.py``
pins that as a flat prohibition, so a first writer appearing here is a test
failure rather than a review catch.

Nothing here imports the runner, and this boundary needs no Protocol seam of its
own — the fourth extraction in this program that needs none, after
``external_write_evidence``, ``workspace_evidence`` and ``handoff_correction``.
The parsed arguments, the plan snapshot, the resolved paths, the lock info, the
run id, the commit mode, the mailbox activation and the verification context all
enter as parameters, and every collaborator is read at its true owner: the flag
readers at ``run_inputs``/``run_selection``/``readiness_gate``/``review_guard``,
the clock at ``clock``, the snapshot and session-kind values at ``plans``, the
turn paths and observed-model block at ``turn_execution``, the seeded control
block at ``run_coordination``, and the review-verdict repair references at
``review_transitions``.

Invariants that survive the move:

* **A dry run is never audit-eligible.** :func:`_initial_payload` passes
  ``eligible=not args.dry_run`` to the assurance summary, because eligibility
  begins with the run id and run directory of a loop that actually executes.
* **The preview renders through the real builders.** :func:`_dry_run_payload`
  chooses between the registered task packet and the prompt builders by the same
  ``task_packet._worker_mode()`` switch a real turn uses, and it refuses a review
  session with no ``--review-harness`` with the same :class:`RunError` the loop
  would raise rather than previewing a turn that could not run.
* **The resume block covers the flag spec.** ``_apply_resume_config`` restores
  only dests it finds in this block and skips the rest without complaint, so
  adding a run flag whose resolved value a resume must recover means adding a
  key to :func:`_run_config_payload`; adding it anywhere else leaves the flag
  invisible to ``--resume``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    agent_mux_projection,
    clock,
    events,
    overseer_discovery,
    plans,
    qna,
    readiness_gate,
    responses,
    review_delta,
    review_guard,
    review_session_contract,
    review_transitions,
    run_commit,
    run_coordination,
    run_inputs,
    run_prompts,
    run_selection,
    state,
    supervisor_audit,
    task_packet,
    turn_execution,
    verification,
)
from .run_errors import RunError


def _run_config_payload(
    args: argparse.Namespace,
    *,
    cwd: Path,
    commit_mode: str | None,
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    """Snapshot the resolved run flags so ``asl run --resume`` can rehydrate.

    Keys are spelled as ``RUN_FLAG_SPEC`` dests so the resume overlay --
    ``_apply_resume_config``, which reads this block and is not part of this
    boundary -- can ``setattr`` a namespace by attribute name without any
    translation. That overlay skips any dest this block omits, silently, so
    every dest in ``_RESUME_OVERLAY_DESTS`` must have a key here or the flag it
    names is lost on resume; ``tests/test_asl_run_payloads.py`` pins the
    containment. This is the additive ``run_config`` block persisted at the top
    level of ``state.json``; it captures the flags that were
    previously invisible to a paused-run resume (``timeout``, ``max_sessions``,
    the resolved ``unsafe_bypass``, and the ``state_dir`` override) alongside the
    harness/model/effort selection that a default resume would otherwise reset
    to the ``codex`` defaults.
    """
    context = verification_context or verification.VerificationContext.legacy(cwd)
    return {
        "harness": args.harness,
        "harness_model": getattr(args, "harness_model", None),
        "spark": bool(getattr(args, "spark", False)),
        "review_harness": getattr(args, "review_harness", None),
        "review_harness_model": getattr(args, "review_harness_model", None),
        "effort": getattr(args, "effort", None),
        "timeout": getattr(args, "timeout", None),
        "max_sessions": getattr(args, "max_sessions", None),
        "max_pending_sessions": getattr(args, "max_pending_sessions", None),
        "max_repair_group_attempts": run_inputs._max_repair_group_attempts(args),
        "max_review_fix_cycles": run_inputs._max_review_fix_cycles(args),
        "max_review_fix_cycles_ceiling": run_inputs._max_review_fix_cycles_ceiling(
            args
        ),
        "review_guard_exclude": list(review_guard._review_guard_excludes(args)),
        "review_guard_include_ignored": review_guard._review_guard_include_ignored(
            args
        ),
        "commit_mode": commit_mode,
        "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
        "unsafe_bypass": run_selection._unsafe_bypass(args),
        "no_mailbox": run_selection._no_mailbox(args),
        "allow_not_loop_ready": readiness_gate._allow_not_loop_ready(args),
        "note": getattr(args, "note", None),
        "cwd": str(cwd),
        "state_dir_override": getattr(args, "state_dir", None),
        **supervisor_audit.config_from_namespace(args).to_run_config(),
        **run_prompts._workspace_payload_fields(context),
    }


def _initial_payload(
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    state_dir: Path,
    run_dir: Path,
    event_path: Path,
    state_path: Path,
    lock_info: state.LockInfo,
    cwd: Path,
    run_id: str,
    commit_mode: str | None,
    mailbox_activation: Mapping[str, Any],
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    context = verification_context or verification.VerificationContext.legacy(cwd)
    model_effort = run_selection._selected_model_effort_payload(args)
    spark = run_selection._safe_initial_spark_payload(args)
    review_harness = getattr(args, "review_harness", None)
    review_requested = review_harness is not None
    review_output_path = (
        str(review_session_contract.final_review_output_path(snapshot.plan_dir))
        if review_requested
        else None
    )
    # A dry run never becomes audit-eligible: eligibility starts with the run ID
    # and run directory of a loop that actually executes.
    assurance = supervisor_audit.initial_summary(
        supervisor_audit.plan_audit(args),
        run_id=run_id,
        eligible=not args.dry_run,
    )
    return responses.success_payload(
        "run",
        paused=False,
        details={},
        dry_run=args.dry_run,
        reason="",
        pause_reason=None,
        pause_message="",
        pause_details={},
        run_id=run_id,
        run_started_at=clock.isoformat(clock.utc_now()),
        run_finished_at=None,
        # When the pause happened, stamped by `run_termination.set_pause`. The
        # overseer's fleet inventory dates a pause from this rather than from
        # the event log, which it may not open before authorization, so a run
        # that never publishes the slot cannot be placed in the window at all.
        **{overseer_discovery.PAUSED_AT_KEY: None},
        # Which discovery vocabulary this record speaks. Absence is what makes a
        # record legacy rather than merely truncated, so it is seeded here at
        # birth and never written conditionally.
        **{
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            )
        },
        agent_mux_sequence=0,
        # The lifecycle sequence counts notifications; the projection
        # generation counts durable transitions. Seeded separately and side by
        # side so neither is ever read as a stand-in for the other.
        **{agent_mux_projection.GENERATION_KEY: 0},
        duration_seconds=None,
        active_turn=None,
        active_verification=None,
        action_required=None,
        approval_mode=run_selection._approval_mode(args),
        commit_mode=commit_mode,
        repair_commit_mode=run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
        max_pending_sessions=getattr(args, "max_pending_sessions", None),
        note=getattr(args, "note", None),
        run_config=_run_config_payload(
            args,
            cwd=cwd,
            commit_mode=commit_mode,
            verification_context=context,
        ),
        assurance=assurance.to_dict(),
        no_mailbox=run_selection._no_mailbox(args),
        mailbox_enabled=bool(mailbox_activation.get("enabled")),
        mailbox_activation=dict(mailbox_activation),
        plan_dir=str(snapshot.plan_dir),
        cwd=str(cwd),
        kitty_window_id=run_selection._kitty_window_id_from_env(),
        harness=args.harness,
        spark=spark,
        **model_effort,
        **turn_execution.observed_model_payload(None),
        model_projection=[],
        state_dir=str(state_dir),
        run_dir=str(run_dir),
        event_path=str(event_path),
        events_path=str(event_path),
        state_path=str(state_path),
        lock_path=str(lock_info.path),
        harness_config=run_selection._safe_harness_config_payload(),
        completed_sessions=[],
        completed_session_ids=[],
        commits=[],
        commit_groups=[],
        turns=[],
        control=run_coordination.initial_control_payload(),
        initial_snapshot=plans.snapshot_payload(snapshot),
        final_snapshot=plans.snapshot_payload(snapshot),
        allow_not_loop_ready=readiness_gate._allow_not_loop_ready(args),
        readiness_gate=None,
        readiness_state_initial=None,
        readiness_state_final=None,
        review_requested=review_requested,
        review_harness=review_harness,
        review_session_id=None,
        review_session_executed=None,
        review_output_path=review_output_path,
        review_completed=False,
        review_verdict=None,
        review_skipped_reason=None,
        max_review_fix_cycles=run_inputs._max_review_fix_cycles(args),
        review_cycles=[],
        review_fix_disposition=None,
        review_resume=None,
        review_reopen=None,
        **run_prompts._workspace_payload_fields(context),
    )


def _dry_run_payload(
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
    cwd: Path,
    run_dir: Path,
    mailbox_activation: Mapping[str, Any],
    reply_text: str | None = None,
    reply_context: qna.QnAContext | None = None,
    reply_origin: str = run_prompts.REPLY_ORIGIN_ARGV,
    verification_context: verification.VerificationContext | None = None,
) -> dict[str, Any]:
    context = verification_context or verification.VerificationContext.legacy(cwd)
    review_harness = getattr(args, "review_harness", None)
    review_requested = review_harness is not None
    review_output_path = (
        str(review_session_contract.final_review_output_path(snapshot.plan_dir))
        if review_requested
        else None
    )
    next_pending = snapshot.next_pending
    mailbox_enabled = bool(mailbox_activation.get("enabled"))
    pending_guidance = run_selection._pending_guidance_snapshot(
        enabled=mailbox_enabled,
        plan_dir=snapshot.plan_dir,
        state_dir_arg=getattr(args, "state_dir", None),
    )
    audit = supervisor_audit.plan_audit(args).to_dict()
    if next_pending is None:
        model_effort = run_selection._selected_model_effort_payload(args)
        spark = run_selection._safe_initial_spark_payload(args)
        return {
            "next_pending": None,
            "next_pending_id": None,
            "prompt": None,
            "invocation": None,
            "resolver_prompt": None,
            "prompt_kind": "session",
            "reply_source": None,
            "audit": audit,
            "no_mailbox": run_selection._no_mailbox(args),
            "mailbox_enabled": mailbox_enabled,
            "mailbox_activation": dict(mailbox_activation),
            "review_requested": review_requested,
            "review_harness": review_harness,
            "review_output_path": review_output_path,
            "spark": spark,
            **model_effort,
            **run_prompts._workspace_payload_fields(context),
        }

    is_review_session = review_session_contract.is_generated_review_session_file(
        next_pending.path
    )
    turn_harness = args.review_harness if is_review_session else args.harness
    if turn_harness is None:
        raise RunError("--review-harness is required for a review session")
    spark = run_selection._safe_initial_spark_payload(args)
    requested_model, model_effort, spark_route = run_selection._spark_turn_route(
        args,
        plan_dir=snapshot.plan_dir,
        session_path=next_pending.path,
        session_id=next_pending.id,
        harness=turn_harness,
        is_review_turn=is_review_session,
        implementation_overrides={},
        spark_state=spark,
        run_suppression=None,
    )
    spark["active_route"] = spark_route
    paths = turn_execution.turn_paths(run_dir, turn_index=1, session_id=next_pending.id)
    workspace = context.workspace
    session_scope = (
        run_prompts._session_scope(next_pending, workspace)
        if workspace is not None
        else None
    )
    use_registered_packet = (
        workspace is not None
        and task_packet._worker_mode() == task_packet.WORKER_MODE_PACKET
    )
    packet_kwargs: dict[str, Any] = {}
    if use_registered_packet:
        dry_run_event_path = (
            state.plan_state_dir(snapshot.plan_dir, getattr(args, "state_dir", None))
            / events.EVENTS_FILE_NAME
        )
        repair_references = review_delta._verification_repair_state(
            dry_run_event_path,
            next_pending.id,
        )["references"]
        if is_review_session:
            repair_references = repair_references + (
                review_transitions.review_verdict_repair_references(
                    dry_run_event_path, next_pending.id
                )
            )
        packet_kwargs = {
            "session_id": next_pending.id,
            "session_kind": (
                plans.SESSION_KIND_REVIEW
                if is_review_session
                else plans.SESSION_KIND_IMPLEMENTATION
            ),
            "predecessor_id": task_packet._predecessor_session_id(
                snapshot, next_pending.id
            ),
            "has_expansion": task_packet._session_has_expansion(
                snapshot.plan_dir, next_pending.id
            ),
            "has_progress": (snapshot.plan_dir / plans.PROGRESS_FILE_NAME).is_file(),
            "repair_references": repair_references,
            "mailbox_enabled": mailbox_enabled,
            "state_dir": getattr(args, "state_dir", None),
            "pending_guidance": pending_guidance,
            "workspace": workspace,
            "session_scope": session_scope,
        }
    if reply_context is not None and reply_text is not None:
        if use_registered_packet:
            prompt = task_packet.build_task_packet(
                turn_harness,
                snapshot.plan_dir,
                reply=reply_text,
                reply_context=reply_context,
                **packet_kwargs,
            ).render()
        else:
            prompt = run_prompts.build_reply_prompt(
                turn_harness,
                snapshot.plan_dir,
                reply=reply_text,
                context=reply_context,
                mailbox_enabled=mailbox_enabled,
                state_dir=getattr(args, "state_dir", None),
                pending_guidance=pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        prompt_kind = "reply"
        resolver_prompt: str | None = qna.render_resolver_prompt(reply_context)
        reply_source: dict[str, Any] | None = {
            "run_id": reply_context.run_id,
            "session_id": reply_context.session_id,
            "turn_index": reply_context.turn_index,
            "last_message_path": str(reply_context.last_message_path),
            "reply_length": len(reply_text),
            "origin": reply_origin,
        }
    else:
        if use_registered_packet:
            prompt = task_packet.build_task_packet(
                turn_harness,
                snapshot.plan_dir,
                **packet_kwargs,
            ).render()
        else:
            prompt = run_prompts.build_session_prompt(
                turn_harness,
                snapshot.plan_dir,
                mailbox_enabled=mailbox_enabled,
                state_dir=getattr(args, "state_dir", None),
                pending_guidance=pending_guidance,
                workspace=workspace,
                session_scope=session_scope,
            )
        prompt_kind = "session"
        resolver_prompt = None
        reply_source = None
    invocation = run_prompts.build_harness_invocation(
        turn_harness,
        cwd=cwd,
        plan_dir=snapshot.plan_dir,
        unsafe_bypass=run_selection._unsafe_bypass(args),
        last_message_path=paths["last_message"],
        model=requested_model,
        effort=args.effort,
        prompt=prompt,
    )
    return {
        "next_pending": plans.session_compat_value(next_pending.id),
        "next_pending_id": next_pending.id,
        "prompt": prompt,
        "invocation": run_commit._build_invocation_payload(
            invocation,
            model_effort=model_effort,
            verification_context=context,
        ),
        "resolver_prompt": resolver_prompt,
        "prompt_kind": prompt_kind,
        "reply_source": reply_source,
        "audit": audit,
        "no_mailbox": run_selection._no_mailbox(args),
        "mailbox_enabled": mailbox_enabled,
        "mailbox_activation": dict(mailbox_activation),
        "review_requested": review_requested,
        "review_harness": review_harness,
        "review_output_path": review_output_path,
        "spark": spark,
        "spark_route": spark_route,
        **model_effort,
        **run_prompts._workspace_payload_fields(context),
    }
