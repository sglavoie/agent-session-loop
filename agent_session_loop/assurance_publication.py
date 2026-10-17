"""Publish audits and issue identities idempotently under the ledger lock."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from . import assurance_ledger as ledger
from . import (
    assurance_projection_replay,
    assurance_projection_vocab,
    assurance_publication_identity,
    assurance_publication_models,
)
from .assurance_ledger import (
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_OBSERVED,
    LedgerView,
    PendingRecord,
    follow_merge_chain,
)
from .assurance_models_findings import (
    Finding,
)
from .assurance_models_inbox import (
    IssueIdentityBinding,
    IssueObservation,
)
from .assurance_models_primitives import (
    ISSUE_ID_PREFIX,
    identity_key_for_issue,
)
from .assurance_models_summary import AssuranceSummary


def _issue_opening_runs(view: LedgerView) -> dict[str, str]:
    """Map each issue to the run whose observation opened it.

    This is what lets a linked issue be counted the same way an allocated one
    is: new when this run opened it, recurring when an earlier run did, so the
    counts stay one vocabulary whether the observation was written by the
    publication or by a guard that ran before it.
    """
    edges = view.merge_edges
    opened: dict[str, str] = {}
    for record in view.records_of_kind(KIND_ISSUE_OBSERVED):
        if record.issue_id is None:
            continue
        opened.setdefault(
            follow_merge_chain(edges, record.issue_id), record.payload.run_id
        )
    return opened


class _IdentityResolver:
    """Which issue owns each finding, decided from state read under the lock.

    The order narrows from facts to requests: a fingerprint ASL derived itself,
    then an ASL-owned identity key already bound to that fingerprint's issue,
    then the selection an audit made and this transaction revalidates, and only
    then a new ID. Anything earlier overrules what follows it, because a defect
    the ledger already names is not made new by an answer that could not see it.

    The maps start from the ledger and are updated as findings resolve, so a
    publication that opens several issues numbers them consecutively, and a
    fingerprint first seen here is already attached to its issue for the next
    finding in the same audit.
    """

    def __init__(self, view: LedgerView, selections: Mapping[str, str]) -> None:
        edges = view.merge_edges
        self._edges = edges
        self._selections = selections
        self._by_fingerprint = {
            fingerprint: follow_merge_chain(edges, issue_id)
            for fingerprint, issue_id in view.issue_ids_by_fingerprint.items()
        }
        self._allocated = set(view.allocated_issue_ids)
        self._next = view.next_issue_number

    def resolve(self, finding: Finding) -> tuple[str, bool]:
        """Return the issue this finding belongs to and whether it was minted."""
        existing = self._by_fingerprint.get(finding.fingerprint)
        if existing is not None:
            return existing, False
        selected = self._selections.get(finding.fingerprint)
        if selected is not None:
            owner = self._owner(selected)
            # The new fingerprint attaches to the selected issue by being
            # observed under it, so the next exact sighting of this defect
            # recurs there without anyone being asked again.
            self._by_fingerprint[finding.fingerprint] = owner
            return owner, False
        issue_id = f"{ISSUE_ID_PREFIX}{self._next}"
        self._next += 1
        self._allocated.add(issue_id)
        self._by_fingerprint[finding.fingerprint] = issue_id
        return issue_id, True

    def _owner(self, issue_id: str) -> str:
        """Return the issue a reviewed selection names in the ledger as it is now.

        A selection aimed at an issue another publisher has since merged away
        follows the merge rather than being refused, because the merge is the
        statement that the two were always one defect. A selection naming an
        issue the ledger never minted is refused outright.
        """
        canonical = follow_merge_chain(self._edges, issue_id)
        if canonical not in self._allocated:
            raise assurance_projection_vocab.ProjectionError(
                assurance_projection_vocab.UNKNOWN_ISSUE,
                f"{issue_id} names no issue in the inbox",
            )
        return canonical


def publish_audit(
    publication: assurance_publication_models.AuditPublication,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
    observed_at: str | None = None,
    promote: Callable[[], None] | None = None,
) -> assurance_publication_models.PublicationResult:
    """Record one audit and the issues it opens as a single atomic append.

    Republishing an audit already in the ledger writes nothing and returns the
    summary that was stored the first time, so a retry, a resumed run, and two
    concurrent publishers all leave exactly one occurrence behind.

    Identity is decided here rather than accepted from the caller. Every route —
    the fingerprint index, the identity keys, and the issue an audit selected —
    is answered from the view taken inside the append lock, so a decision made
    against identity state another publisher has since moved is refreshed rather
    than minted from. A newly minted ID is bound to its ``issue.asi-N`` key in
    the same transaction, because an ID that exists without its key would be an
    issue nothing durable can name.

    ``promote`` is called once identity has been validated and the records are
    built, but before anything is appended. It exists so a caller can make its
    canonical artifacts canonical exactly when the record naming them is about
    to be written: raising from it aborts the transaction with the ledger
    untouched.
    """
    stamp = recorded_at or assurance_projection_vocab._utc_now()
    kind = publication.record_kind
    sighted_at = observed_at or publication.completed_at or stamp
    report = assurance_publication_identity.audit_report_locator(publication.artifacts)
    prior: list[AssuranceSummary] = []
    written: list[AssuranceSummary] = []
    refreshed: list[bool] = []

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        key = assurance_publication_identity.publication_key(
            kind, publication.run_id, publication.audit_input_digest
        )
        already = assurance_publication_identity._publications(view).get(key)
        if already is not None:
            prior.append(already)
            return ()
        if (
            publication.catalog_digest is not None
            and assurance_projection_replay.project(view).catalog.digest
            != publication.catalog_digest
        ):
            refreshed.append(True)
        resolver = _IdentityResolver(view, publication.identity_selections)
        issue_ids: list[str] = []
        new_ids: set[str] = set()
        observations: list[PendingRecord] = []
        bindings: list[PendingRecord] = []
        for finding in publication.issue_findings():
            issue_id, is_new = resolver.resolve(finding)
            if issue_id not in issue_ids:
                issue_ids.append(issue_id)
                if is_new:
                    new_ids.add(issue_id)
            observations.append(
                PendingRecord(
                    kind=KIND_ISSUE_OBSERVED,
                    payload=IssueObservation(
                        run_id=publication.run_id,
                        finding=finding,
                        observed_at=sighted_at,
                        audit_artifact=report,
                    ),
                    issue_id=issue_id,
                    recorded_at=stamp,
                )
            )
            if is_new:
                bindings.append(
                    PendingRecord(
                        kind=KIND_ISSUE_IDENTITY_BOUND,
                        payload=IssueIdentityBinding(
                            identity_key=identity_key_for_issue(issue_id),
                            issue_id=issue_id,
                            source="allocation",
                            bound_at=stamp,
                        ),
                        recorded_at=stamp,
                    )
                )
        if publication.linked_issue_ids:
            opened_by = _issue_opening_runs(view)
            for linked in publication.linked_issue_ids:
                canonical = view.canonical_issue_id(linked)
                if canonical not in view.allocated_issue_ids:
                    raise assurance_projection_vocab.ProjectionError(
                        assurance_projection_vocab.UNKNOWN_ISSUE,
                        f"{linked} names no issue in the inbox",
                    )
                if canonical in issue_ids:
                    continue
                issue_ids.append(canonical)
                if opened_by.get(canonical) == publication.run_id:
                    new_ids.add(canonical)
        summary = publication.to_summary(
            issue_ids=tuple(issue_ids),
            new_issue_count=len(new_ids),
            recurring_issue_count=len(issue_ids) - len(new_ids),
        )
        written.append(summary)
        if promote is not None:
            promote()
        return (
            PendingRecord(kind=kind, payload=summary, recorded_at=stamp),
            *observations,
            # A binding follows the observation that minted its issue: replayed
            # before it, the key would name an issue nothing had opened yet and
            # be reported as an anomaly rather than read as a name.
            *bindings,
        )

    records = ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)
    if not records:
        return assurance_publication_models.PublicationResult(
            summary=prior[0], records=(), created=False
        )
    return assurance_publication_models.PublicationResult(
        summary=written[0],
        records=records,
        created=True,
        identity_refreshed=bool(refreshed),
    )
