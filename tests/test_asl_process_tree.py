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


class VerifiedRootRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = {
            "pid": 10,
            "ppid": 1,
            "birth": _BIRTH,
            "command": "claude --print",
        }

    def prepare(self, table, *, force=False, prior=None):
        with mock.patch.object(
            process_tree, "sample_process_table", return_value=table
        ):
            return process_tree.prepare_verified_root_recovery(
                root_pid=10,
                prior_tree=[self.root] if prior is None else prior,
                attempted_at="now",
                force=force,
                environ={},
            )

    def test_verified_root_is_signaled_with_the_selected_shutdown_stage(self) -> None:
        for force, expected in [(False, signal.SIGTERM), (True, signal.SIGKILL)]:
            with self.subTest(force=force):
                report, pids = self.prepare({10: self.root}, force=force)
                kill = mock.Mock()
                completed = process_tree.signal_verified_root_recovery(
                    report, pids, force=force, kill=kill
                )
                kill.assert_called_once_with(10, expected)
                self.assertEqual([10], completed["signaled_pids"])
                self.assertEqual(expected.name, completed["signal"])

    def test_pid_reuse_or_missing_identity_refuses_both_shutdown_stages(self) -> None:
        for force in [False, True]:
            for changes in [{"birth": _LATER_BIRTH}, {"command": "stranger"}]:
                report, pids = self.prepare({10: {**self.root, **changes}}, force=force)
                self.assertEqual((), pids)
                self.assertEqual(
                    "harness_root_identity", report["identity_mismatches"][0]["reason"]
                )
            for prior in [[], [{**self.root, "birth": None}]]:
                report, pids = self.prepare({10: self.root}, force=force, prior=prior)
                self.assertEqual((), pids)
                self.assertTrue(report["identity_mismatches"])

    def test_new_descendants_prevent_escalation(self) -> None:
        report, pids = self.prepare(
            {10: self.root, 20: {"pid": 20, "ppid": 10}}, force=True
        )
        self.assertEqual((), pids)
        self.assertEqual("harness_has_descendants", report["disabled"])

    def test_missing_root_and_unreadable_table_are_reported(self) -> None:
        report, pids = self.prepare({})
        self.assertEqual((), pids)
        self.assertEqual([10], report["already_gone_pids"])
        report, pids = self.prepare(None)
        self.assertEqual((), pids)
        self.assertIn("sample_error", report)

    def test_opt_out_and_protected_pids_never_sample_or_signal(self) -> None:
        with mock.patch.object(process_tree, "sample_process_table") as sample:
            for pid, environ in [
                (10, {process_tree.SWEEP_DISABLED_ENV_VAR: "true"}),
                (1, {}),
                (os.getpid(), {}),
            ]:
                report, pids = process_tree.prepare_verified_root_recovery(
                    root_pid=pid,
                    prior_tree=[self.root],
                    attempted_at="now",
                    environ=environ,
                )
                self.assertEqual((), pids)
                self.assertIn("disabled", report)
            sample.assert_not_called()


class VerifiedLeafRecoveryTests(unittest.TestCase):
    """SIGTERM recovery selects only revalidated leaves of the harness tree."""

    @staticmethod
    def row(pid: int, ppid: int, command: str, *, birth: str = _BIRTH) -> dict:
        return {
            "pid": pid,
            "ppid": ppid,
            "pgid": 10,
            "birth": birth,
            "command": command,
        }

    def prepare(self, prior: list[dict], current: dict[int, dict]):
        with mock.patch.object(
            process_tree, "sample_process_table", return_value=current
        ):
            return process_tree.prepare_verified_leaf_recovery(
                root_pid=10,
                prior_tree=prior,
                attempted_at="2026-08-20T12:00:00+00:00",
            )

    def test_claude_shell_eza_selects_only_eza(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c eza -la"),
            self.row(30, 20, "eza -la"),
        ]
        report, candidates = self.prepare(prior, {row["pid"]: row for row in prior})

        self.assertEqual([30], report["candidate_pids"])
        self.assertEqual((30,), candidates)
        killed: list[tuple[int, int]] = []
        completed = process_tree.signal_verified_leaf_recovery(
            report, candidates, kill=lambda pid, sig: killed.append((pid, sig))
        )
        self.assertEqual([(30, signal.SIGTERM)], killed)
        self.assertEqual([30], completed["signaled_pids"])

    def test_pipeline_selects_both_deepest_leaves(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c grep needle file | head"),
            self.row(30, 20, "grep needle file"),
            self.row(31, 20, "head"),
        ]
        report, candidates = self.prepare(prior, {row["pid"]: row for row in prior})

        self.assertEqual([30, 31], report["candidate_pids"])
        self.assertEqual((30, 31), candidates)

    def test_identity_and_ancestry_changes_are_rejected(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c workers"),
            self.row(30, 20, "eza -la"),
            self.row(31, 20, "grep needle"),
            self.row(32, 20, "head"),
        ]
        current = {row["pid"]: dict(row) for row in prior}
        current[30]["birth"] = _LATER_BIRTH
        current[31]["command"] = "grep something-else"
        current[32]["ppid"] = 1

        report, candidates = self.prepare(prior, current)

        self.assertEqual((), candidates)
        self.assertEqual(
            [
                {"pid": 30, "reason": "birth"},
                {"pid": 31, "reason": "command"},
                {"pid": 32, "reason": "ancestry"},
            ],
            report["identity_mismatches"],
        )

    def test_reused_harness_root_rejects_every_leaf(self) -> None:
        prior = [self.row(10, 1, "claude --print"), self.row(30, 10, "eza -la")]
        current = {row["pid"]: dict(row) for row in prior}
        current[10]["birth"] = _LATER_BIRTH

        report, candidates = self.prepare(prior, current)

        self.assertEqual((), candidates)
        self.assertEqual(
            [{"pid": 10, "reason": "harness_root_identity"}],
            report["identity_mismatches"],
        )

    def test_disappearing_leaf_is_already_gone_and_parent_is_not_promoted(self) -> None:
        prior = [
            self.row(10, 1, "claude --print"),
            self.row(20, 10, "/bin/zsh -c eza -la"),
            self.row(30, 20, "eza -la"),
        ]

        report, candidates = self.prepare(prior, {10: prior[0], 20: prior[1]})

        self.assertEqual((), candidates)
        self.assertEqual([30], report["already_gone_pids"])

    def test_root_only_tree_and_failed_sample_never_signal(self) -> None:
        root = self.row(10, 1, "claude --print")
        report, candidates = self.prepare([root], {10: root})
        self.assertEqual([], report["candidate_pids"])
        self.assertEqual((), candidates)

        with mock.patch.object(process_tree, "sample_process_table", return_value=None):
            failed, failed_candidates = process_tree.prepare_verified_leaf_recovery(
                root_pid=10,
                prior_tree=[root, self.row(30, 10, "eza -la")],
                attempted_at="now",
            )
        self.assertEqual((), failed_candidates)
        self.assertEqual("process table sample failed", failed["sample_error"])

    def test_process_sweep_opt_out_disables_recovery(self) -> None:
        prior = [self.row(10, 1, "claude --print"), self.row(30, 10, "eza -la")]
        with mock.patch.object(process_tree, "sample_process_table") as sample:
            report, candidates = process_tree.prepare_verified_leaf_recovery(
                root_pid=10,
                prior_tree=prior,
                attempted_at="now",
                environ={process_tree.SWEEP_DISABLED_ENV_VAR: "true"},
            )
        self.assertEqual((), candidates)
        self.assertEqual("process_sweep_opt_out", report["disabled"])
        sample.assert_not_called()

    def test_signal_failures_and_last_moment_exit_are_reported(self) -> None:
        report = {
            "candidate_pids": [30, 31],
            "signaled_pids": [],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }

        def kill(pid: int, sig: int) -> None:
            self.assertEqual(signal.SIGTERM, sig)
            if pid == 30:
                raise ProcessLookupError
            raise PermissionError("denied")

        completed = process_tree.signal_verified_leaf_recovery(
            report, (30, 31), kill=kill
        )

        self.assertEqual([30], completed["already_gone_pids"])
        self.assertEqual([], completed["signaled_pids"])
        self.assertEqual(31, completed["signal_failures"][0]["pid"])
        self.assertIn("denied", completed["signal_failures"][0]["error"])


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


class _UrgentBase(unittest.TestCase):
    """Shared identity for the urgent-termination cases."""

    def setUp(self) -> None:
        for name, value in (("getpid", _SELF_PID), ("getpgrp", _SELF_PGID)):
            patcher = mock.patch.object(process_tree.os, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _birth_result(birth: str | None) -> subprocess.CompletedProcess:
        """A ``ps -o lstart= -p`` answer, or the not-found answer."""
        if birth is None:
            return subprocess.CompletedProcess([], 1, stdout="", stderr="")
        return subprocess.CompletedProcess([], 0, stdout=f"{birth}\n", stderr="")


class CollectDescendantEvidenceTests(_UrgentBase):
    # A worker group (4242) with an in-group child, and a grandchild that
    # called setsid and therefore left the group: the one a killpg will miss.
    TABLE = (
        "    1     0    1 /sbin/launchd\n"
        f" {_SELF_PID}     1 {_SELF_PGID} python3 -m unittest\n"
        f"  4242     1 4242 {_BIRTH} codex exec\n"
        f"  4243  4242 4242 {_BIRTH} /bin/zsh -c build\n"
        f"  4244  4243 4244 {_BIRTH} escaped daemon\n"
        f"  5555     1 5555 {_BIRTH} unrelated\n"
    )

    def _collect(self, stdout: str = TABLE, **kwargs):
        run_command = mock.Mock(return_value=_ps_result(stdout))
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

        self.assertEqual([_BIRTH, _BIRTH], [e["birth"] for e in evidence["entries"]])

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
            f"  4242     1 4242 {_BIRTH} codex exec\n"
            f" {_SELF_PID}  4242 {_SELF_PGID} {_BIRTH} python3 -m unittest\n"
        )
        evidence, _ = self._collect(stdout)

        self.assertEqual([], evidence["entries"])
        self.assertEqual([], evidence["escaped_pids"])

    def test_an_absent_root_is_a_complete_answer_not_a_failed_one(self) -> None:
        stdout = f"  5555     1 5555 {_BIRTH} unrelated\n"
        evidence, _ = self._collect(stdout)

        self.assertTrue(evidence["collected"])
        self.assertTrue(evidence["root_absent"])
        self.assertEqual([], evidence["entries"])

    def test_a_failed_sample_reports_uncollected_rather_than_empty(self) -> None:
        run_command = mock.Mock(return_value=_ps_result("", returncode=1))

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
        run_command = mock.Mock(return_value=_ps_result(self.TABLE))

        evidence = process_tree.collect_descendant_evidence(
            4242,
            run_command=run_command,
            environ={process_tree.SWEEP_DISABLED_ENV_VAR: "1"},
        )

        self.assertEqual("process_sweep_opt_out", evidence["disabled"])
        self.assertFalse(evidence["collected"])
        run_command.assert_not_called()


class ForceStopProcessGroupTests(_UrgentBase):
    def test_one_killpg_and_nothing_else(self) -> None:
        calls: list[tuple[int, int]] = []

        report = process_tree.force_stop_process_group(
            4242, killpg=lambda pgid, sig: calls.append((pgid, sig))
        )

        self.assertEqual([(4242, signal.SIGKILL)], calls)
        self.assertTrue(report["signalled"])
        self.assertFalse(report["already_gone"])
        self.assertIsNone(report["refused"])
        self.assertIsNone(report["error"])

    def test_the_deadline_path_takes_no_process_table_probe(self) -> None:
        # The guard that keeps the five-second probe off this path: the kill
        # cannot sample even if it wanted to, because it is given no sampler.
        with (
            mock.patch.object(process_tree, "sample_process_table") as sampler,
            mock.patch.object(process_tree, "process_birth") as birth,
        ):
            process_tree.force_stop_process_group(4242, killpg=lambda *_: None)

        sampler.assert_not_called()
        birth.assert_not_called()

    def test_own_process_group_is_refused(self) -> None:
        calls: list[tuple[int, int]] = []

        report = process_tree.force_stop_process_group(
            _SELF_PGID, killpg=lambda pgid, sig: calls.append((pgid, sig))
        )

        self.assertEqual(process_tree.REFUSE_OWN_PROCESS_GROUP, report["refused"])
        self.assertEqual([], calls)

    def test_pgid_of_one_or_less_is_refused(self) -> None:
        for pgid in (-1, 0, 1):
            with self.subTest(pgid=pgid):
                report = process_tree.force_stop_process_group(
                    pgid, killpg=lambda *_: None
                )
                self.assertEqual(process_tree.REFUSE_INVALID_PGID, report["refused"])

    def test_an_already_empty_group_is_reported_not_raised(self) -> None:
        def killpg(pgid: int, sig: int) -> None:
            raise ProcessLookupError

        report = process_tree.force_stop_process_group(4242, killpg=killpg)

        self.assertTrue(report["already_gone"])
        self.assertFalse(report["signalled"])

    def test_a_refused_signal_is_reported_not_raised(self) -> None:
        def killpg(pgid: int, sig: int) -> None:
            raise PermissionError("not yours")

        report = process_tree.force_stop_process_group(4242, killpg=killpg)

        self.assertFalse(report["signalled"])
        self.assertIn("not yours", report["error"])


class TerminateEscapedDescendantsTests(_UrgentBase):
    @staticmethod
    def _evidence(**overrides):
        evidence = {
            "root_pid": 4242,
            "root_pgid": 4242,
            "entries": [
                {"pid": 4243, "ppid": 4242, "pgid": 4242, "birth": _BIRTH},
                {"pid": 4244, "ppid": 4243, "pgid": 4244, "birth": _BIRTH},
            ],
            "escaped_pids": [4244],
            "collected": True,
            "disabled": None,
        }
        evidence.update(overrides)
        return evidence

    def _terminate(self, evidence, birth: str | None, kills: list):
        run_command = mock.Mock(return_value=self._birth_result(birth))
        return process_tree.terminate_escaped_descendants(
            evidence,
            kill=lambda pid, sig: kills.append((pid, sig)),
            run_command=run_command,
        )

    def test_only_the_escapee_is_signalled_and_only_after_it_is_identified(
        self,
    ) -> None:
        kills: list[tuple[int, int]] = []

        report = self._terminate(self._evidence(), _BIRTH, kills)

        # 4243 died with the group; re-signalling it would only cost a probe.
        self.assertEqual([(4244, signal.SIGKILL)], kills)
        self.assertEqual([4244], report["candidate_pids"])
        self.assertEqual([4244], report["killed"])
        self.assertEqual(process_tree.URGENT_ESCAPEE_POLICY, report["policy"])

    def test_a_reused_pid_is_reported_and_never_signalled(self) -> None:
        kills: list[tuple[int, int]] = []

        report = self._terminate(self._evidence(), _LATER_BIRTH, kills)

        self.assertEqual([], kills)
        self.assertEqual(
            [{"pid": 4244, "reason": "birth"}], report["identity_mismatches"]
        )
        self.assertEqual([], report["killed"])

    def test_an_unreadable_identity_is_reported_and_never_signalled(self) -> None:
        kills: list[tuple[int, int]] = []

        report = self._terminate(self._evidence(), None, kills)

        self.assertEqual([], kills)
        self.assertEqual([4244], report["unverified"])
        self.assertEqual([], report["killed"])

    def test_an_escapee_recorded_without_a_birth_is_never_signalled(self) -> None:
        kills: list[tuple[int, int]] = []
        evidence = self._evidence(
            entries=[{"pid": 4244, "ppid": 4243, "pgid": 4244, "birth": None}]
        )

        report = self._terminate(evidence, _BIRTH, kills)

        self.assertEqual([], kills)
        self.assertEqual([4244], report["unverified"])

    def test_the_runner_and_the_root_are_never_candidates(self) -> None:
        kills: list[tuple[int, int]] = []
        evidence = self._evidence(escaped_pids=[1, _SELF_PID, 4242, 4244])

        report = self._terminate(evidence, _BIRTH, kills)

        self.assertEqual([4244], report["candidate_pids"])
        self.assertEqual([(4244, signal.SIGKILL)], kills)

    def test_a_last_moment_exit_is_already_gone_rather_than_a_failure(self) -> None:
        def kill(pid: int, sig: int) -> None:
            raise ProcessLookupError

        run_command = mock.Mock(return_value=self._birth_result(_BIRTH))
        report = process_tree.terminate_escaped_descendants(
            self._evidence(), kill=kill, run_command=run_command
        )

        self.assertEqual([4244], report["already_gone"])
        self.assertEqual([], report["killed"])

    def test_a_kill_failure_is_reported_not_raised(self) -> None:
        def kill(pid: int, sig: int) -> None:
            raise PermissionError("not yours")

        run_command = mock.Mock(return_value=self._birth_result(_BIRTH))
        report = process_tree.terminate_escaped_descendants(
            self._evidence(), kill=kill, run_command=run_command
        )

        self.assertEqual([{"pid": 4244, "error": "not yours"}], report["kill_failures"])

    def test_the_report_shape_is_the_same_whether_or_not_it_failed(self) -> None:
        # These reports are copied into a run's artifacts, so a reader must not
        # have to guess which keys this particular outcome happened to write.
        kills: list[tuple[int, int]] = []
        succeeded = self._terminate(self._evidence(), _BIRTH, kills)
        malformed = process_tree.terminate_escaped_descendants(
            {"entries": [], "escaped_pids": 4244},
            kill=mock.Mock(),
            run_command=mock.Mock(),
        )

        self.assertIsNotNone(malformed["error"])
        self.assertIsNone(succeeded["error"])
        self.assertEqual(set(succeeded), set(malformed))

    def test_the_process_sweep_opt_out_signals_nothing(self) -> None:
        kills: list[tuple[int, int]] = []
        evidence = self._evidence(disabled="process_sweep_opt_out")

        report = self._terminate(evidence, _BIRTH, kills)

        self.assertEqual("process_sweep_opt_out", report["disabled"])
        self.assertEqual([], kills)
        self.assertEqual([], report["candidate_pids"])


class ConfirmTerminationTests(_UrgentBase):
    @staticmethod
    def _evidence(**overrides):
        evidence = {
            "root_pid": 4242,
            "root_pgid": 4242,
            "entries": [{"pid": 4243, "ppid": 4242, "pgid": 4242, "birth": _BIRTH}],
            "escaped_pids": [],
            "collected": True,
            "disabled": None,
        }
        evidence.update(overrides)
        return evidence

    def _confirm(
        self, *, group: Exception | None, alive: Exception | None, birth=_BIRTH
    ):
        def killpg(pgid: int, sig: int) -> None:
            if group is not None:
                raise group

        def kill(pid: int, sig: int) -> None:
            if alive is not None:
                raise alive

        return process_tree.confirm_termination(
            pgid=4242,
            evidence=self._evidence(),
            kill=kill,
            killpg=killpg,
            run_command=mock.Mock(return_value=self._birth_result(birth)),
        )

    def test_a_stopped_group_with_nothing_left_is_complete(self) -> None:
        confirmation = self._confirm(
            group=ProcessLookupError(), alive=ProcessLookupError()
        )

        self.assertEqual(process_tree.GROUP_STOPPED, confirmation["group"])
        self.assertTrue(confirmation["complete"])
        self.assertIsNone(confirmation["incomplete_reason"])
        self.assertEqual([], confirmation["remaining_pids"])

    def test_a_group_still_answering_signal_zero_is_not_stopped(self) -> None:
        confirmation = self._confirm(group=None, alive=ProcessLookupError())

        self.assertEqual(process_tree.GROUP_RUNNING, confirmation["group"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_GROUP_NOT_STOPPED,
            confirmation["incomplete_reason"],
        )

    def test_an_unreaped_group_leader_reads_as_ownership_unknown(self) -> None:
        # A signalled but uncollected process is still in its group, and on
        # this platform that group answers signal 0 with EPERM rather than
        # ESRCH -- either way it is not gone.
        confirmation = self._confirm(
            group=PermissionError(), alive=ProcessLookupError()
        )

        self.assertEqual(process_tree.GROUP_OWNERSHIP_UNKNOWN, confirmation["group"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_OWNERSHIP_UNKNOWN,
            confirmation["incomplete_reason"],
        )

    def test_a_surviving_descendant_under_its_own_identity_blocks(self) -> None:
        confirmation = self._confirm(group=ProcessLookupError(), alive=None)

        self.assertEqual([4243], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            confirmation["incomplete_reason"],
        )

    def test_a_reused_descendant_pid_does_not_block(self) -> None:
        confirmation = self._confirm(
            group=ProcessLookupError(), alive=None, birth=_LATER_BIRTH
        )

        self.assertEqual([4243], confirmation["pid_reused"])
        self.assertEqual([], confirmation["remaining_pids"])
        self.assertTrue(confirmation["complete"])

    def test_a_descendant_whose_identity_is_unreadable_blocks(self) -> None:
        confirmation = self._confirm(group=ProcessLookupError(), alive=None, birth=None)

        self.assertEqual([4243], confirmation["unverified_pids"])
        self.assertEqual([4243], confirmation["remaining_pids"])
        self.assertFalse(confirmation["complete"])

    def test_a_descendant_this_run_may_not_signal_is_ownership_unknown(self) -> None:
        confirmation = self._confirm(
            group=ProcessLookupError(), alive=PermissionError()
        )

        self.assertTrue(confirmation["ownership_unknown"])
        self.assertEqual([4243], confirmation["remaining_pids"])
        self.assertEqual(
            process_tree.INCOMPLETE_OWNERSHIP_UNKNOWN,
            confirmation["incomplete_reason"],
        )

    def test_the_process_sweep_opt_out_cannot_prove_the_work_stopped(self) -> None:
        confirmation = process_tree.confirm_termination(
            pgid=4242,
            evidence={
                "entries": [],
                "collected": False,
                "disabled": "process_sweep_opt_out",
            },
            kill=mock.Mock(),
            killpg=mock.Mock(side_effect=ProcessLookupError),
        )

        self.assertEqual(process_tree.GROUP_STOPPED, confirmation["group"])
        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_SWEEP_OPT_OUT, confirmation["incomplete_reason"]
        )

    def test_the_report_shape_is_the_same_whether_or_not_it_failed(self) -> None:
        complete = self._confirm(group=ProcessLookupError(), alive=ProcessLookupError())
        malformed = process_tree.confirm_termination(
            pgid=4242,
            evidence={"entries": 4243, "collected": True},
            kill=mock.Mock(),
            killpg=mock.Mock(side_effect=ProcessLookupError),
        )

        self.assertIsNotNone(malformed["error"])
        self.assertFalse(malformed["complete"])
        self.assertIsNone(complete["error"])
        self.assertEqual(set(complete), set(malformed))

    def test_evidence_that_was_never_collected_cannot_prove_it_either(self) -> None:
        confirmation = process_tree.confirm_termination(
            pgid=4242,
            evidence={"entries": [], "collected": False, "sample_error": "timed out"},
            kill=mock.Mock(),
            killpg=mock.Mock(side_effect=ProcessLookupError),
        )

        self.assertFalse(confirmation["complete"])
        self.assertEqual(
            process_tree.INCOMPLETE_EVIDENCE_UNAVAILABLE,
            confirmation["incomplete_reason"],
        )


if __name__ == "__main__":
    unittest.main()
