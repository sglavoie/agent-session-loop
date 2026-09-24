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
  audit record, every observation it opens, and the identity key of every ID it
  mints are written in one transaction under the ledger's exclusive lock, and a
  publisher that finds its own key already recorded writes nothing at all. Two
  processes publishing the same audit therefore produce one occurrence, not two.
* **Identity is the fingerprint, never the text.** A recurring defect routes to
  the issue its first sighting minted. An audit that could not run deduplicates
  by its stable reason class, so a thousand distinct exception messages of one
  kind remain one issue with a thousand occurrences. A selection an audit made
  is revalidated against the ledger as it stands under that same lock, so a
  decision reviewed against identity state that has since moved is refreshed
  rather than allocated from.
* **Closing is a claim about the future, not about history.** A recurrence after
  ``resolve`` reopens the issue, because the fix evidently did not hold. A
  recurrence after ``dismiss`` or ``accept-risk`` does not: a human already
  decided this defect is tolerable, and the new sighting advances the occurrence
  history without reversing that decision.
* **Merge is irreversible and always forward.** A merged issue can never be
  merged again or un-merged, its whole history moves to the target, and later
  observations of either fingerprint land on the final canonical target.
* **Declining to merge is a fact too.** A reviewed pair that is two defects is
  recorded with the identity digest each issue had when it was read, so the
  suggestion stops being re-proposed while both issues still read that way and
  becomes a question again once one of them materially changes.
* **An identity key outlives the issue it was minted for.** Bindings are
  replayed against the settled merge chain, so a key keeps naming whoever owns
  that history today without any stored record being reinterpreted. The same
  replay produces the identity catalog: every canonical issue in every
  lifecycle state, indexed by fingerprint, key, invariant, normalized
  localization, and normalized summary token, so a recurrence can be recognized
  without scanning the inbox.
* **A hostile ledger is readable.** Records that cannot mean anything — a
  disposition for an issue that was never opened, a merge naming an unknown or
  cyclic target — are reported as anomalies and skipped. The projection never
  raises on stored bytes, because a ledger that cannot be read is a ledger that
  cannot be audited.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from dataclasses import replace as replace_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from . import assurance_ledger as ledger
from .artifacts import sha256_hex
from .assurance_ledger import (
    KIND_AUDIT_COMPLETED,
    KIND_AUDIT_PENDING,
    KIND_AUDIT_SKIPPED,
    KIND_AUDIT_UNAVAILABLE,
    KIND_HUMAN_CLOSEOUT,
    KIND_IMPROVEMENT_PLAN_DRAFTED,
    KIND_ISSUE_DISPOSITION,
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_KEPT_DISTINCT,
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
    IssueIdentityBinding,
    IssueKeptDistinct,
    IssueMerge,
    IssueObservation,
    IssueState,
    RubricLane,
    Severity,
    canonical_json_bytes,
    identity_key_for_issue,
    issue_id_for_identity_key,
    issue_id_number,
    normalize_invariant_tokens,
    normalize_localization,
    normalize_summary_tokens,
    parse_identity_key,
    parse_issue_id,
)

#: The record kind each audit state is stored under. Kind and state are two
#: spellings of one fact and the ledger refuses a record where they disagree.
AUDIT_KIND_FOR_STATE: Mapping[str, str] = {
    "pending": KIND_AUDIT_PENDING,
    "all-clear": KIND_AUDIT_COMPLETED,
    "findings": KIND_AUDIT_COMPLETED,
    # A partial audit completed: it ran, judged what it could see, and found
    # nothing wrong. Its incomplete coverage is reported, not treated as an
    # outage of the audit itself.
    "partial": KIND_AUDIT_COMPLETED,
    "skipped": KIND_AUDIT_SKIPPED,
    "unavailable": KIND_AUDIT_UNAVAILABLE,
}

AUDIT_RECORD_KINDS: frozenset[str] = frozenset(AUDIT_KIND_FOR_STATE.values())

#: States that assert no analysis produced a finding. ``unavailable`` is absent
#: on purpose: it opens exactly one system issue for its reason class, and may
#: additionally carry whatever deterministic findings were reached first.
NO_FINDING_STATES: frozenset[str] = frozenset(
    {"all-clear", "partial", "skipped", "pending"}
)

#: Identity of the system issue an unavailable audit opens. The reason class is
#: the only varying part, which is what keeps a million distinct exception
#: messages of one kind inside a single issue.
SYSTEM_ISSUE_CATEGORY = "assurance"
SYSTEM_ISSUE_COMPONENT = "agent-session-loop/supervisor-audit"
SYSTEM_ISSUE_LANE: RubricLane = "unknown-anomalies"
SYSTEM_ISSUE_SEVERITY: Severity = "medium"
UNAVAILABLE_INVARIANT_PREFIX = "audit.availability"

#: How a re-audit is actually requested. A bare ``process-pending`` pass reports
#: a run that already has an unavailable record as already audited, so the run
#: has to be named; and only a run whose runner died owing its audit has the
#: catch-up record that command reads. Kept in one place so every
#: recommendation below names the command the CLI really accepts.
_REAUDIT_COMMAND = (
    "`asl assurance process-pending --run-id RUN_ID` (a run listed on this "
    "issue; it works for a run whose runner died before auditing it, and a run "
    "that audited itself has no catch-up record to re-audit from)"
)

#: What an operator is told to do about an audit that could not be judged. The
#: default fits a failure a re-run could clear, which is most of them.
DEFAULT_UNAVAILABLE_RECOMMENDATION = (
    f"Re-run the audit with {_REAUDIT_COMMAND}, and read the named attempt "
    "artifacts if it fails the same way or cannot be re-run."
)

#: Reason classes the default would be wrong for. A quota exhaustion is one:
#: re-running changes nothing until the provider's own limit resets, and reading
#: a defect's recommendation over an outage is part of what made this class look
#: like an ASL bug across nine runs (ASI-127). The key is spelled literally
#: because ``supervisor_audit`` imports this module and not the other way round;
#: it is ``supervisor_audit.REASON_QUOTA_EXHAUSTED``.
UNAVAILABLE_RECOMMENDATIONS: Mapping[str, str] = MappingProxyType(
    {
        "quota-exhausted": (
            "Nothing here is broken: the audit was refused because the account "
            f"is out of provider quota. Re-run it with {_REAUDIT_COMMAND} once "
            "the limit resets, and read the named attempt artifacts if it then "
            "fails for some other reason."
        ),
        "workspace-changed-during-audit": (
            f"Re-run the audit with {_REAUDIT_COMMAND} after workspace "
            "activity has stopped. Read supervisor-audit-workspace-change.json "
            "and the named attempt artifacts for the comparison; snapshots "
            "cannot identify who wrote and do not establish an auditor breach."
        ),
    }
)


UNKNOWN_ISSUE = "unknown-issue"
UNKNOWN_RUN = "unknown-run"
DUPLICATE_CLOSEOUT = "duplicate-closeout"
DUPLICATE_IDENTITY_BINDING = "duplicate-identity-binding"
IDENTITY_KEY_CONFLICT = "identity-key-conflict"
#: Anomaly reasons a stored identity binding can produce. They are counted
#: apart from each other because they mean different things: a duplicate
#: restates a binding already in force, while a conflict claims a key that
#: already names a different issue.
IDENTITY_ANOMALY_REASONS: tuple[str, ...] = (
    DUPLICATE_IDENTITY_BINDING,
    IDENTITY_KEY_CONFLICT,
)
MERGE_SELF = "merge-self"
MERGE_UNKNOWN_SOURCE = "merge-unknown-source"
MERGE_UNKNOWN_TARGET = "merge-unknown-target"
MERGE_SOURCE_ALREADY_MERGED = "merge-source-already-merged"
MERGE_TARGET_ALREADY_MERGED = "merge-target-already-merged"
MERGE_CYCLE = "merge-cycle"
UNAVAILABLE_EVIDENCE_REQUIRED = "unavailable-evidence-required"
#: An observation record reached the replay without the issue id its kind is
#: required to allocate. `_record_from_envelope` rejects that shape at the parse
#: boundary, so it can only arrive from a `LedgerRecord` built in code; the
#: replay refuses it rather than projecting an issue under a null identity.
OBSERVATION_WITHOUT_ISSUE_ID = "observation-without-issue-id"
#: An issue node settled with no observation in its history. Nodes are only ever
#: created by an observation and merges move whole histories, so this cannot
#: happen for a ledger the replay itself assembled.
ISSUE_WITHOUT_OBSERVATION = "issue-without-observation"

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
    #: The issue an audit selected for a finding, keyed by that finding's
    #: fingerprint. A finding absent from this map asked for a new allocation,
    #: which is the only request that can mint an ID. Each selection is a request
    #: revalidated under the append lock, never an identity taken on trust.
    identity_selections: Mapping[str, str] = field(default_factory=dict)
    #: Digest of the identity catalog those selections were decided against, or
    #: ``None`` when no decision was reviewed against one. It is what makes a
    #: stale decision recognizable under the append lock.
    catalog_digest: str | None = None

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
        object.__setattr__(
            self,
            "identity_selections",
            MappingProxyType(
                {
                    fingerprint: parse_issue_id(issue_id)
                    for fingerprint, issue_id in self.identity_selections.items()
                }
            ),
        )
        for fingerprint in self.identity_selections:
            if fingerprint not in seen:
                raise AssuranceError(
                    f"identity selection {fingerprint} names a finding this "
                    "publication does not carry; a decision that cannot be "
                    "attributed to a finding would be applied to whichever "
                    "defect happened to line up"
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
            recommendation=UNAVAILABLE_RECOMMENDATIONS.get(
                self.reason or "", DEFAULT_UNAVAILABLE_RECOMMENDATION
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
    #: Whether identity had moved between the decision and the append lock. The
    #: publication is unaffected — every route is answered from the locked view
    #: either way — and this only reports that the refreshed state was the one
    #: that decided.
    identity_refreshed: bool = False

    @property
    def issue_ids(self) -> tuple[str, ...]:
        return self.summary.issue_ids

    @property
    def observations(self) -> tuple[LedgerRecord, ...]:
        return tuple(
            record for record in self.records if record.kind == KIND_ISSUE_OBSERVED
        )

    @property
    def bindings(self) -> tuple[LedgerRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.kind == KIND_ISSUE_IDENTITY_BOUND
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
            raise ProjectionError(
                UNKNOWN_ISSUE, f"{issue_id} names no issue in the inbox"
            )
        return canonical


def publish_audit(
    publication: AuditPublication,
    *,
    state_dir: str | Path | None = None,
    recorded_at: str | None = None,
    observed_at: str | None = None,
    promote: Callable[[], None] | None = None,
) -> PublicationResult:
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
    stamp = recorded_at or _utc_now()
    kind = publication.record_kind
    sighted_at = observed_at or publication.completed_at or stamp
    report = audit_report_locator(publication.artifacts)
    prior: list[AssuranceSummary] = []
    written: list[AssuranceSummary] = []
    refreshed: list[bool] = []

    def build(view: LedgerView) -> tuple[PendingRecord, ...]:
        key = publication_key(kind, publication.run_id, publication.audit_input_digest)
        already = _publications(view).get(key)
        if already is not None:
            prior.append(already)
            return ()
        if (
            publication.catalog_digest is not None
            and project(view).catalog.digest != publication.catalog_digest
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
        return PublicationResult(summary=prior[0], records=(), created=False)
    return PublicationResult(
        summary=written[0],
        records=records,
        created=True,
        identity_refreshed=bool(refreshed),
    )


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


def merge_rejection(
    *,
    source: str,
    target: str,
    known: frozenset[str] | set[str],
    edges: Mapping[str, str],
) -> ProjectionError | None:
    """Return the error refusing this merge, or ``None`` when it can be applied.

    Callers that build several merges into one transaction advance ``edges`` as
    they go and ask again, so each merge is judged against the state its
    siblings already established rather than against the ledger they all started
    from.
    """
    reason = _merge_rejection(source=source, target=target, known=known, edges=edges)
    if reason is None:
        return None
    return ProjectionError(
        reason, _MERGE_REJECTION_DETAIL[reason].format(source=source, target=target)
    )


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
        rejection = merge_rejection(
            source=source,
            target=target,
            known=view.allocated_issue_ids,
            edges=view.merge_edges,
        )
        if rejection is not None:
            raise rejection
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
    #: Every bound key that resolves here, lowest-numbered first. A merge moves
    #: the absorbed issue's key onto the issue that owns its history, so this
    #: holds more than one key exactly when this issue absorbed a named one.
    identity_keys: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """The identity this issue was opened under."""
        return self.fingerprints[0]

    @property
    def primary_identity_key(self) -> str | None:
        """The one key this issue should be named by, or ``None`` when unbound.

        An issue's own key wins whenever it is bound, because that is the name
        minted with the issue itself; an issue named only by keys it absorbed
        falls back to the lowest-numbered of them. Binding is first-write-wins
        per key, which settles who owns a key but not which of several keys an
        issue prefers, so the tie-break is decided here and never by write
        order.
        """
        if not self.identity_keys:
            return None
        own = identity_key_for_issue(self.issue_id)
        return own if own in self.identity_keys else self.identity_keys[0]

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


def issue_pair_key(left: str, right: str) -> tuple[str, str]:
    """Order two issue IDs by number, so both directions name one pair."""
    return (
        (left, right)
        if issue_id_number(left) <= issue_id_number(right)
        else (right, left)
    )


@dataclass(frozen=True)
class AssuranceProjection:
    """The whole inbox, derived from ledger bytes and nothing else."""

    issues: tuple[Issue, ...] = ()
    runs: tuple[RunAudit, ...] = ()
    aliases: Mapping[str, str] = field(default_factory=dict)
    anomalies: tuple[ProjectionAnomaly, ...] = ()
    rejected: tuple[RejectedLedgerRecord, ...] = ()
    #: Every bound identity key mapped to the issue that owns its history now,
    #: merges already followed.
    identity_bindings: Mapping[str, str] = field(default_factory=dict)
    #: Every reviewed keep-distinct pair, named by the issues that own those
    #: histories now, mapped to the identity digest each one was reviewed at.
    kept_distinct: Mapping[tuple[str, str], Mapping[str, str]] = field(
        default_factory=dict
    )
    #: Every keep-distinct ruling the replay accepted, as it was written and in
    #: ledger order. A caller asking whether a pair has already been ruled on has
    #: to read these rather than the stored records: a record replay rejected
    #: means nothing, and reading it as a standing ruling would let it answer for
    #: a review nobody made.
    rulings: tuple[IssueKeptDistinct, ...] = ()
    _catalog: IdentityCatalog | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))
        object.__setattr__(
            self,
            "identity_bindings",
            MappingProxyType(dict(self.identity_bindings)),
        )
        object.__setattr__(
            self,
            "kept_distinct",
            MappingProxyType(
                {
                    pair: MappingProxyType(dict(digests))
                    for pair, digests in self.kept_distinct.items()
                }
            ),
        )

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

    def issue_for_identity_key(self, identity_key: str) -> Issue | None:
        """Return the issue a bound key names today, or ``None`` when unbound."""
        issue_id = self.identity_bindings.get(parse_identity_key(identity_key))
        return None if issue_id is None else self.issue(issue_id)

    def run(self, run_id: str) -> RunAudit | None:
        for item in self.runs:
            if item.run_id == run_id:
                return item
        return None

    def is_kept_distinct(self, left: str, right: str) -> bool:
        """Whether a reviewed ruling still says these two are separate defects.

        The ruling was reached about the issues as they read then, so it holds
        only while both still read that way. A digest that has moved means this
        pair, in the shape it has now, is a question nobody has answered — which
        is why a stale ruling lapses into another review rather than suppressing
        the suggestion forever.
        """
        reviewed = self.kept_distinct.get(
            issue_pair_key(self.resolve(left), self.resolve(right))
        )
        if reviewed is None:
            return False
        catalog = self.catalog
        return all(
            catalog.identity_digest(issue_id) == digest
            for issue_id, digest in reviewed.items()
        )

    @property
    def open_issues(self) -> tuple[Issue, ...]:
        return tuple(item for item in self.issues if item.is_open)

    @property
    def catalog(self) -> IdentityCatalog:
        """The one identity catalog for this projection, built on first use.

        Built once and kept, because a projection is immutable: two catalogs of
        one projection could only ever be equal, and callers that recognize a
        defect against the catalog do so per finding rather than per audit.
        """
        catalog = self._catalog
        if catalog is None:
            catalog = build_catalog(self)
            object.__setattr__(self, "_catalog", catalog)
        return catalog


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


def _identity_key_order(identity_key: str) -> int:
    return issue_id_number(issue_id_for_identity_key(identity_key))


def _build_issue(node: _IssueNode, identity_keys: Sequence[str] = ()) -> Issue:
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
    if finding is None:
        raise ProjectionError(
            ISSUE_WITHOUT_OBSERVATION,
            f"{node.issue_id} settled with no observation in its history; an "
            "issue with no finding describes nothing to investigate",
        )
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
        identity_keys=tuple(sorted(identity_keys, key=_identity_key_order)),
    )


def project(view: LedgerView) -> AssuranceProjection:
    """Replay the ledger into the inbox state it describes."""
    nodes: dict[str, _IssueNode] = {}
    runs: dict[str, _RunNode] = {}
    aliases: dict[str, str] = {}
    bindings: dict[str, str] = {}
    rulings: list[IssueKeptDistinct] = []
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
            if record.issue_id is None:
                raise ProjectionError(
                    OBSERVATION_WITHOUT_ISSUE_ID,
                    f"record {record.seq} is an observation with no issue id; "
                    "an observation is the record that allocates one",
                )
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
        elif record.kind == KIND_ISSUE_IDENTITY_BOUND:
            # A binding names an issue; it never changes one's state. Replaying
            # it here is what makes a stored binding that cannot mean anything
            # visible rather than silently authoritative.
            issue_id = follow_merge_chain(aliases, payload.issue_id)
            if issue_id not in nodes:
                note(
                    record,
                    UNKNOWN_ISSUE,
                    f"{payload.issue_id} names no issue observed before this record",
                )
                continue
            bound = bindings.get(payload.identity_key)
            if bound is not None:
                note(
                    record,
                    DUPLICATE_IDENTITY_BINDING
                    if follow_merge_chain(aliases, bound) == issue_id
                    else IDENTITY_KEY_CONFLICT,
                    f"{payload.identity_key} already names {bound}",
                )
                continue
            bindings[payload.identity_key] = payload.issue_id
        elif record.kind == KIND_ISSUE_KEPT_DISTINCT:
            unknown = [
                issue_id
                for issue_id in payload.issue_ids
                if follow_merge_chain(aliases, issue_id) not in nodes
            ]
            if unknown:
                note(
                    record,
                    UNKNOWN_ISSUE,
                    f"{', '.join(unknown)} names no issue observed before this record",
                )
                continue
            rulings.append(payload)
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

    # Bindings resolve only now, against the settled merge chain. Storing the
    # resolved issue during replay would freeze an answer a later merge record
    # invalidates: a key names whoever owns that history today, and the ledger
    # is never rewritten to say so.
    identity_bindings: dict[str, str] = {}
    keys_by_issue: dict[str, list[str]] = {}
    for identity_key in sorted(bindings, key=_identity_key_order):
        canonical = follow_merge_chain(aliases, bindings[identity_key])
        identity_bindings[identity_key] = canonical
        keys_by_issue.setdefault(canonical, []).append(identity_key)

    # Rulings resolve against the settled chain for the same reason, and a pair
    # whose members have since merged into one issue disappears rather than
    # becoming an anomaly: that merge is a later answer to the same question,
    # and the reviewer who gave it was entitled to change their mind. A later
    # ruling about one pair supersedes an earlier one, which is how a pair read
    # again after an issue moved settles on the digests it was last read at.
    kept_distinct: dict[tuple[str, str], dict[str, str]] = {}
    for ruling in rulings:
        digests: dict[str, str] = {}
        for issue_id, digest in ruling.reviewed_digests:
            digests.setdefault(follow_merge_chain(aliases, issue_id), digest)
        named = sorted(digests, key=issue_id_number)
        for position, left in enumerate(named):
            for right in named[position + 1 :]:
                kept_distinct[(left, right)] = {
                    left: digests[left],
                    right: digests[right],
                }

    issues = tuple(
        _build_issue(nodes[issue_id], keys_by_issue.get(issue_id, ()))
        for issue_id in sorted(nodes, key=issue_id_number)
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
        identity_bindings=identity_bindings,
        kept_distinct=kept_distinct,
        rulings=tuple(rulings),
    )


def read_projection(*, state_dir: str | Path | None = None) -> AssuranceProjection:
    """Read the ledger and return the inbox state it describes."""
    return project(ledger.read_ledger(state_dir=state_dir))


# ---------------------------------------------------------------------------
# Identity catalog
# ---------------------------------------------------------------------------

#: Version of the material :attr:`IdentityCatalog.digest` is taken over. It is
#: part of that material, so changing which fields the digest covers changes
#: every digest rather than silently making two different states collide.
CATALOG_DIGEST_VERSION = 1

#: Least normalized-summary overlap that makes one issue a recurrence candidate
#: for a finding. Two sightings of one defect are usually reworded rather than
#: rewritten, so a majority of shared content words is the signal; below this the
#: pair shares vocabulary rather than a subject.
MIN_SUMMARY_SIMILARITY = 0.60

#: Most candidates one finding is offered. A list long enough to read is worth
#: more than a complete one: a reader that has to weigh thirty near-matches is
#: being asked to redo the matching rather than to confirm it.
MAX_IDENTITY_CANDIDATES = 8

#: An identical invariant key under a different category or component. This is
#: the rename the fingerprint cannot see, since the fingerprint is all three.
MATCH_INVARIANT = "invariant"

#: The same source file, plus an overlapping invariant-key segment. This is the
#: rename the exact-invariant index cannot see. A file alone matches every
#: defect ASL ever localized to `runner.py`, and the segment overlap is what
#: separates a rewritten key from an unrelated defect in the same module.
MATCH_LOCALIZATION = "localization"

#: Normalized-summary overlap of at least :data:`MIN_SUMMARY_SIMILARITY`.
MATCH_SUMMARY = "summary"


@dataclass(frozen=True)
class IdentityCandidate:
    """One issue that could already own a finding, and why the catalog thinks so.

    A candidate is a question, never an answer. Every reason here is semantic —
    text an agent wrote, normalized — so acting on one automatically would let
    rewording a summary merge two unrelated defects. The exact paths, fingerprint
    and identity key, are deliberately not candidate reasons: they are matches,
    and a caller reuses them without asking anyone.
    """

    issue_id: str
    reasons: tuple[str, ...]
    #: Normalized-summary Jaccard overlap, or ``0.0`` when the summaries are not
    #: what matched. It is reported rather than thresholded away because two
    #: candidates that matched the same way are still not equally close.
    similarity: float = 0.0

    def merged(self, other: IdentityCandidate) -> IdentityCandidate:
        """Return one candidate for the same issue matched by two findings."""
        if other.issue_id != self.issue_id:
            raise AssuranceError(
                f"cannot merge candidates for {self.issue_id} and {other.issue_id}"
            )
        return IdentityCandidate(
            issue_id=self.issue_id,
            reasons=tuple(sorted(set(self.reasons) | set(other.reasons))),
            similarity=max(self.similarity, other.similarity),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "reasons": list(self.reasons),
            "similarity": round(self.similarity, 2),
        }


def candidate_order(candidate: IdentityCandidate) -> tuple[int, float, int]:
    """Rank the most corroborated candidate first, ties settled by issue number."""
    return (
        -len(candidate.reasons),
        -candidate.similarity,
        issue_id_number(candidate.issue_id),
    )


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    first, second = set(left), set(right)
    union = first | second
    return 0.0 if not union else len(first & second) / len(union)


@dataclass(frozen=True)
class CatalogEntry:
    """One canonical issue, in the shape a defect is recognized against.

    The entry carries the finding fields a recurrence can be recognized by and
    their normalized forms beside them, so a reader compares against the same
    text the index was built from rather than normalizing again and possibly
    differently.
    """

    issue_id: str
    state: IssueState
    severity: str
    category: str
    component: str
    invariant_key: str
    lane: str
    summary: str
    fingerprints: tuple[str, ...]
    identity_keys: tuple[str, ...]
    primary_identity_key: str | None
    localization: tuple[str, ...]
    summary_tokens: tuple[str, ...]
    invariant_tokens: tuple[str, ...]
    occurrence_count: int
    first_seen_at: str
    last_seen_at: str

    @property
    def fingerprint(self) -> str:
        """The identity this issue was opened under."""
        return self.fingerprints[0]


def _catalog_digest(entries: Sequence[CatalogEntry]) -> str:
    """Return the digest of the identity state ``entries`` describe.

    Only the fields that decide which issue owns a defect participate. An
    occurrence count and a last-seen timestamp change every time a known defect
    is seen again, and folding them in would expire a reviewed decision for the
    one event that proves the decision was right.
    """
    return sha256_hex(
        canonical_json_bytes(
            {
                "kind": "assurance-identity-catalog",
                "version": CATALOG_DIGEST_VERSION,
                "entries": [
                    {
                        "issue_id": entry.issue_id,
                        "state": entry.state,
                        "fingerprints": list(entry.fingerprints),
                        "identity_keys": list(entry.identity_keys),
                        "primary_identity_key": entry.primary_identity_key,
                    }
                    for entry in entries
                ],
            }
        )
    )


#: Version of the material :func:`issue_identity_digest` is taken over. It is
#: part of that material, so changing which fields participate changes every
#: digest rather than silently making two different issues collide.
ISSUE_IDENTITY_DIGEST_VERSION = 1


def issue_identity_digest(entry: CatalogEntry) -> str:
    """Return the digest of everything one issue is recognized as a defect by.

    This is the material a reviewer weighs when ruling on whether two issues are
    one defect: the fingerprints the issue has been seen under, and the exact and
    normalized text every candidate reason is drawn from.

    What is excluded is what a ruling cannot be invalidated by. Identity keys and
    lifecycle state are not here even though
    :func:`_catalog_digest` covers them, because naming an issue or deciding what
    to do about it says nothing about whether it and another issue describe the
    same defect; occurrence counts and timestamps are absent for the reason they
    are absent there. Folding any of them in would expire a reviewed ruling for
    events that cannot have changed it.
    """
    return sha256_hex(
        canonical_json_bytes(
            {
                "kind": "assurance-issue-identity",
                "version": ISSUE_IDENTITY_DIGEST_VERSION,
                "issue_id": entry.issue_id,
                "category": entry.category,
                "component": entry.component,
                "invariant_key": entry.invariant_key,
                "lane": entry.lane,
                "fingerprints": list(entry.fingerprints),
                "localization": list(entry.localization),
                "summary_tokens": list(entry.summary_tokens),
                "invariant_tokens": list(entry.invariant_tokens),
            }
        )
    )


@dataclass(frozen=True)
class IdentityCatalog:
    """Every canonical issue, indexed by the ways one defect is recognized.

    The catalog covers every lifecycle state rather than the open backlog: a
    defect that recurs after being resolved, dismissed, or accepted has to route
    back to the issue that already owns it, and an index of open issues alone
    would mint a second issue for exactly the recurrences the inbox exists to
    collapse.

    Lookups are exact and never resolve a merge chain. A merged-away ID names no
    catalog entry, which is what lets a caller fail closed on a stale reference
    instead of silently redirecting one.
    """

    entries: tuple[CatalogEntry, ...] = ()
    #: SHA-256 of the identity state these entries describe. Occurrence counts
    #: and timestamps are excluded on purpose: a decision reviewed against this
    #: catalog stays valid while a known defect merely recurs, and is invalidated
    #: only when an issue, a fingerprint, a key, or a lifecycle state moves.
    digest: str = field(default="", init=False, compare=False)
    by_issue_id: Mapping[str, CatalogEntry] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_fingerprint: Mapping[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_identity_key: Mapping[str, str] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_invariant: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_localization: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    by_summary_token: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        by_issue_id: dict[str, CatalogEntry] = {}
        by_fingerprint: dict[str, str] = {}
        by_identity_key: dict[str, str] = {}
        by_invariant: dict[str, list[str]] = {}
        by_localization: dict[str, list[str]] = {}
        by_summary_token: dict[str, list[str]] = {}
        for entry in self.entries:
            by_issue_id[entry.issue_id] = entry
            for fingerprint in entry.fingerprints:
                by_fingerprint.setdefault(fingerprint, entry.issue_id)
            for identity_key in entry.identity_keys:
                by_identity_key.setdefault(identity_key, entry.issue_id)
            by_invariant.setdefault(entry.invariant_key, []).append(entry.issue_id)
            for pointer in entry.localization:
                by_localization.setdefault(pointer, []).append(entry.issue_id)
            for token in entry.summary_tokens:
                by_summary_token.setdefault(token, []).append(entry.issue_id)
        object.__setattr__(self, "digest", _catalog_digest(self.entries))
        object.__setattr__(self, "by_issue_id", MappingProxyType(by_issue_id))
        object.__setattr__(self, "by_fingerprint", MappingProxyType(by_fingerprint))
        object.__setattr__(self, "by_identity_key", MappingProxyType(by_identity_key))
        for name, index in (
            ("by_invariant", by_invariant),
            ("by_localization", by_localization),
            ("by_summary_token", by_summary_token),
        ):
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    {key: tuple(issue_ids) for key, issue_ids in index.items()}
                ),
            )

    def entry(self, issue_id: str) -> CatalogEntry | None:
        return self.by_issue_id.get(parse_issue_id(issue_id))

    def identity_digest(self, issue_id: str) -> str | None:
        """Return one issue's recognition digest, or ``None`` when it names none."""
        entry = self.entry(issue_id)
        return None if entry is None else issue_identity_digest(entry)

    def issue_for_fingerprint(self, fingerprint: str) -> str | None:
        return self.by_fingerprint.get(fingerprint)

    def issue_for_identity_key(self, identity_key: str) -> str | None:
        return self.by_identity_key.get(parse_identity_key(identity_key))

    def issues_with_invariant(self, invariant_key: str) -> tuple[str, ...]:
        return self.by_invariant.get(invariant_key, ())

    def issues_localized_at(self, pointer: str) -> tuple[str, ...]:
        """Return every issue localized to the file ``pointer`` names."""
        return self.by_localization.get(normalize_localization(pointer), ())

    def issues_sharing_summary_tokens(self, tokens: Sequence[str]) -> tuple[str, ...]:
        """Return every issue whose summary shares a token, in catalog order.

        The inverted index answers which issues are worth comparing at all;
        scoring the overlap is the caller's, because how much overlap counts as
        one defect is a matching policy rather than a fact about the inbox.
        """
        matched: dict[str, None] = {}
        for token in tokens:
            for issue_id in self.by_summary_token.get(token, ()):
                matched[issue_id] = None
        return tuple(sorted(matched, key=issue_id_number))

    def candidates(
        self, finding: Finding, *, limit: int = MAX_IDENTITY_CANDIDATES
    ) -> tuple[IdentityCandidate, ...]:
        """Return the issues that could already own ``finding``, strongest first.

        Every issue considered arrives from an index, so the cost is the number
        of issues that share an invariant, a file, or a word with this finding
        rather than the size of the inbox. Scanning every pair would put the
        whole catalog in the publication path of every audit.

        The issue an exact fingerprint already names is excluded: that is a
        recurrence a caller reuses outright, and listing it as something to
        deliberate over would invite a reader to decline it.
        """
        recurrence = self.issue_for_fingerprint(finding.fingerprint)
        reasons: dict[str, set[str]] = {}
        similarity: dict[str, float] = {}

        def note(issue_id: str, reason: str) -> None:
            if issue_id != recurrence:
                reasons.setdefault(issue_id, set()).add(reason)

        for issue_id in self.issues_with_invariant(finding.invariant_key):
            note(issue_id, MATCH_INVARIANT)

        invariant_tokens = set(normalize_invariant_tokens(finding.invariant_key))
        localized: set[str] = set()
        for pointer in finding.localization:
            for issue_id in self.issues_localized_at(pointer):
                if issue_id in localized:
                    continue
                localized.add(issue_id)
                if invariant_tokens & set(self.by_issue_id[issue_id].invariant_tokens):
                    note(issue_id, MATCH_LOCALIZATION)

        summary_tokens = normalize_summary_tokens(finding.summary)
        for issue_id in self.issues_sharing_summary_tokens(summary_tokens):
            score = _jaccard(summary_tokens, self.by_issue_id[issue_id].summary_tokens)
            if score >= MIN_SUMMARY_SIMILARITY:
                note(issue_id, MATCH_SUMMARY)
                similarity[issue_id] = score

        candidates = sorted(
            (
                IdentityCandidate(
                    issue_id=issue_id,
                    reasons=tuple(sorted(found)),
                    similarity=similarity.get(issue_id, 0.0),
                )
                for issue_id, found in reasons.items()
            ),
            key=candidate_order,
        )
        return tuple(candidates[:limit])


def _catalog_entry(issue: Issue) -> CatalogEntry:
    finding = issue.finding
    localization: list[str] = []
    for pointer in finding.localization:
        normalized = normalize_localization(pointer)
        if normalized not in localization:
            localization.append(normalized)
    return CatalogEntry(
        issue_id=issue.issue_id,
        state=issue.state,
        severity=finding.severity,
        category=finding.category,
        component=finding.component,
        invariant_key=finding.invariant_key,
        lane=finding.lane,
        summary=finding.summary,
        fingerprints=issue.fingerprints,
        identity_keys=issue.identity_keys,
        primary_identity_key=issue.primary_identity_key,
        localization=tuple(localization),
        summary_tokens=normalize_summary_tokens(finding.summary),
        invariant_tokens=normalize_invariant_tokens(finding.invariant_key),
        occurrence_count=issue.occurrence_count,
        first_seen_at=issue.first_seen_at,
        last_seen_at=issue.last_seen_at,
    )


def build_catalog(inbox: AssuranceProjection) -> IdentityCatalog:
    """Return the identity catalog of one projection, every state included."""
    return IdentityCatalog(
        entries=tuple(_catalog_entry(issue) for issue in inbox.issues)
    )
