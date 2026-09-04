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
- CLI invocation lifecycle diagnostics from the root-level trace.

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
    plans,
    review_continuation,
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


@dataclass
class _SessionTurns:
    """Implementation turns for one (state dir, session id) pair."""

    turns: list[tuple[str, bool]] = field(default_factory=list)  # (started_at, ok)

    def sorted_turns(self) -> list[tuple[str, bool]]:
        return sorted(self.turns)


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
            with contextlib.suppress(plans.PlanError, OSError):
                snapshot = plans.inspect_plan(plan_dir)
        materialization_keys = review_continuation.reconciled_materialization_keys(
            plan_state_dir / events.EVENTS_FILE_NAME,
            snapshot,
            since=cutoff,
        )
        unique_materialization_keys.update(
            (dir_key, key) for key in materialization_keys
        )

        for payload in _iter_turn_results(plan_state_dir):
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

        for record in _read_event_records(plan_state_dir / events.EVENTS_FILE_NAME):
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
