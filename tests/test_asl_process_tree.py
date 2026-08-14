"""Process-tree sampler tests: ps parsing, BFS walk, sweep, and fail-open behavior."""

from __future__ import annotations

import os
import signal
import subprocess
import unittest
from unittest import mock

from agent_session_loop import process_tree


def _ps_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        list(process_tree.PS_COMMAND), returncode, stdout=stdout, stderr=""
    )


# ``ps -o lstart=`` output, as the samplers now ask for it.
_BIRTH = "Sun Jul 26 17:57:01 2026"
_LATER_BIRTH = "Mon Jul 27 09:12:43 2026"


class CpuTimeParsingTests(unittest.TestCase):
    def test_macos_minutes_seconds_fraction(self) -> None:
        self.assertAlmostEqual(0.03, process_tree._parse_cpu_time("0:00.03"))
        self.assertAlmostEqual(83.5, process_tree._parse_cpu_time("1:23.50"))

    def test_linux_mm_ss(self) -> None:
        self.assertEqual(754.0, process_tree._parse_cpu_time("12:34"))

    def test_linux_hh_mm_ss(self) -> None:
        self.assertEqual(3723.0, process_tree._parse_cpu_time("1:02:03"))

    def test_linux_dd_hh_mm_ss(self) -> None:
        self.assertEqual(
            2 * 86400 + 3 * 3600 + 4 * 60 + 5.0,
            process_tree._parse_cpu_time("2-03:04:05"),
        )

    def test_surrounding_whitespace_tolerated(self) -> None:
        self.assertEqual(60.0, process_tree._parse_cpu_time(" 1:00 "))

    def test_invalid_forms_return_none(self) -> None:
        for raw in ("", "12", "abc", "1:2:3:4", "1-2", "-1:00"):
            with self.subTest(raw=raw):
                self.assertIsNone(process_tree._parse_cpu_time(raw))


class SampleProcessTreeTests(unittest.TestCase):
    def test_single_ps_call_uses_portable_headerless_format(self) -> None:
        run_command = mock.Mock(
            return_value=_ps_result(f"10 1 S 0:00.03 {_BIRTH} root\n")
        )

        tree = process_tree.sample_process_tree(
            10, run_command=run_command, timeout=2.5
        )

        self.assertEqual(1, len(tree))
        run_command.assert_called_once()
        args, kwargs = run_command.call_args
        self.assertEqual(
            ["ps", "-axo", "pid=,ppid=,state=,time=,lstart=,command="], args[0]
        )
        self.assertEqual(2.5, kwargs["timeout"])
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["text"])

    def test_bfs_walk_from_root_excludes_unrelated_processes(self) -> None:
        stdout = (
            f"    1     0 Ss    0:01.00 {_BIRTH} /sbin/launchd\n"
            f"  200   100 S     0:00.10 {_BIRTH} grandchild-b\n"
            f"  100    10 S     0:00.50 {_BIRTH} child-a\n"
            f"  101    10 S+    0:00.20 {_BIRTH} child-b\n"
            f"  999     1 R     9:59.99 {_BIRTH} unrelated\n"
            f"   10     1 S     1:00.00 {_BIRTH} harness root\n"
        )

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual([10, 100, 101, 200], [entry["pid"] for entry in tree])
        root = tree[0]
        self.assertEqual(
            {
                "pid": 10,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": 60.0,
                "birth": _BIRTH,
                "command": "harness root",
            },
            root,
        )
        self.assertEqual(200, tree[3]["pid"])
        self.assertEqual(100, tree[3]["ppid"])

    def test_rows_without_a_birth_column_keep_the_whole_command(self) -> None:
        stdout = "10 1 S 0:00.03 /bin/sleep 300\n"

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertIsNone(tree[0]["birth"])
        self.assertEqual("/bin/sleep 300", tree[0]["command"])

    def test_command_is_trimmed_to_160_chars(self) -> None:
        long_command = "/usr/bin/python3 " + "x" * 400
        stdout = f"10 1 S 0:00.03 {_BIRTH} {long_command}\n"

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual(process_tree.COMMAND_MAX_CHARS, len(tree[0]["command"]))
        self.assertEqual(
            long_command[: process_tree.COMMAND_MAX_CHARS], tree[0]["command"]
        )

    def test_command_with_internal_spaces_survives_split(self) -> None:
        stdout = f"10 1 S 0:00.03 {_BIRTH} /bin/zsh -c echo one two three\n"

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual("/bin/zsh -c echo one two three", tree[0]["command"])

    def test_malformed_lines_are_skipped(self) -> None:
        stdout = (
            "garbage\n"
            "10 1 S 0:00.03 root\n"
            "notpid 1 S 0:00.03 bad-pid\n"
            "11 10 S bogus-time child-with-bad-time\n"
            "12 10 S 0:00.10 good-child\n"
        )

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual([10, 12], [entry["pid"] for entry in tree])

    def test_root_absent_returns_none(self) -> None:
        stdout = "100 1 S 0:00.03 other\n"

        self.assertIsNone(
            process_tree.sample_process_tree(
                10, run_command=lambda *args, **kwargs: _ps_result(stdout)
            )
        )

    def test_nonzero_exit_returns_none(self) -> None:
        self.assertIsNone(
            process_tree.sample_process_tree(
                10,
                run_command=lambda *args, **kwargs: _ps_result(
                    "10 1 S 0:00.03 root\n", returncode=1
                ),
            )
        )

    def test_spawn_failure_returns_none(self) -> None:
        def raise_os_error(*args, **kwargs):
            raise OSError("ps missing")

        self.assertIsNone(
            process_tree.sample_process_tree(10, run_command=raise_os_error)
        )

    def test_timeout_returns_none(self) -> None:
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ps", timeout=5.0)

        self.assertIsNone(
            process_tree.sample_process_tree(10, run_command=raise_timeout)
        )

    def test_empty_output_returns_none(self) -> None:
        self.assertIsNone(
            process_tree.sample_process_tree(
                10, run_command=lambda *args, **kwargs: _ps_result("")
            )
        )


class DescendantPidsTests(unittest.TestCase):
    TABLE = {
        1: {"pid": 1, "ppid": 0},
        200: {"pid": 200, "ppid": 100},
        100: {"pid": 100, "ppid": 10},
        101: {"pid": 101, "ppid": 10},
        999: {"pid": 999, "ppid": 1},
        10: {"pid": 10, "ppid": 1},
    }

    def test_bfs_order_excludes_root_and_unrelated(self) -> None:
        self.assertEqual([100, 101, 200], process_tree.descendant_pids(10, self.TABLE))

    def test_leaf_has_no_descendants(self) -> None:
        self.assertEqual([], process_tree.descendant_pids(200, self.TABLE))

    def test_absent_root_has_no_descendants(self) -> None:
        self.assertEqual([], process_tree.descendant_pids(4242, self.TABLE))

    def test_parent_cycle_terminates(self) -> None:
        table = {10: {"pid": 10, "ppid": 11}, 11: {"pid": 11, "ppid": 10}}
        self.assertEqual([11], process_tree.descendant_pids(10, table))

    def test_entries_without_usable_ppid_are_skipped(self) -> None:
        table = {
            10: {"pid": 10, "ppid": 1},
            11: {"pid": 11},
            12: {"pid": 12, "ppid": "not-a-pid"},
        }
        self.assertEqual([], process_tree.descendant_pids(10, table))


class SampleProcessTableTests(unittest.TestCase):
    def test_single_ps_call_uses_pgid_format(self) -> None:
        run_command = mock.Mock(return_value=_ps_result(f"10 1 10 {_BIRTH} root\n"))

        table = process_tree.sample_process_table(run_command=run_command, timeout=1.5)

        self.assertEqual(
            {
                10: {
                    "pid": 10,
                    "ppid": 1,
                    "pgid": 10,
                    "birth": _BIRTH,
                    "command": "root",
                }
            },
            table,
        )
        run_command.assert_called_once()
        args, kwargs = run_command.call_args
        self.assertEqual(["ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="], args[0])
        self.assertEqual(1.5, kwargs["timeout"])
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["text"])

    def test_parses_every_row_including_unrelated_processes(self) -> None:
        stdout = (
            f"    1     0    1 {_BIRTH} /sbin/launchd\n"
            f"   10     1   10 {_BIRTH} harness root\n"
            f"   11    10   10 {_LATER_BIRTH} /bin/zsh -c echo one two three\n"
            f"  999     1  999 {_BIRTH} unrelated\n"
        )

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual([1, 10, 11, 999], sorted(table))
        self.assertEqual(
            {
                "pid": 11,
                "ppid": 10,
                "pgid": 10,
                "birth": _LATER_BIRTH,
                "command": "/bin/zsh -c echo one two three",
            },
            table[11],
        )

    def test_rows_without_a_birth_column_keep_the_whole_command(self) -> None:
        stdout = "   10     1   10 /bin/sleep 300\n"

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertIsNone(table[10]["birth"])
        self.assertEqual("/bin/sleep 300", table[10]["command"])

    def test_space_padded_birth_day_is_normalized(self) -> None:
        stdout = "   10     1   10 Sun Jul  6 17:57:01 2026 harness root\n"

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual("Sun Jul 6 17:57:01 2026", table[10]["birth"])
        self.assertEqual("harness root", table[10]["command"])

    def test_command_is_trimmed_to_160_chars(self) -> None:
        long_command = "/usr/bin/python3 " + "x" * 400
        stdout = f"10 1 10 {long_command}\n"

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual(
            long_command[: process_tree.COMMAND_MAX_CHARS], table[10]["command"]
        )

    def test_malformed_lines_are_skipped(self) -> None:
        stdout = (
            "garbage\n"
            "10 1 10 root\n"
            "notpid 1 10 bad-pid\n"
            "11 10 bogus-pgid child-with-bad-pgid\n"
            "12 10 10 good-child\n"
        )

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual([10, 12], sorted(table))

    def test_nonzero_exit_returns_none(self) -> None:
        self.assertIsNone(
            process_tree.sample_process_table(
                run_command=lambda *args, **kwargs: _ps_result(
                    "10 1 10 root\n", returncode=1
                )
            )
        )

    def test_spawn_failure_returns_none(self) -> None:
        def raise_os_error(*args, **kwargs):
            raise OSError("ps missing")

        self.assertIsNone(process_tree.sample_process_table(run_command=raise_os_error))

    def test_timeout_returns_none(self) -> None:
        def raise_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ps", timeout=5.0)

        self.assertIsNone(process_tree.sample_process_table(run_command=raise_timeout))

    def test_output_without_a_parsable_row_returns_none(self) -> None:
        for stdout in ("", "garbage\n"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(
                    process_tree.sample_process_table(
                        run_command=lambda *args, _stdout=stdout, **kwargs: _ps_result(
                            _stdout
                        )
                    )
                )

    def test_table_feeds_descendant_pids(self) -> None:
        stdout = (
            "   10     1   10 harness root\n"
            "   11    10   10 child\n"
            "   12    11   12 escapee\n"
            "  999     1  999 unrelated\n"
        )

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual([11, 12], process_tree.descendant_pids(10, table))


class GroupMemberPidsTests(unittest.TestCase):
    TABLE = {
        1: {"pid": 1, "ppid": 0, "pgid": 1},
        11: {"pid": 11, "ppid": 10, "pgid": 10},
        10: {"pid": 10, "ppid": 1, "pgid": 10},
        12: {"pid": 12, "ppid": 11, "pgid": 12},
        999: {"pid": 999, "ppid": 1, "pgid": 999},
    }

    def test_members_returned_ascending(self) -> None:
        self.assertEqual([10, 11], process_tree.group_member_pids(10, self.TABLE))

    def test_escapee_group_holds_only_itself(self) -> None:
        self.assertEqual([12], process_tree.group_member_pids(12, self.TABLE))

    def test_unknown_group_is_empty(self) -> None:
        self.assertEqual([], process_tree.group_member_pids(4242, self.TABLE))

    def test_entries_without_usable_pgid_are_skipped(self) -> None:
        table = {
            10: {"pid": 10, "pgid": 10},
            11: {"pid": 11},
            12: {"pid": 12, "pgid": "not-a-pgid"},
        }
        self.assertEqual([10], process_tree.group_member_pids(10, table))


_SELF_PID = 7777
_SELF_PGID = 8888


class SweepSurvivorsTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (("getpid", _SELF_PID), ("getpgrp", _SELF_PGID)):
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
            f" {_SELF_PID}     1 {_SELF_PGID} python3 -m unittest\n"
            "  4243     1 4242 leftover child\n"
            "  4244  4243 4242 leftover grandchild\n"
            "  5555     1 5555 unrelated\n"
        )
        after = (
            "    1     0    1 /sbin/launchd\n"
            f" {_SELF_PID}     1 {_SELF_PGID} python3 -m unittest\n"
            "  5555     1 5555 unrelated\n"
        )
        run_command = mock.Mock(side_effect=[_ps_result(stdout), _ps_result(after)])
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
            f" {_SELF_PID}     1 4242 python3 -m unittest\n"
            "  4243     1 4242 leftover child\n"
        )
        run_command = mock.Mock(side_effect=[_ps_result(stdout), _ps_result(stdout)])
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
            f"  4242     1 4242 {_LATER_BIRTH} someone elses shell\n"
            f"  4243  4242 4242 {_LATER_BIRTH} its child\n"
        )
        run_command = mock.Mock(return_value=_ps_result(stdout))

        report = self._sweep(
            root_pid=4242,
            expected_pgid=4242,
            prior_snapshot=[
                {"pid": 4242, "birth": _LATER_BIRTH, "command": "someone elses shell"},
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
        run_command = mock.Mock(side_effect=[_ps_result(stdout), _ps_result(after)])
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
            f"  4243     1 4243 {_LATER_BIRTH} /bin/sleep 300\n"
            f"  4244     1 4244 {_BIRTH} /bin/sleep 300\n"
        )
        after = f"  4243     1 4243 {_LATER_BIRTH} /bin/sleep 300\n"
        run_command = mock.Mock(side_effect=[_ps_result(stdout), _ps_result(after)])
        kills: list[int] = []

        report = self._sweep(
            root_pid=4242,
            expected_pgid=None,
            prior_snapshot=[
                {"pid": 4243, "birth": _BIRTH, "command": "/bin/sleep 300"},
                {"pid": 4244, "birth": _BIRTH, "command": "/bin/sleep 300"},
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
        run_command = mock.Mock(return_value=_ps_result(stdout))

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
        run_command = mock.Mock(return_value=_ps_result(stdout))

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
            f" {_SELF_PID}     1 {_SELF_PGID} python3 -m unittest\n"
            f" 9999     1 {_SELF_PGID} sibling in the runner group\n"
        )
        run_command = mock.Mock(return_value=_ps_result(stdout))

        report = self._sweep(
            root_pid=4242,
            expected_pgid=_SELF_PGID,
            kill=lambda pid, sig: self.fail("the runner group must survive"),
            run_command=run_command,
        )

        self.assertEqual([], report["killed"])
        self.assertEqual([], report["enumeration"]["pgid_members"])
        self.assertEqual("own_process_group", report["enumeration"]["pgid_skipped"])

    def test_pgid_of_one_or_less_is_never_enumerated(self) -> None:
        stdout = "    1     0    1 /sbin/launchd\n  4243     1    1 orphan\n"
        run_command = mock.Mock(return_value=_ps_result(stdout))

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
        run_command = mock.Mock(return_value=_ps_result(stdout))

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
        run_command = mock.Mock(return_value=_ps_result(stdout))

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
        run_command = mock.Mock(side_effect=[_ps_result(stdout), _ps_result(after)])

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
            side_effect=[_ps_result(stdout), _ps_result("", returncode=1)]
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
            lambda *args, **kwargs: _ps_result("10 1 10 root\n", returncode=1),
            lambda *args, **kwargs: _ps_result(""),
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
                run_command=lambda *args, **kwargs: _ps_result(stdout),
            )
        )

    def test_default_settle_sleeps_before_the_resample(self) -> None:
        stdout = "  4243     1 4242 leftover\n"
        after = "  5555     1 5555 unrelated\n"
        run_command = mock.Mock(side_effect=[_ps_result(stdout), _ps_result(after)])

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


if __name__ == "__main__":
    unittest.main()
