# ruff: noqa: F403, F405
"""Terminal adjudication and readiness lifecycle finalization."""

from __future__ import annotations

# ruff: noqa: F403, F405
from .run_termination_pause import _pause_run
from .run_termination_shared import *
from .run_termination_shared import _UNAUDITED_TERMINAL_OUTCOMES


def _audited_repositories(
    context: verification_models.VerificationContext,
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
    context: run_coordination_terminal.TerminalAuditContext,
    persistence: run_persistence._RunPersistence,
    abort: harnesses.AbortSignal | None = None,
) -> bool:
    """Audit the run and publish the verdict, over the runner's state projection.

    A thin binding rather than an alias: the coordination boundary is told how
    to compact this run's payload instead of importing the projection, and the
    runner is the only place that knows.
    """
    return run_coordination_terminal.attach_terminal_assurance(
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
    audit_context: run_coordination_terminal.TerminalAuditContext | None = None,
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
        run_coordination_terminal.TerminalAssuranceCoordinator(
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
    run_coordination_terminal.record_degraded_assurance(
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
) -> plans_models.ReadinessState:
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
