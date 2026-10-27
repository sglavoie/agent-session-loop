"""The closed field sets (shapes) every envelope and payload is checked against.

Part of :mod:`asl_workbench.transport`, which re-exports everything here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .transport_vocabulary import (
    KIND_DETAIL,
    KIND_OPERATION,
    KIND_RECEIPT,
    KIND_REQUEST,
    REQUEST_CATALOG,
    REQUEST_DETAIL,
    REQUEST_HISTORY,
    REQUEST_MUTATE,
    REQUEST_OBSERVE,
    VIEW_CATALOG_PAGE,
    VIEW_HEARTBEAT,
    VIEW_HISTORY_BATCH,
    VIEW_SNAPSHOT,
)

# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Shape:
    """A closed set of names: required, optional, and nothing else."""

    required: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()

    @property
    def allowed(self) -> frozenset[str]:
        return self.required | self.optional


#: The envelope itself. ``capability`` rides beside ``kind`` so authorization
#: reads one field rather than inferring one from a payload it has not
#: validated yet; ``connector_id`` and ``target_id`` name the two ends;
#: ``request_id`` correlates an answer with the request that asked for it, and
#: ``deadline`` says when a request stops being worth attempting.
ENVELOPE_SHAPE = _Shape(
    required=frozenset(
        {
            "protocol",
            "kind",
            "capability",
            "connector_id",
            "target_id",
            "sent_at",
            "payload",
        }
    ),
    optional=frozenset({"request_id", "deadline"}),
)

#: Where ``request_id`` is required, where it is merely allowed, and where it
#: is forbidden. An observation is unsolicited by nature -- a connector uploads
#: what it saw, not what it was asked for -- so a correlated observation would
#: be claiming a relationship that does not exist.
REQUEST_ID_REQUIRED: frozenset[str] = frozenset(
    {KIND_REQUEST, KIND_DETAIL, KIND_RECEIPT}
)
REQUEST_ID_OPTIONAL: frozenset[str] = frozenset({KIND_OPERATION})

#: Only a request carries a deadline: it is the service saying how long the
#: instruction stays live. An answer is about something that already happened.
DEADLINE_KINDS: frozenset[str] = frozenset({KIND_REQUEST})

#: The plan or run an envelope is about, kept in one nested object so no
#: top-level payload name collides with a storage column.
TARGET_SHAPE = _Shape(
    required=frozenset(),
    optional=frozenset(
        {
            "plan_key",
            "run_key",
            "run_id",
            "question_id",
            "session_id",
            "archive",
            "archive_id",
        }
    ),
)

_SHARED_REQUEST_OPTIONAL = frozenset({"reason"})

#: One shape per request method, so a catalog publish carrying an
#: ``operation_id`` is refused rather than silently ignored.
REQUEST_SHAPES: Mapping[str, _Shape] = {
    REQUEST_OBSERVE: _Shape(
        required=frozenset({"request"}),
        optional=_SHARED_REQUEST_OPTIONAL | frozenset({"target"}),
    ),
    REQUEST_CATALOG: _Shape(
        required=frozenset({"request"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"generation", "offset", "limit"}),
    ),
    REQUEST_HISTORY: _Shape(
        required=frozenset({"request"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"window_start", "window_end", "limit"}),
    ),
    REQUEST_DETAIL: _Shape(
        required=frozenset({"request", "detail", "target"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset({"artifact", "stream", "offset", "limit", "max_bytes"}),
    ),
    # ``reply_ref`` and not ``reply``: reply prose is fetched by reference at
    # the moment of use and never travels in an envelope the service retains.
    REQUEST_MUTATE: _Shape(
        required=frozenset({"request", "operation_id", "method", "target"}),
        optional=_SHARED_REQUEST_OPTIONAL
        | frozenset(
            {
                "action_token",
                "reply_ref",
                "bridge_token",
                "harness",
                "archive_retention_days",
            }
        ),
    ),
}

#: One projected run inside a snapshot. Question *identity* travels; question
#: prose does not, exactly as in the projection this is derived from.
RUN_SHAPE = _Shape(
    required=frozenset(
        {
            "run_key",
            "project",
            "plan_key",
            "run_id",
            "primary_state",
            "phase",
            "urgency_group",
            "actions",
            "last_activity_at",
        }
    ),
    optional=frozenset(
        {
            "action_token",
            "session_id",
            "harness",
            "question_id",
            "question_asked_at",
            "unanswered_count",
            "run_started_at",
            "run_finished_at",
            "pause_reason",
            "outcome",
            "warnings",
        }
    ),
)

#: One completed run inside a history batch. Every name is one
#: :data:`asl_workbench.observation.HISTORY_PERSIST_FIELDS` keeps, minus the
#: ingestion bookkeeping the service assigns itself. ``project`` is required:
#: the store keeps it ``NOT NULL`` and every ending names one, so a frame
#: without it is refused here rather than failing inside the transaction.
HISTORY_ENTRY_SHAPE = _Shape(
    required=frozenset(
        {
            "entry_id",
            "source_key",
            "plan_key",
            "run_key",
            "run_id",
            "project",
            "outcome",
            "recorded_at",
        }
    ),
    optional=frozenset(
        {
            "projects",
            "harness",
            "model",
            "pause_reason",
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
        }
    ),
)

#: One catalog entry. The required half is what identifies and ranks a plan;
#: everything else is absent when the Mac could not determine it, which is a
#: different statement from "the value is zero".
CATALOG_ENTRY_SHAPE = _Shape(
    required=frozenset(
        {
            "plan_key",
            "title",
            "display_label",
            "primary_state",
            "recommended_action",
            "urgency_rank",
            "urgency_group",
            "activity_label",
            "counts",
            "sources",
            "exists",
        }
    ),
    optional=frozenset(
        {
            "run_key",
            "projects",
            "project_group",
            "pinned",
            "next_pending_session_id",
            "last_activity_at",
            "last_activity_source",
            "completed_at",
            "pause_reason",
            "harness",
            "registry_digest",
            "repository_count",
            "multi_repository",
            "warning_count",
            "search_text",
        }
    ),
)

#: One shape per observation view. The payload bodies are the ones
#: :mod:`asl_workbench.store_workbench` already reads, so a validated envelope goes
#: straight to ``ingest_*`` with no field renamed in between.
OBSERVATION_SHAPES: Mapping[str, _Shape] = {
    VIEW_HEARTBEAT: _Shape(
        required=frozenset({"view", "observed_at", "bridge_alive"}),
        optional=frozenset(
            {
                "bridge_protocol",
                "contract_set_id",
                "asl_version",
                "degraded_reason",
                "source_generation",
                "projection_age_seconds",
            }
        ),
    ),
    VIEW_SNAPSHOT: _Shape(
        required=frozenset(
            {
                "view",
                "observed_at",
                "digest",
                "row_count",
                "truncated",
                "runs",
            }
        ),
        optional=frozenset(
            {
                "contract_set_id",
                "source_digest",
                "source_generation",
                "failed_projections",
                "projection_age_seconds",
                "staleness_bound_seconds",
                "warnings",
            }
        ),
    ),
    VIEW_HISTORY_BATCH: _Shape(
        required=frozenset(
            {"view", "batch_id", "generated_at", "entry_count", "complete", "entries"}
        ),
        optional=frozenset({"window_start", "window_end", "more", "warnings"}),
    ),
    VIEW_CATALOG_PAGE: _Shape(
        required=frozenset(
            {
                "view",
                "observed_at",
                "generation",
                "complete",
                "final",
                "offset",
                "entry_count",
                "entries",
            }
        ),
        optional=frozenset({"source_generation", "total", "warnings"}),
    ),
}

#: The detail payload. ``complete`` is the completeness this transport carries
#: for a read: a window that was cut is ``partial``, and a reader that renders
#: it as the whole artifact is the failure the field exists to prevent.
DETAIL_SHAPE = _Shape(
    required=frozenset({"detail", "target", "observed_at", "complete"}),
    optional=frozenset(
        {
            "artifact",
            "stream",
            "content",
            "content_bytes",
            "encoding",
            "truncated",
            "offset",
            "next_offset",
            "total",
            "entries",
            "entry_count",
            "reason",
            "warnings",
        }
    ),
)

#: The fields of a detail answer that may hold prose, named explicitly. This is
#: the transport's single exemption from :func:`assert_no_body`, and it is
#: narrow on purpose: a detail envelope is answered online and dropped, so
#: nothing here reaches a database. The test module pins these names against
#: every persist allowlist in :mod:`asl_workbench.observation` -- a field that
#: may carry prose must not share a name with a column.
DETAIL_BODY_FIELDS: frozenset[str] = frozenset({"content", "entries"})

OPERATION_SHAPE = _Shape(
    required=frozenset({"operation_id", "status", "observed_at"}),
    optional=frozenset(
        {
            "method",
            "target",
            "detail_code",
            "dispatched_at",
            "settled_at",
            "deliver_by",
        }
    ),
)

RECEIPT_SHAPE = _Shape(
    required=frozenset({"outcome", "observed_at"}),
    optional=frozenset({"operation_id", "detail_code", "complete"}),
)

#: What each end announces before anything else is exchanged.
#: ``protocols`` lists every transport version the peer can speak, so the two
#: ends can settle on a shared one instead of comparing a single number.
HELLO_SHAPE = _Shape(
    required=frozenset({"protocol", "connector_id", "target_id", "capabilities"}),
    optional=frozenset(
        {"protocols", "contract_set_id", "asl_version", "observation_only"}
    ),
)

PAYLOAD_SHAPES: Mapping[str, _Shape] = {
    KIND_DETAIL: DETAIL_SHAPE,
    KIND_OPERATION: OPERATION_SHAPE,
    KIND_RECEIPT: RECEIPT_SHAPE,
}


__all__ = [
    "CATALOG_ENTRY_SHAPE",
    "DEADLINE_KINDS",
    "DETAIL_BODY_FIELDS",
    "DETAIL_SHAPE",
    "ENVELOPE_SHAPE",
    "HELLO_SHAPE",
    "HISTORY_ENTRY_SHAPE",
    "OBSERVATION_SHAPES",
    "OPERATION_SHAPE",
    "PAYLOAD_SHAPES",
    "RECEIPT_SHAPE",
    "REQUEST_ID_OPTIONAL",
    "REQUEST_ID_REQUIRED",
    "REQUEST_SHAPES",
    "RUN_SHAPE",
    "TARGET_SHAPE",
]
