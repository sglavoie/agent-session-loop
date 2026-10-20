"""The verdict a finished turn has to survive before anything acts on it.

Preflight settles what the turn will be judged against; this boundary is the
judging. It owns the window between the harness process ending and the run
loop's first result branch -- the last point at which a turn's work can still be
refused, reverted, or amended before a pause, a stop, or another turn adopts it
as the new baseline.

Five guards in one fixed order, which is the spine of what this module owns:

* **The workspace write guard** (or the **legacy implementation write guard**,
  for a run with no registered workspace) asks whether the session stayed inside
  its declared write scope. Exactly one of the two runs, chosen by the same
  turn-start capture that decided the crash path's guard mode.
* **The review mutation guard** asks whether a legacy review turn left the
  worktree exactly as it found it.
* **The protected-path guard** asks the different question: whether the run
  honoured a standing instruction an operator gave it mid-flight. A path can be
  perfectly in scope and still be one the operator said not to touch.
* **The plan artifact guard** asks whether the turn changed a session artifact
  it did not select. On the turn's ordinary path it is gated on the turn having
  reached a terminating result at all; on the path where an earlier guard
  already ended the turn it runs in adjudicate-only mode, so the packets it
  convicts are still named beside the pause that ended the turn.

Between the first guard and the second question sits the one step here that is
not a guard: a session the loop re-attempts inside one run spans several turns,
and ``continuation_guards`` accumulates the earlier attempts' changes into the
write guard's record before anything downstream reads it. It is placed here
rather than in any consumer because there are three of them -- the
protected-path guard below, the per-session commit and the paused-work
settlement -- and they all read the same record.

Durable records this boundary writes: two of its own, both the turn's result
artifact rewritten -- once with the per-repository review bases a passing
workspace review turn projects, and once with that cumulative write-guard
record. Every other write here belongs to the guard that makes it
-- each of the five appends its own event and amends the run payload through the
run's persistence seam -- and every refusal is recorded by ``run_termination``
through ``ctx.pause``.

Nothing here imports the runner, and since Session 37 it does not import
``run_loop``, which owns ``RunContext``, either: the context enters as the
structural :class:`RunContext` Protocol below, naming only the members this
sequence reaches.

The step hands back a frozen :class:`PostTurnGuards` rather than performing the
loop's ``break``: the three refusal paths this replaced each ended in one, and a
boundary cannot break a loop it does not own. ``stopped`` is that ``break``.

Five invariants survive the move:

* **The write guard runs ahead of every result branch.** A retryable pause --
  a timeout, a transport failure -- relaunches the same session, so a guard that
  ran after the branch would let the relaunch adopt the turn's own mutation as
  its new baseline.
* **A guard that pauses still reaches the guards after it.** Each asks a
  different question -- did the session stay in scope, did the run honour the
  operator's standing instruction, did the turn rewrite a packet it was never
  given -- and a turn can breach several of them at once, so a turn that
  breached several has to report all of them. That is why the pause is recorded
  in ``paused`` and the ``break`` moves to *after* the protected-path and plan
  artifact steps rather than being taken where the pause is decided. Those two
  run in adjudicate-only mode there: the verdict is recorded beside the pause
  that already happened, and neither reverts a second time nor displaces the
  reason an operator has to act on.
* **A turn records its write verdict once.** Each of the three write-mode
  branches disarms the crash-path copy of the guard before enforcing the live
  one, so the exception handler above cannot re-enforce the same guard against
  the same before-image. That disarming is a callback rather than a returned
  value precisely because it has to have happened before the guard runs, and so
  must survive the guard raising.
* **"No baseline" and "proven clean" are distinct answers.** The protected-path
  guard reads its changed paths from the write guard's own evidence; a legacy
  review turn has no such evidence, and its passing mutation guard is handed to
  the guard as an empty tuple -- proof the whole worktree is unchanged -- while
  a mutation guard that paused names no paths and clears nothing.
* **Every terminating branch is named before any of them runs.** The
  disposition is computed once, here, ahead of the plan artifact guard, and the
  loop then selects its branch by that name rather than by re-testing the
  result -- so a branch the disposition cannot name is unreachable and none can
  be added without this guard having run for it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from . import (
    continuation_guards,
    durability,
    events,
    guards,
    handoff_correction,
    handoff_guards,
    harnesses,
    plans_models,
    plans_payloads,
    plans_session_scope,
    plans_snapshot,
    repository_evidence,
    run_persistence,
    session_verification,
    workspace_evidence,
    workspaces,
    write_guards,
    write_guards_legacy,
    write_guards_protected,
)
from .run_errors import RunError

# ---------------------------------------------------------------------------
# Collaborator seams
# ---------------------------------------------------------------------------


class RunContext(Protocol):
    """One locked run's own state, as the post-turn guard sequence needs it.

    Declared structurally rather than imported so this module never depends on
    ``run_loop``, which owns the dataclass. Only the members the five guards
    reach are named: the write seam every verdict is recorded through, the run
    payload and event log they record into, the plan cache each guard re-reads
    the plan from after a revert, the run directory the external-write
    comparison is scoped to, the controller that holds the operator's path
    rules, and the one way a turn ends the run.
    """

    @property
    def run_dir(self) -> Path: ...

    @property
    def event_path(self) -> Path: ...

    @property
    def payload(self) -> dict[str, Any]: ...

    @property
    def persistence(self) -> run_persistence._RunPersistence: ...

    @property
    def plan_cache(self) -> plans_snapshot.PlanSnapshotCache: ...

    @property
    def guard_controller(self) -> guards.GuardController: ...

    @property
    def session_attempts(self) -> continuation_guards.SessionAttempts: ...

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# What the sequence decides
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PostTurnGuards:
    """The verdict the guard sequence reached, as the loop has to read it.

    ``stopped`` is the ``break`` the refusal paths ended in: a protected-path
    conviction, a finalization guard that paused, or a plan artifact guard that
    refused the turn. ``terminating_disposition`` is the name of the result
    branch the loop takes next, computed once here because the plan artifact
    guard is gated on it; it is ``None`` both when the turn reaches the status
    guards instead and when the sequence stopped before naming one, and the
    loop reads it only on the path where it did not stop.
    """

    stopped: bool
    terminating_disposition: str | None


def _stopped() -> PostTurnGuards:
    """A guard ended the turn; the loop breaks and names no result branch."""
    return PostTurnGuards(stopped=True, terminating_disposition=None)


def enforce_post_turn_guards(
    ctx: RunContext,
    *,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    result: harnesses.HarnessResult,
    turn_payload: dict[str, Any],
    workspace: workspaces.ResolvedWorkspace | None,
    session_scope: plans_session_scope.SessionScope | None,
    workspace_snapshot_before: repository_evidence.WorkspaceSnapshot | None,
    legacy_snapshot_before: repository_evidence.WorkspaceSnapshot | None,
    review_fingerprint_before: dict[str, Any] | None,
    external_digests_before: dict[str, dict[str, Any]],
    turn_started_ns: int,
    plan_artifacts_before: Mapping[str, str],
    plan_artifacts_written_by_runner: tuple[str, ...],
    is_review_session: bool,
    cwd: Path,
    session_id: str,
    session_label: str,
    session_details: dict[str, Any],
    review_guard_excludes: tuple[str, ...],
    review_guard_include_ignored: bool,
    paths: dict[str, Path],
    state_dir_arg: str | None,
    disarm_exception_guard: Callable[[], None],
    external_change_aliases: frozenset[str] = frozenset(),
) -> PostTurnGuards:
    """Run the five post-execution guards, in order, over one finished turn.

    ``disarm_exception_guard`` retires the caller's crash-path copy of the
    write guard. It is a callback rather than a returned value because the
    ordering is the point: the live guard must not be able to run while the
    crash-path one is still armed, not even if it raises partway through.
    """
    # Ahead of every result branch: a retryable pause (timeout, transport
    # failure) would otherwise relaunch against the turn's own mutation and
    # adopt it as the new baseline.
    #
    # A finalization guard that pauses no longer leaves immediately: it records
    # that it is ending the turn and falls through to the protected-path step
    # below, because the operator's standing instruction is answered against the
    # same comparison the write guard already captured, and a turn that breached
    # both has to report both. The break moves to after that step.
    finalization_paused = False
    if workspace is not None:
        assert session_scope is not None
        assert workspace_snapshot_before is not None
        disarm_exception_guard()
        if write_guards._enforce_workspace_write_guard(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            workspace=workspace,
            scope=session_scope,
            before=before,
            after=after,
            snapshot_before=workspace_snapshot_before,
            exclude=review_guard_excludes,
            include_ignored=review_guard_include_ignored,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
            state_dir_arg=state_dir_arg,
            run_dir=ctx.run_dir,
            external_digests_before=external_digests_before,
            turn_started_ns=turn_started_ns,
            external_change_aliases=external_change_aliases,
        ):
            finalization_paused = True
        elif is_review_session:
            try:
                review_bases = workspace_evidence._workspace_review_bases(
                    workspace, workspace_snapshot_before
                )
            except RunError as error:
                with ctx.persistence.lock:
                    ctx.payload["final_snapshot"] = (
                        plans_payloads.snapshot_payload(after)
                        if after is not None
                        else plans_payloads.snapshot_payload(before)
                    )
                ctx.pause(
                    reason=events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
                    message=str(error),
                    details={**session_details, "error": str(error)},
                )
                finalization_paused = True
            else:
                with ctx.persistence.lock:
                    turn_payload["review_repository_bases"] = review_bases
                    durability.write_json_file(paths["result"], turn_payload)
    elif legacy_snapshot_before is not None:
        disarm_exception_guard()
        if write_guards_legacy._enforce_legacy_implementation_write_guard(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            cwd=cwd,
            before=before,
            after=after,
            snapshot_before=legacy_snapshot_before,
            exclude=review_guard_excludes,
            include_ignored=review_guard_include_ignored,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
            state_dir_arg=state_dir_arg,
            run_dir=ctx.run_dir,
            external_digests_before=external_digests_before,
            turn_started_ns=turn_started_ns,
        ):
            finalization_paused = True
    elif review_fingerprint_before is not None:
        # The turn reached its own finalization guards, which adjudicate the
        # protected paths in line below. Disarming the crash-path copy here
        # mirrors the two branches above and keeps one turn from recording the
        # same verdict twice.
        disarm_exception_guard()
    if (
        not finalization_paused
        and is_review_session
        and review_fingerprint_before is not None
        and handoff_guards._enforce_review_mutation_guard(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            cwd=cwd,
            before=before,
            after=after,
            fingerprint_before=review_fingerprint_before,
            exclude=review_guard_excludes,
            include_ignored=review_guard_include_ignored,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
        )
    ):
        finalization_paused = True

    # Between the write guard that produced the record and the first consumer
    # that reads it. A session the loop re-attempts inside one run continues the
    # earlier attempt's conversation, so its work spans several turns, while this
    # record describes one; accumulating the earlier attempts' changes into it
    # here is what makes the protected-path step below, the per-session commit
    # and the paused-work settlement all see the session's whole delta rather
    # than its last turn's. A finalization that paused keeps its own refusal
    # record untouched: that record is a verdict about one turn, not a statement
    # about what the session wrote, and blurring the two would put paths an
    # operator is not being asked about beside the reason they must act on.
    if not finalization_paused:
        cumulative = continuation_guards.merge_session_write_evidence(
            ctx.session_attempts.latest(session_id),
            turn_payload.get("workspace_write_guard"),
        )
        if cumulative is not None:
            with ctx.persistence.lock:
                turn_payload["workspace_write_guard"] = cumulative
                durability.write_json_file(paths["result"], turn_payload)

    # After the general write guard and before every result branch, for the same
    # reason the write guard sits there: a retryable pause would otherwise
    # relaunch against a workspace in which the protected path has already
    # moved, and adopt that as the baseline. It is a distinct step because it
    # answers a distinct question — not "did the session stay in scope" but
    # "did the run honour the standing instruction an operator gave it".
    protected_path_paused = write_guards_protected._enforce_protected_path_guard(
        event_path=ctx.event_path,
        payload=ctx.payload,
        persistence=ctx.persistence,
        guard_controller=ctx.guard_controller,
        before=before,
        after=after,
        session_id=session_id,
        session_label=session_label,
        session_details=session_details,
        turn_payload=turn_payload,
        paths=paths,
        plan_cache=ctx.plan_cache,
        append_handoff_note=handoff_correction._append_handoff_note,
        reinspect=session_verification._reinspect_after_revert,
        changed_paths=(
            write_guards_protected._protected_path_changed_paths(
                turn_payload.get("workspace_write_guard")
            )
            if isinstance(turn_payload.get("workspace_write_guard"), Mapping)
            # A legacy review turn captures no write-guard comparison, but a
            # mutation guard that passed proves the worktree is unchanged — a
            # stronger clearance than an empty change list, not a missing one.
            # One that paused proves the opposite, and names no paths, so it
            # clears nothing.
            else ()
            if review_fingerprint_before is not None and not finalization_paused
            else None
        ),
        adjudicate_only=finalization_paused,
    )
    if protected_path_paused or finalization_paused:
        # The turn is over, but the packet question is still unanswered, and it
        # is a different question from the one that ended the turn: a session
        # can breach its write scope and rewrite a packet it was never given in
        # the same turn. Leaving this unrun let the changed packets stay in the
        # plan with no verdict at all (ASI-119), so the guard adjudicates here
        # and records beside the pause that already happened rather than
        # displacing it.
        handoff_guards._enforce_plan_artifact_guard(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            before=before,
            after=after,
            digests_before=plan_artifacts_before,
            runner_written=plan_artifacts_written_by_runner,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
            adjudicate_only=True,
        )
        return _stopped()

    # Every branch below can end the turn — by pausing, by recording a clean
    # stop, or by looping straight into another turn — and a relaunch that
    # starts from a tampered packet adopts it as the new baseline. So the
    # digests are compared here, before any of them runs, and each of those
    # branches is then selected by the name computed here rather than by
    # re-testing the result: a branch the disposition cannot name is
    # unreachable, so none can be added below without this guard having run for
    # it. A turn that reaches the status guards instead is left to the call site
    # below them, which keeps the sharper wrong-session diagnosis for a tamper
    # that moved a status.
    terminating_disposition = handoff_guards._terminating_result_disposition(
        result, after, turn_payload
    )
    if (
        terminating_disposition is not None
        and handoff_guards._enforce_plan_artifact_guard(
            event_path=ctx.event_path,
            payload=ctx.payload,
            persistence=ctx.persistence,
            before=before,
            after=after,
            digests_before=plan_artifacts_before,
            runner_written=plan_artifacts_written_by_runner,
            session_id=session_id,
            session_label=session_label,
            session_details=session_details,
            turn_payload=turn_payload,
            paths=paths,
            plan_cache=ctx.plan_cache,
            result_disposition=terminating_disposition,
            append_handoff_note=handoff_correction._append_handoff_note,
            reinspect=session_verification._reinspect_after_revert,
        )
    ):
        return _stopped()
    return PostTurnGuards(
        stopped=False,
        terminating_disposition=terminating_disposition,
    )
