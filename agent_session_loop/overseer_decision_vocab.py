"""Overseer decision vocab."""

from __future__ import annotations

from typing import Literal

from . import (
    mailbox_vocabulary,
    overseer_policy_client,
)

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
