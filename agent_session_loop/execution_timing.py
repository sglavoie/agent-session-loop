"""Per-run timing accounting over supplied event and turn-result evidence.

This module answers one question — *where did a run's wall-clock time go* —
and answers it only as far as the evidence allows. It is a pure collector: it
reads no files, opens no state directory, prints nothing, and holds no clock.
Callers discover the corpus and hand it in; ``asl`` command surfaces render
what comes back.

Accounting rules
----------------

**Identity.** An attempt is ``(plan identity, run id, turn index)``. Event
payloads carry the run id and the turn index but never the plan, because a
plan is identified by the state directory that contains ``events.jsonl``, so
:class:`RunEvidence` makes the caller state it. Two different plans may hold
the same run id without either being counted twice.

**Reconciliation.** ``turn_execution_finished``, ``turn_finished``,
``turn_rejected`` and the turn's ``result.json`` payload are four statements
about the *same* attempt, not four attempts. Timing is taken from
``turn_execution_finished`` when present, then from ``turn_finished``, then
from the result file: the execution event is written by the code that watched
the process exit, while the other two are copies made later. A lower-precedence
source that disagrees with the chosen one beyond ``tolerance_seconds`` does not
overwrite it and is not discarded either — it is recorded as a
``conflicting-evidence`` anomaly, which is what stops a contradiction from
being silently resolved in favour of whichever record was read first.

**Phases.** A run's attributed time is the sum of

* ``worker_execution`` — implementation turns, whole harness process:
  model activity, tool execution and in-turn waits are all time the worker
  spent and are not separable from the outside;
* ``review`` — review turns, same basis;
* ``turn_unclassified`` — turns whose ``session_kind`` no supplied record
  states. Historical runs predate that field; their duration is real and is
  kept, but it is not silently filed under implementation;
* ``verification`` — the elapsed span of each verification phase, from
  ``verification_started`` to ``verification_finished``;
* ``commits`` — each ``commit_finished`` span;
* ``assurance`` — each finalization phase span (today the terminal supervisor
  audit).

``unattributed`` is the remainder of the run's wall duration, which is the
runner's own orchestration between phases. It is a residual, never a
measurement, and it is reported unclamped: a *negative* remainder means the
phases claim more time than the run lasted, and rounding that up to zero would
turn a contradiction into a plausible total.

A non-turn phase whose start and finish both exist but whose length nothing
states — no usable ``duration_seconds`` and not enough timestamps to span one —
contributes no seconds and leaves a ``phase-timing-unknown`` gap. Two event
names are evidence that the phase *ran*, not that it took zero time; without the
gap its seconds would fall into ``unattributed`` while the run still counted as
``complete``, which would put a fabricated zero-length phase into the share
basis. The run's other, known durations stay attributed exactly as they were.

**Verification concurrency.** A parallel recipe runs several checks at once, so
the per-check ``duration_seconds`` values sum to more than the phase lasted.
Run accounting therefore uses the phase elapsed time only. The cumulative
per-check figure is still useful — it is how much check *work* the run did —
and is reported separately under ``verification_check_work``, never added to a
phase share.

An individual check is identified by ``(run id, turn index, check index)``,
which is how the metrics scope collector keys it too, so cumulative check work
and per-scope check totals count the same executions. A repeated
``verification_check_finished`` is a second statement about one execution, not
a second execution: its duration is reconciled against the first rather than
added to it, and the repeat is recorded as ``duplicate-evidence`` when the two
agree within ``tolerance_seconds`` and ``conflicting-evidence`` when they do
not. A record that omits either identity field cannot be told apart from
another such record, so it reconciles into the same slot the metrics collector
would use and leaves a ``missing-identity`` gap rather than inflating the
count.

**Eligibility.** A run is in the window when it has a terminal record and that
record's finish time falls in ``[window_start, window_end)``. Success and pause
outcomes are both included: a paused run spent its time as surely as a
successful one. Dry runs are excluded, on the evidence of
``run_started.dry_run``; a run whose start record was not supplied cannot be
shown not to be a dry run, so it is kept and marked instead of assumed.

**Whole invocations.** An included run contributes its *full* duration, even
the part that elapsed before ``window_start``: half a run is not a measurement
of anything. Conversely the report never spans runs — the idle gap between two
invocations belongs to nobody and is not accounted anywhere.

**Trust.** Every run lands in exactly one classification:

``active``
    No terminal record was supplied. The run may still be running; nothing is
    concluded about it and it is never in the window.
``inconsistent``
    At least one anomaly: an invalid number, a duration contradicting its own
    timestamps, two sources contradicting each other, phases that overlap in
    wall-clock time, a finish with no start, or phases claiming more time than
    the run lasted.
``partial``
    No anomaly, but a gap: a missing start record, an unknown wall duration, a
    terminal record reconstructed after the fact, a turn with no timing, a
    turn of unknown kind, a finished phase of unknown length, or a start with
    no finish.
``complete``
    Every phase reconciles and nothing is missing.

Known durations are aggregated over *every* included run, so a partial or
inconsistent run's measured seconds are not thrown away. Phase *shares* are
aggregated over ``complete`` runs alone, because a share is a claim about a
whole run and a run missing a phase would understate that phase for everyone.
The two numbers are reported side by side and are deliberately not
interchangeable.
"""

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


@dataclass
class _AttemptEvidence:
    """Every statement supplied about one attempt, before reconciliation."""

    plan_key: str
    run_id: str
    turn_index: int | None
    #: source -> (started_at, finished_at, duration_seconds), in arrival order.
    timing: list[tuple[str, datetime | None, datetime | None, float | None]] = field(
        default_factory=list
    )
    session_ids: list[tuple[str, str]] = field(default_factory=list)
    session_kinds: list[tuple[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    started: bool = False
    accepted: bool = False
    rejected: bool = False
    executed: bool = False


@dataclass
class _PhaseEvidence:
    """One non-turn phase occurrence: verification, a commit, a finalization."""

    phase: str
    label: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    start_seen: bool = False
    finish_seen: bool = False
    finish_count: int = 0


@dataclass
class _CheckEvidence:
    """One verification check execution, however many records restate it."""

    duration_seconds: float | None = None
    finish_count: int = 0


@dataclass
class _RunEvidence:
    """Accumulated evidence for one ``(plan, run)`` pair."""

    plan_key: str
    run_id: str
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS
    run_started: Mapping[str, Any] | None = None
    run_finished: Mapping[str, Any] | None = None
    run_finished_created_at: datetime | None = None
    run_finished_count: int = 0
    attempts: dict[Any, _AttemptEvidence] = field(default_factory=dict)
    phases: dict[Any, _PhaseEvidence] = field(default_factory=dict)
    checks: dict[Any, _CheckEvidence] = field(default_factory=dict)
    anomalies: list[Observation] = field(default_factory=list)
    gaps: list[Observation] = field(default_factory=list)


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _hashable(value: Any) -> Any:
    """A dict key for a persisted value of unproven type.

    Mirrors ``metrics._hashable`` deliberately: the two collectors must agree
    on when two check records name the same check, or the same report would
    total a different number of checks in each of its two places.
    """
    return value if isinstance(value, (str, int, type(None))) else repr(value)


def _attempt_key(run: _RunEvidence, turn_index: Any, fallback: str) -> Any:
    """Key an attempt by its turn index, or uniquely when it has none.

    A record with no turn index cannot be reconciled against anything, so it
    gets a key nothing else can collide with rather than being merged into a
    shared ``None`` bucket where two unrelated attempts would cancel out.
    """
    if isinstance(turn_index, int) and not isinstance(turn_index, bool):
        return turn_index
    run.gaps.append(
        Observation(GAP_MISSING_IDENTITY, {"record": fallback, "field": "turn_index"})
    )
    return (fallback, len(run.attempts))


def _record_timing(
    run: _RunEvidence,
    attempt: _AttemptEvidence,
    source: str,
    payload: Mapping[str, Any],
) -> None:
    started_at = _parse_timestamp(payload.get("started_at"))
    finished_at = _parse_timestamp(payload.get("finished_at"))
    raw_duration = payload.get("duration_seconds")
    duration = _finite_seconds(raw_duration)
    if raw_duration is not None and duration is None:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": source,
                    "field": "duration_seconds",
                    "value": repr(raw_duration),
                    "turn_index": attempt.turn_index,
                },
            )
        )
    elif duration is not None and duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": source,
                    "field": "duration_seconds",
                    "value": duration,
                    "reason": "negative",
                    "turn_index": attempt.turn_index,
                },
            )
        )
        duration = None
    attempt.timing.append((source, started_at, finished_at, duration))
    session_id = _string_or_none(payload.get("session_id")) or _string_or_none(
        payload.get("session")
    )
    if session_id is not None:
        attempt.session_ids.append((source, session_id))
    session_kind = _string_or_none(payload.get("session_kind"))
    if session_kind is not None:
        attempt.session_kinds.append((source, session_kind))
    if source not in attempt.sources:
        attempt.sources.append(source)


def _phase_key(phase: str, discriminator: Any, ordinal: int) -> Any:
    if discriminator is None:
        return (phase, "unkeyed", ordinal)
    return (phase, discriminator)


def _phase_slot(
    run: _RunEvidence, phase: str, discriminator: Any, label: str
) -> _PhaseEvidence:
    key = _phase_key(phase, discriminator, len(run.phases))
    slot = run.phases.get(key)
    if slot is None:
        slot = _PhaseEvidence(phase=phase, label=label)
        run.phases[key] = slot
    return slot


def _record_phase_duration(
    run: _RunEvidence, slot: _PhaseEvidence, payload: Mapping[str, Any]
) -> None:
    raw_duration = payload.get("duration_seconds")
    duration = _finite_seconds(raw_duration)
    if raw_duration is not None and duration is None:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": slot.label,
                    "field": "duration_seconds",
                    "value": repr(raw_duration),
                },
            )
        )
    elif duration is not None and duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": slot.label,
                    "field": "duration_seconds",
                    "value": duration,
                    "reason": "negative",
                },
            )
        )
        duration = None
    finished_at = _parse_timestamp(payload.get("finished_at"))
    started_at = _parse_timestamp(payload.get("started_at"))
    if slot.finish_seen:
        slot.finish_count += 1
        previous = slot.duration_seconds
        if previous is not None and duration is not None:
            run.anomalies.append(
                Observation(
                    ANOMALY_DUPLICATE_EVIDENCE
                    if abs(previous - duration) <= run.tolerance_seconds
                    else ANOMALY_CONFLICTING_EVIDENCE,
                    {
                        "source": slot.label,
                        "field": "duration_seconds",
                        "values": [previous, duration],
                    },
                )
            )
        return
    slot.finish_seen = True
    slot.finish_count = 1
    slot.duration_seconds = duration
    if finished_at is not None:
        slot.finished_at = finished_at
    if started_at is not None and slot.started_at is None:
        slot.started_at = started_at


def _record_check(run: _RunEvidence, payload: Mapping[str, Any]) -> None:
    """Reconcile one check finish against the execution it reports on.

    ``(turn_index, check_index)`` is the check's durable identity within a run.
    A second finish for that identity is another copy of one execution, so its
    duration is compared against the first rather than summed with it: summing
    would report two checks and twice the work for a single replayed record,
    and would disagree with the per-scope totals the metrics collector derives
    from the same events.
    """
    raw_duration = payload.get("duration_seconds")
    duration = _finite_seconds(raw_duration)
    if duration is None or duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": events.VERIFICATION_CHECK_FINISHED_EVENT,
                    "field": "duration_seconds",
                    "value": repr(raw_duration),
                },
            )
        )
        return
    turn_index = payload.get("turn_index")
    check_index = payload.get("check_index")
    if turn_index is None or check_index is None:
        # Unidentified records cannot be told apart from each other. They share
        # a slot — the same one the metrics collector shares — so the two never
        # disagree, and the gap says the count is a floor rather than a fact.
        run.gaps.append(
            Observation(
                GAP_MISSING_IDENTITY,
                {
                    "record": events.VERIFICATION_CHECK_FINISHED_EVENT,
                    "field": "turn_index" if turn_index is None else "check_index",
                },
            )
        )
    slot = run.checks.get((_hashable(turn_index), _hashable(check_index)))
    if slot is None:
        run.checks[(_hashable(turn_index), _hashable(check_index))] = _CheckEvidence(
            duration_seconds=duration, finish_count=1
        )
        return
    slot.finish_count += 1
    previous = slot.duration_seconds
    agrees = previous is not None and abs(previous - duration) <= run.tolerance_seconds
    run.anomalies.append(
        Observation(
            ANOMALY_DUPLICATE_EVIDENCE if agrees else ANOMALY_CONFLICTING_EVIDENCE,
            {
                "source": events.VERIFICATION_CHECK_FINISHED_EVENT,
                "field": "duration_seconds",
                "turn_index": turn_index,
                "check_index": check_index,
                "values": [previous, duration],
                "count": slot.finish_count,
            },
        )
    )


def _ingest_event(
    run: _RunEvidence,
    name: str,
    payload: Mapping[str, Any],
    created_at: datetime | None,
) -> None:
    if name == events.RUN_STARTED_EVENT:
        run.run_started = payload
        return
    if name == events.RUN_FINISHED_EVENT:
        run.run_finished_count += 1
        if run.run_finished is None:
            run.run_finished = payload
            run.run_finished_created_at = created_at
        return
    if name == TURN_STARTED_EVENT:
        attempt = _attempt_for(run, payload, TURN_STARTED_EVENT)
        attempt.started = True
        if SOURCE_TURN_STARTED not in attempt.sources:
            attempt.sources.append(SOURCE_TURN_STARTED)
        session_id = _string_or_none(payload.get("session_id")) or _string_or_none(
            payload.get("session")
        )
        if session_id is not None:
            attempt.session_ids.append((SOURCE_TURN_STARTED, session_id))
        return
    if name == events.TURN_EXECUTION_FINISHED_EVENT:
        attempt = _attempt_for(run, payload, SOURCE_EXECUTION_FINISHED)
        if attempt.executed:
            run.anomalies.append(
                Observation(
                    ANOMALY_DUPLICATE_EVIDENCE,
                    {
                        "source": SOURCE_EXECUTION_FINISHED,
                        "turn_index": attempt.turn_index,
                    },
                )
            )
        attempt.executed = True
        _record_timing(run, attempt, SOURCE_EXECUTION_FINISHED, payload)
        return
    if name == events.TURN_FINISHED_EVENT:
        attempt = _attempt_for(run, payload, SOURCE_TURN_FINISHED)
        attempt.accepted = True
        _record_timing(run, attempt, SOURCE_TURN_FINISHED, payload)
        return
    if name == events.TURN_REJECTED_EVENT:
        attempt = _attempt_for(run, payload, SOURCE_TURN_REJECTED)
        attempt.rejected = True
        if SOURCE_TURN_REJECTED not in attempt.sources:
            attempt.sources.append(SOURCE_TURN_REJECTED)
        session_id = _string_or_none(payload.get("session_id")) or _string_or_none(
            payload.get("session")
        )
        if session_id is not None:
            attempt.session_ids.append((SOURCE_TURN_REJECTED, session_id))
        return
    if name == events.VERIFICATION_STARTED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_VERIFICATION,
            payload.get("turn_index"),
            events.VERIFICATION_STARTED_EVENT,
        )
        slot.start_seen = True
        slot.started_at = _parse_timestamp(payload.get("started_at"))
        return
    if name == events.VERIFICATION_FINISHED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_VERIFICATION,
            payload.get("turn_index"),
            events.VERIFICATION_FINISHED_EVENT,
        )
        _record_phase_duration(run, slot, payload)
        return
    if name == events.VERIFICATION_CHECK_FINISHED_EVENT:
        _record_check(run, payload)
        return
    if name == events.COMMIT_STARTED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_COMMITS,
            _commit_discriminator(payload),
            events.COMMIT_STARTED_EVENT,
        )
        slot.start_seen = True
        slot.started_at = _parse_timestamp(payload.get("started_at"))
        return
    if name == events.COMMIT_FINISHED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_COMMITS,
            _commit_discriminator(payload),
            events.COMMIT_FINISHED_EVENT,
        )
        _record_phase_duration(run, slot, payload)
        return
    if name == events.FINALIZATION_STARTED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_ASSURANCE,
            payload.get("phase"),
            events.FINALIZATION_STARTED_EVENT,
        )
        slot.start_seen = True
        slot.started_at = _parse_timestamp(payload.get("started_at"))
        return
    if name == events.FINALIZATION_FINISHED_EVENT:
        slot = _phase_slot(
            run,
            PHASE_ASSURANCE,
            payload.get("phase"),
            events.FINALIZATION_FINISHED_EVENT,
        )
        _record_phase_duration(run, slot, payload)
        return


def _commit_discriminator(payload: Mapping[str, Any]) -> Any:
    index = payload.get("commit_index")
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    return (index, _string_or_none(payload.get("repository_key")))


def _attempt_for(
    run: _RunEvidence, payload: Mapping[str, Any], source: str
) -> _AttemptEvidence:
    key = _attempt_key(run, payload.get("turn_index"), source)
    attempt = run.attempts.get(key)
    if attempt is None:
        turn_index = payload.get("turn_index")
        attempt = _AttemptEvidence(
            plan_key=run.plan_key,
            run_id=run.run_id,
            turn_index=(
                turn_index
                if isinstance(turn_index, int) and not isinstance(turn_index, bool)
                else None
            ),
        )
        run.attempts[key] = attempt
    return attempt


def _reconcile_attempt(run: _RunEvidence, attempt: _AttemptEvidence) -> TurnAttempt:
    tolerance = run.tolerance_seconds
    chosen: tuple[str, datetime | None, datetime | None, float | None] | None = None
    for source in TIMING_SOURCE_PRECEDENCE:
        for record in attempt.timing:
            if record[0] == source and record[3] is not None:
                chosen = record
                break
        if chosen is not None:
            break
    if chosen is None:
        for source in TIMING_SOURCE_PRECEDENCE:
            for record in attempt.timing:
                if record[0] != source:
                    continue
                if record[1] is not None and record[2] is not None:
                    chosen = record
                    break
            if chosen is not None:
                break

    started_at = chosen[1] if chosen is not None else None
    finished_at = chosen[2] if chosen is not None else None
    duration = chosen[3] if chosen is not None else None
    duration_source = chosen[0] if chosen is not None else None
    if duration is None and started_at is not None and finished_at is not None:
        duration = (finished_at - started_at).total_seconds()
    if (
        chosen is not None
        and chosen[3] is not None
        and started_at is not None
        and finished_at is not None
    ):
        spanned = (finished_at - started_at).total_seconds()
        if abs(spanned - chosen[3]) > tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_CONTRADICTORY_DURATION,
                    {
                        "source": chosen[0],
                        "turn_index": attempt.turn_index,
                        "duration_seconds": chosen[3],
                        "timestamp_span_seconds": _round(spanned),
                    },
                )
            )
    if duration is not None and duration < 0:
        run.anomalies.append(
            Observation(
                ANOMALY_INVALID_NUMBER,
                {
                    "source": duration_source,
                    "field": "duration_seconds",
                    "value": _round(duration),
                    "reason": "negative-span",
                    "turn_index": attempt.turn_index,
                },
            )
        )

    for record in attempt.timing:
        if chosen is None or record is chosen:
            continue
        if record[3] is None or chosen[3] is None:
            continue
        if abs(record[3] - chosen[3]) > tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_CONFLICTING_EVIDENCE,
                    {
                        "field": "duration_seconds",
                        "turn_index": attempt.turn_index,
                        "preferred": {"source": chosen[0], "value": chosen[3]},
                        "conflicting": {"source": record[0], "value": record[3]},
                    },
                )
            )

    session_id = attempt.session_ids[0][1] if attempt.session_ids else None
    distinct_ids = {value for _, value in attempt.session_ids}
    if len(distinct_ids) > 1:
        run.anomalies.append(
            Observation(
                ANOMALY_CONFLICTING_EVIDENCE,
                {
                    "field": "session_id",
                    "turn_index": attempt.turn_index,
                    "values": sorted(distinct_ids),
                },
            )
        )
    session_kind: str | None = None
    for source in (SOURCE_TURN_FINISHED, SOURCE_RESULT_FILE, SOURCE_EXECUTION_FINISHED):
        for record_source, value in attempt.session_kinds:
            if record_source == source:
                session_kind = value
                break
        if session_kind is not None:
            break
    distinct_kinds = {value for _, value in attempt.session_kinds}
    if len(distinct_kinds) > 1:
        run.anomalies.append(
            Observation(
                ANOMALY_CONFLICTING_EVIDENCE,
                {
                    "field": "session_kind",
                    "turn_index": attempt.turn_index,
                    "values": sorted(distinct_kinds),
                },
            )
        )
    if session_kind is None:
        run.gaps.append(
            Observation(GAP_TURN_KIND_UNKNOWN, {"turn_index": attempt.turn_index})
        )

    if attempt.accepted:
        outcome = OUTCOME_ACCEPTED
    elif attempt.rejected:
        outcome = OUTCOME_REJECTED
    elif attempt.executed:
        outcome = OUTCOME_EXECUTED
    else:
        outcome = OUTCOME_STARTED

    if duration is None:
        if outcome == OUTCOME_STARTED:
            run.gaps.append(
                Observation(GAP_UNMATCHED_START, {"turn_index": attempt.turn_index})
            )
        else:
            run.gaps.append(
                Observation(
                    GAP_TURN_TIMING_UNKNOWN,
                    {"turn_index": attempt.turn_index, "outcome": outcome},
                )
            )
    elif outcome == OUTCOME_STARTED:
        run.gaps.append(
            Observation(GAP_UNMATCHED_START, {"turn_index": attempt.turn_index})
        )

    return TurnAttempt(
        plan_key=attempt.plan_key,
        run_id=attempt.run_id,
        turn_index=attempt.turn_index,
        session_id=session_id,
        session_kind=session_kind,
        outcome=outcome,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=None if duration is None else _round(duration),
        duration_source=duration_source,
        sources=tuple(attempt.sources),
    )


def _attempt_order(attempt: _AttemptEvidence) -> tuple[int, int]:
    """Order attempts by turn index, with unidentified ones last."""
    if attempt.turn_index is None:
        return (1, 0)
    return (0, attempt.turn_index)


def _phase_interval(
    slot: _PhaseEvidence,
) -> tuple[datetime, datetime] | None:
    if slot.finished_at is None:
        return None
    if slot.started_at is not None:
        return (slot.started_at, slot.finished_at)
    if slot.duration_seconds is None:
        return None
    inferred_start = slot.finished_at - timedelta(seconds=slot.duration_seconds)
    return (inferred_start, slot.finished_at)


def _detect_overlaps(
    run: _RunEvidence, intervals: Sequence[tuple[str, datetime, datetime]]
) -> None:
    """Flag phases that claim the same wall-clock seconds.

    A run executes one turn at a time, verifies after the harness exits, and
    commits after that, so two run-level phases sharing a second means at least
    one of their records is wrong — and their durations would then be counted
    twice against the same wall span.
    """
    tolerance = run.tolerance_seconds
    ordered = sorted(intervals, key=lambda item: (item[1], item[2]))
    # Sweep against the furthest end seen so far rather than the previous
    # interval, so a long phase that swallows several short ones is caught
    # against every one of them and not just its immediate neighbour.
    covering: tuple[str, datetime, datetime] | None = None
    for current in ordered:
        if covering is not None:
            overlap = (covering[2] - current[1]).total_seconds()
            if overlap > tolerance:
                run.anomalies.append(
                    Observation(
                        ANOMALY_OVERLAPPING_PHASES,
                        {
                            "phases": [covering[0], current[0]],
                            "overlap_seconds": _round(overlap),
                        },
                    )
                )
        if covering is None or current[2] > covering[2]:
            covering = current


def _run_timing(
    run: _RunEvidence,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
) -> RunTiming:
    tolerance = run.tolerance_seconds
    attempts = tuple(
        _reconcile_attempt(run, attempt)
        for attempt in sorted(run.attempts.values(), key=_attempt_order)
    )

    phase_seconds = dict.fromkeys(ATTRIBUTED_PHASES, 0.0)
    intervals: list[tuple[str, datetime, datetime]] = []
    for attempt in attempts:
        if attempt.duration_seconds is not None:
            phase_seconds[attempt.phase] += attempt.duration_seconds
        if attempt.started_at is not None and attempt.finished_at is not None:
            intervals.append(
                (f"turn-{attempt.turn_index}", attempt.started_at, attempt.finished_at)
            )

    for key, slot in run.phases.items():
        interval = _phase_interval(slot)
        if slot.finish_seen and slot.duration_seconds is not None:
            phase_seconds[slot.phase] += slot.duration_seconds
            if interval is not None:
                spanned_phase = (interval[1] - interval[0]).total_seconds()
                if abs(spanned_phase - slot.duration_seconds) > tolerance:
                    run.anomalies.append(
                        Observation(
                            ANOMALY_CONTRADICTORY_DURATION,
                            {
                                "phase": slot.phase,
                                "source": slot.label,
                                "duration_seconds": slot.duration_seconds,
                                "timestamp_span_seconds": _round(spanned_phase),
                            },
                        )
                    )
        elif slot.finish_seen and interval is not None:
            phase_seconds[slot.phase] += (interval[1] - interval[0]).total_seconds()
        elif slot.finish_seen:
            # The phase ran — both of its records exist — but nothing states how
            # long it took: no usable duration and not enough timestamps to span
            # one. Contributing nothing would file those seconds under
            # ``unattributed`` and still call the run complete, which asserts a
            # zero-length phase the evidence never claimed. The seconds this run
            # *does* know stay attributed; the run itself leaves the share basis.
            run.gaps.append(
                Observation(
                    GAP_PHASE_TIMING_UNKNOWN, {"phase": slot.phase, "key": str(key)}
                )
            )
        if slot.start_seen and not slot.finish_seen:
            run.gaps.append(
                Observation(GAP_UNMATCHED_START, {"phase": slot.phase, "key": str(key)})
            )
        if slot.finish_seen and not slot.start_seen:
            run.anomalies.append(
                Observation(
                    ANOMALY_UNMATCHED_FINISH, {"phase": slot.phase, "key": str(key)}
                )
            )
        if interval is not None:
            intervals.append((f"{slot.phase}:{key}", interval[0], interval[1]))

    _detect_overlaps(run, intervals)

    finished = run.run_finished
    started_at: datetime | None = None
    finished_at: datetime | None = None
    wall_seconds: float | None = None
    outcome: str | None = None
    paused: bool | None = None
    if finished is not None:
        started_at = _parse_timestamp(finished.get("run_started_at"))
        finished_at = (
            _parse_timestamp(finished.get("run_finished_at"))
            or run.run_finished_created_at
        )
        outcome = _string_or_none(finished.get("outcome")) or _string_or_none(
            finished.get("reason")
        )
        raw_paused = finished.get("paused")
        paused = raw_paused if isinstance(raw_paused, bool) else None
        raw_wall = finished.get("duration_seconds")
        wall_seconds = _finite_seconds(raw_wall)
        if raw_wall is not None and wall_seconds is None:
            run.anomalies.append(
                Observation(
                    ANOMALY_INVALID_NUMBER,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "field": "duration_seconds",
                        "value": repr(raw_wall),
                    },
                )
            )
        if wall_seconds is not None and wall_seconds < 0:
            run.anomalies.append(
                Observation(
                    ANOMALY_INVALID_NUMBER,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "field": "duration_seconds",
                        "value": wall_seconds,
                        "reason": "negative",
                    },
                )
            )
            wall_seconds = None
        spanned: float | None = None
        if started_at is not None and finished_at is not None:
            spanned = (finished_at - started_at).total_seconds()
        if wall_seconds is None:
            wall_seconds = spanned
        elif spanned is not None and abs(spanned - wall_seconds) > tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_CONTRADICTORY_DURATION,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "duration_seconds": wall_seconds,
                        "timestamp_span_seconds": _round(spanned),
                    },
                )
            )
        if run.run_finished_count > 1:
            run.anomalies.append(
                Observation(
                    ANOMALY_DUPLICATE_EVIDENCE,
                    {
                        "source": events.RUN_FINISHED_EVENT,
                        "count": run.run_finished_count,
                    },
                )
            )
        if (
            finished.get("synthetic") is True
            or finished.get("outcome") == events.RUN_OUTCOME_ABANDONED
        ):
            # The terminal record was reconstructed from proof of death, so its
            # finish time is when the corpse was found rather than when the run
            # stopped working. The phases it did record are still real.
            run.gaps.append(
                Observation(
                    GAP_RECONSTRUCTED_TERMINAL,
                    {"outcome": _string_or_none(finished.get("outcome"))},
                )
            )
        if wall_seconds is None:
            run.gaps.append(Observation(GAP_WALL_DURATION_UNKNOWN, {}))

    dry_run: bool | None = None
    if run.run_started is None:
        run.gaps.append(Observation(GAP_RUN_START_ABSENT, {}))
    else:
        raw_dry = run.run_started.get("dry_run")
        dry_run = raw_dry if isinstance(raw_dry, bool) else None
        if started_at is None:
            started_at = _parse_timestamp(run.run_started.get("run_started_at"))

    # One slot per check execution, whatever the number of records that stated
    # it, so a replayed event never counts as extra work.
    check_seconds = sum(
        (
            slot.duration_seconds
            for slot in run.checks.values()
            if slot.duration_seconds is not None
        ),
        0.0,
    )

    attributed = sum(phase_seconds.values())
    unattributed: float | None = None
    if wall_seconds is not None:
        remainder = wall_seconds - attributed
        if remainder < -tolerance:
            run.anomalies.append(
                Observation(
                    ANOMALY_ATTRIBUTED_EXCEEDS_WALL,
                    {
                        "attributed_seconds": _round(attributed),
                        "wall_seconds": _round(wall_seconds),
                        "excess_seconds": _round(-remainder),
                    },
                )
            )
            unattributed = _round(remainder)
        else:
            unattributed = _round(max(0.0, remainder))

    if finished is None:
        classification = CLASSIFICATION_ACTIVE
    elif run.anomalies:
        classification = CLASSIFICATION_INCONSISTENT
    elif run.gaps:
        classification = CLASSIFICATION_PARTIAL
    else:
        classification = CLASSIFICATION_COMPLETE

    included = True
    excluded_reason: str | None = None
    if finished is None:
        included = False
        excluded_reason = EXCLUDED_ACTIVE
    elif dry_run is True:
        included = False
        excluded_reason = EXCLUDED_DRY_RUN
    elif finished_at is None:
        included = False
        excluded_reason = EXCLUDED_NO_FINISH_TIME
    elif (window_start is not None and finished_at < window_start) or (
        window_end is not None and finished_at >= window_end
    ):
        included = False
        excluded_reason = EXCLUDED_OUTSIDE_WINDOW

    return RunTiming(
        plan_key=run.plan_key,
        run_id=run.run_id,
        classification=classification,
        included=included,
        excluded_reason=excluded_reason,
        outcome=outcome,
        paused=paused,
        dry_run=dry_run,
        started_at=started_at,
        finished_at=finished_at,
        wall_seconds=None if wall_seconds is None else _round(wall_seconds),
        phase_seconds={
            phase: _round(seconds) for phase, seconds in phase_seconds.items()
        },
        attributed_seconds=_round(attributed),
        unattributed_seconds=unattributed,
        verification_check_seconds=_round(check_seconds),
        verification_check_count=len(run.checks),
        attempts=attempts,
        anomalies=tuple(run.anomalies),
        gaps=tuple(run.gaps),
    )


def collect_execution_timing(
    evidence: Sequence[RunEvidence],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
) -> ExecutionTimingReport:
    """Reconcile supplied evidence into per-run phase accounting.

    ``window_start`` is inclusive and ``window_end`` exclusive, both compared
    against a run's *finish* time, and either may be ``None`` for an unbounded
    edge. A run straddling the opening edge is included whole, because its
    duration is a property of the run rather than of the window.

    Records the collector cannot attribute to a run — no ``run_id``, or a
    payload that is not a mapping — are counted rather than dropped silently,
    so a caller can see that evidence went unread.
    """
    tolerance = float(tolerance_seconds)
    accumulated: dict[tuple[str, str], _RunEvidence] = {}
    unattributable_records = 0
    unattributable_turn_results = 0

    def slot(plan_key: str, run_id: str) -> _RunEvidence:
        key = (plan_key, run_id)
        found = accumulated.get(key)
        if found is None:
            found = _RunEvidence(
                plan_key=plan_key, run_id=run_id, tolerance_seconds=tolerance
            )
            accumulated[key] = found
        return found

    for supplied in evidence:
        for record in supplied.event_records:
            if not isinstance(record, Mapping):
                unattributable_records += 1
                continue
            name = _string_or_none(record.get("name"))
            payload = record.get("payload")
            if name is None or not isinstance(payload, Mapping):
                unattributable_records += 1
                continue
            run_id = _string_or_none(payload.get("run_id"))
            if run_id is None:
                unattributable_records += 1
                continue
            _ingest_event(
                slot(supplied.plan_key, run_id),
                name,
                payload,
                _parse_timestamp(record.get("created_at")),
            )
        for payload in supplied.turn_results:
            if not isinstance(payload, Mapping):
                unattributable_turn_results += 1
                continue
            run_id = _string_or_none(payload.get("run_id"))
            if run_id is None:
                unattributable_turn_results += 1
                continue
            run = slot(supplied.plan_key, run_id)
            attempt = _attempt_for(run, payload, SOURCE_RESULT_FILE)
            _record_timing(run, attempt, SOURCE_RESULT_FILE, payload)

    runs = tuple(
        _run_timing(accumulated[key], window_start=window_start, window_end=window_end)
        for key in sorted(accumulated)
    )
    return ExecutionTimingReport(
        window_start=window_start,
        window_end=window_end,
        tolerance_seconds=tolerance,
        runs=runs,
        unattributable_records=unattributable_records,
        unattributable_turn_results=unattributable_turn_results,
    )
