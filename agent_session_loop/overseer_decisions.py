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


Implementation is split into focused sibling modules; exports here preserve
the existing import surface. Patch collaborators in their owning modules.
"""

from __future__ import annotations

from .overseer_decision_evidence import (
    _scope_text as _scope_text,
)
from .overseer_decision_evidence import (
    _statement as _statement,
)
from .overseer_decision_evidence import (
    _transcript_voice as _transcript_voice,
)
from .overseer_decision_evidence import (
    evidence_digest as evidence_digest,
)
from .overseer_decision_evidence import (
    generated_marker as generated_marker,
)
from .overseer_decision_evidence import (
    statements_from_history as statements_from_history,
)
from .overseer_decision_evidence import (
    statements_from_supervision as statements_from_supervision,
)
from .overseer_decision_evidence import (
    transcript_authorship as transcript_authorship,
)
from .overseer_decision_models import (
    Claim as Claim,
)
from .overseer_decision_models import (
    Decision as Decision,
)
from .overseer_decision_models import (
    Register as Register,
)
from .overseer_decision_models import (
    Rejection as Rejection,
)
from .overseer_decision_models import (
    Request as Request,
)
from .overseer_decision_models import (
    Ruling as Ruling,
)
from .overseer_decision_models import (
    Scope as Scope,
)
from .overseer_decision_models import (
    Settlement as Settlement,
)
from .overseer_decision_models import (
    Statement as Statement,
)
from .overseer_decision_resolution import (
    dependencies as dependencies,
)
from .overseer_decision_resolution import (
    register as register,
)
from .overseer_decision_resolution import (
    resolve as resolve,
)
from .overseer_decision_resolution import (
    settle as settle,
)
from .overseer_decision_scope import (
    _applies as _applies,
)
from .overseer_decision_scope import (
    _declared_roots as _declared_roots,
)
from .overseer_decision_scope import (
    _governing as _governing,
)
from .overseer_decision_scope import (
    _no_authority_detail as _no_authority_detail,
)
from .overseer_decision_scope import (
    _participates as _participates,
)
from .overseer_decision_scope import (
    _partition as _partition,
)
from .overseer_decision_scope import (
    _project_of as _project_of,
)
from .overseer_decision_scope import (
    _project_scopes as _project_scopes,
)
from .overseer_decision_scope import (
    _refused as _refused,
)
from .overseer_decision_scope import (
    _standing_holds as _standing_holds,
)
from .overseer_decision_scope import (
    _superseded as _superseded,
)
from .overseer_decision_scope import (
    _supporting as _supporting,
)
from .overseer_decision_scope import (
    _unauthorized_projects as _unauthorized_projects,
)
from .overseer_decision_validation import (
    _validate as _validate,
)
from .overseer_decision_vocab import (
    AUDIT_PROMPT_MARKER as AUDIT_PROMPT_MARKER,
)
from .overseer_decision_vocab import (
    AUTHORIZING_VOICES as AUTHORIZING_VOICES,
)
from .overseer_decision_vocab import (
    AUTHORSHIPS as AUTHORSHIPS,
)
from .overseer_decision_vocab import (
    CLAIM_REJECTIONS as CLAIM_REJECTIONS,
)
from .overseer_decision_vocab import (
    CONTEXT_ACCESS as CONTEXT_ACCESS,
)
from .overseer_decision_vocab import (
    DRAFT_PROMPT_MARKER as DRAFT_PROMPT_MARKER,
)
from .overseer_decision_vocab import (
    EVIDENCE_DIGEST_PREFIX as EVIDENCE_DIGEST_PREFIX,
)
from .overseer_decision_vocab import (
    GENERATED_PROMPT_MARKERS as GENERATED_PROMPT_MARKERS,
)
from .overseer_decision_vocab import (
    GUIDANCE_ENVELOPE_MARKER as GUIDANCE_ENVELOPE_MARKER,
)
from .overseer_decision_vocab import (
    HUMAN_SESSION_CLASSES as HUMAN_SESSION_CLASSES,
)
from .overseer_decision_vocab import (
    HUMAN_TOPICS as HUMAN_TOPICS,
)
from .overseer_decision_vocab import (
    MAILBOX_PROTOCOL_MARKER as MAILBOX_PROTOCOL_MARKER,
)
from .overseer_decision_vocab import (
    MAX_STATEMENT_CHARS as MAX_STATEMENT_CHARS,
)
from .overseer_decision_vocab import (
    OPERATIONS as OPERATIONS,
)
from .overseer_decision_vocab import (
    REFUSALS as REFUSALS,
)
from .overseer_decision_vocab import (
    REPLY_CONTEXT_MARKER as REPLY_CONTEXT_MARKER,
)
from .overseer_decision_vocab import (
    REPLY_ENVELOPE_MARKER as REPLY_ENVELOPE_MARKER,
)
from .overseer_decision_vocab import (
    RESOLUTION_CONTEXT_MARKER as RESOLUTION_CONTEXT_MARKER,
)
from .overseer_decision_vocab import (
    RESOLUTION_ENVELOPE_MARKER as RESOLUTION_ENVELOPE_MARKER,
)
from .overseer_decision_vocab import (
    REVIEWED_VOICES as REVIEWED_VOICES,
)
from .overseer_decision_vocab import (
    SESSION_CLASS_AUTOMATED as SESSION_CLASS_AUTOMATED,
)
from .overseer_decision_vocab import (
    SESSION_CLASS_MAIN as SESSION_CLASS_MAIN,
)
from .overseer_decision_vocab import (
    SESSION_CLASS_UNKNOWN as SESSION_CLASS_UNKNOWN,
)
from .overseer_decision_vocab import (
    SESSION_CLASSES as SESSION_CLASSES,
)
from .overseer_decision_vocab import (
    STANCES as STANCES,
)
from .overseer_decision_vocab import (
    STATE_SPACE as STATE_SPACE,
)
from .overseer_decision_vocab import (
    SUPERVISION_VOICES as SUPERVISION_VOICES,
)
from .overseer_decision_vocab import (
    TASK_PACKET_MARKER as TASK_PACKET_MARKER,
)
from .overseer_decision_vocab import (
    TOPICS as TOPICS,
)
from .overseer_decision_vocab import (
    TRANSCRIPT_SPACE as TRANSCRIPT_SPACE,
)
from .overseer_decision_vocab import (
    VOICES as VOICES,
)
from .overseer_decision_vocab import (
    Authorship as Authorship,
)
from .overseer_decision_vocab import (
    Operation as Operation,
)
from .overseer_decision_vocab import (
    Refusal as Refusal,
)
from .overseer_decision_vocab import (
    RejectionReason as RejectionReason,
)
from .overseer_decision_vocab import (
    Stance as Stance,
)
from .overseer_decision_vocab import (
    Topic as Topic,
)
from .overseer_decision_vocab import (
    Voice as Voice,
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
