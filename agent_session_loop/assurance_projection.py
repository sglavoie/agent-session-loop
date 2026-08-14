"""Derived inbox semantics over the immutable assurance ledger.

The ledger stores what happened; nothing in it is ever edited. Every question an
operator asks — is this issue open, how often has it recurred, which issue owns
this defect now — is answered by replaying those records in order:

* **The ledger is the only input.** :func:`project` reads a
  :class:`~agent_session_loop.assurance_ledger.LedgerView` and nothing else, so
  the same bytes always reconstruct the same state. Deleting the index cache,
  copying the file to another machine, or reading it years later all produce
  identical issues.
* **Publication is idempotent under concurrency.** An audit is identified by its
  run ID, its record kind, and the digest of the audit input it judged. The
  audit record and every observation it opens are written in one transaction
  under the ledger's exclusive lock, and a publisher that finds its own key
  already recorded writes nothing at all. Two processes publishing the same
  audit therefore produce one occurrence, not two.
* **Identity is the fingerprint, never the text.** A recurring defect routes to
  the issue its first sighting minted. An audit that could not run deduplicates
  by its stable reason class, so a thousand distinct exception messages of one
  kind remain one issue with a thousand occurrences.
* **Closing is a claim about the future, not about history.** A recurrence after
  ``resolve`` reopens the issue, because the fix evidently did not hold. A
  recurrence after ``dismiss`` or ``accept-risk`` does not: a human already
  decided this defect is tolerable, and the new sighting advances the occurrence
  history without reversing that decision.
* **Merge is irreversible and always forward.** A merged issue can never be
  merged again or un-merged, its whole history moves to the target, and later
  observations of either fingerprint land on the final canonical target.
* **A hostile ledger is readable.** Records that cannot mean anything — a
  disposition for an issue that was never opened, a merge naming an unknown or
  cyclic target — are reported as anomalies and skipped. The projection never
  raises on stored bytes, because a ledger that cannot be read is a ledger that
  cannot be audited.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as replace_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import assurance_ledger as ledger
from .assurance_ledger import (
    KIND_AUDIT_COMPLETED,
    KIND_AUDIT_PENDING,
    KIND_AUDIT_SKIPPED,
    KIND_AUDIT_UNAVAILABLE,
    KIND_HUMAN_CLOSEOUT,
    KIND_IMPROVEMENT_PLAN_DRAFTED,
    KIND_ISSUE_DISPOSITION,
    KIND_ISSUE_MERGED,
    KIND_ISSUE_OBSERVED,
    LedgerRecord,
    LedgerView,
    PendingRecord,
    RejectedLedgerRecord,
    follow_merge_chain,
)
from .assurance_models import (
    AUDIT_INPUT_FILENAME,
    AUDIT_REPORT_FILENAME,
    ISSUE_ID_PREFIX,
    MAX_REFERENCES_PER_ITEM,
    AssuranceError,
    AssuranceSummary,
    AuditAttempt,
    AuditRole,
    AuditState,
    Closeout,
    CloseoutOutcome,
    Disposition,
    DispositionKind,
    EvidenceRef,
    Finding,
    ImprovementPlanDraft,
    IssueMerge,
    IssueObservation,
    IssueState,
    issue_id_number,
    parse_issue_id,
)

#: The record kind each audit state is stored under. Kind and state are two
#: spellings of one fact and the ledger refuses a record where they disagree.
AUDIT_KIND_FOR_STATE: Mapping[str, str] = {
    "pending": KIND_AUDIT_PENDING,
    "all-clear": KIND_AUDIT_COMPLETED,
    "findings": KIND_AUDIT_COMPLETED,
    "skipped": KIND_AUDIT_SKIPPED,
    "unavailable": KIND_AUDIT_UNAVAILABLE,
}

AUDIT_RECORD_KINDS: frozenset[str] = frozenset(AUDIT_KIND_FOR_STATE.values())

#: States that assert no analysis produced a finding. ``unavailable`` is absent
#: on purpose: it opens exactly one system issue for its reason class, and may
#: additionally carry whatever deterministic findings were reached first.
NO_FINDING_STATES: frozenset[str] = frozenset({"all-clear", "skipped", "pending"})

#: Identity of the system issue an unavailable audit opens. The reason class is
#: the only varying part, which is what keeps a million distinct exception
#: messages of one kind inside a single issue.
SYSTEM_ISSUE_CATEGORY = "assurance"
SYSTEM_ISSUE_COMPONENT = "agent-session-loop/supervisor-audit"
SYSTEM_ISSUE_LANE = "unknown-anomalies"
SYSTEM_ISSUE_SEVERITY = "medium"
UNAVAILABLE_INVARIANT_PREFIX = "audit.availability"


UNKNOWN_ISSUE = "unknown-issue"
UNKNOWN_RUN = "unknown-run"
DUPLICATE_CLOSEOUT = "duplicate-closeout"
MERGE_SELF = "merge-self"
MERGE_UNKNOWN_SOURCE = "merge-unknown-source"
MERGE_UNKNOWN_TARGET = "merge-unknown-target"
MERGE_SOURCE_ALREADY_MERGED = "merge-source-already-merged"
MERGE_TARGET_ALREADY_MERGED = "merge-target-already-merged"
MERGE_CYCLE = "merge-cycle"
UNAVAILABLE_EVIDENCE_REQUIRED = "unavailable-evidence-required"

#: Why a merge cannot be recorded, or why a stored one could not be applied.
MERGE_REJECTIONS: tuple[str, ...] = (
    MERGE_SELF,
    MERGE_UNKNOWN_SOURCE,
    MERGE_UNKNOWN_TARGET,
    MERGE_SOURCE_ALREADY_MERGED,
    MERGE_TARGET_ALREADY_MERGED,
    MERGE_CYCLE,
)

_MERGE_REJECTION_DETAIL: Mapping[str, str] = {
    MERGE_SELF: "{source} cannot merge into itself",
    MERGE_UNKNOWN_SOURCE: "{source} names no issue in the inbox",
    MERGE_UNKNOWN_TARGET: "{target} names no issue in the inbox",
    MERGE_SOURCE_ALREADY_MERGED: (
        "{source} has already been merged and merging is irreversible"
    ),
    MERGE_TARGET_ALREADY_MERGED: (
        "{target} has itself been merged away; merge into the issue that owns "
        "its history instead"
    ),
    MERGE_CYCLE: "merging {source} into {target} would close a merge cycle",
}


class ProjectionError(AssuranceError):
    """A stable inbox-semantics failure suitable for CLI translation."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason if detail is None else f"{reason}: {detail}")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
        if record.kind not in AUDIT_RECORD_KINDS:
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


@dataclass(frozen=True)
class AuditPublication:
    """One audit's verdict, before any issue ID for it exists.

    This is the per-run :class:`AssuranceSummary` minus its issue accounting.
    The summary has to name the issues the audit opened, but those IDs are
    minted under the ledger lock during the publication itself, so the summary
    cannot be built until the write is already in progress. Everything the
    summary validates is validated when :meth:`to_summary` runs on the
    publication path; only the findings, which the summary never sees, are
    checked here.
    """

    run_id: str
    state: AuditState
    eligible: bool
    findings: tuple[Finding, ...] = ()
    reason: str | None = None
    role: AuditRole | None = None
    attempts: tuple[AuditAttempt, ...] = ()
    artifacts: tuple[EvidenceRef, ...] = ()
    #: Issues this run's own guards already appended to the ledger before the
    #: publication ran. They are named by the summary and observed again by
    #: nobody: the observation that minted them is already history. Syntax is
    #: checked here; that the ledger really allocated them is checked under the
    #: lock, because only the view taken there can answer it.
    linked_issue_ids: tuple[str, ...] = ()
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self,
            "linked_issue_ids",
            tuple(
                dict.fromkeys(parse_issue_id(item) for item in self.linked_issue_ids)
            ),
        )
        for index, finding in enumerate(self.findings):
            if not isinstance(finding, Finding):
                raise AssuranceError(
                    f"publication finding #{index + 1} must be a Finding; got "
                    f"{type(finding).__name__}"
                )
        seen: set[str] = set()
        for finding in self.findings:
            if finding.fingerprint in seen:
                raise AssuranceError(
                    f"publication repeats the finding identity "
                    f"{finding.category}/{finding.component}/{finding.invariant_key}; "
                    "one audit sees a defect once"
                )
            seen.add(finding.fingerprint)
        if self.state in NO_FINDING_STATES and self.findings:
            raise AssuranceError(
                f"an audit in state {self.state!r} opens no issue; got "
                f"{len(self.findings)} finding(s)"
            )
        if self.state == "findings" and not self.findings:
            raise AssuranceError(
                "an audit in state 'findings' must carry the finding(s) it found"
            )
        if self.state == "unavailable" and self.reason is None:
            raise AssuranceError(
                "an unavailable audit must record the reason class its system "
                "issue deduplicates by"
            )

    @property
    def audit_input_digest(self) -> str | None:
        return audit_input_digest(self.artifacts)

    @property
    def record_kind(self) -> str:
        return AUDIT_KIND_FOR_STATE[self.state]

    def issue_findings(self) -> tuple[Finding, ...]:
        """Return every finding this publication turns into an observation."""
        if self.state != "unavailable":
            return self.findings
        return (self._unavailable_finding(), *self.findings)

    def _unavailable_finding(self) -> Finding:
        evidence = _unavailable_evidence(self.artifacts, self.attempts)
        if not evidence:
            raise ProjectionError(
                UNAVAILABLE_EVIDENCE_REQUIRED,
                f"run {self.run_id} recorded an unavailable audit without naming "
                "an artifact or an attempt directory; an issue that points at "
                "nothing cannot be investigated",
            )
        return Finding(
            category=SYSTEM_ISSUE_CATEGORY,
            component=SYSTEM_ISSUE_COMPONENT,
            invariant_key=f"{UNAVAILABLE_INVARIANT_PREFIX}.{self.reason}",
            lane=SYSTEM_ISSUE_LANE,
            severity=SYSTEM_ISSUE_SEVERITY,
            summary=(
                "The supervisor audit could not be judged; its reason class is "
                f"{self.reason}."
            ),
            recommendation=(
                "Re-run the audit for this run with `asl assurance process-pending` "
                "and read the named attempt artifacts if it fails the same way."
            ),
            evidence=evidence,
        )

    def to_summary(
        self,
        *,
        issue_ids: Sequence[str] = (),
        new_issue_count: int = 0,
        recurring_issue_count: int = 0,
    ) -> AssuranceSummary:
        """Return the persisted summary once issue IDs are known."""
        return AssuranceSummary(
            run_id=self.run_id,
            state=self.state,
            eligible=self.eligible,
            reason=self.reason,
            role=self.role,
            attempts=self.attempts,
            artifacts=self.artifacts,
            issue_ids=tuple(issue_ids),
            new_issue_count=new_issue_count,
            recurring_issue_count=recurring_issue_count,
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_seconds=self.duration_seconds,
        )


@dataclass(frozen=True)
class PublicationResult:
    """What a publication attempt produced, whether or not it wrote anything."""

    summary: AssuranceSummary
    records: tuple[LedgerRecord, ...] = ()
    created: bool = True

    @property
    def issue_ids(self) -> tuple[str, ...]:
        return self.summary.issue_ids

    @property
    def observations(self) -> tuple[LedgerRecord, ...]:
        return tuple(
            record for record in self.records if record.kind == KIND_ISSUE_OBSERVED
        )


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


class _Allocator:
    """Issue-ID allocation for one transaction, merges already applied.

    The map starts from the ledger and is updated as findings are allocated, so
    a publication that opens several new issues numbers them consecutively
    without re-reading the file between them.
    """

    def __init__(self, view: LedgerView) -> None:
        edges = view.merge_edges
        self._by_fingerprint = {
            fingerprint: follow_merge_chain(edges, issue_id)
            for fingerprint, issue_id in view.issue_ids_by_fingerprint.items()
        }
        self._next = view.next_issue_number

    def allocate(self, fingerprint: str) -> tuple[str, bool]:
        """Return the issue this fingerprint belongs to and whether it is new."""
        existing = self._by_fingerprint.get(fingerprint)
        if existing is not None:
            return existing, False
        issue_id = f"{ISSUE_ID_PREFIX}{self._next}"
        self._next += 1
        self._by_fingerprint[fingerprint] = issue_id
        return issue_id, True


def publish_audit(
    publication: AuditPublication,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
    observed_at: str | None = None,
) -> PublicationResult:
    """Record one audit and the issues it opens as a single atomic append.

    Republishing an audit already in the ledger writes nothing and returns the
    summary that was stored the first time, so a retry, a resumed run, and two
    concurrent publishers all leave exactly one occurrence behind.
    """
    stamp = recorded_at or _utc_now()
    kind = publication.record_kind
    sighted_at = observed_at or publication.completed_at or stamp
    report = audit_report_locator(publication.artifacts)
    prior: list[AssuranceSummary] = []
    written: list[AssuranceSummary] = []

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        key = publication_key(kind, publication.run_id, publication.audit_input_digest)
        already = _publications(view).get(key)
        if already is not None:
            prior.append(already)
            return ()
        allocator = _Allocator(view)
        issue_ids: list[str] = []
        new_ids: set[str] = set()
        observations: list[PendingRecord] = []
        for finding in publication.issue_findings():
            issue_id, is_new = allocator.allocate(finding.fingerprint)
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
        if publication.linked_issue_ids:
            opened_by = _issue_opening_runs(view)
            for linked in publication.linked_issue_ids:
                canonical = view.canonical_issue_id(linked)
                if canonical not in view.allocated_issue_ids:
                    raise ProjectionError(
                        UNKNOWN_ISSUE, f"{linked} names no issue in the inbox"
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
        return (
            PendingRecord(kind=kind, payload=summary, recorded_at=stamp),
            *observations,
        )

    records = ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)
    if not records:
        return PublicationResult(summary=prior[0], records=(), created=False)
    return PublicationResult(summary=written[0], records=records, created=True)


# ---------------------------------------------------------------------------
# Lifecycle writes
# ---------------------------------------------------------------------------


def _merge_rejection(
    *,
    source: str,
    target: str,
    known: frozenset[str] | set[str],
    edges: Mapping[str, str],
) -> str | None:
    """Return why this merge cannot be applied, or ``None`` when it can.

    The cycle test runs before the already-merged-target test so that merging
    back into an ancestor is reported as the cycle it is rather than as an
    incidental property of the target.
    """
    if source == target:
        return MERGE_SELF
    if source not in known:
        return MERGE_UNKNOWN_SOURCE
    if target not in known:
        return MERGE_UNKNOWN_TARGET
    if source in edges:
        return MERGE_SOURCE_ALREADY_MERGED
    if follow_merge_chain(edges, target) == source:
        return MERGE_CYCLE
    if target in edges:
        return MERGE_TARGET_ALREADY_MERGED
    return None


def record_disposition(
    issue_id: str,
    kind: DispositionKind,
    *,
    note: str | None = None,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append a human decision about an issue, naming its canonical owner."""
    requested = parse_issue_id(issue_id)
    stamp = recorded_at or _utc_now()

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        canonical = view.canonical_issue_id(requested)
        if canonical not in view.allocated_issue_ids:
            raise ProjectionError(
                UNKNOWN_ISSUE, f"{requested} names no issue in the inbox"
            )
        return (
            PendingRecord(
                kind=KIND_ISSUE_DISPOSITION,
                payload=Disposition(
                    issue_id=canonical, kind=kind, recorded_at=stamp, note=note
                ),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


def record_merge(
    source_issue_id: str,
    target_issue_id: str,
    *,
    note: str | None = None,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append an irreversible redirection of one issue's history into another."""
    source = parse_issue_id(source_issue_id)
    target = parse_issue_id(target_issue_id)
    stamp = recorded_at or _utc_now()

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        rejection = _merge_rejection(
            source=source,
            target=target,
            known=view.allocated_issue_ids,
            edges=view.merge_edges,
        )
        if rejection is not None:
            raise ProjectionError(
                rejection,
                _MERGE_REJECTION_DETAIL[rejection].format(source=source, target=target),
            )
        return (
            PendingRecord(
                kind=KIND_ISSUE_MERGED,
                payload=IssueMerge(
                    source_issue_id=source,
                    target_issue_id=target,
                    recorded_at=stamp,
                    note=note,
                ),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


def record_improvement_plan_draft(
    draft: ImprovementPlanDraft,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append the record that a proposal was drafted for an issue.

    The draft carries the digest of a file that already exists on disk, so this
    append is the last step of drafting rather than a promise about one: the
    canonical issue is resolved inside the transaction, and a request naming an
    unknown issue is refused with nothing written.
    """
    requested = parse_issue_id(draft.issue_id)
    stamp = recorded_at or _utc_now()

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        canonical = view.canonical_issue_id(requested)
        if canonical not in view.allocated_issue_ids:
            raise ProjectionError(
                UNKNOWN_ISSUE, f"{requested} names no issue in the inbox"
            )
        return (
            PendingRecord(
                kind=KIND_IMPROVEMENT_PLAN_DRAFTED,
                payload=replace_dataclass(draft, issue_id=canonical),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


#: How a stored closeout is named by the closeout that supersedes it. A
#: closeout mints no identifier of its own, and the ledger line holding it is
#: the one name that stays true: records are appended and never rewritten, so a
#: physical sequence number always points at the same bytes.
CLOSEOUT_REFERENCE_PREFIX = "seq-"


def closeout_reference(seq: int) -> str:
    """Name the stored closeout on ledger line ``seq``."""
    return f"{CLOSEOUT_REFERENCE_PREFIX}{seq}"


def record_closeout(
    run_id: str,
    outcome: CloseoutOutcome,
    *,
    issue_ids: Sequence[str] = (),
    rework_minutes: float | None = None,
    note: str | None = None,
    plan_id: str | None = None,
    replace: bool = False,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
) -> LedgerRecord:
    """Append a human's verdict on what one run's audit was worth.

    Superseding is an append like everything else: ``replace`` does not rewrite
    the closeout already on file, it writes a new one that names the line the
    old one occupies, so both verdicts and the order they were reached in stay
    readable forever.
    """
    stamp = recorded_at or _utc_now()
    # Everything the model can judge is judged before the lock is taken, so a
    # malformed request never blocks a writer that has something valid to say.
    draft = Closeout(
        run_id=run_id,
        outcome=outcome,
        recorded_at=stamp,
        # Naming one issue twice is a request to judge it, not an error, and
        # merges collapse distinct references onto one owner anyway.
        issue_ids=tuple(dict.fromkeys(parse_issue_id(item) for item in issue_ids)),
        rework_minutes=rework_minutes,
        note=note,
        plan_id=plan_id,
    )

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        audited = any(
            record.kind in AUDIT_RECORD_KINDS and record.payload.run_id == draft.run_id
            for record in view.records
        )
        if not audited:
            raise ProjectionError(
                UNKNOWN_RUN, f"{draft.run_id} has no audit record in the inbox"
            )
        linked: list[str] = []
        for issue_id in draft.issue_ids:
            canonical = view.canonical_issue_id(issue_id)
            if canonical not in view.allocated_issue_ids:
                raise ProjectionError(
                    UNKNOWN_ISSUE, f"{issue_id} names no issue in the inbox"
                )
            if canonical not in linked:
                linked.append(canonical)
        prior = [
            record
            for record in view.records_of_kind(KIND_HUMAN_CLOSEOUT)
            if record.payload.run_id == draft.run_id
        ]
        if prior and not replace:
            raise ProjectionError(
                DUPLICATE_CLOSEOUT,
                f"{draft.run_id} was already closed out at "
                f"{prior[-1].payload.recorded_at}",
            )
        return (
            PendingRecord(
                kind=KIND_HUMAN_CLOSEOUT,
                payload=replace_dataclass(
                    draft,
                    issue_ids=tuple(linked),
                    replaces=(closeout_reference(prior[-1].seq) if prior else None),
                ),
            ),
        )

    return ledger.append_transaction(build, state_dir=state_dir, recorded_at=stamp)[0]


# ---------------------------------------------------------------------------
# Projected state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueOccurrence:
    """One sighting of an issue, kept in the order the ledger recorded it."""

    seq: int
    run_id: str
    observed_at: str
    fingerprint: str
    severity: str
    audit_artifact: str | None = None


@dataclass(frozen=True)
class Issue:
    """One inbox issue as the ledger currently describes it."""

    issue_id: str
    state: IssueState
    finding: Finding
    occurrences: tuple[IssueOccurrence, ...]
    fingerprints: tuple[str, ...]
    dispositions: tuple[Disposition, ...] = ()
    plans: tuple[ImprovementPlanDraft, ...] = ()
    merged_from: tuple[str, ...] = ()
    reopened_count: int = 0

    @property
    def fingerprint(self) -> str:
        """The identity this issue was opened under."""
        return self.fingerprints[0]

    @property
    def occurrence_count(self) -> int:
        return len(self.occurrences)

    @property
    def first_seen_at(self) -> str:
        return self.occurrences[0].observed_at

    @property
    def last_seen_at(self) -> str:
        return self.occurrences[-1].observed_at

    @property
    def run_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for occurrence in self.occurrences:
            seen[occurrence.run_id] = None
        return tuple(seen)

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True)
class RunAudit:
    """Everything the ledger records about one run's audit."""

    run_id: str
    state: AuditState
    summary: AssuranceSummary
    issue_ids: tuple[str, ...] = ()
    summaries: tuple[AssuranceSummary, ...] = ()
    closeouts: tuple[Closeout, ...] = ()

    @property
    def closeout(self) -> Closeout | None:
        """The closeout in force, later records replacing earlier ones."""
        return self.closeouts[-1] if self.closeouts else None


@dataclass(frozen=True)
class ProjectionAnomaly:
    """A stored record that could not mean anything and was therefore skipped."""

    seq: int
    kind: str
    reason: str
    detail: str


@dataclass(frozen=True)
class AssuranceProjection:
    """The whole inbox, derived from ledger bytes and nothing else."""

    issues: tuple[Issue, ...] = ()
    runs: tuple[RunAudit, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    anomalies: tuple[ProjectionAnomaly, ...] = ()
    rejected: tuple[RejectedLedgerRecord, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))

    def resolve(self, issue_id: str) -> str:
        """Return the issue that owns ``issue_id``'s history today."""
        return follow_merge_chain(self.aliases, parse_issue_id(issue_id))

    def issue(self, issue_id: str) -> Issue | None:
        """Return the issue behind any reference, merged-away IDs included."""
        canonical = self.resolve(issue_id)
        for item in self.issues:
            if item.issue_id == canonical:
                return item
        return None

    def run(self, run_id: str) -> RunAudit | None:
        for item in self.runs:
            if item.run_id == run_id:
                return item
        return None

    @property
    def open_issues(self) -> tuple[Issue, ...]:
        return tuple(item for item in self.issues if item.is_open)


_OBSERVED = "observed"
_DISPOSED = "disposed"
_PLANNED = "planned"


class _IssueNode:
    """One issue under construction, holding its events until merges settle.

    Events are kept raw and replayed once at the end rather than folded as they
    arrive, because a merge moves a whole history into another issue and the
    combined lifecycle has to be read in ledger order, not in the order the two
    histories happened to be assembled.
    """

    def __init__(self, issue_id: str) -> None:
        self.issue_id = issue_id
        self.events: list[tuple[int, str, Any]] = []
        self.merged_from: list[str] = []

    def absorb(self, other: _IssueNode) -> None:
        self.events.extend(other.events)
        self.merged_from.append(other.issue_id)
        self.merged_from.extend(other.merged_from)


class _RunNode:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.summaries: list[AssuranceSummary] = []
        self.closeouts: list[Closeout] = []


def _build_issue(node: _IssueNode) -> Issue:
    state: IssueState = "open"
    reopened = 0
    occurrences: list[IssueOccurrence] = []
    fingerprints: list[str] = []
    dispositions: list[Disposition] = []
    plans: list[ImprovementPlanDraft] = []
    finding: Finding | None = None
    for _seq, kind, payload in sorted(node.events, key=lambda item: item[0]):
        if kind == _OBSERVED:
            occurrence, observed = payload
            occurrences.append(occurrence)
            finding = observed
            if observed.fingerprint not in fingerprints:
                fingerprints.append(observed.fingerprint)
            # A defect that came back after a fix means the fix did not hold. A
            # defect that came back after a human dismissed it or accepted its
            # risk means only that the human was right about it recurring, so
            # the occurrence history advances and the decision stands.
            if state == "resolved":
                state = "open"
                reopened += 1
        elif kind == _DISPOSED:
            dispositions.append(payload)
            state = payload.result_state
        else:
            plans.append(payload)
    return Issue(
        issue_id=node.issue_id,
        state=state,
        finding=finding,
        occurrences=tuple(occurrences),
        fingerprints=tuple(fingerprints),
        dispositions=tuple(dispositions),
        plans=tuple(plans),
        merged_from=tuple(sorted(node.merged_from, key=issue_id_number)),
        reopened_count=reopened,
    )


def project(view: LedgerView) -> AssuranceProjection:
    """Replay the ledger into the inbox state it describes."""
    nodes: dict[str, _IssueNode] = {}
    runs: dict[str, _RunNode] = {}
    aliases: dict[str, str] = {}
    anomalies: list[ProjectionAnomaly] = []

    def note(record: LedgerRecord, reason: str, detail: str) -> None:
        anomalies.append(
            ProjectionAnomaly(
                seq=record.seq, kind=record.kind, reason=reason, detail=detail
            )
        )

    for record in view.records:
        payload = record.payload
        if record.kind == KIND_ISSUE_OBSERVED:
            # A writer canonicalizes the ID it stores, but the ledger cannot be
            # edited: a hand-written record naming an already-merged source
            # would otherwise resurrect that source as a separate issue no
            # merge chain points at.
            issue_id = follow_merge_chain(aliases, record.issue_id)
            node = nodes.get(issue_id)
            if node is None:
                node = nodes[issue_id] = _IssueNode(issue_id)
            node.events.append(
                (
                    record.seq,
                    _OBSERVED,
                    (
                        IssueOccurrence(
                            seq=record.seq,
                            run_id=payload.run_id,
                            observed_at=payload.observed_at,
                            fingerprint=payload.fingerprint,
                            severity=payload.finding.severity,
                            audit_artifact=payload.audit_artifact,
                        ),
                        payload.finding,
                    ),
                )
            )
        elif record.kind == KIND_ISSUE_DISPOSITION:
            node = nodes.get(follow_merge_chain(aliases, payload.issue_id))
            if node is None:
                note(
                    record,
                    UNKNOWN_ISSUE,
                    f"{payload.issue_id} names no issue observed before this record",
                )
                continue
            node.events.append((record.seq, _DISPOSED, payload))
        elif record.kind == KIND_IMPROVEMENT_PLAN_DRAFTED:
            node = nodes.get(follow_merge_chain(aliases, payload.issue_id))
            if node is None:
                note(
                    record,
                    UNKNOWN_ISSUE,
                    f"{payload.issue_id} names no issue observed before this record",
                )
                continue
            node.events.append((record.seq, _PLANNED, payload))
        elif record.kind == KIND_ISSUE_MERGED:
            source = payload.source_issue_id
            target = payload.target_issue_id
            rejection = _merge_rejection(
                source=source,
                target=target,
                known=set(nodes) | set(aliases),
                edges=aliases,
            )
            if rejection is not None:
                note(
                    record,
                    rejection,
                    _MERGE_REJECTION_DETAIL[rejection].format(
                        source=source, target=target
                    ),
                )
                continue
            aliases[source] = target
            nodes[target].absorb(nodes.pop(source))
        elif record.kind == KIND_HUMAN_CLOSEOUT:
            run = runs.get(payload.run_id)
            if run is None:
                # A closeout is a verdict on a published audit, so one that
                # names a run with no audit record judges nothing.
                note(
                    record,
                    UNKNOWN_RUN,
                    f"{payload.run_id} has no audit record before this closeout",
                )
                continue
            run.closeouts.append(payload)
        else:
            run = runs.get(payload.run_id)
            if run is None:
                run = runs[payload.run_id] = _RunNode(payload.run_id)
            run.summaries.append(payload)

    issues = tuple(
        _build_issue(nodes[issue_id]) for issue_id in sorted(nodes, key=issue_id_number)
    )
    projected_runs = []
    for run_id in sorted(runs):
        run = runs[run_id]
        latest = run.summaries[-1]
        issue_ids: list[str] = []
        for summary in run.summaries:
            for issue_id in summary.issue_ids:
                canonical = follow_merge_chain(aliases, issue_id)
                if canonical not in issue_ids:
                    issue_ids.append(canonical)
        projected_runs.append(
            RunAudit(
                run_id=run_id,
                state=latest.state,
                summary=latest,
                issue_ids=tuple(issue_ids),
                summaries=tuple(run.summaries),
                closeouts=tuple(run.closeouts),
            )
        )
    return AssuranceProjection(
        issues=issues,
        runs=tuple(projected_runs),
        aliases=aliases,
        anomalies=tuple(anomalies),
        rejected=view.rejected,
    )


def read_projection(*, state_dir: str | Path | None = None) -> AssuranceProjection:
    """Read the ledger and return the inbox state it describes."""
    return project(ledger.read_ledger(state_dir=state_dir))
