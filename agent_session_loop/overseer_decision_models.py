"""Overseer decision models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import (
    overseer_decision_vocab as vocab,
)


@dataclass(frozen=True)
class Scope:
    """What a decision is about, and therefore what it cannot reach.

    A project is a *repository*: the root AgentMux authorized a conversation
    under, and the root a plan declares in its registry. It is never the
    workspace directory several repositories sit under. AgentMux admits a
    conversation only when the project governing its working directory permits
    it, so a workspace path is an identity no evidence can carry and a request
    naming one could be matched by nothing -- a repository approval would read
    as an approval of somewhere else.

    ``project`` names one and ``projects`` names the several a plan spans when
    it participates in more than one repository. They are two spellings of one
    thing rather than two fields: a scope naming exactly one project always
    carries it in ``project``, a scope naming several always carries them
    sorted in ``projects`` with ``project`` empty, and both are normalized on
    construction so one project set has one representation and two scopes about
    the same thing compare equal.

    An empty field is *not narrowed* rather than *any*: on the deciding side it
    means the decision was not made about one plan, run or incident in
    particular, and on the target side it means the caller did not say -- which
    refuses, since a decision narrowed to something the target does not name
    cannot be shown to apply to it.
    """

    project: str = ""
    plan_dir: str = ""
    run_id: str = ""
    incident: str = ""
    projects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        named = tuple(
            dict.fromkeys(item for item in (self.project, *self.projects) if item)
        )
        single = named[0] if len(named) == 1 else ""
        object.__setattr__(self, "project", single)
        object.__setattr__(self, "projects", () if single else tuple(sorted(named)))

    @property
    def named_projects(self) -> tuple[str, ...]:
        """Every project this scope names, which may be one or several."""
        return (self.project,) if self.project else self.projects

    def covers(self, target: Scope) -> bool:
        """Whether authority given in this scope applies to ``target``.

        Every project the target names has to be one this scope names. An
        allowance made in one repository therefore does not reach a plan that
        also participates in another: authority is held per project, and a plan
        spanning two of them needs human evidence covering both.

        This is the allowing half of applicability. A hold constrains through
        :meth:`bears_on` instead, because a hold is not authority and refusing
        to apply it to a wider target would let a union scope walk out from
        under it.
        """
        mine = set(self.named_projects)
        theirs = set(target.named_projects)
        if not mine or not theirs:
            return False
        if not theirs <= mine:
            return False
        return self.narrows(target)

    def bears_on(self, target: Scope) -> bool:
        """Whether something said in this scope reaches ``target`` at all.

        Weaker than :meth:`covers` on the project alone: sharing one project is
        enough. A hold typed in one of a plan's repositories is evidence about
        that plan even though it cannot by itself authorize work spanning the
        others, and evidence that could not be seen is evidence that cannot
        make a reading stale.

        It is also what a *hold* is applied through, in :func:`settle`,
        :func:`dependencies` and :func:`resolve`: a decision to stop work in one
        repository still stops the work that includes it when the action spans
        others as well. Disjoint projects are still disjoint, so a hold
        somewhere nobody named constrains nothing.
        """
        mine = set(self.named_projects)
        theirs = set(target.named_projects)
        if not mine or not theirs:
            return False
        if mine.isdisjoint(theirs):
            return False
        return self.narrows(target)

    def narrows(self, target: Scope) -> bool:
        """Whether this scope's plan, run and incident reach ``target``'s.

        The project half of :meth:`covers`, separated because a claim may rest
        on more than one statement and each of them has to reach the plan, run
        and incident being decided even when only one of them names the project
        that is short.
        """
        narrowings = (
            (self.plan_dir, target.plan_dir),
            (self.run_id, target.run_id),
            (self.incident, target.incident),
        )
        return not any(narrowed and narrowed != named for narrowed, named in narrowings)

    def within(self, other: Scope) -> bool:
        """Whether this scope is ``other`` or a narrowing of it."""
        return other.covers(self)


@dataclass(frozen=True)
class Statement:
    """One thing somebody said, with the provenance that makes it usable.

    ``moment`` is the parsed form of ``at`` and is ``None`` when the record
    carried no readable timestamp. It is kept next to the raw string rather than
    replacing it, because the string is what the source said and the moment is
    what this module can order.
    """

    statement_id: str
    voice: vocab.Voice
    space: str
    locator: str
    scope: Scope
    at: str
    text: str
    truncated: bool = False
    marker: str = ""
    moment: datetime | None = None

    @property
    def authorizing(self) -> bool:
        """Whether a decision may rest on this statement's authorship."""
        return self.voice in vocab.AUTHORIZING_VOICES

    @property
    def order(self) -> tuple[datetime, str]:
        """The total order supersession uses. Undated statements have none."""
        if self.moment is None:
            raise ValueError(f"{self.statement_id} carries no readable moment")
        return (self.moment, self.statement_id)


@dataclass(frozen=True)
class Claim:
    """What the host agent reads one statement as deciding.

    ``reading`` is the agent's own words and is carried through to the decision
    as an interpretation, kept apart from the statement's text so a record never
    presents a paraphrase as the instruction.
    """

    statement_id: str
    topic: vocab.Topic
    stance: vocab.Stance
    scope: Scope
    operations: tuple[vocab.Operation, ...] = ()
    corroborating: tuple[str, ...] = ()
    reading: str = ""


@dataclass(frozen=True)
class Decision:
    """One accepted claim: a user decision, its scope, and what it rests on."""

    decision_id: str
    statement_id: str
    topic: vocab.Topic
    stance: vocab.Stance
    scope: Scope
    operations: tuple[vocab.Operation, ...]
    at: str
    moment: datetime
    sources: tuple[str, ...]
    text: str
    reading: str = ""

    @property
    def order(self) -> tuple[datetime, str]:
        return (self.moment, self.statement_id)

    def speaks_to(self, operation: str) -> bool:
        """Whether this decision says anything about one operation.

        A hold naming no operation speaks to all of them; an allowance has to
        name what it allows, which :data:`operations_unnamed <CLAIM_REJECTIONS>`
        enforces at registration.
        """
        if self.topic != "operations":
            return False
        if operation in self.operations:
            return True
        return self.stance == "hold" and not self.operations


@dataclass(frozen=True)
class Rejection:
    """One claim that did not become a decision, and why."""

    claim: Claim
    reason: vocab.RejectionReason
    detail: str


@dataclass(frozen=True)
class Register:
    """The decisions one body of evidence supports, and the claims it refused."""

    statements: tuple[Statement, ...]
    decisions: tuple[Decision, ...]
    rejections: tuple[Rejection, ...]

    def statement(self, statement_id: str) -> Statement | None:
        for item in self.statements:
            if item.statement_id == statement_id:
                return item
        return None

    def decision(self, decision_id: str) -> Decision | None:
        for item in self.decisions:
            if item.decision_id == decision_id:
                return item
        return None

    def rejected(self, statement_id: str) -> tuple[Rejection, ...]:
        return tuple(
            item for item in self.rejections if item.claim.statement_id == statement_id
        )


@dataclass(frozen=True)
class Settlement:
    """Which decision on one topic is in force for one scope, and which are not.

    ``settled`` is deliberately narrower than "a decision exists": a governing
    decision whose stance is ``hold`` is acceptance withheld, scope not agreed,
    a design or safety question answered *no*. Reporting that as settled would
    turn a refusal into permission.
    """

    topic: vocab.Topic
    scope: Scope
    decision: Decision | None
    superseded: tuple[Decision, ...] = ()
    precedents: tuple[Decision, ...] = ()

    @property
    def settled(self) -> bool:
        return self.decision is not None and self.decision.stance == "allow"

    @property
    def withheld(self) -> bool:
        return self.decision is not None and self.decision.stance == "hold"


@dataclass(frozen=True)
class Request:
    """One operation an overseer is considering, and what it turns on.

    ``raises`` names the human topics this incident depends on -- the acceptance
    of a verification result, the scope of a repair, a design or safety call.
    Naming one that nobody has settled is what makes the ruling
    ``human_decision`` even when the operation itself was authorized.

    ``reviewed`` is the :func:`evidence_digest` of the record the claims were
    read from. Empty means the caller is not binding its reading to a record,
    which is the default so that every existing request keeps its meaning;
    naming one asks for :data:`evidence_changed <REFUSALS>` if human evidence in
    scope has moved since. A caller that *presented* the record to whoever wrote
    the claims owes the binding rather than merely offering it, and enforces
    that where the presentation happened: see
    :data:`evidence_unbound <REFUSALS>`.
    """

    operation: str
    scope: Scope
    raises: tuple[vocab.Topic, ...] = ()
    reviewed: str = ""


@dataclass(frozen=True)
class Ruling:
    """Whether one operation is authorized, on whose decision, and against what.

    ``precedents`` is the answer to "we have done this before": decisions that
    speak to the operation and were made about something else. They are
    reported because a pattern is worth showing an operator and never acted on,
    which is why ``authorized`` is false whenever they are all there is.
    """

    operation: str
    scope: Scope
    authorized: bool
    refusal: vocab.Refusal | None
    decision: Decision | None = None
    superseded: tuple[Decision, ...] = ()
    precedents: tuple[Decision, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    summary: str = ""

    @property
    def unresolved(self) -> tuple[vocab.Topic, ...]:
        """Every raised human topic no in-scope decision settles."""
        return tuple(item.topic for item in self.settlements if not item.settled)

    @property
    def sources(self) -> tuple[str, ...]:
        """Every statement the governing decision rests on."""
        return () if self.decision is None else self.decision.sources
