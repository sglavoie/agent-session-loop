"""Per-run assurance summary models.

Audit roles, audit attempts, and ``AssuranceSummary``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .assurance_models_findings import EvidenceRef, parse_evidence_ref
from .assurance_models_primitives import (
    AssuranceError,
    _duplicates,
    _optional_text,
    _require_bool,
    _require_identifier,
    _require_int,
    _require_issue_ids,
    _require_locator,
    _require_number,
    _require_object,
    _require_schema_version,
    _require_sequence,
    _require_text,
    _require_timestamp,
    _require_token,
    parse_issue_id,
)
from .assurance_models_vocab import (
    ASSURANCE_SCHEMA_VERSION,
    ATTEMPT_OUTCOMES,
    AUDIT_ROLE_SOURCES,
    AUDIT_STATES,
    MAX_AUDIT_ATTEMPT_RECORDS,
    MAX_EVIDENCE_ENTRIES,
    MAX_ISSUE_REFERENCES,
    MAX_TOKEN_CHARS,
    AttemptOutcome,
    AuditRoleSource,
    AuditState,
)
from .harnesses import HARNESS_NAMES, HarnessName
from .literals import as_literal

# ---------------------------------------------------------------------------
# Per-run summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRole:
    """The independent role the audit ran as.

    The audit harness must differ from the implementation harness — an agent
    grading its own turn is not a second opinion — but may equal the review
    harness, which was already independent of the implementation.
    """

    harness: HarnessName
    implementation_harness: HarnessName
    source: AuditRoleSource
    timeout_seconds: float
    model: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        as_literal(self.harness, HARNESS_NAMES, "role 'harness'", error=AssuranceError)
        as_literal(
            self.implementation_harness,
            HARNESS_NAMES,
            "role 'implementation_harness'",
            error=AssuranceError,
        )
        if self.harness == self.implementation_harness:
            raise AssuranceError(
                f"audit harness {self.harness!r} must differ from the "
                "implementation harness; an audit role that grades its own work "
                "is not independent"
            )
        as_literal(
            self.source, AUDIT_ROLE_SOURCES, "role 'source'", error=AssuranceError
        )
        object.__setattr__(
            self,
            "timeout_seconds",
            _require_number(
                self.timeout_seconds, label="role 'timeout_seconds'", minimum=1.0
            ),
        )
        if self.model is not None:
            _require_text(self.model, label="role 'model'", max_chars=MAX_TOKEN_CHARS)
        if self.effort is not None:
            _require_text(self.effort, label="role 'effort'", max_chars=MAX_TOKEN_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "implementation_harness": self.implementation_harness,
            "source": self.source,
            "timeout_seconds": self.timeout_seconds,
            "model": self.model,
            "effort": self.effort,
        }


def parse_audit_role(payload: object, *, label: str = "role") -> AuditRole:
    """Parse the effective audit role, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "harness",
            "implementation_harness",
            "source",
            "timeout_seconds",
            "model",
            "effort",
        ),
    )
    return AuditRole(
        harness=as_literal(
            data.get("harness"),
            HARNESS_NAMES,
            f"{label} 'harness'",
            error=AssuranceError,
        ),
        implementation_harness=as_literal(
            data.get("implementation_harness"),
            HARNESS_NAMES,
            f"{label} 'implementation_harness'",
            error=AssuranceError,
        ),
        source=as_literal(
            data.get("source"),
            AUDIT_ROLE_SOURCES,
            f"{label} 'source'",
            error=AssuranceError,
        ),
        timeout_seconds=_require_number(
            data.get("timeout_seconds"), label=f"{label} 'timeout_seconds'", minimum=1.0
        ),
        model=_optional_text(
            data.get("model"), label=f"{label} 'model'", max_chars=MAX_TOKEN_CHARS
        ),
        effort=_optional_text(
            data.get("effort"), label=f"{label} 'effort'", max_chars=MAX_TOKEN_CHARS
        ),
    )


@dataclass(frozen=True)
class AuditAttempt:
    """One bounded agent invocation and how it ended."""

    number: int
    outcome: AttemptOutcome
    duration_seconds: float | None = None
    artifact_dir: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_int(
            self.number,
            label="attempt 'number'",
            minimum=1,
            maximum=MAX_AUDIT_ATTEMPT_RECORDS,
        )
        as_literal(
            self.outcome, ATTEMPT_OUTCOMES, "attempt 'outcome'", error=AssuranceError
        )
        if self.duration_seconds is not None:
            object.__setattr__(
                self,
                "duration_seconds",
                _require_number(
                    self.duration_seconds,
                    label="attempt 'duration_seconds'",
                    minimum=0.0,
                ),
            )
        if self.artifact_dir is not None:
            _require_locator(self.artifact_dir, label="attempt 'artifact_dir'")
        if self.detail is not None:
            _require_text(self.detail, label="attempt 'detail'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "artifact_dir": self.artifact_dir,
            "detail": self.detail,
        }


def parse_audit_attempt(payload: object, *, label: str = "attempt") -> AuditAttempt:
    """Parse one audit attempt record, failing closed."""
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "number",
            "outcome",
            "duration_seconds",
            "artifact_dir",
            "detail",
        ),
    )
    return AuditAttempt(
        number=_require_int(
            data.get("number"),
            label=f"{label} 'number'",
            minimum=1,
            maximum=MAX_AUDIT_ATTEMPT_RECORDS,
        ),
        outcome=as_literal(
            data.get("outcome"),
            ATTEMPT_OUTCOMES,
            f"{label} 'outcome'",
            error=AssuranceError,
        ),
        duration_seconds=(
            None
            if data.get("duration_seconds") is None
            else _require_number(
                data.get("duration_seconds"),
                label=f"{label} 'duration_seconds'",
                minimum=0.0,
            )
        ),
        artifact_dir=(
            None
            if data.get("artifact_dir") is None
            else _require_locator(
                data.get("artifact_dir"), label=f"{label} 'artifact_dir'"
            )
        ),
        detail=_optional_text(data.get("detail"), label=f"{label} 'detail'"),
    )


@dataclass(frozen=True)
class AssuranceSummary:
    """The bounded assurance object attached to a run's state, events, and status.

    It is additive and advisory: nothing here changes an exit code, a pause
    reason, readiness, a commit, or an implementation artifact.
    """

    run_id: str
    state: AuditState
    eligible: bool
    reason: str | None = None
    role: AuditRole | None = None
    attempts: tuple[AuditAttempt, ...] = ()
    artifacts: tuple[EvidenceRef, ...] = ()
    issue_ids: tuple[str, ...] = ()
    new_issue_count: int = 0
    recurring_issue_count: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: float | None = None
    schema_version: int = ASSURANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(
            self, "issue_ids", tuple(parse_issue_id(item) for item in self.issue_ids)
        )
        _require_schema_version(self.schema_version, label="assurance 'schema_version'")
        _require_identifier(self.run_id, label="assurance 'run_id'")
        as_literal(self.state, AUDIT_STATES, "assurance 'state'", error=AssuranceError)
        _require_bool(self.eligible, label="assurance 'eligible'")
        _require_int(
            self.new_issue_count,
            label="assurance 'new_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        )
        _require_int(
            self.recurring_issue_count,
            label="assurance 'recurring_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        )
        if len(self.attempts) > MAX_AUDIT_ATTEMPT_RECORDS:
            raise AssuranceError(
                f"assurance 'attempts' must hold at most "
                f"{MAX_AUDIT_ATTEMPT_RECORDS} entries; got {len(self.attempts)}"
            )
        numbers = [attempt.number for attempt in self.attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise AssuranceError(
                "assurance 'attempts' must be numbered consecutively from 1; "
                f"got {numbers}"
            )
        duplicates = _duplicates(self.issue_ids)
        if duplicates:
            raise AssuranceError(
                f"assurance 'issue_ids' repeats {', '.join(duplicates)}"
            )
        if len(self.issue_ids) > MAX_ISSUE_REFERENCES:
            raise AssuranceError(
                f"assurance 'issue_ids' must hold at most {MAX_ISSUE_REFERENCES} "
                f"entries; got {len(self.issue_ids)}"
            )
        if self.new_issue_count + self.recurring_issue_count != len(self.issue_ids):
            raise AssuranceError(
                f"assurance new/recurring counts "
                f"({self.new_issue_count}/{self.recurring_issue_count}) must "
                f"account for exactly the {len(self.issue_ids)} issue id(s) listed"
            )
        for timestamp, field in (
            (self.started_at, "started_at"),
            (self.completed_at, "completed_at"),
        ):
            if timestamp is not None:
                _require_timestamp(timestamp, label=f"assurance '{field}'")
        if self.duration_seconds is not None:
            object.__setattr__(
                self,
                "duration_seconds",
                _require_number(
                    self.duration_seconds,
                    label="assurance 'duration_seconds'",
                    minimum=0.0,
                ),
            )
        if self.role is not None and not isinstance(self.role, AuditRole):
            raise AssuranceError("assurance 'role' must be an AuditRole")
        self._validate_state()

    def _validate_state(self) -> None:
        if self.state in {"skipped", "unavailable", "partial"}:
            if self.reason is None:
                raise AssuranceError(f"assurance state {self.state!r} must record why")
            _require_token(self.reason, label="assurance 'reason'")
        elif self.reason is not None:
            raise AssuranceError(
                f"assurance state {self.state!r} must not record a reason"
            )
        if not self.eligible and self.state != "skipped":
            raise AssuranceError(
                f"an ineligible run can only be 'skipped'; got {self.state!r}"
            )
        if self.state in {"all-clear", "partial"} and self.issue_ids:
            raise AssuranceError(
                f"a run in state {self.state!r} creates no issues; "
                f"got {', '.join(self.issue_ids)}"
            )
        if self.state == "skipped" and self.issue_ids:
            raise AssuranceError(
                f"skipping an audit creates no issue; got {', '.join(self.issue_ids)}"
            )
        if self.state == "pending" and (self.attempts or self.issue_ids):
            raise AssuranceError(
                "a pending audit has not run and carries no attempts or issues"
            )
        if self.state == "findings" and not self.issue_ids:
            raise AssuranceError(
                "an audit in state 'findings' must name the issue id(s) it opened "
                "or reopened"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "state": self.state,
            "eligible": self.eligible,
            "reason": self.reason,
            "role": None if self.role is None else self.role.to_dict(),
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "issue_ids": list(self.issue_ids),
            "new_issue_count": self.new_issue_count,
            "recurring_issue_count": self.recurring_issue_count,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
        }


def parse_assurance_summary(payload: object) -> AssuranceSummary:
    """Parse a persisted per-run assurance object, failing closed."""
    label = "assurance"
    data = _require_object(
        payload,
        label=label,
        allowed_keys=(
            "schema_version",
            "run_id",
            "state",
            "eligible",
            "reason",
            "role",
            "attempts",
            "artifacts",
            "issue_ids",
            "new_issue_count",
            "recurring_issue_count",
            "started_at",
            "completed_at",
            "duration_seconds",
        ),
    )
    attempts = tuple(
        parse_audit_attempt(item, label=f"{label} attempt #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("attempts", []),
                label=f"{label} 'attempts'",
                maximum=MAX_AUDIT_ATTEMPT_RECORDS,
            )
        )
    )
    artifacts = tuple(
        parse_evidence_ref(item, label=f"{label} artifact #{index + 1}")
        for index, item in enumerate(
            _require_sequence(
                data.get("artifacts", []),
                label=f"{label} 'artifacts'",
                maximum=MAX_EVIDENCE_ENTRIES,
            )
        )
    )
    return AssuranceSummary(
        schema_version=_require_schema_version(
            data.get("schema_version"), label=f"{label} 'schema_version'"
        ),
        run_id=_require_identifier(data.get("run_id"), label=f"{label} 'run_id'"),
        state=as_literal(
            data.get("state"), AUDIT_STATES, f"{label} 'state'", error=AssuranceError
        ),
        eligible=_require_bool(data.get("eligible"), label=f"{label} 'eligible'"),
        reason=(
            None
            if data.get("reason") is None
            else _require_token(data.get("reason"), label=f"{label} 'reason'")
        ),
        role=(
            None
            if data.get("role") is None
            else parse_audit_role(data.get("role"), label=f"{label} 'role'")
        ),
        attempts=attempts,
        artifacts=artifacts,
        issue_ids=_require_issue_ids(
            data.get("issue_ids", []), label=f"{label} 'issue_ids'"
        ),
        new_issue_count=_require_int(
            data.get("new_issue_count", 0),
            label=f"{label} 'new_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        ),
        recurring_issue_count=_require_int(
            data.get("recurring_issue_count", 0),
            label=f"{label} 'recurring_issue_count'",
            minimum=0,
            maximum=MAX_ISSUE_REFERENCES,
        ),
        started_at=(
            None
            if data.get("started_at") is None
            else _require_timestamp(
                data.get("started_at"), label=f"{label} 'started_at'"
            )
        ),
        completed_at=(
            None
            if data.get("completed_at") is None
            else _require_timestamp(
                data.get("completed_at"), label=f"{label} 'completed_at'"
            )
        ),
        duration_seconds=(
            None
            if data.get("duration_seconds") is None
            else _require_number(
                data.get("duration_seconds"),
                label=f"{label} 'duration_seconds'",
                minimum=0.0,
            )
        ),
    )
