"""Test asl process tree evidence."""

from __future__ import annotations

import unittest
from unittest import mock

from agent_session_loop import process_tree
from tests import support_asl_process_tree as fixtures


class AccumulateCpuSecondsTests(unittest.TestCase):
    def test_sums_cpu_across_tree(self) -> None:
        tree = [
            {"pid": 10, "cpu_seconds": 60.0},
            {"pid": 11, "cpu_seconds": 0.03},
            {"pid": 12, "cpu_seconds": 1.5},
        ]
        total, tracked = process_tree.accumulate_cpu_seconds(tree)
        self.assertAlmostEqual(61.53, total)
        self.assertEqual(3, len(tracked))

    def test_empty_tree_is_zero(self) -> None:
        self.assertEqual((0.0, {}), process_tree.accumulate_cpu_seconds([]))

    def test_missing_cpu_field_counts_as_zero(self) -> None:
        total, _tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 10}, {"pid": 11, "cpu_seconds": 2.0}]
        )
        self.assertEqual(2.0, total)

    def test_row_without_a_pid_is_skipped(self) -> None:
        total, tracked = process_tree.accumulate_cpu_seconds(
            [{"cpu_seconds": 9.0}, {"pid": "x", "cpu_seconds": 9.0}]
        )
        self.assertEqual(0.0, total)
        self.assertEqual({}, tracked)

    def test_departed_descendant_keeps_its_cpu(self) -> None:
        first = [
            {"pid": 10, "cpu_seconds": 1.0, "birth": "a"},
            {"pid": 11, "cpu_seconds": 8.0, "birth": "b"},
        ]
        _total, tracked = process_tree.accumulate_cpu_seconds(first)

        total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 10, "cpu_seconds": 1.2, "birth": "a"}], tracked
        )

        self.assertAlmostEqual(9.2, total)
        self.assertEqual(2, len(tracked))

    def test_new_descendant_adds_its_cpu(self) -> None:
        _total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 10, "cpu_seconds": 1.0, "birth": "a"}]
        )

        total, tracked = process_tree.accumulate_cpu_seconds(
            [
                {"pid": 10, "cpu_seconds": 1.0, "birth": "a"},
                {"pid": 12, "cpu_seconds": 4.0, "birth": "c"},
            ],
            tracked,
        )

        self.assertAlmostEqual(5.0, total)

    def test_pid_reuse_with_a_new_birth_is_new_work(self) -> None:
        _total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 11, "cpu_seconds": 8.0, "birth": "Sun Jul 26 17:57:01 2026"}]
        )

        total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 11, "cpu_seconds": 0.5, "birth": "Sun Jul 26 18:03:44 2026"}],
            tracked,
        )

        self.assertAlmostEqual(8.5, total)
        self.assertEqual(2, len(tracked))

    def test_absent_birth_is_one_stable_identity(self) -> None:
        _total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 11, "cpu_seconds": 8.0}]
        )

        total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 11, "cpu_seconds": 9.0, "birth": None}], tracked
        )

        self.assertAlmostEqual(9.0, total)
        self.assertEqual(1, len(tracked))

    def test_lower_re_reading_keeps_the_high_water_mark(self) -> None:
        _total, tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 10, "cpu_seconds": 6.0, "birth": "a"}]
        )

        total, _tracked = process_tree.accumulate_cpu_seconds(
            [{"pid": 10, "cpu_seconds": 2.0, "birth": "a"}], tracked
        )

        self.assertAlmostEqual(6.0, total)

    def test_total_never_decreases_across_a_churning_sequence(self) -> None:
        """Regression anchor for ``resources.cpu-delta-monotonicity`` (ASI-58).

        Replays the grow/shrink/grow shape of a harness running a test suite
        that constantly spawns and reaps short-lived workers.
        """
        samples = [
            [
                {"pid": 10, "cpu_seconds": 1.0, "birth": "root"},
                {"pid": 11, "cpu_seconds": 5.0, "birth": "w1"},
                {"pid": 12, "cpu_seconds": 4.0, "birth": "w2"},
            ],
            [
                {"pid": 10, "cpu_seconds": 1.1, "birth": "root"},
                {"pid": 12, "cpu_seconds": 4.5, "birth": "w2"},
            ],
            [{"pid": 10, "cpu_seconds": 1.2, "birth": "root"}],
            [
                {"pid": 10, "cpu_seconds": 1.3, "birth": "root"},
                {"pid": 11, "cpu_seconds": 0.4, "birth": "w3"},
                {"pid": 12, "cpu_seconds": 0.2, "birth": "w4"},
            ],
        ]

        tracked: dict[tuple[int, str | None], float] = {}
        totals: list[float] = []
        for sample in samples:
            total, tracked = process_tree.accumulate_cpu_seconds(sample, tracked)
            totals.append(total)

        self.assertEqual(sorted(totals), totals)
        self.assertAlmostEqual(10.0, totals[0])
        self.assertAlmostEqual(11.4, totals[3])


class CollectDescendantEvidenceTests(fixtures._UrgentBase):
    # A worker group (4242) with an in-group child, and a grandchild that
    # called setsid and therefore left the group: the one a killpg will miss.
    TABLE = (
        "    1     0    1 /sbin/launchd\n"
        f" {fixtures._SELF_PID}     1 {fixtures._SELF_PGID} python3 -m unittest\n"
        f"  4242     1 4242 {fixtures._BIRTH} codex exec\n"
        f"  4243  4242 4242 {fixtures._BIRTH} /bin/zsh -c build\n"
        f"  4244  4243 4244 {fixtures._BIRTH} escaped daemon\n"
        f"  5555     1 5555 {fixtures._BIRTH} unrelated\n"
    )

    def _collect(self, stdout: str = TABLE, **kwargs):
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))
        kwargs.setdefault("environ", {})
        evidence = process_tree.collect_descendant_evidence(
            4242, run_command=run_command, **kwargs
        )
        return evidence, run_command

    def test_descendants_are_recorded_with_the_escapee_named(self) -> None:
        evidence, _ = self._collect()

        self.assertTrue(evidence["collected"])
        self.assertEqual(4242, evidence["root_pgid"])
        self.assertEqual([4243, 4244], [entry["pid"] for entry in evidence["entries"]])
        # Only the process the group kill cannot reach is an escapee.
        self.assertEqual([4244], evidence["escaped_pids"])
        self.assertIsNone(evidence["sample_error"])

    def test_entries_carry_the_birth_that_authenticates_them_later(self) -> None:
        evidence, _ = self._collect()

        self.assertEqual(
            [fixtures._BIRTH, fixtures._BIRTH],
            [e["birth"] for e in evidence["entries"]],
        )

    def test_the_urgent_bound_is_shorter_than_the_ordinary_sampling_bound(
        self,
    ) -> None:
        _, run_command = self._collect()

        timeout = run_command.call_args.kwargs["timeout"]
        self.assertEqual(process_tree.URGENT_SAMPLE_TIMEOUT_SECONDS, timeout)
        self.assertLess(timeout, process_tree.SAMPLE_TIMEOUT_SECONDS)

    def test_the_runner_and_pid_one_are_never_recorded_as_descendants(self) -> None:
        stdout = (
            "    1     0    1 /sbin/launchd\n"
            f"  4242     1 4242 {fixtures._BIRTH} codex exec\n"
            f" {fixtures._SELF_PID}  4242 {fixtures._SELF_PGID} "
            f"{fixtures._BIRTH} python3 -m unittest\n"
        )
        evidence, _ = self._collect(stdout)

        self.assertEqual([], evidence["entries"])
        self.assertEqual([], evidence["escaped_pids"])

    def test_an_absent_root_is_a_complete_answer_not_a_failed_one(self) -> None:
        stdout = f"  5555     1 5555 {fixtures._BIRTH} unrelated\n"
        evidence, _ = self._collect(stdout)

        self.assertTrue(evidence["collected"])
        self.assertTrue(evidence["root_absent"])
        self.assertEqual([], evidence["entries"])

    def test_a_failed_sample_reports_uncollected_rather_than_empty(self) -> None:
        run_command = mock.Mock(return_value=fixtures._ps_result("", returncode=1))

        evidence = process_tree.collect_descendant_evidence(
            4242, run_command=run_command, environ={}
        )

        self.assertFalse(evidence["collected"])
        self.assertEqual("process table sample failed", evidence["sample_error"])
        self.assertEqual([], evidence["entries"])

    def test_a_spawn_failure_reports_uncollected_rather_than_raising(self) -> None:
        run_command = mock.Mock(side_effect=OSError("ps is gone"))

        evidence = process_tree.collect_descendant_evidence(
            4242, run_command=run_command, environ={}
        )

        self.assertFalse(evidence["collected"])
        self.assertEqual([], evidence["entries"])

    def test_the_process_sweep_opt_out_withholds_the_evidence(self) -> None:
        run_command = mock.Mock(return_value=fixtures._ps_result(self.TABLE))

        evidence = process_tree.collect_descendant_evidence(
            4242,
            run_command=run_command,
            environ={process_tree.SWEEP_DISABLED_ENV_VAR: "1"},
        )

        self.assertEqual("process_sweep_opt_out", evidence["disabled"])
        self.assertFalse(evidence["collected"])
        run_command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
