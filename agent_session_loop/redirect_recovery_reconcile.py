"""Startup reconciliation of redirects a crashed run left behind."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    clock,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)
from .redirect_recovery_evidence import (
    BLOCKED_FOREIGN_OWNER,
    BLOCKED_JOURNAL_UNREADABLE,
    BLOCKED_LIVE_OWNER,
    OWNER_LIVE,
    OWNER_UNKNOWN,
    ConversationEvidence,
    OwnerEvidence,
    inspect_conversations,
    inspect_owner,
)

# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrandedCorrection:
    """One unfinished correction and the session a future attempt could carry it into.

    The session travels with the id rather than beside it in a parallel list:
    the two are read together every time, and a report that could disagree with
    itself about which session an id belongs to would deliver a correction to
    the wrong worker.
    """

    operation_id: str
    session_id: str

    def to_payload(self) -> dict[str, Any]:
        return {"operation_id": self.operation_id, "session_id": self.session_id}


@dataclass(frozen=True)
class ReconciliationReport:
    """What a starting run did, or declined to do, about a crash's leftovers."""

    plan_dir: str
    run_id: str
    reconciled: bool
    blocked_reason: str | None = None
    expired: tuple[str, ...] = ()
    released: tuple[str, ...] = ()
    settled: tuple[str, ...] = ()
    stranded: tuple[StrandedCorrection, ...] = ()
    recoverable: tuple[StrandedCorrection, ...] = ()
    uncertain: tuple[StrandedCorrection, ...] = ()
    torn_tail: bool = False
    unreadable_lines: int = 0
    owner: OwnerEvidence | None = None
    conversations: ConversationEvidence | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    def recoverable_for(self, session_id: str) -> tuple[str, ...]:
        """The ids a new attempt of ``session_id`` may carry, oldest first."""
        return tuple(
            item.operation_id
            for item in self.recoverable
            if item.session_id == session_id
        )

    def uncertain_for(self, session_id: str) -> tuple[str, ...]:
        """The ids delivered to an attempt of ``session_id`` that nobody receipted."""
        return tuple(
            item.operation_id
            for item in self.uncertain
            if item.session_id == session_id
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "plan_dir": self.plan_dir,
            "run_id": self.run_id,
            "reconciled": self.reconciled,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "expired": list(self.expired),
            "released": list(self.released),
            "settled": list(self.settled),
            "stranded": [item.to_payload() for item in self.stranded],
            "recoverable": [item.to_payload() for item in self.recoverable],
            "uncertain": [item.to_payload() for item in self.uncertain],
            "torn_tail": self.torn_tail,
            "unreadable_lines": self.unreadable_lines,
            "owner": None if self.owner is None else self.owner.to_payload(),
            "conversations": (
                None if self.conversations is None else self.conversations.to_payload()
            ),
        }


def _blocked(
    plan_dir: str | Path,
    run_id: str,
    reason: str,
    owner: OwnerEvidence | None = None,
) -> ReconciliationReport:
    return ReconciliationReport(
        plan_dir=str(plan_dir),
        run_id=run_id,
        reconciled=False,
        blocked_reason=reason,
        owner=owner,
    )


def reconcile_startup(
    plan_dir: str | Path,
    *,
    run_id: str,
    pending_session_ids: Sequence[str] = (),
    state_dir: str | Path | None = None,
    owner_pid: int | None = None,
    now: datetime | None = None,
) -> ReconciliationReport:
    """Settle what a crash stranded, and say what this run may pick up.

    Called once by a starting run, under that run's own lock and before the
    first turn is selected. Four things happen and nothing else does:

    - every request past its window is expired in the journal, so the refusal
      survives this process too;
    - every correction a dead run bound into a prompt but never got as far as
      declaring it about to leave the process is released. That window -- prompt
      finalized, ``launching`` not yet written -- is the one place a carry
      proves nothing about what anybody saw *and* the journal can prove it,
      and leaving the carry standing would let a crash inside it retire the
      operator's words to a turn that never ran. Releasing is safe precisely
      because ``launching`` is written on the far side of it: a correction that
      may already have gone out is not touched here. A carry written before
      that line existed has no far side -- a version-1 one replays as delivered,
      a version-2 one as launch-ambiguous -- so this step passes both by, and an
      upgraded plan reports its predecessor's carries as uncertain rather than
      replaying words that may already have been in front of a worker;
    - every *unanswered* request of a dead run whose session this plan is not
      going to run again is settled ``rejected``/``run-ended``. That reason is
      written exactly here: it means the process that owed the operator an
      answer ended without giving one, which is a different fact from the
      deadline passing and deserves a different word. One the dead run had
      already *accepted* is reported as ``stranded`` instead, because its answer
      is written and first terminal wins: the journal has no transition for
      "accepted, and then nobody was left to deliver it", and inventing one
      would overwrite the runner's own decision after the fact;
    - everything still unfinished whose session *is* pending is reported as
      recoverable, and every request a launched worker never receipted -- or
      whose launch window a dead run left open -- is reported as uncertain.
      Neither is acted on here. Recovery happens when the session is actually
      selected, because only then does a continuation attempt exist to carry a
      correction into.

    ``pending_session_ids`` is supplied by the caller rather than read from the
    plan: this module has no business deciding what a plan is going to run, and
    the run loop already holds that snapshot. An empty sequence therefore means
    "no session remains", which settles every stranded correction -- correct for
    a finished plan and the reason the caller must pass the real list.

    The refusals come first and write nothing. A plan owned by a live process
    other than this one, or by a host we cannot evaluate, is left entirely
    alone: a second reconciler settling what the first is still answering is how
    one correction acquires two outcomes.
    """
    moment = clock.utc_now() if now is None else now
    owner = inspect_owner(plan_dir, state_dir=state_dir)
    if owner.liveness == OWNER_UNKNOWN:
        return _blocked(plan_dir, run_id, BLOCKED_FOREIGN_OWNER, owner)
    if owner.liveness == OWNER_LIVE and owner.pid != _resolved_pid(owner_pid):
        return _blocked(plan_dir, run_id, BLOCKED_LIVE_OWNER, owner)

    try:
        expired = redirect_store_requests.expire_due_requests(
            plan_dir, state_dir=state_dir, now=moment
        )
    except redirect_store_models.RedirectStoreError:
        # An unreadable or unwritable journal stops reconciliation rather than
        # being worked around. Every answer below is a claim about what the
        # journal says, and a journal that cannot be read supports none of them.
        return _blocked(plan_dir, run_id, BLOCKED_JOURNAL_UNREADABLE, owner)

    ledger = inspect_conversations(plan_dir, state_dir=state_dir)
    view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    released = _release_unlaunched_carries(plan_dir, view, run_id, state_dir, moment)
    if released:
        # The releases changed what the journal says about these requests, and
        # every answer below is read off the view. Re-reading is cheaper than
        # reasoning about which of the two states each branch wanted.
        view = redirect_store_journal.read_journal(plan_dir, state_dir=state_dir)
    live_sessions = frozenset(pending_session_ids)

    settled: list[str] = []
    stranded: list[StrandedCorrection] = []
    recoverable: list[StrandedCorrection] = []
    for request in view.strandable_by(run_id):
        where = StrandedCorrection(
            operation_id=request.operation_id,
            session_id=request.target.session_id,
        )
        if request.target.session_id in live_sessions:
            recoverable.append(where)
            continue
        if request.terminal:
            # Accepted, and the session it was accepted for will not run again.
            # Settling it would be a second terminal record over the first.
            stranded.append(where)
            continue
        try:
            redirect_store_requests.reject_request(
                plan_dir,
                request.operation_id,
                reason=redirect_store_vocab.REASON_RUN_ENDED,
                state_dir=state_dir,
                now=moment,
            )
        except redirect_store_models.RedirectStoreError:
            # Settled by someone else between the read and here, or a journal
            # that just filled. Either way the request is not this run's to
            # report as settled, and nothing is lost by saying less.
            stranded.append(where)
            continue
        settled.append(request.operation_id)

    uncertain = tuple(
        StrandedCorrection(
            operation_id=request.operation_id,
            session_id=request.target.session_id,
        )
        for request in view.requests
        if request.target.run_id != run_id
        and request.accepted
        and not request.acknowledged
        and (request.delivered or request.launch_ambiguous)
    )
    return ReconciliationReport(
        plan_dir=str(plan_dir),
        run_id=run_id,
        reconciled=True,
        expired=tuple(request.operation_id for request in expired),
        released=released,
        settled=tuple(settled),
        stranded=tuple(stranded),
        recoverable=tuple(recoverable),
        uncertain=uncertain,
        torn_tail=view.torn_tail,
        unreadable_lines=len(view.rejected),
        owner=owner,
        conversations=ledger,
    )


def _release_unlaunched_carries(
    plan_dir: str | Path,
    view: redirect_store_models.RedirectJournalView,
    run_id: str,
    state_dir: str | Path | None,
    moment: datetime,
) -> tuple[str, ...]:
    """Hand back every carry a dead run provably never sent, oldest first.

    "Provably" is the whole of it. A carry the dead run had already declared it
    was about to submit is skipped, because the release would rest on a silence
    that has two readings and this module picks neither.

    Restricted to *other* runs on purpose. A carry belonging to this run at
    startup is impossible -- the run id is minted for this process -- and a rule
    that did not say so would become a live run releasing its own in-flight
    prompt the first time reconciliation was called from anywhere else.

    A refusal is swallowed rather than raised or reported: the request is simply
    left carried, which is the state it was already in, and the caller's job of
    settling a crash's leftovers should not fail over one line it could not
    append.
    """
    released: list[str] = []
    for request in view.requests:
        if request.target.run_id == run_id or not request.accepted:
            continue
        carried_into = request.continuation
        if carried_into is None or request.reached_a_worker:
            continue
        if request.launch_ambiguous:
            # The dead run declared this prompt about to go out and then went
            # silent. Handing the carry back here would be this module deciding
            # the words never left that process, which is exactly what it has no
            # evidence for. Left carried, and reported as uncertain below.
            continue
        try:
            redirect_store_delivery.release_request(
                plan_dir,
                request.operation_id,
                continuation=carried_into,
                state_dir=state_dir,
                now=moment,
            )
        except redirect_store_models.RedirectStoreError:
            continue
        released.append(request.operation_id)
    return tuple(released)


def _resolved_pid(owner_pid: int | None) -> int:
    return os.getpid() if owner_pid is None else owner_pid


# ---------------------------------------------------------------------------
# Handing recovered corrections to a continuation
# ---------------------------------------------------------------------------


def recoverable_operation_ids(
    report: ReconciliationReport,
    session_id: str,
) -> tuple[str, ...]:
    """The ids an attempt of ``session_id`` may carry across the crash.

    Gated on the conversation ledger being usable. An untrustworthy ledger means
    no conversation can be selected yet, so the turn this would deliver into is
    not going to exist; handing corrections to it would mark them delivered to
    a prompt that was never built. Reporting nothing leaves them recoverable for
    the run that starts after the ledger is repaired, which is the outcome that
    loses nothing.

    The uncertain ids are never here, and there is no flag that adds them. Their
    delivery is already written, so the journal would refuse a second one; and
    the question they raise -- did a model read this before the machine stopped
    -- is not one the journal answers. An operator who decides it did not says
    so by submitting the correction again, which produces a new request with its
    own acceptance and its own words, chosen knowing what happened.
    """
    if report.blocked or not report.reconciled:
        return ()
    if report.conversations is not None and not report.conversations.usable:
        return ()
    return report.recoverable_for(session_id)
