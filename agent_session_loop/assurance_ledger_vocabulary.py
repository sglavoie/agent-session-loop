"""Assurance ledger envelope versions, payload kinds, limits, and errors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final, Literal

from .assurance_models_inbox import (
    Closeout,
    Disposition,
    ImprovementPlanDraft,
    IssueIdentityBinding,
    IssueKeptDistinct,
    IssueMerge,
    IssueObservation,
    parse_closeout,
    parse_disposition,
    parse_improvement_plan_draft,
    parse_issue_identity_binding,
    parse_issue_kept_distinct,
    parse_issue_merge,
    parse_issue_observation,
)
from .assurance_models_primitives import AssuranceError
from .assurance_models_summary import AssuranceSummary, parse_assurance_summary
from .assurance_models_vocab import COMPLETED_AUDIT_STATES

#: Canonical ledger, its lock, and its rebuildable cache, all directly under the
#: state root because the inbox spans every plan rather than belonging to one.
LEDGER_FILE_NAME = "assurance-inbox.jsonl"

LEDGER_LOCK_FILE_NAME = "assurance-inbox.lock"

LEDGER_INDEX_FILE_NAME = "assurance-index.json"

#: Bumped when the meaning of the record envelope changes. Records written by a
#: newer build are rejected rather than guessed at. Every bump so far has added
#: one kind and changed nothing else -- v2 the identity binding, v3 the
#: keep-distinct ruling -- so a line means under a later version exactly what it
#: meant when it was written and the whole prior history reads without
#: reinterpretation.
LEDGER_SCHEMA_VERSION = 3

#: The cache format is versioned separately: it may change freely because a
#: version this build does not recognize simply triggers a rebuild.
LEDGER_INDEX_VERSION = 1

#: A single record is bounded well above any legitimate payload and far below a
#: captured log. The typed parsers already exclude raw content; this is the
#: structural backstop that holds even if a model later grows a text field.
MAX_LEDGER_RECORD_BYTES = 256 * 1024

#: How much of a rejected line is retained for diagnostics. The line itself
#: stays on disk untouched; only the in-memory view is truncated.
MAX_REJECTED_RAW_CHARS = 2000

LedgerKind = Literal[
    "audit_pending",
    "audit_completed",
    "audit_skipped",
    "audit_unavailable",
    "issue_observed",
    "issue_disposition",
    "issue_merged",
    "human_closeout",
    "improvement_plan_drafted",
    "issue_identity_bound",
    "issue_kept_distinct",
]

KIND_AUDIT_PENDING: Final = "audit_pending"

KIND_AUDIT_COMPLETED: Final = "audit_completed"

KIND_AUDIT_SKIPPED: Final = "audit_skipped"

KIND_AUDIT_UNAVAILABLE: Final = "audit_unavailable"

KIND_ISSUE_OBSERVED: Final = "issue_observed"

KIND_ISSUE_DISPOSITION: Final = "issue_disposition"

KIND_ISSUE_MERGED: Final = "issue_merged"

KIND_HUMAN_CLOSEOUT: Final = "human_closeout"

KIND_IMPROVEMENT_PLAN_DRAFTED: Final = "improvement_plan_drafted"

KIND_ISSUE_IDENTITY_BOUND: Final = "issue_identity_bound"

KIND_ISSUE_KEPT_DISTINCT: Final = "issue_kept_distinct"

#: Every record kind the ledger stores, in the order they occur in a run's life.
LEDGER_KINDS: tuple[LedgerKind, ...] = (
    KIND_AUDIT_PENDING,
    KIND_AUDIT_COMPLETED,
    KIND_AUDIT_SKIPPED,
    KIND_AUDIT_UNAVAILABLE,
    KIND_ISSUE_OBSERVED,
    KIND_ISSUE_DISPOSITION,
    KIND_ISSUE_MERGED,
    KIND_HUMAN_CLOSEOUT,
    KIND_IMPROVEMENT_PLAN_DRAFTED,
    KIND_ISSUE_IDENTITY_BOUND,
    KIND_ISSUE_KEPT_DISTINCT,
)

#: The kinds each readable schema version may carry. A line claiming a kind
#: introduced after its own version is rejected as an unknown kind for that
#: version rather than read as if the build that wrote it had meant the new one.
KINDS_BY_SCHEMA_VERSION: Mapping[int, frozenset[str]] = {
    1: frozenset(LEDGER_KINDS) - {KIND_ISSUE_IDENTITY_BOUND, KIND_ISSUE_KEPT_DISTINCT},
    2: frozenset(LEDGER_KINDS) - {KIND_ISSUE_KEPT_DISTINCT},
    3: frozenset(LEDGER_KINDS),
}

SUPPORTED_LEDGER_SCHEMA_VERSIONS: frozenset[int] = frozenset(KINDS_BY_SCHEMA_VERSION)

#: The only kind that mints an issue ID. Keeping the set explicit is what makes
#: "allocated under the append lock" a property of the module rather than of a
#: caller remembering to ask.
ISSUE_ALLOCATING_KINDS: frozenset[str] = frozenset({KIND_ISSUE_OBSERVED})

#: Kinds whose validity depends on the ledger as a whole rather than on the
#: record alone, and the entry point that decides them inside the append lock.
#: The generic writer refuses them so that lock-scoped validation is structural.
GUARDED_KINDS: Mapping[str, str] = {
    KIND_ISSUE_OBSERVED: "append_issue_observation",
    KIND_ISSUE_IDENTITY_BOUND: "append_identity_binding",
}

ENVELOPE_KEYS: tuple[str, ...] = (
    "version",
    "kind",
    "recorded_at",
    "issue_id",
    "payload",
)

#: An audit record's kind and its summary's state are two spellings of one fact.
#: Storing them without checking would let the ledger contradict itself.
_AUDIT_KIND_STATES: Mapping[str, frozenset[str]] = {
    KIND_AUDIT_PENDING: frozenset({"pending"}),
    KIND_AUDIT_COMPLETED: COMPLETED_AUDIT_STATES,
    KIND_AUDIT_SKIPPED: frozenset({"skipped"}),
    KIND_AUDIT_UNAVAILABLE: frozenset({"unavailable"}),
}

_PAYLOAD_PARSERS: Mapping[str, Callable[[Any], Any]] = {
    KIND_AUDIT_PENDING: parse_assurance_summary,
    KIND_AUDIT_COMPLETED: parse_assurance_summary,
    KIND_AUDIT_SKIPPED: parse_assurance_summary,
    KIND_AUDIT_UNAVAILABLE: parse_assurance_summary,
    KIND_ISSUE_OBSERVED: parse_issue_observation,
    KIND_ISSUE_DISPOSITION: parse_disposition,
    KIND_ISSUE_MERGED: parse_issue_merge,
    KIND_HUMAN_CLOSEOUT: parse_closeout,
    KIND_IMPROVEMENT_PLAN_DRAFTED: parse_improvement_plan_draft,
    KIND_ISSUE_IDENTITY_BOUND: parse_issue_identity_binding,
    KIND_ISSUE_KEPT_DISTINCT: parse_issue_kept_distinct,
}

_PAYLOAD_TYPES: Mapping[str, type[Any]] = {
    KIND_AUDIT_PENDING: AssuranceSummary,
    KIND_AUDIT_COMPLETED: AssuranceSummary,
    KIND_AUDIT_SKIPPED: AssuranceSummary,
    KIND_AUDIT_UNAVAILABLE: AssuranceSummary,
    KIND_ISSUE_OBSERVED: IssueObservation,
    KIND_ISSUE_DISPOSITION: Disposition,
    KIND_ISSUE_MERGED: IssueMerge,
    KIND_HUMAN_CLOSEOUT: Closeout,
    KIND_IMPROVEMENT_PLAN_DRAFTED: ImprovementPlanDraft,
    KIND_ISSUE_IDENTITY_BOUND: IssueIdentityBinding,
    KIND_ISSUE_KEPT_DISTINCT: IssueKeptDistinct,
}

REJECT_EMPTY_LINE = "empty-line"

REJECT_INVALID_JSON = "invalid-json"

REJECT_NOT_OBJECT = "not-object"

REJECT_UNSUPPORTED_VERSION = "unsupported-version"

REJECT_UNKNOWN_KIND = "unknown-kind"

REJECT_INVALID_RECORD = "invalid-record"

#: Why a physical line could not become a record. Every reason preserves the
#: line; none of them removes it.
REJECTION_REASONS: tuple[str, ...] = (
    REJECT_EMPTY_LINE,
    REJECT_INVALID_JSON,
    REJECT_NOT_OBJECT,
    REJECT_UNSUPPORTED_VERSION,
    REJECT_UNKNOWN_KIND,
    REJECT_INVALID_RECORD,
)

ERROR_LEDGER_READ = "ledger-read-failed"

ERROR_LEDGER_WRITE = "ledger-write-failed"

ERROR_RECORD_TOO_LARGE = "record-too-large"

ERROR_INVALID_KIND = "invalid-kind"

ERROR_INVALID_PAYLOAD = "invalid-payload"

ERROR_UNKNOWN_ISSUE = "unknown-issue"

ERROR_ISSUE_NOT_CANONICAL = "issue-not-canonical"

ERROR_IDENTITY_ALREADY_BOUND = "identity-key-already-bound"

ERROR_IDENTITY_KEY_CONFLICT = "identity-key-conflict"


class LedgerError(AssuranceError):
    """A stable assurance-ledger failure suitable for CLI translation."""

    def __init__(
        self,
        reason: str,
        detail: str | None = None,
        *,
        path: Path | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.path = path
        super().__init__(reason if detail is None else f"{reason}: {detail}")
