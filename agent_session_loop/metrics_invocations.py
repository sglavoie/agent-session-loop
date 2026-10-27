"""Aggregate CLI invocation traces into windowed lifecycle metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (
    cli_trace,
    metrics_models,
)


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
    return " ".join(names) if names else metrics_models.ROOT_COMMAND_LABEL


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
    return {name: entry[name] for name in metrics_models.RECENT_FAILURE_FIELDS}


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
    ``exclude_invocation_id``'s records are dropped entirely, from
    ``records_read`` as well as from every count.
    """
    corpus = cli_trace.read_corpus(trace_root)
    records = corpus.records
    invocations: dict[str, _TracedInvocation] = {}
    ignored = corpus.malformed_lines
    excluded = 0
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
            excluded += 1
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
        started_at = metrics_models._parse_timestamp((anchor or {}).get("timestamp"))
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
        _bump(
            by_harness,
            _string_or_none(facts.get("harness")) or metrics_models.UNKNOWN_LABEL,
        )
        _bump(
            by_origin,
            _string_or_none(facts.get("origin")) or metrics_models.UNKNOWN_LABEL,
        )
        help_depth = _string_or_none(facts.get("help_depth"))
        if help_depth is not None:
            _bump(by_help_depth, help_depth)

        end = tracked.end
        if end is None:
            if tracked.start is not None:
                unmatched_starts += 1
            continue
        completions += 1
        outcome = _string_or_none(end.get("outcome")) or metrics_models.UNKNOWN_LABEL
        _bump(outcome_counts, outcome)
        exit_code = end.get("exit_code")
        _bump(by_exit_code, str(exit_code) if isinstance(exit_code, int) else "none")
        duration = end.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            durations.append(float(duration))
        if outcome in metrics_models.FAILING_OUTCOMES:
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
        # The report's own records are not part of what it reads: on an empty
        # corpus it would otherwise report one record read and nothing else.
        "records_read": len(records) + corpus.malformed_lines - excluded,
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
        "recent_failures": [
            entry for _, _, entry in failures[: metrics_models.RECENT_FAILURE_LIMIT]
        ],
    }
