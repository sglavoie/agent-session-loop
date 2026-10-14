"""Vocabularies, errors, and record types for the overseer ledger.

Everything here is a plain value: the closed status and withholding
vocabularies, the two ledger exceptions, and the frozen dataclasses a
:class:`~agent_session_loop.overseer_store_ledger.Ledger` hands back. None of
it touches SQLite. See ``overseer_store_ledger`` for the design the records
exist to serve: a stored decision has no conversion to a grant, and an
``attempted`` action is never ``confirmed``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

#: The ledger file, directly under ASL's XDG state root beside the per-plan
#: state directories. See the module docstring for why it is not a directory.
LEDGER_FILE_NAME = "overseer-ledger.sqlite3"

IncidentStatus = Literal["open", "resolved", "escalated", "abandoned"]

#: Every state an incident can be in. ``escalated`` is the one the shared
#: requirements name explicitly: an operation the overseer is not allowed to
#: perform stops here rather than being attempted in a weaker form.
INCIDENT_STATUSES: tuple[IncidentStatus, ...] = (
    "open",
    "resolved",
    "escalated",
    "abandoned",
)

ActionStatus = Literal["attempted", "executed", "failed", "abandoned"]

#: Every state a dispatched action can be in.
#:
#: * ``attempted`` -- written before the dispatch and never advanced by anything
#:   but a receipt. It is the in-doubt state, and a restart leaves it there.
#: * ``executed`` -- a receipt confirms the dispatch actually happened.
#: * ``failed`` -- a receipt confirms it did not.
#: * ``abandoned`` -- the overseer gave up asking; still not a claim about
#:   whether the action landed.
ACTION_STATUSES: tuple[ActionStatus, ...] = (
    "attempted",
    "executed",
    "failed",
    "abandoned",
)

Withholding = Literal[
    "unauthorized",
    "policy_unobserved",
    "policy_superseded",
    "enrollment_revoked",
]

#: Every reason stored project evidence is not handed back. Treat them as a
#: consumer contract: an operator surface reads them.
#:
#: * ``unauthorized`` -- no grant, a grant for another plan, a grant for another
#:   access category, or a grant carrying no request identity.
#: * ``policy_unobserved`` -- the ledger has not been told which configuration
#:   is in force, so it cannot tell whether this grant was issued under it.
#: * ``policy_superseded`` -- the grant names a configuration that is no longer
#:   the one in force.
#: * ``enrollment_revoked`` -- the plan's last answer from AgentMux was a
#:   refusal, or it was revoked explicitly.
WITHHOLDING_REASONS: tuple[Withholding, ...] = (
    "unauthorized",
    "policy_unobserved",
    "policy_superseded",
    "enrollment_revoked",
)


class LedgerError(Exception):
    """The ledger could not be opened, migrated, or used as asked."""


class LedgerRefused(LedgerError):
    """A write was refused because the grant presented did not permit it.

    Raised rather than returned. A read that is withheld still leaves the
    caller with a usable, honestly empty result; a write that was not permitted
    has no such result, and a return value would be one more thing a caller can
    forget to check.
    """


@dataclass(frozen=True)
class PolicyEpoch:
    """The configuration the ledger currently believes is in force.

    ``epoch`` is zero and the fingerprint empty until :meth:`Ledger.note_policy`
    is called, which is a state in which no stored evidence is readable: not
    knowing which policy applies is never read as any policy applying.
    """

    epoch: int
    config_fingerprint: str
    observed_at: str

    @property
    def observed(self) -> bool:
        """Whether any configuration has been recorded at all."""
        return self.epoch > 0 and bool(self.config_fingerprint)


@dataclass(frozen=True)
class PolicyTransition:
    """What one observation of the current configuration did to the ledger."""

    epoch: int
    config_fingerprint: str
    previous_fingerprint: str
    changed: bool
    sealed_evidence: int


@dataclass(frozen=True)
class EnrollmentRecord:
    """The last answer AgentMux gave about one plan.

    An observation, never a permission. It carries no request digest that could
    be replayed and no method that turns it into a grant; a caller wanting to
    act asks ``overseer_policy_client.authorize`` again.
    """

    plan_dir: str
    enrolled: bool
    revoked: bool
    epoch: int
    config_fingerprint: str
    access: str
    last_failure: str | None
    first_seen_at: str
    observed_at: str


@dataclass(frozen=True)
class WatchLease:
    """One watcher's claim on one plan, as the ledger holds it.

    ``watcher_id`` is empty on a lease that was released: the row stays so the
    run it last named survives, but it is owned by nobody and the next claim
    takes it without waiting. ``expires_at`` is epoch seconds, and it is the
    *only* thing that lets a claim be taken from a watcher that did not release
    it -- see :meth:`Ledger.claim_watch`.
    """

    plan_dir: str
    watcher_id: str
    run_id: str
    host: str
    pid: int
    expires_at: float
    claimed_at: str
    renewed_at: str
    released_at: str | None

    @property
    def held(self) -> bool:
        """Whether anybody owns this lease at all."""
        return bool(self.watcher_id)

    def expired(self, now: float) -> bool:
        """Whether an unreleased claim has run out of time."""
        return self.expires_at <= now


@dataclass(frozen=True)
class WatchClaim:
    """What one attempt to own a plan did.

    ``previous_run_id`` is what makes a superseded run visible: the lease
    remembers which run the last watcher was watching, so a plan whose record
    now names a different one is a new question rather than a continuation of
    the old one.
    """

    plan_dir: str
    watcher_id: str
    run_id: str
    granted: bool
    took_over: bool
    previous_watcher_id: str
    previous_run_id: str
    expires_at: float
    holder: WatchLease | None

    @property
    def superseded_run(self) -> str:
        """The run this claim replaced, or ``""`` when it continues one."""
        if not self.granted or not self.previous_run_id:
            return ""
        return "" if self.previous_run_id == self.run_id else self.previous_run_id


@dataclass(frozen=True)
class ObservationCursor:
    """How far the overseer has read one stream of one plan."""

    plan_dir: str
    stream: str
    position: int
    marker: str
    observed_at: str


@dataclass(frozen=True)
class EvidenceInput:
    """One evidence reference, as the caller supplies it.

    A *reference*: an alias-qualified locator and a digest, not the content. The
    ledger records where the evidence was and what it hashed to, so a later read
    can be checked against the source rather than trusting a copy that outlived
    the authorization it was made under.
    """

    alias: str
    kind: str
    locator: str
    digest: str


@dataclass(frozen=True)
class EvidenceRecord:
    """One stored evidence reference, with the policy it was gathered under."""

    evidence_id: int
    incident_id: int
    plan_dir: str
    alias: str
    kind: str
    locator: str
    digest: str
    epoch: int
    config_fingerprint: str
    sealed: bool
    recorded_at: str


@dataclass(frozen=True)
class EvidenceView:
    """The evidence a caller was allowed to see, and how much it was not.

    ``withheld`` is reported rather than silently omitted: a partial view that
    reads as a whole one is how an overseer concludes there is nothing to act
    on when in fact it was not allowed to look.
    """

    plan_dir: str
    records: tuple[EvidenceRecord, ...]
    withheld: int
    refusal: Withholding | None

    @property
    def complete(self) -> bool:
        """Whether every stored row for this plan is in ``records``."""
        return self.refusal is None and self.withheld == 0


@dataclass(frozen=True)
class Release:
    """The outcome of re-authorizing a plan's sealed evidence."""

    plan_dir: str
    released: int
    refusal: Withholding | None


@dataclass(frozen=True)
class IncidentRecord:
    """One operational blocker the overseer observed on a run."""

    incident_id: int
    plan_dir: str
    run_id: str
    kind: str
    status: IncidentStatus
    summary: str
    epoch: int
    config_fingerprint: str
    opened_at: str
    settled_at: str | None


@dataclass(frozen=True)
class DecisionRecord:
    """One authorization answer, as it was recorded.

    Deliberately not convertible to a grant. See the module docstring: this
    type existing without that conversion is what makes "reuse the last
    successful authorization" unrepresentable rather than merely discouraged.
    """

    decision_id: int
    plan_dir: str
    access: str
    allowed: bool
    available: bool
    failure: str | None
    reason: str
    summary: str
    config_fingerprint: str
    request_digest: str
    recorded_at: str


@dataclass(frozen=True)
class ReceiptRecord:
    """The confirmation that an attempted action actually happened, or did not."""

    receipt_id: int
    action_id: int
    outcome: str
    detail: str
    locator: str
    confirmed_at: str


@dataclass(frozen=True)
class ActionRecord:
    """One action the overseer dispatched, and what is known about its fate."""

    action_id: int
    plan_dir: str
    incident_id: int | None
    operation: str
    target: str
    status: ActionStatus
    epoch: int
    config_fingerprint: str
    request_digest: str
    progress_token: str
    attempted_at: str
    settled_at: str | None
    receipt: ReceiptRecord | None

    @property
    def confirmed(self) -> bool:
        """Whether a receipt proves what became of this dispatch.

        ``attempted`` is never confirmed, however long ago it was written. Age
        is not evidence.
        """
        return self.receipt is not None and self.status in ("executed", "failed")

    @property
    def in_doubt(self) -> bool:
        """Whether the dispatch's outcome is genuinely unknown."""
        return self.status == "attempted"


@dataclass(frozen=True)
class CheckpointGroupRecord:
    """A multi-repository checkpoint and how much of it is still owed.

    ``record`` is opaque here on purpose. The ledger's stake in a group is that
    it survives the process, is found again by the identity a retry can derive,
    and reports which repositories are still outstanding; what a snapshot token
    or a commit sha *means* belongs to the boundary that made them, and a table
    that understood the shape would have to be migrated every time that boundary
    learned something new.
    """

    group_key: str
    plan_dir: str
    incident_id: int | None
    target: str
    status: str
    phase: str
    epoch: int
    config_fingerprint: str
    request_digest: str
    record: dict[str, Any]
    opened_at: str
    updated_at: str

    @property
    def settled(self) -> bool:
        """Whether every repository in the group reached an outcome."""
        return self.status == "complete"

    @property
    def outstanding(self) -> tuple[str, ...]:
        """The repository keys this group still owes a commit, in order."""
        remaining = self.record.get("remaining_aliases")
        if not isinstance(remaining, list):
            return ()
        return tuple(str(alias) for alias in remaining)

    @property
    def committed(self) -> tuple[str, ...]:
        """The repository keys this group has already committed, in order."""
        rows = self.record.get("successful_repositories")
        if not isinstance(rows, list):
            return ()
        return tuple(
            str(row["key"])
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("key"), str)
        )
