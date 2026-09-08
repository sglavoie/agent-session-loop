"""The one door an overseer action goes through, and what it checks on the way.

Every operation the overseer is permitted to perform -- answering a question,
sending guidance, running verification, committing a checkpoint, resuming a
run -- arrives here as a typed :class:`Action` and leaves as a :class:`Dispatch`
that either happened or names why it did not. There is no second path. The
value of a single door is entirely in what it refuses, so most of this module
is refusals.

## An action cannot say "run this"

The payload is a closed union of five frozen dataclasses -- :class:`Reply`,
:class:`Guidance`, :class:`Verify`, :class:`Commit`, :class:`Resume` -- and not
one of them has a field for a command, an argument vector, an interpreter, an
environment or a shell. :class:`Verify` names *which* check to run and nothing
about how; the argv comes from the plan's own verification contract, which the
adapter reads for itself. So "the overseer ran an arbitrary command" is not a
thing a caller can express and a guard failed to catch: there is no field to
put it in.

That is why the registry is closed too. :func:`registry` refuses any key
outside :data:`OPERATIONS` at construction, and :func:`dispatch` refuses a
payload that is not one of the five types before it looks at anything else --
before the inventory walk, before the authorization question, before any
ledger row. An unsupported operation costs a subprocess nowhere.

## Authorization is asked twice, and the second one is the one that counts

The sequence is: locate the run from identity metadata, ask AgentMux, read the
run's content, reconcile anything left in doubt, let the adapter prepare, then
**locate and ask AgentMux again** and require both answers to be identical to
the first before content is read a second time. Preparation may wait -- on a
subprocess, on a lock, on a mailbox -- and a decision that was true before a
wait is not evidence about the moment after it. A run that was replaced, a
lease that expired, a policy that moved, an operator who withdrew the opt-in
mid-wait: all of them land between the two passes and all of them refuse.

The split between :func:`_locate` and :func:`_examine` is where "authorize
before access" is enforced rather than described. :func:`_locate` reads only
identity metadata -- discovery's own record of the plan and this ledger's watch
lease -- which is the entire budget the shared requirements allow before
permission. Everything that opens the run itself, starting with the run state
and the mailbox behind :func:`agent_session_loop.intervention.inspect_run`,
lives in :func:`_examine` and runs only under a grant established moments
earlier. A revoked opt-in therefore costs a discovery walk and nothing else, on
both passes.

The recheck earns its keep in one more case that no single pass could catch.
Reconciliation settles a dispatch that a crashed process left in doubt, which
can turn an action that *looks* new into one that already executed -- so the
duplicate check has to run after reconciliation, and the only place after
reconciliation is the second pass.

For the same reason :func:`reconcile` takes a :class:`Grant` and not a flag:
its adapters read run state, mailboxes and Git history to answer "did this
happen?", so it is content access, and there is no way to call it without
having asked.

## Attempted is not executed

:meth:`agent_session_loop.overseer_store.Ledger.attempt_action` writes the row
*before* the adapter is called, and only a receipt settles it. A process that
dies mid-dispatch therefore leaves a durable ``attempted`` row, and the next
dispatch against that plan refuses with ``outcome_uncertain`` until
:func:`reconcile` has asked the adapters what actually happened. An adapter
that raises is the same case, not a failure: an exception says the call did not
return, never that the thing did not happen.

## Two recoveries, and no way to buy a third

After :data:`RECOVERY_LIMIT` unsuccessful attempts on one plan without
meaningful progress, the incident escalates and further dispatch is refused.
The count is durable -- it is rows in the ``action`` table -- so restarting the
process does not reset it, and it is keyed by :func:`progress_token`, a
fingerprint of the run's *situation* that omits every wall clock stamp and
every question identity. A retry loop that only ever changes when it ran, or
that provokes the run into asking the same question under a new id, computes
the same token and gets the same refusal. Nor is the count keyed by the
incident, because filing the same trouble under a second incident row is
something the overseer does for itself. What does reset it is the run genuinely
moving: a new turn, a resumption, a pause reason changing, a question appearing
that was not there before.

Durable records this boundary writes: **ledger rows only** -- policy epochs,
decisions, action attempts, receipts and incident escalations. Anything that
touches a plan's state directory or a repository is the adapter's doing, behind
the seam, under a grant this module verified twice.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal, Protocol

from . import (
    intervention,
    overseer_context,
    overseer_decisions,
    overseer_discovery,
    overseer_policy_client,
    overseer_store,
)

__all__ = [
    "ACTION_ACCESS",
    "OPERATIONS",
    "OUTCOMES",
    "PAYLOAD_TYPES",
    "RECOVERY_LIMIT",
    "REFUSALS",
    "Action",
    "Adapter",
    "Commit",
    "Dispatch",
    "DispatchError",
    "Guidance",
    "Identity",
    "Preparation",
    "Receipt",
    "Reconciliation",
    "Registry",
    "Reply",
    "Resume",
    "Verify",
    "dispatch",
    "progress_token",
    "reconcile",
    "registry",
]

Operation = overseer_decisions.Operation

#: The operational scope, imported rather than restated so the operations a
#: decision can authorize and the ones a dispatcher can perform cannot drift
#: apart. Adding one here without an adapter changes nothing; adding an adapter
#: for a name that is not here is refused by :func:`registry`.
OPERATIONS: tuple[Operation, ...] = overseer_decisions.OPERATIONS

#: The access category every dispatch asks under. Never ``context`` and never
#: ``mutation``: those are easier questions, and answering the easy one is how a
#: read-only permission turns into a write.
ACTION_ACCESS = overseer_policy_client.ACCESS_ACTION

#: How many unsuccessful attempts one plan gets in one situation before its
#: incident escalates. Two, per the shared requirements. It is a count of
#: durable rows, not of anything this process remembers.
RECOVERY_LIMIT = 2

Outcome = Literal["executed", "failed", "uncertain"]

#: What an adapter may report. ``uncertain`` is the important one and the reason
#: this is not a boolean: an adapter that cannot tell whether its call landed
#: must be able to say so, because the alternative is guessing, and a wrong
#: guess either loses an action that happened or repeats one that did.
OUTCOMES: tuple[Outcome, ...] = ("executed", "failed", "uncertain")

Refusal = Literal[
    "operation_unsupported",
    "plan_unobserved",
    "run_replaced",
    "scope_expanded",
    "ownership_lost",
    "stale_action",
    "recovery_exhausted",
    "incident_unknown",
    "question_unknown",
    "duplicate_dispatch",
    "policy_unavailable",
    "unauthorized",
    "policy_superseded",
    "outcome_uncertain",
    "preparation_declined",
    "ledger_refused",
]

#: Every reason a dispatch does not happen. A consumer contract: operator
#: surfaces read these, so a spelling change is a contract change.
#:
#: * ``operation_unsupported`` -- the payload is not one of the five typed
#:   payloads, or no adapter is registered for its operation.
#: * ``plan_unobserved`` -- discovery cannot see the plan, or cannot establish
#:   enough identity to ask about it.
#: * ``run_replaced`` -- the plan is running something other than the run this
#:   action names.
#: * ``scope_expanded`` -- the run's declared repositories, or the identity the
#:   authorization was issued against, are not the ones the action carries.
#: * ``ownership_lost`` -- this watcher does not hold a live lease on the plan.
#: * ``stale_action`` -- the run moved since the action was built.
#: * ``recovery_exhausted`` -- :data:`RECOVERY_LIMIT` attempts have already
#:   failed on this plan without the run's situation changing.
#: * ``incident_unknown`` -- no open incident of that identifier on this run.
#: * ``question_unknown`` -- the reply names a question that is not open.
#: * ``duplicate_dispatch`` -- this exact action already executed in this exact
#:   situation.
#: * ``policy_unavailable`` -- AgentMux could not be asked. A fault to report,
#:   never a refusal to attribute to an operator.
#: * ``unauthorized`` -- AgentMux answered, and the answer was no.
#: * ``policy_superseded`` -- the answer changed between the two asks.
#: * ``outcome_uncertain`` -- a dispatch on this plan is in doubt and
#:   reconciliation could not settle it.
#: * ``preparation_declined`` -- the adapter could not make the action ready.
#: * ``ledger_refused`` -- the ledger would not record the attempt; the grant
#:   did not survive the trip to the row.
REFUSALS: tuple[Refusal, ...] = (
    "operation_unsupported",
    "plan_unobserved",
    "run_replaced",
    "scope_expanded",
    "ownership_lost",
    "stale_action",
    "recovery_exhausted",
    "incident_unknown",
    "question_unknown",
    "duplicate_dispatch",
    "policy_unavailable",
    "unauthorized",
    "policy_superseded",
    "outcome_uncertain",
    "preparation_declined",
    "ledger_refused",
)

_PROGRESS_DOMAIN = b"agent_session_loop.overseer_dispatch.progress.v1\x00"
_IDENTITY_DOMAIN = b"agent_session_loop.overseer_dispatch.identity.v1\x00"
_TARGET_DOMAIN = b"agent_session_loop.overseer_dispatch.target.v1\x00"


class DispatchError(Exception):
    """A dispatcher was assembled out of something it cannot dispatch."""


@dataclass(frozen=True)
class Reply:
    """Answer one open question on a run.

    The target is the question, not the text: a question gets one answer, so a
    second reply to the same question in the same situation is a repeat however
    differently it is worded.
    """

    question_id: str
    text: str

    operation: ClassVar[Operation] = "reply"

    def target(self) -> str:
        """What this action is against, for duplicate detection."""
        return self.question_id


@dataclass(frozen=True)
class Guidance:
    """Send one non-blocking notice to a run.

    Guidance has no identity of its own, so the text is the target. Sending the
    same words twice in the same situation is a repeat; sending different words
    is a different action.
    """

    text: str

    operation: ClassVar[Operation] = "guidance"

    def target(self) -> str:
        """What this action is against, for duplicate detection."""
        return _digest(_TARGET_DOMAIN, "guidance", self.text)


@dataclass(frozen=True)
class Verify:
    """Run one check the plan's verification contract already defines.

    ``check`` names a check; it is not a command and cannot become one. The
    argv is whatever the plan declared for that name, read by the adapter from
    the contract -- so a caller who wants a different command has to change the
    plan, in a repository, under the write rules that govern repositories.
    """

    check: str

    operation: ClassVar[Operation] = "verify"

    def target(self) -> str:
        """What this action is against, for duplicate detection."""
        return self.check


@dataclass(frozen=True)
class Commit:
    """Commit the work in progress as a checkpoint."""

    message: str

    operation: ClassVar[Operation] = "commit"

    def target(self) -> str:
        """What this action is against, for duplicate detection."""
        return _digest(_TARGET_DOMAIN, "commit", self.message)


@dataclass(frozen=True)
class Resume:
    """Resume a paused run.

    An empty ``session_id`` means whatever the run would resume into on its
    own, which is the ordinary case: naming one is how a caller says it means
    that session specifically and no other.
    """

    session_id: str = ""

    operation: ClassVar[Operation] = "resume"

    def target(self) -> str:
        """What this action is against, for duplicate detection."""
        return self.session_id or "next"


Payload = Reply | Guidance | Verify | Commit | Resume

#: The five payload types, as an ``isinstance`` argument. The union above is the
#: contract; this is how a caller that ignored it is caught at runtime.
PAYLOAD_TYPES: tuple[type[Payload], ...] = (Reply, Guidance, Verify, Commit, Resume)


@dataclass(frozen=True)
class Action:
    """One operation, against one incident, on one run, at one moment.

    ``token`` is :func:`agent_session_loop.intervention.action_token` for the
    facts the caller built this from, and ``scope`` is
    :func:`agent_session_loop.overseer_context.scope_digest` for the candidate
    it was authorized against. Both are proofs about a moment that has passed
    by the time :func:`dispatch` reads them, which is exactly what makes them
    worth carrying: they are how the dispatcher finds out the moment passed.
    """

    plan_dir: str
    run_id: str
    incident_id: int
    payload: Payload
    token: str
    scope: str
    repositories: tuple[overseer_policy_client.Repository, ...]

    @property
    def operation(self) -> Operation:
        """The operation this action's payload is."""
        return self.payload.operation

    @property
    def target(self) -> str:
        """What the action is against, as the ledger records it."""
        return self.payload.target()


@dataclass(frozen=True)
class Preparation:
    """Whether an adapter got an action ready to go."""

    ready: bool
    detail: str = ""

    @classmethod
    def ok(cls) -> Preparation:
        """Prepared, with nothing to report."""
        return cls(ready=True)

    @classmethod
    def declined(cls, detail: str) -> Preparation:
        """Not prepared, and why."""
        return cls(ready=False, detail=detail)


@dataclass(frozen=True)
class Receipt:
    """What an adapter says became of a call.

    ``locator`` is where the evidence for that claim lives -- a commit sha, a
    message id, a log path -- so a receipt is checkable rather than merely
    assertive.
    """

    outcome: Outcome
    detail: str = ""
    locator: str = ""

    @classmethod
    def executed(cls, detail: str = "", locator: str = "") -> Receipt:
        """It happened."""
        return cls(outcome="executed", detail=detail, locator=locator)

    @classmethod
    def failed(cls, detail: str = "", locator: str = "") -> Receipt:
        """It did not happen, and the adapter knows that."""
        return cls(outcome="failed", detail=detail, locator=locator)

    @classmethod
    def uncertain(cls, detail: str = "", locator: str = "") -> Receipt:
        """The adapter cannot tell. Never resolved here by guessing."""
        return cls(outcome="uncertain", detail=detail, locator=locator)


class Adapter(Protocol):
    """One operation's implementation, behind the guards.

    An adapter is called only with an action the dispatcher has already
    authorized twice, and it is called through this interface only: there is no
    other entry point, and nothing it is handed carries a command.

    :meth:`reconcile` takes a ledger row rather than an
    :class:`Action` because the case it exists for is a row written by a
    process that is gone. Only what the ledger persisted survives, so only what
    the ledger persisted may be needed to answer "did this happen?".
    """

    def prepare(self, action: Action) -> Preparation:
        """Get ready to perform the action. May wait; must not perform it."""
        ...

    def execute(self, action: Action) -> Receipt:
        """Perform the action, and say what became of it."""
        ...

    def reconcile(self, record: overseer_store.ActionRecord) -> Receipt:
        """Say what became of a dispatch that was left in doubt."""
        ...


@dataclass(frozen=True)
class Registry:
    """The closed set of operations this dispatcher can perform."""

    adapters: Mapping[Operation, Adapter]

    def supports(self, operation: str) -> bool:
        """Whether an adapter is registered for an operation."""
        return self.adapter(operation) is not None

    def adapter(self, operation: str) -> Adapter | None:
        """The adapter for an operation, or ``None`` if there is none.

        Takes a plain string and walks the entries rather than indexing,
        because the caller's operation is whatever arrived on an action and the
        point of this method is to find out whether it is one of the five.
        """
        for known, adapter in self.adapters.items():
            if known == operation:
                return adapter
        return None


def registry(adapters: Mapping[str, Adapter]) -> Registry:
    """Build a registry, refusing any operation outside :data:`OPERATIONS`.

    Refused at construction rather than at dispatch on purpose. A registry that
    accepts an arbitrary key and rejects it later is a registry that can be
    built holding an operation nobody authorized, and the window between
    building it and using it is the whole problem.
    """
    unsupported = sorted(key for key in adapters if key not in OPERATIONS)
    if unsupported:
        raise DispatchError(
            f"{', '.join(repr(key) for key in unsupported)} is not an overseer "
            f"operation; the operational scope is "
            f"{', '.join(OPERATIONS)}"
        )
    known: dict[Operation, Adapter] = {
        operation: adapters[operation]
        for operation in OPERATIONS
        if operation in adapters
    }
    return Registry(adapters=MappingProxyType(known))


@dataclass(frozen=True)
class Identity:
    """Everything about a run that a dispatch is entitled to assume.

    Rebuilt from scratch before and after preparation, and the two are compared
    by :meth:`fingerprint` rather than field by field: a comparison that
    enumerates fields is a comparison that silently stops covering the next one
    somebody adds.
    """

    plan_dir: str
    run_id: str
    scope: str
    token: str
    progress: str
    repositories: tuple[overseer_policy_client.Repository, ...]
    question_id: str | None

    def fingerprint(self) -> str:
        """One value covering the whole identity."""
        parts = [
            self.plan_dir,
            self.run_id,
            self.scope,
            self.token,
            self.progress,
            self.question_id or "",
        ]
        for repository in self.repositories:
            parts.append(repository.alias)
            parts.append(repository.path)
        return _digest(_IDENTITY_DOMAIN, *parts)


@dataclass(frozen=True)
class Dispatch:
    """What came of one attempt to perform one action.

    ``performed`` and ``refusal`` are not opposites. A refusal means nothing was
    dispatched; ``performed`` false with no refusal means the action *was*
    dispatched and the adapter reported that it failed, which is a fact about
    the world rather than a decision about permission.
    """

    action: Action
    performed: bool
    refusal: Refusal | None
    detail: str
    record: overseer_store.ActionRecord | None
    receipt: Receipt | None
    escalated: bool
    recoveries: int


@dataclass(frozen=True)
class Reconciliation:
    """What one in-doubt dispatch turned out to be."""

    record: overseer_store.ActionRecord
    receipt: Receipt | None
    settled: bool
    detail: str


#: ``overseer_policy_client.authorize``,
#: ``overseer_discovery.build_inventory``, ``intervention.inspect_run`` and the
#: clock, as parameters. Injected so the guards are provable without an ``amx``
#: on PATH and without waiting real seconds for a lease to expire.
Authorize = Callable[..., overseer_policy_client.Decision]
BuildInventory = Callable[..., overseer_discovery.Inventory]
InspectRun = Callable[..., intervention.RunFacts]
Now = Callable[[], float]


def progress_token(facts: intervention.RunFacts) -> str:
    """Fingerprint the run's *situation*, blind to time and to question ids.

    This is what makes "two recoveries without meaningful progress" a
    well-defined limit rather than a suggestion. It deliberately omits every
    timestamp -- ``run_started_at``, ``last_activity_at``, ``idle_seconds`` and
    the rest -- so waiting does not count as progress, and it omits question
    identifiers, keeping only how *many* are open, so a run that regenerates
    the same question under a new id does not count either. Both omissions are
    the requirement, stated the only way a caller cannot work around: the value
    it would have to change is not one it can reach.

    What it keeps is the run actually moving. A turn starting or ending, the
    turn index advancing, a resumption, a pause reason changing, a question
    appearing where there was none -- each changes the token, and each resets
    the count as a side effect of being real progress rather than by anyone
    deciding it should.

    Distinct from :func:`agent_session_loop.intervention.action_token`, which
    answers the other question. That one covers the open question ids precisely
    because it is a staleness proof: a caller holding a picture of a question
    that has been replaced is holding a stale picture. This one covers the same
    run in the same trouble and must not notice the replacement.
    """
    turn = facts.runner_state.get("active_turn")
    turn = turn if isinstance(turn, Mapping) else {}
    return _digest(
        _PROGRESS_DOMAIN,
        str(facts.plan_dir),
        facts.run_id or "",
        "1" if facts.run_active else "0",
        "1" if facts.paused else "0",
        "1" if facts.turn_active else "0",
        facts.context_kind or "",
        _text(facts.runner_state.get("reason")),
        "1" if facts.runner_state.get("run_finished_at") else "0",
        _text(turn.get("session_id")),
        _text(turn.get("turn_index")),
        str(len(facts.unanswered_ids)),
    )


def dispatch(
    *,
    action: Action,
    watcher_id: str,
    ledger: overseer_store.Ledger,
    adapters: Registry,
    authorize: Authorize = overseer_policy_client.authorize,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    inspect_run: InspectRun = intervention.inspect_run,
    state_dir: str | Path | None = None,
    timeout: float = overseer_policy_client.DEFAULT_TIMEOUT_SECONDS,
    now: Now = time.time,
) -> Dispatch:
    """Perform one action, or refuse it and say which guard said no.

    The order of the guards is the order of the questions, and the pivot in the
    middle is AgentMux. First, from identity metadata alone: what is this, is
    the plan there, is it the same run, is it the same scope, is it ours. Then
    AgentMux is asked about that scope. Only under its answer is the run itself
    opened -- is our picture current, have we already burned our recoveries, is
    the incident real, is the question open, have we already done this -- and
    only then is anything in doubt reconciled and an adapter allowed to prepare.
    Then the metadata and the authorization are established a second time and
    required to match *before* the content is read again, because everything
    above was true before a wait.

    Nothing here rolls anything back. An action that executed stays executed
    and an action left in doubt stays in doubt until an adapter says otherwise;
    see :func:`reconcile`.
    """
    if not isinstance(action.payload, PAYLOAD_TYPES):
        return _refusal(
            action,
            "operation_unsupported",
            f"{type(action.payload).__name__} is not an overseer action payload; "
            f"an action carries one of {', '.join(t.__name__ for t in PAYLOAD_TYPES)}",
        )
    adapter = adapters.adapter(action.operation)
    if adapter is None:
        return _refusal(
            action,
            "operation_unsupported",
            f"no adapter is registered for {action.operation!r}",
        )

    located = _locate(
        action=action,
        watcher_id=watcher_id,
        ledger=ledger,
        build_inventory=build_inventory,
        state_dir=state_dir,
        now=now,
    )
    if isinstance(located, _Refused):
        return _settle_refusal(action, located, ledger)

    grant = _permit(
        action=action,
        repositories=located.repositories,
        ledger=ledger,
        authorize=authorize,
        timeout=timeout,
    )
    if isinstance(grant, _Refused):
        return _settle_refusal(action, grant, ledger)

    identity = _examine(
        action=action,
        located=located,
        ledger=ledger,
        inspect_run=inspect_run,
        state_dir=state_dir,
    )
    if isinstance(identity, _Refused):
        return _settle_refusal(action, identity, ledger)

    outstanding = reconcile(
        plan_dir=action.plan_dir, ledger=ledger, adapters=adapters, grant=grant
    )
    unsettled = tuple(item for item in outstanding if not item.settled)
    if unsettled:
        return _refusal(
            action,
            "outcome_uncertain",
            f"{len(unsettled)} dispatch(es) on this plan are in doubt and could "
            f"not be reconciled: {'; '.join(item.detail for item in unsettled)}",
        )

    preparation = adapter.prepare(action)
    if not preparation.ready:
        return _refusal(action, "preparation_declined", preparation.detail)

    relocated = _locate(
        action=action,
        watcher_id=watcher_id,
        ledger=ledger,
        build_inventory=build_inventory,
        state_dir=state_dir,
        now=now,
    )
    if isinstance(relocated, _Refused):
        return _settle_refusal(action, relocated, ledger)

    regrant = _permit(
        action=action,
        repositories=relocated.repositories,
        ledger=ledger,
        authorize=authorize,
        timeout=timeout,
    )
    if isinstance(regrant, _Refused):
        return _settle_refusal(action, regrant, ledger)
    if (
        regrant.request_digest != grant.request_digest
        or regrant.config_fingerprint != grant.config_fingerprint
    ):
        return _refusal(
            action,
            "policy_superseded",
            "authorization changed while the action was being prepared",
        )

    rechecked = _examine(
        action=action,
        located=relocated,
        ledger=ledger,
        inspect_run=inspect_run,
        state_dir=state_dir,
    )
    if isinstance(rechecked, _Refused):
        return _settle_refusal(action, rechecked, ledger)
    if rechecked.fingerprint() != identity.fingerprint():
        return _refusal(
            action,
            "stale_action",
            "the run changed while the action was being prepared",
        )

    return _perform(
        action=action,
        identity=rechecked,
        grant=regrant,
        adapter=adapter,
        ledger=ledger,
    )


def reconcile(
    *,
    plan_dir: str,
    ledger: overseer_store.Ledger,
    adapters: Registry,
    grant: overseer_policy_client.Grant,
) -> tuple[Reconciliation, ...]:
    """Ask the adapters what became of every dispatch left in doubt.

    Called before any retry, and safe to call on its own: it settles rows from
    receipts an adapter can produce now, and leaves alone every row it cannot
    settle. An in-doubt row is never rolled forward into ``executed`` on the
    grounds that it probably worked, and never rolled back on the grounds that
    it probably did not. Both are guesses, and the shared requirements say an
    already-dispatched action is not undone.

    ``grant`` is required and is checked, not merely accepted. Answering "did
    this happen?" means opening run state, mailboxes and Git history, so
    reconciliation is content access however little it writes -- and a caller
    that has not just asked AgentMux about this plan under
    :data:`ACTION_ACCESS` has nothing to pass. That is why it is a
    :class:`~agent_session_loop.overseer_policy_client.Grant` and not a
    boolean: a grant is only ever built from a live allowed answer, so it
    cannot be assembled by a caller who would like to proceed.
    """
    if grant.access != ACTION_ACCESS:
        raise DispatchError(
            f"reconciliation runs under {ACTION_ACCESS!r} permission; this grant "
            f"is for {grant.access!r}"
        )
    if grant.plan_dir != plan_dir:
        raise DispatchError(f"this grant permits {grant.plan_dir!r}, not {plan_dir!r}")
    results: list[Reconciliation] = []
    for record in ledger.in_doubt_actions(plan_dir=plan_dir):
        adapter = adapters.adapter(record.operation)
        if adapter is None:
            results.append(
                Reconciliation(
                    record=record,
                    receipt=None,
                    settled=False,
                    detail=(
                        f"action {record.action_id} was attempted as "
                        f"{record.operation!r}, which no registered adapter can "
                        "account for"
                    ),
                )
            )
            continue
        try:
            receipt = adapter.reconcile(record)
        except Exception as error:  # noqa: BLE001 - see below
            # An adapter that raises has said nothing about the world. Reporting
            # that as a failure would be inventing a receipt out of a stack
            # trace, so the row stays in doubt and the plan stays blocked.
            results.append(
                Reconciliation(
                    record=record,
                    receipt=None,
                    settled=False,
                    detail=(
                        f"reconciling action {record.action_id} raised "
                        f"{type(error).__name__}: {error}"
                    ),
                )
            )
            continue
        if receipt.outcome == "uncertain":
            results.append(
                Reconciliation(
                    record=record,
                    receipt=receipt,
                    settled=False,
                    detail=receipt.detail
                    or f"action {record.action_id} remains in doubt",
                )
            )
            continue
        settled = ledger.confirm_action(
            action_id=record.action_id,
            outcome=receipt.outcome,
            detail=receipt.detail,
            locator=receipt.locator,
            status="executed" if receipt.outcome == "executed" else "failed",
        )
        results.append(
            Reconciliation(
                record=settled,
                receipt=receipt,
                settled=True,
                detail=receipt.detail,
            )
        )
    return tuple(results)


@dataclass(frozen=True)
class _Refused:
    """One guard's answer, before it becomes a :class:`Dispatch`."""

    refusal: Refusal
    detail: str
    escalate: bool = False
    recoveries: int = 0


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
    ledger: overseer_store.Ledger,
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
    ledger: overseer_store.Ledger,
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
    ledger: overseer_store.Ledger,
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


def _perform(
    *,
    action: Action,
    identity: Identity,
    grant: overseer_policy_client.Grant,
    adapter: Adapter,
    ledger: overseer_store.Ledger,
) -> Dispatch:
    """Write the attempt, call the adapter, settle what came back."""
    try:
        record = ledger.attempt_action(
            grant=grant,
            plan_dir=action.plan_dir,
            operation=action.operation,
            target=action.target,
            incident_id=action.incident_id,
            progress_token=identity.progress,
        )
    except overseer_store.LedgerRefused as error:
        return _refusal(action, "ledger_refused", str(error))

    try:
        receipt = adapter.execute(action)
    except Exception as error:  # noqa: BLE001 - see below
        # The call did not return. That is not the same as the action not
        # happening, so the row stays ``attempted`` and the plan stays blocked
        # until an adapter can say which it was.
        return Dispatch(
            action=action,
            performed=False,
            refusal="outcome_uncertain",
            detail=f"dispatch raised {type(error).__name__}: {error}",
            record=record,
            receipt=None,
            escalated=False,
            recoveries=0,
        )

    if receipt.outcome == "uncertain":
        return Dispatch(
            action=action,
            performed=False,
            refusal="outcome_uncertain",
            detail=receipt.detail or "the adapter could not confirm the dispatch",
            record=record,
            receipt=receipt,
            escalated=False,
            recoveries=0,
        )

    settled = ledger.confirm_action(
        action_id=record.action_id,
        outcome=receipt.outcome,
        detail=receipt.detail,
        locator=receipt.locator,
        status="executed" if receipt.outcome == "executed" else "failed",
    )
    if receipt.outcome == "executed":
        return Dispatch(
            action=action,
            performed=True,
            refusal=None,
            detail=receipt.detail,
            record=settled,
            receipt=receipt,
            escalated=False,
            recoveries=0,
        )

    spent = len(
        ledger.recovery_attempts(
            plan_dir=action.plan_dir, progress_token=identity.progress
        )
    )
    escalated = (
        _escalate(ledger, action.incident_id) if spent >= RECOVERY_LIMIT else False
    )
    return Dispatch(
        action=action,
        performed=False,
        refusal=None,
        detail=receipt.detail or "the adapter reported the dispatch failed",
        record=settled,
        receipt=receipt,
        escalated=escalated,
        recoveries=spent,
    )


def _settle_refusal(
    action: Action, refused: _Refused, ledger: overseer_store.Ledger
) -> Dispatch:
    """Turn a guard's answer into a result, escalating if it asked for that."""
    escalated = _escalate(ledger, action.incident_id) if refused.escalate else False
    return Dispatch(
        action=action,
        performed=False,
        refusal=refused.refusal,
        detail=refused.detail,
        record=None,
        receipt=None,
        escalated=escalated,
        recoveries=refused.recoveries,
    )


def _refusal(action: Action, refusal: Refusal, detail: str) -> Dispatch:
    """A refusal that escalates nothing and recorded nothing."""
    return Dispatch(
        action=action,
        performed=False,
        refusal=refusal,
        detail=detail,
        record=None,
        receipt=None,
        escalated=False,
        recoveries=0,
    )


def _escalate(ledger: overseer_store.Ledger, incident_id: int) -> bool:
    """Escalate an open incident, and say whether this call is what did it.

    An incident that is already settled is left alone: escalating it again
    would restamp a settlement somebody else made, and the answer to "is this
    escalated?" is the same either way.
    """
    incident = ledger.incident(incident_id)
    if incident is None or incident.status != "open":
        return False
    ledger.settle_incident(incident_id=incident_id, status="escalated")
    return True


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


def _digest(domain: bytes, *parts: str) -> str:
    """Length-prefixed, domain-separated digest over an ordered field list."""
    digest = hashlib.sha256(domain)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"
