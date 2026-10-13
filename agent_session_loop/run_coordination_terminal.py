"""Terminal assurance coordination: the advisory audit and its finalization phase."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import (
    assurance_report,
    harnesses,
    run_coordination_seams,
    supervisor_audit,
)

#: Assurance states a terminating run still owes an audit for. ``skipped`` is a
#: settled decision to run no checks; anything else is already a verdict.
AUDITABLE_ASSURANCE_STATES = frozenset({"pending", "unavailable"})

#: Assurance states that leave the optional post-run audit unable to speak.
#: ``skipped`` is a settled decision not to audit and is not a degradation.
DEGRADED_ASSURANCE_STATES = frozenset({"unavailable", "partial"})


@dataclass(frozen=True)
class TerminalAuditContext:
    """Everything the terminal audit needs, resolved once per run."""

    plan: supervisor_audit.AuditPlan
    run_dir: Path
    state_dir: Path
    plan_dir: Path
    workspace_root: Path
    #: The state root holding the one shared assurance ledger, not this plan's
    #: state directory. ``None`` resolves to the default root.
    ledger_state_dir: Path | None = None
    #: Workspace-relative paths of the repositories the plan registered, which
    #: are the only ones the audit's read-only boundary check fingerprints.
    repositories: tuple[str, ...] = ()
    #: Whether those paths came from a plan registry rather than being
    #: synthesized for a legacy plan that registered none.
    registry_declared: bool = True


@dataclass(frozen=True)
class TerminalAssuranceOutcome:
    """What the advisory terminal audit did, as a value rather than a side effect."""

    #: Whether the audit was interrupted rather than allowed to render a verdict.
    cancelled: bool
    #: Whether an audit ran at all. ``False`` means the run was not auditable
    #: (ineligible, already graded, or a settled decision not to audit).
    audited: bool
    #: Whether the derived operator projection was attached beside the verdict.
    reported: bool
    #: The assurance state the run ended on, when it has one.
    state: str | None = None

    @property
    def degraded(self) -> bool:
        """Whether the audit left the run graded but unable to speak."""
        return self.state in DEGRADED_ASSURANCE_STATES


def attach_terminal_assurance(
    payload: dict[str, Any],
    context: TerminalAuditContext,
    persistence: run_coordination_seams.CoordinationPersistence,
    abort: harnesses.AbortSignal | None = None,
    *,
    state_snapshot: Callable[[dict[str, Any]], dict[str, Any]],
) -> bool:
    """Audit the run, publish the verdict, and attach the bounded summary.

    A run that resolved no independent role starts ``unavailable`` and is still
    audited, because the deterministic half needs no agent and the reason the
    run could not be graded belongs in the shared inbox rather than only in its
    own state.

    ``state_snapshot`` is the runner's compaction of the live payload; it is
    taken under the run lock and handed to the audit as an immutable copy, so
    the audit never reads a payload another thread is still writing.

    Returns whether the audit was cancelled rather than completed.
    """
    assurance = payload.get("assurance")
    if not isinstance(assurance, Mapping) or not assurance.get("eligible"):
        return False
    if assurance.get("state") not in AUDITABLE_ASSURANCE_STATES:
        return False
    with persistence.lock:
        state_payload = state_snapshot(payload)
    cancelled = False
    try:
        summary = supervisor_audit.audit_terminal_run(
            context.plan,
            run_id=str(payload.get("run_id")),
            run_dir=context.run_dir,
            state_dir=context.state_dir,
            plan_dir=context.plan_dir,
            workspace_root=context.workspace_root,
            ledger_state_dir=context.ledger_state_dir,
            state_payload=state_payload,
            repositories=context.repositories,
            registry_declared=context.registry_declared,
            abort=abort,
        )
    except KeyboardInterrupt, supervisor_audit.AuditCancelled:
        # Cancelling an advisory audit cancels the audit alone: the outcome and
        # the exit code were decided before this ran, and the terminal record
        # they belong to has still not been written.
        summary = supervisor_audit.cancelled_summary(
            context.plan, run_id=str(payload.get("run_id"))
        )
        cancelled = True
    with persistence.lock:
        payload["assurance"] = summary.to_dict()
    return cancelled


def attach_assurance_report(
    payload: dict[str, Any],
    context: TerminalAuditContext,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> bool:
    """Attach the additive operator projection of this run's assurance object.

    Written beside the assurance object rather than inside it: the per-run
    object is a validated schema that rejects unknown keys, while this block is
    a derived view whose open-issue count is a fact about the whole inbox at the
    moment the run ended.
    """
    with persistence.lock:
        assurance = payload.get("assurance")
        run_dir = payload.get("run_dir")
    report = assurance_report.build_run_report(
        assurance,
        run_dir=run_dir,
        state_dir=context.ledger_state_dir,
    )
    if report is None:
        return False
    with persistence.lock:
        payload["assurance_report"] = report
    return True


def record_degraded_assurance(
    payload: dict[str, Any],
    *,
    persistence: run_coordination_seams.CoordinationPersistence,
) -> None:
    """Record that optional assurance could not speak, without blocking on it.

    Post-run assurance is advisory by design, so its unavailability may not
    turn a run that earned its success into a failure. What it may not do is
    disappear: a run whose audit never rendered a verdict is a *degraded*
    success, and this states that explicitly beside the evidence the audit did
    leave behind, so an operator reading a green run can tell the difference
    between "graded clean" and "not graded".
    """
    with persistence.lock:
        assurance = payload.get("assurance")
        assurance = assurance if isinstance(assurance, Mapping) else {}
        state = assurance.get("state")
        if state not in DEGRADED_ASSURANCE_STATES:
            payload.pop("assurance_degraded", None)
            return
        artifacts = assurance.get("artifacts")
        evidence = [
            item.get("locator")
            for item in (artifacts if isinstance(artifacts, list) else [])
            if isinstance(item, Mapping) and item.get("locator")
        ]
        payload["assurance_degraded"] = {
            "assurance_state": state,
            "reason": assurance.get("reason"),
            "blocking": False,
            "run_ok": bool(payload.get("ok")),
            "evidence": evidence,
            "issue_ids": list(assurance.get("issue_ids") or ()),
        }
        persistence.write_state(payload)


class FinalizationPhase(Protocol):
    """The run's published "terminal work in flight" phase, as the audit uses it.

    Structural so the runner keeps ownership of the heartbeat thread and its
    durable events while this module owns the guarantee that the phase is
    always closed.
    """

    def start(self) -> None: ...

    def finish(self, *, cancelled: bool = False) -> None: ...


class TerminalAssuranceCoordinator:
    """Runs the advisory terminal audit inside a published, closeable phase.

    The invariant is that the phase is closed exactly once whatever happens
    inside it. A cancelled audit closes it as cancelled; an audit that raises
    something the audit boundary does not convert still closes it, and the
    exception propagates to a caller that already decided this run's exit code.
    """

    def __init__(
        self,
        *,
        payload: dict[str, Any],
        context: TerminalAuditContext,
        persistence: run_coordination_seams.CoordinationPersistence,
        phase: FinalizationPhase,
        state_snapshot: Callable[[dict[str, Any]], dict[str, Any]],
        abort: harnesses.AbortSignal | None = None,
    ) -> None:
        self._payload = payload
        self._context = context
        self._persistence = persistence
        self._phase = phase
        self._state_snapshot = state_snapshot
        self._abort = abort

    def run(self) -> TerminalAssuranceOutcome:
        """Publish the phase, audit, report, and always close the phase."""
        before = self._assurance_state()
        auditable = before in AUDITABLE_ASSURANCE_STATES and self._eligible()
        self._phase.start()
        cancelled = False
        reported = False
        try:
            cancelled = attach_terminal_assurance(
                self._payload,
                self._context,
                self._persistence,
                abort=self._abort,
                state_snapshot=self._state_snapshot,
            )
            reported = attach_assurance_report(
                self._payload, self._context, self._persistence
            )
        finally:
            self._phase.finish(cancelled=cancelled)
        return TerminalAssuranceOutcome(
            cancelled=cancelled,
            audited=auditable,
            reported=reported,
            state=self._assurance_state(),
        )

    def _assurance(self) -> Mapping[str, Any]:
        with self._persistence.lock:
            assurance = self._payload.get("assurance")
        return assurance if isinstance(assurance, Mapping) else {}

    def _assurance_state(self) -> str | None:
        state = self._assurance().get("state")
        return state if isinstance(state, str) else None

    def _eligible(self) -> bool:
        return bool(self._assurance().get("eligible"))
