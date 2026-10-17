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
  cannot be audited."""

from __future__ import annotations

from .assurance_identity_catalog import (
    CATALOG_DIGEST_VERSION,
    ISSUE_IDENTITY_DIGEST_VERSION,
    MATCH_INVARIANT,
    MATCH_LOCALIZATION,
    MATCH_SUMMARY,
    MAX_IDENTITY_CANDIDATES,
    MIN_SUMMARY_SIMILARITY,
    CatalogEntry,
    IdentityCandidate,
    IdentityCatalog,
    _catalog_digest,
    _catalog_entry,
    _jaccard,
    build_catalog,
    candidate_order,
    issue_identity_digest,
)
from .assurance_issue_actions import (
    CLOSEOUT_REFERENCE_PREFIX,
    _merge_rejection,
    closeout_reference,
    merge_rejection,
    record_closeout,
    record_disposition,
    record_improvement_plan_draft,
    record_merge,
)
from .assurance_projection_models import (
    AssuranceProjection,
    Issue,
    IssueOccurrence,
    ProjectionAnomaly,
    RunAudit,
    issue_pair_key,
)
from .assurance_projection_replay import _DISPOSED as _DISPOSED
from .assurance_projection_replay import _OBSERVED as _OBSERVED
from .assurance_projection_replay import _PLANNED as _PLANNED
from .assurance_projection_replay import _build_issue as _build_issue
from .assurance_projection_replay import _identity_key_order as _identity_key_order
from .assurance_projection_replay import _IssueNode as _IssueNode
from .assurance_projection_replay import _RunNode as _RunNode
from .assurance_projection_replay import project as project
from .assurance_projection_replay import read_projection as read_projection
from .assurance_projection_vocab import (
    _MERGE_REJECTION_DETAIL,
    _REAUDIT_COMMAND,
    AUDIT_KIND_FOR_STATE,
    AUDIT_RECORD_KINDS,
    DEFAULT_UNAVAILABLE_RECOMMENDATION,
    DUPLICATE_CLOSEOUT,
    DUPLICATE_IDENTITY_BINDING,
    IDENTITY_ANOMALY_REASONS,
    IDENTITY_KEY_CONFLICT,
    ISSUE_WITHOUT_OBSERVATION,
    MERGE_CYCLE,
    MERGE_REJECTIONS,
    MERGE_SELF,
    MERGE_SOURCE_ALREADY_MERGED,
    MERGE_TARGET_ALREADY_MERGED,
    MERGE_UNKNOWN_SOURCE,
    MERGE_UNKNOWN_TARGET,
    NO_FINDING_STATES,
    OBSERVATION_WITHOUT_ISSUE_ID,
    SYSTEM_ISSUE_CATEGORY,
    SYSTEM_ISSUE_COMPONENT,
    SYSTEM_ISSUE_LANE,
    SYSTEM_ISSUE_SEVERITY,
    UNAVAILABLE_EVIDENCE_REQUIRED,
    UNAVAILABLE_INVARIANT_PREFIX,
    UNAVAILABLE_RECOMMENDATIONS,
    UNKNOWN_ISSUE,
    UNKNOWN_RUN,
    ProjectionError,
    _utc_now,
)
from .assurance_publication import _IdentityResolver, _issue_opening_runs, publish_audit
from .assurance_publication_identity import (
    _named_artifact,
    _publications,
    _unavailable_evidence,
    audit_input_digest,
    audit_report_locator,
    publication_key,
)
from .assurance_publication_models import AuditPublication, PublicationResult

__all__ = [
    "AUDIT_KIND_FOR_STATE",
    "AUDIT_RECORD_KINDS",
    "AssuranceProjection",
    "AuditPublication",
    "CATALOG_DIGEST_VERSION",
    "CLOSEOUT_REFERENCE_PREFIX",
    "CatalogEntry",
    "DEFAULT_UNAVAILABLE_RECOMMENDATION",
    "DUPLICATE_CLOSEOUT",
    "DUPLICATE_IDENTITY_BINDING",
    "IDENTITY_ANOMALY_REASONS",
    "IDENTITY_KEY_CONFLICT",
    "ISSUE_IDENTITY_DIGEST_VERSION",
    "ISSUE_WITHOUT_OBSERVATION",
    "IdentityCandidate",
    "IdentityCatalog",
    "Issue",
    "IssueOccurrence",
    "MATCH_INVARIANT",
    "MATCH_LOCALIZATION",
    "MATCH_SUMMARY",
    "MAX_IDENTITY_CANDIDATES",
    "MERGE_CYCLE",
    "MERGE_REJECTIONS",
    "MERGE_SELF",
    "MERGE_SOURCE_ALREADY_MERGED",
    "MERGE_TARGET_ALREADY_MERGED",
    "MERGE_UNKNOWN_SOURCE",
    "MERGE_UNKNOWN_TARGET",
    "MIN_SUMMARY_SIMILARITY",
    "NO_FINDING_STATES",
    "OBSERVATION_WITHOUT_ISSUE_ID",
    "ProjectionAnomaly",
    "ProjectionError",
    "PublicationResult",
    "RunAudit",
    "SYSTEM_ISSUE_CATEGORY",
    "SYSTEM_ISSUE_COMPONENT",
    "SYSTEM_ISSUE_LANE",
    "SYSTEM_ISSUE_SEVERITY",
    "UNAVAILABLE_EVIDENCE_REQUIRED",
    "UNAVAILABLE_INVARIANT_PREFIX",
    "UNAVAILABLE_RECOMMENDATIONS",
    "UNKNOWN_ISSUE",
    "UNKNOWN_RUN",
    "_IdentityResolver",
    "_MERGE_REJECTION_DETAIL",
    "_REAUDIT_COMMAND",
    "_catalog_digest",
    "_catalog_entry",
    "_issue_opening_runs",
    "_jaccard",
    "_merge_rejection",
    "_named_artifact",
    "_publications",
    "_unavailable_evidence",
    "_utc_now",
    "audit_input_digest",
    "audit_report_locator",
    "build_catalog",
    "candidate_order",
    "closeout_reference",
    "issue_identity_digest",
    "issue_pair_key",
    "merge_rejection",
    "project",
    "publication_key",
    "publish_audit",
    "read_projection",
    "record_closeout",
    "record_disposition",
    "record_improvement_plan_draft",
    "record_merge",
]
