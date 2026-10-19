"""Overseer context packets."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from . import (
    clock,
    events,
    mailbox,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
)
from . import (
    overseer_context_models as models,
)
from . import (
    overseer_context_reader as reader_module,
)
from . import (
    overseer_context_scope as scope_module,
)
from . import (
    overseer_context_values as values,
)


def build_packet(
    *,
    grant: overseer_policy_client.Grant | None,
    ledger: overseer_store_ledger.Ledger,
    candidate: overseer_discovery.Candidate,
    kind: str,
    summary: str,
    state_dir: str | Path | None = None,
    now: Callable[[], str] = clock.utc_iso_now,
    read_mailbox: models.ReadMailbox = mailbox.read_mailbox,
    max_records: int = models.MAX_RECORDS,
    max_summary_chars: int = models.MAX_SUMMARY_CHARS,
) -> models.Packet:
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
        raise models.ContextError(
            "context can only be assembled for a run discovery identified; "
            f"{candidate.state_dir} is ineligible "
            f"({', '.join(candidate.ineligible_reasons) or 'no reason recorded'})"
        )
    scope = scope_module.scope_digest(candidate)
    captured_at = now()

    if (
        grant is None
        or grant.plan_dir != plan_dir
        or grant.access != models.CONTEXT_ACCESS
        or not grant.request_digest
        or not grant.config_fingerprint
    ):
        return values._withheld(
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
        return values._withheld(
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

    reader = reader_module._Reader(
        plan_dir=plan_dir,
        run_id=run_id,
        state_dir=state_dir,
        read_mailbox=read_mailbox,
    )
    sources: list[models.Source] = []
    gaps: list[models.Gap] = []
    evidence: list[overseer_store_models.EvidenceInput] = []
    questions: tuple[str, ...] = ()

    for source_kind in models.SOURCE_KINDS:
        locator = reader.locate(source_kind)
        if locator is None:
            gaps.append(
                models.Gap(
                    kind=source_kind,
                    reason="absent",
                    detail=reader_module._ABSENT_DETAIL[source_kind],
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
                models.Gap(
                    kind=source_kind,
                    reason="unreadable",
                    detail=f"{locator} could not be read as the record it should be",
                )
            )
            continue
        if projection.records == 0:
            gaps.append(
                models.Gap(
                    kind=source_kind,
                    reason="empty",
                    detail=projection.summary,
                )
            )
            continue
        if source_kind == "questions":
            questions = projection.identities
        sources.append(
            models.Source(
                kind=source_kind,
                space=models.STATE_SPACE,
                locator=locator,
                summary=projection.summary,
                digest=digest,
                recorded_digest=digest,
                size=size,
                truncated=projection.shown < projection.records,
            )
        )
        evidence.append(
            overseer_store_models.EvidenceInput(
                alias=models.STATE_SPACE,
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
        overseer_store_models.EvidenceInput(
            alias=models.STATE_SPACE,
            kind=models.SCOPE_KIND,
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
    except overseer_store_models.LedgerRefused:
        # The gate above passed and this one did not, so the policy moved while
        # the sources were being read. Nothing was written; report the refusal
        # the ledger would report now rather than the one it would have then.
        refusal = ledger.read_evidence(plan_dir=plan_dir, grant=grant).refusal
        return values._withheld(
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

    return models.Packet(
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
    ledger: overseer_store_ledger.Ledger,
    incident_id: int,
    state_dir: str | Path | None = None,
    now: Callable[[], str] = clock.utc_iso_now,
    read_mailbox: models.ReadMailbox = mailbox.read_mailbox,
    build_inventory: models.BuildInventory = overseer_discovery.build_inventory,
    max_records: int = models.MAX_RECORDS,
    max_summary_chars: int = models.MAX_SUMMARY_CHARS,
) -> models.Packet:
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
        raise models.ContextError(f"no incident {incident_id} to load context for")

    scope = ""
    captured_at = now()
    view = ledger.read_evidence(plan_dir=incident.plan_dir, grant=grant)
    if view.refusal is not None:
        return values._withheld(
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
        return values._withheld(
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

    candidate = scope_module._candidate_for(
        incident.plan_dir, build_inventory=build_inventory, state_dir=state_dir
    )
    scope = values._first(
        *(row.digest for row in rows if row.kind == models.SCOPE_KIND)
    )
    probe = models.Packet(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        incident_id=incident_id,
        kind=incident.kind,
        summary=incident.summary,
        questions=(),
        request_digest=scope_module._request_digest(grant),
        config_fingerprint=incident.config_fingerprint,
        scope=scope,
        sources=(),
        gaps=(),
        captured_at=captured_at,
    )
    checked = scope_module.revalidate(packet=probe, candidate=candidate)
    if not checked.fresh and checked.reason is not None:
        return values._withheld(
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

    reader = reader_module._Reader(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        state_dir=state_dir,
        read_mailbox=read_mailbox,
    )
    sources: list[models.Source] = []
    gaps: list[models.Gap] = []
    questions: tuple[str, ...] = ()
    for row in rows:
        if row.kind == models.SCOPE_KIND:
            continue
        source_kind = scope_module._source_kind(row.kind)
        if source_kind is None:
            gaps.append(
                models.Gap(
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
                models.Gap(
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
            models.Source(
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

    return models.Packet(
        plan_dir=incident.plan_dir,
        run_id=incident.run_id,
        incident_id=incident_id,
        kind=incident.kind,
        summary=incident.summary,
        questions=questions,
        request_digest=scope_module._request_digest(grant),
        config_fingerprint=incident.config_fingerprint,
        scope=scope,
        sources=tuple(sources),
        gaps=tuple(gaps),
        captured_at=captured_at,
        withheld=view.withheld,
    )
