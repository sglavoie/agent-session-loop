from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import events, plans_payloads, session_overview

#: Appended by ``turn_execution_executor.TurnExecutor.launch`` as a bare string rather
#: than through an ``events`` constant. It is the only evidence that a turn
#: began, so an interrupted turn is visible as a start no finish answers.
TURN_STARTED_EVENT = "turn_started"

#: How far two statements about the same span may differ before the difference
#: is a contradiction rather than arithmetic. Durations are persisted rounded
#: to milliseconds and timestamps to microseconds, so agreeing records normally
#: agree exactly; a whole second of disagreement is not rounding.
DEFAULT_TOLERANCE_SECONDS = 1.0

PHASE_WORKER_EXECUTION = "worker_execution"
PHASE_REVIEW = "review"
PHASE_TURN_UNCLASSIFIED = "turn_unclassified"
PHASE_VERIFICATION = "verification"
PHASE_COMMITS = "commits"
PHASE_ASSURANCE = "assurance"
PHASE_UNATTRIBUTED = "unattributed"

#: The phases measured from evidence. Their sum is a run's attributed time.
ATTRIBUTED_PHASES: tuple[str, ...] = (
    PHASE_WORKER_EXECUTION,
    PHASE_REVIEW,
    PHASE_TURN_UNCLASSIFIED,
    PHASE_VERIFICATION,
    PHASE_COMMITS,
    PHASE_ASSURANCE,
)
#: Every phase a share is reported for, including the residual.
PHASES: tuple[str, ...] = (*ATTRIBUTED_PHASES, PHASE_UNATTRIBUTED)

CLASSIFICATION_COMPLETE = "complete"
CLASSIFICATION_PARTIAL = "partial"
CLASSIFICATION_INCONSISTENT = "inconsistent"
CLASSIFICATION_ACTIVE = "active"
CLASSIFICATIONS: tuple[str, ...] = (
    CLASSIFICATION_COMPLETE,
    CLASSIFICATION_PARTIAL,
    CLASSIFICATION_INCONSISTENT,
    CLASSIFICATION_ACTIVE,
)

EXCLUDED_ACTIVE = "active"
EXCLUDED_DRY_RUN = "dry-run"
EXCLUDED_OUTSIDE_WINDOW = "outside-window"
EXCLUDED_NO_FINISH_TIME = "no-finish-time"

#: Anomalies. Each is a statement the evidence cannot both make and be right.
ANOMALY_INVALID_NUMBER = "invalid-number"
ANOMALY_CONTRADICTORY_DURATION = "contradictory-duration"
ANOMALY_CONFLICTING_EVIDENCE = "conflicting-evidence"
ANOMALY_OVERLAPPING_PHASES = "overlapping-phases"
ANOMALY_UNMATCHED_FINISH = "unmatched-finish"
ANOMALY_DUPLICATE_EVIDENCE = "duplicate-evidence"
ANOMALY_ATTRIBUTED_EXCEEDS_WALL = "attributed-exceeds-wall"
ANOMALY_CODES: tuple[str, ...] = (
    ANOMALY_INVALID_NUMBER,
    ANOMALY_CONTRADICTORY_DURATION,
    ANOMALY_CONFLICTING_EVIDENCE,
    ANOMALY_OVERLAPPING_PHASES,
    ANOMALY_UNMATCHED_FINISH,
    ANOMALY_DUPLICATE_EVIDENCE,
    ANOMALY_ATTRIBUTED_EXCEEDS_WALL,
)

#: Gaps. Each is something the evidence does not say, which is different from
#: something it says wrongly.
GAP_RUN_START_ABSENT = "run-start-absent"
GAP_WALL_DURATION_UNKNOWN = "wall-duration-unknown"
GAP_RECONSTRUCTED_TERMINAL = "reconstructed-terminal"
GAP_TURN_TIMING_UNKNOWN = "turn-timing-unknown"
GAP_TURN_KIND_UNKNOWN = "turn-kind-unknown"
GAP_UNMATCHED_START = "unmatched-start"
GAP_MISSING_IDENTITY = "missing-identity"
GAP_PHASE_TIMING_UNKNOWN = "phase-timing-unknown"
GAP_CODES: tuple[str, ...] = (
    GAP_RUN_START_ABSENT,
    GAP_WALL_DURATION_UNKNOWN,
    GAP_RECONSTRUCTED_TERMINAL,
    GAP_TURN_TIMING_UNKNOWN,
    GAP_TURN_KIND_UNKNOWN,
    GAP_UNMATCHED_START,
    GAP_MISSING_IDENTITY,
    GAP_PHASE_TIMING_UNKNOWN,
)

#: Timing sources for one attempt, most authoritative first.
SOURCE_EXECUTION_FINISHED = "execution-finished"
SOURCE_TURN_FINISHED = "turn-finished"
SOURCE_RESULT_FILE = "result-file"
SOURCE_TURN_REJECTED = "turn-rejected"
SOURCE_TURN_STARTED = "turn-started"
TIMING_SOURCE_PRECEDENCE: tuple[str, ...] = (
    SOURCE_EXECUTION_FINISHED,
    SOURCE_TURN_FINISHED,
    SOURCE_RESULT_FILE,
)

OUTCOME_ACCEPTED = "accepted"
OUTCOME_REJECTED = "rejected"
OUTCOME_EXECUTED = "executed"
OUTCOME_STARTED = "started"


def _parse_timestamp(value: Any) -> datetime | None:
    """Read a persisted timestamp, accepting the trailing-``Z`` JSON form."""
    return session_overview.parse_iso_timestamp(value)


def _finite_seconds(value: Any) -> float | None:
    """Return ``value`` as a usable duration, or ``None`` when it is not one.

    ``bool`` is rejected because ``True`` is an ``int`` that would silently
    become one second, and non-finite floats are rejected because ``inf`` and
    ``nan`` propagate through every later sum without ever failing a
    comparison.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _round(value: float) -> float:
    return round(value, 3)


@dataclass(frozen=True)
class Observation:
    """One anomaly or gap, with the evidence that produced it."""

    code: str
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": dict(self.detail)}


@dataclass(frozen=True)
class RunEvidence:
    """Everything one plan's state directory supplies about its runs.

    ``plan_key`` is whatever stable identity the caller uses for the plan — the
    state directory name, the plan path, a digest. The collector only requires
    that two different plans never share it.
    """

    plan_key: str
    event_records: Sequence[Mapping[str, Any]] = ()
    turn_results: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class TurnAttempt:
    """The reconciled timing of one ``(plan, run, turn index)`` attempt."""

    plan_key: str
    run_id: str
    turn_index: int | None
    session_id: str | None
    session_kind: str | None
    outcome: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    duration_source: str | None
    sources: tuple[str, ...]

    @property
    def phase(self) -> str:
        if self.session_kind == plans_payloads.SESSION_KIND_IMPLEMENTATION:
            return PHASE_WORKER_EXECUTION
        if self.session_kind == plans_payloads.SESSION_KIND_REVIEW:
            return PHASE_REVIEW
        return PHASE_TURN_UNCLASSIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_key": self.plan_key,
            "run_id": self.run_id,
            "turn_index": self.turn_index,
            "session_id": self.session_id,
            "session_kind": self.session_kind,
            "outcome": self.outcome,
            "phase": self.phase,
            "started_at": (
                self.started_at.isoformat() if self.started_at is not None else None
            ),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at is not None else None
            ),
            "duration_seconds": self.duration_seconds,
            "duration_source": self.duration_source,
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class RunTiming:
    """One run's phase accounting and how far it can be trusted."""

    plan_key: str
    run_id: str
    classification: str
    included: bool
    excluded_reason: str | None
    outcome: str | None
    paused: bool | None
    dry_run: bool | None
    started_at: datetime | None
    finished_at: datetime | None
    wall_seconds: float | None
    phase_seconds: Mapping[str, float]
    attributed_seconds: float
    unattributed_seconds: float | None
    verification_check_seconds: float
    verification_check_count: int
    attempts: tuple[TurnAttempt, ...]
    anomalies: tuple[Observation, ...]
    gaps: tuple[Observation, ...]

    @property
    def trustworthy(self) -> bool:
        """Whether this run may back a phase share rather than only a total."""
        return self.included and self.classification == CLASSIFICATION_COMPLETE

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_key": self.plan_key,
            "run_id": self.run_id,
            "classification": self.classification,
            "included": self.included,
            "excluded_reason": self.excluded_reason,
            "outcome": self.outcome,
            "paused": self.paused,
            "dry_run": self.dry_run,
            "started_at": (
                self.started_at.isoformat() if self.started_at is not None else None
            ),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at is not None else None
            ),
            "wall_seconds": self.wall_seconds,
            "phase_seconds": dict(self.phase_seconds),
            "attributed_seconds": self.attributed_seconds,
            "unattributed_seconds": self.unattributed_seconds,
            "verification_check_seconds": self.verification_check_seconds,
            "verification_check_count": self.verification_check_count,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "anomalies": [item.to_dict() for item in self.anomalies],
            "gaps": [item.to_dict() for item in self.gaps],
        }


@dataclass(frozen=True)
class ExecutionTimingReport:
    """Windowed timing over every run the caller supplied evidence for."""

    window_start: datetime | None
    window_end: datetime | None
    tolerance_seconds: float
    runs: tuple[RunTiming, ...]
    unattributable_records: int
    unattributable_turn_results: int

    @property
    def included_runs(self) -> tuple[RunTiming, ...]:
        return tuple(run for run in self.runs if run.included)

    @property
    def trustworthy_runs(self) -> tuple[RunTiming, ...]:
        return tuple(run for run in self.runs if run.trustworthy)

    def counts_by_classification(self) -> dict[str, int]:
        counts = dict.fromkeys(CLASSIFICATIONS, 0)
        for run in self.runs:
            counts[run.classification] += 1
        return counts

    def included_counts_by_classification(self) -> dict[str, int]:
        counts = dict.fromkeys(CLASSIFICATIONS, 0)
        for run in self.included_runs:
            counts[run.classification] += 1
        return counts

    def exclusions(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for run in self.runs:
            if run.included or run.excluded_reason is None:
                continue
            counts[run.excluded_reason] = counts.get(run.excluded_reason, 0) + 1
        return dict(sorted(counts.items()))

    def known_durations(self) -> dict[str, Any]:
        """Measured seconds per phase over every included run.

        Deliberately not a share: a run that never recorded its verification
        phase still contributes its worker time here, so the denominators
        differ per phase and are reported with the numerators.
        """
        seconds = dict.fromkeys(PHASES, 0.0)
        runs_with_phase = dict.fromkeys(PHASES, 0)
        wall_seconds = 0.0
        runs_with_wall = 0
        for run in self.included_runs:
            for phase in ATTRIBUTED_PHASES:
                measured = run.phase_seconds.get(phase, 0.0)
                if measured:
                    seconds[phase] += measured
                    runs_with_phase[phase] += 1
            if run.wall_seconds is not None:
                wall_seconds += run.wall_seconds
                runs_with_wall += 1
            if run.unattributed_seconds is not None:
                seconds[PHASE_UNATTRIBUTED] += run.unattributed_seconds
                runs_with_phase[PHASE_UNATTRIBUTED] += 1
        return {
            "runs": len(self.included_runs),
            "runs_with_known_wall": runs_with_wall,
            "wall_seconds": _round(wall_seconds),
            "seconds": {phase: _round(seconds[phase]) for phase in PHASES},
            "runs_with_phase": dict(runs_with_phase),
        }

    def phase_shares(self) -> dict[str, Any]:
        """Shares of run time by phase, over trustworthy runs only.

        ``shares`` is ``None`` when no run qualifies. An empty basis reported
        as zeroes would read as "no time went anywhere", which is a claim the
        absence of evidence does not support.
        """
        basis = self.trustworthy_runs
        seconds = dict.fromkeys(PHASES, 0.0)
        total = 0.0
        for run in basis:
            for phase in ATTRIBUTED_PHASES:
                seconds[phase] += run.phase_seconds.get(phase, 0.0)
            if run.unattributed_seconds is not None:
                seconds[PHASE_UNATTRIBUTED] += run.unattributed_seconds
            if run.wall_seconds is not None:
                total += run.wall_seconds
        shares: dict[str, float] | None = None
        if basis and total > 0:
            shares = {phase: _round(seconds[phase] / total) for phase in PHASES}
        return {
            "basis_runs": len(basis),
            "basis_seconds": _round(total),
            "seconds": {phase: _round(seconds[phase]) for phase in PHASES},
            "shares": shares,
        }

    def verification_check_work(self) -> dict[str, Any]:
        """Cumulative per-check work, which is not run time.

        Concurrent checks overlap, so this can exceed the verification phase
        elapsed time it sits beside. Keeping it out of the phase totals is what
        makes both numbers true at once.
        """
        seconds = 0.0
        checks = 0
        runs = 0
        for run in self.included_runs:
            if run.verification_check_count:
                runs += 1
            seconds += run.verification_check_seconds
            checks += run.verification_check_count
        return {
            "runs_with_checks": runs,
            "checks": checks,
            "seconds": _round(seconds),
            "phase_seconds": _round(
                sum(
                    run.phase_seconds.get(PHASE_VERIFICATION, 0.0)
                    for run in self.included_runs
                )
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_start": (
                self.window_start.isoformat() if self.window_start is not None else None
            ),
            "window_end": (
                self.window_end.isoformat() if self.window_end is not None else None
            ),
            "tolerance_seconds": self.tolerance_seconds,
            "runs_supplied": len(self.runs),
            "runs_included": len(self.included_runs),
            "runs_by_classification": self.counts_by_classification(),
            "included_runs_by_classification": (
                self.included_counts_by_classification()
            ),
            "exclusions": self.exclusions(),
            "unattributable_records": self.unattributable_records,
            "unattributable_turn_results": self.unattributable_turn_results,
            "known_durations": self.known_durations(),
            "phase_shares": self.phase_shares(),
            "verification_check_work": self.verification_check_work(),
            "runs": [run.to_dict() for run in self.runs],
        }


# The collector is split across modules; keep the evidence vocabulary explicit
# so both runtime and static ``import *`` consumers receive the same contract.
# fmt: off
__all__ = ("Mapping", "Sequence", "dataclass", "field", "datetime", "timedelta", "Any", "events", "plans_payloads", "session_overview", "TURN_STARTED_EVENT", "DEFAULT_TOLERANCE_SECONDS", "PHASE_WORKER_EXECUTION", "PHASE_REVIEW", "PHASE_TURN_UNCLASSIFIED", "PHASE_VERIFICATION", "PHASE_COMMITS", "PHASE_ASSURANCE", "PHASE_UNATTRIBUTED", "ATTRIBUTED_PHASES", "PHASES", "CLASSIFICATION_COMPLETE", "CLASSIFICATION_PARTIAL", "CLASSIFICATION_INCONSISTENT", "CLASSIFICATION_ACTIVE", "CLASSIFICATIONS", "EXCLUDED_ACTIVE", "EXCLUDED_DRY_RUN", "EXCLUDED_OUTSIDE_WINDOW", "EXCLUDED_NO_FINISH_TIME", "ANOMALY_INVALID_NUMBER", "ANOMALY_CONTRADICTORY_DURATION", "ANOMALY_CONFLICTING_EVIDENCE", "ANOMALY_OVERLAPPING_PHASES", "ANOMALY_UNMATCHED_FINISH", "ANOMALY_DUPLICATE_EVIDENCE", "ANOMALY_ATTRIBUTED_EXCEEDS_WALL", "ANOMALY_CODES", "GAP_RUN_START_ABSENT", "GAP_WALL_DURATION_UNKNOWN", "GAP_RECONSTRUCTED_TERMINAL", "GAP_TURN_TIMING_UNKNOWN", "GAP_TURN_KIND_UNKNOWN", "GAP_UNMATCHED_START", "GAP_MISSING_IDENTITY", "GAP_PHASE_TIMING_UNKNOWN", "GAP_CODES", "SOURCE_EXECUTION_FINISHED", "SOURCE_TURN_FINISHED", "SOURCE_RESULT_FILE", "SOURCE_TURN_REJECTED", "SOURCE_TURN_STARTED", "TIMING_SOURCE_PRECEDENCE", "OUTCOME_ACCEPTED", "OUTCOME_REJECTED", "OUTCOME_EXECUTED", "OUTCOME_STARTED", "_parse_timestamp", "_finite_seconds", "_round", "Observation", "RunEvidence", "TurnAttempt", "RunTiming", "ExecutionTimingReport")
# fmt: on
