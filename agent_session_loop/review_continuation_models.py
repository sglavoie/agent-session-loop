"""Review continuation vocabulary: dispositions, records, and value coercers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

REVIEW_CYCLE_DISPOSITION_ALL_CLEAR = "all-clear"


REVIEW_CYCLE_DISPOSITION_REPAIRS = "repairs-materialized"


REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED = "cycles-disabled"


REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED = "cycle-cap-exhausted"


REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED = "repair-group-exhausted"


REVIEW_CYCLE_DISPOSITION_REPEATED = "findings-repeated"


REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED = "unstructured-findings"


REVIEW_CYCLE_DISPOSITION_MALFORMED = "malformed-review-repair"


REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY = "not-loop-ready"


REVIEW_CYCLE_DISPOSITIONS: tuple[str, ...] = (
    REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
    REVIEW_CYCLE_DISPOSITION_REPAIRS,
    REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
    REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
    REVIEW_CYCLE_DISPOSITION_REPEATED,
    REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
    REVIEW_CYCLE_DISPOSITION_MALFORMED,
    REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
)


TERMINAL_REVIEW_DISPOSITIONS = frozenset(
    {
        REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
        REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
        REVIEW_CYCLE_DISPOSITION_REPEATED,
        REVIEW_CYCLE_DISPOSITION_UNSTRUCTURED,
    }
)


BUDGET_REVIEW_DISPOSITIONS = frozenset(
    {
        REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
        REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
    }
)


MATERIALIZED_REVIEW_DISPOSITIONS = frozenset(
    {
        REVIEW_CYCLE_DISPOSITION_REPAIRS,
        REVIEW_CYCLE_DISPOSITION_NOT_LOOP_READY,
    }
)


ReviewResumeAction = Literal[
    "initial-review",
    "materialize-repairs",
    "run-repairs",
    "rereview",
    "terminal",
]


@dataclass(frozen=True)
class ReviewEventRecord:
    """One relevant, well-formed append-only review event."""

    name: str
    payload: Mapping[str, Any]
    created_at: str | None = None


@dataclass(frozen=True)
class ReviewResume:
    """Deterministic review continuation projection for public JSON surfaces."""

    source_cycle: int | None
    review_session_id: str | None
    source_disposition: str | None
    action: ReviewResumeAction
    eligible: bool
    blocker: str | None
    historical_materializations: int
    minimum_required_cap: int
    selected_cap: int | None
    max_repair_group_attempts: int | None = None
    repair_group_attempts: Mapping[str, int] | None = None
    repair_session_ids: tuple[str, ...] = ()
    pending_repair_session_ids: tuple[str, ...] = ()
    completed_repair_session_ids: tuple[str, ...] = ()
    source_fingerprints: tuple[str, ...] | None = None
    current_fingerprints: tuple[str, ...] | None = None
    current_repeat_keys: tuple[str, ...] | None = None
    source_review_output_sha256: str | None = None
    source_review_artifact_sha256: str | None = None
    current_review_output_sha256: str | None = None
    current_review_artifact_sha256: str | None = None
    artifact_valid: bool | None = None
    artifact_error: str | None = None
    source_findings: tuple[Mapping[str, Any], ...] | None = None
    current_findings: tuple[Mapping[str, Any], ...] | None = None
    source_repositories: tuple[Mapping[str, Any], ...] | None = None
    current_repositories: tuple[Mapping[str, Any], ...] | None = None
    repair_groups: tuple[Mapping[str, Any], ...] | None = None
    repair_scope_gaps: tuple[Mapping[str, Any], ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "source_cycle": self.source_cycle,
            "review_session_id": self.review_session_id,
            "source_disposition": self.source_disposition,
            "action": self.action,
            "eligible": self.eligible,
            "blocker": self.blocker,
            "historical_materializations": self.historical_materializations,
            "minimum_required_cap": self.minimum_required_cap,
            "selected_cap": self.selected_cap,
            "repair_session_ids": list(self.repair_session_ids),
            "pending_repair_session_ids": list(self.pending_repair_session_ids),
            "completed_repair_session_ids": list(self.completed_repair_session_ids),
            "source_fingerprints": (
                list(self.source_fingerprints)
                if self.source_fingerprints is not None
                else None
            ),
            "current_fingerprints": (
                list(self.current_fingerprints)
                if self.current_fingerprints is not None
                else None
            ),
            "current_repeat_keys": (
                list(self.current_repeat_keys)
                if self.current_repeat_keys is not None
                else None
            ),
            "source_review_output_sha256": self.source_review_output_sha256,
            "source_review_artifact_sha256": (self.source_review_artifact_sha256),
            "current_review_output_sha256": self.current_review_output_sha256,
            "current_review_artifact_sha256": (self.current_review_artifact_sha256),
            "artifact_valid": self.artifact_valid,
            "artifact_error": self.artifact_error,
        }
        if self.max_repair_group_attempts is not None:
            payload["max_repair_group_attempts"] = self.max_repair_group_attempts
        if self.repair_group_attempts is not None:
            payload["repair_group_attempts"] = dict(self.repair_group_attempts)
        if self.source_findings is not None:
            payload["source_findings"] = [dict(item) for item in self.source_findings]
        if self.current_findings is not None:
            payload["current_findings"] = [dict(item) for item in self.current_findings]
        if self.source_repositories is not None:
            payload["source_repositories"] = [
                dict(item) for item in self.source_repositories
            ]
        if self.current_repositories is not None:
            payload["current_repositories"] = [
                dict(item) for item in self.current_repositories
            ]
        if self.repair_groups is not None:
            payload["repair_groups"] = [dict(item) for item in self.repair_groups]
        if self.repair_scope_gaps is not None:
            payload["repair_scope_gaps"] = [
                dict(item) for item in self.repair_scope_gaps
            ]
        return payload


def _nonempty_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _nonnegative_int(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    return None


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(item for item in value if isinstance(item, str))


def _mapping_tuple(value: Any) -> tuple[Mapping[str, Any], ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(dict(item) for item in value if isinstance(item, Mapping))
