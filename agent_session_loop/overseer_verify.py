"""The overseer's verify: run a check the plan already declared, and no other.

Every other operation in the scope moves words or commits. This one runs a
program, which makes it the single place in the overseer where a supervisor
could turn into a shell. So the payload it is reached through,
:class:`agent_session_loop.overseer_dispatch.Verify`, carries a *name* --- and
this module is what that name is looked up in.

## The contract is the menu

The argv is never in the action. :func:`select` reads the session the run is
actually stopped on out of the run's own state, reads that session's
``## Completion Contract`` with
:func:`agent_session_loop.contracts.read_completion_contract`, and takes the
check whose ``name`` matches. A name the contract does not declare is
``check_undeclared``, and the refusal lists the names it does declare rather
than inviting another guess.

The consequence is the point: changing what a verify runs means changing a
plan file, which lives in a repository, under the write rules that govern
repositories --- a reviewed edit rather than a string in a supervisor's
argument list. Nothing in this module composes a command, and there is no
parameter through which one could be passed.

Which session is likewise not a choice. It is
``runner_state["active_turn"]["session_id"]``, the session the run is on;
naming one here would let a caller verify session 3's contract against session
9's working tree and read the result as though it meant something.

## The check runs where the action was authorized

:func:`agent_session_loop.verification.VerificationContext` already refuses a
check whose location escapes its repository. What it cannot know is which
repositories *this action* was permitted in, because AgentMux answered that
about the action and not about the plan. So the resolved root is checked
against the action's own authorized set, and a check that resolves outside it
is ``scope_unauthorized`` --- the case being a registered plan whose contract
names an alias the authorization did not cover.

The directory the context is built from is read back rather than chosen: a
registered plan resolves against the workspace root discovery recorded for the
run, and a legacy plan against the one repository the action names. A plan with
no registry and more than one repository has no unambiguous root, and that is
``workspace_unrecoverable`` rather than a repository picked by position.

## A failing check is a fact, not a refusal

``Receipt.failed`` here means the check ran and did not pass, and the detail
carries the real reason the check reported. That reads out of the dispatcher as
``performed: false`` with no ``refusal``, which is exactly right: nothing
refused the verify, and the answer is no. Reporting a red check as
``executed`` because the operation completed would close an incident on a
verification that failed.

Nothing is retried and nothing is interpreted. One check is named, one check
runs, and its own ``status`` is the answer.

## Reconciliation is decidable, uniquely

A verification has no side effect to look for afterwards, which would normally
make a row a dead end. It is the opposite: because nothing was changed, a row
whose result nobody observed is fully settled by saying so. There is no world
to re-read, no commit to search for, and no honest reason to leave the plan
blocked --- ``result_unobserved`` says the answer was lost, and the remedy is
to ask again.

Durable records this boundary writes: **none**. ``artifact_dir`` is not passed,
so a failed command's streams stay in the bounded preview the result carries and
nothing is retained on disk; the dispatcher owns the ledger row.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import (
    contracts,
    intervention,
    overseer_checkpoint,
    overseer_decisions,
    overseer_discovery,
    overseer_dispatch,
    overseer_policy_client,
    overseer_store,
    plans,
    verification,
)

__all__ = [
    "ESCALATING_REFUSALS",
    "REFUSALS",
    "VERIFY_OPERATIONS",
    "Prepared",
    "Refusal",
    "Selection",
    "VerifyAdapter",
    "adapters",
    "select",
    "session_under_verification",
]

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other four are somebody else's adapters.
VERIFY_OPERATIONS: tuple[Operation, ...] = ("verify",)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "check_unnamed",
    "run_unreadable",
    "session_unknown",
    "contract_absent",
    "contract_unreadable",
    "check_undeclared",
    "workspace_unrecoverable",
    "check_unresolvable",
    "scope_unauthorized",
    "not_prepared",
    "run_moved",
    "contract_moved",
    "check_failed",
    "check_errored",
    "result_unobserved",
]

#: Why a verification did not happen, or did not pass. Closed, and a consumer
#: contract: operator surfaces read these words, so a spelling change is a
#: contract change. The first five are
#: :data:`~agent_session_loop.overseer_checkpoint.AUTHORITY_REFUSALS` unchanged,
#: so the authority check is one implementation rather than two.
#:
#: * ``payload_mismatch`` -- the action is not a verify.
#: * ``check_unnamed`` -- the payload names no check, so there is nothing to
#:   look up. Distinct from a name that was looked up and not found.
#: * ``run_unreadable`` -- the plan's own state could not be read, so which
#:   session it is on cannot be established.
#: * ``session_unknown`` -- the run records no session, or one that is not a
#:   session id. A contract cannot be read without knowing whose it is.
#: * ``contract_absent`` -- that session declares no completion contract, so it
#:   declares no checks.
#: * ``contract_unreadable`` -- the session file exists and could not be parsed.
#:   Escalates: a malformed contract is an authoring problem, not a retry.
#: * ``check_undeclared`` -- the contract does not declare this name. The
#:   detail lists the names it does declare.
#: * ``workspace_unrecoverable`` -- the directory the checks resolve against
#:   could not be established from the run's own record. Escalates.
#: * ``check_unresolvable`` -- the declared check's own location does not
#:   resolve under its repository. That is the contract's problem, reported
#:   rather than worked around.
#: * ``scope_unauthorized`` -- the check resolves into a repository this action
#:   was not authorized for. Escalates.
#: * ``not_prepared`` -- execution found no preparation to consume.
#: * ``run_moved`` -- the run left the situation the verify was prepared in, so
#:   the check would be answering about a different state than the one asked
#:   about.
#: * ``contract_moved`` -- the contract changed between preparation and
#:   execution, so the name no longer denotes the check that was authorized.
#: * ``check_failed`` -- the check ran and did not pass. Not a refusal of the
#:   operation: the operation happened and the answer is no.
#: * ``check_errored`` -- the check could not be run to a verdict at all.
#: * ``result_unobserved`` -- reconciliation found a row whose result nobody
#:   recorded. A verification changes nothing, so the only thing lost is the
#:   answer, and asking again is the whole remedy.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "check_unnamed",
    "run_unreadable",
    "session_unknown",
    "contract_absent",
    "contract_unreadable",
    "check_undeclared",
    "workspace_unrecoverable",
    "check_unresolvable",
    "scope_unauthorized",
    "not_prepared",
    "run_moved",
    "contract_moved",
    "check_failed",
    "check_errored",
    "result_unobserved",
)

#: The refusals a retry cannot clear. Published rather than acted on: this
#: module holds no ledger and settles no incident, so escalation is something it
#: reports and the dispatcher's caller performs. Each of these is a statement
#: about the plan or the permission rather than about now, and looking again
#: later would produce the same answer.
ESCALATING_REFUSALS: tuple[Refusal, ...] = (
    "contract_unreadable",
    "workspace_unrecoverable",
    "scope_unauthorized",
)

Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]
BuildInventory = Callable[..., overseer_discovery.Inventory]
InspectRun = Callable[..., intervention.RunFacts]
ReadContract = Callable[..., contracts.CompletionContract | None]
RunCheck = Callable[..., verification.CheckResult]


@dataclass(frozen=True)
class Selection:
    """The check a name resolved to, or why that name resolved to nothing.

    ``declared`` is carried whether or not the lookup succeeded, because the
    useful thing to tell an operator who named a check that does not exist is
    which ones do.
    """

    session_id: str
    declared: tuple[str, ...]
    check: contracts.VerificationCheck | None = None
    context: verification.VerificationContext | None = None
    resolved_root: str = ""
    refusal: Refusal | None = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Whether this is a runnable check rather than a reason there is none."""
        return self.refusal is None


@dataclass(frozen=True)
class Prepared:
    """What preparation established, handed to the execution that consumes it."""

    target: str
    selection: Selection
    progress: str
    ruling: overseer_decisions.Ruling | None


def session_under_verification(facts: intervention.RunFacts) -> str:
    """The session this run is on, as a session id, or ``""``.

    Read from the run rather than taken from the caller. A verify that could be
    pointed at any session would be answering about one session's contract with
    another session's working tree, and the result would look like a verdict.
    """
    turn = facts.runner_state.get("active_turn")
    named = turn.get("session_id") if isinstance(turn, Mapping) else None
    if not isinstance(named, str) or not named.strip():
        return ""
    try:
        return plans.parse_session_id(named).value
    except ValueError:
        return ""


def select(
    *,
    plan_dir: str,
    check: str,
    facts: intervention.RunFacts,
    repositories: Sequence[overseer_policy_client.Repository],
    state_dir: str | Path | None = None,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    read_contract: ReadContract = contracts.read_completion_contract,
) -> Selection:
    """Look one check name up in the contract of the session the run is on.

    Every step can only narrow: the session comes from the run, the contract
    comes from the session, the check comes from the contract, and the root the
    check resolves against comes from what discovery recorded and what the
    action was authorized for. There is no step at which a value the caller
    supplied becomes part of what runs.
    """
    named = check.strip()
    if not named:
        return Selection(
            session_id="",
            declared=(),
            refusal="check_unnamed",
            detail="a verify names the check to run; this action names none",
        )
    session_id = session_under_verification(facts)
    if not session_id:
        return Selection(
            session_id="",
            declared=(),
            refusal="session_unknown",
            detail=f"{plan_dir} records no session id for its active turn, so "
            "there is no contract to read the check out of",
        )

    try:
        contract = read_contract(plan_dir, session_id)
    except plans.PlanError as error:
        return Selection(
            session_id=session_id,
            declared=(),
            refusal="contract_unreadable",
            detail=str(error),
        )
    if contract is None:
        return Selection(
            session_id=session_id,
            declared=(),
            refusal="contract_absent",
            detail=f"session {session_id} declares no completion contract, so it "
            "declares no checks",
        )

    declared = tuple(item.name for item in contract.verify.checks)
    found = next((item for item in contract.verify.checks if item.name == named), None)
    if found is None:
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal="check_undeclared",
            detail=f"session {session_id} declares no check named {named!r}; it "
            f"declares {', '.join(declared) or '(none)'}",
        )

    context = _context(
        plan_dir=plan_dir,
        repositories=repositories,
        state_dir=state_dir,
        build_inventory=build_inventory,
    )
    if isinstance(context, tuple):
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal=context[0],
            detail=context[1],
        )

    try:
        resolved = context.resolve_check(found)
    except plans.PlanError as error:
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal="check_unresolvable",
            detail=str(error),
        )

    root = plans.canonicalize_path(resolved.resolved_root)
    permitted = {plans.canonicalize_path(item.path) for item in repositories}
    if root not in permitted:
        return Selection(
            session_id=session_id,
            declared=declared,
            refusal="scope_unauthorized",
            detail=f"check {named!r} resolves into {root}, which is not one of "
            f"the repositories this action was authorized for: "
            f"{', '.join(sorted(str(item) for item in permitted)) or '(none)'}",
        )
    return Selection(
        session_id=session_id,
        declared=declared,
        check=found,
        context=context,
        resolved_root=str(root),
    )


@dataclass(frozen=True)
class VerifyAdapter:
    """The ``verify`` operation, behind the dispatcher's guards.

    Every collaborator that reads the world is a seam with a real default, so
    the adapter can be exercised against a corpus without a process --- which
    is what lets the operator-surface tests drive a real declared check and
    still spawn nothing.

    ``prepared`` carries one preparation forward to the execution that consumes
    it, keyed on the plan, incident and target, and is emptied by that
    execution: a memo between two calls about one action rather than state this
    module keeps.
    """

    authority: Authority
    state_dir: Path | None = None
    build_inventory: BuildInventory = overseer_discovery.build_inventory
    inspect_run: InspectRun = intervention.inspect_run
    read_contract: ReadContract = contracts.read_completion_contract
    run_check: RunCheck = verification.run_check
    prepared: dict[str, Prepared] = field(
        default_factory=dict, repr=False, compare=False
    )

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Check authority, then resolve the name to a declared check.

        Nothing is run here, so preparation is a set of readings: an execution
        that never arrives leaves the plan exactly as it found it.
        """
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Verify):
            return overseer_dispatch.Preparation.declined(
                _phrase("payload_mismatch", "not a verify")
            )
        ruling = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            ruling,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
            operation="verify",
        )
        if granted is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*granted))

        facts = self._facts(action.plan_dir)
        if facts is None:
            return overseer_dispatch.Preparation.declined(
                _phrase("run_unreadable", f"{action.plan_dir} could not be read")
            )
        selection = self._select(action, facts)
        if selection.refusal is not None:
            return overseer_dispatch.Preparation.declined(
                _phrase(selection.refusal, selection.detail)
            )

        self.prepared[_key(action.plan_dir, action.incident_id, action.target)] = (
            Prepared(
                target=action.target,
                selection=selection,
                progress=overseer_dispatch.progress_token(facts),
                ruling=ruling,
            )
        )
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Re-establish authority and the contract, then run the one check."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Verify):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not a verify")
            )
        prepared = self.prepared.pop(
            _key(action.plan_dir, action.incident_id, action.target), None
        )
        if prepared is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("not_prepared", "no preparation established this verify")
            )
        current = self.authority(action)
        granted = overseer_checkpoint.authority_refusal(
            current,
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
            operation="verify",
        )
        if granted is not None:
            return overseer_dispatch.Receipt.failed(_phrase(*granted))
        if not _same_decision(prepared.ruling, current):
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "authority_superseded",
                    f"{_decision_id(prepared.ruling)} was replaced by "
                    f"{_decision_id(current)}",
                )
            )

        facts = self._facts(action.plan_dir)
        if facts is None:
            return overseer_dispatch.Receipt.failed(
                _phrase("run_unreadable", f"{action.plan_dir} could not be read")
            )
        if overseer_dispatch.progress_token(facts) != prepared.progress:
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "run_moved",
                    "the run left the situation this verify was prepared in, so "
                    "the check would answer about a different state",
                )
            )

        # Re-read rather than trust the memo. The contract is a file in a
        # repository, and the window between preparation and execution is
        # exactly the window a turn could rewrite it in.
        selection = self._select(action, facts)
        if selection.refusal is not None:
            return overseer_dispatch.Receipt.failed(
                _phrase(selection.refusal, selection.detail)
            )
        if selection.check != prepared.selection.check:
            return overseer_dispatch.Receipt.failed(
                _phrase(
                    "contract_moved",
                    f"check {action.target!r} is not the check that was prepared; "
                    "the contract changed while the action was in flight",
                )
            )

        try:
            result = self.run_check(selection.check, context=selection.context)
        except (plans.PlanError, OSError, ValueError) as error:
            return overseer_dispatch.Receipt.failed(
                _phrase("check_errored", f"{type(error).__name__}: {error}")
            )
        return _receipt(result, selection)

    def reconcile(
        self,
        record: overseer_store.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Settle a verify whose result a dead process took with it.

        The one operation in the scope whose in-doubt row is decidable without
        reading anything. A verification changes nothing, so there is no world
        that could carry evidence of it, and no state a later reader could be
        misled by: whatever the check said, nobody has it. Saying so settles the
        row honestly and leaves the plan free, and running the check again is
        the entire remedy.
        """
        if record.operation != "verify":
            return overseer_dispatch.Receipt.uncertain(
                detail=_phrase("payload_mismatch", record.operation)
            )
        return overseer_dispatch.Receipt.failed(
            detail=_phrase(
                "result_unobserved",
                f"check {record.target!r} was dispatched and its result was never "
                "recorded; verification changes nothing, so the answer is the "
                "only thing lost and asking again is the remedy",
            ),
            locator=record.progress_token,
        )

    # -- seams, read the same way everywhere -------------------------------

    def _facts(self, plan_dir: str) -> intervention.RunFacts | None:
        try:
            return self.inspect_run(plan_dir, state_dir=self.state_dir)
        except intervention.InterventionError, OSError, ValueError:
            return None

    def _select(
        self, action: overseer_dispatch.Action, facts: intervention.RunFacts
    ) -> Selection:
        payload = action.payload
        named = payload.check if isinstance(payload, overseer_dispatch.Verify) else ""
        return select(
            plan_dir=action.plan_dir,
            check=named,
            facts=facts,
            repositories=action.repositories,
            state_dir=self.state_dir,
            build_inventory=self.build_inventory,
            read_contract=self.read_contract,
        )


def adapters(
    *,
    authority: Authority,
    state_dir: Path | None = None,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    inspect_run: InspectRun = intervention.inspect_run,
    read_contract: ReadContract = contracts.read_completion_contract,
    run_check: RunCheck = verification.run_check,
) -> overseer_dispatch.Registry:
    """Build a dispatcher registry holding exactly the verify adapter.

    The other four operations are absent rather than stubbed, because
    :meth:`~agent_session_loop.overseer_dispatch.Registry.supports` is what the
    single door consults and a stub that refuses is indistinguishable from an
    adapter that failed.
    """
    return overseer_dispatch.registry(
        {
            "verify": VerifyAdapter(
                authority=authority,
                state_dir=state_dir,
                build_inventory=build_inventory,
                inspect_run=inspect_run,
                read_contract=read_contract,
                run_check=run_check,
            )
        }
    )


# -- internals -------------------------------------------------------------


def _context(
    *,
    plan_dir: str,
    repositories: Sequence[overseer_policy_client.Repository],
    state_dir: str | Path | None,
    build_inventory: BuildInventory,
) -> verification.VerificationContext | tuple[Refusal, str]:
    """Build the context this plan's checks resolve against, or say why not.

    Only one question is answered here --- which directory the plan's own
    verification context is built from --- and it is answered from the record
    rather than from anything this process was configured with. How a context is
    then built from a plan stays :mod:`agent_session_loop.verification`'s
    answer, so a registered plan and a legacy one differ here by one path and
    nowhere else.
    """
    try:
        registry = plans.read_repository_registry(plan_dir)
    except plans.PlanError as error:
        return ("workspace_unrecoverable", str(error))

    if registry is None:
        if len(repositories) != 1:
            return (
                "workspace_unrecoverable",
                f"{plan_dir} declares no repository registry and this action "
                f"names {len(repositories)} repositories, so a check's root is "
                "ambiguous",
            )
        cwd: str = repositories[0].path
    else:
        recorded = _workspace_root(
            plan_dir, state_dir=state_dir, build_inventory=build_inventory
        )
        if not recorded:
            return (
                "workspace_unrecoverable",
                f"discovery records no workspace root for {plan_dir}, so the "
                "registered checks have nothing to resolve against",
            )
        cwd = recorded

    try:
        return verification.verification_context_from_plan(plan_dir, cwd)
    except plans.PlanError as error:
        return ("workspace_unrecoverable", str(error))


def _workspace_root(
    plan_dir: str,
    *,
    state_dir: str | Path | None,
    build_inventory: BuildInventory,
) -> str:
    """The workspace root the run itself recorded, as discovery read it."""
    inventory = build_inventory(state_dir=state_dir)
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return candidate.workspace_root or ""
    return ""


def _receipt(
    result: verification.CheckResult, selection: Selection
) -> overseer_dispatch.Receipt:
    """Report what the check said, in the vocabulary the dispatcher settles on.

    A check that ran and failed is ``failed`` with no refusal spelled over it in
    the dispatcher's own vocabulary: the operation happened, and the answer is
    no. The detail is the check's own reason rather than a summary of it.
    """
    locator = _locator(result, selection)
    if result.status == "pass":
        return overseer_dispatch.Receipt.executed(
            detail=f"check {result.name!r} passed in {selection.session_id}'s contract",
            locator=locator,
        )
    return overseer_dispatch.Receipt.failed(
        detail=_phrase(
            "check_failed",
            f"{result.name}: {result.detail or result.failure_reason or 'no detail'}",
        ),
        locator=locator,
    )


def _locator(result: verification.CheckResult, selection: Selection) -> str:
    """Where the check ran, which is the only evidence a verification leaves."""
    where = (
        result.qualified_cwd
        or result.qualified_path
        or result.cwd
        or result.path
        or result.resolved_root
        or selection.resolved_root
    )
    return f"{result.name} in {where}" if where else result.name


def _key(plan_dir: str, incident_id: int, target: str) -> str:
    """The memo key one action's preparation and execution agree on."""
    return "\x00".join((plan_dir, str(incident_id), target))


def _phrase(refusal: Refusal, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


def _decision_id(ruling: overseer_decisions.Ruling | None) -> str:
    if ruling is None or ruling.decision is None:
        return "no decision"
    return ruling.decision.decision_id


def _same_decision(
    first: overseer_decisions.Ruling | None,
    second: overseer_decisions.Ruling | None,
) -> bool:
    """Whether two rulings rest on the same governing decision, unchanged."""
    if first is None or second is None:
        return False
    left, right = first.decision, second.decision
    if left is None or right is None:
        return False
    return (
        left.decision_id == right.decision_id
        and left.stance == right.stance
        and left.at == right.at
        and left.operations == right.operations
    )
