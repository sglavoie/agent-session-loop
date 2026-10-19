"""Overseer context excerpts."""

from __future__ import annotations

from pathlib import Path

from . import (
    mailbox,
    overseer_policy_client,
    overseer_store_ledger,
)
from . import (
    overseer_context_models as models,
)
from . import (
    overseer_context_reader as reader_module,
)
from . import (
    overseer_context_values as values,
)


def expand(
    *,
    grant: overseer_policy_client.Grant | None,
    ledger: overseer_store_ledger.Ledger,
    incident_id: int,
    kind: models.SourceKind,
    state_dir: str | Path | None = None,
    read_mailbox: models.ReadMailbox = mailbox.read_mailbox,
    limit: int = models.MAX_EXCERPT_CHARS,
    max_records: int = models.MAX_EXCERPT_RECORDS,
) -> models.Excerpt:
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
        raise models.ContextError(f"no incident {incident_id} to expand")
    if kind not in models.SOURCE_KINDS:
        raise models.ContextError(
            f"{kind!r} is not an evidence family; use one of {models.SOURCE_KINDS}"
        )

    view = ledger.read_evidence(plan_dir=incident.plan_dir, grant=grant)
    if view.refusal is not None:
        return values._unavailable(kind=kind, locator="", refusal=view.refusal)

    row = next(
        (
            record
            for record in view.records
            if record.incident_id == incident_id and record.kind == kind
        ),
        None,
    )
    if row is None:
        return values._unavailable(kind=kind, locator="", refusal="evidence_missing")

    reader = reader_module._Reader(
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
        max_summary_chars=models.MAX_SUMMARY_CHARS,
    )
    if digest is None or projection is None:
        return values._unavailable(
            kind=kind, locator=row.locator, refusal="source_unreadable"
        )

    text = projection.detail
    clipped = len(text) > limit
    if clipped:
        text = text[:limit].rstrip()
    return models.Excerpt(
        kind=kind,
        space=row.alias,
        locator=row.locator,
        text=text,
        digest=digest,
        recorded_digest=row.digest,
        size=size,
        truncated=clipped or projection.shown < projection.records,
    )
