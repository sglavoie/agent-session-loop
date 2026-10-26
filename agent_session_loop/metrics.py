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
accounting rules the numbers obey."""

from __future__ import annotations

import argparse
import json
import sys

from . import (
    metrics_collection,
    metrics_models,
    metrics_render,
    responses,
)
from .cli_defaults import program_name
from .metrics_collection import collect_metrics
from .metrics_invocations import (
    _bump,
    _command_label,
    _percentile,
    _recent_failure_entry,
    _string_or_none,
    _TracedInvocation,
    collect_cli_invocations,
)
from .metrics_models import (
    _COMPLETE_STATUS,
    _IMPLEMENTATION_KIND,
    _RUN_LEVEL_TIMING_PHASES,
    _VERIFY_FAILED_REASON,
    DEFAULT_SINCE_DAYS,
    FAILING_OUTCOMES,
    RECENT_FAILURE_FIELDS,
    RECENT_FAILURE_LIMIT,
    ROOT_COMMAND_LABEL,
    TIMING_CHECK_LIMIT,
    TIMING_PHASE_GENERATED_REPAIR,
    TIMING_PHASE_IMPLEMENTATION,
    TIMING_PHASES,
    TIMING_PLAN_LIMIT,
    UNKNOWN_COMMAND_LABEL,
    UNKNOWN_LABEL,
    MetricsReport,
    _CheckRun,
    _CheckScope,
    _directory_sizes,
    _iter_turn_results,
    _parse_timestamp,
    _plan_dir_for_state_dir,
    _PlanTimingEvidence,
    _read_event_records,
    _read_result_payload,
    _SessionTurns,
)
from .metrics_render import (
    _format_counts,
    _format_duration_ms,
    _print_assurance_section,
    _print_cli_invocations_section,
    _print_report,
)
from .metrics_render_timing import (
    _format_bytes,
    _format_hours,
    _format_rate,
    _print_execution_timing_section,
)
from .metrics_timing import (
    _check_scope_rows,
    _plan_rows,
    _review_scope_rows,
    collect_execution_timing,
)
from .metrics_timing_evidence import (
    _attempt_order_key,
    _attempt_phase,
    _check_scope,
    _check_slot,
    _hashable,
    _ingest_timing_evidence,
    _repair_session_ids,
    _repeated_attempts,
    _run_phase_seconds,
)


class _InvalidSinceDays(ValueError):
    """A ``--since-days`` value the collector would refuse."""


def metrics_from_args(args: argparse.Namespace) -> int:
    """CLI handler for ``asl metrics``."""
    since_days = getattr(args, "since_days", metrics_models.DEFAULT_SINCE_DAYS)
    try:
        if since_days <= 0:
            raise _InvalidSinceDays(
                f"--since-days must be a positive integer: {since_days}"
            )
        report = metrics_collection.collect_metrics(
            state_dir=getattr(args, "state_dir", None),
            since_days=since_days,
        )
    except (OSError, ValueError) as error:
        if getattr(args, "json", False):
            responses.print_json(
                responses.error_payload(
                    "metrics",
                    reason=(
                        "invalid-since-days"
                        if isinstance(error, _InvalidSinceDays)
                        else "os-error"
                        if isinstance(error, OSError)
                        else "metrics-error"
                    ),
                    error=str(error),
                    paused=False,
                )
            )
        else:
            print(f"{program_name()} metrics: {error}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), sort_keys=True))
    else:
        metrics_render._print_report(report)
    return 0


__all__ = [
    "DEFAULT_SINCE_DAYS",
    "FAILING_OUTCOMES",
    "MetricsReport",
    "RECENT_FAILURE_FIELDS",
    "RECENT_FAILURE_LIMIT",
    "ROOT_COMMAND_LABEL",
    "TIMING_CHECK_LIMIT",
    "TIMING_PHASES",
    "TIMING_PHASE_GENERATED_REPAIR",
    "TIMING_PHASE_IMPLEMENTATION",
    "TIMING_PLAN_LIMIT",
    "UNKNOWN_COMMAND_LABEL",
    "UNKNOWN_LABEL",
    "_COMPLETE_STATUS",
    "_CheckRun",
    "_CheckScope",
    "_IMPLEMENTATION_KIND",
    "_PlanTimingEvidence",
    "_RUN_LEVEL_TIMING_PHASES",
    "_SessionTurns",
    "_TracedInvocation",
    "_VERIFY_FAILED_REASON",
    "_attempt_order_key",
    "_attempt_phase",
    "_bump",
    "_check_scope",
    "_check_scope_rows",
    "_check_slot",
    "_command_label",
    "_directory_sizes",
    "_format_bytes",
    "_format_counts",
    "_format_duration_ms",
    "_format_hours",
    "_format_rate",
    "_hashable",
    "_ingest_timing_evidence",
    "_iter_turn_results",
    "_parse_timestamp",
    "_percentile",
    "_plan_dir_for_state_dir",
    "_plan_rows",
    "_print_assurance_section",
    "_print_cli_invocations_section",
    "_print_execution_timing_section",
    "_print_report",
    "_read_event_records",
    "_read_result_payload",
    "_recent_failure_entry",
    "_repair_session_ids",
    "_repeated_attempts",
    "_review_scope_rows",
    "_run_phase_seconds",
    "_string_or_none",
    "collect_cli_invocations",
    "collect_execution_timing",
    "collect_metrics",
    "metrics_from_args",
]
