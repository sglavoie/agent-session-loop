"""Operational metrics over the runner's persisted state corpus.

``asl metrics`` scans every plan state directory under the state root and
reports workflow reliability and storage numbers for a recent window
(default: the last seven days):

- first-pass session completion (implementation turns),
- runtime consumed by unsuccessful turns,
- pause classifications (including never-retry harness failures),
- verification repair convergence, including how often a recoverable pause
  settled the work its turn retained instead of leaving it for a human,
- review repair-cycle closure and repetition,
- artifact growth and projected compaction savings,
- CLI invocation lifecycle diagnostics from the root-level trace,
- execution timing: where the runs that finished inside the window spent their
  wall clock, plus the coverage that says how much of the corpus that answers
  for.

Windowed activity is derived from ``events.jsonl`` and per-turn
``result.json`` files. A state directory whose files were all last written
before the window opened is measured for size and then skipped, so a narrow
window never reads the whole corpus. Review-materialization totals are
windowed the same way: readable current plan snapshots still reconcile
generated repair children with the full review-event history so one source
review is never counted twice, but a materialization is only counted when the
event carrying it falls inside the window. No state or plan content is
mutated. Compaction savings are projected with a dry-run over the state
directories that had activity inside the window (the full corpus is available
via ``asl compact-state`` per plan).

The execution-timing section is windowed differently on purpose, because it
answers a different question. Its unit is a whole run, so a run belongs to the
window by its *finish* time and then contributes every second it lasted, even
the ones before the window opened; half a run measures nothing. The evidence
behind it — a plan's full event log and turn results — is read unfiltered, so a
retry whose first attempt predates the window is still recognized as a repeat,
and a check that started before the window still counts against the run that
finished inside it. See :mod:`agent_session_loop.execution_timing` for the
accounting rules the numbers obey.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import (
    assurance_report,
    cli_trace,
    compact_state,
    events,
    execution_timing,
    plans_errors,
    plans_models,
    plans_snapshot,
    review_continuation,
    review_repair,
    session_overview,
    state,
)

DEFAULT_SINCE_DAYS = 7

_IMPLEMENTATION_KIND = "implementation"
_COMPLETE_STATUS = "complete"
_VERIFY_FAILED_REASON = "verify-failed"

# A failing invocation is reported with the same closed, parser-derived field
# set the trace itself carries, so the diagnostic surface cannot widen into
# content even if a record ever grows a new key.
RECENT_FAILURE_FIELDS = (
    "invocation_id",
    "timestamp",
    "command",
    "options",
    "help_depth",
    "harness",
    "origin",
    "outcome",
    "exit_code",
    "duration_ms",
    "exception_type",
)
RECENT_FAILURE_LIMIT = 10
FAILING_OUTCOMES = (
    cli_trace.OUTCOME_FAILED,
    cli_trace.OUTCOME_USAGE,
    cli_trace.OUTCOME_INTERRUPTED,
    cli_trace.OUTCOME_EXCEPTION,
)
ROOT_COMMAND_LABEL = "(root)"
UNKNOWN_LABEL = "unknown"

#: The phase vocabulary the timing section renders. It is the collector's, with
#: ``worker_execution`` split by lineage into original implementation work and
#: generated review-repair work — a distinction timing evidence alone cannot
#: make, which is why the collector deliberately does not make it.
TIMING_PHASE_IMPLEMENTATION = "implementation"
TIMING_PHASE_GENERATED_REPAIR = "generated_repair"
TIMING_PHASES: tuple[str, ...] = (
    TIMING_PHASE_IMPLEMENTATION,
    TIMING_PHASE_GENERATED_REPAIR,
    execution_timing.PHASE_TURN_UNCLASSIFIED,
    execution_timing.PHASE_REVIEW,
    execution_timing.PHASE_VERIFICATION,
    execution_timing.PHASE_COMMITS,
    execution_timing.PHASE_ASSURANCE,
    execution_timing.PHASE_UNATTRIBUTED,
)
#: Phases taken from the collector's run-level accounting unchanged, because
#: they are measured from phase records rather than from turn attempts.
_RUN_LEVEL_TIMING_PHASES: tuple[str, ...] = (
    execution_timing.PHASE_VERIFICATION,
    execution_timing.PHASE_COMMITS,
    execution_timing.PHASE_ASSURANCE,
)
#: The text report ranks; JSON carries every plan and every check scope.
TIMING_PLAN_LIMIT = 10
TIMING_CHECK_LIMIT = 10
UNKNOWN_COMMAND_LABEL = "(unknown command)"


@dataclass
class _SessionTurns:
    """Implementation turns for one (state dir, session id) pair."""

    turns: list[tuple[str, bool]] = field(default_factory=list)  # (started_at, ok)

    def sorted_turns(self) -> list[tuple[str, bool]]:
        return sorted(self.turns)


@dataclass
class _CheckRun:
    """One verification check execution, before it is scoped and ranked.

    ``verification_check_started`` carries the command and working directory
    while ``verification_check_finished`` carries the duration, so a check's
    identity and its cost arrive in two records that have to be paired.
    """

    run_id: str
    name: str | None = None
    kind: str | None = None
    repository: str | None = None
    working_directory: str | None = None
    argv: tuple[str, ...] | None = None
    status: str | None = None
    duration_seconds: float | None = None
    finish_seen: bool = False


@dataclass(frozen=True)
class _CheckScope:
    """Where a check ran and what it ran — never its name alone.

    Two plans checked out against the same repository run the *same* check; two
    unrelated projects whose contracts both name a check ``tests`` do not. When
    neither a repository key nor a working directory was recorded, the plan key
    keeps those two apart instead of merging them on a shared name.
    """

    repository: str | None
    working_directory: str | None
    plan_key: str | None
    command: str

    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.repository or "",
            self.working_directory or "",
            self.plan_key or "",
            self.command,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "working_directory": self.working_directory,
            "plan_key": self.plan_key,
            "command": self.command,
        }


@dataclass
class _PlanTimingEvidence:
    """Per-plan lineage and check evidence the timing collector cannot carry.

    The collector is a pure function of timing records. Which sessions are
    generated repair children, and which command a check ran, are facts about
    the plan rather than about a duration, so they are gathered here from the
    same event corpus the rest of this module already read.
    """

    plan_key: str
    plan_dir: str | None = None
    repair_session_ids: set[str] = field(default_factory=set)
    checks: dict[tuple[str, Any, Any], _CheckRun] = field(default_factory=dict)
    #: (run_id, scope, fallback_reason, refreshed)
    review_scopes: list[tuple[str, str, str | None, bool]] = field(default_factory=list)


@dataclass(frozen=True)
class MetricsReport:
    """Aggregated metrics for one state root and window."""

    state_root: str
    since_days: int
    window_start: str
    generated_at: str
    state_dirs_scanned: int
    state_dirs_active: int
    turns: dict[str, Any]
    first_pass: dict[str, Any]
    pauses: dict[str, Any]
    verification_repair: dict[str, Any]
    review_cycles: dict[str, Any]
    agent_mux_delivery: dict[str, Any]
    artifacts: dict[str, Any]
    compaction: dict[str, Any]
    assurance: dict[str, Any] = field(default_factory=dict)
    cli_invocations: dict[str, Any] = field(default_factory=dict)
    execution_timing: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "metrics",
            "state_root": self.state_root,
            "since_days": self.since_days,
            "window_start": self.window_start,
            "generated_at": self.generated_at,
            "state_dirs_scanned": self.state_dirs_scanned,
            "state_dirs_active": self.state_dirs_active,
            "turns": self.turns,
            "first_pass": self.first_pass,
            "pauses": self.pauses,
            "verification_repair": self.verification_repair,
            "review_cycles": self.review_cycles,
            "agent_mux_delivery": self.agent_mux_delivery,
            "artifacts": self.artifacts,
            "compaction": self.compaction,
            "assurance": self.assurance,
            "cli_invocations": self.cli_invocations,
            "execution_timing": self.execution_timing,
        }


def _parse_timestamp(value: Any) -> datetime | None:
    return session_overview.parse_iso_timestamp(value)


def _read_event_records(events_path: Path) -> list[dict[str, Any]]:
    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _read_result_payload(result_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _iter_turn_results(state_dir: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for run_dir in session_overview._iter_run_dirs(state_dir):
        for result_path in session_overview._iter_turn_result_paths(run_dir):
            payload = _read_result_payload(result_path)
            if payload is not None:
                payloads.append(payload)
    return payloads


def _plan_dir_for_state_dir(state_dir: Path) -> str | None:
    plan_dir, _run_id = session_overview._read_state_dir_plan_dir_fallbacks(state_dir)
    return plan_dir


def _directory_sizes(state_dir: Path, cutoff: datetime) -> tuple[int, int, int]:
    """Return (total_bytes, grown_bytes, grown_files) for one state dir.

    Walked with :func:`os.scandir` rather than :meth:`Path.rglob` so each entry
    is classified and sized from the directory entry the kernel already
    returned, instead of paying a fresh ``stat`` for ``is_file()`` and a second
    one for ``st_size``.  Directory symlinks are not descended into (matching
    ``rglob``), while file symlinks are still followed and counted.
    """
    total = 0
    grown = 0
    grown_files = 0
    cutoff_ts = cutoff.timestamp()
    pending = [str(state_dir)]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except OSError:
            # Unreadable or vanished subtree; rglob skips these silently too.
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
                    continue
                if not entry.is_file():
                    continue
                stat_result = entry.stat()
            except OSError:
                continue
            total += stat_result.st_size
            if stat_result.st_mtime >= cutoff_ts:
                grown += stat_result.st_size
                grown_files += 1
    return total, grown, grown_files


@dataclass
class _TracedInvocation:
    """The start and end record a single invocation UUID produced."""

    start: dict[str, Any] | None = None
    end: dict[str, Any] | None = None

    def facts(self) -> dict[str, Any]:
        # End records repeat the start record's parser-derived fields, so
        # either half alone still describes the command that ran.
        return self.start if self.start is not None else (self.end or {})


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _command_label(record: dict[str, Any]) -> str:
    path = record.get("command_path")
    names = (
        [name for name in path if isinstance(name, str)]
        if isinstance(path, list)
        else []
    )
    return " ".join(names) if names else ROOT_COMMAND_LABEL


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _percentile(ordered: list[float], fraction: float) -> float:
    # Nearest-rank: with a handful of invocations an interpolated percentile
    # would report a duration no invocation actually took.
    rank = math.ceil(fraction * len(ordered))
    return ordered[min(len(ordered) - 1, max(0, rank - 1))]


def _recent_failure_entry(
    invocation_id: str, invocation: _TracedInvocation
) -> dict[str, Any]:
    facts = invocation.facts()
    end = invocation.end or {}
    entry = {
        "invocation_id": invocation_id,
        "timestamp": _string_or_none(end.get("timestamp"))
        or _string_or_none(facts.get("timestamp")),
        "command": _command_label(facts),
        "options": [
            name for name in facts.get("options") or [] if isinstance(name, str)
        ],
        "help_depth": _string_or_none(facts.get("help_depth")),
        "harness": _string_or_none(facts.get("harness")),
        "origin": _string_or_none(facts.get("origin")),
        "outcome": _string_or_none(end.get("outcome")),
        "exit_code": end.get("exit_code")
        if isinstance(end.get("exit_code"), int)
        else None,
        "duration_ms": end.get("duration_ms")
        if isinstance(end.get("duration_ms"), (int, float))
        else None,
        "exception_type": _string_or_none(end.get("exception_type")),
    }
    return {name: entry[name] for name in RECENT_FAILURE_FIELDS}


def collect_cli_invocations(
    trace_root: Path,
    *,
    cutoff: datetime,
    exclude_invocation_id: str | None = None,
) -> dict[str, Any]:
    """Pair CLI trace records by invocation UUID and summarize the window.

    Records are tolerated rather than trusted: a malformed or non-object line
    and a record without a usable invocation id, event, or timestamp are all
    counted as read and ignored instead of failing the report, a duplicate
    record for an id keeps the first one without being counted as ignored, and
    an invocation whose start was pruned is still summarized from its end.
    An invocation belongs to the window by its start timestamp, so a pair that
    straddles midnight or a day boundary is counted once, under its start.
    """
    corpus = cli_trace.read_corpus(trace_root)
    records = corpus.records
    invocations: dict[str, _TracedInvocation] = {}
    ignored = corpus.malformed_lines
    for record in records:
        invocation_id = _string_or_none(record.get("invocation_id"))
        event = record.get("event")
        if invocation_id is None or event not in (
            cli_trace.EVENT_START,
            cli_trace.EVENT_END,
        ):
            ignored += 1
            continue
        if invocation_id == exclude_invocation_id:
            continue
        # A duplicate of either half is dropped rather than counted twice, so a
        # replayed or double-appended file cannot inflate the totals.
        tracked = invocations.setdefault(invocation_id, _TracedInvocation())
        if event == cli_trace.EVENT_START:
            if tracked.start is None:
                tracked.start = record
        elif tracked.end is None:
            tracked.end = record

    windowed: dict[str, _TracedInvocation] = {}
    for invocation_id, tracked in invocations.items():
        anchor = tracked.start if tracked.start is not None else tracked.end
        started_at = _parse_timestamp((anchor or {}).get("timestamp"))
        if started_at is None:
            ignored += 1
            continue
        if started_at >= cutoff:
            windowed[invocation_id] = tracked

    starts = 0
    completions = 0
    unmatched_starts = 0
    outcome_counts: dict[str, int] = {}
    by_command: dict[str, int] = {}
    by_help_depth: dict[str, int] = {}
    by_exit_code: dict[str, int] = {}
    by_harness: dict[str, int] = {}
    by_origin: dict[str, int] = {}
    durations: list[float] = []
    failures: list[tuple[str, str, dict[str, Any]]] = []

    for invocation_id, tracked in windowed.items():
        facts = tracked.facts()
        if tracked.start is not None:
            starts += 1
        _bump(by_command, _command_label(facts))
        _bump(by_harness, _string_or_none(facts.get("harness")) or UNKNOWN_LABEL)
        _bump(by_origin, _string_or_none(facts.get("origin")) or UNKNOWN_LABEL)
        help_depth = _string_or_none(facts.get("help_depth"))
        if help_depth is not None:
            _bump(by_help_depth, help_depth)

        end = tracked.end
        if end is None:
            if tracked.start is not None:
                unmatched_starts += 1
            continue
        completions += 1
        outcome = _string_or_none(end.get("outcome")) or UNKNOWN_LABEL
        _bump(outcome_counts, outcome)
        exit_code = end.get("exit_code")
        _bump(by_exit_code, str(exit_code) if isinstance(exit_code, int) else "none")
        duration = end.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            durations.append(float(duration))
        if outcome in FAILING_OUTCOMES:
            failures.append(
                (
                    _string_or_none(end.get("timestamp")) or "",
                    invocation_id,
                    _recent_failure_entry(invocation_id, tracked),
                )
            )

    failures.sort(key=lambda item: (item[0], item[1]), reverse=True)
    ordered_durations = sorted(durations)

    return {
        "trace_root": str(trace_root),
        "retention_days": cli_trace.RETENTION_DAYS,
        "records_read": len(records) + corpus.malformed_lines,
        "records_ignored": ignored,
        "starts": starts,
        "completions": completions,
        "successes": outcome_counts.get(cli_trace.OUTCOME_OK, 0),
        "nonzero_exits": outcome_counts.get(cli_trace.OUTCOME_FAILED, 0),
        "usage_errors": outcome_counts.get(cli_trace.OUTCOME_USAGE, 0),
        "help_calls": outcome_counts.get(cli_trace.OUTCOME_HELP, 0),
        "interruptions": outcome_counts.get(cli_trace.OUTCOME_INTERRUPTED, 0),
        "exceptions": outcome_counts.get(cli_trace.OUTCOME_EXCEPTION, 0),
        "unmatched_starts": unmatched_starts,
        "by_command": dict(sorted(by_command.items())),
        "by_help_depth": dict(sorted(by_help_depth.items())),
        "by_exit_code": dict(sorted(by_exit_code.items())),
        "by_harness": dict(sorted(by_harness.items())),
        "by_origin": dict(sorted(by_origin.items())),
        "durations_ms": {
            "count": len(ordered_durations),
            "median": round(_percentile(ordered_durations, 0.5), 3)
            if ordered_durations
            else None,
            "p95": round(_percentile(ordered_durations, 0.95), 3)
            if ordered_durations
            else None,
            "max": round(ordered_durations[-1], 3) if ordered_durations else None,
            "total": round(sum(ordered_durations), 3),
        },
        "recent_failures": [entry for _, _, entry in failures[:RECENT_FAILURE_LIMIT]],
    }


def _hashable(value: Any) -> Any:
    """A dict key for a persisted value of unproven type."""
    return value if isinstance(value, (str, int, type(None))) else repr(value)


def _repair_session_ids(
    event_records: list[dict[str, Any]],
    snapshot: plans_models.PlanSnapshot | None,
) -> set[str]:
    """Session ids that are generated review-repair children.

    Two independent kinds of evidence answer this, and the *shape* of a session
    id is neither of them: an authored plan may legitimately use dotted ids, so
    a dotted id is a candidate, never a classification. Materialization lineage
    names the children a review created, and a plan's own session bodies carry
    the generated marker. Dotted ids only decide which bodies are worth reading.
    """
    found: set[str] = set()
    for record in event_records:
        if record.get("name") not in (
            events.REVIEW_CYCLE_EVENT,
            events.REVIEW_REPAIR_REOPENED_EVENT,
        ):
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        raw_ids = payload.get("repair_session_ids")
        if isinstance(raw_ids, list):
            found.update(value for value in raw_ids if isinstance(value, str) and value)
        raw_groups = payload.get("repair_groups")
        if isinstance(raw_groups, list):
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                value = group.get("repair_session_id")
                if isinstance(value, str) and value:
                    found.add(value)
    if snapshot is not None:
        for session in snapshot.sessions:
            if session.session_id.is_integer:
                continue
            try:
                body = session.path.read_text(encoding="utf-8")
            except OSError:
                continue
            if review_repair.contains_review_repair_session_marker(body):
                found.add(session.id)
    return found


def _check_slot(
    evidence: _PlanTimingEvidence, run_id: str, payload: dict[str, Any]
) -> _CheckRun:
    key = (
        run_id,
        _hashable(payload.get("turn_index")),
        _hashable(payload.get("check_index")),
    )
    slot = evidence.checks.get(key)
    if slot is None:
        slot = _CheckRun(run_id=run_id)
        evidence.checks[key] = slot
    return slot


def _ingest_timing_evidence(
    evidence: _PlanTimingEvidence, event_records: list[dict[str, Any]]
) -> None:
    """Read check identity and review-scope evidence from the same records.

    Read from the whole history rather than the window: a check or a scope
    selection belongs to the report when its *run* does, and a run that finished
    inside the window may have started its checks before the window opened.
    """
    for record in event_records:
        name = record.get("name")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        run_id = _string_or_none(payload.get("run_id"))
        if run_id is None:
            continue
        if name == events.VERIFICATION_CHECK_STARTED_EVENT:
            check = _check_slot(evidence, run_id, payload)
            check.name = _string_or_none(payload.get("name")) or check.name
            check.kind = _string_or_none(payload.get("kind")) or check.kind
            check.repository = _string_or_none(payload.get("repo")) or check.repository
            check.working_directory = (
                _string_or_none(payload.get("cwd")) or check.working_directory
            )
            argv = payload.get("argv")
            if isinstance(argv, list):
                command = tuple(item for item in argv if isinstance(item, str))
                if command:
                    check.argv = command
        elif name == events.VERIFICATION_CHECK_FINISHED_EVENT:
            check = _check_slot(evidence, run_id, payload)
            check.finish_seen = True
            check.name = _string_or_none(payload.get("name")) or check.name
            check.kind = _string_or_none(payload.get("kind")) or check.kind
            check.repository = _string_or_none(payload.get("repo")) or check.repository
            check.status = _string_or_none(payload.get("status")) or check.status
            duration = payload.get("duration_seconds")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                seconds = float(duration)
                # First usable reading wins, as it does in the execution timing
                # collector: a repeated finish restates one execution, and
                # letting the later copy overwrite would make the scoped seconds
                # here disagree with the cumulative check work beside them.
                if (
                    check.duration_seconds is None
                    and seconds >= 0
                    and seconds == seconds
                    and seconds != float("inf")
                ):
                    check.duration_seconds = seconds
        elif name == events.REVIEW_SCOPE_EVENT:
            evidence.review_scopes.append(
                (
                    run_id,
                    _string_or_none(payload.get("scope")) or UNKNOWN_LABEL,
                    _string_or_none(payload.get("fallback_reason")),
                    payload.get("refreshed") is True,
                )
            )


def _check_scope(plan_key: str, check: _CheckRun) -> _CheckScope:
    command = " ".join(check.argv) if check.argv else check.name
    if check.repository is None and check.working_directory is None:
        return _CheckScope(None, None, plan_key, command or UNKNOWN_COMMAND_LABEL)
    return _CheckScope(
        check.repository,
        check.working_directory,
        None,
        command or UNKNOWN_COMMAND_LABEL,
    )


def _attempt_phase(
    attempt: execution_timing.TurnAttempt, repair_session_ids: set[str]
) -> str:
    phase = attempt.phase
    if phase != execution_timing.PHASE_WORKER_EXECUTION:
        return phase
    if attempt.session_id is not None and attempt.session_id in repair_session_ids:
        return TIMING_PHASE_GENERATED_REPAIR
    return TIMING_PHASE_IMPLEMENTATION


def _run_phase_seconds(
    run: execution_timing.RunTiming, repair_session_ids: set[str]
) -> dict[str, float]:
    """One run's seconds under this report's phase vocabulary.

    Turn phases are re-derived from the run's attempts so the implementation and
    generated-repair figures add back up to the collector's single
    ``worker_execution`` total; the remaining phases are the collector's own.
    """
    seconds = dict.fromkeys(TIMING_PHASES, 0.0)
    for attempt in run.attempts:
        if attempt.duration_seconds is None:
            continue
        seconds[_attempt_phase(attempt, repair_session_ids)] += attempt.duration_seconds
    for phase in _RUN_LEVEL_TIMING_PHASES:
        seconds[phase] += run.phase_seconds.get(phase, 0.0)
    if run.unattributed_seconds is not None:
        seconds[execution_timing.PHASE_UNATTRIBUTED] += run.unattributed_seconds
    return seconds


def _attempt_order_key(
    run: execution_timing.RunTiming, attempt: execution_timing.TurnAttempt
) -> tuple[int, str, str, int]:
    """Order a session's attempts oldest first, undated ones last."""
    moment = attempt.started_at or attempt.finished_at
    return (
        1 if moment is None else 0,
        moment.isoformat() if moment is not None else "",
        run.run_id,
        attempt.turn_index if attempt.turn_index is not None else -1,
    )


def _repeated_attempts(
    timing: execution_timing.ExecutionTimingReport,
    repair_ids: dict[str, set[str]],
    included_run_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    """Count in-window attempts that followed an earlier attempt.

    Ordering spans the *whole* supplied history, not the window, so a retry
    whose first attempt happened before the window opened is still recognized as
    a repeat rather than mistaken for a first try.

    The seconds are reported as what they are — time spent on attempts after the
    first. Some of it produced the work that finally landed, so it is never
    labelled discarded or wasted.
    """
    ordered: dict[
        tuple[str, str],
        list[
            tuple[
                tuple[int, str, str, int],
                execution_timing.RunTiming,
                execution_timing.TurnAttempt,
            ]
        ],
    ] = {}
    for run in timing.runs:
        for attempt in run.attempts:
            if attempt.session_id is None:
                continue
            ordered.setdefault((run.plan_key, attempt.session_id), []).append(
                (_attempt_order_key(run, attempt), run, attempt)
            )

    additional = 0
    additional_seconds = 0.0
    attempts_without_duration = 0
    sessions_with_additional = 0
    by_phase: dict[str, int] = {}
    per_plan: dict[str, int] = {}
    for (plan_key, _session_id), items in ordered.items():
        items.sort(key=lambda item: item[0])
        counted = 0
        for _key, run, attempt in items[1:]:
            if (run.plan_key, run.run_id) not in included_run_keys:
                continue
            counted += 1
            additional += 1
            phase = _attempt_phase(attempt, repair_ids.get(plan_key, set()))
            by_phase[phase] = by_phase.get(phase, 0) + 1
            if attempt.duration_seconds is None:
                attempts_without_duration += 1
            else:
                additional_seconds += attempt.duration_seconds
        if counted:
            sessions_with_additional += 1
            per_plan[plan_key] = per_plan.get(plan_key, 0) + counted

    return {
        "sessions_tracked": len(ordered),
        "sessions_with_additional_attempts": sessions_with_additional,
        "additional_attempts": additional,
        "additional_attempt_seconds": round(additional_seconds, 3),
        "additional_attempts_without_duration": attempts_without_duration,
        "additional_attempts_by_phase": dict(sorted(by_phase.items())),
        "_per_plan": per_plan,
    }


def _check_scope_rows(
    evidence: dict[str, _PlanTimingEvidence],
    included_run_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], int, int]:
    """Aggregate finished checks into ranked scopes.

    Returns the rows, the number of checks they cover, and how many of those
    had no recorded command, which is the label that keeps a partial ranking
    from reading as a complete one.
    """
    rows: dict[_CheckScope, dict[str, Any]] = {}
    runs_by_scope: dict[_CheckScope, set[tuple[str, str]]] = {}
    scoped_checks = 0
    without_command = 0
    for plan_key, item in sorted(evidence.items()):
        for key in sorted(item.checks, key=repr):
            check = item.checks[key]
            if not check.finish_seen:
                continue
            if (plan_key, check.run_id) not in included_run_keys:
                continue
            scoped_checks += 1
            if check.argv is None:
                without_command += 1
            scope = _check_scope(plan_key, check)
            row = rows.get(scope)
            if row is None:
                row = scope.to_dict() | {
                    "checks": 0,
                    "seconds": 0.0,
                    "checks_without_duration": 0,
                    "by_status": {},
                }
                rows[scope] = row
                runs_by_scope[scope] = set()
            row["checks"] += 1
            runs_by_scope[scope].add((plan_key, check.run_id))
            if check.duration_seconds is None:
                row["checks_without_duration"] += 1
            else:
                row["seconds"] += check.duration_seconds
            status = check.status or UNKNOWN_LABEL
            row["by_status"][status] = row["by_status"].get(status, 0) + 1
    ordered: list[dict[str, Any]] = []
    for scope, row in rows.items():
        row["runs"] = len(runs_by_scope[scope])
        row["seconds"] = round(row["seconds"], 3)
        row["by_status"] = dict(sorted(row["by_status"].items()))
        ordered.append(row)
    ordered.sort(
        key=lambda row: (
            -row["seconds"],
            -row["checks"],
            row["repository"] or "",
            row["working_directory"] or "",
            row["plan_key"] or "",
            row["command"],
        )
    )
    return ordered, scoped_checks, without_command


def _review_scope_rows(
    evidence: dict[str, _PlanTimingEvidence],
    included_run_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    selections = 0
    refreshed = 0
    by_scope: dict[str, int] = {}
    fallback_reasons: dict[str, int] = {}
    without_fallback = 0
    for plan_key, item in sorted(evidence.items()):
        for run_id, scope, reason, was_refreshed in item.review_scopes:
            if (plan_key, run_id) not in included_run_keys:
                continue
            selections += 1
            by_scope[scope] = by_scope.get(scope, 0) + 1
            if was_refreshed:
                refreshed += 1
            if reason is None:
                without_fallback += 1
            else:
                fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
    return {
        "selections": selections,
        "refreshed_selections": refreshed,
        "by_scope": dict(sorted(by_scope.items())),
        "fallback_reasons": dict(sorted(fallback_reasons.items())),
        "selections_without_fallback_reason": without_fallback,
    }


def _plan_rows(
    timing: execution_timing.ExecutionTimingReport,
    evidence: dict[str, _PlanTimingEvidence],
    per_run_seconds: dict[tuple[str, str], dict[str, float]],
    additional_by_plan: dict[str, int],
) -> list[dict[str, Any]]:
    """One row per plan, summed across every invocation it resumed through.

    A plan that was paused and resumed is one body of work in several runs, so
    its rows aggregate by plan rather than by run, and ``runs`` says how many
    invocations the total covers.
    """
    rows: dict[str, dict[str, Any]] = {}
    for run in timing.included_runs:
        row = rows.get(run.plan_key)
        if row is None:
            known = evidence.get(run.plan_key)
            row = {
                "plan_key": run.plan_key,
                "plan_dir": known.plan_dir if known is not None else None,
                "runs": 0,
                "runs_with_known_wall": 0,
                "wall_seconds": 0.0,
                "attributed_seconds": 0.0,
                "seconds": dict.fromkeys(TIMING_PHASES, 0.0),
                "attempts": 0,
                "additional_attempts": additional_by_plan.get(run.plan_key, 0),
                "verification_checks": 0,
                "verification_check_seconds": 0.0,
                "by_classification": dict.fromkeys(execution_timing.CLASSIFICATIONS, 0),
            }
            rows[run.plan_key] = row
        row["runs"] += 1
        row["by_classification"][run.classification] += 1
        if run.wall_seconds is not None:
            row["runs_with_known_wall"] += 1
            row["wall_seconds"] += run.wall_seconds
        phases = per_run_seconds[(run.plan_key, run.run_id)]
        for phase in TIMING_PHASES:
            row["seconds"][phase] += phases[phase]
            if phase != execution_timing.PHASE_UNATTRIBUTED:
                row["attributed_seconds"] += phases[phase]
        row["attempts"] += len(run.attempts)
        row["verification_checks"] += run.verification_check_count
        row["verification_check_seconds"] += run.verification_check_seconds
    ordered = list(rows.values())
    for row in ordered:
        row["wall_seconds"] = round(row["wall_seconds"], 3)
        row["attributed_seconds"] = round(row["attributed_seconds"], 3)
        row["verification_check_seconds"] = round(row["verification_check_seconds"], 3)
        row["seconds"] = {
            phase: round(value, 3) for phase, value in row["seconds"].items()
        }
    ordered.sort(
        key=lambda row: (
            -row["wall_seconds"],
            -row["attributed_seconds"],
            row["plan_key"],
        )
    )
    return ordered


def collect_execution_timing(
    timing: execution_timing.ExecutionTimingReport,
    evidence: dict[str, _PlanTimingEvidence],
) -> dict[str, Any]:
    """Render the timing collector's per-run accounting as one report section.

    Nothing here re-measures anything: the seconds all come from
    :func:`execution_timing.collect_execution_timing`. What this adds is the
    lineage the collector has no evidence for (which worker turns were generated
    repairs), the scoping a ranking needs (which project a check ran in), and
    the coverage numbers that keep a share from being read as a census.
    """
    repair_ids = {key: item.repair_session_ids for key, item in evidence.items()}
    per_run_seconds = {
        (run.plan_key, run.run_id): _run_phase_seconds(
            run, repair_ids.get(run.plan_key, set())
        )
        for run in timing.runs
    }
    included_run_keys = {(run.plan_key, run.run_id) for run in timing.included_runs}

    known_seconds = dict.fromkeys(TIMING_PHASES, 0.0)
    runs_with_phase = dict.fromkeys(TIMING_PHASES, 0)
    wall_seconds = 0.0
    runs_with_known_wall = 0
    for run in timing.included_runs:
        phases = per_run_seconds[(run.plan_key, run.run_id)]
        for phase in TIMING_PHASES:
            if phases[phase]:
                known_seconds[phase] += phases[phase]
                runs_with_phase[phase] += 1
        if run.wall_seconds is not None:
            wall_seconds += run.wall_seconds
            runs_with_known_wall += 1

    share_seconds = dict.fromkeys(TIMING_PHASES, 0.0)
    share_basis = 0.0
    trustworthy = timing.trustworthy_runs
    for run in trustworthy:
        phases = per_run_seconds[(run.plan_key, run.run_id)]
        for phase in TIMING_PHASES:
            share_seconds[phase] += phases[phase]
        if run.wall_seconds is not None:
            share_basis += run.wall_seconds
    # An empty or zero basis reports no shares at all: zeroes would read as
    # "no time went anywhere", which absent evidence does not support.
    shares = (
        {phase: round(share_seconds[phase] / share_basis, 4) for phase in TIMING_PHASES}
        if trustworthy and share_basis > 0
        else None
    )

    repeated = _repeated_attempts(timing, repair_ids, included_run_keys)
    additional_by_plan: dict[str, int] = repeated.pop("_per_plan")
    scope_rows, scoped_checks, checks_without_command = _check_scope_rows(
        evidence, included_run_keys
    )
    check_work = timing.verification_check_work()
    included_by_classification = timing.included_counts_by_classification()

    return {
        "window_start": (
            timing.window_start.isoformat() if timing.window_start is not None else None
        ),
        "window_end": (
            timing.window_end.isoformat() if timing.window_end is not None else None
        ),
        "tolerance_seconds": timing.tolerance_seconds,
        "coverage": {
            "plans_with_evidence": len(evidence),
            "runs_with_evidence": len(timing.runs),
            "runs_included": len(timing.included_runs),
            "runs_by_classification": timing.counts_by_classification(),
            "included_runs_by_classification": included_by_classification,
            "exclusions": timing.exclusions(),
            "runs_with_known_wall": runs_with_known_wall,
            "runs_outside_share_basis": len(timing.included_runs) - len(trustworthy),
            "unattributable_records": timing.unattributable_records,
            "unattributable_turn_results": timing.unattributable_turn_results,
        },
        "phases": {
            "wall_seconds": round(wall_seconds, 3),
            # Measured over every included run, so a run that never recorded one
            # phase still contributes the phases it did record. The per-phase
            # denominators therefore differ and are published beside them.
            "seconds": {
                phase: round(known_seconds[phase], 3) for phase in TIMING_PHASES
            },
            "runs_with_phase": dict(runs_with_phase),
            "share_basis_runs": len(trustworthy),
            "share_basis_seconds": round(share_basis, 3),
            "share_seconds": {
                phase: round(share_seconds[phase], 3) for phase in TIMING_PHASES
            },
            "shares": shares,
        },
        "verification_checks": {
            "runs_with_checks": check_work["runs_with_checks"],
            "checks": check_work["checks"],
            # Cumulative check work, not run time: a parallel recipe's checks
            # overlap, so this can exceed the phase elapsed seconds beside it.
            "cumulative_seconds": check_work["seconds"],
            "phase_seconds": check_work["phase_seconds"],
            "scoped_checks": scoped_checks,
            "checks_without_command": checks_without_command,
            "scopes": scope_rows,
        },
        "repeated_attempts": repeated,
        "review_scope": _review_scope_rows(evidence, included_run_keys),
        "plans": _plan_rows(timing, evidence, per_run_seconds, additional_by_plan),
    }


def collect_metrics(
    *,
    state_dir: str | Path | None = None,
    since_days: int = DEFAULT_SINCE_DAYS,
    now: datetime | None = None,
) -> MetricsReport:
    """Aggregate reliability and storage metrics for the given state root."""
    if since_days <= 0:
        raise ValueError("--since-days must be a positive integer")
    state_root = state.state_root(state_dir)
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=since_days)

    turns_total = 0
    turns_successful = 0
    successful_runtime = 0.0
    unsuccessful_runtime = 0.0
    interrupted_turns = 0
    session_turns: dict[tuple[str, str], _SessionTurns] = {}
    completion_times: dict[tuple[str, str], str] = {}

    pause_counts: dict[str, int] = {}
    pauses_total = 0
    pauses_never_retry = 0
    pauses_retryable = 0

    verify_failures = 0
    verify_exhausted = 0
    verify_failed_sessions: dict[tuple[str, str], str] = {}
    paused_work_commits = 0
    paused_work_commit_results: dict[str, int] = {}

    cycle_disposition_counts: dict[str, int] = {}
    cycles_total = 0
    findings_introduced = 0
    findings_closed = 0
    findings_repeated = 0
    unique_materialization_keys: set[tuple[str, str]] = set()
    reopen_attempts = 0
    successful_reopen_materializations = 0
    reopen_failures = 0
    reopen_source_disposition_counts: dict[str, int] = {}
    reopen_failure_result_counts: dict[str, int] = {}

    agent_mux_deliveries = 0
    agent_mux_acknowledged = 0
    agent_mux_failed = 0
    agent_mux_legacy = 0
    agent_mux_status_counts: dict[str, int] = {}
    agent_mux_error_counts: dict[str, int] = {}

    artifact_total_bytes = 0
    artifact_grown_bytes = 0
    artifact_grown_files = 0

    compaction_bytes_before = 0
    compaction_bytes_after = 0
    compaction_artifact_bytes = 0
    compaction_projected = 0
    compaction_dirs = 0
    compaction_skipped: list[str] = []

    state_dirs_scanned = 0
    state_dirs_active = 0

    timing_runs: list[execution_timing.RunTiming] = []
    timing_evidence: dict[str, _PlanTimingEvidence] = {}
    timing_unattributable_records = 0
    timing_unattributable_results = 0

    for plan_state_dir in sorted(session_overview._iter_state_plan_dirs(state_root)):
        state_dirs_scanned += 1
        dir_key = plan_state_dir.name
        total_bytes, grown_bytes, grown_files = _directory_sizes(plan_state_dir, cutoff)
        artifact_total_bytes += total_bytes
        artifact_grown_bytes += grown_bytes
        artifact_grown_files += grown_files
        if grown_files == 0:
            # Nothing under this dir was written inside the window, so no turn
            # result or event record can carry an in-window timestamp either.
            # Skipping here is what keeps a narrow window off the full corpus.
            continue

        active = False
        plan_dir = _plan_dir_for_state_dir(plan_state_dir)
        snapshot = None
        if plan_dir is not None:
            # Metrics remain best-effort for stale state directories. The
            # full event history still supplies an events-only fallback.
            with contextlib.suppress(plans_errors.PlanError, OSError):
                snapshot = plans_snapshot.inspect_plan(plan_dir)
        materialization_keys = review_continuation.reconciled_materialization_keys(
            plan_state_dir / events.EVENTS_FILE_NAME,
            snapshot,
            since=cutoff,
        )
        unique_materialization_keys.update(
            (dir_key, key) for key in materialization_keys
        )

        # Read once, then used twice: the windowed sections below filter these
        # record by record, while the timing collector needs the full history so
        # lineage and retry ordering can see an attempt that predates the window.
        turn_payloads = _iter_turn_results(plan_state_dir)
        event_records = _read_event_records(plan_state_dir / events.EVENTS_FILE_NAME)

        plan_timing = _PlanTimingEvidence(plan_key=dir_key, plan_dir=plan_dir)
        plan_timing.repair_session_ids = _repair_session_ids(event_records, snapshot)
        _ingest_timing_evidence(plan_timing, event_records)
        timing_evidence[dir_key] = plan_timing
        # Collected per plan and reduced to per-run accounting immediately, so
        # the corpus is never held in memory all at once.
        plan_timing_report = execution_timing.collect_execution_timing(
            [
                execution_timing.RunEvidence(
                    plan_key=dir_key,
                    event_records=event_records,
                    turn_results=turn_payloads,
                )
            ],
            window_start=cutoff,
        )
        timing_runs.extend(plan_timing_report.runs)
        timing_unattributable_records += plan_timing_report.unattributable_records
        timing_unattributable_results += plan_timing_report.unattributable_turn_results

        for payload in turn_payloads:
            finished_at = _parse_timestamp(payload.get("finished_at"))
            if finished_at is None or finished_at < cutoff:
                continue
            active = True
            turns_total += 1
            duration = payload.get("duration_seconds")
            duration = float(duration) if isinstance(duration, (int, float)) else 0.0
            successful = payload.get("status_result") == _COMPLETE_STATUS
            if successful:
                turns_successful += 1
                successful_runtime += duration
            else:
                unsuccessful_runtime += duration
            if payload.get("interrupted") is True:
                interrupted_turns += 1

            session_id = payload.get("session_id")
            started_at = payload.get("started_at")
            if (
                payload.get("session_kind") == _IMPLEMENTATION_KIND
                and isinstance(session_id, str)
                and isinstance(started_at, str)
            ):
                key = (dir_key, session_id)
                session_turns.setdefault(key, _SessionTurns()).turns.append(
                    (started_at, successful)
                )
                if successful:
                    recorded = completion_times.get(key)
                    if recorded is None or started_at > recorded:
                        completion_times[key] = started_at

        for record in event_records:
            name = record.get("name")
            payload = record.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            created_at = _parse_timestamp(record.get("created_at"))
            if created_at is None or created_at < cutoff:
                continue
            if name == events.RUN_PAUSED_EVENT:
                active = True
                pauses_total += 1
                reason = payload.get("reason")
                reason = reason if isinstance(reason, str) and reason else "unknown"
                pause_counts[reason] = pause_counts.get(reason, 0) + 1
                details = payload.get("details")
                details = details if isinstance(details, dict) else {}
                classification = details.get("failure_classification")
                if isinstance(classification, dict):
                    if classification.get("retryable") is True:
                        pauses_retryable += 1
                    else:
                        pauses_never_retry += 1
                if reason == _VERIFY_FAILED_REASON:
                    verify_failures += 1
                    if details.get("verification_repair_exhausted") is True:
                        verify_exhausted += 1
                    session_id = details.get("session")
                    if isinstance(session_id, str):
                        key = (dir_key, session_id)
                        record_time = record.get("created_at")
                        if isinstance(record_time, str):
                            existing = verify_failed_sessions.get(key)
                            if existing is None or record_time < existing:
                                verify_failed_sessions[key] = record_time
            elif name == events.PAUSED_WORK_COMMITTED_EVENT:
                active = True
                # One event per settlement, not per commit: the run appends it
                # for every outcome the settlement reached, including finding
                # nothing to commit. The total answers "how often did a
                # recoverable pause settle its own dirt instead of leaving it
                # for a human", and the breakdown is what keeps that number
                # from reading as "how often did a commit land".
                paused_work_commits += 1
                result = payload.get("result")
                result = result if isinstance(result, str) and result else "unknown"
                paused_work_commit_results[result] = (
                    paused_work_commit_results.get(result, 0) + 1
                )
            elif name == events.REVIEW_CYCLE_EVENT:
                active = True
                cycles_total += 1
                disposition = payload.get("disposition")
                disposition = (
                    disposition
                    if isinstance(disposition, str) and disposition
                    else "unknown"
                )
                cycle_disposition_counts[disposition] = (
                    cycle_disposition_counts.get(disposition, 0) + 1
                )
                for field_name, counter in (
                    ("introduced_findings", "introduced"),
                    ("closed_findings", "closed"),
                    ("repeated_findings", "repeated"),
                ):
                    values = payload.get(field_name)
                    count = len(values) if isinstance(values, list) else 0
                    if counter == "introduced":
                        findings_introduced += count
                    elif counter == "closed":
                        findings_closed += count
                    else:
                        findings_repeated += count
            elif name == events.REVIEW_REPAIR_REOPENED_EVENT:
                active = True
                if payload.get("recovered") is True:
                    # Crash-recovery backfills preserve durable lineage but
                    # are not fresh reopen attempts in this time window.
                    continue
                reopen_attempts += 1
                source_disposition = payload.get("source_disposition")
                source_disposition = (
                    source_disposition
                    if isinstance(source_disposition, str) and source_disposition
                    else "unknown"
                )
                reopen_source_disposition_counts[source_disposition] = (
                    reopen_source_disposition_counts.get(source_disposition, 0) + 1
                )
                if payload.get("materialized") is True:
                    successful_reopen_materializations += 1
                else:
                    reopen_failures += 1
                    result = payload.get("result")
                    result = result if isinstance(result, str) and result else "unknown"
                    reopen_failure_result_counts[result] = (
                        reopen_failure_result_counts.get(result, 0) + 1
                    )
            elif name == events.AGENT_MUX_DELIVERY_EVENT:
                active = True
                agent_mux_deliveries += 1
                status = payload.get("final_status")
                status = status if isinstance(status, str) and status else "unknown"
                agent_mux_status_counts[status] = (
                    agent_mux_status_counts.get(status, 0) + 1
                )
                error_classification = payload.get("error_classification")
                error_classification = (
                    error_classification
                    if isinstance(error_classification, str) and error_classification
                    else "unknown"
                )
                agent_mux_error_counts[error_classification] = (
                    agent_mux_error_counts.get(error_classification, 0) + 1
                )
                if payload.get("acknowledged") is True:
                    agent_mux_acknowledged += 1
                elif status == "legacy_unacknowledged":
                    agent_mux_legacy += 1
                else:
                    agent_mux_failed += 1

        if active:
            state_dirs_active += 1
            if plan_dir is None:
                compaction_skipped.append(dir_key)
            else:
                try:
                    report = compact_state.compact_plan_state(
                        plan_dir, state_dir=state_root, apply=False
                    )
                except Exception:  # noqa: BLE001 - projection is best-effort
                    compaction_skipped.append(dir_key)
                else:
                    compaction_dirs += 1
                    compaction_bytes_before += report.bytes_before
                    compaction_bytes_after += report.bytes_after
                    compaction_artifact_bytes += report.artifact_bytes_added
                    compaction_projected += report.projected_bytes_reclaimed

    sessions_attempted = len(session_turns)
    sessions_completed = 0
    sessions_first_pass = 0
    for tracker in session_turns.values():
        ordered = tracker.sorted_turns()
        if any(ok for _, ok in ordered):
            sessions_completed += 1
            if ordered[0][1]:
                sessions_first_pass += 1

    verify_sessions_repaired = 0
    for key, first_failure_at in verify_failed_sessions.items():
        completed_at = completion_times.get(key)
        if completed_at is not None and completed_at >= first_failure_at:
            verify_sessions_repaired += 1

    # The per-plan reports are recombined rather than recollected: every run is
    # keyed by (plan, run id), so concatenating them is the same answer one
    # whole-corpus call would have given, without holding the corpus.
    timing_report = execution_timing.ExecutionTimingReport(
        window_start=cutoff,
        window_end=None,
        tolerance_seconds=execution_timing.DEFAULT_TOLERANCE_SECONDS,
        runs=tuple(sorted(timing_runs, key=lambda run: (run.plan_key, run.run_id))),
        unattributable_records=timing_unattributable_records,
        unattributable_turn_results=timing_unattributable_results,
    )

    first_pass_rate = (
        sessions_first_pass / sessions_attempted if sessions_attempted else None
    )
    repair_rate = (
        verify_sessions_repaired / len(verify_failed_sessions)
        if verify_failed_sessions
        else None
    )

    return MetricsReport(
        state_root=str(state_root),
        since_days=since_days,
        window_start=cutoff.isoformat(),
        generated_at=now.isoformat(),
        state_dirs_scanned=state_dirs_scanned,
        state_dirs_active=state_dirs_active,
        turns={
            "total": turns_total,
            "successful": turns_successful,
            "unsuccessful": turns_total - turns_successful,
            "interrupted": interrupted_turns,
            "successful_runtime_seconds": round(successful_runtime, 3),
            "unsuccessful_runtime_seconds": round(unsuccessful_runtime, 3),
        },
        first_pass={
            "sessions_attempted": sessions_attempted,
            "sessions_completed": sessions_completed,
            "sessions_completed_first_pass": sessions_first_pass,
            "first_pass_rate": first_pass_rate,
        },
        pauses={
            "total": pauses_total,
            "by_reason": dict(sorted(pause_counts.items())),
            "classified_never_retry": pauses_never_retry,
            "classified_retryable": pauses_retryable,
        },
        verification_repair={
            "verify_failed_pauses": verify_failures,
            "repair_exhausted_pauses": verify_exhausted,
            "sessions_with_verify_failures": len(verify_failed_sessions),
            "sessions_repaired": verify_sessions_repaired,
            "repair_success_rate": repair_rate,
            "paused_work_commits": paused_work_commits,
            "paused_work_commits_by_result": dict(
                sorted(paused_work_commit_results.items())
            ),
        },
        review_cycles={
            "cycles_recorded": cycles_total,
            "by_disposition": dict(sorted(cycle_disposition_counts.items())),
            # Keep the legacy key while making its cross-event semantics
            # idempotent. New consumers should prefer the explicit name.
            "repairs_materialized": len(unique_materialization_keys),
            "unique_materializations": len(unique_materialization_keys),
            "reopen_attempts": reopen_attempts,
            "successful_reopen_materializations": (successful_reopen_materializations),
            "reopen_failures": reopen_failures,
            "reopen_attempts_by_source_disposition": dict(
                sorted(reopen_source_disposition_counts.items())
            ),
            "reopen_failures_by_result": dict(
                sorted(reopen_failure_result_counts.items())
            ),
            "findings_introduced": findings_introduced,
            "findings_closed": findings_closed,
            "findings_repeated": findings_repeated,
        },
        agent_mux_delivery={
            "total": agent_mux_deliveries,
            "acknowledged": agent_mux_acknowledged,
            "failed": agent_mux_failed,
            "legacy_unacknowledged": agent_mux_legacy,
            "by_status": dict(sorted(agent_mux_status_counts.items())),
            "by_error_classification": dict(sorted(agent_mux_error_counts.items())),
        },
        artifacts={
            "total_bytes": artifact_total_bytes,
            "grown_bytes": artifact_grown_bytes,
            "grown_files": artifact_grown_files,
        },
        compaction={
            "dirs_projected": compaction_dirs,
            "dirs_skipped": sorted(compaction_skipped),
            "bytes_before": compaction_bytes_before,
            "bytes_after": compaction_bytes_after,
            "artifact_bytes_added": compaction_artifact_bytes,
            "projected_bytes_reclaimed": compaction_projected,
        },
        # The inbox spans every plan, so it is aggregated once from the shared
        # ledger rather than per state directory.
        assurance=assurance_report.collect_assurance_metrics(
            state_dir=state_root, since=cutoff
        ),
        # The trace is per-invocation, not per-plan, so it is read once from
        # the root itself. This report's own start record is already on disk
        # and would otherwise show up as an unmatched start.
        cli_invocations=collect_cli_invocations(
            state_root,
            cutoff=cutoff,
            exclude_invocation_id=cli_trace.current_invocation_id(),
        ),
        # Rendered from evidence already read above; the section adds no scan.
        execution_timing=collect_execution_timing(timing_report, timing_evidence),
    )


def _format_bytes(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(count)} B"


def _format_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _format_hours(seconds: float) -> str:
    return f"{seconds / 3600:.2f} h"


def _print_execution_timing_section(section: dict[str, Any]) -> None:
    """Print where run time went, with the coverage that qualifies it.

    Coverage is printed before any share so a share is never read as a census,
    the cumulative check figure is labelled as work rather than elapsed time,
    and the ranked lists say how much of the whole they show.
    """
    print("\nExecution timing:")
    if not section:
        print("  not collected")
        return
    coverage = section["coverage"]
    print(
        f"  runs {coverage['runs_included']} in window "
        f"of {coverage['runs_with_evidence']} with evidence "
        f"across {coverage['plans_with_evidence']} plan(s)"
    )
    if coverage["runs_included"] == 0:
        print("  no run finished inside the window; nothing is measured")
        return
    included = coverage["included_runs_by_classification"]
    print(
        "  included by trust: "
        + ", ".join(f"{name} {included.get(name, 0)}" for name in included)
    )
    if coverage["exclusions"]:
        print(f"  excluded: {_format_counts(coverage['exclusions'])}")
    if coverage["unattributable_records"] or coverage["unattributable_turn_results"]:
        print(
            f"  unattributable evidence: "
            f"{coverage['unattributable_records']} event record(s), "
            f"{coverage['unattributable_turn_results']} turn result(s)"
        )
    phases = section["phases"]
    print(
        f"  wall clock {_format_hours(phases['wall_seconds'])} known for "
        f"{coverage['runs_with_known_wall']} of "
        f"{coverage['runs_included']} included run(s)"
    )
    print("  measured seconds by phase (every included run, partial ones too):")
    for phase in TIMING_PHASES:
        print(
            f"    {phase} {_format_hours(phases['seconds'][phase])} "
            f"({phases['runs_with_phase'][phase]} run(s))"
        )
    shares = phases["shares"]
    if shares is None:
        print(
            f"  shares: n/a - no complete run with a known duration "
            f"({coverage['runs_outside_share_basis']} included run(s) "
            f"carry a gap or an anomaly)"
        )
    else:
        print(
            f"  shares over {phases['share_basis_runs']} complete run(s) "
            f"({_format_hours(phases['share_basis_seconds'])}), "
            f"{coverage['runs_outside_share_basis']} included run(s) excluded "
            f"from that basis:"
        )
        print(
            "    "
            + ", ".join(
                f"{phase} {shares[phase] * 100:.1f}%" for phase in TIMING_PHASES
            )
        )
    checks = section["verification_checks"]
    print(
        f"  verification checks {checks['checks']} across "
        f"{checks['runs_with_checks']} run(s): cumulative check work "
        f"{_format_hours(checks['cumulative_seconds'])} "
        f"(concurrent checks overlap, so this is work done and not elapsed "
        f"run time) beside verification phase "
        f"{_format_hours(checks['phase_seconds'])}"
    )
    repeated = section["repeated_attempts"]
    print(
        f"  repeated attempts {repeated['additional_attempts']} after the first "
        f"in {repeated['sessions_with_additional_attempts']} session(s), "
        f"{_format_hours(repeated['additional_attempt_seconds'])} "
        f"(time spent re-attempting; some of it produced the work that landed)"
    )
    if repeated["additional_attempts_without_duration"]:
        print(
            f"    {repeated['additional_attempts_without_duration']} of those "
            f"recorded no duration"
        )
    if repeated["additional_attempts_by_phase"]:
        print(
            "    by phase: " + _format_counts(repeated["additional_attempts_by_phase"])
        )
    review_scope = section["review_scope"]
    if review_scope["selections"]:
        print(
            f"  review scope selections {review_scope['selections']} "
            f"(refreshed {review_scope['refreshed_selections']})"
            + (
                f": {_format_counts(review_scope['by_scope'])}"
                if review_scope["by_scope"]
                else ""
            )
        )
        if review_scope["fallback_reasons"]:
            print(
                "    fallback reasons: "
                + _format_counts(review_scope["fallback_reasons"])
            )
        else:
            print("    fallback reasons: none recorded")
    plan_rows = section["plans"]
    if plan_rows:
        shown = plan_rows[:TIMING_PLAN_LIMIT]
        print(f"  top plans by duration ({len(shown)} of {len(plan_rows)}):")
        for row in shown:
            print(
                f"    {_format_hours(row['wall_seconds'])} "
                f"{row['plan_dir'] or row['plan_key']} "
                f"(runs {row['runs']}, known wall {row['runs_with_known_wall']}, "
                f"repeats {row['additional_attempts']})"
            )
    scopes = checks["scopes"]
    if scopes:
        shown_checks = scopes[:TIMING_CHECK_LIMIT]
        print(
            f"  top checks by cumulative work ({len(shown_checks)} of {len(scopes)}):"
        )
        for row in shown_checks:
            where = (
                row["repository"]
                or row["working_directory"]
                or row["plan_key"]
                or UNKNOWN_LABEL
            )
            print(
                f"    {_format_hours(row['seconds'])} "
                f"{row['checks']} execution(s) of {row['command']} [{where}]"
            )
        if checks["checks_without_command"]:
            print(
                f"    {checks['checks_without_command']} of "
                f"{checks['scoped_checks']} scoped check(s) recorded no command"
            )


def _print_assurance_section(assurance: dict[str, Any]) -> None:
    """Print the supervisor-audit section, separating coverage from correctness."""
    print("\nSupervisor audit:")
    if not assurance:
        print("  not collected")
        return
    if not assurance.get("available"):
        print(f"  ledger unreadable: {assurance.get('error')}")
        return
    coverage = assurance["coverage"]
    print(
        f"  runs recorded {assurance['runs_recorded']} "
        f"(eligible {coverage['eligible_runs']}, "
        f"ineligible {coverage['ineligible_runs']}), "
        f"audited {coverage['audited_runs']} "
        f"({_format_rate(coverage['coverage_rate'])})"
    )
    states = assurance["audit_states"]
    print("  states " + ", ".join(f"{name} {states.get(name, 0)}" for name in states))
    issues = assurance["issues"]
    print(
        f"  issues: {issues['observations']} sighting(s), "
        f"{issues['new']} new, {issues['recurring']} recurring, "
        f"{issues['open_total']} open now"
    )
    if issues["by_severity"]:
        print(
            "  by severity: "
            + ", ".join(
                f"{name} {count}" for name, count in issues["by_severity"].items()
            )
        )
    if issues["by_category"]:
        print(
            "  by category: "
            + ", ".join(
                f"{name} {count}" for name, count in issues["by_category"].items()
            )
        )
    print(
        f"  recurrence after resolution: "
        f"{issues['recurrence_after_resolution']} sighting(s) across "
        f"{issues['issues_recurring_after_resolution']} issue(s)"
    )
    recurring_categories = issues["recurrence_after_resolution_by_category"]
    if recurring_categories:
        print(
            "  recurrence by category: "
            + ", ".join(
                f"{name} {count}" for name, count in recurring_categories.items()
            )
        )
    dispositions = assurance["dispositions"]
    latency = dispositions["time_to_disposition"]
    median = latency["median_hours"]
    print(
        f"  dispositions {dispositions['total']} "
        f"(dismissed {_format_rate(dispositions['dismissal_rate'])}, "
        f"accepted risk {_format_rate(dispositions['accepted_risk_rate'])}), "
        f"median time to disposition "
        f"{'n/a' if median is None else f'{median:.1f} h'}"
    )
    closeouts = assurance["closeouts"]
    outcomes = ", ".join(
        f"{name} {count}" for name, count in closeouts["by_outcome"].items()
    )
    print(
        f"  closeouts {closeouts['total']}"
        f"{f' ({outcomes})' if outcomes else ''}, "
        f"escaped issues {closeouts['escaped_issues']}, "
        f"rework {closeouts['rework_minutes']:.0f} min"
    )
    integrity = assurance["integrity"]
    if integrity["anomalies"] or integrity["rejected_records"]:
        print(
            f"  ledger integrity: {integrity['anomalies']} anomaly(ies), "
            f"{integrity['rejected_records']} rejected record(s)"
        )


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def _format_duration_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f} ms"


def _print_cli_invocations_section(section: dict[str, Any]) -> None:
    """Print which command surfaces ran and which of them failed."""
    print("\nCLI invocations:")
    if not section:
        print("  not collected")
        return
    print(
        f"  trace root {section['trace_root']} "
        f"(retention {section['retention_days']} day(s), "
        f"{section['records_read']} record(s) read, "
        f"{section['records_ignored']} ignored)"
    )
    print(
        f"  starts {section['starts']}, completions {section['completions']}, "
        f"unmatched starts {section['unmatched_starts']}"
    )
    print(
        f"  successes {section['successes']}, "
        f"nonzero exits {section['nonzero_exits']}, "
        f"usage errors {section['usage_errors']}, "
        f"help {section['help_calls']}, "
        f"interrupted {section['interruptions']}, "
        f"exceptions {section['exceptions']}"
    )
    durations = section["durations_ms"]
    print(
        f"  duration: median {_format_duration_ms(durations['median'])}, "
        f"p95 {_format_duration_ms(durations['p95'])}, "
        f"max {_format_duration_ms(durations['max'])}"
    )
    for label, counts in (
        ("by command", section["by_command"]),
        ("by help depth", section["by_help_depth"]),
        ("by exit code", section["by_exit_code"]),
        ("by harness", section["by_harness"]),
        ("by origin", section["by_origin"]),
    ):
        if counts:
            print(f"  {label}: {_format_counts(counts)}")
    failures = section["recent_failures"]
    if not failures:
        return
    print(f"  recent failures ({len(failures)}):")
    for entry in failures:
        exception = entry["exception_type"]
        print(
            f"    {entry['timestamp']} {entry['command']} "
            f"{entry['outcome']} exit {entry['exit_code']} "
            f"[{entry['origin']}]"
            f"{f' {exception}' if exception else ''}"
        )


def _print_report(report: MetricsReport) -> None:
    print(f"Metrics for {report.state_root}")
    print(f"Window: last {report.since_days} day(s), since {report.window_start}")
    print(
        f"State dirs: {report.state_dirs_scanned} scanned, "
        f"{report.state_dirs_active} active in window"
    )
    turns = report.turns
    print("\nTurns:")
    print(
        f"  total {turns['total']}, successful {turns['successful']}, "
        f"unsuccessful {turns['unsuccessful']}, interrupted {turns['interrupted']}"
    )
    print(
        f"  unsuccessful runtime "
        f"{turns['unsuccessful_runtime_seconds'] / 3600:.2f} h "
        f"(successful {turns['successful_runtime_seconds'] / 3600:.2f} h)"
    )
    _print_execution_timing_section(report.execution_timing)
    first_pass = report.first_pass
    print("\nFirst-pass completion (implementation sessions):")
    print(
        f"  attempted {first_pass['sessions_attempted']}, "
        f"completed {first_pass['sessions_completed']}, "
        f"first-pass {first_pass['sessions_completed_first_pass']} "
        f"({_format_rate(first_pass['first_pass_rate'])})"
    )
    pauses = report.pauses
    print("\nPauses:")
    print(
        f"  total {pauses['total']} "
        f"(never-retry {pauses['classified_never_retry']}, "
        f"retryable {pauses['classified_retryable']})"
    )
    for reason, count in pauses["by_reason"].items():
        print(f"  {reason}: {count}")
    repair = report.verification_repair
    print("\nVerification repair:")
    print(
        f"  verify-failed pauses {repair['verify_failed_pauses']} "
        f"(exhausted {repair['repair_exhausted_pauses']}), "
        f"sessions {repair['sessions_with_verify_failures']}, "
        f"repaired {repair['sessions_repaired']} "
        f"({_format_rate(repair['repair_success_rate'])})"
    )
    paused_work = repair["paused_work_commits_by_result"]
    print(
        f"  paused-work settlements {repair['paused_work_commits']}"
        + (
            " ("
            + ", ".join(f"{result} {count}" for result, count in paused_work.items())
            + ")"
            if paused_work
            else ""
        )
    )
    cycles = report.review_cycles
    print("\nReview cycles:")
    print(
        f"  recorded {cycles['cycles_recorded']}, "
        f"unique materializations {cycles['unique_materializations']}"
    )
    print(
        f"  reopen attempts {cycles['reopen_attempts']}, "
        f"successful materializations "
        f"{cycles['successful_reopen_materializations']}, "
        f"failures {cycles['reopen_failures']}"
    )
    print(
        f"  findings introduced {cycles['findings_introduced']}, "
        f"closed {cycles['findings_closed']}, "
        f"repeated {cycles['findings_repeated']}"
    )
    for disposition, count in cycles["by_disposition"].items():
        print(f"  {disposition}: {count}")
    for disposition, count in cycles["reopen_attempts_by_source_disposition"].items():
        print(f"  reopen source {disposition}: {count}")
    for result, count in cycles["reopen_failures_by_result"].items():
        print(f"  reopen failure {result}: {count}")
    delivery = report.agent_mux_delivery
    print("\nAgentMux delivery:")
    print(
        f"  total {delivery['total']}, acknowledged {delivery['acknowledged']}, "
        f"failed {delivery['failed']}, "
        f"legacy unacknowledged {delivery['legacy_unacknowledged']}"
    )
    for status, count in delivery["by_status"].items():
        print(f"  {status}: {count}")
    artifacts_section = report.artifacts
    print("\nArtifacts:")
    print(
        f"  corpus {_format_bytes(artifacts_section['total_bytes'])}, "
        f"grown in window {_format_bytes(artifacts_section['grown_bytes'])} "
        f"({artifacts_section['grown_files']} files)"
    )
    _print_assurance_section(report.assurance)
    compaction_section = report.compaction
    print("\nCompaction (dry-run over active dirs):")
    print(
        f"  projected reclaim "
        f"{_format_bytes(compaction_section['projected_bytes_reclaimed'])} "
        f"across {compaction_section['dirs_projected']} dir(s)"
    )
    if compaction_section["dirs_skipped"]:
        print(f"  skipped: {len(compaction_section['dirs_skipped'])} dir(s)")
    _print_cli_invocations_section(report.cli_invocations)


def metrics_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl metrics``."""
    try:
        report = collect_metrics(
            state_dir=getattr(args, "state_dir", None),
            since_days=getattr(args, "since_days", DEFAULT_SINCE_DAYS),
        )
    except (OSError, ValueError) as error:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {"ok": False, "command": "metrics", "error": str(error)},
                    sort_keys=True,
                )
            )
        else:
            print(f"metrics failed: {error}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        _print_report(report)
    return 0
