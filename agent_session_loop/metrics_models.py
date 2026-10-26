"""Metric report records, shared vocabulary, and persisted-corpus readers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    cli_trace,
    execution_timing,
    session_overview,
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
        # Bytes, decoded line by line below: a stray non-UTF-8 byte then
        # spoils only its own line, the way a torn line does, not the file.
        data = events_path.read_bytes()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _read_result_payload(result_path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        # ValueError covers both a torn document and bytes that are not UTF-8.
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
