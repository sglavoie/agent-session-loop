"""Incident context for the overseer: bounded, referenced, and re-authorized.

The watcher establishes that a run is blocked. This is the boundary that says
*what about it* -- and almost every decision here is about handing over less
than was asked for.

## A packet is references plus projections, never a copy

:func:`build_packet` reads six families of record -- the run's structured
status, its open questions, its run configuration, its verification results,
what the worker changed, and the supervision records that bear on it -- and
writes into the ledger exactly what
:class:`agent_session_loop.overseer_store.EvidenceInput` allows: a locator and
a digest. No content is stored. The consequence is the point: a packet cannot
outlive the authorization it was assembled under, because there is nothing
durable to outlive it with. Every later read goes back to the source, under a
fresh grant, or it does not happen.

That is also why a summary is regenerated rather than recalled. Loading a
packet re-reads each recorded source and re-summarizes it, so what an overseer
is shown describes the incident *now*: a question answered since the packet was
built no longer appears in it, and the source it came from is reported as
``changed`` rather than quietly served from a stale copy. A stored summary
would be a copy of project content by another name, and would say what was true
once with the confidence of what is true now.

## Three identities, and what each one refuses

A packet carries the plan, the run, and the question identities that make its
evidence applicable, plus the ``request_digest`` and ``config_fingerprint`` of
the grant that produced it. :func:`revalidate` compares a packet against a
freshly discovered candidate and rejects it when the *scope* moved -- a changed
repository declaration, a moved workspace root, a re-registered digest -- and
separately when the run identity moved. Scope is checked first: the grant was
issued for a repository set, so a declaration that has moved invalidates the
permission itself rather than merely the evidence it covered.

Exclusion is a different refusal and a stronger one. Loading and expanding both
gate through
:meth:`agent_session_loop.overseer_store.Ledger.read_evidence`, so a revoked
enrollment, an unobserved policy and a superseded one withhold the rows before
any file is touched -- and a revoked plan can never produce the fresh
``context`` grant that would unseal them. "Cannot be loaded or expanded after
exclusion" is therefore an operation with no success path rather than a rule
somebody has to remember.

## Bounds

Summaries are one clipped sentence and expansion is opt-in, one source at a
time, capped by :data:`MAX_EXCERPT_CHARS` and :data:`MAX_EXCERPT_RECORDS`. A
plan's mailbox and a run's turn artifacts are both unbounded in principle, and
an overseer that loads one of them whole has spent its own context on the first
incident it looked at. What was left out is counted rather than dropped:
:attr:`Source.truncated` says the summary does not name everything, and a
family that could not be read at all becomes a :class:`Gap` rather than an
absence the reader has to notice.

Durable records this boundary writes: **ledger rows only** -- one incident and
its evidence references. It writes nothing into any plan's state directory and
nothing into any repository; every plan-side path it knows is opened read-only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    clock,
    events,
    mailbox,
    mailbox_constants,
    mailbox_models,
    mailbox_snapshot,
    mailbox_vocabulary,
    overseer_discovery,
    overseer_policy_client,
    overseer_store,
    state,
)

__all__ = [
    "CONTEXT_ACCESS",
    "GAP_REASONS",
    "MAX_EXCERPT_CHARS",
    "MAX_EXCERPT_RECORDS",
    "MAX_RECORDS",
    "MAX_SUMMARY_CHARS",
    "REFUSALS",
    "SOURCE_KINDS",
    "STALENESS_REASONS",
    "STATE_SPACE",
    "SUPERVISION_KINDS",
    "ContextError",
    "Excerpt",
    "Gap",
    "Packet",
    "Revalidation",
    "Source",
    "build_packet",
    "expand",
    "load_packet",
    "revalidate",
    "scope_digest",
]

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
#: :data:`agent_session_loop.overseer_store.WITHHOLDING_REASONS` unchanged --
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


def scope_digest(candidate: overseer_discovery.Candidate) -> str:
    """Digest the identity an authorization was issued against.

    Covers the plan, the workspace root, the registry digest and every declared
    repository's alias and resolved root, in declaration order -- the same set
    and the same order the policy request is keyed by. It deliberately does
    *not* cover the run's lifecycle: a run that pauses while a packet is held
    has not changed scope, it has changed state, and conflating the two would
    make every packet stale the moment the thing it describes happens.
    """
    parts: list[str] = [
        candidate.plan_dir or "",
        candidate.workspace_root or "",
        candidate.registry_digest or "",
    ]
    for declaration in candidate.repositories:
        parts.append(declaration.alias)
        parts.append(declaration.resolved_root)
    digest = hashlib.sha256(_SCOPE_DOMAIN)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(f"{len(encoded)}:".encode("ascii"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def revalidate(
    *,
    packet: Packet,
    candidate: overseer_discovery.Candidate | None,
) -> Revalidation:
    """Compare a packet against the plan as discovery sees it now.

    Metadata only, and no grant: this reads nothing but the identity discovery
    already publishes without authorization, so a caller can find out that a
    packet is stale without asking AgentMux for permission to look at a run
    whose scope it no longer covers.
    """
    if candidate is None or not candidate.record_observed:
        return Revalidation(
            plan_dir=packet.plan_dir,
            fresh=False,
            reason="plan_unobserved",
            detail=(
                "discovery no longer reads a record for this plan, so there is "
                "nothing to revalidate the packet against"
            ),
            recorded_scope=packet.scope,
            current_scope="",
        )
    current = scope_digest(candidate)
    if current != packet.scope:
        return Revalidation(
            plan_dir=packet.plan_dir,
            fresh=False,
            reason="scope_changed",
            detail=(
                "the workspace, registry digest or declared repositories moved "
                "since this packet was authorized"
            ),
            recorded_scope=packet.scope,
            current_scope=current,
        )
    if (candidate.run_id or "") != packet.run_id:
        return Revalidation(
            plan_dir=packet.plan_dir,
            fresh=False,
            reason="run_replaced",
            detail=(
                f"the plan now runs {candidate.run_id or 'no run'}, not {packet.run_id}"
            ),
            recorded_scope=packet.scope,
            current_scope=current,
        )
    return Revalidation(
        plan_dir=packet.plan_dir,
        fresh=True,
        reason=None,
        detail="",
        recorded_scope=packet.scope,
        current_scope=current,
    )


def build_packet(
    *,
    grant: overseer_policy_client.Grant | None,
    ledger: overseer_store.Ledger,
    candidate: overseer_discovery.Candidate,
    kind: str,
    summary: str,
    state_dir: str | Path | None = None,
    now: Callable[[], str] = clock.utc_iso_now,
    read_mailbox: ReadMailbox = mailbox.read_mailbox,
    max_records: int = MAX_RECORDS,
    max_summary_chars: int = MAX_SUMMARY_CHARS,
) -> Packet:
    """Open an incident and assemble bounded context for it.

    The grant is checked twice before anything is read: once here, so an absent
    or mismatched one costs no file access at all, and once by the ledger, whose
    gate also covers revocation and the policy epoch. Between them the grant's
    fingerprint is recorded as the policy in force -- the grant is live, so by
    definition it is -- which is what makes a configuration change seal the
    evidence gathered under the old one instead of letting this packet inherit
    it silently.
    """
    plan_dir = candidate.plan_dir
    run_id = candidate.run_id
    if not candidate.eligible or not plan_dir or not run_id:
        raise ContextError(
            "context can only be assembled for a run discovery identified; "
            f"{candidate.state_dir} is ineligible "
            f"({', '.join(candidate.ineligible_reasons) or 'no reason recorded'})"
        )
    scope = scope_digest(candidate)
    captured_at = now()

    if (
        grant is None
        or grant.plan_dir != plan_dir
        or grant.access != CONTEXT_ACCESS
        or not grant.request_digest
        or not grant.config_fingerprint
    ):
        return _withheld(
            plan_dir=plan_dir,
            run_id=run_id,
            incident_id=0,
            kind=kind,
            summary=summary,
            grant=None,
            scope=scope,
            captured_at=captured_at,
            refusal="unauthorized",
            withheld=0,
        )

    ledger.note_policy(config_fingerprint=grant.config_fingerprint)
    gate = ledger.read_evidence(plan_dir=plan_dir, grant=grant)
    if gate.refusal is not None:
        return _withheld(
            plan_dir=plan_dir,
            run_id=run_id,
            incident_id=0,
            kind=kind,
            summary=summary,
            grant=grant,
            scope=scope,
            captured_at=captured_at,
            refusal=gate.refusal,
            withheld=gate.withheld,
        )

    reader = _Reader(
        plan_dir=plan_dir,
        run_id=run_id,
        state_dir=state_dir,
        read_mailbox=read_mailbox,
    )
    sources: list[Source] = []
    gaps: list[Gap] = []
    evidence: list[overseer_store.EvidenceInput] = []
    questions: tuple[str, ...] = ()

    for source_kind in SOURCE_KINDS:
        locator = reader.locate(source_kind)
        if locator is None:
            gaps.append(
                Gap(
                    kind=source_kind,
                    reason="absent",
                    detail=_ABSENT_DETAIL[source_kind],
                )
            )
            continue
        digest, size = reader.fingerprint(locator)
        projection = reader.project(
            source_kind,
            locator,
            max_records=max_records,
            max_summary_chars=max_summary_chars,
        )
        if digest is None or projection is None:
            gaps.append(
                Gap(
                    kind=source_kind,
                    reason="unreadable",
                    detail=f"{locator} could not be read as the record it should be",
                )
            )
            continue
        if projection.records == 0:
            gaps.append(
                Gap(
                    kind=source_kind,
                    reason="empty",
                    detail=projection.summary,
                )
            )
            continue
        if source_kind == "questions":
            questions = projection.identities
        sources.append(
            Source(
                kind=source_kind,
                space=STATE_SPACE,
                locator=locator,
                summary=projection.summary,
                digest=digest,
                recorded_digest=digest,
                size=size,
                truncated=projection.shown < projection.records,
            )
        )
        evidence.append(
            overseer_store.EvidenceInput(
                alias=STATE_SPACE,
                kind=source_kind,
                locator=locator,
                digest=digest,
            )
        )

    # Recorded ahead of the sources so it survives even a packet that found
    # nothing to describe: an incident with no readable evidence is still an
    # incident about one repository set, and a later load has to be able to say
    # so. ``locate`` is not consulted -- the scope came from discovery, not from
    # a file, and naming the state record here says where it was declared.
    evidence.insert(
        0,
        overseer_store.EvidenceInput(
            alias=STATE_SPACE,
            kind=SCOPE_KIND,
            locator=events.STATE_FILE_NAME,
            digest=scope,
        ),
    )

    try:
        incident = ledger.open_incident(
            grant=grant,
            plan_dir=plan_dir,
            run_id=run_id,
            kind=kind,
            summary=summary,
            evidence=tuple(evidence),
        )
    except overseer_store.LedgerRefused:
        # The gate above passed and this one did not, so the policy moved while
        # the sources were being read. Nothing was written; report the refusal
        # the ledger would report now rather than the one it would have then.
        refusal = ledger.read_evidence(plan_dir=plan_dir, grant=grant).refusal
        return _withheld(
            plan_dir=plan_dir,
            run_id=run_id,
            incident_id=0,
            kind=kind,
            summary=summary,
            grant=grant,
            scope=scope,
            captured_at=captured_at,
            refusal=refusal or "unauthorized",
            withheld=0,
        )

    return Packet(
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident.incident_id,
        kind=incident.kind,
        summary=incident.summary,
        questions=questions,
        request_digest=grant.request_digest,
        config_fingerprint=incident.config_fingerprint,
        scope=scope,
        sources=tuple(sources),
        gaps=tuple(gaps),
        captured_at=captured_at,
    )


def load_packet(
    *,
    grant: overseer_policy_client.Grant | None,
    ledger: overseer_store.Ledger,
    incident_id: int,
    state_dir: str | Path | None = None,
    now: Callable[[], str] = clock.utc_iso_now,
    read_mailbox: ReadMailbox = mailbox.read_mailbox,
    build_inventory: BuildInventory = overseer_discovery.build_inventory,
    max_records: int = MAX_RECORDS,
    max_summary_chars: int = MAX_SUMMARY_CHARS,
) -> Packet:
    """Rebuild one incident's packet from its evidence references.

    Three refusals in order, and each of them happens before the next thing is
    read. The ledger's gate first, so an excluded plan's sources are never
    opened. Then revalidation, so a packet whose scope has moved is refused
    rather than described under a permission that covered a different
    repository set. Only then the sources, re-read and re-summarized, so what
    comes back is the incident as it stands and not as it was filed.
    """
    incident = ledger.incident(incident_id)
    if incident is None:
        raise ContextError(f"no incident {incident_id} to load context for")

    scope = ""
    captured_at = now()
    view = ledger.read_evidence(plan_dir=incident.plan_dir, grant=grant)
    if view.refusal is not None:
        return _withheld(
            plan_dir=incident.plan_dir,
            run_id=incident.run_id,
            incident_id=incident_id,
            kind=incident.kind,
            summary=incident.summary,
            grant=grant,
            scope=scope,
            captured_at=captured_at,
            refusal=view.refusal,
            withheld=view.withheld,
        )

    rows = tuple(row for row in view.records if row.incident_id == incident_id)
    if not rows:
        return _withheld(
            plan_dir=incident.plan_dir,
            run_id=incident.run_id,
            incident_id=incident_id,
            kind=incident.kind,
            summary=incident.summary,
            grant=grant,
            scope=scope,
            captured_at=captured_at,
            refusal="evidence_missing",
            withheld=view.withheld,
        )

    candidate = _candidate_for(
        incident.plan_dir, build_inventory=build_inventory, state_dir=state_dir
    )
    scope = _first(*(row.digest for row in rows if row.kind == SCOPE_KIND))
    probe = Packet(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        incident_id=incident_id,
        kind=incident.kind,
        summary=incident.summary,
        questions=(),
        request_digest=_request_digest(grant),
        config_fingerprint=incident.config_fingerprint,
        scope=scope,
        sources=(),
        gaps=(),
        captured_at=captured_at,
    )
    checked = revalidate(packet=probe, candidate=candidate)
    if not checked.fresh and checked.reason is not None:
        return _withheld(
            plan_dir=incident.plan_dir,
            run_id=incident.run_id,
            incident_id=incident_id,
            kind=incident.kind,
            summary=incident.summary,
            grant=grant,
            scope=scope,
            captured_at=captured_at,
            refusal=checked.reason,
            withheld=view.withheld,
        )

    reader = _Reader(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        state_dir=state_dir,
        read_mailbox=read_mailbox,
    )
    sources: list[Source] = []
    gaps: list[Gap] = []
    questions: tuple[str, ...] = ()
    for row in rows:
        if row.kind == SCOPE_KIND:
            continue
        source_kind = _source_kind(row.kind)
        if source_kind is None:
            gaps.append(
                Gap(
                    kind="status",
                    reason="unreadable",
                    detail=(
                        f"evidence {row.evidence_id} names family {row.kind!r}, "
                        "which this build does not know how to describe"
                    ),
                )
            )
            continue
        digest, size = reader.fingerprint(row.locator)
        projection = reader.project(
            source_kind,
            row.locator,
            max_records=max_records,
            max_summary_chars=max_summary_chars,
        )
        if digest is None or projection is None:
            gaps.append(
                Gap(
                    kind=source_kind,
                    reason="unreadable",
                    detail=(
                        f"{row.locator} was recorded as evidence and cannot be read now"
                    ),
                )
            )
            continue
        if source_kind == "questions":
            questions = projection.identities
        sources.append(
            Source(
                kind=source_kind,
                space=row.alias,
                locator=row.locator,
                summary=projection.summary,
                digest=digest,
                recorded_digest=row.digest,
                size=size,
                truncated=projection.shown < projection.records,
            )
        )

    return Packet(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        incident_id=incident_id,
        kind=incident.kind,
        summary=incident.summary,
        questions=questions,
        request_digest=_request_digest(grant),
        config_fingerprint=incident.config_fingerprint,
        scope=scope,
        sources=tuple(sources),
        gaps=tuple(gaps),
        captured_at=captured_at,
        withheld=view.withheld,
    )


def expand(
    *,
    grant: overseer_policy_client.Grant | None,
    ledger: overseer_store.Ledger,
    incident_id: int,
    kind: SourceKind,
    state_dir: str | Path | None = None,
    read_mailbox: ReadMailbox = mailbox.read_mailbox,
    limit: int = MAX_EXCERPT_CHARS,
    max_records: int = MAX_EXCERPT_RECORDS,
) -> Excerpt:
    """Read one of an incident's sources at greater length, under a fresh grant.

    Expansion is per source and re-authorized every time, so an overseer spends
    its context on the one family it decided it needed rather than on whatever
    the packet happened to reference. The excerpt is built from the source as it
    is now and reports ``changed`` when that disagrees with the digest the
    incident recorded: serving the recorded digest's content is impossible --
    the ledger never had it -- and serving current content under a stale digest
    silently would be worse than saying so.
    """
    incident = ledger.incident(incident_id)
    if incident is None:
        raise ContextError(f"no incident {incident_id} to expand")
    if kind not in SOURCE_KINDS:
        raise ContextError(
            f"{kind!r} is not an evidence family; use one of {SOURCE_KINDS}"
        )

    view = ledger.read_evidence(plan_dir=incident.plan_dir, grant=grant)
    if view.refusal is not None:
        return _unavailable(kind=kind, locator="", refusal=view.refusal)

    row = next(
        (
            record
            for record in view.records
            if record.incident_id == incident_id and record.kind == kind
        ),
        None,
    )
    if row is None:
        return _unavailable(kind=kind, locator="", refusal="evidence_missing")

    reader = _Reader(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        state_dir=state_dir,
        read_mailbox=read_mailbox,
    )
    digest, size = reader.fingerprint(row.locator)
    projection = reader.project(
        kind,
        row.locator,
        max_records=max_records,
        max_summary_chars=MAX_SUMMARY_CHARS,
    )
    if digest is None or projection is None:
        return _unavailable(kind=kind, locator=row.locator, refusal="source_unreadable")

    text = projection.detail
    clipped = len(text) > limit
    if clipped:
        text = text[:limit].rstrip()
    return Excerpt(
        kind=kind,
        space=row.alias,
        locator=row.locator,
        text=text,
        digest=digest,
        recorded_digest=row.digest,
        size=size,
        truncated=clipped or projection.shown < projection.records,
    )


# -- assembly --------------------------------------------------------------


@dataclass(frozen=True)
class _Projection:
    """One family, rendered short and rendered long, from one read."""

    summary: str
    detail: str
    records: int
    shown: int
    identities: tuple[str, ...] = ()


#: What an absent family means, per family. Written out rather than generated
#: because "the run has not committed yet" and "the run has no turns yet" are
#: different things for an operator to read.
_ABSENT_DETAIL: dict[SourceKind, str] = {
    "status": "the run has published no state record",
    "questions": "the plan has no mailbox",
    "run_config": "the run has published no state record",
    "verification": "no turn of this run has recorded a verification",
    "worker_changes": "this run has recorded neither a commit nor a turn",
    "supervision": "the plan has no mailbox",
}


class _Reader:
    """One plan's records, read at most once each and never written.

    Every path it opens is derived from the plan's own state directory and a
    locator the ledger stored, and :meth:`_resolve` refuses a locator that
    escapes it -- a row is data, and data that names ``../../`` somewhere else
    is not a reference this boundary follows.
    """

    def __init__(
        self,
        *,
        plan_dir: str,
        run_id: str,
        state_dir: str | Path | None,
        read_mailbox: ReadMailbox,
    ) -> None:
        self.plan_dir = plan_dir
        self.run_id = run_id
        self.state_dir = state_dir
        self.plan_state = state.plan_state_dir(plan_dir, state_dir)
        self._read_mailbox = read_mailbox
        self._records: dict[str, dict[str, Any] | None] = {}
        self._view: mailbox_snapshot.MailboxView | None = None
        self._view_read = False

    # -- locating ----------------------------------------------------------

    def locate(self, kind: SourceKind) -> str | None:
        """Where this run currently keeps one family, relative to its state dir."""
        if kind in ("status", "run_config"):
            path = self.plan_state / events.STATE_FILE_NAME
            return events.STATE_FILE_NAME if path.is_file() else None
        if kind in ("questions", "supervision"):
            path = self.plan_state / mailbox_constants.MAILBOX_FILE_NAME
            return mailbox_constants.MAILBOX_FILE_NAME if path.is_file() else None
        if kind == "verification":
            return self._latest_turn(requiring="verification")
        return self._latest_commit() or self._latest_turn(requiring="status_changes")

    def _latest_turn(self, *, requiring: str) -> str | None:
        """The newest turn artifact carrying ``requiring``, newest first.

        Newest first rather than newest only: a turn that paused before it
        verified writes no verification block, and reporting "no verification"
        for a run that verified three turns ago would describe the artifact
        layout rather than the run.
        """
        for directory in reversed(self._artifacts(TURN_PREFIX)):
            locator = self._relative(directory / state.RESULT_FILE_NAME)
            record = self.record(locator)
            if record is not None and record.get(requiring):
                return locator
        return None

    def _latest_commit(self) -> str | None:
        """The newest commit artifact of this run, if it has committed."""
        for directory in reversed(self._artifacts(COMMIT_PREFIX)):
            locator = self._relative(directory / state.RESULT_FILE_NAME)
            if self.record(locator) is not None:
                return locator
        return None

    def _artifacts(self, prefix: str) -> list[Path]:
        """This run's artifact directories under one prefix, oldest first.

        Sorted by name, which orders them by the zero-padded index the runner
        writes, so "newest" needs no timestamps and no ``stat`` call.
        """
        run_dir = self.plan_state / state.RUNS_DIR_NAME / self.run_id
        try:
            entries = [
                entry
                for entry in run_dir.iterdir()
                if entry.name.startswith(prefix) and entry.is_dir()
            ]
        except OSError:
            return []
        return sorted(entries, key=lambda entry: entry.name)

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.plan_state).as_posix()

    def _resolve(self, locator: str) -> Path | None:
        """The path a locator names, or ``None`` if it names somewhere else."""
        if not locator:
            return None
        candidate = Path(locator)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        return self.plan_state / candidate

    # -- reading -----------------------------------------------------------

    def fingerprint(self, locator: str) -> tuple[str | None, int]:
        """The digest and size of one source, or ``None`` when unreadable."""
        path = self._resolve(locator)
        if path is None:
            return None, 0
        try:
            data = path.read_bytes()
        except OSError:
            return None, 0
        return f"sha256:{hashlib.sha256(data).hexdigest()}", len(data)

    def record(self, locator: str) -> dict[str, Any] | None:
        """One JSON object this plan published, read at most once."""
        if locator in self._records:
            return self._records[locator]
        parsed: dict[str, Any] | None = None
        path = self._resolve(locator)
        if path is not None:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except OSError, ValueError:
                loaded = None
            if isinstance(loaded, dict):
                parsed = loaded
        self._records[locator] = parsed
        return parsed

    def view(self) -> mailbox_snapshot.MailboxView | None:
        """The plan's mailbox, read once under a shared lock and never written."""
        if not self._view_read:
            self._view_read = True
            try:
                self._view = self._read_mailbox(self.plan_dir, state_dir=self.state_dir)
            except mailbox_models.MailboxError:
                self._view = None
        return self._view

    # -- projecting --------------------------------------------------------

    def project(
        self,
        kind: SourceKind,
        locator: str,
        *,
        max_records: int,
        max_summary_chars: int,
    ) -> _Projection | None:
        """Describe one family from its source, short and long, or ``None``."""
        if kind in ("questions", "supervision"):
            view = self.view()
            if view is None:
                return None
            if kind == "questions":
                return _project_questions(
                    view,
                    run_id=self.run_id,
                    max_records=max_records,
                    max_summary_chars=max_summary_chars,
                )
            return _project_supervision(
                view, max_records=max_records, max_summary_chars=max_summary_chars
            )
        record = self.record(locator)
        if record is None:
            return None
        if kind == "status":
            return _project_status(record, max_summary_chars=max_summary_chars)
        if kind == "run_config":
            return _project_run_config(
                record, max_records=max_records, max_summary_chars=max_summary_chars
            )
        if kind == "verification":
            return _project_verification(
                record, max_records=max_records, max_summary_chars=max_summary_chars
            )
        return _project_worker_changes(
            record, max_records=max_records, max_summary_chars=max_summary_chars
        )


def _project_status(record: dict[str, Any], *, max_summary_chars: int) -> _Projection:
    """The run's own record: what phase it is in, and what it waits on."""
    paused = bool(record.get("paused"))
    reason = _first(record.get("pause_reason"), record.get("reason"))
    paused_at = _first(record.get(overseer_discovery.PAUSED_AT_KEY))
    finished = _first(record.get("run_finished_at"))
    if finished:
        phase = f"finished at {finished}"
    elif paused:
        phase = f"paused ({reason or 'no reason recorded'})"
        if paused_at:
            phase = f"{phase} at {paused_at}"
    else:
        phase = "running"

    clauses = [f"run {_value(record.get('run_id'))} {phase}"]
    active = record.get("active_turn")
    if isinstance(active, dict):
        clauses.append(
            f"turn {_value(active.get('turn_index'))} of "
            f"{_value(active.get('session') or active.get('session_id'))} active"
        )
    if isinstance(record.get("waiting_for_reply"), dict):
        clauses.append("waiting for a reply")
    if isinstance(record.get("abandoned"), dict):
        clauses.append("recorded abandoned")

    fields = (
        ("run_id", record.get("run_id")),
        ("ok", record.get("ok")),
        ("paused", record.get("paused")),
        ("pause_reason", reason),
        ("paused_at", paused_at),
        ("run_started_at", record.get("run_started_at")),
        ("run_finished_at", record.get("run_finished_at")),
        ("active_turn", record.get("active_turn")),
        ("active_verification", record.get("active_verification")),
        ("active_finalization", record.get("active_finalization")),
        ("waiting_for_reply", record.get("waiting_for_reply")),
        ("pause_policy", record.get("pause_policy")),
        ("control", record.get("control")),
        ("abandoned", record.get("abandoned")),
    )
    return _Projection(
        summary=_clip("; ".join(clauses), max_summary_chars),
        detail=_lines(fields),
        records=1,
        shown=1,
    )


def _project_run_config(
    record: dict[str, Any], *, max_records: int, max_summary_chars: int
) -> _Projection:
    """The binding a run works under: workspace, registry, repositories."""
    repositories = record.get("repositories")
    entries = repositories if isinstance(repositories, list) else []
    aliases = [
        str(entry.get("alias"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("alias")
    ]
    run_config = record.get("run_config")
    config = run_config if isinstance(run_config, dict) else {}

    summary = (
        f"workspace {_value(record.get('workspace_root'))} at registry "
        f"{_value(record.get('registry_digest'))} with "
        f"{len(entries)} declared "
        f"{'repository' if len(entries) == 1 else 'repositories'}"
    )
    if aliases:
        summary = f"{summary} ({', '.join(aliases[:max_records])})"

    fields: list[tuple[str, Any]] = [
        ("workspace_root", record.get("workspace_root")),
        ("cwd", record.get("cwd")),
        ("registry_digest", record.get("registry_digest")),
        ("harness", record.get("harness")),
        ("review_harness", config.get("review_harness")),
        ("supervisor_audit", config.get("supervisor_audit")),
        ("dry_run", record.get("dry_run")),
        (
            overseer_discovery.DISCOVERY_VERSION_KEY,
            record.get(overseer_discovery.DISCOVERY_VERSION_KEY),
        ),
    ]
    shown = 0
    for entry in entries[:max_records]:
        if not isinstance(entry, dict):
            continue
        shown += 1
        fields.append(
            (
                f"repository[{shown - 1}]",
                {
                    "alias": entry.get("alias"),
                    "path": entry.get("path"),
                    "origin": entry.get("origin"),
                    "resolved_root": entry.get("resolved_root"),
                },
            )
        )
    return _Projection(
        summary=_clip(summary, max_summary_chars),
        detail=_lines(fields),
        records=max(len(entries), 1),
        shown=max(shown, 1),
    )


def _project_questions(
    view: mailbox_snapshot.MailboxView,
    *,
    run_id: str,
    max_records: int,
    max_summary_chars: int,
) -> _Projection:
    """The questions this run can still consume an answer for.

    ``open_questions`` rather than ``unanswered_questions``: a question whose
    owning run has ended blocks nothing and no reply can reach a worker, so
    reporting it as a live incident would send an operator to answer a mailbox
    nobody is reading.

    The identities follow the same cap as the summary. A run with more open
    questions than one summary names has more than one incident, and listing
    fifty identifiers to prove it would spend the context this boundary exists
    to protect; ``truncated`` is what says there are more.
    """
    questions = view.open_questions(run_id)
    if not questions:
        return _Projection(
            summary="no question of this run is waiting on an answer",
            detail="",
            records=0,
            shown=0,
        )
    named = questions[:max_records]
    labels = []
    for message in named:
        waiting = view.active_wait_for(message.id) is not None
        labels.append(f"{message.id}{' (waiting)' if waiting else ''}")
    summary = (
        f"{len(questions)} open "
        f"{'question' if len(questions) == 1 else 'questions'}: "
        f"{', '.join(labels)}"
    )
    fields: list[tuple[str, Any]] = []
    for message in named:
        fields.append(
            (
                message.id,
                {
                    "created_at": message.created_at,
                    "session_id": message.session_id,
                    "turn_index": message.turn_index,
                    "delivery_mode": message.delivery_mode,
                    "delivery_state": view.delivery_state_for(message.id),
                    "waiting": view.active_wait_for(message.id) is not None,
                    "text": _clip(message.text or "", 200),
                },
            )
        )
    return _Projection(
        summary=_clip(summary, max_summary_chars),
        detail=_lines(fields),
        records=len(questions),
        shown=len(named),
        identities=tuple(message.id for message in named),
    )


def _project_supervision(
    view: mailbox_snapshot.MailboxView, *, max_records: int, max_summary_chars: int
) -> _Projection:
    """What a human contributed to this plan, most recent last."""
    records = tuple(
        message for message in view.messages if message.kind in SUPERVISION_KINDS
    )
    if not records:
        return _Projection(
            summary="no supervision record on this plan",
            detail="",
            records=0,
            shown=0,
        )
    named = records[-max_records:]
    counts: dict[str, int] = {}
    for message in records:
        counts[message.kind] = counts.get(message.kind, 0) + 1
    ended = view.latest_supervision_end
    summary = f"{len(records)} supervision records (" + ", ".join(
        f"{count} {kind}" for kind, count in sorted(counts.items())
    )
    summary = f"{summary})"
    if ended is not None:
        summary = (
            f"{summary}; last supervision ended {ended.created_at} "
            f"({_value(ended.supervision_outcome)})"
        )
    fields: list[tuple[str, Any]] = []
    for message in named:
        fields.append(
            (
                f"{message.seq}:{message.kind}",
                {
                    "id": message.id,
                    "created_at": message.created_at,
                    "to": message.to,
                    "supervision_id": message.supervision_id,
                    "supervision_outcome": message.supervision_outcome,
                    "delivery_state": message.delivery_state,
                    "text": _clip(message.text or "", 200),
                },
            )
        )
    return _Projection(
        summary=_clip(summary, max_summary_chars),
        detail=_lines(fields),
        records=len(records),
        shown=len(named),
    )


def _project_verification(
    record: dict[str, Any], *, max_records: int, max_summary_chars: int
) -> _Projection:
    """One turn's verification result, named checks first."""
    block = record.get("verification")
    if not isinstance(block, dict):
        return _Projection(
            summary="this turn recorded no verification",
            detail="",
            records=0,
            shown=0,
        )
    failures = _named(block.get("failures"))
    diagnostics = _named(block.get("diagnostic_failures"))
    checks = _named(block.get("checks"))
    passed = block.get("passed")
    verdict = "passed" if passed else ("failed" if passed is False else "unresolved")
    summary = (
        f"verification of {_value(block.get('session'))} {verdict} "
        f"({_value(block.get('reason'))}) at turn "
        f"{_value(record.get('turn_index'))}"
    )
    if failures:
        summary = f"{summary}; failing: {', '.join(failures[:max_records])}"
    fields: list[tuple[str, Any]] = [
        ("session", block.get("session")),
        ("passed", passed),
        ("reason", block.get("reason")),
        ("error", block.get("error")),
        ("acceptance", block.get("acceptance")),
        ("recipe", block.get("recipe")),
        ("checks", checks[:max_records]),
        ("failures", failures[:max_records]),
        ("diagnostic_failures", diagnostics[:max_records]),
        ("skipped_checks", _named(block.get("skipped_checks"))[:max_records]),
    ]
    return _Projection(
        summary=_clip(summary, max_summary_chars),
        detail=_lines(fields),
        records=max(len(checks), 1),
        shown=max(min(len(checks), max_records), 1),
    )


def _project_worker_changes(
    record: dict[str, Any], *, max_records: int, max_summary_chars: int
) -> _Projection:
    """What the worker changed: a commit if there is one, session status if not.

    Two shapes because the run writes two. A blocked run has often changed the
    plan without reaching a commit, and reporting nothing for it would describe
    the artifact layout rather than the work.
    """
    if "commit_index" in record:
        paths = record.get("qualified_paths")
        qualified = [str(item) for item in paths] if isinstance(paths, list) else []
        summary = (
            f"commit {_value(record.get('commit_sha'))} in "
            f"{_value(record.get('repository_alias'))} "
            f"({_value(record.get('status_result'))}) touching "
            f"{len(qualified)} {'path' if len(qualified) == 1 else 'paths'}"
        )
        fields: list[tuple[str, Any]] = [
            ("commit_index", record.get("commit_index")),
            ("scope", record.get("scope")),
            ("session", record.get("session_id") or record.get("session")),
            ("repository_alias", record.get("repository_alias")),
            ("status_result", record.get("status_result")),
            ("commit_sha", record.get("commit_sha")),
            ("head_before", record.get("head_before")),
            ("head_after", record.get("head_after")),
            ("clean_after", record.get("clean_after")),
            ("qualified_paths", qualified[:max_records]),
        ]
        return _Projection(
            summary=_clip(summary, max_summary_chars),
            detail=_lines(fields),
            records=max(len(qualified), 1),
            shown=max(min(len(qualified), max_records), 1),
        )

    changes = record.get("status_changes")
    entries = [str(item) for item in changes] if isinstance(changes, list) else []
    if not entries:
        return _Projection(
            summary="this run has committed nothing and changed no session status",
            detail="",
            records=0,
            shown=0,
        )
    summary = (
        f"turn {_value(record.get('turn_index'))} of "
        f"{_value(record.get('session_id') or record.get('session'))} changed "
        f"{len(entries)} session {'status' if len(entries) == 1 else 'statuses'}: "
        f"{', '.join(entries[:max_records])}"
    )
    fields = [
        ("turn_index", record.get("turn_index")),
        ("session", record.get("session_id") or record.get("session")),
        ("returncode", record.get("returncode")),
        ("timed_out", record.get("timed_out")),
        ("interrupted", record.get("interrupted")),
        ("status_result", record.get("status_result")),
        ("status_changes", entries[:max_records]),
        ("pending_completed", record.get("pending_completed_ids")),
    ]
    return _Projection(
        summary=_clip(summary, max_summary_chars),
        detail=_lines(fields),
        records=len(entries),
        shown=min(len(entries), max_records),
    )


# -- helpers ---------------------------------------------------------------


def _candidate_for(
    plan_dir: str,
    *,
    build_inventory: BuildInventory,
    state_dir: str | Path | None,
) -> overseer_discovery.Candidate | None:
    """The plan as discovery sees it now, or ``None`` if it sees it no longer."""
    inventory = build_inventory(state_dir=state_dir)
    for candidate in inventory.candidates:
        if candidate.plan_dir == plan_dir:
            return candidate
    return None


def _source_kind(value: str) -> SourceKind | None:
    """Read a stored family name, refusing one this build does not know."""
    for kind in SOURCE_KINDS:
        if kind == value:
            return kind
    return None


def _request_digest(grant: overseer_policy_client.Grant | None) -> str:
    return grant.request_digest if grant is not None else ""


def _withheld(
    *,
    plan_dir: str,
    run_id: str,
    incident_id: int,
    kind: str,
    summary: str,
    grant: overseer_policy_client.Grant | None,
    scope: str,
    captured_at: str,
    refusal: Refusal,
    withheld: int,
) -> Packet:
    """A packet that describes nothing, and says so."""
    return Packet(
        plan_dir=plan_dir,
        run_id=run_id,
        incident_id=incident_id,
        kind=kind,
        summary=summary,
        questions=(),
        request_digest=_request_digest(grant),
        config_fingerprint=(grant.config_fingerprint if grant is not None else ""),
        scope=scope,
        sources=(),
        gaps=(),
        captured_at=captured_at,
        withheld=withheld,
        refusal=refusal,
    )


def _unavailable(*, kind: SourceKind, locator: str, refusal: Refusal) -> Excerpt:
    """An excerpt that carries no text, and says why."""
    return Excerpt(
        kind=kind,
        space=STATE_SPACE,
        locator=locator,
        text="",
        digest="",
        recorded_digest="",
        size=0,
        truncated=False,
        refusal=refusal,
    )


def _named(value: Any) -> list[str]:
    """Name the entries of a list of check records, however they are shaped."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for index, entry in enumerate(value):
        if isinstance(entry, dict):
            label = entry.get("name") or entry.get("label") or entry.get("command")
            names.append(str(label) if label else f"check[{index}]")
        else:
            names.append(_clip(str(entry), 80))
    return names


def _lines(fields: tuple[tuple[str, Any], ...] | list[tuple[str, Any]]) -> str:
    """One ``key: value`` line per field, omitting the ones with nothing in them."""
    rendered = []
    for key, value in fields:
        if value is None or value == [] or value == {}:
            continue
        rendered.append(f"{key}: {_value(value)}")
    return "\n".join(rendered)


def _value(value: Any, *, limit: int = 240) -> str:
    """Render one field compactly, whatever JSON put there."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, str):
        rendered = value
    elif isinstance(value, int | float):
        rendered = str(value)
    else:
        try:
            rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except TypeError, ValueError:
            rendered = repr(value)
    return _clip(rendered, limit)


def _clip(text: str, limit: int) -> str:
    """One line, no longer than ``limit``, with the truncation visible."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 3, 0)].rstrip() + "..."


def _first(*values: Any) -> str:
    """The first value that is a non-empty string, or ``""``."""
    for value in values:
        if isinstance(value, str) and value:
            return value
    return ""
