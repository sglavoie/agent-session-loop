"""CLI invocation trace metrics tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    cli_trace,
    metrics,
)
from tests.asl_metrics_support import (
    _iso,
    _trace_record,
)

ASL_TEST_PORTABLE = True


class CliInvocationMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.now = datetime.now(UTC)

    def _write(self, *, days_ago: int, lines: list[str]) -> Path:
        day = (self.now - timedelta(days=days_ago)).date()
        path = self.root / cli_trace.trace_file_name(day)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _collect(self, *, since_days: int = 7, exclude: str | None = None) -> dict:
        return metrics.collect_cli_invocations(
            self.root,
            cutoff=self.now - timedelta(days=since_days),
            exclude_invocation_id=exclude,
        )

    def _pair(
        self,
        invocation_id: str,
        *,
        hours_ago: float,
        outcome: str = cli_trace.OUTCOME_OK,
        exit_code: int | None = 0,
        duration_ms: float = 100.0,
        **shape: object,
    ) -> list[str]:
        start = _trace_record(
            event=cli_trace.EVENT_START,
            invocation_id=invocation_id,
            timestamp=_iso(self.now, hours_ago=hours_ago),
            **shape,
        )
        end = dict(start)
        end.update(
            {
                "event": cli_trace.EVENT_END,
                "timestamp": _iso(self.now, hours_ago=hours_ago - 0.01),
                "exit_code": exit_code,
                "outcome": outcome,
                "duration_ms": duration_ms,
                "exception_type": None,
            }
        )
        return [json.dumps(start, sort_keys=True), json.dumps(end, sort_keys=True)]

    def test_an_empty_trace_still_reports_root_and_retention(self) -> None:
        section = self._collect()

        self.assertEqual(str(self.root), section["trace_root"])
        self.assertEqual(cli_trace.RETENTION_DAYS, section["retention_days"])
        self.assertEqual(0, section["starts"])
        self.assertEqual(0, section["completions"])
        self.assertIsNone(section["durations_ms"]["median"])
        self.assertEqual([], section["recent_failures"])

    def test_lifecycle_outcomes_are_counted_separately(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair("ok", hours_ago=5, command_path=["status"]),
                *self._pair(
                    "bad",
                    hours_ago=4,
                    command_path=["run"],
                    outcome=cli_trace.OUTCOME_FAILED,
                    exit_code=1,
                ),
                *self._pair(
                    "usage",
                    hours_ago=3,
                    outcome=cli_trace.OUTCOME_USAGE,
                    exit_code=2,
                ),
                *self._pair(
                    "help",
                    hours_ago=2,
                    outcome=cli_trace.OUTCOME_HELP,
                    help_depth=cli_trace.HELP_ROOT,
                ),
                *self._pair(
                    "sigint",
                    hours_ago=1,
                    outcome=cli_trace.OUTCOME_INTERRUPTED,
                    exit_code=130,
                ),
                *self._pair(
                    "boom",
                    hours_ago=0.5,
                    outcome=cli_trace.OUTCOME_EXCEPTION,
                    exit_code=None,
                ),
            ],
        )

        section = self._collect()

        self.assertEqual(6, section["starts"])
        self.assertEqual(6, section["completions"])
        self.assertEqual(1, section["successes"])
        self.assertEqual(1, section["nonzero_exits"])
        self.assertEqual(1, section["usage_errors"])
        self.assertEqual(1, section["help_calls"])
        self.assertEqual(1, section["interruptions"])
        self.assertEqual(1, section["exceptions"])
        self.assertEqual(0, section["unmatched_starts"])

    def test_grouped_counts_cover_every_documented_dimension(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair(
                    "a",
                    hours_ago=3,
                    command_path=["workbench", "serve"],
                    harness="claude",
                    origin=cli_trace.ORIGIN_WORKBENCH,
                ),
                *self._pair(
                    "b",
                    hours_ago=2,
                    command_path=["run"],
                    origin=cli_trace.ORIGIN_AUTOPILOT,
                    outcome=cli_trace.OUTCOME_FAILED,
                    exit_code=3,
                ),
                *self._pair(
                    "c",
                    hours_ago=1,
                    outcome=cli_trace.OUTCOME_HELP,
                    help_depth=cli_trace.HELP_LEAF,
                ),
            ],
        )

        section = self._collect()

        self.assertEqual(
            {"(root)": 1, "run": 1, "workbench serve": 1}, section["by_command"]
        )
        self.assertEqual({cli_trace.HELP_LEAF: 1}, section["by_help_depth"])
        self.assertEqual({"0": 2, "3": 1}, section["by_exit_code"])
        self.assertEqual({"claude": 1, "unknown": 2}, section["by_harness"])
        self.assertEqual(
            {
                cli_trace.ORIGIN_AUTOPILOT: 1,
                cli_trace.ORIGIN_DIRECT: 1,
                cli_trace.ORIGIN_WORKBENCH: 1,
            },
            section["by_origin"],
        )

    def test_a_null_exit_code_is_grouped_without_being_dropped(self) -> None:
        self._write(
            days_ago=0,
            lines=self._pair(
                "boom",
                hours_ago=1,
                outcome=cli_trace.OUTCOME_EXCEPTION,
                exit_code=None,
            ),
        )

        self.assertEqual({"none": 1}, self._collect()["by_exit_code"])

    def test_durations_summarize_only_completed_invocations(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair("a", hours_ago=4, duration_ms=10.0),
                *self._pair("b", hours_ago=3, duration_ms=30.0),
                *self._pair("c", hours_ago=2, duration_ms=90.0),
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_START,
                        invocation_id="killed",
                        timestamp=_iso(self.now, hours_ago=1),
                    ),
                    sort_keys=True,
                ),
            ],
        )

        durations = self._collect()["durations_ms"]

        self.assertEqual(3, durations["count"])
        self.assertEqual(30.0, durations["median"])
        self.assertEqual(90.0, durations["p95"])
        self.assertEqual(90.0, durations["max"])
        self.assertEqual(130.0, durations["total"])

    def test_a_killed_process_is_reported_as_an_unmatched_start(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_START,
                        invocation_id="killed",
                        timestamp=_iso(self.now, hours_ago=1),
                        command_path=["run"],
                    ),
                    sort_keys=True,
                )
            ],
        )

        section = self._collect()

        self.assertEqual(1, section["starts"])
        self.assertEqual(0, section["completions"])
        self.assertEqual(1, section["unmatched_starts"])
        self.assertEqual({"run": 1}, section["by_command"])

    def test_an_end_whose_start_was_pruned_is_still_summarized(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_END,
                        invocation_id="orphan",
                        timestamp=_iso(self.now, hours_ago=1),
                        command_path=["gc"],
                        exit_code=1,
                        outcome=cli_trace.OUTCOME_FAILED,
                        duration_ms=25.0,
                        exception_type=None,
                    ),
                    sort_keys=True,
                )
            ],
        )

        section = self._collect()

        self.assertEqual(0, section["starts"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(1, section["nonzero_exits"])
        self.assertEqual({"gc": 1}, section["by_command"])

    def test_a_pair_split_across_midnight_is_counted_once(self) -> None:
        yesterday = _trace_record(
            event=cli_trace.EVENT_START,
            invocation_id="overnight",
            timestamp=_iso(self.now, hours_ago=25),
            command_path=["run"],
        )
        today = dict(yesterday)
        today.update(
            {
                "event": cli_trace.EVENT_END,
                "timestamp": _iso(self.now, hours_ago=23),
                "exit_code": 0,
                "outcome": cli_trace.OUTCOME_OK,
                "duration_ms": 7200000.0,
                "exception_type": None,
            }
        )
        self._write(days_ago=1, lines=[json.dumps(yesterday, sort_keys=True)])
        self._write(days_ago=0, lines=[json.dumps(today, sort_keys=True)])

        section = self._collect()

        self.assertEqual(1, section["starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual({"run": 1}, section["by_command"])

    def test_duplicate_records_never_inflate_the_totals(self) -> None:
        pair = self._pair("dup", hours_ago=1, command_path=["status"])
        self._write(days_ago=0, lines=[*pair, *pair])

        section = self._collect()

        self.assertEqual(4, section["records_read"])
        self.assertEqual(1, section["starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(1, section["successes"])
        self.assertEqual({"status": 1}, section["by_command"])

    def test_malformed_and_unusable_records_degrade_the_sample_only(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                "not json at all",
                "[]",
                json.dumps({"event": "start", "timestamp": "now"}),
                json.dumps({"event": "sideways", "invocation_id": "x"}),
                json.dumps(
                    {
                        "event": cli_trace.EVENT_START,
                        "invocation_id": "undated",
                        "timestamp": "not-a-timestamp",
                    }
                ),
                *self._pair("good", hours_ago=1, command_path=["status"]),
            ],
        )

        section = self._collect()

        # Seven non-blank lines: one malformed, one non-object, three unusable
        # dictionaries, and the two halves of the only usable invocation.
        self.assertEqual(7, section["records_read"])
        self.assertEqual(5, section["records_ignored"])
        self.assertEqual(1, section["starts"])
        self.assertEqual(1, section["completions"])
        self.assertEqual(1, section["successes"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual({"status": 1}, section["by_command"])

    def test_an_invocation_is_windowed_by_its_start(self) -> None:
        self._write(days_ago=0, lines=self._pair("recent", hours_ago=1))
        self._write(days_ago=3, lines=self._pair("older", hours_ago=72))

        wide = self._collect(since_days=7)
        narrow = self._collect(since_days=1)

        self.assertEqual(2, wide["starts"])
        self.assertEqual(1, narrow["starts"])
        self.assertLessEqual(narrow["completions"], wide["completions"])

    def test_recent_failures_are_bounded_newest_first_and_content_free(self) -> None:
        lines: list[str] = []
        for index in range(metrics.RECENT_FAILURE_LIMIT + 4):
            lines.extend(
                self._pair(
                    f"fail-{index:02d}",
                    hours_ago=float(index + 1),
                    command_path=["run"],
                    options=["--json"],
                    origin=cli_trace.ORIGIN_AUTOPILOT,
                    outcome=cli_trace.OUTCOME_FAILED,
                    exit_code=1,
                )
            )
        lines.extend(self._pair("fine", hours_ago=0.5, command_path=["status"]))
        self._write(days_ago=0, lines=lines)

        failures = self._collect()["recent_failures"]

        self.assertEqual(metrics.RECENT_FAILURE_LIMIT, len(failures))
        self.assertEqual("fail-00", failures[0]["invocation_id"])
        self.assertNotIn("fine", {entry["invocation_id"] for entry in failures})
        for entry in failures:
            self.assertEqual(set(metrics.RECENT_FAILURE_FIELDS), set(entry))
            self.assertEqual("run", entry["command"])
            self.assertEqual(["--json"], entry["options"])
            self.assertEqual(cli_trace.ORIGIN_AUTOPILOT, entry["origin"])

    def test_every_failing_outcome_reaches_recent_failures(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                *self._pair("ok", hours_ago=5),
                *self._pair(
                    "bad", hours_ago=4, outcome=cli_trace.OUTCOME_FAILED, exit_code=1
                ),
                *self._pair(
                    "usage", hours_ago=3, outcome=cli_trace.OUTCOME_USAGE, exit_code=2
                ),
                *self._pair(
                    "sigint",
                    hours_ago=2,
                    outcome=cli_trace.OUTCOME_INTERRUPTED,
                    exit_code=130,
                ),
                *self._pair(
                    "boom",
                    hours_ago=1,
                    outcome=cli_trace.OUTCOME_EXCEPTION,
                    exit_code=None,
                ),
                *self._pair(
                    "help", hours_ago=0.5, outcome=cli_trace.OUTCOME_HELP, exit_code=0
                ),
            ],
        )

        failures = self._collect()["recent_failures"]
        reported = {entry["invocation_id"] for entry in failures}

        self.assertEqual({"bad", "usage", "sigint", "boom"}, reported)

    def test_the_excluded_invocation_is_dropped_entirely(self) -> None:
        self._write(
            days_ago=0,
            lines=[
                json.dumps(
                    _trace_record(
                        event=cli_trace.EVENT_START,
                        invocation_id="self",
                        timestamp=_iso(self.now, hours_ago=0.1),
                        command_path=["metrics"],
                    ),
                    sort_keys=True,
                ),
                *self._pair("other", hours_ago=1, command_path=["status"]),
            ],
        )

        section = self._collect(exclude="self")

        self.assertEqual(1, section["starts"])
        self.assertEqual(0, section["unmatched_starts"])
        self.assertEqual({"status": 1}, section["by_command"])
        # Two records for "other"; the report's own start is not read.
        self.assertEqual(2, section["records_read"])
        self.assertEqual(0, section["records_ignored"])


if __name__ == "__main__":
    unittest.main()
