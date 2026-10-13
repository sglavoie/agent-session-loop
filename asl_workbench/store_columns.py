"""Column lists and bookkeeping sets the store checks against the allowlists."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

# Columns that are the store's own bookkeeping rather than something a source
# uploaded. Listed so the schema test can assert that every *other* column is a
# field the allowlist already permits.
_RUN_BOOKKEEPING: frozenset[str] = frozenset(
    {"connector_id", "received_at", "source_generation", "present", "absent_since"}
)
_HISTORY_BOOKKEEPING: frozenset[str] = frozenset({"connector_id", "received_at"})
_OPERATION_BOOKKEEPING: frozenset[str] = frozenset(
    {"connector_id", "received_at", "updated_at"}
)
_CATALOG_BOOKKEEPING: frozenset[str] = frozenset(
    {
        "connector_id",
        "generation",
        "observed_at",
        "received_at",
        "present",
        "absent_since",
    }
)

#: Every table whose columns are checked against an allowlist, with the
#: bookkeeping set that is allowed to be on neither side.
SCHEMA_BOOKKEEPING: Mapping[str, frozenset[str]] = {
    "run_summary": _RUN_BOOKKEEPING,
    "history_entry": _HISTORY_BOOKKEEPING,
    "operation": _OPERATION_BOOKKEEPING,
    "catalog_entry": _CATALOG_BOOKKEEPING,
}

# The persisted columns that are booleans in the record and integers on disk.
_HISTORY_BOOLEANS: tuple[str, ...] = ("review_completed", "reconstructed")
_CATALOG_BOOLEANS: tuple[str, ...] = ("exists", "pinned")
# The persisted columns that are sequences in the record and JSON text on disk.
_CATALOG_JSON: tuple[str, ...] = ("projects", "counts", "sources")

_RUN_COLUMNS: tuple[str, ...] = (
    "connector_id",
    "run_key",
    "project",
    "plan_key",
    "run_id",
    "session_id",
    "harness",
    "primary_state",
    "phase",
    "urgency_group",
    "pause_reason",
    "outcome",
    "question_id",
    "question_asked_at",
    "unanswered_count",
    "run_started_at",
    "run_finished_at",
    "last_activity_at",
    "observed_at",
    "received_at",
    "source_generation",
    "present",
    "absent_since",
)

_HISTORY_COLUMNS: tuple[str, ...] = (
    "connector_id",
    "entry_id",
    "source_key",
    "plan_key",
    "run_key",
    "project",
    "run_id",
    "harness",
    "model",
    "outcome",
    "pause_reason",
    "recorded_at",
    "run_started_at",
    "run_finished_at",
    "duration_seconds",
    "turn_count",
    "completed_session_count",
    "pending_session_count",
    "completed_session_ids",
    "review_completed",
    "review_outcome",
    "reconstructed",
    "event_schema_version",
    "received_at",
)

_OPERATION_COLUMNS: tuple[str, ...] = (
    "operation_id",
    "connector_id",
    "command_id",
    "method",
    "status",
    "plan_key",
    "run_key",
    "run_id",
    "question_id",
    "requested_at",
    "deliver_by",
    "dispatched_at",
    "settled_at",
    "detail_code",
    "received_at",
    "updated_at",
)

_CATALOG_COLUMNS: tuple[str, ...] = (
    "connector_id",
    "plan_key",
    "run_key",
    "title",
    "display_label",
    "primary_state",
    "recommended_action",
    "urgency_rank",
    "urgency_group",
    "activity_label",
    "projects",
    "project_group",
    "counts",
    "sources",
    "exists",
    "pinned",
    "next_pending_session_id",
    "last_activity_at",
    "last_activity_source",
    "completed_at",
    "pause_reason",
    "harness",
    "registry_digest",
    "repository_count",
    "warning_count",
    "search_text",
    "generation",
    "observed_at",
    "received_at",
    "present",
    "absent_since",
)

_CATALOG_IDENTITY: tuple[str, ...] = ("connector_id", "plan_key")


def _quoted(columns: Sequence[str]) -> str:
    """Column names, always quoted, because one of them is a keyword."""
    return ", ".join(f'"{column}"' for column in columns)
