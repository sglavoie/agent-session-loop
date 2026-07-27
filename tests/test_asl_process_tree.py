"""Process-tree sampler tests: ps parsing, BFS walk, and fail-open behavior."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from agent_session_loop import process_tree


def _ps_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        list(process_tree.PS_COMMAND), returncode, stdout=stdout, stderr=""
    )


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
        run_command = mock.Mock(return_value=_ps_result("10 1 S 0:00.03 root\n"))

        tree = process_tree.sample_process_tree(
            10, run_command=run_command, timeout=2.5
        )

        self.assertEqual(1, len(tree))
        run_command.assert_called_once()
        args, kwargs = run_command.call_args
        self.assertEqual(
            ["ps", "-axo", "pid=,ppid=,state=,time=,command="], args[0]
        )
        self.assertEqual(2.5, kwargs["timeout"])
        self.assertFalse(kwargs["check"])
        self.assertTrue(kwargs["text"])

    def test_bfs_walk_from_root_excludes_unrelated_processes(self) -> None:
        stdout = (
            "    1     0 Ss    0:01.00 /sbin/launchd\n"
            "  200   100 S     0:00.10 grandchild-b\n"
            "  100    10 S     0:00.50 child-a\n"
            "  101    10 S+    0:00.20 child-b\n"
            "  999     1 R     9:59.99 unrelated\n"
            "   10     1 S     1:00.00 harness root\n"
        )

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual([10, 100, 101, 200], [entry["pid"] for entry in tree])
        root = tree[0]
        self.assertEqual(
            {"pid": 10, "ppid": 1, "state": "S", "cpu_seconds": 60.0,
             "command": "harness root"},
            root,
        )
        self.assertEqual(200, tree[3]["pid"])
        self.assertEqual(100, tree[3]["ppid"])

    def test_command_is_trimmed_to_160_chars(self) -> None:
        long_command = "/usr/bin/python3 " + "x" * 400
        stdout = f"10 1 S 0:00.03 {long_command}\n"

        tree = process_tree.sample_process_tree(
            10, run_command=lambda *args, **kwargs: _ps_result(stdout)
        )

        self.assertEqual(
            process_tree.COMMAND_MAX_CHARS, len(tree[0]["command"])
        )
        self.assertEqual(
            long_command[: process_tree.COMMAND_MAX_CHARS], tree[0]["command"]
        )

    def test_command_with_internal_spaces_survives_split(self) -> None:
        stdout = "10 1 S 0:00.03 /bin/zsh -c echo one two three\n"

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


class TotalCpuSecondsTests(unittest.TestCase):
    def test_sums_cpu_across_tree(self) -> None:
        tree = [
            {"pid": 10, "cpu_seconds": 60.0},
            {"pid": 11, "cpu_seconds": 0.03},
            {"pid": 12, "cpu_seconds": 1.5},
        ]
        self.assertAlmostEqual(61.53, process_tree.total_cpu_seconds(tree))

    def test_empty_tree_is_zero(self) -> None:
        self.assertEqual(0.0, process_tree.total_cpu_seconds([]))

    def test_missing_cpu_field_counts_as_zero(self) -> None:
        self.assertEqual(
            2.0,
            process_tree.total_cpu_seconds(
                [{"pid": 10}, {"pid": 11, "cpu_seconds": 2.0}]
            ),
        )


if __name__ == "__main__":
    unittest.main()
