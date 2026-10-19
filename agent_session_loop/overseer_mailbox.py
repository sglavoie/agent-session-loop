"""Nonblocking mailbox adapters and fleet polling.

Posting, authority checks, and outcome records live in the sibling
``overseer_mailbox_*`` modules."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import (
    intervention,
    mailbox,
    mailbox_models,
    overseer_dispatch,
    overseer_store_ledger,
    overseer_store_models,
)
from .overseer_mailbox_authority import _check_authority
from .overseer_mailbox_models import (
    MAIL_OPERATIONS,
    MAILBOX_STREAM,
    Authority,
    FleetPoll,
    PlanPoll,
    Posting,
    PostingUncertain,
    Refusal,
)
from .overseer_mailbox_models import REFUSALS as REFUSALS
from .overseer_mailbox_models import STATES as STATES
from .overseer_mailbox_models import Operation as Operation
from .overseer_mailbox_models import State as State
from .overseer_mailbox_posting import (
    inspect_posting,
    message_id,
    post_guidance,
    post_reply,
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


def _phrase(refusal: Refusal, detail: str) -> str:
    return f"{refusal}: {detail}" if detail else refusal


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
