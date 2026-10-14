"""Execution timing section tests: replays, repairs, plan rows, and reads."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    events,
    execution_timing,
    metrics,
)
from tests.asl_metrics_support import (
    TimingSectionBase,
    _append_event,
    _iso,
    _make_state_dir,
    _timing_check,
    _timing_run,
    _timing_turn,
    _write_generated_review_repair_plan,
    _write_turn_result,
)
from tests.support import write_plan_with_ids


class ExecutionTimingReconciliationTests(TimingSectionBase):
    def _replayed_check_finish(
        self, state_dir: Path, *, duration_seconds: float
    ) -> None:
        """Restate the check written by `_timing_check` once more."""
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=5),
            name=events.VERIFICATION_CHECK_FINISHED_EVENT,
            payload={
                "run_id": "run-1",
                "turn_index": 1,
                "session": "1",
                "check_index": 0,
                "name": "ci",
                "kind": "command",
                "repo": "app",
                "status": "passed",
                "duration_seconds": duration_seconds,
            },
        )

    def _one_scoped_check(self) -> Path:
        state_dir = self._plan("plan-replay")
        _timing_run(state_dir, self.now, run_id="run-1", started=6, finished=1)
        _timing_check(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            check_index=0,
            name="ci",
            argv=["make", "ci"],
            repo="app",
            cwd="/src/app",
            started=6,
            finished=5,
        )
        return state_dir

    def test_a_replayed_check_record_inflates_neither_check_total(self) -> None:
        state_dir = self._one_scoped_check()
        self._replayed_check_finish(state_dir, duration_seconds=3600.0)

        checks = self._section()["verification_checks"]

        # One execution stated twice is one check in both places: the
        # cumulative work figure and the scoped ranking count the same thing.
        self.assertEqual(1, checks["checks"])
        self.assertEqual(1, checks["scoped_checks"])
        self.assertEqual(3600.0, checks["cumulative_seconds"])
        self.assertEqual([3600.0], [row["seconds"] for row in checks["scopes"]])
        self.assertEqual([1], [row["checks"] for row in checks["scopes"]])

    def test_a_check_restated_with_a_different_duration_stays_reconciled(self) -> None:
        state_dir = self._one_scoped_check()
        self._replayed_check_finish(state_dir, duration_seconds=7200.0)

        section = self._section()
        checks = section["verification_checks"]

        # The contradiction does not double the work nor split the totals; the
        # run simply stops backing a share.
        self.assertEqual(1, checks["checks"])
        self.assertEqual(1, checks["scoped_checks"])
        self.assertEqual(3600.0, checks["cumulative_seconds"])
        self.assertEqual([3600.0], [row["seconds"] for row in checks["scopes"]])
        self.assertEqual(1, section["coverage"]["runs_outside_share_basis"])
        self.assertEqual(
            1, section["coverage"]["included_runs_by_classification"]["inconsistent"]
        )

    def test_a_check_without_repository_or_directory_falls_back_to_the_plan(
        self,
    ) -> None:
        state_dir = self._plan("plan-unscoped")
        _timing_run(state_dir, self.now, run_id="run-1", started=4, finished=1)
        _timing_check(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            check_index=0,
            name="suite",
            started=4,
            finished=3,
        )

        checks = self._section()["verification_checks"]

        row = checks["scopes"][0]
        self.assertIsNone(row["repository"])
        self.assertIsNone(row["working_directory"])
        self.assertEqual(state_dir.name, row["plan_key"])
        # No argv was recorded, so the check name stands in and the count of
        # commandless checks says the ranking is partial.
        self.assertEqual("suite", row["command"])
        self.assertEqual(1, checks["checks_without_command"])

    def test_a_generated_repair_session_is_split_out_by_its_marker(self) -> None:
        plan_dir = self.tmp_path / "plan-repair"
        _write_generated_review_repair_plan(plan_dir)
        state_dir = _make_state_dir(self.state_root, plan_dir)
        _timing_run(state_dir, self.now, run_id="run-1", started=5, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started=5,
            finished=4,
        )
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=2,
            session_id="2.1",
            started=3,
            finished=2,
        )

        seconds = self._section()["phases"]["seconds"]

        self.assertEqual(3600.0, seconds["implementation"])
        self.assertEqual(3600.0, seconds["generated_repair"])

    def test_a_dotted_session_id_alone_is_not_a_generated_repair(self) -> None:
        plan_dir = self.tmp_path / "plan-dotted"
        write_plan_with_ids(plan_dir, (("1", "complete"), ("2.1", "pending")))
        state_dir = _make_state_dir(self.state_root, plan_dir)
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="2.1",
            started=3,
            finished=2,
        )

        seconds = self._section()["phases"]["seconds"]

        # An operator is free to hand-write a session called `2.1`; the shape of
        # the id is evidence of nothing.
        self.assertEqual(3600.0, seconds["implementation"])
        self.assertEqual(0.0, seconds["generated_repair"])

    def test_event_lineage_identifies_a_repair_without_reading_the_plan(self) -> None:
        state_dir = self._plan("plan-lineage")
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="2.1",
            started=3,
            finished=2,
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=3),
            name=events.REVIEW_CYCLE_EVENT,
            payload={
                "run_id": "run-1",
                "session": "2",
                "disposition": "findings",
                "repair_session_ids": ["2.1"],
            },
        )

        seconds = self._section()["phases"]["seconds"]

        self.assertEqual(3600.0, seconds["generated_repair"])
        self.assertEqual(0.0, seconds["implementation"])

    def test_a_plan_row_aggregates_the_invocations_it_was_resumed_through(
        self,
    ) -> None:
        state_dir = self._plan("plan-resumed")
        for index, (started, finished) in enumerate(((6, 5), (3, 1)), start=1):
            run_id = f"run-{index}"
            _timing_run(
                state_dir, self.now, run_id=run_id, started=started, finished=finished
            )
            _timing_turn(
                state_dir,
                self.now,
                run_id=run_id,
                turn_index=1,
                session_id=str(index),
                started=started,
                finished=finished,
            )

        section = self._section()

        self.assertEqual(1, len(section["plans"]))
        row = section["plans"][0]
        self.assertEqual(2, row["runs"])
        self.assertEqual(2, row["runs_with_known_wall"])
        self.assertEqual(3 * 3600.0, row["wall_seconds"])
        self.assertEqual(3 * 3600.0, row["seconds"]["implementation"])
        self.assertEqual(2, row["attempts"])

    def test_a_dry_run_is_excluded_from_the_measured_time(self) -> None:
        state_dir = self._plan("plan-dry")
        _timing_run(
            state_dir, self.now, run_id="run-1", started=3, finished=1, dry_run=True
        )
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started=3,
            finished=2,
        )

        section = self._section()

        self.assertEqual(1, section["coverage"]["runs_with_evidence"])
        self.assertEqual(0, section["coverage"]["runs_included"])
        self.assertEqual({"dry-run": 1}, section["coverage"]["exclusions"])
        self.assertEqual(0.0, section["phases"]["seconds"]["implementation"])

    def test_review_scope_fallback_reasons_are_reported(self) -> None:
        state_dir = self._plan("plan-scope")
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name=events.REVIEW_SCOPE_EVENT,
            payload={
                "run_id": "run-1",
                "session_id": "2",
                "scope": "full",
                "base_sha": None,
                "repositories": [],
                "fallback_reason": "no-recorded-base",
                "refreshed": False,
            },
        )
        _append_event(
            state_dir,
            created_at=_iso(self.now, hours_ago=2),
            name=events.REVIEW_SCOPE_EVENT,
            payload={
                "run_id": "run-1",
                "session_id": "3",
                "scope": "delta",
                "base_sha": "abc123",
                "repositories": ["root"],
                "refreshed": True,
            },
        )

        review_scope = self._section()["review_scope"]

        self.assertEqual(2, review_scope["selections"])
        self.assertEqual(1, review_scope["refreshed_selections"])
        self.assertEqual({"delta": 1, "full": 1}, review_scope["by_scope"])
        self.assertEqual({"no-recorded-base": 1}, review_scope["fallback_reasons"])
        self.assertEqual(1, review_scope["selections_without_fallback_reason"])
        self.assertIn("fallback reasons: no-recorded-base 1", self._text())

    def test_text_output_ranks_ten_plans_and_says_what_it_left_out(self) -> None:
        for index in range(metrics.TIMING_PLAN_LIMIT + 2):
            state_dir = self._plan(f"plan-{index:02d}")
            _timing_run(
                state_dir,
                self.now,
                run_id=f"run-{index}",
                started=index + 2,
                finished=1,
            )
            _timing_turn(
                state_dir,
                self.now,
                run_id=f"run-{index}",
                turn_index=1,
                session_id="1",
                started=index + 2,
                finished=1.5,
            )

        section = self._section()
        text = self._text()

        self.assertEqual(metrics.TIMING_PLAN_LIMIT + 2, len(section["plans"]))
        self.assertIn(
            f"top plans by duration ({metrics.TIMING_PLAN_LIMIT} of "
            f"{metrics.TIMING_PLAN_LIMIT + 2})",
            text,
        )
        # Longest first, and the JSON keeps every row the text dropped.
        durations = [row["wall_seconds"] for row in section["plans"]]
        self.assertEqual(sorted(durations, reverse=True), durations)

    def test_the_section_is_additive_to_the_existing_metrics_output(self) -> None:
        state_dir = self._plan("plan-compat")
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _write_turn_result(
            state_dir,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started_at=_iso(self.now, hours_ago=3),
            finished_at=_iso(self.now, hours_ago=2),
            duration_seconds=3600.0,
        )

        report = metrics.collect_metrics(
            state_dir=self.state_root, since_days=7, now=self.now
        )
        payload = report.to_dict()

        for section in (
            "turns",
            "first_pass",
            "pauses",
            "verification_repair",
            "review_cycles",
            "artifacts",
            "compaction",
            "assurance",
            "cli_invocations",
        ):
            self.assertIn(section, payload)
        self.assertIn("execution_timing", payload)
        # The pre-existing sections still see the same turn the timing section
        # measures: one read of the corpus feeds both.
        self.assertEqual(1, payload["turns"]["total"])
        self.assertEqual(1, payload["execution_timing"]["coverage"]["runs_included"])
        text = self._text()
        self.assertLess(text.index("Execution timing:"), text.index("First-pass"))
        self.assertIn("Supervisor audit:", text)

    def test_timing_reuses_the_corpus_reads_instead_of_adding_a_traversal(
        self,
    ) -> None:
        """Measuring must not cost what it measures.

        Equality of contents cannot catch a second traversal, because a second
        read of an unchanged corpus returns equal records. Identity can: the
        evidence handed to the collector has to be the very list object the
        windowed sections were already given, and each file has to be opened
        once for the whole report.
        """
        plans_written = ("plan-reuse-one", "plan-reuse-two")
        for index, name in enumerate(plans_written, start=1):
            state_dir = self._plan(name)
            run_id = f"run-{index}"
            _timing_run(state_dir, self.now, run_id=run_id, started=4, finished=1)
            for turn_index in (1, 2):
                _timing_turn(
                    state_dir,
                    self.now,
                    run_id=run_id,
                    turn_index=turn_index,
                    session_id=str(turn_index),
                    started=4 - turn_index,
                    finished=4 - turn_index - 0.5,
                )
                _write_turn_result(
                    state_dir,
                    run_id=run_id,
                    turn_index=turn_index,
                    session_id=str(turn_index),
                    started_at=_iso(self.now, hours_ago=4 - turn_index),
                    finished_at=_iso(self.now, hours_ago=4 - turn_index - 0.5),
                )

        event_reads: list[Path] = []
        result_reads: list[Path] = []
        read_events_by_plan: dict[str, list] = {}
        read_results_by_plan: dict[str, list] = {}
        collected_by_plan: dict[str, tuple[object, object]] = {}
        real_read_events = metrics._read_event_records
        real_read_result = metrics._read_result_payload
        real_iter_results = metrics._iter_turn_results
        real_collect = execution_timing.collect_execution_timing

        def read_events(path: Path):
            event_reads.append(path)
            records = real_read_events(path)
            read_events_by_plan[path.parent.name] = records
            return records

        def read_result(path: Path):
            result_reads.append(path)
            return real_read_result(path)

        def iter_results(state_dir: Path):
            payloads = real_iter_results(state_dir)
            read_results_by_plan[state_dir.name] = payloads
            return payloads

        def collect(evidence, **kwargs):
            for item in evidence:
                collected_by_plan[item.plan_key] = (
                    item.event_records,
                    item.turn_results,
                )
            return real_collect(evidence, **kwargs)

        with (
            mock.patch.object(metrics, "_read_event_records", read_events),
            mock.patch.object(metrics, "_read_result_payload", read_result),
            mock.patch.object(metrics, "_iter_turn_results", iter_results),
            mock.patch.object(execution_timing, "collect_execution_timing", collect),
        ):
            report = metrics.collect_metrics(
                state_dir=self.state_root, since_days=7, now=self.now
            )

        self.assertEqual(2, report.execution_timing["coverage"]["runs_included"])
        # This module reads each plan's events file once and each turn result
        # once, however many of its sections consume them. Other modules the
        # report calls into (materialization reconciliation, the compaction
        # dry-run) do their own reads; what is asserted here is that timing
        # added none.
        self.assertEqual(len(event_reads), len(set(event_reads)))
        self.assertEqual(len(plans_written), len(event_reads))
        self.assertEqual(len(result_reads), len(set(result_reads)))
        self.assertEqual(2 * len(plans_written), len(result_reads))
        # And the collector was handed those same reads rather than its own.
        self.assertEqual(sorted(read_events_by_plan), sorted(collected_by_plan))
        self.assertEqual(sorted(read_results_by_plan), sorted(collected_by_plan))
        for plan_key, (records, payloads) in collected_by_plan.items():
            self.assertIs(read_events_by_plan[plan_key], records)
            self.assertIs(read_results_by_plan[plan_key], payloads)


if __name__ == "__main__":
    unittest.main()
