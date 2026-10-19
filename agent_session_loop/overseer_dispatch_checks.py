"""Identity and authorization checks before an overseer action runs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import (
    intervention,
    overseer_context,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
)
from .overseer_dispatch_models import (
    ACTION_ACCESS,
    RECOVERY_LIMIT,
    Action,
    Authorize,
    BuildInventory,
    Identity,
    InspectRun,
    Now,
    Reply,
    _Refused,
    progress_token,
)


@dataclass(frozen=True)
class _Located:
    """The run, as much of it as may be read before permission is asked.

    Discovery's own record of the plan and this ledger's watch lease, and
    nothing from inside the run: no state file, no mailbox, no repository. The
    shared requirements allow minimal identity metadata before authorization
    and nothing else, so this type is the shape of that allowance -- and
    :func:`_examine` cannot be reached without going past :func:`_permit`.
    """

    scope: str
    repositories: tuple[overseer_policy_client.Repository, ...]


def _locate(
    *,
    action: Action,
    watcher_id: str,
    ledger: overseer_store_ledger.Ledger,
    build_inventory: BuildInventory,
    state_dir: str | Path | None,
    now: Now,
) -> _Located | _Refused:
    """Identify the run from metadata alone, or refuse before asking anybody.

    Read-only, and re-runnable for exactly that reason: the recheck after
    preparation is this function called again, so anything it wrote would be
    written twice.

    The scope guards live here rather than after authorization because they
    decide *what question to ask*. An action naming repositories the run does
    not declare is refused without AgentMux hearing about it at all, which is
    the difference between refusing an out-of-scope request and asking whether
    an out-of-scope request is permitted.
    """
    inventory = build_inventory(state_dir=state_dir)
    candidate = _candidate(inventory, action.plan_dir)
    if candidate is None or not candidate.record_observed:
        return _Refused(
            "plan_unobserved",
            f"discovery cannot read {action.plan_dir}",
        )
    if not candidate.eligible:
        reasons = ", ".join(candidate.ineligible_reasons)
        return _Refused(
            "plan_unobserved",
            f"{action.plan_dir} cannot be acted on: {reasons}",
        )
    if candidate.run_id != action.run_id:
        return _Refused(
            "run_replaced",
            f"the plan is running {candidate.run_id!r}, not {action.run_id!r}",
        )

    scope = overseer_context.scope_digest(candidate)
    if scope != action.scope:
        return _Refused(
            "scope_expanded",
            "the run's repository scope is not the one this action was "
            "authorized against",
        )
    declared = overseer_policy_client.repositories_for(candidate.repositories)
    if tuple(action.repositories) != declared:
        return _Refused(
            "scope_expanded",
            "the action names repositories the run does not declare: asked for "
            f"{_names(action.repositories)}, run declares {_names(declared)}",
        )

    lease = ledger.watch_lease(action.plan_dir)
    if lease is None or not lease.held:
        return _Refused("ownership_lost", f"no watcher holds {action.plan_dir}")
    if lease.watcher_id != watcher_id:
        return _Refused(
            "ownership_lost",
            f"{lease.watcher_id!r} holds this plan, not {watcher_id!r}",
        )
    if lease.expired(now()):
        return _Refused("ownership_lost", "this watcher's lease has expired")
    if lease.run_id != action.run_id:
        return _Refused(
            "ownership_lost",
            f"the lease names run {lease.run_id!r}, not {action.run_id!r}",
        )

    return _Located(scope=scope, repositories=declared)


def _examine(
    *,
    action: Action,
    located: _Located,
    ledger: overseer_store_ledger.Ledger,
    inspect_run: InspectRun,
    state_dir: str | Path | None,
) -> Identity | _Refused:
    """Open the run and establish the rest of the identity, or refuse.

    Everything here reads the run's own content -- the state file and the
    mailbox behind :func:`agent_session_loop.intervention.inspect_run` -- so it
    is only ever called under a grant :func:`_permit` established moments
    before. Read-only and re-runnable, like :func:`_locate`, because the second
    pass is this function called again.
    """
    facts = inspect_run(action.plan_dir, state_dir=state_dir)
    if facts.run_id != action.run_id:
        return _Refused(
            "run_replaced",
            f"run state names {facts.run_id!r}, not {action.run_id!r}",
        )
    token = intervention.action_token(facts)
    if token != action.token:
        return _Refused(
            "stale_action",
            "the action token does not match the run's current state",
        )
    progress = progress_token(facts)

    # Before the incident's own status, so a run that escalated on its second
    # failure is told it ran out of recoveries rather than that its incident is
    # not open. The count reads rows regardless of incident status and
    # regardless of which incident they name, because both of those are things
    # a retry loop can change and the situation is not.
    spent = ledger.recovery_attempts(plan_dir=action.plan_dir, progress_token=progress)
    if len(spent) >= RECOVERY_LIMIT:
        return _Refused(
            "recovery_exhausted",
            f"{len(spent)} attempts on this plan have failed without the run "
            "making progress",
            escalate=True,
            recoveries=len(spent),
        )

    incident = ledger.incident(action.incident_id)
    if incident is None:
        return _Refused("incident_unknown", f"no incident {action.incident_id}")
    if incident.plan_dir != action.plan_dir or incident.run_id != action.run_id:
        return _Refused(
            "incident_unknown",
            f"incident {action.incident_id} belongs to another run",
        )
    if incident.status != "open":
        return _Refused(
            "incident_unknown",
            f"incident {action.incident_id} is {incident.status}, not open",
        )

    question_id: str | None = None
    if isinstance(action.payload, Reply):
        question_id = intervention.selected_question_id(
            facts, action.payload.question_id
        )
        if question_id is None:
            return _Refused(
                "question_unknown",
                f"question {action.payload.question_id!r} is not open on this run",
            )

    already = ledger.executed_action(
        plan_dir=action.plan_dir,
        operation=action.operation,
        target=action.target,
        progress_token=progress,
    )
    if already is not None:
        return _Refused(
            "duplicate_dispatch",
            f"action {already.action_id} already performed this operation in "
            "this situation",
        )

    return Identity(
        plan_dir=action.plan_dir,
        run_id=action.run_id,
        scope=located.scope,
        token=token,
        progress=progress,
        repositories=located.repositories,
        question_id=question_id,
    )


def _permit(
    *,
    action: Action,
    repositories: tuple[overseer_policy_client.Repository, ...],
    ledger: overseer_store_ledger.Ledger,
    authorize: Authorize,
    timeout: float,
) -> overseer_policy_client.Grant | _Refused:
    """Ask AgentMux about every participating repository, and record the answer.

    The repositories asked about are the run's declared set as discovery reads
    it now, not the set the action carries: the action's set was already
    required to equal it by :func:`_locate`, so asking about the run's own is
    the same question asked against the current corpus rather than against a
    caller's copy. Taking them as a plain tuple rather than off an
    :class:`Identity` is what lets this run before any content is read, since an
    ``Identity`` cannot exist until it has been.
    """
    decision = authorize(
        plan_dir=action.plan_dir,
        access=ACTION_ACCESS,
        repositories=repositories,
        timeout=timeout,
    )
    ledger.record_decision(decision)
    if decision.config_fingerprint:
        ledger.note_policy(config_fingerprint=decision.config_fingerprint)
    grant = decision.grant()
    if grant is not None:
        return grant
    if not decision.available:
        return _Refused(
            "policy_unavailable",
            decision.summary or f"AgentMux could not be asked: {decision.reason}",
        )
    refused = decision.refused_repositories()
    if refused:
        names = ", ".join(outcome.alias for outcome in refused)
        return _Refused(
            "unauthorized",
            f"AgentMux refused {names}: {decision.summary or decision.reason}",
        )
    return _Refused(
        "unauthorized",
        decision.summary or f"AgentMux refused this dispatch: {decision.reason}",
    )


def _candidate(
    inventory: overseer_discovery.Inventory, plan_dir: str
) -> overseer_discovery.Candidate | None:
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return candidate
    return None


def _names(repositories: tuple[overseer_policy_client.Repository, ...]) -> str:
    return ", ".join(repository.alias for repository in repositories) or "(none)"


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)
