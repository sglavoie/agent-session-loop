"""Execution timing section tests: phases, repeats, and check scopes."""

from __future__ import annotations

import unittest

from agent_session_loop import assurance_projection as assurance_projection
from agent_session_loop import (
    metrics,
)
from tests.asl_metrics_support import (
    TimingSectionBase,
    _make_state_dir,
    _timing_check,
    _timing_run,
    _timing_turn,
)


class ExecutionTimingMetricsSectionTests(TimingSectionBase):
    def test_an_empty_corpus_reports_the_section_with_nothing_measured(self) -> None:
        section = self._section()

        self.assertEqual(0, section["coverage"]["plans_with_evidence"])
        self.assertEqual(0, section["coverage"]["runs_with_evidence"])
        self.assertEqual(0, section["coverage"]["runs_included"])
        self.assertEqual({}, section["coverage"]["exclusions"])
        # Absent evidence reports no shares at all; zeroes would read as a
        # measurement that no time went anywhere.
        self.assertIsNone(section["phases"]["shares"])
        self.assertEqual([], section["plans"])
        self.assertEqual([], section["verification_checks"]["scopes"])
        self.assertIn("no run finished inside the window", self._text())

    def test_a_missing_state_root_still_reports_an_empty_section(self) -> None:
        report = metrics.collect_metrics(
            state_dir=self.state_root / "absent", since_days=7, now=self.now
        )

        self.assertEqual(0, report.execution_timing["coverage"]["runs_included"])

    def test_a_plan_directory_that_no_longer_exists_is_still_reported(self) -> None:
        plan_dir = self.tmp_path / "plan-gone"
        state_dir = _make_state_dir(self.state_root, plan_dir)
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started=3,
            finished=2,
        )
        plan_dir.rmdir()

        section = self._section()

        self.assertEqual(1, section["coverage"]["runs_included"])
        row = section["plans"][0]
        # The plan is named by the path the state directory still records; only
        # the marker evidence inside the plan is unavailable.
        self.assertEqual(str(plan_dir), row["plan_dir"])
        self.assertEqual(1, row["runs"])
        self.assertEqual(3600.0, row["seconds"]["implementation"])

    def test_a_turn_of_unknown_kind_is_unclassified_rather_than_implementation(
        self,
    ) -> None:
        state_dir = self._plan("plan-historical")
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started=3,
            finished=2,
            session_kind=None,
        )

        section = self._section()

        seconds = section["phases"]["seconds"]
        self.assertEqual(3600.0, seconds["turn_unclassified"])
        self.assertEqual(0.0, seconds["implementation"])
        self.assertEqual(0.0, seconds["generated_repair"])
        # A turn of unknown kind is a gap, so the run is not a share basis, but
        # its measured seconds are still reported.
        self.assertIsNone(section["phases"]["shares"])
        self.assertEqual(0, section["phases"]["share_basis_runs"])
        self.assertEqual(1, section["coverage"]["runs_outside_share_basis"])
        self.assertIn("shares: n/a", self._text())

    def test_a_complete_run_reports_shares_over_its_own_wall_clock(self) -> None:
        state_dir = self._plan("plan-complete")
        _timing_run(state_dir, self.now, run_id="run-1", started=4, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started=4,
            finished=2,
        )

        section = self._section()

        shares = section["phases"]["shares"]
        self.assertIsNotNone(shares)
        assert shares is not None
        self.assertEqual(1, section["phases"]["share_basis_runs"])
        self.assertAlmostEqual(2 / 3, shares["implementation"], places=3)
        self.assertAlmostEqual(1 / 3, shares["unattributed"], places=3)

    def test_an_attempt_before_the_window_makes_the_in_window_one_a_repeat(
        self,
    ) -> None:
        state_dir = self._plan("plan-retry")
        _timing_run(
            state_dir, self.now, run_id="run-old", started=24 * 9, finished=24 * 9 - 2
        )
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-old",
            turn_index=1,
            session_id="1",
            started=24 * 9,
            finished=24 * 9 - 1,
        )
        _timing_run(state_dir, self.now, run_id="run-new", started=3, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-new",
            turn_index=1,
            session_id="1",
            started=3,
            finished=2,
        )

        section = self._section()

        coverage = section["coverage"]
        self.assertEqual(2, coverage["runs_with_evidence"])
        self.assertEqual(1, coverage["runs_included"])
        self.assertEqual({"outside-window": 1}, coverage["exclusions"])
        repeated = section["repeated_attempts"]
        # The first attempt is outside the window; recognizing the second one as
        # a repeat is exactly what reading the unfiltered history buys.
        self.assertEqual(1, repeated["additional_attempts"])
        self.assertEqual(1, repeated["sessions_with_additional_attempts"])
        self.assertEqual(3600.0, repeated["additional_attempt_seconds"])
        self.assertEqual(
            {"implementation": 1}, repeated["additional_attempts_by_phase"]
        )
        self.assertEqual(1, section["plans"][0]["additional_attempts"])
        self.assertIn(
            "some of it produced the work that landed",
            self._text(),
        )

    def test_a_single_attempt_is_never_counted_as_a_repeat(self) -> None:
        state_dir = self._plan("plan-single")
        _timing_run(state_dir, self.now, run_id="run-1", started=3, finished=1)
        _timing_turn(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=1,
            session_id="1",
            started=3,
            finished=2,
        )

        repeated = self._section()["repeated_attempts"]

        self.assertEqual(1, repeated["sessions_tracked"])
        self.assertEqual(0, repeated["additional_attempts"])

    def test_checks_named_alike_in_different_repositories_do_not_merge(self) -> None:
        state_dir = self._plan("plan-checks")
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
        _timing_check(
            state_dir,
            self.now,
            run_id="run-1",
            turn_index=2,
            check_index=0,
            name="ci",
            argv=["make", "ci"],
            repo="lib",
            cwd="/src/lib",
            started=4,
            finished=2,
        )

        checks = self._section()["verification_checks"]

        self.assertEqual(2, checks["checks"])
        self.assertEqual(2, checks["scoped_checks"])
        self.assertEqual(0, checks["checks_without_command"])
        scopes = checks["scopes"]
        self.assertEqual(2, len(scopes))
        # Ranked by cumulative work, so the two-hour `lib` run leads.
        self.assertEqual(["lib", "app"], [row["repository"] for row in scopes])
        self.assertEqual(["make ci", "make ci"], [row["command"] for row in scopes])
        self.assertEqual(
            ["/src/lib", "/src/app"], [row["working_directory"] for row in scopes]
        )
        self.assertEqual([7200.0, 3600.0], [row["seconds"] for row in scopes])
        self.assertIsNone(scopes[0]["plan_key"])
        # Cumulative work sits beside the phase elapsed seconds, never inside.
        self.assertEqual(10800.0, checks["cumulative_seconds"])
        self.assertEqual(10800.0, checks["phase_seconds"])
        self.assertIn("cumulative check work", self._text())


if __name__ == "__main__":
    unittest.main()
