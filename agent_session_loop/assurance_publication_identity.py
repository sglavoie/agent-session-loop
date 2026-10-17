"""Compute audit publication identities and locate their persisted artifacts."""

from __future__ import annotations

from collections.abc import Sequence

from . import assurance_projection_vocab
from .assurance_ledger import (
    LedgerView,
)
from .assurance_models_findings import (
    EvidenceRef,
)
from .assurance_models_summary import AssuranceSummary, AuditAttempt
from .assurance_models_vocab import (
    AUDIT_INPUT_FILENAME,
    AUDIT_REPORT_FILENAME,
    MAX_REFERENCES_PER_ITEM,
)

# ---------------------------------------------------------------------------
# Audit artifacts
# ---------------------------------------------------------------------------


def _named_artifact(
    artifacts: Sequence[EvidenceRef], filename: str
) -> EvidenceRef | None:
    for reference in artifacts:
        if reference.kind == "artifact" and reference.locator.rsplit("/", 1)[-1] == (
            filename
        ):
            return reference
    return None


def audit_input_digest(artifacts: Sequence[EvidenceRef]) -> str | None:
    """Return the digest of the audit input these artifacts were produced from.

    This is half of a publication's identity: two audits of one run that read
    different inputs are different audits, and re-publishing the same input is
    the duplicate that must collapse to one occurrence.
    """
    reference = _named_artifact(artifacts, AUDIT_INPUT_FILENAME)
    return None if reference is None else reference.digest


def audit_report_locator(artifacts: Sequence[EvidenceRef]) -> str | None:
    """Return the audit report an observation should point a reader at."""
    reference = _named_artifact(artifacts, AUDIT_REPORT_FILENAME)
    return None if reference is None else reference.locator


def publication_key(kind: str, run_id: str, digest: str | None) -> tuple[str, str, str]:
    """Return the identity that makes republishing an audit a no-op.

    The kind participates because an ``audit_pending`` placeholder and the
    terminal record that later replaces it describe the same run and, before any
    audit input exists, the same absent digest. Without the kind, recording that
    an audit was skipped would silently vanish behind the pending record.
    """
    return (run_id, kind, digest or "")


def _publications(view: LedgerView) -> dict[tuple[str, str, str], AssuranceSummary]:
    published: dict[tuple[str, str, str], AssuranceSummary] = {}
    for record in view.records:
        if record.kind not in assurance_projection_vocab.AUDIT_RECORD_KINDS:
            continue
        summary = record.payload
        published.setdefault(
            publication_key(
                record.kind, summary.run_id, audit_input_digest(summary.artifacts)
            ),
            summary,
        )
    return published


# ---------------------------------------------------------------------------
# Publication input
# ---------------------------------------------------------------------------


def _unavailable_evidence(
    artifacts: Sequence[EvidenceRef], attempts: Sequence[AuditAttempt]
) -> tuple[str, ...]:
    locators: list[str] = []
    for reference in artifacts:
        if reference.locator not in locators:
            locators.append(reference.locator)
    for attempt in attempts:
        if attempt.artifact_dir is not None and attempt.artifact_dir not in locators:
            locators.append(attempt.artifact_dir)
    return tuple(locators[:MAX_REFERENCES_PER_ITEM])
