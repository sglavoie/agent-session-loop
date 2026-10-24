"""Setup phase: open the run state and register the control surfaces."""

from __future__ import annotations

import functools
import sys
import threading
from pathlib import Path
from typing import Any

from . import (
    commit_groups,
    commit_settlement,
    conversation_resume,
    events,
    guards,
    plans_errors,
    plans_handoff,
    plans_snapshot,
    qna,
    review_guard,
    review_repair,
    run_commit,
    run_control,
    run_coordination_control_drain,
    run_coordination_mailbox_poll,
    run_coordination_reply_state,
    run_coordination_terminal,
    run_inputs,
    run_outcome,
    run_payloads,
    run_persistence,
    run_prompts,
    run_reporting,
    run_selection,
    run_termination,
    state,
    state_abandonment,
    supervisor_audit,
    verification_models,
)
from .run_loop_context import RunContext
from .run_loop_control_redirects import (
    _handle_run_control_action,
    _handle_run_control_status,
    _pause_for_redirect_restart,
    _reconcile_stranded_redirects,
)
from .run_loop_frame import RunFrame, RunResult
from .run_loop_terminal import _terminalize


def _open_run_state(frame: RunFrame) -> RunResult | None:
    """Load the plan, build the run context and announce the run."""
    # The caller holds this run's lock and opened the wakeup endpoint before
    # publishing it, so `wakeup` is already being set by producers from here
    # on. A wakeup that lands outside the poll loop below only leaves the
    # event set for the next waiter to observe.
    if frame.verification_context is None:
        frame.verification_context = verification_models.VerificationContext.legacy(
            frame.cwd
        )
    plan_cache = plans_snapshot.PlanSnapshotCache()
    frame.normalized_review_handoffs = list(
        plans_handoff.normalize_generated_review_handoff_artifacts(frame.args.plan_dir)
    )
    frame.executed_review_requires_artifact = False
    # ASI-136: a repair publication that died mid-flight owns this plan's files
    # until it is rolled back, and its children are pending but not loop-ready.
    # Recover under this run's lock, before the first snapshot, so selection
    # never sees a child whose loop-ready state was never established.
    repair_publication_recovery: dict[str, Any] | None = None
    if not frame.args.dry_run:
        try:
            repair_publication_recovery = (
                review_repair.recover_review_repair_publication(frame.args.plan_dir)
            )
        except plans_errors.PlanError as error:
            repair_publication_recovery = {
                "recovered": False,
                "reason": "recovery-failed",
                "error": str(error),
            }
    frame.snapshot = plan_cache.get(frame.args.plan_dir)
    if repair_publication_recovery is not None and not (
        repair_publication_recovery.get("recovered")
    ):
        # The image could not be put back, so the plan on disk is neither the
        # state the publication started from nor a published one. Refuse to run
        # rather than select out of a half-published generation.
        frame.payload = run_outcome._error_payload(
            frame.args,
            reason="malformed-plan",
            message=(
                "an interrupted review repair publication could not be rolled "
                f"back: {repair_publication_recovery.get('reason')}"
            ),
            paused=True,
            cwd=frame.cwd,
            snapshot=frame.snapshot,
        )
        frame.payload["details"] = {
            "review_repair_publication_recovery": repair_publication_recovery
        }
        frame.payload["pause_details"] = dict(frame.payload["details"])
        return 1, frame.payload
    if not frame.args.dry_run:
        run_inputs._ensure_legacy_run_metadata(
            frame.snapshot.plan_dir, frame.verification_context
        )
    frame.state_dir = state.plan_state_dir(
        frame.snapshot.plan_dir, frame.args.state_dir
    )
    run_dir = frame.state_dir / "runs" / frame.run_id
    # The run's command gate. Constructed for every run and left empty until a
    # guard is added, so the drain always has somewhere to apply one and never
    # has to decide whether this run is guardable.
    #
    # The repository binding is supplied here rather than at the first turn
    # because the queue is drained before any turn starts: a protected-path
    # rule naming a declared alias would otherwise be refused as unknown
    # simply for having been queued before the run began.
    guard_controller = guards.GuardController(
        run_id=frame.run_id,
        run_dir=run_dir,
        workspace_aliases=(
            {
                repository.alias: str(repository.root)
                for repository in frame.verification_context.workspace.repositories
            }
            if frame.verification_context.workspace is not None
            else {guards.LEGACY_PATH_QUALIFIER: str(frame.cwd)}
        ),
    )
    event_path = frame.state_dir / events.EVENTS_FILE_NAME
    frame.state_path = frame.state_dir / events.STATE_FILE_NAME
    try:
        frame.unresolved_commit_group = commit_groups.load_unresolved_group(
            state_path=frame.state_path,
            event_path=event_path,
        )
    except commit_groups.CommitGroupError as error:
        frame.unresolved_commit_group = None
        frame.commit_group_load_error = str(error)
    else:
        frame.commit_group_load_error = None
    frame.mailbox_activation = run_selection._mailbox_activation_payload(
        frame.args, frame.snapshot.plan_dir
    )
    frame.mailbox_enabled = bool(frame.mailbox_activation["enabled"])
    frame.review_guard_excludes = review_guard._review_guard_excludes(frame.args)
    frame.review_guard_include_ignored = review_guard._review_guard_include_ignored(
        frame.args
    )
    frame.external_change_aliases = review_guard._external_change_aliases(
        frame.args, frame.verification_context
    )
    # One selection for every commit this run makes, including the recovery of
    # a group a previous process left unresolved: the declared implementation
    # harness, not whichever harness a live switch moved a turn onto.
    commit_generation = run_commit._commit_generation(frame.args)
    audited_repositories, audited_registry_declared = (
        run_termination._audited_repositories(frame.verification_context)
    )
    audit_context = run_coordination_terminal.TerminalAuditContext(
        plan=supervisor_audit.plan_audit(frame.args),
        run_dir=run_dir,
        state_dir=frame.state_dir,
        plan_dir=frame.snapshot.plan_dir,
        workspace_root=(
            frame.cwd
            if frame.verification_context.workspace is None
            else frame.verification_context.workspace.root
        ),
        ledger_state_dir=(
            None if frame.args.state_dir is None else Path(frame.args.state_dir)
        ),
        repositories=audited_repositories,
        registry_declared=audited_registry_declared,
    )

    # Detect state stranded under a moved plan's old path hash *before* this
    # run writes its own state.json (which would suppress the scan). A scan
    # failure must never block the run, so any error yields no candidates.
    try:
        frame.orphan_candidates = state_abandonment.orphaned_state_candidates(
            frame.snapshot, frame.args.state_dir
        )
    # Defensive; the scan is best-effort.
    except Exception:  # noqa: BLE001  # pragma: no cover
        frame.orphan_candidates = []

    frame.reply_context = None
    if frame.reply_text is not None:
        try:
            frame.reply_context = qna.load_qna_context(
                frame.snapshot.plan_dir,
                state_dir=frame.args.state_dir,
            )
        except qna.QnAError as error:
            frame.payload = run_outcome._error_payload(
                frame.args,
                reason=error.reason,
                message=error.error,
                paused=False,
                cwd=frame.cwd,
                snapshot=frame.snapshot,
            )
            frame.payload["details"] = dict(error.details)
            return 1, frame.payload

    frame.payload = run_payloads._initial_payload(
        args=frame.args,
        snapshot=frame.snapshot,
        state_dir=frame.state_dir,
        run_dir=run_dir,
        event_path=event_path,
        state_path=frame.state_path,
        lock_info=frame.lock_info,
        cwd=frame.cwd,
        run_id=frame.run_id,
        commit_mode=frame.commit_mode,
        mailbox_activation=frame.mailbox_activation,
        verification_context=frame.verification_context,
    )
    frame.spark_run_suppression = None
    frame.spark_fallback_reason = None
    if frame.normalized_review_handoffs:
        frame.payload["normalized_review_handoffs"] = [
            str(path) for path in frame.normalized_review_handoffs
        ]
    if frame.unresolved_commit_group is not None:
        commit_settlement._upsert_commit_group(
            frame.payload, frame.unresolved_commit_group
        )
    if frame.commit_group_load_error is not None:
        frame.payload["commit_group_load_error"] = frame.commit_group_load_error
    reply_state_preserver = run_coordination_reply_state.ReplyStatePreserver(
        state_path=frame.state_path,
        event_path=event_path,
        enabled=frame.reply_text is not None and not frame.args.dry_run,
    )
    frame.pending_reply = (
        run_prompts._PendingReply(
            text=frame.reply_text,
            context=frame.reply_context,
            origin=frame.reply_origin,
            process_start=True,
        )
        if frame.reply_text is not None and frame.reply_context is not None
        else None
    )
    # Session 4 wires committed live-wait resolutions into this slot. Keeping
    # a distinct one-shot type here prevents answerless continuations from
    # inheriting reply semantics or leaking into a later turn.
    frame.pending_resolution = None
    # The correction an accepted redirect left for the next attempt of the same
    # session. A third one-shot slot rather than a reuse of either above: this
    # one is not an answer to anything the worker asked, it pins the harness and
    # conversation the interrupted attempt ran on, and it carries no text -- the
    # feedback stays in the redirect journal.
    frame.pending_redirect = None

    persist_run_state = not (frame.args.dry_run and frame.reply_text is not None)
    frame.output = run_reporting._StderrOutputCoordinator(enabled=not frame.args.json)
    frame.ctx = RunContext(
        run_id=frame.run_id,
        plan_dir=frame.snapshot.plan_dir,
        run_dir=run_dir,
        event_path=event_path,
        payload=frame.payload,
        persistence=run_persistence._RunPersistence(
            write_state=persist_run_state,
            append_events=persist_run_state,
        ),
        # The reporter renders commit failures but owns none of the commit
        # lifecycle's vocabulary, so the run hands it the key terminalization
        # writes owed groups under and the labeller that names one.
        progress=run_reporting._ProgressReporter(
            enabled=not frame.args.json,
            output=frame.output,
            commit_group_label=commit_settlement._unresolved_commit_group_label,
            unresolved_groups_key=commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
        ),
        inline_tail=run_reporting._InlineTailController(
            plan_dir=frame.snapshot.plan_dir,
            state_dir=frame.args.state_dir,
            output=frame.output,
        ),
        reply_state_preserver=reply_state_preserver,
        audit_context=audit_context,
        guard_controller=guard_controller,
        plan_cache=plan_cache,
        verification_context=frame.verification_context,
        commit_generation=commit_generation,
    )

    frame.ctx.persistence.append_event(
        frame.ctx.event_path,
        events.RUN_STARTED_EVENT,
        {
            "plan_dir": str(frame.snapshot.plan_dir),
            "harness": frame.args.harness,
            "model": frame.ctx.payload["model"],
            "model_source": frame.ctx.payload["model_source"],
            "effort": frame.ctx.payload["effort"],
            "effort_source": frame.ctx.payload["effort_source"],
            "spark": frame.ctx.payload["spark"],
            "dry_run": frame.args.dry_run,
            "max_sessions": frame.max_sessions,
            "max_pending_sessions": getattr(frame.args, "max_pending_sessions", None),
            "commit_mode": frame.commit_mode,
            "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            "note": frame.ctx.payload.get("note"),
            "run_id": frame.ctx.run_id,
            "run_dir": str(frame.ctx.run_dir),
            "run_started_at": frame.ctx.payload["run_started_at"],
            "approval_mode": frame.ctx.payload["approval_mode"],
            "no_mailbox": run_selection._no_mailbox(frame.args),
            "mailbox_enabled": frame.mailbox_enabled,
            "counts": frame.ctx.payload["initial_snapshot"]["counts"],
            "next_pending": frame.ctx.payload["initial_snapshot"]["next_pending"],
            **(
                {
                    "cwd": str(frame.cwd),
                    **run_prompts._workspace_payload_fields(
                        frame.ctx.verification_context
                    ),
                }
                if frame.ctx.verification_context.workspace is not None
                else {}
            ),
        },
    )
    if _pause_for_redirect_restart(frame.ctx, args=frame.args, snapshot=frame.snapshot):
        return _terminalize(frame.ctx, 1)
    _reconcile_stranded_redirects(frame.ctx, args=frame.args, snapshot=frame.snapshot)
    frame.ctx.persistence.write_state(frame.ctx.payload)
    run_persistence._notify_agent_mux_loop_started(
        frame.ctx.payload, frame.ctx.persistence
    )
    frame.ctx.progress.run_started(frame.ctx.payload)
    frame.ctx.progress.orphaned_state(frame.orphan_candidates)
    return None


def _register_control_surfaces(frame: RunFrame) -> RunResult | None:
    """Start the run controls, mailbox poll and exception-guard state."""
    frame.run_control_lock = frame.ctx.persistence.lock
    frame.ctx.control_payload = dict(frame.ctx.payload["control"])

    frame.ctx.run_controls = run_control.RunControlListener.for_run(
        frame.args,
        state=run_control.RunControlState(current_harness=frame.args.harness),
        stdin=sys.stdin,
        stderr=frame.output.stream,
        on_action=functools.partial(_handle_run_control_action, frame.ctx),
        on_status=functools.partial(_handle_run_control_status, frame.ctx),
    )
    frame.ctx.run_controls.start()

    # Served unconditionally, unlike the keyboard listener: a `--json`
    # supervised run is precisely the case with no interactive control surface,
    # so it is the one that most needs an `asl control` command observed now
    # rather than at the end of the poll interval. The caller owns the endpoint
    # feeding this event; a run reached without one still polls.
    if frame.wakeup is None:
        frame.wakeup = threading.Event()

    frame.mailbox_poll = run_coordination_mailbox_poll.MailboxPoll(
        plan_dir=frame.snapshot.plan_dir,
        state_dir_arg=frame.args.state_dir,
        control_state=frame.ctx.run_controls.state,
        seen_control_ids=frame.ctx.seen_external_command_ids,
        payload=frame.ctx.payload,
        event_path=frame.ctx.event_path,
        control_payload=frame.ctx.control_payload,
        persistence=frame.ctx.persistence,
        run_control_lock=frame.run_control_lock,
        enabled=frame.mailbox_enabled,
        wakeup=frame.wakeup,
        guard_controller=frame.ctx.guard_controller,
    )

    # Drain any commands that were queued before the run started so the
    # first turn already reflects them.
    run_coordination_control_drain.drain_external_control_queue(
        plan_dir=frame.snapshot.plan_dir,
        state_dir_arg=frame.args.state_dir,
        control_state=frame.ctx.run_controls.state,
        seen_ids=frame.ctx.seen_external_command_ids,
        payload=frame.ctx.payload,
        event_path=frame.ctx.event_path,
        control_payload=frame.ctx.control_payload,
        context=None,
        persistence=frame.ctx.persistence,
        run_control_lock=frame.run_control_lock,
        wakeup=frame.wakeup,
        guard_controller=frame.ctx.guard_controller,
    )
    frame.pending_exception_guard = None

    # Built once per run, deliberately. `--fresh-conversation` authorizes one
    # fresh start, and a run may take many turns across many sessions; a value
    # rebuilt per turn would re-grant the same permission on every one of them.
    # Nothing persists it, so a later `asl run --resume` or a supervisor
    # relaunch starts from an ungranted authorization however this run ends.
    frame.fresh_conversation = conversation_resume.FreshConversationAuthorization(
        bool(getattr(frame.args, "fresh_conversation", False))
    )
    return None
