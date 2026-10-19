"""Process-tree sampler tests: ps parsing, BFS walk, sweep, and fail-open behavior."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from agent_session_loop import process_tree
from tests import support_asl_process_tree as fixtures


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
            return_value=fixtures._ps_result(f"10 1 S 0:00.03 {fixtures._BIRTH} root\n")
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
            f"    1     0 Ss    0:01.00 {fixtures._BIRTH} /sbin/launchd\n"
            f"  200   100 S     0:00.10 {fixtures._BIRTH} grandchild-b\n"
            f"  100    10 S     0:00.50 {fixtures._BIRTH} child-a\n"
            f"  101    10 S+    0:00.20 {fixtures._BIRTH} child-b\n"
            f"  999     1 R     9:59.99 {fixtures._BIRTH} unrelated\n"
            f"   10     1 S     1:00.00 {fixtures._BIRTH} harness root\n"
        )

        tree = process_tree.sample_process_tree(
            10,
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
        )

        self.assertEqual([10, 100, 101, 200], [entry["pid"] for entry in tree])
        root = tree[0]
        self.assertEqual(
            {
                "pid": 10,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": 60.0,
                "birth": fixtures._BIRTH,
                "command": "harness root",
            },
            root,
        )
        self.assertEqual(200, tree[3]["pid"])
        self.assertEqual(100, tree[3]["ppid"])

    def test_rows_without_a_birth_column_keep_the_whole_command(self) -> None:
        stdout = "10 1 S 0:00.03 /bin/sleep 300\n"

        tree = process_tree.sample_process_tree(
            10,
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
        )

        self.assertIsNone(tree[0]["birth"])
        self.assertEqual("/bin/sleep 300", tree[0]["command"])

    def test_command_is_trimmed_to_160_chars(self) -> None:
        long_command = "/usr/bin/python3 " + "x" * 400
        stdout = f"10 1 S 0:00.03 {fixtures._BIRTH} {long_command}\n"

        tree = process_tree.sample_process_tree(
            10,
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
        )

        self.assertEqual(process_tree.COMMAND_MAX_CHARS, len(tree[0]["command"]))
        self.assertEqual(
            long_command[: process_tree.COMMAND_MAX_CHARS], tree[0]["command"]
        )

    def test_command_with_internal_spaces_survives_split(self) -> None:
        stdout = f"10 1 S 0:00.03 {fixtures._BIRTH} /bin/zsh -c echo one two three\n"

        tree = process_tree.sample_process_tree(
            10,
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
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
            10,
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
        )

        self.assertEqual([10, 12], [entry["pid"] for entry in tree])

    def test_root_absent_returns_none(self) -> None:
        stdout = "100 1 S 0:00.03 other\n"

        self.assertIsNone(
            process_tree.sample_process_tree(
                10,
                run_command=lambda *args, **kwargs: fixtures._ps_result(stdout),
            )
        )

    def test_nonzero_exit_returns_none(self) -> None:
        self.assertIsNone(
            process_tree.sample_process_tree(
                10,
                run_command=lambda *args, **kwargs: fixtures._ps_result(
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
                10,
                run_command=lambda *args, **kwargs: fixtures._ps_result(""),
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
        run_command = mock.Mock(
            return_value=fixtures._ps_result(f"10 1 10 {fixtures._BIRTH} root\n")
        )

        table = process_tree.sample_process_table(run_command=run_command, timeout=1.5)

        self.assertEqual(
            {
                10: {
                    "pid": 10,
                    "ppid": 1,
                    "pgid": 10,
                    "birth": fixtures._BIRTH,
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
            f"    1     0    1 {fixtures._BIRTH} /sbin/launchd\n"
            f"   10     1   10 {fixtures._BIRTH} harness root\n"
            f"   11    10   10 {fixtures._LATER_BIRTH} /bin/zsh -c echo one two three\n"
            f"  999     1  999 {fixtures._BIRTH} unrelated\n"
        )

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout)
        )

        self.assertEqual([1, 10, 11, 999], sorted(table))
        self.assertEqual(
            {
                "pid": 11,
                "ppid": 10,
                "pgid": 10,
                "birth": fixtures._LATER_BIRTH,
                "command": "/bin/zsh -c echo one two three",
            },
            table[11],
        )

    def test_rows_without_a_birth_column_keep_the_whole_command(self) -> None:
        stdout = "   10     1   10 /bin/sleep 300\n"

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout)
        )

        self.assertIsNone(table[10]["birth"])
        self.assertEqual("/bin/sleep 300", table[10]["command"])

    def test_space_padded_birth_day_is_normalized(self) -> None:
        stdout = "   10     1   10 Sun Jul  6 17:57:01 2026 harness root\n"

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout)
        )

        self.assertEqual("Sun Jul 6 17:57:01 2026", table[10]["birth"])
        self.assertEqual("harness root", table[10]["command"])

    def test_command_is_trimmed_to_160_chars(self) -> None:
        long_command = "/usr/bin/python3 " + "x" * 400
        stdout = f"10 1 10 {long_command}\n"

        table = process_tree.sample_process_table(
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout)
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
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout)
        )

        self.assertEqual([10, 12], sorted(table))

    def test_nonzero_exit_returns_none(self) -> None:
        self.assertIsNone(
            process_tree.sample_process_table(
                run_command=lambda *args, **kwargs: fixtures._ps_result(
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
                        run_command=lambda *args, _stdout=stdout, **kwargs: (
                            fixtures._ps_result(_stdout)
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
            run_command=lambda *args, **kwargs: fixtures._ps_result(stdout)
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


if __name__ == "__main__":
    unittest.main()
