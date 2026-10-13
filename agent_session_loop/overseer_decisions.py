"""Who decided what, over which scope, and which decision is still in force.

The watcher finds a blocked run and :mod:`overseer_context` and
:mod:`overseer_history` say what is known about it. Neither says whether the
overseer may act, because that turns on a different question: did a human
decide this, for this, recently enough that a later decision has not replaced
it. This module is the deterministic half of that answer. The judgment is the
host agent's -- it reads the evidence and states what it thinks a message
decided -- and everything here is a check on that statement rather than a
second reading of it.

## Four voices, and only one of them authorizes

A blocked run's record is full of imperative sentences and almost none of them
are a human's. ASL composes task packets, guidance envelopes, reply and
resolution context, and audit prompts, and every one of them reaches a harness
transcript under the ``user`` role, indistinguishable by role alone from
something an operator typed. So role alone is not the classifier:
:data:`GENERATED_PROMPT_MARKERS` names the openings ASL itself publishes, and a
``user``-role message carrying one is a :data:`generated_prompt
<VOICES>` whatever it says. The other three are the ones an unqualified "the
user said so" would blur: an assistant restating an instruction is an
``assistant_interpretation``, a worker asking for something is a
``worker_request``, and a mailbox guidance or reply is a
``relayed_instruction`` -- ASL records no author for those, so a human's own
words and a supervising agent's paraphrase are the same bytes and neither can
be proven to be the former.

Only ``user_instruction`` authorizes, and a claim resting on anything else is
rejected rather than downgraded. That is deliberately strict: the relay is
where an instruction *arrives*, and the transcript is the only record that says
who *spoke*.

## Whose conversation it was, before whose turn it is

A marker catches the prompts ASL itself composes, and it catches nothing an
orchestrating agent typed in its own words. AgentMux answers that separately:
every admitted conversation carries a :data:`session class <SESSION_CLASSES>`
-- ``main`` for a session a person started, ``automated`` for programmatic
``exec`` runs, ASL workers, subagents and sidechains, ``unknown`` when the
harness could not be asked -- and the classification is the first check, ahead
of the role and ahead of the marker.

It is read fail-closed, so authorship has to be *established* rather than
merely not disproved: only ``main`` yields human turns. ``automated`` yields
generated prompts, and ``unknown``, an absent class from an older answer, and
any spelling this build does not know all yield ``unattributed`` -- a
conversation nobody can attribute cannot supply the one voice that authorizes.
:data:`generated_classes <statements_from_history>` remains available and can
only narrow this further: it can mark a class machine-authored, and there is no
value it can carry that turns a class into a human one.

## Scope is what stops an approval from travelling

Every statement carries the scope it was made in -- a transcript message the
project its conversation was authorized under, a mailbox record the plan and
run it belongs to -- and a claim may narrow that scope but never widen it. An
approval given in one project is therefore unrepresentable as permission in
another, not because a rule forbids the transfer but because the claim that
would perform it cannot be built: :data:`scope_widened <CLAIM_REJECTIONS>` is
the rejection, and it is checked against the statement's own scope rather than
against anything the claimant asserts.

A project is a repository, never the workspace directory a plan's repositories
sit under. AgentMux admits a conversation under the project governing its
working directory, so a workspace path is an identity no evidence can carry: a
request naming one would be matched by nothing and every repository approval
would read as an approval of somewhere else. A plan participating in several
repositories names all of them, and a decision reaches it only by naming all of
them too -- which one statement cannot do, so such a claim has to rest on a
human statement in each project and cite them together. That is what keeps one
project's authority from becoming another's.

That rule is about *authority*, and a hold is not authority. An allowance has
to name every project a target names, or one repository's permission would be
spent on work in another; a hold has only to name one of them, because adding a
second repository to an action cannot make an existing hold stop reaching the
work that still includes the first. Applicability is therefore read from the
stance: :meth:`Scope.covers` for an allowance, :meth:`Scope.bears_on` for a
hold, in :func:`settle`, :func:`dependencies` and :func:`resolve` alike. A hold
in a project the target does not name stays a precedent, so repositories nobody
mentioned remain independent.

The same rule separates incidents. A decision narrowed to one incident does not
cover the next one, and a decision that names no incident covers the plan it
was made about and no other. What an out-of-scope decision *does* do is show up
in a :class:`Ruling` as a precedent: history may suggest that a recovery is the
right one, and the ruling says so, while ``authorized`` stays false and the
refusal is :data:`unsupported_recovery <REFUSALS>`. A pattern is a reason to
look, never a reason to act.

## Later replaces earlier, within scope

Applicable decisions on one topic are ordered by the moment their statement was
made -- ties broken by statement identity, so the order is total and stable --
and the last one governs. Every other applicable decision is reported as
``superseded`` rather than dropped, because "the user changed their mind" and
"the user never said" are different situations and an overseer that cannot tell
them apart will eventually re-apply a decision that was revoked. A statement
whose timestamp cannot be read is refused outright: without a moment there is no
order, and without an order supersession is a guess.

A hold is ordered against the repository it was typed in, and so is the
allowance that lifts it. Each participating repository has its own chain -- the
applicable decisions naming it, in order -- and the repository is stopped when
the last decision in that chain is a hold. The target is refused while any of
its repositories is stopped, and only a decision naming a repository can start
it again: an allowance covering the checkout beside it lifts nothing. Once
nothing is stopped, authority is read as it always was -- the last applicable
allowance :meth:`covering <Scope.covers>` every repository the target names --
so a release restores the authority that was already established rather than
creating one. That is what keeps a release proportionate to its hold: the person
who stopped one checkout can start it again without the checkout beside it being
asked to authorize unchanged work a second time, and a repository nobody
released stays stopped however many others were.

## A reading is bound to the evidence it was read from

Supersession orders the statements a caller *claimed*, and a claim can only
cite a statement the caller has seen. That leaves one gap the ordering cannot
close: a statement nobody claimed, because it did not exist when the host read
the evidence. A hold typed while an action was being prepared is exactly that
shape -- the claims still cite the statements they always cited, every one of
them is still in force, and the allowance they carry is still the last word
among them, so a re-check that only re-resolves the same claims agrees with
itself no matter what arrived in between.

:func:`evidence_digest` closes it by summarizing the human evidence in scope --
every :data:`REVIEWED_VOICES` statement covering the target, ordered and hashed
with its identity, moment, text and scope -- so that a reading can carry the
state of the record it was made against. A :class:`Request` may name that
digest in ``reviewed``, and :func:`resolve` refuses
:data:`evidence_changed <REFUSALS>` when the evidence in hand no longer hashes
to it. The refusal is not a verdict on the new statement: it says the reading
is about a record that has moved, and the answer is another reading rather than
a retry. It therefore names the record the caller read and never the record it
moved to, because the second name is precisely what these claims would need to
pass and an answer that supplied it would be refusing and authorizing at once.
``reviewed`` empty means the caller is not making the binding, which is what
keeps this a check a caller offers rather than a second place a decision could
be made.

Who is *owed* a binding is a different question, and it is not answerable here.
This module is handed claims and never sees where they came from, so it cannot
tell a caller that composed them against a record it was shown from one that
invented them. The surface that showed the record can: :mod:`overseer_cli`
issues a digest only from the reading that prints the statements, requires
every claim-bearing invocation to name one, and refuses
:data:`evidence_unbound <REFUSALS>` when none is named -- a refusal spelled in
this vocabulary and raised only there.

## What stays human

:data:`HUMAN_TOPICS` -- acceptance, scope, design, safety -- are decisions this
program never makes. They are representable here so that a human decision on
one can be *recorded* and superseded like any other, and a :class:`Request` may
name the topics its incident turns on; any that no in-scope decision settles
makes the ruling :data:`human_decision <REFUSALS>` no matter what the
operational decisions say. Withheld acceptance settles nothing either: a
governing decision whose stance is ``hold`` leaves its topic unsettled and the
decision is reported, so an operator sees a refusal rather than an absence.

Durable records this boundary writes: **none**. A resolved decision is derived
from evidence every time it is needed, exactly as :mod:`overseer_context`
re-reads rather than recalls, because a stored ruling is the shape a cached
permission takes when nobody is looking: it would outlive the statement it
rested on, the scope it was made under, and the later instruction that replaced
it. Nothing here opens a file, spawns a process, or holds state between calls.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from . import (
    clock,
    mailbox_constants,
    mailbox_models,
    mailbox_vocabulary,
    overseer_history,
    overseer_policy_client,
)

__all__ = [
    "AUDIT_PROMPT_MARKER",
    "AUTHORIZING_VOICES",
    "AUTHORSHIPS",
    "CLAIM_REJECTIONS",
    "CONTEXT_ACCESS",
    "DRAFT_PROMPT_MARKER",
    "EVIDENCE_DIGEST_PREFIX",
    "GENERATED_PROMPT_MARKERS",
    "GUIDANCE_ENVELOPE_MARKER",
    "HUMAN_SESSION_CLASSES",
    "HUMAN_TOPICS",
    "MAILBOX_PROTOCOL_MARKER",
    "MAX_STATEMENT_CHARS",
    "OPERATIONS",
    "REFUSALS",
    "REPLY_CONTEXT_MARKER",
    "REPLY_ENVELOPE_MARKER",
    "RESOLUTION_CONTEXT_MARKER",
    "RESOLUTION_ENVELOPE_MARKER",
    "REVIEWED_VOICES",
    "SESSION_CLASSES",
    "SESSION_CLASS_AUTOMATED",
    "SESSION_CLASS_MAIN",
    "SESSION_CLASS_UNKNOWN",
    "STANCES",
    "STATE_SPACE",
    "SUPERVISION_VOICES",
    "TASK_PACKET_MARKER",
    "TOPICS",
    "TRANSCRIPT_SPACE",
    "VOICES",
    "Claim",
    "Decision",
    "Register",
    "Rejection",
    "Request",
    "Ruling",
    "Scope",
    "Settlement",
    "Statement",
    "dependencies",
    "evidence_digest",
    "generated_marker",
    "register",
    "resolve",
    "settle",
    "statements_from_history",
    "statements_from_supervision",
    "transcript_authorship",
]

#: The access category evidence is gathered under. Reading a conversation or a
#: plan's supervision traffic is ``context``; nothing here asks for more, and a
#: grant of another category yields no statements at all.
CONTEXT_ACCESS = overseer_policy_client.ACCESS_CONTEXT

#: Where a statement was read. ``asl-state`` is the same reserved pseudo-alias
#: :mod:`overseer_context` labels the runner's own records with -- restated
#: rather than imported, so this module does not depend on the packet builder to
#: name a mailbox; a test pins the two spellings together.
TRANSCRIPT_SPACE = "transcript"
STATE_SPACE = "asl-state"

#: How much of one statement is carried. The evidence boundaries already bound
#: what they hand over; this is a second cap so a hand-built statement cannot
#: put an unbounded string into a decision record.
MAX_STATEMENT_CHARS = 4000

Voice = Literal[
    "user_instruction",
    "assistant_interpretation",
    "worker_request",
    "generated_prompt",
    "relayed_instruction",
    "machine_record",
    "unattributed",
]

#: Who is speaking in one record. Treat as a consumer contract: a claim is
#: accepted or rejected on this value alone.
#:
#: * ``user_instruction`` -- a human's own words, in a record that attests the
#:   authorship: a transcript message under the ``user`` role carrying no
#:   machine marker.
#: * ``assistant_interpretation`` -- an assistant restating, summarizing or
#:   proposing. It may be right; it is not the instruction.
#: * ``worker_request`` -- the run's own worker asking for something.
#: * ``generated_prompt`` -- text ASL composed and delivered under a party's
#:   role: a task packet, a guidance envelope, reply or resolution context, an
#:   audit or draft prompt.
#: * ``relayed_instruction`` -- a mailbox guidance, reply or resolution. An
#:   instruction that reached the run through the supervision channel, whose
#:   author the record does not name.
#: * ``machine_record`` -- ASL's own bookkeeping: acks, deliveries, receipts,
#:   wait markers, supervision closures.
#: * ``unattributed`` -- a role or kind this build cannot attribute. Fail
#:   closed: it is never read as any of the others.
VOICES: tuple[Voice, ...] = (
    "user_instruction",
    "assistant_interpretation",
    "worker_request",
    "generated_prompt",
    "relayed_instruction",
    "machine_record",
    "unattributed",
)

#: The only voice a decision may rest on. A tuple rather than a constant so the
#: check reads as membership and so widening it is a visible edit to a published
#: name rather than a changed comparison buried in a validator.
AUTHORIZING_VOICES: tuple[Voice, ...] = ("user_instruction",)

#: The voices a human's words can reach evidence through, and therefore the
#: statements :func:`evidence_digest` summarizes. Wider than
#: :data:`AUTHORIZING_VOICES` on purpose: a relayed instruction cannot carry a
#: decision, but a hold arriving through the mailbox is still a change in what a
#: host would have read. Every other voice is excluded so that ASL's own
#: bookkeeping -- the ack of the very message being sent, a receipt, a worker's
#: new question -- cannot change the digest and make an unchanged record look
#: like a moved one.
REVIEWED_VOICES: tuple[Voice, ...] = ("user_instruction", "relayed_instruction")

#: Domain separator for :func:`evidence_digest`. Named and prefixed onto the
#: value so a digest is recognizable in a payload and so a later hashing of a
#: different thing cannot be mistaken for this one.
EVIDENCE_DIGEST_PREFIX = "asl-overseer-evidence-v1"

#: AgentMux's classification of a whole conversation, restated here for the same
#: reason the prompt markers are: this module's import surface is four ASL
#: modules and the vocabulary is published by another repository. The spellings
#: are ``transcript.SessionClass`` in ``sglavoie_agentmux``, and
#: ``amx overseer history`` normalizes anything it cannot classify to
#: ``unknown`` before answering -- so an empty or unrecognized value reaching
#: here is an older or unexpected answer, and is treated as unattributable.
SESSION_CLASS_MAIN = "main"
SESSION_CLASS_AUTOMATED = "automated"
SESSION_CLASS_UNKNOWN = "unknown"

#: Every class this build knows how to attribute. A class outside it is not an
#: error and not a person: it is unattributable, which refuses.
SESSION_CLASSES: tuple[str, ...] = (
    SESSION_CLASS_MAIN,
    SESSION_CLASS_AUTOMATED,
    SESSION_CLASS_UNKNOWN,
)

#: The classes whose ``user`` turns may be read as a person's own words. Only a
#: session AgentMux positively identified as human-started qualifies, so the
#: provenance boundary holds without a caller remembering to name anything.
HUMAN_SESSION_CLASSES: tuple[str, ...] = (SESSION_CLASS_MAIN,)

Authorship = Literal["human", "generated", "unattributed"]

#: What a conversation's classification says about who wrote its ``user`` turns,
#: before the turn itself is looked at. ``human`` still has to survive the
#: marker check; the other two are already disqualifying.
AUTHORSHIPS: tuple[Authorship, ...] = ("human", "generated", "unattributed")

Operation = Literal["reply", "guidance", "verify", "commit", "resume"]

#: The operational scope an overseer can be authorized for: answering a
#: question, sending guidance, running verification, committing a checkpoint,
#: and resuming a run. Repairing the application a run is working on is not
#: here, and no decision can put it here. Neither is urgent redirect
#: (``asl control redirect``), for the same reason read the other way round:
#: every operation above either answers a question the run asked or acts
#: between turns, while a redirect interrupts the turn in flight and replaces
#: what the worker was told. That is an intervention a user authorizes for one
#: correction, not a standing authority a fleet is granted -- so it is
#: documented for an explicitly authorized supervisor in ``supervisor_guide``
#: and deliberately absent from this tuple.
OPERATIONS: tuple[Operation, ...] = (
    "reply",
    "guidance",
    "verify",
    "commit",
    "resume",
)

Topic = Literal["operations", "acceptance", "scope", "design", "safety"]

#: What a decision is about. ``operations`` decisions name operations; the other
#: four name none and cannot be made to.
TOPICS: tuple[Topic, ...] = (
    "operations",
    "acceptance",
    "scope",
    "design",
    "safety",
)

#: The questions that stay with a human. A decision on one is recordable, so
#: that an operator's answer can supersede their earlier answer; what is not
#: available is for the overseer to answer one itself.
HUMAN_TOPICS: tuple[Topic, ...] = ("acceptance", "scope", "design", "safety")

Stance = Literal["allow", "hold"]

#: What a decision does with its topic. A ``hold`` naming no operation holds
#: every one of them, which is what "stop, do not touch this run" has to mean if
#: preserving a hold is to be structural rather than a matter of listing.
STANCES: tuple[Stance, ...] = ("allow", "hold")

RejectionReason = Literal[
    "unknown_statement",
    "not_user_authored",
    "undated",
    "scope_unnamed",
    "scope_widened",
    "unknown_topic",
    "unknown_operation",
    "operations_unnamed",
    "operations_named",
    "duplicate_claim",
]

#: Why a claim did not become a decision.
#:
#: * ``unknown_statement`` -- it cites a statement not in the evidence.
#: * ``not_user_authored`` -- its statement is not a direct user instruction.
#: * ``undated`` -- its statement carries no readable moment, so nothing can be
#:   ordered against it.
#: * ``scope_unnamed`` -- it names no project, so it covers nothing.
#: * ``scope_widened`` -- it claims reach its statement does not have.
#: * ``unknown_topic`` / ``unknown_operation`` -- outside the closed vocabulary.
#: * ``operations_unnamed`` -- an operational allowance that allows nothing.
#: * ``operations_named`` -- an acceptance, scope, design or safety decision
#:   carrying operations, which is how a human topic would be laundered into an
#:   operational permission.
#: * ``duplicate_claim`` -- a second reading of one statement on one topic.
CLAIM_REJECTIONS: tuple[RejectionReason, ...] = (
    "unknown_statement",
    "not_user_authored",
    "undated",
    "scope_unnamed",
    "scope_widened",
    "unknown_topic",
    "unknown_operation",
    "operations_unnamed",
    "operations_named",
    "duplicate_claim",
)

Refusal = Literal[
    "target_unscoped",
    "operation_unsupported",
    "evidence_unbound",
    "evidence_changed",
    "held",
    "human_decision",
    "unsupported_recovery",
    "no_decision",
]

#: Why an operation is not authorized, strongest first.
#:
#: * ``target_unscoped`` -- the request names no project, so no decision can
#:   apply to it.
#: * ``operation_unsupported`` -- outside :data:`OPERATIONS` entirely.
#: * ``evidence_unbound`` -- claims were submitted without naming the record
#:   they were read from, so there is no reading to check. :func:`resolve`
#:   never raises it: an empty ``reviewed`` here is a caller declining to make
#:   the binding, and only the surface that *presented* a record to a host can
#:   say that a reading of it was owed. :mod:`overseer_cli` raises it for every
#:   claim-bearing invocation, which is where the presentation happens.
#: * ``evidence_changed`` -- the request names the evidence its claims were read
#:   from and the evidence in hand is no longer that evidence. The claims may
#:   all still stand; what cannot stand is a reading of a record that has moved.
#: * ``held`` -- the governing decision holds this run.
#: * ``human_decision`` -- the incident turns on an acceptance, scope, design or
#:   safety question that no in-scope decision settles.
#: * ``unsupported_recovery`` -- decisions elsewhere suggest this recovery and
#:   none of them was made about this.
#: * ``no_decision`` -- nothing in the evidence speaks to it at all.
REFUSALS: tuple[Refusal, ...] = (
    "target_unscoped",
    "operation_unsupported",
    "evidence_unbound",
    "evidence_changed",
    "held",
    "human_decision",
    "unsupported_recovery",
    "no_decision",
)

#: The openings of text ASL composes and sends to a harness. Restated here
#: rather than imported: ``task_packet``, ``run_prompts``, ``supervisor_audit_agent``
#: and ``assurance_draft`` are prompt-building modules that pull argparse,
#: workspaces and verification behind them, and this boundary needs eight
#: strings from them. ``tests/test_asl_overseer_decisions.py`` compares every
#: one against the module that publishes it, so a reworded prompt fails there
#: rather than silently reclassifying a machine prompt as an instruction.
TASK_PACKET_MARKER = "# ASL session task packet"
MAILBOX_PROTOCOL_MARKER = "## Live mailbox protocol"
GUIDANCE_ENVELOPE_MARKER = "BEGIN ASL PENDING GUIDANCE JSONL"
REPLY_CONTEXT_MARKER = "## ASL reply context"
REPLY_ENVELOPE_MARKER = "BEGIN ASL OPERATOR REPLY JSONL"
RESOLUTION_CONTEXT_MARKER = "## ASL resolution context"
RESOLUTION_ENVELOPE_MARKER = "BEGIN ASL SUPERVISOR RESOLUTION JSONL"
AUDIT_PROMPT_MARKER = (
    "You are auditing the control plane of one finished agent-session-loop (ASL) run."
)
DRAFT_PROMPT_MARKER = (
    "You are drafting an improvement proposal for one issue in the agent-session-loop"
)

#: Matched as substrings rather than prefixes, because ASL appends its context
#: blocks to a base prompt and a harness stores the whole thing as one message.
GENERATED_PROMPT_MARKERS: tuple[str, ...] = (
    TASK_PACKET_MARKER,
    MAILBOX_PROTOCOL_MARKER,
    GUIDANCE_ENVELOPE_MARKER,
    REPLY_CONTEXT_MARKER,
    REPLY_ENVELOPE_MARKER,
    RESOLUTION_CONTEXT_MARKER,
    RESOLUTION_ENVELOPE_MARKER,
    AUDIT_PROMPT_MARKER,
    DRAFT_PROMPT_MARKER,
)

#: What each mailbox record is, as a voice. Guidance, replies and resolutions
#: are relayed rather than attributed because ``MailboxMessage`` has no author
#: field: an operator typing into ``asl inbox guidance`` and a supervising agent
#: relaying them write the same record. A test pins this table to
#: ``mailbox_vocabulary.MESSAGE_KINDS`` so a new kind cannot arrive unclassified.
SUPERVISION_VOICES: dict[str, Voice] = {
    mailbox_vocabulary.KIND_GUIDANCE: "relayed_instruction",
    mailbox_vocabulary.KIND_REPLY: "relayed_instruction",
    mailbox_vocabulary.KIND_RESOLUTION: "relayed_instruction",
    mailbox_vocabulary.KIND_QUESTION: "worker_request",
    mailbox_vocabulary.KIND_NOTICE: "worker_request",
    mailbox_vocabulary.KIND_ACK: "machine_record",
    mailbox_vocabulary.KIND_WAIT_START: "machine_record",
    mailbox_vocabulary.KIND_WAIT_END: "machine_record",
    mailbox_vocabulary.KIND_RECEIPT: "machine_record",
    mailbox_vocabulary.KIND_SUPERVISION_END: "machine_record",
    mailbox_vocabulary.KIND_GUIDANCE_DELIVERY: "machine_record",
}


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
    voice: Voice
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
        return self.voice in AUTHORIZING_VOICES

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
    topic: Topic
    stance: Stance
    scope: Scope
    operations: tuple[Operation, ...] = ()
    corroborating: tuple[str, ...] = ()
    reading: str = ""


@dataclass(frozen=True)
class Decision:
    """One accepted claim: a user decision, its scope, and what it rests on."""

    decision_id: str
    statement_id: str
    topic: Topic
    stance: Stance
    scope: Scope
    operations: tuple[Operation, ...]
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
    reason: RejectionReason
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

    topic: Topic
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
    raises: tuple[Topic, ...] = ()
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
    refusal: Refusal | None
    decision: Decision | None = None
    superseded: tuple[Decision, ...] = ()
    precedents: tuple[Decision, ...] = ()
    settlements: tuple[Settlement, ...] = ()
    summary: str = ""

    @property
    def unresolved(self) -> tuple[Topic, ...]:
        """Every raised human topic no in-scope decision settles."""
        return tuple(item.topic for item in self.settlements if not item.settled)

    @property
    def sources(self) -> tuple[str, ...]:
        """Every statement the governing decision rests on."""
        return () if self.decision is None else self.decision.sources


def generated_marker(text: str) -> str:
    """The machine-prompt marker a text carries, or ``""`` if it carries none."""
    for marker in GENERATED_PROMPT_MARKERS:
        if marker in text:
            return marker
    return ""


def transcript_authorship(
    session_class: str,
    *,
    generated_classes: Sequence[str] = (),
) -> Authorship:
    """What one conversation's class says about who wrote its ``user`` turns.

    Checked in the order that keeps ``generated_classes`` a restriction: a
    named class is machine-authored whatever AgentMux called it, ``automated``
    is machine-authored on AgentMux's own word, ``main`` is the one human
    answer, and everything else -- ``unknown``, the empty class an older answer
    carries, a spelling from a newer AgentMux -- is unattributable.
    """
    named = session_class.strip()
    if named in {value.strip() for value in generated_classes if value.strip()}:
        return "generated"
    if named == SESSION_CLASS_AUTOMATED:
        return "generated"
    if named in HUMAN_SESSION_CLASSES:
        return "human"
    return "unattributed"


def statements_from_history(
    history: overseer_history.History,
    *,
    generated_classes: Sequence[str] = (),
    repositories: Sequence[str] = (),
) -> tuple[Statement, ...]:
    """Read transcript evidence as statements, scoped to the project it names.

    Evidence that was not permitted yields nothing: without a grant there is no
    answer to read, and reading one anyway would be exactly the substitution
    :mod:`overseer_history` refuses to make on its own side.

    A conversation's scope is the project AgentMux authorized it under, and no
    more -- a transcript names no plan, so a claim that wants one has to narrow
    the scope itself and be checked for it.

    ``repositories`` names the roots a plan declares. A conversation's working
    directory is resolved back to the one containing it, because the project is
    what AgentMux authorized and the directory is merely where the harness was
    started: a conversation opened in a repository subdirectory is evidence
    about that repository, and scoping it to the subdirectory would name a
    project no request ever carries. A directory under none of them keeps its
    own spelling, which authorizes nothing, and passing no repositories at all
    reads every directory literally.

    Authorship comes from the conversation's own class before anything about
    the turn is read, on the fail-closed reading in
    :func:`transcript_authorship`: an ``automated`` conversation contributes
    ``generated_prompt`` statements and an unattributable one contributes
    ``unattributed`` statements, neither of which can carry a decision, whether
    or not the text holds a recognized marker.

    ``generated_classes`` names further conversation classes whose ``user``
    turns are written by another agent. It is a restriction and only a
    restriction: no value in it can make a class human, so a caller that
    forgets it loses no safeguard.
    """
    if history.grant() is None:
        return ()
    roots = _declared_roots(repositories)
    statements: list[Statement] = []
    for conversation in history.conversations:
        authorship = transcript_authorship(
            conversation.session_class, generated_classes=generated_classes
        )
        locator = f"{conversation.harness}:{conversation.session_id}"
        project = _project_of(conversation.directory, roots)
        for excerpt in conversation.excerpts:
            marker = generated_marker(excerpt.text)
            voice = _transcript_voice(
                role=excerpt.role, marker=marker, authorship=authorship
            )
            statements.append(
                _statement(
                    statement_id=f"{TRANSCRIPT_SPACE}:{locator}#{excerpt.index}",
                    voice=voice,
                    space=TRANSCRIPT_SPACE,
                    locator=locator,
                    scope=Scope(project=project),
                    at=excerpt.timestamp,
                    text=excerpt.text,
                    marker=marker if voice == "generated_prompt" else "",
                )
            )
    return tuple(statements)


def statements_from_supervision(
    messages: Iterable[mailbox_models.MailboxMessage],
    *,
    grant: overseer_policy_client.Grant | None,
    scope: Scope,
) -> tuple[Statement, ...]:
    """Read a plan's mailbox traffic as statements, scoped to that plan.

    The grant is checked against the scope before anything is classified, on the
    same terms :mod:`overseer_context` checks it: the same plan, the ``context``
    category, and both identities present. A mismatch yields no statements
    rather than unscoped ones, because a statement with a scope nobody
    authorized is a statement that could be claimed against any plan.

    Nothing here becomes a ``user_instruction``. The mailbox records no author,
    so its instruction-shaped records are relayed: they show that an instruction
    arrived, never that a human composed it.
    """
    if (
        grant is None
        or not scope.plan_dir
        or grant.plan_dir != scope.plan_dir
        or grant.access != CONTEXT_ACCESS
        or not grant.request_digest
        or not grant.config_fingerprint
    ):
        return ()
    statements: list[Statement] = []
    for message in messages:
        text = message.text or ""
        voice = SUPERVISION_VOICES.get(message.kind, "unattributed")
        marker = generated_marker(text)
        if marker and voice == "relayed_instruction":
            voice = "generated_prompt"
        statements.append(
            _statement(
                statement_id=f"{STATE_SPACE}:{message.seq}:{message.id}",
                voice=voice,
                space=STATE_SPACE,
                locator=f"{mailbox_constants.MAILBOX_FILE_NAME}#{message.seq}",
                scope=scope,
                at=message.created_at,
                text=text,
                marker=marker if voice == "generated_prompt" else "",
            )
        )
    return tuple(statements)


def evidence_digest(statements: Sequence[Statement], *, scope: Scope) -> str:
    """Summarize the human evidence in scope, so a reading can name what it read.

    Only :data:`REVIEWED_VOICES` statements whose own scope bears on ``scope``
    take part. Both narrowings matter: a machine record cannot move the digest,
    since ASL writes one for every message it delivers and a digest that
    answered to those would report a change on every action; and a statement
    about another plan cannot move it either, for the same reason a decision
    about another plan cannot authorize this one.

    Bearing on a scope is deliberately weaker than authorizing it: a statement
    in one of a multi-repository plan's projects counts, even though on its own
    it could authorize nothing there. Freshness asks what the host could have
    read, and a hold it would have read is a hold that has to move the digest.

    The identity is order-independent -- statements are sorted by their own ID
    -- so a boundary returning the same records in a different order is the same
    record. Everything a reading could turn on is hashed: the statement's
    identity and voice, its moment, its text, and the scope it was made in, each
    length-prefixed so no two different records can pack into one string.
    """
    relevant = sorted(
        (
            item
            for item in statements
            if item.voice in REVIEWED_VOICES and item.scope.bears_on(scope)
        ),
        key=lambda item: item.statement_id,
    )
    digest = hashlib.sha256()
    parts = [EVIDENCE_DIGEST_PREFIX]
    for item in relevant:
        parts.extend(
            (
                item.statement_id,
                item.voice,
                item.at,
                item.text,
                "\x1f".join(item.scope.named_projects),
                item.scope.plan_dir,
                item.scope.run_id,
                item.scope.incident,
            )
        )
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return f"ev1-{digest.hexdigest()[:32]}"


def register(
    *,
    statements: Sequence[Statement],
    claims: Sequence[Claim],
) -> Register:
    """Turn the host agent's readings into decisions, refusing what cannot hold.

    Every claim produces exactly one outcome -- a decision or a named rejection
    -- so a caller can account for each reading it submitted. Rejections carry
    the claim itself rather than an identifier, since a claim citing a statement
    that does not exist has no identity to look up.
    """
    known = {item.statement_id: item for item in statements}
    decisions: list[Decision] = []
    rejections: list[Rejection] = []
    seen: set[str] = set()
    for claim in claims:
        statement = known.get(claim.statement_id)
        outcome = _validate(claim, statement, known, seen)
        if isinstance(outcome, Rejection):
            rejections.append(outcome)
            continue
        seen.add(outcome.decision_id)
        decisions.append(outcome)
    decisions.sort(key=lambda item: item.order)
    return Register(
        statements=tuple(statements),
        decisions=tuple(decisions),
        rejections=tuple(rejections),
    )


def settle(*, register: Register, topic: Topic, scope: Scope) -> Settlement:
    """Which decision on one topic is in force for one scope.

    Applicable decisions are ordered, a hold still standing over any
    participating repository governs, and otherwise the last allowance covering
    the target does; the rest are superseded, and decisions on the topic that
    were made about something else are precedents. It is the same partition and
    the same governing rule :func:`resolve` performs for an operation, so a topic
    and an operation are answered by one rule rather than two that could drift
    apart -- which is what makes a per-repository release of a human hold read
    the same way as a release of an operational one.
    """
    applicable, precedents = _partition(
        (item for item in register.decisions if item.topic == topic), scope
    )
    governing = _governing(applicable, scope)
    return Settlement(
        topic=topic,
        scope=scope,
        decision=governing,
        superseded=_superseded(applicable, governing),
        precedents=precedents,
    )


def dependencies(
    *, register: Register, scope: Scope, raises: Sequence[Topic] = ()
) -> tuple[Topic, ...]:
    """Which human questions one request depends on.

    A human topic never :meth:`Decision.speaks_to` an operation, so an accepted
    decision about acceptance, scope, design or safety is invisible to
    :func:`resolve` unless the request names its topic. That is the right shape
    for a caller who knows a question is open and the wrong one for a caller who
    was handed the answer: a user who typed a safety hold about this run said the
    safety question governs it, and a caller that had to name the topic to be
    stopped by it could be stopped only by holds it already knew about.

    So the topics a caller names are unioned with the human topics some in-scope
    decision was claimed on, in that order and without repeats. Scope is the
    same :func:`_participates` relation :func:`settle` partitions by, so a topic
    is raised exactly when :func:`settle` could report a decision on it: a human
    decision made about another project, plan, run or incident stays a precedent
    rather than becoming a question about this one, because the anti-transfer
    rule reads in both directions -- a neighbour's hold no more raises this run's
    questions than a neighbour's allowance settles them. A decision in one of
    *this* target's own repositories is not a neighbour's, and raises the topic
    even when the action spans repositories it does not name; otherwise adding a
    second repository would hide the question entirely. That reaches an
    acceptance given in one repository as well as a hold typed in one, and
    raising it is what makes the missing half a visible unsettled question rather
    than a silence: settling still takes an allowance covering every repository.

    Raising a topic is not settling it. A topic reaches :attr:`Request.raises`
    whether the claimed decision allows or holds, and :func:`resolve` refuses
    ``human_decision`` for a hold and for a withheld or absent settlement alike,
    which is what keeps an operational allowance from standing in for an answer
    nobody gave.
    """
    claimed = tuple(
        decision.topic
        for decision in register.decisions
        if decision.topic in HUMAN_TOPICS and _participates(decision, scope)
    )
    return tuple(dict.fromkeys((*raises, *claimed)))


def resolve(*, register: Register, request: Request) -> Ruling:
    """Whether one operation is authorized for one target, and on what.

    Refusals are ordered by how fundamental they are rather than by how they
    were discovered: an unscoped request and an unsupported operation are
    questions that cannot be asked, a reading of evidence that has moved is a
    reading nobody made, a hold is a decision to respect, an unsettled human
    question outranks any operational allowance, and only then is the absence of
    a decision -- or the presence of one made about something else -- reported.

    Which decisions are in the partition at all is :func:`_participates`: every
    decision reaching the target or any single repository it names, so a hold
    reaches a target that names its repository among others and the allowance
    that later releases that repository is in the same history rather than
    filed away as somebody else's business. What each of them can *do* is
    :func:`_governing`: a hold still standing over any participating repository
    refuses, and authority remains the last allowance covering every one of them,
    so an action does not escape a hold by growing and a release does not become
    a permission by arriving.

    ``evidence_changed`` sits above the decision checks because it is not a
    verdict on them: the claims can all be in force and the allowance can be the
    last word among them while the record they were read from has gained a hold
    nobody claimed, which is precisely the case re-resolving the same claims
    cannot see.

    That refusal names the record the caller says it read and never the one it
    found instead. The digest of the record as it stands is what these very
    claims would need in order to pass, so printing it beside "your reading is
    out of date" would issue the replacement binding in the same breath as the
    refusal. The new record has a name only for somebody who has read it, and
    reading it is :func:`evidence_digest` over a listing, not this refusal.
    """
    settlements = tuple(
        settle(register=register, topic=topic, scope=request.scope)
        for topic in request.raises
    )
    if not request.scope.named_projects:
        return _refused(
            request,
            "target_unscoped",
            "the request names no project, so no decision can be applicable",
            settlements=settlements,
        )
    if request.operation not in OPERATIONS:
        return _refused(
            request,
            "operation_unsupported",
            f"{request.operation!r} is not an operation an overseer performs",
            settlements=settlements,
        )
    # Computed, compared, and dropped. The digest of the record as it stands is
    # the name of a record nobody has read, and a refusal that printed it would
    # be handing the caller the very binding its stale claims need. Only the
    # comparison survives these lines.
    if request.reviewed and request.reviewed != evidence_digest(
        register.statements, scope=request.scope
    ):
        return _refused(
            request,
            "evidence_changed",
            f"the claims were read from evidence {request.reviewed}, which is "
            "no longer the record in scope; the record in scope now is named "
            "only by reading it",
            settlements=settlements,
        )

    applicable, precedents = _partition(
        (item for item in register.decisions if item.speaks_to(request.operation)),
        request.scope,
    )
    governing = _governing(applicable, request.scope)
    superseded = _superseded(applicable, governing)

    if governing is None:
        partial = tuple(item for item in applicable if item.stance == "allow")
        unauthorized = _unauthorized_projects(applicable, request.scope)
        refusal: Refusal = (
            "unsupported_recovery" if precedents or partial else "no_decision"
        )
        detail = _no_authority_detail(
            request.operation,
            precedents=precedents,
            partial=partial,
            unauthorized=unauthorized,
        )
        return _refused(
            request,
            refusal,
            detail,
            superseded=superseded,
            precedents=precedents,
            settlements=settlements,
        )

    if governing.stance == "hold":
        return _refused(
            request,
            "held",
            f"{governing.decision_id} holds this run",
            decision=governing,
            superseded=superseded,
            precedents=precedents,
            settlements=settlements,
        )

    unsettled = tuple(item.topic for item in settlements if not item.settled)
    if unsettled:
        return _refused(
            request,
            "human_decision",
            f"unsettled human {'question' if len(unsettled) == 1 else 'questions'}: "
            f"{', '.join(unsettled)}",
            decision=governing,
            superseded=superseded,
            precedents=precedents,
            settlements=settlements,
        )

    return Ruling(
        operation=request.operation,
        scope=request.scope,
        authorized=True,
        refusal=None,
        decision=governing,
        superseded=superseded,
        precedents=precedents,
        settlements=settlements,
        summary=(
            f"{request.operation} authorized by {governing.decision_id} "
            f"({governing.at})"
        ),
    )


# -- validation ------------------------------------------------------------


def _validate(
    claim: Claim,
    statement: Statement | None,
    known: dict[str, Statement],
    seen: set[str],
) -> Decision | Rejection:
    """One claim against the statement it cites, in refusal order."""
    if statement is None:
        return Rejection(
            claim=claim,
            reason="unknown_statement",
            detail=f"{claim.statement_id} is not in the evidence",
        )
    if not statement.authorizing:
        return Rejection(
            claim=claim,
            reason="not_user_authored",
            detail=(
                f"{statement.statement_id} is a {statement.voice}, "
                "which cannot carry a decision"
            ),
        )
    if statement.moment is None:
        return Rejection(
            claim=claim,
            reason="undated",
            detail=(
                f"{statement.statement_id} carries no readable moment, so no "
                "later decision could be shown to replace it"
            ),
        )
    if not claim.scope.named_projects:
        return Rejection(
            claim=claim,
            reason="scope_unnamed",
            detail="a decision that names no project covers nothing",
        )
    if not statement.scope.narrows(claim.scope):
        return Rejection(
            claim=claim,
            reason="scope_widened",
            detail=(
                f"{statement.statement_id} was made about "
                f"{_scope_text(statement.scope)}, which does not reach "
                f"{_scope_text(claim.scope)}"
            ),
        )
    supporting = _supporting(claim, statement, known)
    covered = {project for item in supporting for project in item.scope.named_projects}
    uncovered = tuple(
        project for project in claim.scope.named_projects if project not in covered
    )
    if uncovered:
        return Rejection(
            claim=claim,
            reason="scope_widened",
            detail=(
                f"{statement.statement_id} was made about "
                f"{_scope_text(statement.scope)}, which does not reach "
                f"{_scope_text(claim.scope)}"
            )
            if len(supporting) == 1
            else (
                "no statement this claim rests on was made about "
                f"{', '.join(uncovered)}"
            ),
        )
    if claim.topic not in TOPICS:
        return Rejection(
            claim=claim,
            reason="unknown_topic",
            detail=f"{claim.topic!r} is not a topic a decision can be about",
        )
    unknown = tuple(item for item in claim.operations if item not in OPERATIONS)
    if unknown:
        return Rejection(
            claim=claim,
            reason="unknown_operation",
            detail=f"{', '.join(unknown)} is outside the operational scope",
        )
    if claim.topic == "operations":
        if claim.stance == "allow" and not claim.operations:
            return Rejection(
                claim=claim,
                reason="operations_unnamed",
                detail="an operational allowance has to name what it allows",
            )
    elif claim.operations:
        return Rejection(
            claim=claim,
            reason="operations_named",
            detail=(
                f"a {claim.topic} decision names no operation; naming one would "
                "turn a human judgement into an operational permission"
            ),
        )
    decision_id = f"{claim.statement_id}:{claim.topic}"
    if decision_id in seen:
        return Rejection(
            claim=claim,
            reason="duplicate_claim",
            detail=f"{claim.statement_id} was already read as a {claim.topic} decision",
        )
    sources = (claim.statement_id, *claim.corroborating)
    return Decision(
        decision_id=decision_id,
        statement_id=claim.statement_id,
        topic=claim.topic,
        stance=claim.stance,
        scope=claim.scope,
        operations=tuple(claim.operations),
        at=statement.at,
        moment=statement.moment,
        sources=sources,
        text=statement.text,
        reading=claim.reading,
    )


# -- helpers ---------------------------------------------------------------


def _supporting(
    claim: Claim, statement: Statement, known: dict[str, Statement]
) -> tuple[Statement, ...]:
    """The statements whose authorship can carry the projects a claim names.

    The cited statement, plus every corroborating one that is itself a dated
    user instruction reaching the same plan, run and incident. This is the only
    way one decision covers more than one project: a plan participating in two
    repositories needs a human statement in each, cited together, and no
    reading of the one can supply the other's authority.

    A corroborating statement that is not a dated user instruction lends
    nothing rather than rejecting the claim. Corroboration is also how a
    mailbox record showing that an instruction *arrived* is cited beside the
    transcript turn that shows who spoke, and that record has always been
    reportable without being authorizing.
    """
    supporting = [statement]
    for statement_id in claim.corroborating:
        item = known.get(statement_id)
        if item is None or not item.authorizing or item.moment is None:
            continue
        if item.scope.narrows(claim.scope):
            supporting.append(item)
    return tuple(supporting)


def _declared_roots(repositories: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Each declared root as (comparable, declared), longest comparable first.

    Repositories nest -- a checkout inside a checkout is a different project --
    so the most specific root has to be tried first or the enclosing one would
    claim a directory the nested one owns. The declared spelling is carried
    beside the comparable form because it is the identity a request names, and
    a statement scoped to a normalized variant of it would match nothing.
    """
    pairs = []
    for item in repositories:
        comparable = item.rstrip("/")
        if comparable:
            pairs.append((comparable, item))
    return tuple(sorted(pairs, key=lambda pair: len(pair[0]), reverse=True))


def _project_of(directory: str, roots: Sequence[tuple[str, str]]) -> str:
    """The declared repository a working directory belongs to, or the directory.

    Containment is measured on path boundaries, the same way AgentMux decides
    whether a conversation is in scope, so ``/srv/api-internal`` is not inside
    ``/srv/api``.
    """
    trimmed = directory.rstrip("/") or directory
    for comparable, declared in roots:
        if trimmed == comparable or trimmed.startswith(f"{comparable}/"):
            return declared
    return directory


def _applies(decision: Decision, scope: Scope) -> bool:
    """Whether one decision governs a target, which its stance decides.

    An allowance has to :meth:`Scope.covers` the target. Authority is held per
    repository, so a permission given in one of them cannot be spent on work
    that also touches another nobody spoke about, and a plan spanning two needs
    a human statement in each.

    A hold only has to :meth:`Scope.bears_on` it. A hold is a constraint on the
    repository it names rather than a permission for a set, and a target that
    still includes that repository is still work the hold was about: adding a
    second repository to an action cannot make an existing hold cease to reach
    it. Reading a hold through ``covers`` instead is exactly what let a union
    scope walk out from under one, and reading an allowance through ``bears_on``
    would let one repository's allowance authorize another.

    Both halves keep the plan, run and incident narrowing, since ``covers`` and
    ``bears_on`` share :meth:`Scope.narrows`, and both leave a decision about a
    project the target does not name a precedent.

    It is asked of a whole target and of one repository at a time -- the two
    questions :func:`_participates` and :func:`_standing_holds` ask -- and on a
    scope naming one project the two stances coincide, since a set of one is
    covered exactly when it is shared. That is why a repository's own chain can
    be read with the same predicate that decides authority over the target: the
    difference between a hold and an allowance is a difference about *sets* of
    repositories, and inside one repository there is no set to disagree about.
    """
    if decision.stance == "hold":
        return decision.scope.bears_on(scope)
    return decision.scope.covers(scope)


def _project_scopes(scope: Scope) -> tuple[Scope, ...]:
    """One target, seen one participating repository at a time.

    Everything but the project is carried through, because narrowing a target to
    a repository must not widen it in any other direction: a decision about
    another plan, run or incident is no more applicable to one of this target's
    repositories than it is to the target.
    """
    return tuple(
        Scope(
            project=project,
            plan_dir=scope.plan_dir,
            run_id=scope.run_id,
            incident=scope.incident,
        )
        for project in scope.named_projects
    )


def _participates(decision: Decision, scope: Scope) -> bool:
    """Whether a decision takes part in the target's history at all.

    A decision participates when it :func:`applies <_applies>` to the target or
    to any single repository the target names. The second half is what a
    per-repository release needs: an allowance naming one of two repositories
    cannot authorize the pair, but it is a decision about work the target
    includes, and one that arrived after a hold on that repository is the answer
    to whether the repository is still stopped. Excluding it -- which reading
    applicability against the whole target alone did -- left the hold governing a
    target the person had already released.

    A decision reaching none of the repositories reaches nothing here and stays
    a precedent, so the anti-transfer rule is untouched.
    """
    return any(_applies(decision, narrowed) for narrowed in _project_scopes(scope))


def _partition(
    decisions: Iterable[Decision], scope: Scope
) -> tuple[tuple[Decision, ...], tuple[Decision, ...]]:
    """Split decisions into the ones that reach ``scope`` and the ones that do not.

    Both halves come back ordered, because the applicable half is read as a
    supersession chain and the precedents are shown to an operator, and an
    unordered list of either would be a set of claims with no history.
    """
    applicable: list[Decision] = []
    precedents: list[Decision] = []
    for decision in decisions:
        target = applicable if _participates(decision, scope) else precedents
        target.append(decision)
    applicable.sort(key=lambda item: item.order)
    precedents.sort(key=lambda item: item.order)
    return tuple(applicable), tuple(precedents)


def _standing_holds(
    applicable: Sequence[Decision], scope: Scope
) -> tuple[Decision, ...]:
    """The hold still in force over each participating repository.

    One chain per repository: the applicable decisions naming it, in order,
    which ``applicable`` already is. The repository is stopped when the last of
    them is a hold -- so a hold is lifted by a later decision naming the same
    repository and by nothing else, and a repository nobody spoke about again
    stays stopped however many others were released.
    """
    holds: list[Decision] = []
    for narrowed in _project_scopes(scope):
        chain = [item for item in applicable if _applies(item, narrowed)]
        if chain and chain[-1].stance == "hold":
            holds.append(chain[-1])
    return tuple(holds)


def _governing(applicable: Sequence[Decision], scope: Scope) -> Decision | None:
    """Which applicable decision is in force over the target as a whole.

    A hold standing over any participating repository governs, because the
    target includes work that repository was stopped for; the latest of them is
    reported when more than one repository is stopped, so an operator is shown
    the most recent word rather than an arbitrary one.

    Otherwise the governing decision is the last allowance
    :meth:`covering <Scope.covers>` every repository the target names. Authority
    is still held per repository and is still read over the whole target, so
    lifting a hold restores the authority that was already established and never
    manufactures one: an allowance naming a subset can end that subset's hold and
    can never be the permission an action rests on.
    """
    stopped = _standing_holds(applicable, scope)
    if stopped:
        return max(stopped, key=lambda item: item.order)
    authority: Decision | None = None
    for item in applicable:
        if item.stance == "allow" and item.scope.covers(scope):
            authority = item
    return authority


def _superseded(
    applicable: Sequence[Decision], governing: Decision | None
) -> tuple[Decision, ...]:
    """Every applicable decision that is not the one in force, in order.

    Both the decisions the governing one replaced and the ones replaced under it
    -- a hold a release ended, a release that ended a hold in a repository that
    is not the one still stopping the target. They are reported rather than
    dropped for the reason supersession is reported at all: "replaced" and
    "never said" are different, and an operator reading a refusal needs to see
    the whole chain that produced it.
    """
    return tuple(item for item in applicable if item is not governing)


def _unauthorized_projects(
    applicable: Sequence[Decision], scope: Scope
) -> tuple[str, ...]:
    """Which participating repositories no applicable allowance speaks for."""
    allowed = {
        project
        for item in applicable
        if item.stance == "allow"
        for project in item.scope.named_projects
    }
    return tuple(project for project in scope.named_projects if project not in allowed)


def _no_authority_detail(
    operation: str,
    *,
    precedents: Sequence[Decision],
    partial: Sequence[Decision],
    unauthorized: Sequence[str],
) -> str:
    """Why nothing authorizes an operation, in the terms that make it actionable.

    Three different absences, and telling an operator which one this is decides
    what they do next: nobody spoke, somebody spoke about another run, or
    somebody spoke about some of this run's repositories and not the rest --
    which is answered by a statement in the repository that is short, cited
    beside the one that already spoke, rather than by re-asking the repository
    that did.
    """
    if partial and unauthorized:
        return (
            f"no decision authorizes {operation} in {', '.join(unauthorized)}, "
            "which this run participates in"
        )
    if partial:
        return (
            f"{len(partial)} decision(s) allow {operation} in this run's "
            "repositories one at a time, and authority over all of them has to "
            "rest on one claim citing a statement in each"
        )
    if precedents:
        return (
            f"{len(precedents)} decision(s) elsewhere allow {operation} "
            "and none of them was made about this run"
        )
    return f"no user decision speaks to {operation} here"


def _refused(
    request: Request,
    refusal: Refusal,
    detail: str,
    *,
    decision: Decision | None = None,
    superseded: tuple[Decision, ...] = (),
    precedents: tuple[Decision, ...] = (),
    settlements: tuple[Settlement, ...] = (),
) -> Ruling:
    """A ruling that authorizes nothing, and says which refusal it is."""
    return Ruling(
        operation=request.operation,
        scope=request.scope,
        authorized=False,
        refusal=refusal,
        decision=decision,
        superseded=superseded,
        precedents=precedents,
        settlements=settlements,
        summary=f"{request.operation} refused ({refusal}): {detail}",
    )


def _transcript_voice(*, role: str, marker: str, authorship: Authorship) -> Voice:
    """Attribute one transcript message from its conversation, role and text.

    Only the ``user`` role is ambiguous, so only it consults the conversation's
    authorship: an assistant is an assistant in an automated conversation too,
    and neither answer authorizes anything.
    """
    normalized = role.strip().lower()
    if normalized == "assistant":
        return "assistant_interpretation"
    if normalized != "user":
        return "unattributed"
    if authorship == "unattributed":
        return "unattributed"
    if authorship == "generated" or marker:
        return "generated_prompt"
    return "user_instruction"


def _statement(
    *,
    statement_id: str,
    voice: Voice,
    space: str,
    locator: str,
    scope: Scope,
    at: str,
    text: str,
    marker: str = "",
) -> Statement:
    """One statement, clipped and dated, however the source spelled its moment."""
    clipped = text[:MAX_STATEMENT_CHARS]
    return Statement(
        statement_id=statement_id,
        voice=voice,
        space=space,
        locator=locator,
        scope=scope,
        at=at,
        text=clipped,
        truncated=len(clipped) < len(text),
        marker=marker,
        moment=clock.parse_datetime(at),
    )


def _scope_text(scope: Scope) -> str:
    """One scope, rendered for a refusal an operator has to act on."""
    named = scope.named_projects
    label = "projects" if len(named) > 1 else "project"
    parts = [f"{label} {', '.join(named) or '-'}"]
    for label, value in (
        ("plan", scope.plan_dir),
        ("run", scope.run_id),
        ("incident", scope.incident),
    ):
        if value:
            parts.append(f"{label} {value}")
    return ", ".join(parts)
