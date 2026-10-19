"""Overseer context models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from . import (
    mailbox_snapshot,
    mailbox_vocabulary,
    overseer_discovery,
    overseer_policy_client,
)

#: The access category every operation here asks under. Assembling context is
#: reading project-adjacent records, so ``context`` is the honest question; a
#: mutation or a dispatch is a different question a different boundary asks.
CONTEXT_ACCESS = overseer_policy_client.ACCESS_CONTEXT


#: The ``alias`` an evidence row carries for ASL's own records. Deliberately not
#: a repository alias: these are the runner's state directory, not project
#: content, and labelling them with a plan's alias would claim the evidence came
#: from a repository the authorization named.
STATE_SPACE = "asl-state"


#: The evidence family carrying the scope an incident was authorized under.
#: Deliberately not a :data:`SourceKind`: nothing projects or expands it, and it
#: is never described to an overseer. It exists because the ledger records
#: references and a repository set is not a file -- without a stored digest, a
#: later load could only recompute the scope from the run as it stands now and
#: would find it unchanged by construction, which is a check that cannot fail.
SCOPE_KIND = "scope"


#: How much of one source a packet's summary may spend.
MAX_SUMMARY_CHARS = 400


#: How many records of one family a summary names.
MAX_RECORDS = 5


#: How much of one source :func:`expand` may hand back.
MAX_EXCERPT_CHARS = 4000


#: How many records of one family :func:`expand` may name.
MAX_EXCERPT_RECORDS = 50


#: Artifact directory prefixes under ``runs/<run_id>/``.
#: ``turn_execution_artifacts`` and ``run_commit`` build these names inline
#: rather than publishing them, so they are restated here; the suffix after
#: the zero-padded index varies and is never matched on.
TURN_PREFIX = "turn-"


COMMIT_PREFIX = "commit-"


SourceKind = Literal[
    "status",
    "questions",
    "run_config",
    "verification",
    "worker_changes",
    "supervision",
]


#: The evidence families a packet assembles, in the order it assembles them.
#: Treat them as a consumer contract: they are the ``kind`` of an evidence row
#: and the selector :func:`expand` takes, so a spelling change is a contract
#: change.
#:
#: * ``status`` -- the run's own record: paused or not, why, since when.
#: * ``questions`` -- the questions this run can still consume an answer for.
#: * ``run_config`` -- workspace, registry digest and declared repositories.
#: * ``verification`` -- the most recent turn that recorded a verification.
#: * ``worker_changes`` -- the most recent commit, or the session status changes
#:   of the most recent turn when the run has not committed yet.
#: * ``supervision`` -- the guidance, resolutions and supervision closures a
#:   human contributed.
SOURCE_KINDS: tuple[SourceKind, ...] = (
    "status",
    "questions",
    "run_config",
    "verification",
    "worker_changes",
    "supervision",
)


#: The mailbox records that count as supervision. Guidance and its delivery say
#: what a human sent and whether it landed; a resolution closes a question
#: without answering it; a supervision-end says a supervising launch stopped.
SUPERVISION_KINDS: tuple[str, ...] = (
    mailbox_vocabulary.KIND_GUIDANCE,
    mailbox_vocabulary.KIND_GUIDANCE_DELIVERY,
    mailbox_vocabulary.KIND_RESOLUTION,
    mailbox_vocabulary.KIND_SUPERVISION_END,
)


GapReason = Literal["absent", "unreadable", "empty"]


#: Why a family is missing from a packet. Reported rather than omitted, because
#: a packet with five families reads as a complete picture whether the sixth was
#: empty, unwritten, or unreadable -- and those are three different incidents.
#:
#: * ``absent`` -- the run has not written this record yet.
#: * ``unreadable`` -- it exists and this boundary could not read it.
#: * ``empty`` -- it was read and has nothing to say about this run.
GAP_REASONS: tuple[GapReason, ...] = ("absent", "unreadable", "empty")


Staleness = Literal["plan_unobserved", "scope_changed", "run_replaced"]


#: Why a packet no longer describes the run it was built for.
#:
#: * ``plan_unobserved`` -- discovery can no longer read the plan's record, so
#:   there is nothing to revalidate against. Unproven, never "unchanged".
#: * ``scope_changed`` -- a repository declaration, the workspace root or the
#:   registry digest moved. The grant covered the old set.
#: * ``run_replaced`` -- a different run owns the plan now; this packet is about
#:   a run that is over.
STALENESS_REASONS: tuple[Staleness, ...] = (
    "plan_unobserved",
    "scope_changed",
    "run_replaced",
)


Refusal = Literal[
    "unauthorized",
    "policy_unobserved",
    "policy_superseded",
    "enrollment_revoked",
    "plan_unobserved",
    "scope_changed",
    "run_replaced",
    "evidence_missing",
    "source_unreadable",
]


#: Every reason a packet is not handed over. The first four are
#: :data:`agent_session_loop.overseer_store_models.WITHHOLDING_REASONS` unchanged --
#: this boundary reports the ledger's refusal rather than paraphrasing it -- the
#: next three are :data:`STALENESS_REASONS`, and the last two are this
#: boundary's own.
#:
#: * ``evidence_missing`` -- the incident carries no readable evidence row, so
#:   there is nothing to describe it with.
#: * ``source_unreadable`` -- the referenced record is gone or unparseable now.
REFUSALS: tuple[Refusal, ...] = (
    "unauthorized",
    "policy_unobserved",
    "policy_superseded",
    "enrollment_revoked",
    "plan_unobserved",
    "scope_changed",
    "run_replaced",
    "evidence_missing",
    "source_unreadable",
)


#: ``mailbox.read_mailbox`` and ``overseer_discovery.build_inventory``, as
#: parameters. Injected for the same reason the watcher injects its seams: a
#: test drives the whole boundary without a corpus this process installed.
ReadMailbox = Callable[..., mailbox_snapshot.MailboxView]


BuildInventory = Callable[..., overseer_discovery.Inventory]


#: Domain separation for :func:`scope_digest`. Length-prefixed fields under a
#: versioned prefix, so no rearrangement of aliases and roots can produce the
#: digest of a different repository set.
_SCOPE_DOMAIN = b"agent_session_loop.overseer_context.scope.v1\x00"


class ContextError(Exception):
    """A packet was asked for that cannot be assembled or named."""


@dataclass(frozen=True)
class Source:
    """One evidence reference, as this packet describes it now.

    ``digest`` is what the source hashes to at the moment it was read and
    ``recorded_digest`` is what the ledger stored when the incident was opened.
    Both are carried because their disagreement is the fact: it means the
    incident's evidence has moved under it.
    """

    kind: SourceKind
    space: str
    locator: str
    summary: str
    digest: str
    recorded_digest: str
    size: int
    truncated: bool

    @property
    def changed(self) -> bool:
        """Whether the source has moved since the incident was opened."""
        return bool(self.recorded_digest) and self.recorded_digest != self.digest


@dataclass(frozen=True)
class Gap:
    """One evidence family this packet has nothing to say about, and why."""

    kind: SourceKind
    reason: GapReason
    detail: str


@dataclass(frozen=True)
class Packet:
    """Bounded context for one incident, with the identities that scope it.

    ``refusal`` set means nothing was read: the sources are empty because this
    packet was withheld, not because the run had nothing wrong with it. The two
    are indistinguishable to a caller that only looks at ``sources``, which is
    why ``refusal`` exists rather than an empty return.
    """

    plan_dir: str
    run_id: str
    incident_id: int
    kind: str
    summary: str
    questions: tuple[str, ...]
    request_digest: str
    config_fingerprint: str
    scope: str
    sources: tuple[Source, ...]
    gaps: tuple[Gap, ...]
    captured_at: str
    withheld: int = 0
    refusal: Refusal | None = None

    @property
    def available(self) -> bool:
        """Whether this packet describes anything at all."""
        return self.refusal is None

    @property
    def complete(self) -> bool:
        """Whether every family was read and nothing was held back."""
        return self.refusal is None and not self.gaps and self.withheld == 0

    @property
    def stale(self) -> tuple[Source, ...]:
        """Every source that has changed since the incident was opened."""
        return tuple(item for item in self.sources if item.changed)

    def source(self, kind: SourceKind) -> Source | None:
        """This packet's reference for one family, if it has one."""
        for item in self.sources:
            if item.kind == kind:
                return item
        return None

    def gap(self, kind: SourceKind) -> Gap | None:
        """Why one family is missing, if it is."""
        for item in self.gaps:
            if item.kind == kind:
                return item
        return None


@dataclass(frozen=True)
class Excerpt:
    """One source, expanded, bounded, and read fresh under a live grant."""

    kind: SourceKind
    space: str
    locator: str
    text: str
    digest: str
    recorded_digest: str
    size: int
    truncated: bool
    refusal: Refusal | None = None

    @property
    def available(self) -> bool:
        """Whether there is any text here."""
        return self.refusal is None

    @property
    def changed(self) -> bool:
        """Whether the source has moved since the incident was opened."""
        return bool(self.recorded_digest) and self.recorded_digest != self.digest


@dataclass(frozen=True)
class Revalidation:
    """Whether a packet still describes the run it was built for."""

    plan_dir: str
    fresh: bool
    reason: Staleness | None
    detail: str
    recorded_scope: str
    current_scope: str


@dataclass(frozen=True)
class _Projection:
    """One family, rendered short and rendered long, from one read."""

    summary: str
    detail: str
    records: int
    shown: int
    identities: tuple[str, ...] = ()
