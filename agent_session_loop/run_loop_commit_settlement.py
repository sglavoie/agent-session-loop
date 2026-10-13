"""Settling the commit group a run owes, and the retained work of a pause."""

from __future__ import annotations

import functools
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    commit_groups,
    commit_settlement,
    durability,
    plans_models,
    plans_session_scope,
    review_guard,
    run_selection,
)
from .run_errors import RunError
from .run_loop_context import RunContext


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
            failed_commit = record.get("commit")
            stderr_path = (
                failed_commit.get("stderr_path")
                if isinstance(failed_commit, Mapping)
                else None
            )
            ctx.unsettled_commit_group = {
                "scope": record["scope"],
                "session_id": record["session_id"],
                "group_id": record["group_id"],
                "failed_alias": record["failed_alias"],
                "retries_spent": spent,
                "message": record["message"],
                "error": record["error"],
                # Where the last attempt's `asl commit` stderr landed, so the
                # remediation can point at the error instead of asking for a
                # re-run to reproduce it (ASI-160).
                "stderr_path": stderr_path if isinstance(stderr_path, str) else None,
            }
        return
    ctx.owed_commit_group_attempts[str(group_id)] = spent + 1
    ctx.owed_commit_group = dict(group) if group is not None else None


def _pause_for_unsettled_commit_group(
    ctx: RunContext, *, snapshot: plans_models.PlanSnapshot
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


def _commit_preflight_refusal(
    ctx: RunContext,
    *,
    cwd: Path,
    session_scope: plans_session_scope.SessionScope | None,
) -> RunError | None:
    """Ask the per-session commit gate whether it would refuse, without pausing.

    The gate is consulted twice on the path below -- once to learn whether the
    dirt it sees belongs to a commit the run still owes, and once after that
    commit has been attempted -- so the question is asked here and the answer is
    returned rather than acted on. The scope is required because a per-session
    commit is defined by the aliases the session declared; a missing scope is a
    refusal for the same reason unreadable dirt is, which is why it is raised
    into the same channel instead of being reported as a clean tree.
    """
    try:
        if session_scope is None:
            raise RunError("current session scope is unavailable")
        review_guard._validate_commit_preflight(
            cwd,
            verification_context=ctx.verification_context,
            needed_aliases=frozenset((*session_scope.reads, *session_scope.writes)),
            clean_aliases=frozenset(session_scope.writes),
        )
    except RunError as error:
        return error
    return None


def _spend_owed_commit_group_budget(ctx: RunContext) -> None:
    """Spend an owed group's remaining retries before a gate refuses its dirt.

    This is the mid-loop twin of
    :func:`_settle_owed_commit_group_at_startup`, and it exists for the same
    ordering reason: a settlement that fails re-arms its group for the *next*
    commit boundary, but the per-session commit preflight stands between here
    and that boundary and refuses exactly the dirt the failed settlement left
    behind. A group re-armed behind that gate never gets its retry, and the run
    stops as ``commit-preflight`` -- non-recoverable, and a description of the
    worktree rather than of what actually happened, which was a commit this run
    tried and failed to make with attempts still budgeted.

    Every scope repeats here, unlike at startup. The startup helper limits
    itself to ``session-paused`` because the alternative is asking the message
    harness for a turn before the run loop has begun; by this point the harness
    is the one the loop already uses at its own commit boundary, and the attempt
    being brought forward is one this iteration was going to make anyway.

    The bound is the same one, twice over: ``_soft_fail_commit`` stops re-arming
    at :data:`commit_settlement.MAX_COMMIT_GROUP_RETRIES`, and the iteration cap
    is the backstop for a recovery that answers under a different ``group_id``.
    A group that exhausts its budget leaves
    :attr:`RunContext.unsettled_commit_group` armed, which is the caller's cue
    to stop as ``commit-failed``.
    """
    for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
        _settle_owed_commit_group(ctx)
        if ctx.owed_commit_group is None:
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
