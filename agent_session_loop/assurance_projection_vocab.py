"""Assurance projection vocabulary, stable refusal reasons, and recommendations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType

from .assurance_ledger import (
    KIND_AUDIT_COMPLETED,
    KIND_AUDIT_PENDING,
    KIND_AUDIT_SKIPPED,
    KIND_AUDIT_UNAVAILABLE,
)
from .assurance_models_primitives import (
    AssuranceError,
)
from .assurance_models_vocab import (
    RubricLane,
    Severity,
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
