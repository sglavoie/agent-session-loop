"""Typed action, identity, and receipt models for overseer dispatch."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Literal, Protocol

from . import (
    intervention,
    overseer_decisions,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_models,
)

Operation = overseer_decisions.Operation
OPERATIONS: tuple[Operation, ...] = overseer_decisions.OPERATIONS
ACTION_ACCESS = overseer_policy_client.ACCESS_ACTION
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

    def reconcile(self, record: overseer_store_models.ActionRecord) -> Receipt:
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
    record: overseer_store_models.ActionRecord | None
    receipt: Receipt | None
    escalated: bool
    recoveries: int


@dataclass(frozen=True)
class Reconciliation:
    """What one in-doubt dispatch turned out to be."""

    record: overseer_store_models.ActionRecord
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


@dataclass(frozen=True)
class _Refused:
    """One guard's answer, before it becomes a :class:`Dispatch`."""

    refusal: Refusal
    detail: str
    escalate: bool = False
    recoveries: int = 0


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
