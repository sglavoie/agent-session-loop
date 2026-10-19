"""Test asl process tree sweep."""

from __future__ import annotations

import os
import signal
import unittest
from unittest import mock

from agent_session_loop import process_tree
from tests import support_asl_process_tree as fixtures


class SweepSurvivorsTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (
            ("getpid", fixtures._SELF_PID),
            ("getpgrp", fixtures._SELF_PGID),
        ):
            patcher = mock.patch.object(process_tree.os, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _sweep(**kwargs):
        kwargs.setdefault("prior_snapshot", ())
        return process_tree.sweep_survivors(settle_seconds=0.0, **kwargs)

    def test_group_members_killed_and_reported(self) -> None:
        # The root is reaped before the sweep, so it is absent from the table.
        stdout = (
            "    1     0    1 /sbin/launchd\n"
            f" {fixtures._SELF_PID}     1 {fixtures._SELF_PGID} python3 -m unittest\n"
            "  4243     1 4242 leftover child\n"
            "  4244  4243 4242 leftover grandchild\n"
            "  5555     1 5555 unrelated\n"
        )
        after = (
            "    1     0    1 /sbin/launchd\n"
            f" {fixtures._SELF_PID}     1 {fixtures._SELF_PGID} python3 -m unittest\n"
            "  5555     1 5555 unrelated\n"
        )
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result(after),
            ]
        )
        kills: list[tuple[int, int]] = []

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=lambda pid, sig: kills.append((pid, sig)),
            run_command=run_command,
        )

        self.assertEqual([(4243, signal.SIGKILL), (4244, signal.SIGKILL)], kills)
        self.assertEqual([4243, 4244], report["killed"])
        self.assertEqual([], report["kill_failed"])
        self.assertEqual([], report["survivors_after"])
        self.assertEqual(2, run_command.call_count)
        enumeration = report["enumeration"]
        self.assertEqual(4242, enumeration["expected_pgid"])
        self.assertEqual(5, enumeration["table_size"])
        self.assertEqual([4243, 4244], enumeration["pgid_members"])
        self.assertEqual([], enumeration["already_gone"])
        self.assertTrue(enumeration["resampled"])

    def test_self_and_pid_one_are_never_signalled(self) -> None:
        stdout = (
            "    1     0 4242 /sbin/launchd\n"
            f" {fixtures._SELF_PID}     1 4242 python3 -m unittest\n"
            "  4243     1 4242 leftover child\n"
        )
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result(stdout),
            ]
        )
        kills: list[int] = []

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=lambda pid, sig: kills.append(pid),
            run_command=run_command,
        )

        self.assertEqual([4243], kills)
        self.assertEqual([4243], report["killed"])
        self.assertEqual([4243], report["survivors_after"])

    def test_reused_root_pid_skips_group_sweep(self) -> None:
        # The caller reaped the root before sweeping, so a live 4242 is a
        # different process and the group under that number is not the turn's.
        stdout = (
            f"  4242     1 4242 {fixtures._LATER_BIRTH} someone elses shell\n"
            f"  4243  4242 4242 {fixtures._LATER_BIRTH} its child\n"
        )
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            prior_snapshot=[
                {
                    "pid": 4242,
                    "birth": fixtures._LATER_BIRTH,
                    "command": "someone elses shell",
                },
            ],
            kill=lambda pid, sig: self.fail("a reused pid's group is not ours"),
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        enumeration = report["enumeration"]
        self.assertEqual([], enumeration["pgid_members"])
        self.assertEqual("root_pid_reused", enumeration["pgid_skipped"])
        # Verified by the snapshot yet still never signalled: the root pid is
        # protected whichever route identifies it.
        self.assertEqual([4242], enumeration["snapshot_verified"])

    def test_snapshot_candidate_reverified_by_command(self) -> None:
        stdout = (
            "  4243     1 4243 leftover escapee\n"
            "  4244     1 4244 unrelated process that reused the pid\n"
        )
        after = "  4244     1 4244 unrelated process that reused the pid\n"
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result(after),
            ]
        )
        kills: list[int] = []

        report = self._sweep(
            root_pid=4242,
            expected_pgid=None,
            prior_snapshot=[
                {"pid": 4243, "command": "leftover escapee"},
                {"pid": 4244, "command": "the command it had when sampled"},
                {"pid": 4245, "command": "already exited"},
            ],
            kill=lambda pid, sig: kills.append(pid),
            run_command=run_command,
        )

        self.assertEqual([4243], kills)
        self.assertEqual([4243], report["killed"])
        enumeration = report["enumeration"]
        self.assertIsNone(enumeration["expected_pgid"])
        self.assertEqual([], enumeration["pgid_members"])
        self.assertEqual(3, enumeration["snapshot_size"])
        self.assertEqual([4243], enumeration["snapshot_verified"])

    def test_snapshot_rejects_reused_pid_with_same_command(self) -> None:
        # Two identical commands: 4243 is a pid the escapee lost and something
        # else took, 4244 is the escapee itself.
        stdout = (
            f"  4243     1 4243 {fixtures._LATER_BIRTH} /bin/sleep 300\n"
            f"  4244     1 4244 {fixtures._BIRTH} /bin/sleep 300\n"
        )
        after = f"  4243     1 4243 {fixtures._LATER_BIRTH} /bin/sleep 300\n"
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result(after),
            ]
        )
        kills: list[int] = []

        report = self._sweep(
            root_pid=4242,
            expected_pgid=None,
            prior_snapshot=[
                {
                    "pid": 4243,
                    "birth": fixtures._BIRTH,
                    "command": "/bin/sleep 300",
                },
                {
                    "pid": 4244,
                    "birth": fixtures._BIRTH,
                    "command": "/bin/sleep 300",
                },
            ],
            kill=lambda pid, sig: kills.append(pid),
            run_command=run_command,
        )

        self.assertEqual([4244], kills)
        self.assertEqual([4244], report["killed"])
        self.assertEqual([], report["survivors_after"])
        enumeration = report["enumeration"]
        self.assertEqual([4244], enumeration["snapshot_verified"])
        self.assertEqual([4243], enumeration["snapshot_pid_reused"])

    def test_snapshot_entries_without_command_are_ignored(self) -> None:
        stdout = "  4243     1 4243 leftover escapee\n"
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        report = self._sweep(
            root_pid=4242,
            expected_pgid=None,
            prior_snapshot=[{"pid": 4243}, {"command": "no pid"}],
            kill=lambda pid, sig: self.fail("nothing should be signalled"),
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        self.assertEqual([], report["enumeration"]["snapshot_verified"])

    def test_no_candidates_skips_the_resample(self) -> None:
        stdout = "  5555     1 5555 unrelated\n"
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=lambda pid, sig: self.fail("nothing should be signalled"),
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        self.assertEqual([], report["kill_failed"])
        self.assertEqual([], report["survivors_after"])
        self.assertFalse(report["enumeration"]["resampled"])
        self.assertEqual(1, run_command.call_count)

    def test_own_process_group_is_never_enumerated(self) -> None:
        stdout = (
            f" {fixtures._SELF_PID}     1 {fixtures._SELF_PGID} python3 -m unittest\n"
            f" 9999     1 {fixtures._SELF_PGID} sibling in the runner group\n"
        )
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        report = self._sweep(
            root_pid=4242,
            expected_pgid=fixtures._SELF_PGID,
            kill=lambda pid, sig: self.fail("the runner group must survive"),
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        self.assertEqual([], report["enumeration"]["pgid_members"])
        self.assertEqual("own_process_group", report["enumeration"]["pgid_skipped"])

    def test_pgid_of_one_or_less_is_never_enumerated(self) -> None:
        stdout = "    1     0    1 /sbin/launchd\n  4243     1    1 orphan\n"
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        for expected_pgid in (1, 0, -1):
            with self.subTest(expected_pgid=expected_pgid):
                report = self._sweep(
                    root_pid=4242,
                    expected_pgid=expected_pgid,
                    kill=lambda pid, sig: self.fail("pgid 1 must be ignored"),
                    run_command=run_command,
                )
                self.assertEqual([], report["enumeration"]["pgid_members"])
                self.assertNotIn("pgid_skipped", report["enumeration"])

    def test_kill_failure_is_reported_not_raised(self) -> None:
        stdout = "  4243     1 4242 leftover\n"
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        def refuse(pid: int, sig: int) -> None:
            raise PermissionError("not permitted")

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=refuse,
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        self.assertEqual([4243], [entry["pid"] for entry in report["kill_failed"]])
        self.assertIn("not permitted", report["kill_failed"][0]["error"])
        self.assertEqual(1, run_command.call_count)

    def test_already_gone_candidate_is_not_counted_as_killed(self) -> None:
        stdout = "  4243     1 4242 leftover\n"
        run_command = mock.Mock(return_value=fixtures._ps_result(stdout))

        def already_gone(pid: int, sig: int) -> None:
            raise ProcessLookupError(pid)

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=already_gone,
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        self.assertEqual([], report["kill_failed"])
        self.assertEqual([4243], report["enumeration"]["already_gone"])

    def test_survivor_ignored_when_the_pid_was_reused(self) -> None:
        stdout = "  4243     1 4242 leftover\n"
        after = "  4243     1 4243 something new\n"
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result(after),
            ]
        )

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=lambda pid, sig: None,
            run_command=run_command,
        )

        self.assertEqual([4243], report["killed"])
        self.assertEqual([], report["survivors_after"])

    def test_unreadable_resample_leaves_survivors_unreported(self) -> None:
        stdout = "  4243     1 4242 leftover\n"
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result("", returncode=1),
            ]
        )

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            kill=lambda pid, sig: None,
            run_command=run_command,
        )

        self.assertEqual([4243], report["killed"])
        self.assertEqual([], report["survivors_after"])
        self.assertFalse(report["enumeration"]["resampled"])

    def test_unreadable_table_fails_open(self) -> None:
        for run_command in (
            lambda *args, **kwargs: fixtures._ps_result("10 1 10 root\n", returncode=1),
            lambda *args, **kwargs: fixtures._ps_result(""),
        ):
            with self.subTest(run_command=run_command):
                self.assertIsNone(
                    self._sweep(
                        root_pid=4242,
                        expected_pgid=4242,
                        kill=lambda pid, sig: self.fail("no table, no kills"),
                        run_command=run_command,
                    )
                )

    def test_unusable_snapshot_fails_open_instead_of_raising(self) -> None:
        stdout = "  4242     1 4242 harness root\n"

        self.assertIsNone(
            self._sweep(
                root_pid=4242,
                expected_pgid=None,
                prior_snapshot=object(),  # type: ignore[arg-type]
                kill=lambda pid, sig: None,
                run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
            )
        )

    def test_default_settle_sleeps_before_the_resample(self) -> None:
        stdout = "  4243     1 4242 leftover\n"
        after = "  5555     1 5555 unrelated\n"
        run_command = mock.Mock(
            side_effect=[
                fixtures._ps_result(stdout),
                fixtures._ps_result(after),
            ]
        )

        with mock.patch.object(process_tree.time, "sleep") as sleep:
            report = process_tree.sweep_survivors(
                root_pid=4242,
                expected_pgid=4242,
                prior_snapshot=(),
                kill=lambda pid, sig: None,
                run_command=run_command,
            )

        sleep.assert_called_once_with(process_tree.SWEEP_SETTLE_SECONDS)
        self.assertEqual([4243], report["killed"])


class RecordedProcessTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(process_tree.record_process_tree, 0, ())

    def test_recorded_sample_is_returned_for_its_own_root_only(self) -> None:
        process_tree.record_process_tree(10, [{"pid": 10, "command": "root"}])

        self.assertEqual(
            ({"pid": 10, "command": "root"},), process_tree.last_process_tree(10)
        )
        self.assertEqual((), process_tree.last_process_tree(11))

    def test_clearing_consumes_the_sample_for_that_root(self) -> None:
        process_tree.record_process_tree(10, [{"pid": 10, "command": "root"}])

        process_tree.clear_process_tree(10)

        self.assertEqual((), process_tree.last_process_tree(10))

    def test_clearing_another_root_leaves_the_sample_alone(self) -> None:
        process_tree.record_process_tree(10, [{"pid": 10, "command": "root"}])

        process_tree.clear_process_tree(11)

        self.assertEqual(
            ({"pid": 10, "command": "root"},), process_tree.last_process_tree(10)
        )


class SweepEnabledTests(unittest.TestCase):
    def test_default_on_when_unset(self) -> None:
        self.assertTrue(process_tree.sweep_enabled({}))

    def test_truthy_opt_out_disables_the_sweep(self) -> None:
        for raw in ("1", "true", "TRUE", " yes ", "on", "On"):
            with self.subTest(raw=raw):
                self.assertFalse(
                    process_tree.sweep_enabled(
                        {process_tree.SWEEP_DISABLED_ENV_VAR: raw}
                    )
                )

    def test_other_values_keep_the_sweep_on(self) -> None:
        for raw in ("", "0", "false", "no", "off", "maybe"):
            with self.subTest(raw=raw):
                self.assertTrue(
                    process_tree.sweep_enabled(
                        {process_tree.SWEEP_DISABLED_ENV_VAR: raw}
                    )
                )

    def test_unreadable_value_defaults_on(self) -> None:
        self.assertTrue(
            process_tree.sweep_enabled(
                {process_tree.SWEEP_DISABLED_ENV_VAR: 1}  # type: ignore[dict-item]
            )
        )

    def test_reads_the_process_environment_by_default(self) -> None:
        with mock.patch.dict(os.environ, {process_tree.SWEEP_DISABLED_ENV_VAR: "1"}):
            self.assertFalse(process_tree.sweep_enabled())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(process_tree.sweep_enabled())


if __name__ == "__main__":
    unittest.main()
