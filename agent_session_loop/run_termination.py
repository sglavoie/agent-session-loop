"""How a run ends: the terminal verdict, the pause, and the honored stop.

Every run stops exactly once, and there are only three ways it can: it reaches
an ending it earned, it is paused because something blocked it, or a supervisor
asked it to stop. This module owns all three so the loop is left deciding *when*
a run ends rather than restating *what* each ending means.

The terminal path is a fixed order and the order is the contract.
:func:`_terminalize_run` adjudicates first, audits second, finalizes third.
Adjudication is the only step here that can change a verdict: a run may not
report success it did not earn, so :func:`_adjudicate_terminal_run` re-derives
the claim from the durable evidence and pauses the run when the evidence is
missing. The audit stays advisory and downstream of it, and it grades the same
verdict an operator reads. Finalization closes the readiness lifecycle and the
commit ledger last, so ``run_finished`` names artifacts that already exist.

Durable records this boundary writes:

* ``payload["terminal_adjudication"]`` -- the typed verdict of every ending,
  carrying what the run claimed about itself so an operator can see the claim
  was tested rather than assumed. Written under the run's mutex, before the
  audit and before any pause it causes.
* ``payload["paused"]`` and its whole block -- ``pause_reason``,
  ``pause_message``, ``pause_details``, ``pause_policy`` and the
  ``action_required`` ask. :func:`set_pause` is the single writer;
  :func:`_pause_run` is the same write plus the ``run-paused`` event and the
  AgentMux notification.
* ``payload["readiness_state_final"]`` -- the plan's readiness state as the run
  leaves it, or the error that stopped it being written.
* ``payload["reason"] = "stopped"`` and ``payload["final_snapshot"]`` -- the
  clean early exit a supervisor's stop request earns.

Nothing here imports the runner. Four collaborators the loop still owns enter
through seams rather than through an import, each declared below and satisfied
by the runner today and by a later boundary once it is extracted:
:class:`ReadinessStateWriter`, :class:`SessionStatusReverter`,
:data:`SnapshotReinspector` and :data:`ExistingReviewVerdictRecorder`.

Invariants that survive the move:

* **One definition of success.** Every terminal path -- normal completion,
  review exhaustion, a resumed run, stop-after-current, and the failure paths
  -- closes through :func:`_adjudicate_terminal_run`. A run that already paused
  keeps the specific blocker an operator needs instead of a generic verdict, and
  a dry run is not adjudicated because it claimed nothing.
* **Lock ownership.** Adjudication, the pause write, and the honored stop each
  take the run's reentrant persistence mutex around their whole
  read-decide-write sequence, so a concurrent control action cannot interleave
  between a decision and the record of it.
* **Advisory audit.** An audit that fails, finds something, or is cancelled
  never changes the adjudicated verdict or the exit code.
* **One pause writer.** A pause is never assembled field by field at a call
  site: :func:`set_pause` classifies the reason through
  :mod:`agent_session_loop.pause_policy` and attaches the remediation the pause
  evidence carries, so ``asl status`` and the supervisor helper read one
  vocabulary whichever refusal produced the pause.
* **Consumption at the stop.** A stop-after-current request is consumed only in
  :func:`_honor_stop_after_current`, at the moment the loop actually stops, so a
  request is never recorded as consumed by a run that then keeps working.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import (
    commit_settlement,
    events,
    harnesses,
    live_verification,
    pause_policy,
    plans,
    preflight,
    run_control,
    run_coordination,
    run_persistence,
    run_reporting,
    verification,
    verification_transitions,
)


class ReadinessStateWriter(Protocol):
    """Writes a plan's readiness metadata, reporting failure rather than raising.

    Satisfied by ``readiness_gate.write_readiness_state``. It stays a seam
    rather than an import because the readiness gate left the runner *after*
    this module did, and because the dependency has to run this way round: the
    readiness gate pauses through :func:`_pause_run`, so an import back the
    other way would be a cycle.
    """

    def __call__(
        self,
        plan_dir: Path,
        new_state: plans.ReadinessState,
        *,
        expected_sha256: str | None = None,
    ) -> tuple[bool, str | None]: ...


class SessionStatusReverter(Protocol):
    """Puts an aborted session back to ``pending`` with a note saying why.

    Returns the session file it rewrote, or ``None`` when there was nothing to
    revert -- which is what tells the caller whether the plan snapshot it holds
    is now stale.
    """

    def __call__(
        self,
        snapshot: plans.PlanSnapshot,
        session_id: str,
        *,
        failure_note: str,
    ) -> Path | None: ...


#: Re-reads the plan after a status revert, returning the fallback snapshot
#: unchanged when nothing was reverted. The most-shared primitive in the guard
#: layer; declared here because this is the first boundary to leave the runner
#: that needs it.
SnapshotReinspector = Callable[
    [Path, plans.PlanSnapshot, Path | None, plans.PlanSnapshotCache],
    plans.PlanSnapshot,
]

#: Projects a review verdict already persisted in the plan directory onto the
#: run payload. Used by the stop that lands when only review work would follow,
#: so the run reports the verdict it has rather than none at all.
ExistingReviewVerdictRecorder = Callable[[dict[str, Any], Path], None]


def readiness_finalizer(
    write_readiness_state: ReadinessStateWriter,
) -> commit_settlement.RunFinalizer:
    """Bind the readiness writer into the finalizer ``_finish_run`` expects.

    The commit ledger takes the readiness close as a callback so it never has to
    import this boundary; this is the one place that binding is made, so the
    writer seam is threaded in a single spot rather than at every terminal path.
    """

    def finalize(payload: dict[str, Any]) -> None:
        _finalize_readiness_lifecycle(
            payload, write_readiness_state=write_readiness_state
        )

    return finalize


#: The one ending that closes without an audit: a dry run rehearsed a loop and
#: produced no run to audit. Every other persisted ending is a supervisor
#: decision — a pause and an interruption most of all — and grading those is
#: what the audit exists for.
_UNAUDITED_TERMINAL_OUTCOMES = frozenset({"dry-run"})


def _audited_repositories(
    context: verification.VerificationContext,
) -> tuple[tuple[str, ...], bool]:
    """Name the repositories the audit's boundary check covers, and their source.

    A legacy plan registers no repositories, so the run root is its one
    repository. Naming nothing there would leave the boundary check comparing
    two empty snapshots, which can never notice a read-only auditor writing.
    That run root is routinely not a worktree root, so the audit is told the
    paths are synthesized: the tolerance that keeps such a run auditable must
    never reach a repository a plan really registered.
    """
    if context.workspace is None:
        return (".",), False
    return (
        tuple(
            repository.repository.path for repository in context.workspace.repositories
        ),
        True,
    )


def _terminal_outcome(payload: Mapping[str, Any]) -> str:
    """Name the ending a terminating run reached.

    ``all-complete`` covers two different endings an operator reads
    differently: a run that did the plan's remaining work, and one that found
    nothing left to do. The turn list is what separates them.
    """
    if payload.get("paused"):
        return "paused"
    reason = str(payload.get("reason") or "")
    if reason == "dry-run":
        return "dry-run"
    if reason == "all-complete":
        return "complete" if payload.get("turns") else "no-work"
    return reason or "stopped"


def _attach_terminal_assurance(
    payload: dict[str, Any],
    context: run_coordination.TerminalAuditContext,
    persistence: run_persistence._RunPersistence,
    abort: harnesses.AbortSignal | None = None,
) -> bool:
    """Audit the run and publish the verdict, over the runner's state projection.

    A thin binding rather than an alias: the coordination boundary is told how
    to compact this run's payload instead of importing the projection, and the
    runner is the only place that knows.
    """
    return run_coordination.attach_terminal_assurance(
        payload,
        context,
        persistence,
        abort=abort,
        state_snapshot=run_persistence._compact_state_payload,
    )


@dataclass(frozen=True)
class _TerminalAdjudication:
    """The typed verdict every terminal path is closed through.

    ``blocker`` is ``None`` when the run kept the outcome it arrived with —
    including every run that was already paused, whose existing blocker is
    preserved rather than restated.
    """

    #: What the run said about itself before adjudication, so an operator can
    #: see that a success claim was tested rather than assumed.
    claimed_ok: bool
    claimed_reason: str
    blocker: verification_transitions.TerminalBlocker | None = None

    @property
    def blocked(self) -> bool:
        return self.blocker is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "claimed_ok": self.claimed_ok,
            "claimed_reason": self.claimed_reason,
            "blocked": self.blocked,
            "reason": None if self.blocker is None else self.blocker.reason,
            "details": {} if self.blocker is None else self.blocker.details,
        }


def _adjudicate_terminal_run(
    *,
    event_path: Path,
    payload: dict[str, Any],
    persistence: run_persistence._RunPersistence,
) -> _TerminalAdjudication:
    """Decide whether this ending may report the success it arrived with.

    One adjudicator for every terminal path — normal completion, review
    exhaustion, a resumed run, stop-after-current, and the failure paths — so
    success has a single definition instead of one per exit. A run that already
    paused is left exactly as it is: its blocker is the specific one an operator
    needs, and restating it as a generic adjudication verdict would lose that. A
    dry run rehearsed a loop and claimed nothing, so it is not adjudicated
    either.

    A run that is failing but *not* paused is still adjudicated, because the two
    endings are not the same claim. The only such ending ASL has is
    ``review-findings-unresolved``, and a run that reaches it while still owing
    an unredeemed broad-check deferral has a repair loop that never converged —
    which a resume can converge. Naming that as a pause makes the blocker
    durable and re-evaluated instead of leaving the deferral stranded behind a
    plain failure; the specific reason survives on ``claimed_reason`` and on the
    ``action_required`` record the review path already wrote.
    """
    with persistence.lock:
        claimed_ok = bool(payload.get("ok"))
        claimed_reason = str(payload.get("reason") or "")
        already_settled = bool(payload.get("paused")) or claimed_reason == "dry-run"
        blocker = (
            None
            if already_settled
            else verification_transitions.terminal_blocker(payload, event_path)
        )
        adjudication = _TerminalAdjudication(
            claimed_ok=claimed_ok,
            claimed_reason=claimed_reason,
            blocker=blocker,
        )
        payload["terminal_adjudication"] = adjudication.to_payload()
        persistence.write_state(payload)
    if blocker is not None:
        _pause_run(
            event_path,
            payload,
            reason=blocker.reason,
            message=blocker.message,
            details=blocker.details,
            persistence=persistence,
        )
    return adjudication


def _terminalize_run(
    *,
    exit_code: int,
    event_path: Path,
    payload: dict[str, Any],
    write_readiness_state: ReadinessStateWriter,
    progress: run_reporting._ProgressReporter | None = None,
    persistence: run_persistence._RunPersistence | None = None,
    audit_context: run_coordination.TerminalAuditContext | None = None,
    cancel: harnesses.AbortSignal | None = None,
    poll: Callable[[], None] | None = None,
) -> int:
    """Close one run: adjudicate it, audit it, then finalize it.

    Adjudication runs first and is the only thing here that can change the
    run's verdict: a run may not claim success it did not earn, so
    :func:`_adjudicate_terminal_run` re-derives success from the durable
    evidence and pauses the run when the evidence is missing. Auditing stays
    advisory and downstream of it — an audit that fails or finds something
    still leaves the adjudicated verdict alone, and it now grades the same
    verdict an operator reads. The audit runs before finalization so its
    artifacts and ledger records exist before ``run_finished`` carries the
    summary that names them; readiness, the terminal event, the state
    projection, AgentMux, and the final output all follow unchanged.

    ``cancel`` and ``poll`` come from the run loop's exit, the one path that
    still has a live control queue to drain: together they make the audit
    cancellable by repeating ``asl control abort``. The early exits that
    terminalize before the loop starts pass neither, so their finalization is
    visible but not cancellable.

    ``write_readiness_state`` is required rather than defaulted: the readiness
    gate is ``readiness_gate.write_readiness_state``, which this module cannot
    import because the readiness gate pauses through :func:`_pause_run`, and a
    terminal path that silently skipped the readiness close would leave the plan
    advertising a state the run no longer holds.
    """
    resolved_persistence = run_persistence._run_persistence(persistence)
    adjudication = _adjudicate_terminal_run(
        event_path=event_path,
        payload=payload,
        persistence=resolved_persistence,
    )
    if adjudication.blocked:
        exit_code = 1
    with resolved_persistence.lock:
        outcome = _terminal_outcome(payload)
    if audit_context is not None and outcome not in _UNAUDITED_TERMINAL_OUTCOMES:
        # The coordinator owns the one invariant this block existed for: the
        # published finalization phase is closed exactly once, cancelled or
        # not, however the advisory audit inside it ends.
        run_coordination.TerminalAssuranceCoordinator(
            payload=payload,
            context=audit_context,
            persistence=resolved_persistence,
            phase=live_verification._LiveFinalization(
                event_path=event_path,
                payload=payload,
                persistence=resolved_persistence,
                run_id=payload.get("run_id"),
                phase=live_verification.FINALIZATION_PHASE_ASSURANCE_AUDIT,
                cancellable=cancel is not None,
                poll=poll,
            ),
            state_snapshot=run_persistence._compact_state_payload,
            abort=cancel,
        ).run()
    run_coordination.record_degraded_assurance(
        payload, persistence=resolved_persistence
    )
    commit_settlement._finish_run(
        event_path,
        payload,
        progress,
        persistence=persistence,
        finalize_readiness=readiness_finalizer(write_readiness_state),
    )
    return exit_code


def _final_readiness_state_from_payload(
    payload: dict[str, Any],
) -> plans.ReadinessState:
    if payload.get("paused") or not payload.get("ok"):
        return "reviewed"
    final_snapshot = payload.get("final_snapshot")
    counts: dict[str, Any] | None = None
    if isinstance(final_snapshot, dict):
        snapshot_counts = final_snapshot.get("counts")
        if isinstance(snapshot_counts, dict):
            counts = snapshot_counts
    if counts is not None:
        pending = int(counts.get("pending", 0) or 0)
        empty = int(counts.get("empty", 0) or 0)
        unknown = int(counts.get("unknown", 0) or 0)
        if pending == 0 and empty == 0 and unknown == 0:
            return "complete"
    return "loop-ready"


def _finalize_readiness_lifecycle(
    payload: dict[str, Any], *, write_readiness_state: ReadinessStateWriter
) -> None:
    if not payload.get("readiness_state_active"):
        return
    plan_dir_value = payload.get("plan_dir")
    if not isinstance(plan_dir_value, str) or not plan_dir_value:
        return
    final_state = _final_readiness_state_from_payload(payload)
    success, error = write_readiness_state(Path(plan_dir_value), final_state)
    if success:
        payload["readiness_state_final"] = final_state
        payload["readiness_state_current"] = final_state
    else:
        payload["readiness_state_final"] = None
        payload["readiness_state_write_error"] = error


#: Where a pause's remediation hints sit inside its details payload. A
#: readiness pause carries the gate payload as its details; the workspace-scope
#: pause nests that same payload under its own key.
_PAUSE_REMEDIATION_PATHS: tuple[tuple[str, ...], ...] = (
    ("repair_hints",),
    ("readiness_gate", "repair_hints"),
)


def _optional_str(value: Any) -> str | None:
    """``value`` when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _runtime_reason_remediation(
    reason: str, *, plan_dir: str | None, cwd: str | None
) -> dict[str, Any] | None:
    """The repair hint a stable pause reason resolves to, if it has one.

    Shaped like ``readiness_gate._readiness_repair_hints`` entries so a consumer
    reads one remediation vocabulary whichever source produced it. The plan
    directory and repository root are substituted when the payload knows them,
    leaving the placeholders intact rather than quoting them when it does not.
    """

    hint = preflight.runtime_repair_hint(reason)
    if hint is None:
        return None
    message = hint.message
    if plan_dir:
        message = preflight.repair_hint_payload(hint, plan_dir=plan_dir, cwd=cwd)[
            "message"
        ]
    return {
        "check_id": hint.check_id,
        "category": hint.category,
        "fix_category": hint.fix_category,
        "message": message,
    }


def _pause_remediation(
    details: Mapping[str, Any],
    *,
    reason: str,
    plan_dir: str | None = None,
    cwd: str | None = None,
) -> list[dict[str, Any]]:
    """Remediation for a pause: its own details first, else its reason.

    A gate that already resolved its findings to repair hints carries them in
    the pause details, and those are the most specific guidance available. A
    runner-side refusal carries findings but no hints, so the stable pause
    reason resolves one from ``preflight.RUNTIME_REPAIR_HINTS`` instead. Without
    that fallback an ``inspect`` pause tells an operator to look without saying
    where, which is what the audit filed against `commit-preflight`.
    """

    for path in _PAUSE_REMEDIATION_PATHS:
        current: Any = details
        for key in path:
            current = current.get(key) if isinstance(current, Mapping) else None
        if isinstance(current, list) and current:
            return [entry for entry in current if isinstance(entry, Mapping)]
    fallback = _runtime_reason_remediation(reason, plan_dir=plan_dir, cwd=cwd)
    return [] if fallback is None else [fallback]


def set_pause(
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write the whole pause block onto ``payload``. The single writer.

    This boundary's primary public entry, and public precisely because every
    refusal in the run reaches it: a pause assembled field by field at a call
    site would classify its own reason and pick its own remediation, which is
    how ``asl status``, the supervisor helper and ``asl questions`` would end up
    reading three different pause vocabularies. Callers that also need the
    durable ``run-paused`` event use :func:`_pause_run`, which is this write plus
    the event and the AgentMux notification.
    """
    payload["ok"] = False
    payload["paused"] = True
    payload["reason"] = reason
    payload["pause_reason"] = reason
    payload["pause_message"] = message
    payload["pause_details"] = {} if details is None else details
    payload["details"] = payload["pause_details"]
    # Carry the single-source-of-truth pause classification so consumers
    # (the supervisor helper, `asl status`, `asl questions`) stop hardcoding
    # the recoverable/user-required reason sets.
    policy = pause_policy.classify_pause(reason)
    payload["pause_policy"] = policy
    # A pause a machine cannot retry is by definition waiting on a human, so the
    # terminal state names that ask rather than leaving an operator to infer it
    # from the reason. A signal the turn already raised is the more specific
    # record of the same ask and is left standing. Remediation is whatever the
    # pause evidence carries: a reason with no command still gets a record.
    if policy["requires_user"] and not isinstance(
        payload.get("action_required"), Mapping
    ):
        payload["action_required"] = {
            "reason": reason,
            "message": message,
            "source": "pause",
            "user_action": policy["user_action"],
            "remediation": _pause_remediation(
                payload["pause_details"],
                reason=reason,
                plan_dir=_optional_str(payload.get("plan_dir")),
                cwd=_optional_str(payload.get("cwd")),
            ),
        }


def _pause_run(
    event_path: Path,
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
    details: dict[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        set_pause(payload, reason=reason, message=message, details=details)
        persistence.append_event(
            event_path,
            events.RUN_PAUSED_EVENT,
            {
                "run_id": payload.get("run_id"),
                "run_dir": payload.get("run_dir"),
                "reason": reason,
                "message": message,
                "details": (
                    {}
                    if details is None
                    else run_persistence._bounded_verification_details(details)
                ),
            },
        )
    run_persistence._notify_agent_mux_loop_paused(payload, persistence)


def _detach_turn_controls(poll: run_coordination.MailboxPoll) -> None:
    """Take the finished turn's mid-turn inputs away from the poller.

    Called from ``after_stop``, which runs once the poll thread is joined and
    before ``active_turn`` is cleared: after this, an observed abort arms
    nothing and observed guidance is delivered nowhere until the next turn
    registers its own signal and endpoint.
    """
    poll.set_abort(None)
    poll.set_live_delivery(None)


def _record_abort_satisfied_by_worker(
    listener: run_control.RunControlListener,
    *,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Clear a pending abort whose turn ended on its own inside the window."""
    action = listener.consume_abort_turn()
    if action is None:
        return
    run_coordination.record_run_control_action(
        event_path=event_path,
        payload=payload,
        control_payload=control_payload,
        action=action,
        context=context,
        persistence=run_persistence._run_persistence(persistence),
        event_action=events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
        message="turn abort satisfied by the worker inside the grace window",
    )


def _honor_abort_turn(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans.PlanSnapshot,
    session_id: str,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    plan_cache: plans.PlanSnapshotCache,
    revert_session_status: SessionStatusReverter,
    reinspect: SnapshotReinspector,
    context: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Record the clean early stop that follows a killed turn.

    A supervisor-requested abort is not a fault, so this is deliberately not a
    pause: the killed session goes back to ``pending`` with a Handoff note
    explaining why it holds no completion, and the run exits ``stopped``.

    The revert and the re-inspection are seams because both targets leave the
    runner after this module does. Neither is optional: a stop recorded against
    a snapshot taken before the revert would report the aborted session as still
    holding the completion the abort just took away.
    """
    persistence = run_persistence._run_persistence(persistence)
    action = listener.consume_abort_turn()
    reason = run_coordination.ABORT_TURN_DEFAULT_REASON
    if action is not None:
        reason = action.abort_reason or run_coordination.ABORT_TURN_DEFAULT_REASON
        run_coordination.record_run_control_action(
            event_path=event_path,
            payload=payload,
            control_payload=control_payload,
            action=action,
            context=context,
            persistence=persistence,
        )
    reverted = revert_session_status(
        snapshot,
        session_id,
        failure_note=(
            "turn aborted by supervisor request before it completed "
            f"({reason}); the session holds no completion from it"
        ),
    )
    snapshot_after = reinspect(snapshot.plan_dir, snapshot, reverted, plan_cache)
    _honor_stop_after_current(
        listener,
        snapshot=snapshot_after,
        payload=payload,
        event_path=event_path,
        control_payload=control_payload,
        context=context,
        persistence=persistence,
    )


def _honor_stop_after_current(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans.PlanSnapshot,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    context: Mapping[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Consume the pending stop request and record the run's clean early exit.

    Consumption happens only here, at the moment the loop actually stops, so a
    request is never recorded as ``consumed`` by a run that then keeps working.
    """
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        action = listener.consume_stop_after_current()
        if action is not None:
            run_coordination.record_run_control_action(
                event_path=event_path,
                payload=payload,
                control_payload=control_payload,
                action=action,
                context=context,
                persistence=persistence,
            )
        payload["reason"] = "stopped"
        payload["final_snapshot"] = plans.snapshot_payload(snapshot)
        persistence.write_state(payload)


def _stop_before_review_work(
    listener: run_control.RunControlListener,
    *,
    snapshot: plans.PlanSnapshot,
    payload: dict[str, Any],
    event_path: Path,
    control_payload: dict[str, Any],
    record_existing_review_verdict: ExistingReviewVerdictRecorder,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    """Honor a stop that lands when only review work would follow.

    The generated review session and its repair children are work that starts
    after the session the operator asked to stop after, so an all-complete plan
    is not a no-op: the run exits ``stopped`` with the review still pending, and
    re-running the same command later performs it.

    The verdict projection is a seam because the review cycle leaves the runner
    after this module does; it runs inside the run's mutex, ahead of the stop it
    is reporting, so the recorded exit and the verdict it carries agree.
    """
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        record_existing_review_verdict(payload, snapshot.plan_dir)
        _honor_stop_after_current(
            listener,
            snapshot=snapshot,
            payload=payload,
            event_path=event_path,
            control_payload=control_payload,
            persistence=persistence,
        )
