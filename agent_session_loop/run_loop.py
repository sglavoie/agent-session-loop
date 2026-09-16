"""The locked run itself: one plan, one lock, one loop, one exit code.

Every other module in this package answers a question about a *part* of a run
-- how a turn is prepared, how its guards are enforced, how its outcome is
settled, how one handshake with the outside world stays correct. This module is
where those answers are sequenced into a run. It opens with the setup that a
run cannot start without, walks the readiness gates in the order the plan
requires, registers the control surfaces an operator can reach, and then enters
the ``while True`` skeleton that drives session after session until the plan
completes, pauses, or fails. When the loop leaves -- normally, by refusal, or
through a crash -- the finalization block below is what turns whatever happened
into a terminal record and an exit code.

:class:`RunContext` is the loop's memory. It is deliberately a plain dataclass
rather than a Protocol: the delegate modules each redeclare the *narrow*
structural view they need, so what lives here is the union of every field the
loop itself carries across a pass -- run-start collaborators that never change,
loop-pass bookkeeping reset or advanced per session, and the per-turn slots the
delegates read and write through the context instead of through 20 more
keyword arguments.

Nothing here imports the runner. That is not incidental: this module *is* the
run loop, so a runner import would be a cycle by construction rather than a
layering slip. Everything the loop needs enters as a parameter (``args``,
``cwd``, ``lock_info``, ``run_id``), through a package module that owns the
behaviour, or through the seams the delegate modules already publish. The
runner is left holding two argv entry points that call :func:`run_locked` and
nothing else.

Four properties of the loop are load-bearing and easy to break by accident:

* **The lock is already held.** :func:`run_locked` is called with the run lock
  taken and ``lock_info`` describing it. Nothing here acquires it, and every
  durable write below assumes it. Re-entering the runner's lock acquisition
  from inside this module would deadlock rather than fail loudly.
* **The exception guard is armed for the whole loop.** ``pending_exception_guard``
  is armed before the first pass and disarmed only on a clean terminalization.
  A ``BaseException`` escaping the loop body -- including ``KeyboardInterrupt``
  -- must reach the crash-path handler below, which is the only writer of the
  terminal record for an unplanned exit. Swallowing an exception inside the
  loop silently converts a crash into a hung, non-terminal run.
* **The retained-work settlement has one arming site and one call site.** A
  turn that clears the post-turn write guards arms ``ctx.retained_work``; the
  convergence point every loop exit falls through to is where that work is
  committed, if the pause the run recorded is one a machine can recover from
  and a commit mode makes the commit this run's to make. Both halves are
  placements rather than conditions: arming below the guards is what keeps a
  write-scope or protected-path refusal from ever reaching the settlement, and
  calling after the loop is what keeps the commit behind the revert that a
  repository-wide stage would otherwise commit the wrong side of.
* **The ``finally`` block is the only shutdown.** The mailbox poll's abort hook,
  the run guards' expiry, the control surfaces, and the inline tail are all
  stopped there. Returning early from a branch above without going through it
  leaks the poll thread and the control listener into the caller's process.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    commit_groups,
    commit_settlement,
    continuation_guards,
    conversation_resume,
    durability,
    events,
    guards,
    guidance_delivery,
    handoff_correction,
    handoff_guards,
    harnesses,
    model_availability,
    plans,
    preflight,
    qna,
    readiness_gate,
    redirect_ack,
    redirect_continuation,
    redirect_recovery,
    redirect_restart,
    redirect_store,
    review,
    review_continuation,
    review_cycle,
    review_guard,
    review_repair,
    review_session_body,
    review_transitions,
    run_commit,
    run_completion,
    run_control,
    run_coordination,
    run_inputs,
    run_outcome,
    run_payloads,
    run_persistence,
    run_prompts,
    run_reporting,
    run_selection,
    run_termination,
    session_verification,
    spark_status,
    state,
    supervisor_audit,
    turn_dispositions,
    turn_execution,
    turn_postflight,
    turn_preflight,
    turn_preparation,
    turn_settlement,
    verification,
    workspace_evidence,
    write_guards,
)
from .run_errors import RunError

# Registered implementation turns are enabled only because every such turn is
# wrapped by the authoritative workspace snapshot guard below. Registered
# review turns remain fail-closed until the combined review guard in Session 13.
MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED = True


@dataclasses.dataclass(kw_only=True)
class RunContext:
    """One locked run's own state, grouped by how long each value lives.

    ``run_locked`` is a single function with roughly eighty locals, and the
    helpers it owns used to reach them as closure captures. A capture is
    invisible at the call site and unsliceable: a helper that reads a loop
    local cannot leave the function that binds it. This object makes that
    dependency explicit so each helper states what it needs.

    The groups below are the run's three clocks. Run-start fields are bound
    before the first helper runs and never rebound; loop-pass fields advance
    once per pass around the session loop; turn fields advance once per
    harness turn. Nothing here is durable on its own: every write still goes
    through :attr:`persistence`, and the object never outlives the run lock.
    """

    # Fixed at run start. `plan_dir` is the canonicalized directory every
    # snapshot of this run shares, so it stays here rather than being re-read
    # off whichever snapshot the loop currently holds.
    run_id: str
    plan_dir: Path
    run_dir: Path
    event_path: Path
    payload: dict[str, Any]
    persistence: run_persistence._RunPersistence
    progress: run_reporting._ProgressReporter
    inline_tail: run_reporting._InlineTailController
    reply_state_preserver: run_coordination.ReplyStatePreserver
    audit_context: run_coordination.TerminalAuditContext
    guard_controller: guards.GuardController
    plan_cache: plans.PlanSnapshotCache
    verification_context: verification.VerificationContext
    commit_generation: run_commit._CommitGeneration
    #: What each session's earlier attempts in *this* run were seen to change.
    #: Bound at run start rather than per session because the loop re-attempts
    #: a session across several passes and the ledger has to outlive each of
    #: them; it is keyed by session id and retired when a session completes.
    session_attempts: continuation_guards.SessionAttempts = dataclasses.field(
        default_factory=continuation_guards.SessionAttempts
    )
    #: Read off the payload once the readiness gate has passed, because the
    #: gate is what settles which control block the rest of the run answers to.
    control_payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: Wired once the control listener exists, because the listener is built
    #: from the two handlers that already take this context.
    run_controls: run_control.RunControlListener | None = None
    #: What a crashed predecessor left unfinished in the redirect journal,
    #: settled once under this run's lock before any turn is selected. Held for
    #: the whole run because the session a stranded correction belongs to may
    #: not be the first one this run selects.
    redirect_recovery_report: redirect_recovery.ReconciliationReport | None = None

    # Updated once per loop pass: the counters and flags that decide whether
    # the next pass runs, and the readiness gate's latest verdict.
    completed_count: int = 0
    #: Stamped when the runner stops working on a turn, not when the harness
    #: exits: verification and finalization are runner-owned work, so counting
    #: them as idle time would report the previous turn's own cost as a gap.
    runner_work_finished_at: datetime | None = None
    first_loop_pass: bool = True
    idle_baseline_pinned: bool = False
    commit_at_end_attempted: bool = False
    review_appended: bool = False
    gate_payload: dict[str, Any] = dataclasses.field(default_factory=dict)
    gate_passed: bool = False
    core_skipped: bool = False

    # Updated once per turn.
    run_control_context: dict[str, Any] | None = None
    last_session_details: dict[str, Any] = dataclasses.field(default_factory=dict)
    seen_external_command_ids: set[str] = dataclasses.field(default_factory=set)
    #: The group this run still owes, carried between commit boundaries. A
    #: commit failure soft-fails, so this is what makes the failure retryable
    #: inside the same run instead of only after a restart.
    owed_commit_group: dict[str, Any] | None = None
    #: Retries already spent per group, so a repository that fails the same way
    #: every time cannot turn the run loop into a commit loop.
    owed_commit_group_attempts: dict[str, int] = dataclasses.field(default_factory=dict)
    #: The group that spent its whole retry budget without settling, armed by
    #: :func:`_soft_fail_commit` and read at the loop's selection boundary.
    #: ``None`` means nothing durable is owed past its budget, which is the
    #: only state in which this run may start another session.
    unsettled_commit_group: dict[str, Any] | None = None
    #: The turn whose filesystem changes a pause would leave behind, armed
    #: only once that turn has cleared the post-turn write guards. A pause is
    #: not a place the loop can rebuild a turn's identity from, so the turn
    #: hands it over while it still exists; ``None`` past the loop means no
    #: turn ever got far enough to retain work anyone may commit.
    retained_work: commit_settlement.RetainedWorkContext | None = None

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None:
        """Publish the plan image this run ends on, under the write lock."""
        with self.persistence.lock:
            self.payload["final_snapshot"] = plans.snapshot_payload(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        """Stop this run with a reason, through the one pause coordinator.

        Every pause site named the same three run-start values -- the event
        path, the payload, the persistence seam -- and differed only in the
        reason it was pausing for. Those three now live here, so a caller
        states its reason and nothing else.
        """
        run_termination._pause_run(
            self.event_path,
            self.payload,
            reason=reason,
            message=message,
            details=details,
            persistence=self.persistence,
        )


def _finish_run_locked(
    ctx: RunContext,
    exit_code: int,
    result_payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    ctx.reply_state_preserver.restore_if_unconsumed()
    return exit_code, result_payload


def _current_session_scope_or_none(
    ctx: RunContext,
    session: plans.SessionStatus | None,
) -> plans.SessionScope | None:
    """Read a session's durable repository scope, or ``None`` if it cannot be.

    The run-start refusal below judges a retained scope violation against the
    scope now on disk, and a scope it cannot read is one it cannot clear a
    violation with, so an unreadable or invalid one reads as absent and the
    refusal stands.
    """
    workspace = ctx.verification_context.workspace
    if workspace is None or session is None:
        return None
    try:
        return run_prompts._session_scope(session, workspace)
    except plans.PlanError:
        return None


def _terminalize(
    ctx: RunContext,
    exit_code: int,
    *,
    cancel: harnesses.AbortSignal | None = None,
    poll: Callable[[], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Close this run through the one coordinator every ending shares.

    The refusals above the loop end runs as truly as the loop's own exits
    do, so they terminalize here rather than finalizing on their own; the
    exit code each one already decided passes through untouched. Those
    refusals run before the control queue has a poller, so only the loop's
    own exit supplies ``cancel`` and ``poll``.

    A commit failure no longer stops the run, so the last thing decided
    before the audit runs is whether any group is still owed. The answer is
    written into the payload here, ahead of
    ``run_termination._terminalize_run``, because every reader that must not
    miss it — terminal adjudication, the assurance audit, and the final
    summary — is downstream of this point.

    The exit code each caller passes is a proposal, not a verdict: the
    adjudicator inside ``run_termination._terminalize_run`` may refuse a
    success and return a failing code instead, which is why the returned
    value is used rather than the one passed in.
    """
    with ctx.persistence.lock:
        ctx.payload[commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY] = (
            commit_settlement._unresolved_commit_groups(ctx.payload)
        )
    return _finish_run_locked(
        ctx,
        run_termination._terminalize_run(
            exit_code=exit_code,
            event_path=ctx.event_path,
            payload=ctx.payload,
            write_readiness_state=readiness_gate.write_readiness_state,
            progress=ctx.progress,
            persistence=ctx.persistence,
            audit_context=ctx.audit_context,
            cancel=cancel,
            poll=poll,
        ),
        ctx.payload,
    )


def _soft_fail_commit(
    ctx: RunContext,
    *,
    scope: str,
    session_id: str | None,
    group: Mapping[str, Any] | None,
    commit: Mapping[str, Any] | None,
    error: str | None,
    message: str,
) -> None:
    """Absorb a commit failure: record it, warn, and keep the run going.

    The group is already marked ``failed`` in ``commit_groups`` by the
    coordinator, and the commit's own ``commit_finished`` record already
    carries the outcome that classified it. What is added here is the
    run-level admission — durable in state, visible in the progress output,
    and retried at the next commit boundary.

    The retry is bounded. An attempt that never built a group has nothing
    durable to retry, and a group that has already spent its retries is
    left owed rather than attempted forever; either way the group stays
    ``failed`` in ``commit_groups``, so ``terminalize`` reports it and a
    later run can still recover it.

    Spending the budget is where the run keeps going but stops working. A
    durable group that is still owed past its last retry arms
    :attr:`RunContext.unsettled_commit_group`, which
    :func:`_pause_for_unsettled_commit_group` reads at the next selection
    boundary. Arming is deliberately limited to a group: an attempt that never
    built one has nothing durable a later run could recover, so pausing on it
    would refuse the first commit failure without ever retrying it.
    """
    record = commit_settlement._commit_failure_record(
        scope=scope,
        session_id=session_id,
        group=group,
        commit=commit,
        error=error,
        message=message,
    )
    group_id = record["group_id"]
    spent = (
        ctx.owed_commit_group_attempts.get(group_id, 0)
        if isinstance(group_id, str)
        else commit_settlement.MAX_COMMIT_GROUP_RETRIES
    )
    retrying = group is not None and spent < commit_settlement.MAX_COMMIT_GROUP_RETRIES
    record["retrying"] = retrying
    with ctx.persistence.lock:
        commit_settlement._append_commit_failure(ctx.payload, record)
        ctx.persistence.write_state(ctx.payload)
    ctx.progress.commit_failed(record)
    if not retrying:
        ctx.owed_commit_group = None
        if group is not None:
            ctx.unsettled_commit_group = {
                "scope": record["scope"],
                "session_id": record["session_id"],
                "group_id": record["group_id"],
                "failed_alias": record["failed_alias"],
                "retries_spent": spent,
                "message": record["message"],
                "error": record["error"],
            }
        return
    ctx.owed_commit_group_attempts[str(group_id)] = spent + 1
    ctx.owed_commit_group = dict(group) if group is not None else None


def _pause_for_unsettled_commit_group(
    ctx: RunContext, *, snapshot: plans.PlanSnapshot
) -> bool:
    """Stop the run before it selects work on top of a commit it never made.

    A commit failure soft-fails so a transient Git refusal can heal at the next
    commit boundary, and that retry is bounded. What used to happen past the
    bound was that the loss was simply inherited: the group stayed ``failed``
    for a later run, and *this* run went on selecting sessions over a worktree
    whose last accepted session was never committed. Everything after it then
    builds on changes no commit describes, and the end-of-run sweep folds them
    into a message written for different work (ASI-138, ASI-139).

    So the bound now ends the run rather than only ending the retries. The
    reason is ``commit-failed``, which the pause policy already classes as
    environment-blocked -- the working tree, or the commit harness, has to
    change on disk before the same gate can pass -- and which the Recovery
    Center already renders from the commit-failure rows this run wrote. The
    group is still left owed in ``commit_groups``, so the restart half of the
    invariant recovers it exactly as before; what changed is that no further
    session runs first.

    This is checked at the top of the loop rather than at each failure because
    the selection boundary is what the barrier is about, and one place covers
    every way a run reaches it: the settlement ahead of the next worker, the
    settlement a startup recovery already spent, the per-turn commit, and the
    completion branch that would otherwise append a review or materialize
    repair children over the same uncommitted tree.

    Reusing that reason also settles what happens to the dirt, without this
    barrier deciding anything about it. ``commit-failed`` is environment
    blocked rather than recoverable, so the retained-work settlement below
    refuses this pause the way it refuses every other one a machine cannot
    retry -- which is the outcome that is wanted here, and for the reason the
    policy already gives. The turn's changes belong to a session that
    completed and to a durable ``failed`` group still owed a retry; committing
    them under the paused-work marking would say the session is back to
    ``pending`` when it is not, and would leave the owed group a clean tree
    that the next run reads back as ``skipped-clean`` (ASI-19).
    """
    unsettled = ctx.unsettled_commit_group
    if unsettled is None:
        return False
    ctx.record_final_snapshot(snapshot)
    ctx.pause(
        reason="commit-failed",
        message=(
            "an auto-commit group could not be completed within "
            f"{commit_settlement.MAX_COMMIT_GROUP_RETRIES} retries, so no "
            "further session is selected until it is settled"
        ),
        details=dict(unsettled),
    )
    return True


def _settle_owed_commit_group(ctx: RunContext) -> None:
    """Complete the group this run still owes before it commits anything else.

    This is the restart invariant applied inside a single run: an
    unresolved group is finished at the next commit boundary, ahead of the
    next worker and ahead of the end-of-run commit, so a transient failure
    heals without a human. A second failure re-arms the same retry rather
    than escalating.

    How the recovered commit gets its message is the group's to decide rather
    than this run's, which is why the generation goes through
    :func:`commit_settlement._recovered_commit_generation` instead of being
    handed straight from the context: a ``session-paused`` group is settled
    with the deterministic marking it already carries, and every other scope
    keeps the run's own message harness.
    """
    group = ctx.owed_commit_group
    if group is None:
        return
    ctx.owed_commit_group = None
    session_id = (
        str(group["session_id"]) if group.get("session_id") is not None else None
    )
    recovered_group: dict[str, Any] = dict(group)
    failed_commit: dict[str, Any] | None = None
    recovery_error: str | None = None
    try:
        recovered_group, failed_commit = commit_settlement._run_commit_group(
            event_path=ctx.event_path,
            payload=ctx.payload,
            progress=ctx.progress,
            verification_context=ctx.verification_context,
            run_dir=ctx.run_dir,
            run_id=ctx.run_id,
            session_id=session_id,
            targets=commit_settlement._resolve_recovered_commit_targets(
                group, ctx.verification_context
            ),
            generation=commit_settlement._recovered_commit_generation(
                group, generation=ctx.commit_generation, run_dir=ctx.run_dir
            ),
            recovered_group=group,
            persistence=ctx.persistence,
        )
    except (RunError, commit_groups.CommitGroupError) as error:
        recovery_error = str(error)
    resolved = recovery_error is None and failed_commit is None
    with ctx.persistence.lock:
        ctx.payload["commit_group_recovery"] = {
            "group_id": group.get("group_id"),
            "success": resolved,
            "error": recovery_error,
            "group": recovered_group,
        }
        ctx.persistence.write_state(ctx.payload)
    if resolved:
        ctx.progress.commit_group_recovered(recovered_group)
        return
    _soft_fail_commit(
        ctx,
        scope=str(group.get("scope") or "session"),
        session_id=session_id,
        group=recovered_group,
        commit=failed_commit,
        error=recovery_error,
        message=(
            "unresolved auto-commit group could not be completed "
            "before the next session"
        ),
    )


def _settle_owed_commit_group_at_startup(
    ctx: RunContext, *, group: Mapping[str, Any]
) -> None:
    """Spend a paused-work group's whole retry budget before the startup gate.

    A failed settlement re-arms its group, but the boundary that consumes a
    re-armed retry is the top of the run loop -- and between here and there
    stands the startup commit preflight, which refuses exactly the dirt the
    failed settlement left behind. So a group that failed once here used to be
    re-armed behind the gate that prevented its retry: the relaunch paused
    ``commit-preflight``, which is non-recoverable, and a second transient Git
    refusal turned a repairable pause into one that needs a human.

    Repeating the attempt here is what closes that ordering. Only a
    ``session-paused`` group repeats, because only that scope recovers without
    a harness -- it commits the deterministic text it carries through
    ``--message-file``, so an extra attempt costs one Git command. A ``session``
    or ``end`` group asks the run's message harness for a message per attempt,
    which is a bounded but real turn each time, and its retry can wait for the
    loop boundary it has always used.

    The repetition is bounded twice over. ``_soft_fail_commit`` stops re-arming
    once the group has spent :data:`commit_settlement.MAX_COMMIT_GROUP_RETRIES`,
    which is what normally ends this loop; the iteration cap is the backstop for
    a recovery that somehow answers under a different ``group_id`` and would
    otherwise never charge an attempt against the one this started with. A
    repository that refuses every time still reaches the gate with its dirt --
    persistent refusal is an environment fault a human should see -- it just no
    longer gets there with a retry left unspent.
    """
    ctx.owed_commit_group = dict(group)
    for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
        _settle_owed_commit_group(ctx)
        owed = ctx.owed_commit_group
        if owed is None:
            return
        if owed.get("scope") != commit_groups.GROUP_SCOPE_SESSION_PAUSED:
            return


def _settle_paused_retained_work(ctx: RunContext, *, commit_mode: str | None) -> None:
    """Commit what a recoverable pause is about to leave in the worktree.

    A pause is not an ending the accepted path ever reaches, so under a commit
    mode the turn's retained changes used to survive the process and meet the
    next one's startup commit gate as unexplained dirt. This is the one place
    that settles them, and it runs after the pause is already recorded:
    ``run_termination.set_pause`` has written the reason, the details and the
    classification, and the ``run_paused`` event has been appended. The commit
    must not run any earlier than that. Staging in a legacy workspace is a
    repository-wide ``git add -A``, so committing before
    ``session_verification`` reverts the session to ``pending`` would commit
    the plan file saying ``complete`` and the revert would immediately re-dirty
    it — the same dead end, one line later.

    The gate reads the policy the pause already recorded rather than
    classifying the reason again: ``pause_policy`` has one owner, and a second
    opinion here would be a second vocabulary for the supervisor to reconcile.
    What it adds to that policy is what only the loop knows -- whether a turn
    ever armed :attr:`RunContext.retained_work`, and whether this run's commit
    mode makes the commit the run's to make. A repair child owns its commit
    with no mode at all, which is why ``None`` is not simply a refusal.

    The record is mirrored twice on purpose. The ``run_paused`` event is
    already on disk and does not carry it -- events are append-only and the
    settlement is later than the pause by construction -- so every reader that
    must see it reads state instead: the run payload under
    :data:`commit_settlement.PAUSED_WORK_COMMIT_KEY`, and the pause details,
    which ``run_termination.set_pause`` aliases to ``payload["details"]`` so
    one in-place update serves both spellings. The turn's own ``result.json``
    is rewritten last, because ``commit_settlement`` writes the record onto the
    live turn payload but makes no file writes of its own.

    The progress line comes last and is the only part of this that is not
    durable. It exists because the durable record answers a question the
    operator has already stopped asking by the time they can read state: the
    run is about to print ``run paused``, and whether they should relaunch or
    look is decided by what the settlement did with the worktree.
    """
    retained = ctx.retained_work
    if retained is None:
        return
    payload = ctx.payload
    if payload.get("paused") is not True:
        return
    policy = payload.get("pause_policy")
    if not isinstance(policy, Mapping) or policy.get("recoverable") is not True:
        return
    if retained.is_review_session:
        return
    if commit_mode not in run_selection.COMMIT_MODES and not (
        commit_mode is None and retained.is_repair_session
    ):
        return
    # `set_pause` is the only writer of the block gated on above, and it always
    # writes a dict here; the isinstance is what lets the mirror below be an
    # in-place update of the payload's own details rather than of a copy.
    raw_details = payload.get("pause_details")
    details = raw_details if isinstance(raw_details, dict) else {}
    record = commit_settlement._settle_paused_retained_work(
        event_path=ctx.event_path,
        payload=payload,
        progress=ctx.progress,
        verification_context=ctx.verification_context,
        run_dir=ctx.run_dir,
        run_id=ctx.run_id,
        retained=retained,
        pause_reason=str(payload.get("pause_reason") or ""),
        pause_details=details,
        commit_mode=commit_mode,
        generation=ctx.commit_generation,
        soft_fail=functools.partial(_soft_fail_commit, ctx),
        persistence=ctx.persistence,
    )
    if record is None:
        return
    with ctx.persistence.lock:
        payload[commit_settlement.PAUSED_WORK_COMMIT_KEY] = record
        details[commit_settlement.PAUSED_WORK_COMMIT_KEY] = record
        ctx.persistence.write_state(payload)
        durability.write_json_file(retained.result_path, retained.turn_payload)
    # Outside the lock, like every other reporter call: the settlement is the
    # last thing the run does before it prints its verdict, and a slow terminal
    # must not hold the state lock open across it.
    ctx.progress.paused_work_committed(record)


def _handle_run_control_action(
    ctx: RunContext, action: run_control.RunControlAction
) -> None:
    if action.action in ("consumed", "abort-consumed"):
        # Both consumptions are recorded by the site that acts on them, and
        # an abort's consumption has three distinct outcomes this callback
        # cannot tell apart.
        return
    if action.action == "tail-disabled":
        ctx.inline_tail.disable()
    with ctx.persistence.lock:
        context = (
            None if ctx.run_control_context is None else dict(ctx.run_control_context)
        )
        run_coordination.record_run_control_action(
            event_path=ctx.event_path,
            payload=ctx.payload,
            control_payload=ctx.control_payload,
            action=action,
            context=context,
            persistence=ctx.persistence,
        )
    if action.action == "tail-enabled":
        ctx.inline_tail.enable()


def _handle_run_control_status(
    ctx: RunContext, action: run_control.RunControlAction
) -> None:
    with ctx.persistence.lock:
        context = (
            None if ctx.run_control_context is None else dict(ctx.run_control_context)
        )
        status_control = dict(ctx.control_payload)
        status_control["stop_after_current"] = action.stop_after_current
        status_control["tail_enabled"] = action.tail_enabled
        status_control["next_harness"] = action.next_harness
        status_payload = dict(ctx.payload)
        active_turn = ctx.payload.get("active_turn")
        if isinstance(active_turn, dict):
            status_payload["active_turn"] = dict(active_turn)
        elif context is not None:
            status_payload["active_turn"] = dict(context)
        turns = ctx.payload.get("turns")
        if isinstance(turns, list):
            status_payload["turns"] = [
                dict(turn) if isinstance(turn, dict) else turn for turn in turns
            ]

    try:
        status_snapshot = ctx.plan_cache.get(ctx.plan_dir)
        plan_error = None
    except plans.PlanError as error:
        status_snapshot = None
        plan_error = str(error)

    ctx.progress.status_snapshot(
        run_reporting._compact_status_lines(
            payload=status_payload,
            control=status_control,
            snapshot=status_snapshot,
            plan_error=plan_error,
        )
    )


def _expire_run_guards(ctx: RunContext) -> None:
    """Disable this run's command gate on every exit path."""
    run_coordination.expire_run_guards(
        controller=ctx.guard_controller,
        event_path=ctx.event_path,
        payload=ctx.payload,
        control_payload=ctx.control_payload,
        persistence=ctx.persistence,
    )


def _abandon_turn(
    ctx: RunContext,
    redirect: redirect_continuation.TurnRedirect | None = None,
) -> None:
    """Drop this run's interactive surfaces when a turn cannot be launched.

    The redirect gate is closed here for the same reason the run controls are
    stopped: it holds a watch thread and an escalation clock aimed at a process
    that never became this turn's worker, and a launch that died leaves nobody
    else to take either of them down.
    """
    if redirect is not None:
        redirect.close()
    if ctx.run_controls is not None:
        ctx.run_controls.stop()
    ctx.inline_tail.stop()


def _close_turn_controls(
    redirect: redirect_continuation.TurnRedirect,
    mailbox_poll: run_coordination.MailboxPoll,
) -> None:
    """Disarm everything the turn was handed, in the order that proves it.

    Settling the redirect gate first is what serializes completion against
    acceptance: from this line on a correction is refused rather than accepted
    against a turn whose process is already gone. Closing it second is what
    releases the escalation, and it happens here rather than later because the
    harness has by now waited on its child -- which is the only evidence that
    lets the escalation's report claim the predecessor was collected rather
    than merely killed.
    """
    redirect.close()
    run_termination._detach_turn_controls(mailbox_poll)


def _redirected_turn_harness(
    ctx: RunContext,
    *,
    pending: redirect_continuation.PendingRedirect | None,
    selected: str,
    session_id: str,
    turn_index: int,
) -> str:
    """Pin a redirected attempt to the harness its predecessor ran on.

    A future-harness selection queued mid-run is a statement about the *next*
    session, not about finishing the one an operator just corrected. Letting it
    apply here would either strand the conversation the correction was written
    against or fail the resume outright on ``SELECTION_INCOMPATIBLE``, so the
    switch is deferred and recorded rather than silently taken.
    """
    if pending is None or pending.session_id != session_id:
        return selected
    if pending.harness is None or pending.harness == selected:
        return selected
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans.session_identity_payload(session_id),
            "outcome": "harness-deferred",
            "redirect": pending.to_payload(),
            "deferred_harness": selected,
        },
    )
    return pending.harness


def _pause_for_redirect_restart(
    ctx: RunContext, *, args: argparse.Namespace, snapshot: plans.PlanSnapshot
) -> bool:
    if args.dry_run:
        return False
    try:
        blocked = redirect_restart.restart_blocker(
            snapshot.plan_dir,
            pending_session_ids=tuple(
                s.id for s in snapshot.sessions if s.status == "pending"
            ),
            state_dir=args.state_dir,
        )
    except (OSError, redirect_store.RedirectStoreError) as error:
        blocked = {"reason": "redirect-successor-blocked", "error": str(error)}
    if blocked is None:
        return False
    ctx.record_final_snapshot(snapshot)
    ctx.pause(
        reason=blocked["reason"],
        message=(
            "Redirect recovery requires predecessor termination proof and "
            "an explicit resolution of any uncertain native submission."
        ),
        details={"redirect_recovery": blocked},
    )
    return True


def _reconcile_stranded_redirects(
    ctx: RunContext,
    *,
    args: argparse.Namespace,
    snapshot: plans.PlanSnapshot,
) -> None:
    """Settle what a crashed predecessor left in the redirect journal.

    Runs once, under this run's own lock, before the first session is selected
    — the same placement and the same reason as the repair-publication
    rollback above it. A correction the dead run accepted and never delivered is
    not something a turn can discover later: by the time a prompt is being built
    the loop is already deciding what that prompt says, and a journal still
    claiming an unanswered correction for a run that no longer exists would make
    that decision on a lie.

    The restart gate runs before reconciliation: no release, commit, or worker
    is allowed until predecessor termination and submission ambiguity have
    been checked. This function then settles only provably unsent carries.
    """
    if args.dry_run:
        # A dry run promises to write nothing durable, and settling a request is
        # durable. The corrections stay stranded for the real run to reconcile.
        return
    pending_sessions = tuple(
        session.id for session in snapshot.sessions if session.status == "pending"
    )
    try:
        report = redirect_recovery.reconcile_startup(
            snapshot.plan_dir,
            run_id=ctx.run_id,
            pending_session_ids=pending_sessions,
            state_dir=args.state_dir,
        )
    except (redirect_store.RedirectStoreError, OSError) as error:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "outcome": "recovery-failed",
                "error": str(error),
            },
        )
        return
    ctx.redirect_recovery_report = report
    notable = (
        report.blocked
        or report.expired
        or report.settled
        or report.stranded
        or report.recoverable
        or report.uncertain
        or report.torn_tail
        or report.unreadable_lines
    )
    if not notable:
        # Nothing was stranded and nothing is in doubt: the overwhelmingly
        # common case, and an event for it would be noise in every run's log.
        return
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "outcome": "recovery-blocked" if report.blocked else "recovered",
            "recovery": report.to_payload(),
        },
    )


def _carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    pending: redirect_continuation.PendingRedirect | None,
    session_id: str,
    turn_index: int,
    harness: str,
    recovery: redirect_recovery.ReconciliationReport | None = None,
) -> redirect_ack.CarriedCorrections | None:
    """Bind every correction this attempt should carry, just before its prompt.

    This call *is* the coalescing boundary. Corrections keep arriving while the
    predecessor is collected and the resumed prompt is assembled, and all of
    them are about the same situation, so everything unsettled against an
    earlier attempt of this session is swept in here and ordered by submission.
    A correction submitted after this point names the attempt that is about to
    be live, which is the ordinary mid-turn redirect path rather than a late
    edit to a prompt already written.

    Gated on the same session match ``interrupted_operations`` uses: a
    correction is never swept into a session the operator was not talking about.
    A journal that cannot be carried at all degrades to session 11's behaviour —
    the resumed worker is still told which operations were interrupted — and
    the corrections stay accepted in the journal rather than being lost, so the
    failure is recorded instead of silently changing what the worker reads.

    A second population reaches the same boundary: corrections a *previous* run
    was holding when it died. They have no ``PendingRedirect`` — the object that
    would have carried them across attempts died with the process that built it
    — so they arrive from the startup reconciliation instead, and they are the
    reason this can run for a session that was never redirected in this run.
    """
    recovered_ids: tuple[str, ...] = ()
    if recovery is not None:
        recovered_ids = redirect_recovery.recoverable_operation_ids(
            recovery, session_id
        )
    redirected_here = pending is not None and pending.session_id == session_id
    if not redirected_here and not recovered_ids:
        return None
    redirect_payload: dict[str, Any] = (
        pending.to_payload()
        if pending is not None and redirected_here
        else {**plans.session_identity_payload(session_id), "recovered": True}
    )
    continuation = redirect_store.RedirectTarget(
        run_id=ctx.run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )
    try:
        carried = redirect_ack.carry_corrections(
            plan_dir,
            continuation=continuation,
            accepted_operation_ids=(
                pending.operation_ids if pending is not None and redirected_here else ()
            ),
            recovered_operation_ids=recovered_ids,
            state_dir=state_dir_arg,
        )
    except redirect_store.RedirectStoreError as error:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **plans.session_identity_payload(session_id),
                "outcome": "carry-failed",
                "redirect": redirect_payload,
                "harness": harness,
                "error": {"reason": error.reason, "message": str(error)},
            },
        )
        return None
    if not redirected_here and not carried.corrections:
        # The recoverable list is a fact about the crash and does not shrink as
        # this run works through it, so it still names this session on every
        # later turn. By then the journal has recorded the delivery and the
        # coalescing boundary drops the ids; saying "carried" again would claim
        # a delivery per turn for one correction handed over once.
        return None
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans.session_identity_payload(session_id),
            "outcome": "carried",
            "redirect": redirect_payload,
            "harness": harness,
            # Digests, byte counts and operation ids. The words are in the
            # journal and reach nothing but the prompt itself.
            **carried.to_payload(),
        },
    )
    return carried


def _annotate_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
    outcome: str,
    annotate: Callable[..., redirect_ack.CarryAnnotations],
) -> None:
    """Record what became of this attempt's carry, and say so in an event.

    Shared by the two sides of the launch boundary because they differ in one
    call and one word. Both are bookkeeping about a decision already made, so a
    journal that refuses them is reported and never raised: at the release site
    the run is already pausing for a reason of its own, and at the delivery site
    a worker is already running.

    Not shared with :func:`_launch_carried_corrections`, which is bookkeeping the
    decision *depends on* and therefore cannot swallow its own failure.
    """
    if carried is None or not carried.corrections:
        return
    try:
        result = annotate(
            plan_dir,
            continuation=carried.continuation,
            operation_ids=carried.operation_ids(),
            state_dir=state_dir_arg,
        )
    except redirect_store.RedirectStoreError as error:
        ctx.persistence.append_event(
            ctx.event_path,
            events.REDIRECT_CONTINUATION_EVENT,
            {
                "run_id": ctx.run_id,
                "turn_index": turn_index,
                **plans.session_identity_payload(session_id),
                "outcome": f"{outcome}-failed",
                "harness": harness,
                "error": {"reason": error.reason, "message": str(error)},
            },
        )
        return
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans.session_identity_payload(session_id),
            "outcome": outcome,
            "harness": harness,
            **result.to_payload(),
        },
    )


def _launch_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
) -> bool:
    """Declare this attempt's carry about to go out, and say whether it worked.

    The one piece of redirect bookkeeping the run loop may not shrug off. Its
    siblings report a decision already made; this one is a precondition of
    making it. Appended before the executor is given the prompt, it is what lets
    a later reader tell an attempt that died before submitting from one that
    died just after -- and the whole point of that distinction is to stop the
    second case from being replayed to a new worker as if it were the first.

    So a refusal here returns ``False`` and the caller must abandon the turn
    rather than launch it. Submitting a prompt this run could not declare would
    put the operator's words in a worker's hands with nothing on disk to say so,
    which is precisely the shape that produces a duplicate delivery later. A
    turn not run costs a pause the operator can act on; a turn run undeclared
    costs a correction delivered twice with nobody able to tell.

    ``True`` with nothing carried is the ordinary case: no carry, nothing to
    declare, nothing that can fail.
    """
    if carried is None or not carried.corrections:
        return True
    error: dict[str, Any] | None = None
    result: redirect_ack.CarryAnnotations | None = None
    try:
        result = redirect_ack.launch_corrections(
            plan_dir,
            continuation=carried.continuation,
            operation_ids=carried.operation_ids(),
            state_dir=state_dir_arg,
        )
    except redirect_store.RedirectStoreError as failure:
        error = {"reason": failure.reason, "message": str(failure)}
    else:
        if result.refused:
            # A per-id refusal is as disqualifying as a raised one. The prompt
            # holds every carried correction, so declaring only some of them
            # would leave the rest in exactly the state this call exists to
            # prevent.
            refusal = result.refused[0]
            error = {"reason": refusal.reason, "message": refusal.message}
    ctx.persistence.append_event(
        ctx.event_path,
        events.REDIRECT_CONTINUATION_EVENT,
        {
            "run_id": ctx.run_id,
            "turn_index": turn_index,
            **plans.session_identity_payload(session_id),
            "outcome": "launching" if error is None else "launching-failed",
            "harness": harness,
            **(result.to_payload() if result is not None else {}),
            **({"error": error} if error is not None else {}),
        },
    )
    return error is None


def _release_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
) -> None:
    """Hand back a carry this attempt assembled a prompt for but never launched.

    Called at every way out between the coalescing boundary and the launch: a
    prompt build that pauses, a conversation this run cannot prove is usable, a
    baseline capture that refuses. Each of them ends the attempt with the
    correction bound to a turn that will never run, and without this the journal
    would go on claiming the operator's words were delivered to it -- which is
    what makes a later authorized fresh start arrive empty-handed.
    """
    _annotate_carried_corrections(
        ctx,
        plan_dir=plan_dir,
        state_dir_arg=state_dir_arg,
        carried=carried,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        outcome="released",
        annotate=redirect_ack.release_corrections,
    )


def _deliver_carried_corrections(
    ctx: RunContext,
    *,
    plan_dir: Path,
    state_dir_arg: str | Path | None,
    carried: redirect_ack.CarriedCorrections | None,
    session_id: str,
    turn_index: int,
    harness: str,
) -> None:
    """Record that a worker was handed this attempt's carry.

    Hung off the harness's submission callback, which is the earliest moment
    anything can honestly be called delivery: the prompt has left this process
    for the worker. A spawn is not that moment -- a protocol harness spawns,
    handshakes, and can still be refused with the prompt unsent -- and reading
    one as the other is what leaves an operator's words spent on a turn that
    never received them. From here the correction is the worker's, and recovery
    treats an unreceipted one as an open question rather than something to hand
    out again.
    """
    _annotate_carried_corrections(
        ctx,
        plan_dir=plan_dir,
        state_dir_arg=state_dir_arg,
        carried=carried,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
        outcome="delivered",
        annotate=redirect_ack.deliver_corrections,
    )


class _TurnDelivery:
    """One attempt's carry, from the launch boundary to the turn's outcome.

    An attempt that carried corrections ends in one of three states, and only
    the harness can narrow it: the prompt reached a worker, it provably did not,
    or neither could be established. The first two are recorded here because
    they are the same decision read from opposite ends -- ``delivered`` makes
    taking the words back a duplicate, ``released`` makes handing them out again
    a rescue. The third is deliberately recorded as nothing at all: the
    ``launching`` line written before the launch already stands, and leaving it
    unanswered is what tells a later reader to ask rather than to guess.

    Settled exactly once, under a lock, because the two ends are reached from
    different threads: a protocol harness submits on its own driver thread while
    the run loop is waiting on the turn it will later settle.

    Bound per attempt rather than closed over, like the guidance delivery
    alongside it: a closure would read the loop variables as the *next* attempt
    left them.
    """

    def __init__(
        self,
        ctx: RunContext,
        *,
        plan_dir: Path,
        state_dir_arg: str | Path | None,
        carried: redirect_ack.CarriedCorrections | None,
        session_id: str,
        turn_index: int,
        harness: str,
    ) -> None:
        self._ctx = ctx
        self._plan_dir = plan_dir
        self._state_dir_arg = state_dir_arg
        self._carried = carried
        self._session_id = session_id
        self._turn_index = turn_index
        self._harness = harness
        self._lock = threading.Lock()
        self._settled = False

    def note_prompt_submitted(self) -> None:
        """The harness's submission callback: a worker now holds this prompt.

        Up to here the run had only undertaken to deliver these words; from here
        they went out. Exceptions are the harness seam's to swallow -- this is
        bookkeeping about a turn that is already running.
        """
        if not self._claim():
            return
        _deliver_carried_corrections(
            self._ctx,
            plan_dir=self._plan_dir,
            state_dir_arg=self._state_dir_arg,
            carried=self._carried,
            session_id=self._session_id,
            turn_index=self._turn_index,
            harness=self._harness,
        )

    def settle_unsubmitted(self, result: harnesses.HarnessResult) -> None:
        """Hand the carry back if the turn *proved* it submitted no prompt.

        The launch-side counterpart of the prelaunch releases: a turn that
        spawned and then refused -- a native restore the provider declined, a
        cancellation caught mid-handshake, a conversation it could not record --
        leaves the correction bound to an attempt that never delivered it.
        Without this the journal would go on claiming those words were the dead
        worker's, and the authorized fresh continuation that follows would
        arrive empty-handed.

        The proof is the result's own refusal fields, not the absence of a
        submission callback. A missing callback is a silence, and by the time
        this runs the journal already says the prompt was about to go out, so
        the silence has two readings: the turn never sent it, or it sent it and
        whatever would have reported that did not survive -- an adapter thread
        that died between the write and the notification, a delivery line the
        journal refused. Releasing on the silence is how the same correction
        reaches a second worker, so an unproven turn releases nothing and the
        carry stands as :attr:`~.redirect_store.RedirectRequest.launch_ambiguous`
        for an operator to reconcile.

        Called on every path out of the turn, including the ones that submitted:
        the settle guard is what makes it a no-op there rather than a withdrawal
        of work already done.
        """
        if not harnesses.prompt_provably_unsubmitted(result):
            return
        if not self._claim():
            return
        _release_carried_corrections(
            self._ctx,
            plan_dir=self._plan_dir,
            state_dir_arg=self._state_dir_arg,
            carried=self._carried,
            session_id=self._session_id,
            turn_index=self._turn_index,
            harness=self._harness,
        )

    def _claim(self) -> bool:
        with self._lock:
            if self._settled:
                return False
            self._settled = True
            return True


def run_locked(
    *,
    args: argparse.Namespace,
    cwd: Path,
    timeout: int | None,
    max_sessions: int | None,
    commit_mode: str | None,
    lock_info: state.LockInfo,
    run_id: str,
    reply_text: str | None = None,
    reply_origin: str = run_prompts.REPLY_ORIGIN_ARGV,
    verification_context: verification.VerificationContext | None = None,
    wakeup: threading.Event | None = None,
    redirect_wakeup: threading.Event | None = None,
) -> tuple[int, dict[str, Any]]:
    # The caller holds this run's lock and opened the wakeup endpoint before
    # publishing it, so `wakeup` is already being set by producers from here
    # on. A wakeup that lands outside the poll loop below only leaves the
    # event set for the next waiter to observe.
    if verification_context is None:
        verification_context = verification.VerificationContext.legacy(cwd)
    plan_cache = plans.PlanSnapshotCache()
    normalized_review_handoffs = list(
        plans.normalize_generated_review_handoff_artifacts(args.plan_dir)
    )
    executed_review_requires_artifact = False
    # ASI-136: a repair publication that died mid-flight owns this plan's files
    # until it is rolled back, and its children are pending but not loop-ready.
    # Recover under this run's lock, before the first snapshot, so selection
    # never sees a child whose loop-ready state was never established.
    repair_publication_recovery: dict[str, Any] | None = None
    if not args.dry_run:
        try:
            repair_publication_recovery = (
                review_repair.recover_review_repair_publication(args.plan_dir)
            )
        except plans.PlanError as error:
            repair_publication_recovery = {
                "recovered": False,
                "reason": "recovery-failed",
                "error": str(error),
            }
    snapshot = plan_cache.get(args.plan_dir)
    if repair_publication_recovery is not None and not (
        repair_publication_recovery.get("recovered")
    ):
        # The image could not be put back, so the plan on disk is neither the
        # state the publication started from nor a published one. Refuse to run
        # rather than select out of a half-published generation.
        payload = run_outcome._error_payload(
            args,
            reason="malformed-plan",
            message=(
                "an interrupted review repair publication could not be rolled "
                f"back: {repair_publication_recovery.get('reason')}"
            ),
            paused=True,
            cwd=cwd,
            snapshot=snapshot,
        )
        payload["details"] = {
            "review_repair_publication_recovery": repair_publication_recovery
        }
        payload["pause_details"] = dict(payload["details"])
        return 1, payload
    if not args.dry_run:
        run_inputs._ensure_legacy_run_metadata(snapshot.plan_dir, verification_context)
    state_dir = state.plan_state_dir(snapshot.plan_dir, args.state_dir)
    run_dir = state_dir / "runs" / run_id
    # The run's command gate. Constructed for every run and left empty until a
    # guard is added, so the drain always has somewhere to apply one and never
    # has to decide whether this run is guardable.
    #
    # The repository binding is supplied here rather than at the first turn
    # because the queue is drained before any turn starts: a protected-path
    # rule naming a declared alias would otherwise be refused as unknown
    # simply for having been queued before the run began.
    guard_controller = guards.GuardController(
        run_id=run_id,
        run_dir=run_dir,
        workspace_aliases=(
            {
                repository.alias: str(repository.root)
                for repository in verification_context.workspace.repositories
            }
            if verification_context.workspace is not None
            else {guards.LEGACY_PATH_QUALIFIER: str(cwd)}
        ),
    )
    event_path = state_dir / events.EVENTS_FILE_NAME
    state_path = state_dir / events.STATE_FILE_NAME
    try:
        unresolved_commit_group = commit_groups.load_unresolved_group(
            state_path=state_path,
            event_path=event_path,
        )
    except commit_groups.CommitGroupError as error:
        unresolved_commit_group = None
        commit_group_load_error = str(error)
    else:
        commit_group_load_error = None
    mailbox_activation = run_selection._mailbox_activation_payload(
        args, snapshot.plan_dir
    )
    mailbox_enabled = bool(mailbox_activation["enabled"])
    review_guard_excludes = review_guard._review_guard_excludes(args)
    review_guard_include_ignored = review_guard._review_guard_include_ignored(args)
    # One selection for every commit this run makes, including the recovery of
    # a group a previous process left unresolved: the declared implementation
    # harness, not whichever harness a live switch moved a turn onto.
    commit_generation = run_commit._commit_generation(args)
    audited_repositories, audited_registry_declared = (
        run_termination._audited_repositories(verification_context)
    )
    audit_context = run_coordination.TerminalAuditContext(
        plan=supervisor_audit.plan_audit(args),
        run_dir=run_dir,
        state_dir=state_dir,
        plan_dir=snapshot.plan_dir,
        workspace_root=(
            cwd
            if verification_context.workspace is None
            else verification_context.workspace.root
        ),
        ledger_state_dir=(None if args.state_dir is None else Path(args.state_dir)),
        repositories=audited_repositories,
        registry_declared=audited_registry_declared,
    )

    # Detect state stranded under a moved plan's old path hash *before* this
    # run writes its own state.json (which would suppress the scan). A scan
    # failure must never block the run, so any error yields no candidates.
    try:
        orphan_candidates = state.orphaned_state_candidates(snapshot, args.state_dir)
    except Exception:  # pragma: no cover - defensive; scan is best-effort
        orphan_candidates = []

    reply_context: qna.QnAContext | None = None
    if reply_text is not None:
        try:
            reply_context = qna.load_qna_context(
                snapshot.plan_dir,
                state_dir=args.state_dir,
            )
        except qna.QnAError as error:
            payload = run_outcome._error_payload(
                args,
                reason=error.reason,
                message=error.error,
                paused=False,
                cwd=cwd,
                snapshot=snapshot,
            )
            payload["details"] = dict(error.details)
            return 1, payload

    payload = run_payloads._initial_payload(
        args=args,
        snapshot=snapshot,
        state_dir=state_dir,
        run_dir=run_dir,
        event_path=event_path,
        state_path=state_path,
        lock_info=lock_info,
        cwd=cwd,
        run_id=run_id,
        commit_mode=commit_mode,
        mailbox_activation=mailbox_activation,
        verification_context=verification_context,
    )
    spark_run_suppression: str | None = None
    spark_fallback_reason: str | None = None
    if normalized_review_handoffs:
        payload["normalized_review_handoffs"] = [
            str(path) for path in normalized_review_handoffs
        ]
    if unresolved_commit_group is not None:
        commit_settlement._upsert_commit_group(payload, unresolved_commit_group)
    if commit_group_load_error is not None:
        payload["commit_group_load_error"] = commit_group_load_error
    reply_state_preserver = run_coordination.ReplyStatePreserver(
        state_path=state_path,
        event_path=event_path,
        enabled=reply_text is not None and not args.dry_run,
    )
    pending_reply = (
        run_prompts._PendingReply(
            text=reply_text,
            context=reply_context,
            origin=reply_origin,
            process_start=True,
        )
        if reply_text is not None and reply_context is not None
        else None
    )
    # Session 4 wires committed live-wait resolutions into this slot. Keeping
    # a distinct one-shot type here prevents answerless continuations from
    # inheriting reply semantics or leaking into a later turn.
    pending_resolution: run_prompts._PendingResolution | None = None
    # The correction an accepted redirect left for the next attempt of the same
    # session. A third one-shot slot rather than a reuse of either above: this
    # one is not an answer to anything the worker asked, it pins the harness and
    # conversation the interrupted attempt ran on, and it carries no text -- the
    # feedback stays in the redirect journal.
    pending_redirect: redirect_continuation.PendingRedirect | None = None

    persist_run_state = not (args.dry_run and reply_text is not None)
    output = run_reporting._StderrOutputCoordinator(enabled=not args.json)
    ctx = RunContext(
        run_id=run_id,
        plan_dir=snapshot.plan_dir,
        run_dir=run_dir,
        event_path=event_path,
        payload=payload,
        persistence=run_persistence._RunPersistence(
            write_state=persist_run_state,
            append_events=persist_run_state,
        ),
        # The reporter renders commit failures but owns none of the commit
        # lifecycle's vocabulary, so the run hands it the key terminalization
        # writes owed groups under and the labeller that names one.
        progress=run_reporting._ProgressReporter(
            enabled=not args.json,
            output=output,
            commit_group_label=commit_settlement._unresolved_commit_group_label,
            unresolved_groups_key=commit_settlement.UNRESOLVED_COMMIT_GROUPS_KEY,
        ),
        inline_tail=run_reporting._InlineTailController(
            plan_dir=snapshot.plan_dir,
            state_dir=args.state_dir,
            output=output,
        ),
        reply_state_preserver=reply_state_preserver,
        audit_context=audit_context,
        guard_controller=guard_controller,
        plan_cache=plan_cache,
        verification_context=verification_context,
        commit_generation=commit_generation,
    )

    ctx.persistence.append_event(
        ctx.event_path,
        events.RUN_STARTED_EVENT,
        {
            "plan_dir": str(snapshot.plan_dir),
            "harness": args.harness,
            "model": ctx.payload["model"],
            "model_source": ctx.payload["model_source"],
            "effort": ctx.payload["effort"],
            "effort_source": ctx.payload["effort_source"],
            "spark": ctx.payload["spark"],
            "dry_run": args.dry_run,
            "max_sessions": max_sessions,
            "max_pending_sessions": getattr(args, "max_pending_sessions", None),
            "commit_mode": commit_mode,
            "repair_commit_mode": run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            "note": ctx.payload.get("note"),
            "run_id": ctx.run_id,
            "run_dir": str(ctx.run_dir),
            "run_started_at": ctx.payload["run_started_at"],
            "approval_mode": ctx.payload["approval_mode"],
            "no_mailbox": run_selection._no_mailbox(args),
            "mailbox_enabled": mailbox_enabled,
            "counts": ctx.payload["initial_snapshot"]["counts"],
            "next_pending": ctx.payload["initial_snapshot"]["next_pending"],
            **(
                {
                    "cwd": str(cwd),
                    **run_prompts._workspace_payload_fields(ctx.verification_context),
                }
                if ctx.verification_context.workspace is not None
                else {}
            ),
        },
    )
    if _pause_for_redirect_restart(ctx, args=args, snapshot=snapshot):
        return _terminalize(ctx, 1)
    _reconcile_stranded_redirects(ctx, args=args, snapshot=snapshot)
    ctx.persistence.write_state(ctx.payload)
    run_persistence._notify_agent_mux_loop_started(ctx.payload, ctx.persistence)
    ctx.progress.run_started(ctx.payload)
    ctx.progress.orphaned_state(orphan_candidates)

    invalid_pause = run_inputs._invalid_status_pause(snapshot)
    if invalid_pause is not None:
        reason, message, details = invalid_pause
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=reason,
            message=message,
            details=details,
        )
        return _terminalize(ctx, 1)

    if commit_group_load_error is not None:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="commit-failed",
            message="durable commit-group evidence could not be reconstructed",
            details={"error": commit_group_load_error},
        )
        return _terminalize(ctx, 1)

    if unresolved_commit_group is not None and args.dry_run:
        with ctx.persistence.lock:
            ctx.payload["commit_group_recovery"] = {
                "group_id": unresolved_commit_group.get("group_id"),
                "success": False,
                "pending": True,
                "error": None,
                "group": unresolved_commit_group,
            }
    elif unresolved_commit_group is not None:
        # The restart half of the invariant: a group another run left owed is
        # completed here, before this run launches a worker of its own. A
        # second failure soft-fails like any other; a paused-work group spends
        # the retry that arms right here, because the startup commit gate below
        # would otherwise refuse the dirt it left before the loop boundary that
        # consumes it is ever reached.
        _settle_owed_commit_group_at_startup(ctx, group=unresolved_commit_group)

    review_requested = getattr(args, "review_harness", None) is not None
    max_review_fix_cycles = run_inputs._max_review_fix_cycles(args)
    review_resume: review_continuation.ReviewResume | None = None
    if review_requested:
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

    if snapshot.next_pending is None:
        if not run_inputs._all_sessions_complete(snapshot):
            ctx.pause(
                reason="no-pending-but-incomplete",
                message="no pending session was found, but not every session is "
                "complete",
                details={"snapshot": plans.snapshot_payload(snapshot)},
            )
            return _terminalize(ctx, 1)
        if not review_requested:
            with ctx.persistence.lock:
                ctx.payload["reason"] = "all-complete"
            return _terminalize(ctx, 0)

    if not review_requested:
        pending_review = review_session_body.find_pending_generated_review_session(
            snapshot.plan_dir
        )
        if pending_review is not None:
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason="review-harness-required",
                message="a pending generated review session is present; "
                "rerun with --review-harness to execute it",
                details={
                    "session_id": pending_review.session_id.value,
                    "session_path": str(pending_review.path),
                },
            )
            return _terminalize(ctx, 1)

    if args.dry_run:
        with ctx.persistence.lock:
            ctx.payload["reason"] = "dry-run"
            ctx.payload["planned"] = run_payloads._dry_run_payload(
                args=args,
                snapshot=snapshot,
                cwd=cwd,
                run_dir=ctx.run_dir,
                mailbox_activation=mailbox_activation,
                reply_text=reply_text,
                reply_context=reply_context,
                reply_origin=reply_origin,
                verification_context=ctx.verification_context,
            )
        return _terminalize(ctx, 0)

    if (
        ctx.verification_context.workspace is not None
        and not MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED
    ):
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            message="multi-repository harness turns are disabled until the "
            "authoritative workspace write guard is installed",
            details={
                "workspace_root": str(ctx.verification_context.root),
                "registry_digest": ctx.verification_context.registry_digest,
            },
        )
        return _terminalize(ctx, 1)

    if (
        review_resume is not None
        and review_resume.action in ("run-repairs", "rereview")
        and (
            review_resume.source_disposition is None
            or review_resume.source_disposition
            in (
                *review_continuation.BUDGET_REVIEW_DISPOSITIONS,
                review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            )
        )
        and review_resume.repair_session_ids
        and not review_cycle._has_successful_review_reopen(
            ctx.event_path, review_resume.review_session_id
        )
    ):
        # The atomic child write can win a crash race against event append.
        # Generated children are authoritative.  This also recovers an
        # eventless findings review whose cycle record was lost, preserving
        # its fingerprints before the next review overwrites the artifact.
        recovered_record = review_cycle._review_reopen_record(
            event_path=ctx.event_path,
            payload=ctx.payload,
            resume=review_resume,
            max_review_fix_cycles=max_review_fix_cycles,
            materialized=True,
            repair_session_ids=review_resume.repair_session_ids,
            result="recovered-existing-children",
            recovered=True,
        )
        review_cycle._record_review_reopen(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            record=recovered_record,
        )
        review_resume = review_cycle._set_review_resume(
            snapshot=snapshot,
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
            max_review_fix_cycles=max_review_fix_cycles,
            review_requested=True,
        )

    allow_not_loop_ready = readiness_gate._allow_not_loop_ready(args)
    max_pending_sessions = getattr(args, "max_pending_sessions", None)
    preflight_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "harness": args.harness,
        "commit_mode": commit_mode,
        "state_dir": args.state_dir,
        "max_pending_sessions": max_pending_sessions,
    }
    resume_report = readiness_gate._reduced_preflight_report(
        snapshot.plan_dir,
        snapshot=snapshot,
        lock_info=lock_info,
        preflight_kwargs=preflight_kwargs,
    )
    ctx.core_skipped = resume_report is not None
    if ctx.core_skipped:
        preflight_report = resume_report
        ctx.persistence.append_event(
            ctx.event_path,
            events.PREFLIGHT_SKIPPED_EVENT,
            {
                "readiness_state": preflight_report.readiness.state,
                "readiness_sha256": preflight_report.expected_readiness_sha256,
            },
        )
    else:
        preflight_report = readiness_gate._strip_own_lock_findings(
            preflight.run_preflight(snapshot.plan_dir, **preflight_kwargs),
            lock_info=lock_info,
        )
    ctx.gate_payload = readiness_gate._readiness_gate_payload(preflight_report, cwd=cwd)
    if ctx.core_skipped:
        ctx.gate_payload["core_skipped"] = True
    ctx.gate_passed = ctx.gate_payload["ok"]
    review_only_complete_start = readiness_gate._review_only_complete_start(
        preflight_report,
        review_requested=review_requested,
        snapshot=snapshot,
    )
    with ctx.persistence.lock:
        ctx.payload["readiness_gate"] = ctx.gate_payload
        if preflight_report.readiness is not None:
            ctx.payload["readiness_state_initial"] = preflight_report.readiness.state

    initial_workspace_scope_repair = (
        workspace_evidence._latest_workspace_scope_repair_requirement(
            ctx.event_path, snapshot.next_pending.id
        )
        if ctx.verification_context.workspace is not None
        and snapshot.next_pending is not None
        else None
    )
    if (
        initial_workspace_scope_repair is not None
        and not ctx.gate_passed
        and workspace_evidence._workspace_scope_repair_still_unauthorized(
            initial_workspace_scope_repair,
            _current_session_scope_or_none(ctx, snapshot.next_pending),
        )
    ):
        session_id = snapshot.next_pending.id
        details = {
            **plans.session_details_payload(session_id),
            "readiness_gate": ctx.gate_payload,
            "workspace_write_guard": initial_workspace_scope_repair,
        }
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason=events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            message=f"{plans.session_label(session_id)} has retained changes to "
            "registered repositories outside its write aliases; repair "
            "its durable repository scope and rerun expansion plus "
            "preflight before continuing",
            details=details,
        )
        return _terminalize(ctx, 1)

    non_bypassable = readiness_gate._non_bypassable_blockers(preflight_report)
    if non_bypassable:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="not-loop-ready",
            message=readiness_gate._readiness_gate_failure_message(preflight_report),
            details={**ctx.gate_payload, "blocker": list(non_bypassable)},
        )
        return _terminalize(ctx, 1)

    if (
        not ctx.gate_passed
        and not allow_not_loop_ready
        and not review_only_complete_start
    ):
        preflight_report, repair_payload = readiness_gate._repair_readiness_gate(
            preflight_report,
            plan_dir=snapshot.plan_dir,
            lock_info=lock_info,
            cwd=cwd,
            harness=args.harness,
            commit_mode=commit_mode,
            state_dir=args.state_dir,
            max_pending_sessions=max_pending_sessions,
            event_path=ctx.event_path,
            persistence=ctx.persistence,
            snapshot=snapshot,
            review_requested=review_requested,
            core_skipped=ctx.core_skipped,
        )
        ctx.core_skipped = bool(repair_payload["core_skipped"])
        ctx.gate_payload = readiness_gate._readiness_gate_payload(
            preflight_report, cwd=cwd
        )
        if ctx.core_skipped:
            ctx.gate_payload["core_skipped"] = True
        ctx.gate_passed = ctx.gate_payload["ok"]
        review_only_complete_start = readiness_gate._review_only_complete_start(
            preflight_report,
            review_requested=review_requested,
            snapshot=snapshot,
        )
        with ctx.persistence.lock:
            ctx.payload["readiness_gate"] = ctx.gate_payload
            ctx.payload["readiness_gate_repair"] = repair_payload

        if not ctx.gate_passed and not review_only_complete_start:
            gate_message = readiness_gate._readiness_gate_failure_message(
                preflight_report
            )
            ctx.record_final_snapshot(snapshot)
            ctx.pause(
                reason="not-loop-ready",
                message=gate_message,
                details=ctx.gate_payload,
            )
            return _terminalize(ctx, 1)

    try:
        if commit_mode == run_selection.COMMIT_MODE_END:
            review_guard._validate_commit_at_end_scope(
                snapshot=snapshot,
                max_sessions=max_sessions,
            )
        if commit_mode is not None:
            commit_preflight = review_guard._validate_commit_preflight(
                cwd,
                verification_context=ctx.verification_context,
            )
            with ctx.persistence.lock:
                ctx.payload["commit_preflight"] = commit_preflight
    except RunError as error:
        ctx.record_final_snapshot(snapshot)
        ctx.pause(
            reason="commit-preflight",
            message=str(error),
            details={
                "commit_mode": commit_mode,
                "max_sessions": max_sessions,
                "pending_sessions": plans.status_counts(snapshot)["pending"],
            },
        )
        return _terminalize(ctx, 1)

    should_transition = ctx.gate_passed or (
        allow_not_loop_ready and preflight_report.readiness is not None
    )
    if should_transition:
        running_success, running_error = readiness_gate.write_readiness_state(
            snapshot.plan_dir,
            "running",
            expected_sha256=preflight_report.expected_readiness_sha256,
        )
        with ctx.persistence.lock:
            if running_success:
                ctx.payload["readiness_state_active"] = True
                ctx.payload["readiness_state_current"] = "running"
            else:
                ctx.payload["readiness_state_active"] = False
                ctx.payload["readiness_state_write_error"] = running_error

    run_control_lock = ctx.persistence.lock
    ctx.control_payload = dict(ctx.payload["control"])

    ctx.run_controls = run_control.RunControlListener.for_run(
        args,
        state=run_control.RunControlState(current_harness=args.harness),
        stdin=sys.stdin,
        stderr=output.stream,
        on_action=functools.partial(_handle_run_control_action, ctx),
        on_status=functools.partial(_handle_run_control_status, ctx),
    )
    ctx.run_controls.start()

    # Served unconditionally, unlike the keyboard listener: a `--json`
    # supervised run is precisely the case with no interactive control surface,
    # so it is the one that most needs an `asl control` command observed now
    # rather than at the end of the poll interval. The caller owns the endpoint
    # feeding this event; a run reached without one still polls.
    if wakeup is None:
        wakeup = threading.Event()

    mailbox_poll = run_coordination.MailboxPoll(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=args.state_dir,
        control_state=ctx.run_controls.state,
        seen_control_ids=ctx.seen_external_command_ids,
        payload=ctx.payload,
        event_path=ctx.event_path,
        control_payload=ctx.control_payload,
        persistence=ctx.persistence,
        run_control_lock=run_control_lock,
        enabled=mailbox_enabled,
        wakeup=wakeup,
        guard_controller=ctx.guard_controller,
    )

    # Drain any commands that were queued before the run started so the
    # first turn already reflects them.
    run_coordination.drain_external_control_queue(
        plan_dir=snapshot.plan_dir,
        state_dir_arg=args.state_dir,
        control_state=ctx.run_controls.state,
        seen_ids=ctx.seen_external_command_ids,
        payload=ctx.payload,
        event_path=ctx.event_path,
        control_payload=ctx.control_payload,
        context=None,
        persistence=ctx.persistence,
        run_control_lock=run_control_lock,
        wakeup=wakeup,
        guard_controller=ctx.guard_controller,
    )
    pending_exception_guard: dict[str, Any] | None = None

    # Built once per run, deliberately. `--fresh-conversation` authorizes one
    # fresh start, and a run may take many turns across many sessions; a value
    # rebuilt per turn would re-grant the same permission on every one of them.
    # Nothing persists it, so a later `asl run --resume` or a supervisor
    # relaunch starts from an ungranted authorization however this run ends.
    fresh_conversation = conversation_resume.FreshConversationAuthorization(
        bool(getattr(args, "fresh_conversation", False))
    )

    def disarm_exception_guard() -> None:
        """Retire the crash-path copy of the write guard for this turn.

        The post-turn sequence disarms this *before* it enforces the live
        guard, so the handler below cannot re-enforce the same guard against
        the same before-image if the live one raises. That ordering is why it
        is a callback rather than a value the boundary hands back.
        """
        nonlocal pending_exception_guard
        pending_exception_guard = None

    try:
        while True:
            if ctx.idle_baseline_pinned:
                # A path that reached here after deliberately establishing its
                # own baseline opts out by pinning it. Refreshing would
                # overwrite that earlier instant with this one and charge the
                # gap it was measuring to the runner.
                ctx.idle_baseline_pinned = False
            elif not ctx.first_loop_pass:
                # Every path back to the top got here by finishing
                # runner-owned work — a turn and its verification and
                # finalization, or the all-complete branch's final commit,
                # repair materialization, re-gating, readiness writes and
                # review creation. Refreshing the baseline in this one place
                # covers every path, including ones added later.
                ctx.runner_work_finished_at = clock.utc_now()
            if _pause_for_redirect_restart(ctx, args=args, snapshot=snapshot):
                break
            entering_first_pass = ctx.first_loop_pass
            ctx.first_loop_pass = False
            if not entering_first_pass:
                # One place covers both boundaries a still-owed group must be
                # settled at: ahead of the next worker, and ahead of the
                # end-of-run commit the all-complete branch below runs. The
                # first pass is excluded because the recovery above the loop
                # is that pass's boundary and has just run.
                _settle_owed_commit_group(ctx)
            if _pause_for_unsettled_commit_group(ctx, snapshot=snapshot):
                break
            pending_session = snapshot.next_pending

            if pending_session is None:
                # No session is selectable, which is not yet an ending: the
                # plan may still owe a commit, a review, or the repair
                # children a review reopens. The completion boundary decides
                # which, grows the plan when it must, and hands back the
                # image it ended on.
                completion = run_completion.complete_plan(
                    ctx,
                    snapshot=snapshot,
                    args=args,
                    cwd=cwd,
                    commit_mode=commit_mode,
                    review_requested=review_requested,
                    max_review_fix_cycles=max_review_fix_cycles,
                    allow_not_loop_ready=allow_not_loop_ready,
                    review_guard_excludes=review_guard_excludes,
                    review_guard_include_ignored=review_guard_include_ignored,
                    run_controls=ctx.run_controls,
                    run_control_lock=run_control_lock,
                    wakeup=wakeup,
                    soft_fail_commit=functools.partial(_soft_fail_commit, ctx),
                )
                snapshot = completion.snapshot
                if completion.action == run_completion.ACTION_CONTINUE:
                    continue
                break

            classification = turn_preparation.classify_session(pending_session.path)
            is_review_session = classification.is_review_session
            is_repair_session = classification.is_repair_session
            review_preparation = turn_preparation.prepare_review_session(
                ctx,
                session=pending_session,
                snapshot=snapshot,
                classification=classification,
                cwd=cwd,
                review_guard_excludes=review_guard_excludes,
                review_guard_include_ignored=review_guard_include_ignored,
            )
            snapshot = review_preparation.snapshot
            if review_preparation.paused:
                break
            prepared_review_workspace_snapshot = review_preparation.workspace_snapshot
            review_session_version = review_preparation.session_version

            if (
                not is_review_session
                and max_sessions is not None
                and ctx.completed_count >= max_sessions
            ):
                with ctx.persistence.lock:
                    ctx.payload["reason"] = "max-sessions"
                break

            before = snapshot
            session_id = pending_session.id
            session_label = plans.session_label(session_id)
            session_identity = plans.session_identity_payload(session_id)
            session_details = plans.session_details_payload(session_id)
            ctx.last_session_details = session_details

            if is_review_session:
                with ctx.persistence.lock:
                    if ctx.payload.get("review_session_id") != session_id:
                        ctx.payload["review_session_id"] = session_id

            with ctx.persistence.lock:
                turn_index = len(ctx.payload["turns"]) + 1
                turns_already_run = bool(ctx.payload["turns"])
            paths = turn_execution.turn_paths(
                ctx.run_dir,
                turn_index=turn_index,
                session_id=session_id,
            )
            implementation_overrides: Mapping[str, Mapping[str, str]] = {}
            pre_turn_control_context = {
                **session_identity,
                "turn_index": turn_index,
                "total_sessions": sum(plans.status_counts(before).values()),
            }
            run_coordination.drain_external_control_queue(
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
                control_state=ctx.run_controls.state,
                seen_ids=ctx.seen_external_command_ids,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                context=pre_turn_control_context,
                persistence=ctx.persistence,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                guard_controller=ctx.guard_controller,
            )
            with run_control_lock:
                # A stop observed here must never let another turn start. Only
                # a stop that lands after a turn already ran in this process
                # qualifies: one queued before the run began still means "run
                # the first session, then stop". A pending reply or resolution
                # continuation belongs to the session already in flight, so it
                # runs before the stop is honored.
                stop_before_turn = (
                    turns_already_run
                    and pending_reply is None
                    and pending_resolution is None
                    and ctx.run_controls.state.stop_after_current
                )
                if stop_before_turn:
                    run_termination._honor_stop_after_current(
                        ctx.run_controls,
                        snapshot=before,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        context=pre_turn_control_context,
                        persistence=ctx.persistence,
                    )
            if stop_before_turn:
                break
            if is_review_session:
                turn_harness = args.review_harness
            else:
                with run_control_lock:
                    turn_harness = run_selection._selected_future_harness(
                        args, ctx.control_payload
                    )
                    implementation_overrides = (
                        ctx.run_controls.state.implementation_overrides
                    )
            if turn_harness is None:
                raise RunError("--review-harness is required for a review session")
            turn_harness = _redirected_turn_harness(
                ctx,
                pending=pending_redirect,
                selected=turn_harness,
                session_id=session_id,
                turn_index=turn_index,
            )
            spark_eligible = (
                getattr(args, "spark", False)
                and spark_run_suppression is None
                and spark_fallback_reason is None
                and turn_harness == "codex"
                and not is_review_session
                and not implementation_overrides.get(turn_harness)
                and not review_repair.is_generated_review_repair_session_file(
                    pending_session.path
                )
                and run_selection._session_spark_suitability(
                    before.plan_dir, session_id
                )[0]
            )
            if spark_eligible:
                try:
                    refreshed_availability = model_availability.reconcile_live(
                        state.state_root(args.state_dir),
                        run_selection.SPARK_MODEL,
                        spark_status.read_status(),
                        run_id=ctx.run_id,
                    )
                except OSError as error:
                    refreshed_availability = model_availability.Availability(
                        False,
                        "ledger-unreadable",
                        warning=f"model availability ledger is unreadable: {error}",
                    )
                with ctx.persistence.lock:
                    ctx.payload["spark"].update(refreshed_availability.to_dict())
            # The immediate retry after a Spark failure stays on the baseline.
            # Later sessions may use a fresh live read, including an early reset.
            requested_model, turn_model_effort, spark_route = (
                run_selection._spark_turn_route(
                    args,
                    plan_dir=before.plan_dir,
                    session_path=pending_session.path,
                    session_id=session_id,
                    harness=turn_harness,
                    is_review_turn=is_review_session,
                    implementation_overrides=implementation_overrides,
                    spark_state=ctx.payload["spark"],
                    run_suppression=(
                        spark_run_suppression
                        or (
                            f"{spark_fallback_reason}-immediate-fallback"
                            if spark_fallback_reason is not None
                            else None
                        )
                    ),
                    fallback_reason=spark_fallback_reason,
                )
            )
            spark_fallback_reason = None
            with ctx.persistence.lock:
                ctx.payload["spark"]["active_route"] = spark_route
            requested_effort = run_selection._requested_effort(
                args,
                harness=turn_harness,
                is_review_turn=is_review_session,
                implementation_overrides=implementation_overrides,
            )
            preflight_failure = run_selection._harness_preflight_failure(
                turn_harness,
                session_id=session_id,
            )
            if preflight_failure is not None:
                message, details = preflight_failure
                ctx.record_final_snapshot(before)
                ctx.pause(
                    reason="harness-unavailable",
                    message=f"harness unavailable before {session_label}: {message}",
                    details=details,
                )
                break
            packet_inputs = turn_preparation.collect_packet_inputs(
                ctx,
                snapshot=before,
                classification=classification,
                args=args,
                session_id=session_id,
                mailbox_enabled=mailbox_enabled,
            )
            workspace = ctx.verification_context.workspace
            session_scope = (
                run_prompts._session_scope(pending_session, workspace)
                if workspace is not None
                else None
            )
            retained_scope_repair = turn_preflight.resolve_retained_scope_repair(
                event_path=ctx.event_path,
                session_id=session_id,
                workspace=workspace,
            )
            repair_resumption = turn_preparation.resume_repair_session(
                ctx,
                session=pending_session,
                snapshot=before,
                classification=classification,
                args=args,
                cwd=cwd,
                workspace=workspace,
                session_scope=session_scope,
                session_label=session_label,
                session_identity=session_identity,
                session_details=session_details,
                retained_scope_repair=retained_scope_repair,
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=review_requested,
            )
            session_scope = repair_resumption.session_scope
            regenerated_write_scope = repair_resumption.regenerated_write_scope
            if repair_resumption.paused:
                break
            turn_reply = pending_reply
            turn_resolution = pending_resolution
            use_reply_prompt = turn_reply is not None
            use_resolution_prompt = turn_resolution is not None
            # Immediately before the prompt is built, because the boundary the
            # operator experiences is "did it make it into the prompt".
            turn_corrections = _carried_corrections(
                ctx,
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
                pending=pending_redirect,
                session_id=session_id,
                turn_index=turn_index,
                harness=turn_harness,
                recovery=ctx.redirect_recovery_report,
            )
            turn_prompt = turn_preparation.prepare_turn_prompt(
                ctx,
                snapshot=before,
                classification=classification,
                inputs=packet_inputs,
                args=args,
                cwd=cwd,
                harness=turn_harness,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                session_scope=session_scope,
                workspace=workspace,
                mailbox_enabled=mailbox_enabled,
                reply=turn_reply,
                resolution=turn_resolution,
                corrections=turn_corrections,
            )
            if turn_prompt.paused:
                _release_carried_corrections(
                    ctx,
                    plan_dir=before.plan_dir,
                    state_dir_arg=args.state_dir,
                    carried=turn_corrections,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
                break
            continuation = turn_preparation.prepare_turn_continuation(
                ctx,
                snapshot=before,
                args=args,
                cwd=cwd,
                harness=turn_harness,
                session_id=session_id,
                session_details=session_details,
                session_scope=session_scope,
                workspace=workspace,
                authorization=fresh_conversation,
                # The conversation the correction was written against is the
                # one this attempt continues, and the resumed worker is told
                # which operations were cut short by identifier. The words stay
                # in the redirect journal: this is the handle, not the feedback.
                interrupted_operations=(
                    pending_redirect.resumable_operation_ids()
                    if pending_redirect is not None
                    and pending_redirect.session_id == session_id
                    else ()
                ),
            )
            if continuation.paused:
                _release_carried_corrections(
                    ctx,
                    plan_dir=before.plan_dir,
                    state_dir_arg=args.state_dir,
                    carried=turn_corrections,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
                break
            conversation_capture = continuation.capture(
                state.plan_state_dir(before.plan_dir, args.state_dir)
            )
            prompt = turn_prompt.prompt
            reply_source_meta = turn_prompt.reply_source
            resolution_source_meta = turn_prompt.resolution_source
            turn_record = turn_preflight.build_active_turn(
                ctx,
                snapshot=before,
                classification=classification,
                prompt=turn_prompt,
                session_identity=session_identity,
                turn_index=turn_index,
                paths=paths,
                harness=turn_harness,
                model_effort=turn_model_effort,
                spark_route=spark_route,
                cwd=cwd,
            )
            active_turn = turn_record.payload
            total_sessions = turn_record.total_sessions
            log_stream_callback = turn_execution.prepare_stream_logs(paths)
            mailbox_poll.set_context(active_turn)
            # Handed to the turn before it starts even though nothing has
            # requested an abort yet: the wait loop can only be cancellable if
            # the signal object already exists when the harness is launched.
            abort_signal = harnesses.AbortSignal()
            mailbox_poll.set_abort(abort_signal)
            # The turn's redirect gate, bound to the same identity a producer
            # reads out of `active_turn` when it submits a correction, and
            # sharing the abort signal above rather than owning a second one:
            # the cooperative cancellation a redirect initiates and the one an
            # operator abort initiates are the same request to the same turn,
            # and the signal is also the only proof the escalation has that the
            # worker was reaped. It watches its own event, not the control
            # wakeup, so a correction never queues behind the control poll.
            turn_redirect = redirect_continuation.TurnRedirect(
                plan_dir=before.plan_dir,
                target=redirect_store.RedirectTarget(
                    run_id=ctx.run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                ),
                state_dir=args.state_dir,
                wakeup=redirect_wakeup,
                cancellation=abort_signal,
            )
            # The other turn-scoped control input beside the abort signal, and
            # bound to the same identity the delivery ledger records. It stays
            # inert until the harness opens it around the live process.
            delivery_endpoint = guidance_delivery.LiveDeliveryEndpoint(
                guidance_delivery.TurnIdentity(
                    run_id=ctx.run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
            )
            mailbox_poll.set_live_delivery(delivery_endpoint)

            live_turn = run_commit._LiveTurn(
                payload=ctx.payload,
                event_path=ctx.event_path,
                progress=ctx.progress,
                active_turn=active_turn,
                persistence=ctx.persistence,
                input_wait_probe=functools.partial(
                    turn_execution.worker_input_wait_probe,
                    plan_dir=before.plan_dir,
                    run_id=ctx.run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                    state_dir=args.state_dir,
                ),
                poll_callback=mailbox_poll,
                poll_interval=turn_execution.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
                wakeup=wakeup,
            )
            # One executor per turn: the identity and the artifact paths are
            # what make this turn this turn, so they are bound once here
            # rather than repeated at each of the three transitions below.
            turn_executor = turn_execution.TurnExecutor(
                identity=turn_execution.TurnIdentity(
                    run_id=ctx.run_id,
                    turn_index=turn_index,
                    session_id=session_id,
                ),
                paths=paths,
                event_path=ctx.event_path,
                persistence=ctx.persistence,
                progress=ctx.progress,
                plan_cache=ctx.plan_cache,
            )

            stream_callback = turn_execution.live_stream_callback(
                log_stream_callback, live_turn
            )

            with run_control_lock:
                ctx.run_control_context = dict(active_turn)
            baselines = turn_preflight.capture_turn_baselines(
                ctx,
                snapshot=before,
                state_dir_arg=args.state_dir,
                cwd=cwd,
                classification=classification,
                workspace=workspace,
                session_scope=session_scope,
                review_base=prepared_review_workspace_snapshot,
                retained_scope_repair=retained_scope_repair,
                regenerated_write_scope=regenerated_write_scope,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                review_guard_excludes=review_guard_excludes,
                review_guard_include_ignored=review_guard_include_ignored,
                active_turn=active_turn,
                live_turn=live_turn,
                paths=paths,
            )
            if baselines.paused:
                _release_carried_corrections(
                    ctx,
                    plan_dir=before.plan_dir,
                    state_dir_arg=args.state_dir,
                    carried=turn_corrections,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
                break
            workspace_snapshot_before = baselines.workspace_snapshot
            legacy_snapshot_before = baselines.legacy_snapshot
            review_fingerprint_before = baselines.review_fingerprint
            external_digests_before = baselines.external_digests
            turn_started_ns = baselines.turn_started_ns
            pending_exception_guard = baselines.pending_exception_guard
            protected_path_baseline = baselines.protected_path_baseline
            plan_artifacts_before = plans.session_artifact_digests(
                before.plan_dir, exclude_session_id=session_id
            )
            if pending_redirect is not None:
                # Consumed at the last point before the turn starts, and only
                # against the session it was accepted for. Selection can move on
                # while a correction is in flight -- a repair child, the
                # generated review -- and a correction spent on a session the
                # operator was not talking about is worse than one dropped, so
                # the mismatch is recorded and the slot cleared either way.
                consumed_redirect = pending_redirect
                pending_redirect = None
                ctx.persistence.append_event(
                    ctx.event_path,
                    events.REDIRECT_CONTINUATION_EVENT,
                    {
                        "run_id": ctx.run_id,
                        "turn_index": turn_index,
                        **session_identity,
                        "outcome": (
                            "resumed"
                            if consumed_redirect.session_id == session_id
                            else "dropped"
                        ),
                        "redirect": consumed_redirect.to_payload(),
                        "harness": turn_harness,
                        "conversation": continuation.resume_conversation_id,
                        "interrupted_operations": list(
                            consumed_redirect.resumable_operation_ids()
                        ),
                    },
                )
                if consumed_redirect.session_id == session_id:
                    with ctx.persistence.lock:
                        active_turn["redirect_resumed"] = consumed_redirect.to_payload()

            if use_reply_prompt:
                assert reply_source_meta is not None and turn_reply is not None
                # Consumption is explicit rather than positional: a mailbox
                # continuation can begin on any later turn index, and no reply
                # may leak into a subsequent session prompt.
                pending_reply = None
                if turn_reply.process_start:
                    ctx.reply_state_preserver.mark_consumed()
                reply_event_payload = {
                    "run_id": ctx.run_id,
                    "turn_index": turn_index,
                    "session_id": session_id,
                    "reply_length": len(turn_reply.text),
                    "source": reply_source_meta,
                }
                if turn_reply.question_id is not None:
                    reply_event_payload.update(
                        {
                            "origin": turn_reply.origin,
                            "question_id": turn_reply.question_id,
                        }
                    )
                ctx.persistence.append_event(
                    ctx.event_path,
                    events.REPLY_PROVIDED_EVENT,
                    reply_event_payload,
                )
            elif use_resolution_prompt:
                assert (
                    resolution_source_meta is not None and turn_resolution is not None
                )
                # Clear the pending object before recording turn start. A
                # successful start therefore consumes this exact resolution
                # once, even when the same session needs another turn later.
                pending_resolution = None
                ctx.persistence.append_event(
                    ctx.event_path,
                    events.RESOLUTION_CONSUMED_EVENT,
                    {
                        "run_id": ctx.run_id,
                        "turn_index": turn_index,
                        "session_id": session_id,
                        "question_id": turn_resolution.question_id,
                        "resolution_message_id": (
                            turn_resolution.resolution_message_id
                        ),
                        "resolution_seq": turn_resolution.resolution_seq,
                        "action": turn_resolution.action,
                        "note_length": resolution_source_meta["note_length"],
                        "source": resolution_source_meta,
                    },
                )

            # Materialized per turn, not once per run: the guard-bin directory
            # must exist before the child's PATH names it, and a guard added
            # during the previous turn has to be armed for this one. The
            # workspace binding is refreshed here too, because it is what turns
            # the absolute path a tool call carries into the `alias:path` a
            # rule is written in, and a run can change harness mid-flight.
            with run_control_lock:
                ctx.guard_controller.set_workspace_aliases(
                    {
                        repository.alias: str(repository.root)
                        for repository in workspace.repositories
                    }
                    if workspace is not None
                    else {guards.LEGACY_PATH_QUALIFIER: str(cwd)}
                )
                ctx.guard_controller.mark_turn_snapshot(protected_path_baseline)
                turn_env = ctx.guard_controller.prepare_turn(
                    harness=turn_harness,
                    pretool_armed=harnesses.get_adapter(
                        turn_harness
                    ).enforces_guard_pretool,
                )

            record_guidance_delivery = (
                functools.partial(
                    run_selection._record_next_turn_guidance_delivery,
                    plan_dir=before.plan_dir,
                    state_dir_arg=args.state_dir,
                    event_path=ctx.event_path,
                    append_event=ctx.persistence.append_event,
                    pending_guidance=packet_inputs.pending_guidance,
                    harness=turn_harness,
                    run_id=ctx.run_id,
                    session_id=session_id,
                    turn_index=turn_index,
                )
                if (
                    packet_inputs.pending_guidance
                    and harnesses.delivers_guidance_next_turn(turn_harness)
                )
                else None
            )

            # The launch side of the coalescing boundary, and this attempt's
            # alone: it settles the carry as delivered when the harness reports
            # the prompt submitted, and hands it back below if the turn ended
            # without ever getting that far.
            turn_delivery = _TurnDelivery(
                ctx,
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
                carried=turn_corrections,
                session_id=session_id,
                turn_index=turn_index,
                harness=turn_harness,
            )

            # The last durable statement made while it is still true that the
            # words have not gone out. After this line the journal can no longer
            # prove this attempt did not submit, which is the entire reason a
            # crash from here on reads as a question instead of an invitation to
            # deliver the same correction again.
            if not _launch_carried_corrections(
                ctx,
                plan_dir=before.plan_dir,
                state_dir_arg=args.state_dir,
                carried=turn_corrections,
                session_id=session_id,
                turn_index=turn_index,
                harness=turn_harness,
            ):
                # Undeclarable, so unsendable. The carry goes back by the same
                # route every other prelaunch refusal uses -- a best effort,
                # since the journal that just refused one line may refuse this
                # one too, and a carry left standing is reported as uncertain
                # rather than handed out again.
                _release_carried_corrections(
                    ctx,
                    plan_dir=before.plan_dir,
                    state_dir_arg=args.state_dir,
                    carried=turn_corrections,
                    session_id=session_id,
                    turn_index=turn_index,
                    harness=turn_harness,
                )
                ctx.record_final_snapshot(before)
                ctx.pause(
                    reason=events.PAUSE_REASON_REDIRECT_DELIVERY_UNRECORDABLE,
                    message=(
                        "the redirect journal would not record that this turn's "
                        "prompt was about to be submitted, so the turn was not "
                        "started"
                    ),
                    details={
                        "session_id": session_id,
                        "turn_index": turn_index,
                        "harness": turn_harness,
                        "operation_ids": list(turn_corrections.operation_ids())
                        if turn_corrections is not None
                        else [],
                    },
                )
                break

            # Started before the launch, not after it: a correction submitted
            # against this turn's identity can land the instant that identity is
            # published, and the gate's own bind wait is what holds such an
            # acceptance until there is a worker to interrupt.
            turn_redirect.start()
            execution = turn_executor.launch(
                turn_execution.TurnLaunch(
                    env=turn_env,
                    harness=turn_harness,
                    cwd=cwd,
                    plan_dir=before.plan_dir,
                    prompt=prompt,
                    unsafe_bypass=run_selection._unsafe_bypass(args),
                    timeout=timeout,
                    model=requested_model,
                    effort=requested_effort,
                    abort=abort_signal,
                    live_delivery=delivery_endpoint,
                    stream_callback=stream_callback,
                    on_prompt_submitted=turn_delivery.note_prompt_submitted,
                    # The guidance ledger stays on the spawn it has always been
                    # recorded at. It answers a different question than the
                    # redirect journal -- which pending guidance this turn's
                    # prompt was built from -- and nothing reads it back as
                    # proof that a worker received anything.
                    on_spawned=record_guidance_delivery,
                    build_invocation=functools.partial(
                        run_prompts.build_harness_invocation,
                        turn_harness,
                        cwd=cwd,
                        plan_dir=before.plan_dir,
                        unsafe_bypass=run_selection._unsafe_bypass(args),
                        last_message_path=paths["last_message"],
                        model=requested_model,
                        effort=requested_effort,
                    ),
                    resume_conversation_id=continuation.resume_conversation_id,
                    on_conversation=conversation_capture,
                    on_worker=turn_redirect.bind_worker,
                ),
                live_turn=live_turn,
                active_turn=active_turn,
                payload=ctx.payload,
                after_stop=functools.partial(
                    _close_turn_controls, turn_redirect, mailbox_poll
                ),
                on_failure=functools.partial(_abandon_turn, ctx, turn_redirect),
            )
            result = execution.result
            live_turn_payload = execution.live_turn
            # Immediately on the way out of the turn, ahead of every guard that
            # can end the run: a turn that can show it never submitted its
            # prompt is holding words that belong back in the journal, and the
            # pause it is about to take is exactly when the operator would look
            # for them. The result is what carries that showing; a turn that
            # cannot make it leaves the carry where it is.
            turn_delivery.settle_unsubmitted(result)
            with run_control_lock:
                ctx.run_control_context = None
            if conversation_capture is not None:
                # Ahead of every post-turn guard, because confirmation is what
                # makes the conversation resumable and every branch below can
                # end the run. A guard that refuses the turn still leaves work
                # in the conversation the next attempt has to continue.
                outcome_record = conversation_resume.record_turn_outcome(
                    state.plan_state_dir(before.plan_dir, args.state_dir),
                    capture=conversation_capture,
                    result=result,
                    run_id=ctx.run_id,
                    turn=turn_index,
                )
                if outcome_record.error is not None:
                    ctx.persistence.append_event(
                        ctx.event_path,
                        events.CONVERSATION_CONTINUATION_EVENT,
                        {
                            "run_id": ctx.run_id,
                            **plans.session_identity_payload(session_id),
                            "disposition": continuation.disposition,
                            "conversation": outcome_record.conversation,
                            "confirmed": outcome_record.confirmed,
                            "detail": outcome_record.error,
                        },
                    )
            plan_artifacts_written_by_runner: tuple[str, ...] = ()
            if is_review_session:
                # Stamped from the turn's own completion rather than from the
                # commit, because every guard and terminating branch below can
                # end the run before the commit that would otherwise be the
                # only record that an independent review happened at all.
                executed_review_requires_artifact = (
                    review_session_version is not None
                    and review_session_version >= review.STRUCTURED_REVIEW_MIN_VERSION
                )
                with ctx.persistence.lock:
                    ctx.payload["review_session_executed"] = session_id
                normalized_after_review = (
                    plans.normalize_generated_review_handoff_artifacts(before.plan_dir)
                )
                if normalized_after_review:
                    normalized_review_handoffs.extend(normalized_after_review)
                    plan_artifacts_written_by_runner = tuple(
                        path.name for path in normalized_after_review
                    )
                    with ctx.persistence.lock:
                        ctx.payload["normalized_review_handoffs"] = [
                            str(path) for path in normalized_review_handoffs
                        ]
            finalization = turn_executor.finalize(
                execution,
                before=before,
                unredacted_prompt=(
                    prompt if use_reply_prompt or use_resolution_prompt else None
                ),
                pending_exception_guard=pending_exception_guard,
            )
            after = finalization.after
            plan_error = finalization.plan_error
            turn_payload = finalization.turn_payload
            if turn_redirect.redirected or turn_redirect.to_payload()["refused"]:
                # Recorded ahead of every guard because this is what names the
                # turn's disposition: `_terminating_result_disposition` reads
                # the acceptance back out of here rather than off the harness
                # result, which cannot distinguish a redirect from an abort. A
                # turn that only ever refused corrections records them too --
                # an operator whose correction arrived a moment too late is
                # owed the evidence of where it went.
                turn_payload[redirect_continuation.TURN_PAYLOAD_KEY] = (
                    turn_redirect.to_payload()
                )
            # The turn enters run state as executed-but-unaccepted. Every guard
            # below may still amend or refuse it; `turn_execution.commit_turn`
            # is what replaces this record with the published one.
            with ctx.persistence.lock:
                ctx.payload["turns"].append(turn_payload)
                # A harness that reports no model leaves the last observation
                # standing rather than erasing it.
                if turn_payload.get("observed_model") is not None:
                    ctx.payload.update(
                        turn_execution.observed_model_payload(
                            turn_payload["observed_model"]
                        )
                    )
                ctx.payload["model_projection"] = run_commit._merged_model_projection(
                    ctx.payload.get("model_projection"), turn_payload
                )
                turn_identity = {
                    "run_id": ctx.run_id,
                    "turn_index": turn_index,
                    **plans.session_identity_payload(session_id),
                }
                run_persistence._append_process_sweep_event(
                    event_path=ctx.event_path,
                    persistence=ctx.persistence,
                    report=result.process_sweep,
                    phase="turn",
                    identity=turn_identity,
                )
                run_persistence._append_harness_degradation_event(
                    event_path=ctx.event_path,
                    persistence=ctx.persistence,
                    degradation=turn_payload.get("harness_degradation"),
                    identity=turn_identity,
                )
                ctx.persistence.write_state(ctx.payload)

            if abort_signal.armed and not result.aborted:
                # The worker ended the turn inside the grace window, so nothing
                # was killed and whatever it recorded stands: the pause or
                # completion path below owns the outcome from here.
                with run_control_lock:
                    run_termination._record_abort_satisfied_by_worker(
                        ctx.run_controls,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        context=turn_payload,
                        persistence=ctx.persistence,
                    )

            postflight = turn_postflight.enforce_post_turn_guards(
                ctx,
                before=before,
                after=after,
                result=result,
                turn_payload=turn_payload,
                workspace=workspace,
                session_scope=session_scope,
                workspace_snapshot_before=workspace_snapshot_before,
                legacy_snapshot_before=legacy_snapshot_before,
                review_fingerprint_before=review_fingerprint_before,
                external_digests_before=external_digests_before,
                turn_started_ns=turn_started_ns,
                plan_artifacts_before=plan_artifacts_before,
                plan_artifacts_written_by_runner=plan_artifacts_written_by_runner,
                is_review_session=is_review_session,
                cwd=cwd,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                review_guard_excludes=review_guard_excludes,
                review_guard_include_ignored=review_guard_include_ignored,
                paths=paths,
                state_dir_arg=args.state_dir,
                disarm_exception_guard=disarm_exception_guard,
            )
            if postflight.stopped:
                break
            # The one arming site, and it sits here rather than at any pause
            # because of what is above it: every post-turn write guard has run
            # and reported. A write-scope violation or a protected-path change
            # stops inside `enforce_post_turn_guards` and leaves the loop on
            # the line above, so those pauses structurally never arm this and
            # the dirt they retain for a human to inspect is never a candidate
            # for a commit. That guarantee is the control flow, not a blacklist
            # of reasons the settlement below has to keep in step with.
            ctx.retained_work = commit_settlement.RetainedWorkContext(
                session_id=session_id,
                session_label=session_label,
                turn_payload=turn_payload,
                result_path=paths["result"],
                artifact_dir=paths["artifact_dir"],
                is_review_session=is_review_session,
                is_repair_session=is_repair_session,
            )
            terminating_disposition = postflight.terminating_disposition
            # The same site, for the same reason: an attempt only joins the
            # session's accumulated evidence once every post-turn write guard
            # has cleared it. A refused turn left the loop above, so its paths
            # can never be carried into a later attempt's commit scope -- the
            # control flow guarantees it rather than a list of reasons the merge
            # would have to keep in step with. The record is the guard's own
            # evidence as it now stands, which is already cumulative, so the
            # next attempt merges against one record instead of a history.
            ctx.session_attempts.record(
                session_id=session_id,
                turn_index=turn_index,
                disposition=terminating_disposition,
                guard=turn_payload.get("workspace_write_guard"),
            )
            with ctx.persistence.lock:
                ctx.payload[continuation_guards.SESSION_ATTEMPTS_PAYLOAD_KEY] = (
                    ctx.session_attempts.to_payload()
                )
            ctx.persistence.write_state(ctx.payload)

            if terminating_disposition == "interrupted":
                turn_dispositions.pause_interrupted_run(
                    ctx,
                    before=before,
                    after=after,
                    session_details=session_details,
                )
                break

            if terminating_disposition == redirect_continuation.DISPOSITION_REDIRECTED:
                redirected = turn_dispositions.settle_redirected_turn(
                    ctx,
                    ctx.run_controls,
                    before=before,
                    after=after,
                    redirect=turn_redirect,
                    harness=turn_harness,
                    conversation_id=(
                        result.conversation_id or continuation.resume_conversation_id
                    ),
                    session_id=session_id,
                    session_label=session_label,
                    turn_index=turn_index,
                    result=result,
                    turn_payload=turn_payload,
                    paths=paths,
                    session_details=session_details,
                    pending=pending_redirect,
                )
                if redirected is None:
                    break
                pending_redirect = redirected.pending
                if redirected.snapshot is not None:
                    snapshot = redirected.snapshot
                    continue
                # The plan could not be inspected after the forced stop. The
                # correction is already held above, so it rides along into
                # whatever attempt the repair below produces rather than being
                # spent on a turn that has no plan image to run against.
                terminating_disposition = "malformed-plan"

            if terminating_disposition == "timeout":
                turn_dispositions.pause_timed_out_turn(
                    ctx,
                    before=before,
                    after=after,
                    session_label=session_label,
                    session_details=session_details,
                )
                break

            if terminating_disposition == "aborted":
                turn_dispositions.record_aborted_turn(
                    ctx,
                    ctx.run_controls,
                    before=before,
                    after=after,
                    session_id=session_id,
                    turn_payload=turn_payload,
                )
                break
            if terminating_disposition == "restore-refused":
                turn_dispositions.settle_restore_refused(
                    ctx,
                    plan_state=state.plan_state_dir(before.plan_dir, args.state_dir),
                    before=before,
                    after=after,
                    result=result,
                    continuation=continuation,
                    session_id=session_id,
                    session_label=session_label,
                    turn_index=turn_index,
                    turn_payload=turn_payload,
                    session_details=session_details,
                    paths=paths,
                )
                break

            if terminating_disposition == "malformed-plan":
                corrected = turn_dispositions.settle_malformed_plan(
                    ctx,
                    before=before,
                    workspace=workspace,
                    session_id=session_id,
                    turn_index=turn_index,
                    plan_error=plan_error,
                    turn_payload=turn_payload,
                    paths=paths,
                    session_details=session_details,
                )
                if corrected is not None:
                    snapshot = corrected
                    continue
                break

            # Every disposition that survives the branches above names a turn
            # the plan could be inspected after, so the malformed-plan branch
            # owns the only `after is None` turn that reaches here.
            assert after is not None
            after, paused_status_repair = (
                handoff_correction._repair_paused_handoff_status(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    plan_cache=ctx.plan_cache,
                    after=after,
                    session_id=session_id,
                    turn_index=turn_index,
                    runner_pause=turn_payload.get("runner_pause"),
                )
            )
            invalid_pause = run_inputs._invalid_status_pause(after)
            if invalid_pause is not None:
                reason, message, details = invalid_pause
                ctx.record_final_snapshot(after)
                turn_execution.record_turn_rejected(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    turn_payload=turn_payload,
                    session_id=session_id,
                    guard="plan-status-validity",
                    reason=reason,
                    detail=details,
                )
                ctx.pause(
                    reason=reason,
                    message=message,
                    details=details | session_details,
                )
                break

            selected_after = next(
                (item for item in after.sessions if item.id == session_id),
                None,
            )
            if (
                mailbox_enabled
                and selected_after is not None
                and selected_after.status == "complete"
                and handoff_guards._enforce_mailbox_completion_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    after=after,
                    session_id=session_id,
                    session_label=session_label,
                    session_details=session_details,
                    turn_index=turn_index,
                    state_dir_arg=args.state_dir,
                    turn_payload=turn_payload,
                    paths=paths,
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                )
            ):
                break

            if terminating_disposition == "runner-pause":
                continuation = turn_dispositions.settle_runner_pause(
                    ctx,
                    after=after,
                    session_id=session_id,
                    turn_index=turn_index,
                    result=result,
                    turn_payload=turn_payload,
                    session_details=session_details,
                    paused_status_repair=paused_status_repair,
                    mailbox_enabled=mailbox_enabled,
                    mailbox_poll=mailbox_poll,
                    plan_state=state_dir,
                    state_path=state_path,
                    state_dir_arg=args.state_dir,
                    # Resolved here rather than inside the boundary so this
                    # module keeps owning all three names: callers patch them
                    # on the runner to assert a wait was or was not entered.
                    prepare_wait=run_coordination.prepare_mailbox_reply_wait,
                    wait_for_reply=run_coordination.wait_for_mailbox_reply,
                    finish_lifecycle=run_coordination.finish_mailbox_reply_wait_lifecycle,
                )
                if continuation is not None:
                    snapshot = continuation.snapshot
                    pending_reply = continuation.pending_reply
                    pending_resolution = continuation.pending_resolution
                    continue
                break
            if terminating_disposition == "nonzero-exit":
                retry = turn_dispositions.settle_nonzero_exit(
                    ctx,
                    before=before,
                    after=after,
                    result=result,
                    spark_route=spark_route,
                    session_id=session_id,
                    session_label=session_label,
                    turn_index=turn_index,
                    turn_payload=turn_payload,
                    session_details=session_details,
                    paths=paths,
                    state_dir_arg=args.state_dir,
                    spark_run_suppression=spark_run_suppression,
                )
                if retry is not None:
                    snapshot = retry.snapshot
                    spark_fallback_reason = retry.fallback_reason
                    spark_run_suppression = retry.run_suppression
                    continue
                break

            settlement = turn_settlement.settle_turn(
                ctx,
                args=args,
                cwd=cwd,
                before=before,
                after=after,
                snapshot=snapshot,
                session_id=session_id,
                session_label=session_label,
                session_details=session_details,
                turn_index=turn_index,
                total_sessions=total_sessions,
                turn_payload=turn_payload,
                paths=paths,
                plan_artifacts_before=plan_artifacts_before,
                plan_artifacts_written_by_runner=plan_artifacts_written_by_runner,
                is_review_session=is_review_session,
                is_repair_session=is_repair_session,
                review_session_version=review_session_version,
                allow_not_loop_ready=allow_not_loop_ready,
                commit_mode=commit_mode,
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=review_requested,
                turn_executor=turn_executor,
                live_turn_payload=live_turn_payload,
                run_controls=ctx.run_controls,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                soft_fail_commit=functools.partial(_soft_fail_commit, ctx),
            )
            snapshot = settlement.snapshot
            if settlement.action == turn_settlement.ACTION_STOP:
                break
            if settlement.action == turn_settlement.ACTION_RETRY:
                continue

            run_coordination.drain_external_control_queue(
                plan_dir=snapshot.plan_dir,
                state_dir_arg=args.state_dir,
                control_state=ctx.run_controls.state,
                seen_ids=ctx.seen_external_command_ids,
                payload=ctx.payload,
                event_path=ctx.event_path,
                control_payload=ctx.control_payload,
                context=settlement.turn_record,
                persistence=ctx.persistence,
                run_control_lock=run_control_lock,
                wakeup=wakeup,
                guard_controller=ctx.guard_controller,
            )
            with run_control_lock:
                # An unpending request is left set on purpose: the all-complete
                # branch at the top of the loop owns it, so a stop is never
                # swallowed by a run that still has a review to append.
                stop_now = (
                    ctx.run_controls.state.stop_after_current
                    and snapshot.next_pending is not None
                )
                if stop_now:
                    run_termination._honor_stop_after_current(
                        ctx.run_controls,
                        snapshot=snapshot,
                        payload=ctx.payload,
                        event_path=ctx.event_path,
                        control_payload=ctx.control_payload,
                        context=settlement.turn_record,
                        persistence=ctx.persistence,
                    )
            if stop_now:
                break

    except BaseException as error:
        guard_context = pending_exception_guard
        pending_exception_guard = None
        if guard_context is not None:
            live_turn = guard_context["live_turn"]
            live_turn.stop()
            finished_at = clock.utc_now()
            live_turn_payload = live_turn.finish(finished_at)
            with run_control_lock:
                ctx.run_control_context = None

            after = guard_context.get("after")
            if after is None:
                try:
                    after = ctx.plan_cache.get_fresh(snapshot.plan_dir)
                except Exception:
                    after = None
            turn_payload = guard_context.get("turn_payload")
            if not isinstance(turn_payload, dict):
                turn_payload = {
                    **live_turn_payload,
                    "run_id": ctx.run_id,
                    "turn_index": live_turn_payload.get("turn_index"),
                    **plans.session_identity_payload(str(guard_context["session_id"])),
                    "status_result": "exception",
                    "exception": {
                        "type": type(error).__name__,
                        "message": str(error),
                    },
                    "before": plans.snapshot_payload(guard_context["before"]),
                    "after": (
                        plans.snapshot_payload(after) if after is not None else None
                    ),
                }
            guard_mode = guard_context.get("mode")
            # ``review`` matches neither branch on purpose: a legacy review turn
            # declares no write scope and captured no write-guard snapshot, so
            # there is no write guard to enforce for it. Its comparison is the
            # review fingerprint, which the protected-path step below reads.
            try:
                if guard_mode == "legacy":
                    write_guards._enforce_legacy_implementation_write_guard(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        cwd=guard_context["cwd"],
                        before=guard_context["before"],
                        after=after,
                        snapshot_before=guard_context["snapshot_before"],
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                        session_id=str(guard_context["session_id"]),
                        session_label=str(guard_context["session_label"]),
                        session_details=guard_context["session_details"],
                        turn_payload=turn_payload,
                        paths=guard_context["paths"],
                        plan_cache=ctx.plan_cache,
                        append_handoff_note=handoff_correction._append_handoff_note,
                        reinspect=session_verification._reinspect_after_revert,
                        state_dir_arg=args.state_dir,
                        run_dir=ctx.run_dir,
                        external_digests_before=guard_context.get(
                            "external_digests_before"
                        ),
                        turn_started_ns=guard_context.get("turn_started_ns"),
                    )
                elif guard_mode == "workspace":
                    write_guards._enforce_workspace_write_guard(
                        event_path=ctx.event_path,
                        payload=ctx.payload,
                        persistence=ctx.persistence,
                        workspace=guard_context["workspace"],
                        scope=guard_context["scope"],
                        before=guard_context["before"],
                        after=after,
                        snapshot_before=guard_context["snapshot_before"],
                        exclude=review_guard_excludes,
                        include_ignored=review_guard_include_ignored,
                        session_id=str(guard_context["session_id"]),
                        session_label=str(guard_context["session_label"]),
                        session_details=guard_context["session_details"],
                        turn_payload=turn_payload,
                        paths=guard_context["paths"],
                        plan_cache=ctx.plan_cache,
                        append_handoff_note=handoff_correction._append_handoff_note,
                        reinspect=session_verification._reinspect_after_revert,
                        state_dir_arg=args.state_dir,
                        run_dir=ctx.run_dir,
                        external_digests_before=guard_context.get(
                            "external_digests_before"
                        ),
                        turn_started_ns=guard_context.get("turn_started_ns"),
                    )
            except Exception as guard_error:
                error.add_note(
                    "post-turn workspace write guard failed while handling "
                    f"the exception: {guard_error}"
                )
            # The run's guards are expired a few lines below, so this is the
            # last moment an operator's standing instruction can be answered
            # at all. It is answered in adjudicate-only mode because the
            # exception, not the guard, is what ended this turn: the verdict
            # is recorded and the failure it belongs to is re-raised intact.
            try:
                write_guards._enforce_protected_path_guard(
                    event_path=ctx.event_path,
                    payload=ctx.payload,
                    persistence=ctx.persistence,
                    guard_controller=ctx.guard_controller,
                    before=guard_context["before"],
                    after=after,
                    session_id=str(guard_context["session_id"]),
                    session_label=str(guard_context["session_label"]),
                    session_details=guard_context["session_details"],
                    turn_payload=turn_payload,
                    paths=guard_context["paths"],
                    plan_cache=ctx.plan_cache,
                    append_handoff_note=handoff_correction._append_handoff_note,
                    reinspect=session_verification._reinspect_after_revert,
                    # A write guard that never recorded its comparison — it
                    # raised, or the inventory failed — leaves this guard
                    # blind, and blind is recorded as unavailable rather than
                    # read as clean. A legacy review turn has no write-guard
                    # comparison by construction, so it is answered from its
                    # own turn-start fingerprint instead.
                    changed_paths=(
                        write_guards._protected_path_review_changed_paths(
                            guard_context,
                            exclude=review_guard_excludes,
                            include_ignored=review_guard_include_ignored,
                        )
                        if guard_mode == "review"
                        else write_guards._protected_path_changed_paths(
                            turn_payload.get("workspace_write_guard")
                        )
                    ),
                    adjudicate_only=True,
                )
            except Exception as guard_error:
                error.add_note(
                    "post-turn protected-path guard failed while handling "
                    f"the exception: {guard_error}"
                )
        if isinstance(error, KeyboardInterrupt):
            if not ctx.payload.get("paused"):
                ctx.pause(
                    reason="interrupted",
                    message="run interrupted by user",
                    details=dict(ctx.last_session_details),
                )
        else:
            _expire_run_guards(ctx)
            ctx.run_controls.stop()
            ctx.inline_tail.stop()
            raise

    # The one call site, at the point every one of the loop's breaks converges
    # on. Placing it here rather than at each pause is what keeps the ordering
    # the settlement depends on -- the revert, the plan re-read, `turn_rejected`
    # and `run_paused` have all happened by now -- and it is still ahead of
    # `_terminalize`, which snapshots the unresolved commit groups a soft-failed
    # settlement would have to appear in.
    _settle_paused_retained_work(ctx, commit_mode=commit_mode)

    if review_requested:
        # An ordinary repair child can change pending/completed membership
        # without traversing a review-cycle branch. Re-inspect here so every
        # loop exit (including max-sessions, stop-after-current, and pauses)
        # publishes the plan's final continuation state.
        try:
            final_review_snapshot = ctx.plan_cache.get(snapshot.plan_dir)
        except plans.PlanError:
            # Preserve the run's existing malformed-plan pause and its last
            # safely computed projection when the final plan is unreadable.
            pass
        else:
            review_resume = review_cycle._set_review_resume(
                snapshot=final_review_snapshot,
                event_path=ctx.event_path,
                payload=ctx.payload,
                persistence=ctx.persistence,
                max_repair_group_attempts=run_inputs._max_repair_group_attempts(args),
                max_review_fix_cycles=max_review_fix_cycles,
                review_requested=True,
            )

    with ctx.persistence.lock:
        if ctx.payload["ok"] and ctx.payload["reason"] == "":
            ctx.payload["reason"] = "stopped"
        review_executed = ctx.payload.get("review_session_executed") is not None
        if (
            review_requested
            and review_executed
            and not ctx.payload.get("review_completed")
        ):
            review_cycle._attest_executed_review(
                ctx.payload,
                snapshot.plan_dir,
                require_artifact=executed_review_requires_artifact,
                read_review_verdict=handoff_guards._read_review_verdict,
            )
        if (
            review_requested
            and not review_executed
            and not ctx.payload.get("review_completed")
            and ctx.payload.get("review_skipped_reason") is None
        ):
            if ctx.payload.get("paused"):
                ctx.payload["review_skipped_reason"] = (
                    ctx.payload.get("pause_reason") or "paused"
                )
            elif ctx.payload["reason"] not in (
                "",
                "all-complete",
                review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            ):
                ctx.payload["review_skipped_reason"] = ctx.payload["reason"]

    with ctx.persistence.lock:
        exit_code = 0 if ctx.payload["ok"] else 1
        if ctx.payload.get("pause_reason") == "interrupted":
            exit_code = turn_execution.INTERRUPTED_EXIT
    # The controls outlive the loop so a supervisor can still reach a run that
    # is finalizing: the terminal audit is a harness turn of its own, and an
    # abort arriving during it has to find a live listener and a live drain.
    finalization_abort = harnesses.AbortSignal()
    mailbox_poll.set_context(None)
    mailbox_poll.set_abort(finalization_abort, finalization=True)
    try:
        return _terminalize(
            ctx, exit_code, cancel=finalization_abort, poll=mailbox_poll
        )
    finally:
        mailbox_poll.set_abort(None)
        _expire_run_guards(ctx)
        ctx.run_controls.stop()
        ctx.inline_tail.stop()
