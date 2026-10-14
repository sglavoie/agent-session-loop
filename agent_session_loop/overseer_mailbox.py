"""The overseer's two mailbox operations, and the five states they pass through.

Answering a question and sending guidance are the only things the overseer does
that a worker can see, and both of them go into the plan's append-only mailbox
through the primitives :mod:`agent_session_loop.mailbox` already publishes.
This module adds no storage, no second ledger of messages, and no wait loop. It
adds three things the mailbox by itself does not give a fleet supervisor: an
identity that survives a crash, a state vocabulary that keeps *queued* apart
from *consumed*, and a poll that reads one plan without holding up the rest.

## The message id is derived, so a retry is not a second message

Every posting's ``message_id`` is :func:`message_id`, a UUID5 over the plan
directory, the incident, the operation and the dispatch target -- the four
fields that appear in both :class:`~agent_session_loop.overseer_dispatch.Action`
and :class:`~agent_session_loop.overseer_store_models.ActionRecord`. Nothing about the
moment goes into it, which is the whole point: a process that appended and then
died wrote a record whose id the next process can compute rather than guess.
So the append is idempotent by construction. A retry either lands or is refused
``duplicate-id``, and that refusal is proof the earlier attempt landed, not a
new failure. It is also what makes :meth:`ReplyAdapter.reconcile` answerable
from the ledger row alone: the row carries the four fields, the four fields name
the record, and the record is either in the mailbox or it is not. No timestamp
heuristic, no content match, and no risk of reading another supervisor's reply
as our own.

## Five states, and the mailbox's own word kept alongside

:data:`STATES` is ``queued``, ``accepted``, ``delivered``, ``consumed``,
``rejected``, and the reason they are tracked separately is that collapsing any
two of them tells an operator something false. A reply that is in the mailbox
but that the worker has not taken is ``accepted``; it becomes ``consumed`` only
when a receipt for the closure exists, which is the mailbox's own evidence that
the answer reached the run. Guidance that is in the mailbox but that no channel
has carried is ``queued`` -- and the mailbox's ``deferred`` state, which means
*an attempt was made and did not deliver*, is ``queued`` here too, because a
deferred attempt is a durable "not yet" and never a "no". Only ``delivered``
means it arrived. Every :class:`Posting` also carries ``mailbox_state``, the
mailbox's unparaphrased word for the same fact, so a caller that needs to know
whether guidance was never attempted or attempted and deferred still can.

## Nothing here waits

There is no sleep, no poll interval and no blocking read in this module.
:func:`poll_plan` performs exactly one mailbox read and projects every reply and
guidance row the ledger holds for that plan; :func:`poll_fleet` does that for
many plans in the rotation :mod:`agent_session_loop.overseer_watch` uses --
least-recently-polled first, keyed on a durable cursor so the order survives a
restart -- stops *starting* work at its deadline, and reports the plans it did
not reach as ``deferred`` rather than returning a quiet fleet. A plan whose
receipt has not arrived is simply ``outstanding`` and is looked at again next
pass, and a plan whose mailbox cannot be read is one unreadable plan rather than
a stalled sweep. That is what "poll receipts through the fleet scheduler"
buys: no plan's silence is another plan's wait.

## An answer needs authority that is current, twice

:func:`post_reply` and :func:`post_guidance` take a
:class:`~agent_session_loop.overseer_decisions.Ruling` as a required keyword and
check it before doing anything, so there is no code path from this module to an
append that no user decision authorized. The check is not "is this a ruling" --
it is that the ruling authorizes *this* operation, rests on a governing decision
that speaks to it with an ``allow`` stance, leaves no raised human topic
unsettled, and was made in a scope naming this plan, this run and this incident.
Then, after the mailbox has been read and the question revalidated, authority is
established again through the caller's ``authority`` seam and required to be the
same decision. A decision replaced or withdrawn in between is
``authority_superseded``, which is what revocation looks like from here: the
append never happens, and the refusal names why.

The question is revalidated in that same window, from the mailbox itself, for
the case the overseer cannot prevent: another supervisor or the user answering
first. A closure that is not ours is ``question_closed``, and the append is not
attempted. Should the two of us race past the read anyway, the mailbox's own
exclusive-lock append refuses the loser with ``question-already-answered``, and
that refusal is translated to the same word -- so the outcome is identical
whether the race was lost early or late.

Replies are appended with :func:`agent_session_loop.mailbox.append_reply`
directly rather than through
:func:`agent_session_loop.intervention.append_question_reply`, which writes a
supervisor lease. The overseer is not the plan's supervisor and displacing one
would be an intervention nobody authorized.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import (
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_dispatch,
    overseer_store_ledger,
    overseer_store_models,
)

__all__ = [
    "MAILBOX_STREAM",
    "MAIL_OPERATIONS",
    "REFUSALS",
    "STATES",
    "FleetPoll",
    "GuidanceAdapter",
    "PlanPoll",
    "Posting",
    "PostingUncertain",
    "ReplyAdapter",
    "adapters",
    "inspect_posting",
    "message_id",
    "poll_fleet",
    "poll_plan",
    "post_guidance",
    "post_reply",
]

Operation = overseer_decisions.Operation

#: The operations this module implements. A strict subset of the operational
#: scope: the other three are somebody else's adapters.
MAIL_OPERATIONS: tuple[Operation, ...] = ("reply", "guidance")

State = Literal["queued", "accepted", "delivered", "consumed", "rejected"]

#: What became of one posting, ordered from least to most carried. ``queued``
#: and ``accepted`` are outstanding; ``delivered`` and ``consumed`` are the two
#: kinds of arrival evidence the mailbox actually records.
STATES: tuple[State, ...] = (
    "queued",
    "accepted",
    "delivered",
    "consumed",
    "rejected",
)

Refusal = Literal[
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "question_unknown",
    "question_closed",
    "question_stale",
    "mailbox_full",
    "mailbox_unreadable",
    "record_rejected",
]

#: Why a posting did not happen. Closed, and deliberately separate from the
#: dispatcher's refusals: these are what an adapter reports about the mailbox,
#: not what the single door decided about permission.
REFUSALS: tuple[Refusal, ...] = (
    "authority_absent",
    "authority_refused",
    "authority_silent",
    "authority_held",
    "authority_unscoped",
    "authority_superseded",
    "payload_mismatch",
    "question_unknown",
    "question_closed",
    "question_stale",
    "mailbox_full",
    "mailbox_unreadable",
    "record_rejected",
)

#: The ledger observation stream this module's rotation cursor lives in. Its own
#: stream rather than the watcher's, so polling and enrolling rotate
#: independently and neither starves the other.
MAILBOX_STREAM = "mailbox"

#: The UUID5 namespace posting identities are derived in. Fixed, because an id
#: that changed between builds would make every pre-upgrade row unreconcilable.
_ID_NAMESPACE = uuid.UUID("6f4a1d38-5f2e-5a63-9a2d-2b0f0f1a7c11")

#: How the mailbox's own rejection words map onto :data:`REFUSALS`. A reason
#: absent from here is ``record_rejected``; ``duplicate-id`` and
#: ``mailbox-write-error`` are not refusals at all and are handled separately.
_REFUSAL_FOR_REASON: dict[str, Refusal] = {
    mailbox_constants.REJECT_QUESTION_NOT_FOUND: "question_unknown",
    mailbox_constants.REJECT_QUESTION_ALREADY_ANSWERED: "question_closed",
    mailbox_constants.REJECT_QUESTION_ALREADY_RESOLVED: "question_closed",
    mailbox_constants.REJECT_QUESTION_NOT_OPEN: "question_stale",
    mailbox_constants.REJECT_QUESTION_NOT_WAITING: "question_stale",
    mailbox_constants.ERROR_MAILBOX_FULL: "mailbox_full",
    mailbox_constants.ERROR_MAILBOX_READ: "mailbox_unreadable",
}


class PostingUncertain(Exception):
    """A posting whose outcome this module refuses to guess.

    Raised only when the mailbox could neither be written nor re-read, so
    "it landed" and "it did not" are both unsupported. The dispatcher turns
    this into ``outcome_uncertain`` and leaves the ledger row attempted, which
    is what :func:`inspect_posting` is later able to settle.
    """

    def __init__(self, detail: str, *, message_id: str = "") -> None:
        self.detail = detail
        self.message_id = message_id
        super().__init__(detail)


@dataclass(frozen=True)
class Posting:
    """One mailbox operation, and how far it has actually got.

    ``mailbox_state`` is the mailbox's own vocabulary for the same fact --
    ``queued``, ``deferred``, ``delivered-live``, ``accepted``, ``consumed`` --
    kept next to :attr:`state` rather than replaced by it, because the
    projection loses the difference between guidance nobody tried to deliver
    and guidance a delivery attempt deferred.
    """

    operation: Operation
    plan_dir: str
    incident_id: int | None
    target: str
    message_id: str
    state: State
    mailbox_state: str = ""
    refusal: Refusal | None = None
    detail: str = ""
    seq: int = 0
    created_at: str = ""

    @property
    def landed(self) -> bool:
        """Whether a record for this posting is in the mailbox."""
        return self.state != "rejected"

    @property
    def outstanding(self) -> bool:
        """Whether the worker has not yet been shown to have received it."""
        return self.state in ("queued", "accepted")

    @property
    def arrived(self) -> bool:
        """Whether the mailbox holds evidence the worker received it."""
        return self.state in ("delivered", "consumed")


@dataclass(frozen=True)
class PlanPoll:
    """One non-blocking look at one plan's mailbox.

    ``read`` false means the mailbox could not be read this pass. It is kept
    apart from an empty ``postings`` because a plan with nothing outstanding
    and a plan nobody could look at are opposite facts.
    """

    plan_dir: str
    read: bool
    postings: tuple[Posting, ...] = ()
    last_seq: int = 0
    detail: str = ""

    @property
    def outstanding(self) -> tuple[Posting, ...]:
        """Postings still waiting on the worker."""
        return tuple(posting for posting in self.postings if posting.outstanding)

    @property
    def arrived(self) -> tuple[Posting, ...]:
        """Postings the mailbox says reached the worker."""
        return tuple(posting for posting in self.postings if posting.arrived)


@dataclass(frozen=True)
class FleetPoll:
    """One pass over many plans, and the ones it did not reach."""

    polls: tuple[PlanPoll, ...] = ()
    deferred: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether every plan offered was actually polled."""
        return not self.deferred

    @property
    def outstanding(self) -> tuple[Posting, ...]:
        """Every posting still waiting, across every plan polled."""
        return tuple(posting for poll in self.polls for posting in poll.outstanding)


def message_id(
    *,
    plan_dir: str,
    incident_id: int | None,
    operation: str,
    target: str,
) -> str:
    """Return the mailbox id one posting always has.

    Derived rather than generated, from the four fields an action and a ledger
    row both carry, so appending twice is refused by the mailbox instead of
    producing two messages and so a crashed process's record is findable by a
    process that never saw it.

    ``incident_id`` is optional because the ledger's is:
    :class:`~agent_session_loop.overseer_store_models.ActionRecord` carries a nullable
    incident, and the derivation has to accept whatever a row actually holds or
    a reconciliation could not look up the row it is reconciling. An absent
    incident derives its own stable id, which is what keeps such a row findable
    by the same rule as any other rather than unaddressable.
    """
    name = "\x00".join((plan_dir, str(incident_id), operation, target))
    return str(uuid.uuid5(_ID_NAMESPACE, name))


def inspect_posting(
    view: mailbox_snapshot.MailboxView,
    *,
    plan_dir: str,
    incident_id: int | None,
    operation: str,
    target: str,
) -> Posting | None:
    """Project one posting's state from a mailbox snapshot, or ``None``.

    ``None`` means no record with this posting's derived id is in the mailbox:
    nothing was appended, or an append did not land. That is deliberately not a
    state, because "not posted" is the absence of a posting rather than a sixth
    thing one can be in.
    """
    identity = message_id(
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation=operation,
        target=target,
    )
    record = _message(view, identity)
    if record is None:
        return None
    if operation == "reply":
        mailbox_state = view.delivery_state_for(target)
        state: State = "consumed" if mailbox_state == "consumed" else "accepted"
    else:
        mailbox_state = view.guidance_delivery_state(identity)
        state = (
            "delivered"
            if mailbox_state in mailbox_vocabulary.GUIDANCE_DELIVERED_STATES
            else "queued"
        )
    return Posting(
        operation=_operation(operation),
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=target,
        message_id=identity,
        state=state,
        mailbox_state=mailbox_state,
        seq=record.seq,
        created_at=record.created_at,
    )


def post_reply(
    *,
    plan_dir: str,
    incident_id: int,
    question_id: str,
    text: str,
    ruling: overseer_decisions.Ruling | None,
    run_id: str,
    authority: Callable[[], overseer_decisions.Ruling | None] | None = None,
    state_dir: str | Path | None = None,
) -> Posting:
    """Answer one open question, if current user authority says it applies.

    The order is authority, then read, then revalidate the question, then
    authority again, then append -- and the second authority check is the one
    that counts, because the read happened in between and a decision that was in
    force before a read is not evidence about the moment after it.

    ``authority`` re-establishes the ruling immediately before the append.
    Omitting it means the caller asserts the ruling it passed is still current;
    supplying it is how a withdrawal made during the read is caught.
    """
    identity = message_id(
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="reply",
        target=question_id,
    )
    refused = _refused_posting(
        operation="reply",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=question_id,
        identity=identity,
    )
    granted = _check_authority(
        ruling,
        operation="reply",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except (mailbox_models.MailboxError, OSError) as error:
        return refused("mailbox_unreadable", str(error))
    landed = inspect_posting(
        view,
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="reply",
        target=question_id,
    )
    if landed is not None:
        return landed
    stale = _check_question(view, question_id=question_id, run_id=run_id)
    if stale is not None:
        return refused(*stale)
    current = ruling if authority is None else authority()
    granted = _check_authority(
        current,
        operation="reply",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    if not _same_decision(ruling, current):
        return refused(
            "authority_superseded",
            f"{_decision_id(ruling)} was replaced by {_decision_id(current)}",
        )
    try:
        message = mailbox_appenders.append_reply(
            plan_dir,
            question_id,
            text,
            state_dir=state_dir,
            message_id=identity,
        )
    except mailbox_models.MailboxError as error:
        return _after_failed_append(
            error,
            plan_dir=plan_dir,
            incident_id=incident_id,
            operation="reply",
            target=question_id,
            identity=identity,
            state_dir=state_dir,
            refused=refused,
        )
    except OSError as error:
        raise PostingUncertain(str(error), message_id=identity) from error
    return Posting(
        operation="reply",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=question_id,
        message_id=identity,
        state="accepted",
        mailbox_state="accepted",
        seq=message.seq,
        created_at=message.created_at,
    )


def post_guidance(
    *,
    plan_dir: str,
    incident_id: int,
    text: str,
    ruling: overseer_decisions.Ruling | None,
    run_id: str,
    authority: Callable[[], overseer_decisions.Ruling | None] | None = None,
    state_dir: str | Path | None = None,
) -> Posting:
    """Queue one notice for a run, if current user authority says it applies.

    A guidance record that lands is ``queued``, never ``delivered``: this module
    appends the guidance and nothing else, and the delivery ledger the harness
    seam writes is what later moves it. Reporting an append as delivery would be
    the one lie the state vocabulary exists to prevent.
    """
    target = overseer_dispatch.Guidance(text=text).target()
    identity = message_id(
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="guidance",
        target=target,
    )
    refused = _refused_posting(
        operation="guidance",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=target,
        identity=identity,
    )
    granted = _check_authority(
        ruling,
        operation="guidance",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except (mailbox_models.MailboxError, OSError) as error:
        return refused("mailbox_unreadable", str(error))
    landed = inspect_posting(
        view,
        plan_dir=plan_dir,
        incident_id=incident_id,
        operation="guidance",
        target=target,
    )
    if landed is not None:
        return landed
    current = ruling if authority is None else authority()
    granted = _check_authority(
        current,
        operation="guidance",
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
    )
    if granted is not None:
        return refused(*granted)
    if not _same_decision(ruling, current):
        return refused(
            "authority_superseded",
            f"{_decision_id(ruling)} was replaced by {_decision_id(current)}",
        )
    try:
        message = mailbox_appenders.append_guidance(
            plan_dir,
            text,
            state_dir=state_dir,
            message_id=identity,
        )
    except mailbox_models.MailboxError as error:
        return _after_failed_append(
            error,
            plan_dir=plan_dir,
            incident_id=incident_id,
            operation="guidance",
            target=target,
            identity=identity,
            state_dir=state_dir,
            refused=refused,
        )
    except OSError as error:
        raise PostingUncertain(str(error), message_id=identity) from error
    return Posting(
        operation="guidance",
        plan_dir=plan_dir,
        incident_id=incident_id,
        target=target,
        message_id=identity,
        state="queued",
        mailbox_state=mailbox_vocabulary.GUIDANCE_STATE_QUEUED,
        seq=message.seq,
        created_at=message.created_at,
    )


def poll_plan(
    *,
    plan_dir: str,
    ledger: overseer_store_ledger.Ledger,
    state_dir: str | Path | None = None,
) -> PlanPoll:
    """Read one plan's mailbox once and project every posting it holds.

    One read, no wait, no retry. A mailbox that cannot be read yields
    ``read`` false and still advances the rotation cursor, because a plan that
    fails to be polled must not sit at the head of the queue starving the
    plans behind it.
    """
    records = tuple(
        record
        for record in ledger.actions(plan_dir=plan_dir)
        if record.operation in MAIL_OPERATIONS
    )
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except (mailbox_models.MailboxError, OSError) as error:
        _stamp(ledger, plan_dir=plan_dir, position=None, marker="")
        return PlanPoll(plan_dir=plan_dir, read=False, detail=str(error))
    postings = tuple(
        posting
        for posting in (
            inspect_posting(
                view,
                plan_dir=record.plan_dir,
                incident_id=record.incident_id,
                operation=record.operation,
                target=record.target,
            )
            for record in records
        )
        if posting is not None
    )
    outstanding = tuple(posting for posting in postings if posting.outstanding)
    _stamp(
        ledger,
        plan_dir=plan_dir,
        position=view.last_seq,
        marker=outstanding[0].message_id if outstanding else "",
    )
    return PlanPoll(
        plan_dir=plan_dir,
        read=True,
        postings=postings,
        last_seq=view.last_seq,
    )


def poll_fleet(
    *,
    plan_dirs: Sequence[str],
    ledger: overseer_store_ledger.Ledger,
    state_dir: str | Path | None = None,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> FleetPoll:
    """Poll many plans in one bounded pass, least-recently-polled first.

    The rotation is the watcher's rule on this module's own cursor: a plan
    polled a moment ago sorts behind one never polled at all, and the order is
    durable, so a killed overseer's unreached plans are still at the head when
    it comes back. ``deadline`` stops the pass from *starting* another plan;
    the plans left are reported rather than dropped, so a truncated pass is
    never mistaken for a fleet with nothing outstanding.
    """
    polls: list[PlanPoll] = []
    deferred: list[str] = []
    for plan_dir in _service_order(plan_dirs, ledger):
        if deadline is not None and monotonic() >= deadline:
            deferred.append(plan_dir)
            continue
        polls.append(poll_plan(plan_dir=plan_dir, ledger=ledger, state_dir=state_dir))
    return FleetPoll(polls=tuple(polls), deferred=tuple(deferred))


Authority = Callable[[overseer_dispatch.Action], overseer_decisions.Ruling | None]


@dataclass(frozen=True)
class ReplyAdapter:
    """The ``reply`` operation, behind the dispatcher's guards.

    ``authority`` is asked for a fresh ruling every time rather than handed one
    at construction, because an adapter holding a ruling is a cached permission
    with extra steps.
    """

    authority: Authority
    state_dir: Path | None = None
    inspect: Callable[..., intervention.RunFacts] = intervention.inspect_run

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Check authority and that the question is still one this run has open."""
        if not isinstance(action.payload, overseer_dispatch.Reply):
            return overseer_dispatch.Preparation.declined(
                "payload_mismatch: not a reply"
            )
        granted = _check_authority(
            self.authority(action),
            operation="reply",
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
        )
        if granted is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*granted))
        try:
            facts = self.inspect(action.plan_dir, state_dir=self.state_dir)
        except (mailbox_models.MailboxError, OSError) as error:
            return overseer_dispatch.Preparation.declined(
                _phrase("mailbox_unreadable", str(error))
            )
        if intervention.selected_question_id(facts, action.target) is None:
            return overseer_dispatch.Preparation.declined(
                _phrase("question_unknown", action.target)
            )
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Append the reply, revalidating the question and the ruling first."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Reply):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not a reply")
            )
        try:
            posting = post_reply(
                plan_dir=action.plan_dir,
                incident_id=action.incident_id,
                question_id=payload.question_id,
                text=payload.text,
                ruling=self.authority(action),
                run_id=action.run_id,
                authority=lambda: self.authority(action),
                state_dir=self.state_dir,
            )
        except PostingUncertain as error:
            return overseer_dispatch.Receipt.uncertain(
                detail=error.detail,
                locator=error.message_id,
            )
        return _receipt(posting)

    def reconcile(
        self,
        record: overseer_store_models.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Say whether a reply a dead process may have appended is in there."""
        return _reconcile(record, operation="reply", state_dir=self.state_dir)


@dataclass(frozen=True)
class GuidanceAdapter:
    """The ``guidance`` operation, behind the dispatcher's guards."""

    authority: Authority
    state_dir: Path | None = None

    def prepare(
        self, action: overseer_dispatch.Action
    ) -> overseer_dispatch.Preparation:
        """Check authority. Guidance names no question, so there is none to check."""
        if not isinstance(action.payload, overseer_dispatch.Guidance):
            return overseer_dispatch.Preparation.declined(
                "payload_mismatch: not guidance"
            )
        granted = _check_authority(
            self.authority(action),
            operation="guidance",
            plan_dir=action.plan_dir,
            run_id=action.run_id,
            incident_id=action.incident_id,
        )
        if granted is not None:
            return overseer_dispatch.Preparation.declined(_phrase(*granted))
        return overseer_dispatch.Preparation.ok()

    def execute(self, action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
        """Append the guidance, re-establishing the ruling first."""
        payload = action.payload
        if not isinstance(payload, overseer_dispatch.Guidance):
            return overseer_dispatch.Receipt.failed(
                _phrase("payload_mismatch", "not guidance")
            )
        try:
            posting = post_guidance(
                plan_dir=action.plan_dir,
                incident_id=action.incident_id,
                text=payload.text,
                ruling=self.authority(action),
                run_id=action.run_id,
                authority=lambda: self.authority(action),
                state_dir=self.state_dir,
            )
        except PostingUncertain as error:
            return overseer_dispatch.Receipt.uncertain(
                detail=error.detail,
                locator=error.message_id,
            )
        return _receipt(posting)

    def reconcile(
        self,
        record: overseer_store_models.ActionRecord,
    ) -> overseer_dispatch.Receipt:
        """Say whether guidance a dead process may have appended is in there."""
        return _reconcile(record, operation="guidance", state_dir=self.state_dir)


def adapters(
    *,
    authority: Authority,
    state_dir: Path | None = None,
) -> overseer_dispatch.Registry:
    """Build the dispatcher registry holding exactly the two mailbox adapters.

    The other three operations are absent rather than stubbed, because
    :meth:`~agent_session_loop.overseer_dispatch.Registry.supports` is what the
    single door consults and a stub that refuses is indistinguishable from an
    adapter that failed.
    """
    return overseer_dispatch.registry(
        {
            "reply": ReplyAdapter(authority=authority, state_dir=state_dir),
            "guidance": GuidanceAdapter(authority=authority, state_dir=state_dir),
        }
    )


# -- internals -------------------------------------------------------------


def _operation(value: str) -> Operation:
    """Narrow a stored operation string to the literal type."""
    for known in MAIL_OPERATIONS:
        if known == value:
            return known
    raise ValueError(f"{value!r} is not a mailbox operation")


def _message(
    view: mailbox_snapshot.MailboxView,
    identity: str,
) -> mailbox_models.MailboxMessage | None:
    for message in view.messages:
        if message.id == identity:
            return message
    return None


def _phrase(refusal: Refusal, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


def _refused_posting(
    *,
    operation: str,
    plan_dir: str,
    incident_id: int,
    target: str,
    identity: str,
) -> Callable[[Refusal, str], Posting]:
    """Return a builder for this posting's refusals, so none can omit a reason."""

    def build(refusal: Refusal, detail: str = "") -> Posting:
        return Posting(
            operation=_operation(operation),
            plan_dir=plan_dir,
            incident_id=incident_id,
            target=target,
            message_id=identity,
            state="rejected",
            refusal=refusal,
            detail=detail,
        )

    return build


def _check_authority(
    ruling: overseer_decisions.Ruling | None,
    *,
    operation: str,
    plan_dir: str,
    run_id: str,
    incident_id: int,
) -> tuple[Refusal, str] | None:
    """Return why this ruling does not authorize this posting, or ``None``.

    Scope is checked field by field against what the caller is about to do
    rather than by :meth:`~agent_session_loop.overseer_decisions.Scope.covers`,
    because covers answers "may this decision reach that target" and the
    question here is the stricter one: was the ruling made about *this* plan,
    run and incident. A ruling that named none of them is a ruling that would
    equally authorize the next plan.
    """
    if ruling is None:
        return ("authority_absent", "no ruling was established")
    if ruling.operation != operation:
        return (
            "authority_silent",
            f"the ruling is about {ruling.operation!r}, not {operation!r}",
        )
    if ruling.refusal is not None:
        return ("authority_refused", ruling.refusal)
    if not ruling.authorized:
        return ("authority_refused", ruling.summary or "not authorized")
    if ruling.unresolved:
        return ("authority_held", ", ".join(ruling.unresolved))
    decision = ruling.decision
    if decision is None:
        return ("authority_absent", "the ruling names no governing decision")
    if not decision.speaks_to(operation):
        return (
            "authority_silent",
            f"{decision.decision_id} says nothing about {operation!r}",
        )
    if decision.stance != "allow":
        return ("authority_held", f"{decision.decision_id} is a {decision.stance}")
    scope = ruling.scope
    if not scope.named_projects:
        return ("authority_unscoped", "the ruling names no project")
    named = (
        ("plan", scope.plan_dir, plan_dir),
        ("run", scope.run_id, run_id),
        ("incident", scope.incident, str(incident_id)),
    )
    for field, ruled, wanted in named:
        if not ruled or ruled != wanted:
            return (
                "authority_unscoped",
                f"the ruling's {field} is {ruled!r}, not {wanted!r}",
            )
    if not decision.scope.covers(scope):
        return (
            "authority_unscoped",
            f"{decision.decision_id} was not made about this scope",
        )
    return None


def _decision_id(ruling: overseer_decisions.Ruling | None) -> str:
    if ruling is None or ruling.decision is None:
        return "no decision"
    return ruling.decision.decision_id


def _same_decision(
    first: overseer_decisions.Ruling | None,
    second: overseer_decisions.Ruling | None,
) -> bool:
    """Whether two rulings rest on the same governing decision, unchanged.

    The stance and the moment are compared as well as the identity, because a
    decision re-read from moved evidence can keep its id and mean the opposite.
    """
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


def _check_question(
    view: mailbox_snapshot.MailboxView,
    *,
    question_id: str,
    run_id: str,
) -> tuple[Refusal, str] | None:
    """Return why this question cannot be answered now, or ``None``.

    This is the "somebody got there first" check, and it runs against the
    mailbox rather than a cached projection because the mailbox is what the
    append itself will be judged against a moment later.
    """
    closure = view.closure_for(question_id)
    if closure is not None:
        return (
            "question_closed",
            f"{closure.kind} {closure.id} already closed it",
        )
    if not view.accepts_replies:
        return ("mailbox_full", "the mailbox is not accepting replies")
    open_ids = (
        {message.id for message in view.open_questions(run_id)}
        if run_id
        else {message.id for message in view.unanswered_questions}
    )
    if question_id not in open_ids:
        known = any(
            message.kind == mailbox_vocabulary.KIND_QUESTION
            and message.id == question_id
            for message in view.messages
        )
        if not known:
            return ("question_unknown", question_id)
        return ("question_stale", f"{question_id} is no longer open")
    return None


def _after_failed_append(
    error: mailbox_models.MailboxError,
    *,
    plan_dir: str,
    incident_id: int,
    operation: str,
    target: str,
    identity: str,
    state_dir: str | Path | None,
    refused: Callable[[Refusal, str], Posting],
) -> Posting:
    """Decide what a refused append means, re-reading before concluding anything.

    ``duplicate-id`` is the retry case and is proof our own record is already
    there; a write error is the case nobody can conclude from, so both re-read
    and only a re-read that also fails is uncertain.
    """
    if error.reason in (
        mailbox_constants.REJECT_DUPLICATE_ID,
        mailbox_constants.ERROR_MAILBOX_WRITE,
    ):
        try:
            view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
        except (mailbox_models.MailboxError, OSError) as failure:
            raise PostingUncertain(
                f"{error}; the mailbox could not be re-read: {failure}",
                message_id=identity,
            ) from failure
        landed = inspect_posting(
            view,
            plan_dir=plan_dir,
            incident_id=incident_id,
            operation=operation,
            target=target,
        )
        if landed is not None:
            return landed
        if error.reason == mailbox_constants.REJECT_DUPLICATE_ID:
            return refused("record_rejected", str(error))
        raise PostingUncertain(str(error), message_id=identity)
    return refused(
        _REFUSAL_FOR_REASON.get(error.reason, "record_rejected"),
        str(error),
    )


def _receipt(posting: Posting) -> overseer_dispatch.Receipt:
    """Translate a posting into what the dispatcher records.

    A refused posting is ``failed`` rather than a dispatcher refusal: the
    adapter ran and the mailbox said no, which is a fact about the world and
    not a decision about permission.
    """
    if posting.state == "rejected":
        assert posting.refusal is not None
        return overseer_dispatch.Receipt.failed(
            detail=_phrase(posting.refusal, posting.detail),
            locator=posting.message_id,
        )
    return overseer_dispatch.Receipt.executed(
        detail=f"{posting.state} ({posting.mailbox_state})",
        locator=posting.message_id,
    )


def _reconcile(
    record: overseer_store_models.ActionRecord,
    *,
    operation: str,
    state_dir: Path | None,
) -> overseer_dispatch.Receipt:
    """Answer "did this happen?" from the mailbox and the ledger row alone."""
    if record.operation != operation:
        return overseer_dispatch.Receipt.uncertain(
            detail=_phrase("payload_mismatch", record.operation),
        )
    identity = message_id(
        plan_dir=record.plan_dir,
        incident_id=record.incident_id,
        operation=operation,
        target=record.target,
    )
    try:
        view = mailbox.read_mailbox(record.plan_dir, state_dir=state_dir)
    except (mailbox_models.MailboxError, OSError) as error:
        return overseer_dispatch.Receipt.uncertain(
            detail=_phrase("mailbox_unreadable", str(error)),
            locator=identity,
        )
    landed = inspect_posting(
        view,
        plan_dir=record.plan_dir,
        incident_id=record.incident_id,
        operation=operation,
        target=record.target,
    )
    if landed is not None:
        return _receipt(landed)
    if operation == "reply":
        closure = view.closure_for(record.target)
        if closure is not None:
            return overseer_dispatch.Receipt.failed(
                detail=_phrase(
                    "question_closed", f"{closure.kind} {closure.id} closed it"
                ),
                locator=identity,
            )
    return overseer_dispatch.Receipt.failed(
        detail=_phrase("record_rejected", "no such record in the mailbox"),
        locator=identity,
    )


def _stamp(
    ledger: overseer_store_ledger.Ledger,
    *,
    plan_dir: str,
    position: int | None,
    marker: str,
) -> None:
    """Move this plan to the back of the rotation, whatever the poll found.

    ``position`` ``None`` means the mailbox was unreadable, so the cursor keeps
    the position it had: the stamp is about when the plan was serviced and must
    not claim a read that did not happen.
    """
    if position is None:
        existing = ledger.cursor(plan_dir=plan_dir, stream=MAILBOX_STREAM)
        position = 0 if existing is None else existing.position
    ledger.advance_cursor(
        plan_dir=plan_dir,
        stream=MAILBOX_STREAM,
        position=position,
        marker=marker,
    )


def _service_order(
    plan_dirs: Sequence[str],
    ledger: overseer_store_ledger.Ledger,
) -> tuple[str, ...]:
    """Order plans least-recently-polled first, never-polled ahead of all."""
    unique = tuple(dict.fromkeys(plan_dirs))
    stamps: dict[str, str] = {}
    for plan_dir in unique:
        cursor = ledger.cursor(plan_dir=plan_dir, stream=MAILBOX_STREAM)
        stamps[plan_dir] = "" if cursor is None else cursor.observed_at
    return tuple(sorted(unique, key=lambda plan_dir: (stamps[plan_dir], plan_dir)))
