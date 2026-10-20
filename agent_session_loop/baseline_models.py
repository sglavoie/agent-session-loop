"""Baseline outcome models, status mapping, and exit-reason vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .verification_models import CheckResult

#: Baseline outcome statuses recorded in the cache. ``pass``/``fail`` mirror the
#: verification status; ``timeout`` and ``error`` capture command executions
#: that never produced a clean pass/fail signal.
BaselineStatus = str

#: A baseline check whose measured duration exceeds this fraction of its budget
#: is flagged as "near timeout" so an unrealistically slow recipe surfaces
#: before it starts timing out.
NEAR_TIMEOUT_FRACTION = 0.8

BASELINE_EXIT_REASON_OK = "ok"
BASELINE_EXIT_REASON_UNSATISFIED = "baseline-unsatisfied"
BASELINE_EXIT_REASON_NO_CHECKS = "no-baseline-checks"
BASELINE_EXIT_REASON_CWD_ERROR = "cwd-error"
BASELINE_EXIT_REASON_PLAN_STRUCTURE = "plan-structure"
BASELINE_EXIT_REASON_MALFORMED_CONTRACT = "malformed-contract"


def _baseline_status(result: CheckResult) -> BaselineStatus:
    if result.status == "pass":
        return "pass"
    reason = result.failure_reason
    if reason == "command-timeout":
        return "timeout"
    if reason in ("command-not-found", "command-error", "file-error"):
        return "error"
    return "fail"


def _satisfied(expectation: str, status: BaselineStatus) -> bool:
    """Whether ``status`` satisfies ``expectation``.

    ``timeout`` and ``error`` never satisfy an expectation — a timed-out or
    unrunnable command is not evidence that the check passed *or* failed.
    """
    if expectation == "pass":
        return status == "pass"
    if expectation == "fail":
        return status == "fail"
    return False


@dataclass(frozen=True)
class BaselineCheckOutcome:
    """The measured outcome of running one baseline check."""

    session_id: str
    key: str
    name: str
    kind: str
    expectation: str
    status: BaselineStatus
    satisfied: bool
    duration_seconds: float
    budget_seconds: float
    near_timeout: bool
    total_budget_exceeded: bool
    detail: str
    repository_fingerprint: str
    recorded_at: str
    repo: str | None = None
    resolved_root: str | None = None
    qualified_cwd: str | None = None
    qualified_path: str | None = None
    registry_digest: str | None = None
    aggregate_repository_fingerprint: str | None = None
    repository_evidence_available: bool | None = None

    def to_result(self) -> dict[str, Any]:
        """Return the cache ``result`` payload for this outcome."""
        payload = {
            "check_name": self.name,
            "kind": self.kind,
            "expectation": self.expectation,
            "status": self.status,
            "satisfied": self.satisfied,
            "duration_seconds": self.duration_seconds,
            "budget_seconds": self.budget_seconds,
            "near_timeout": self.near_timeout,
            "total_budget_exceeded": self.total_budget_exceeded,
            "detail": self.detail,
            "repository_fingerprint": self.repository_fingerprint,
            "recorded_at": self.recorded_at,
        }
        if self.registry_digest is not None:
            payload.update(
                {
                    "repo": self.repo,
                    "resolved_root": self.resolved_root,
                    "qualified_cwd": self.qualified_cwd,
                    "qualified_path": self.qualified_path,
                    "registry_digest": self.registry_digest,
                    "aggregate_repository_fingerprint": (
                        self.aggregate_repository_fingerprint
                    ),
                    "repository_evidence_available": (
                        self.repository_evidence_available
                    ),
                }
            )
        return payload


@dataclass(frozen=True)
class BaselineRunResult:
    """Aggregated result of executing every collected baseline check."""

    plan_dir: Path
    cwd: Path
    outcomes: tuple[BaselineCheckOutcome, ...]
    total_seconds: float
    total_budget_seconds: float
    total_exceeded: bool
    results_path: Path
    registry_digest: str | None = None
    aggregate_repository_fingerprint: str | None = None

    @property
    def all_satisfied(self) -> bool:
        return all(outcome.satisfied for outcome in self.outcomes)

    @property
    def unsatisfied(self) -> tuple[BaselineCheckOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.satisfied)
