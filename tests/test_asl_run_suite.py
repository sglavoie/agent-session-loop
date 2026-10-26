"""The shard driver's own behavior: slices, timeouts, signals, output caps."""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import run_suite

#: A shard that starts a grandchild in its own process group, says so, and
#: then hangs; only killing the whole group ends both.
_HANGING_SHARD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(f"grandchild {child.pid}", flush=True)
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _TempLogsMixin(unittest.TestCase):
    """Point the driver's log directory at a scratch one for each test."""

    def setUp(self) -> None:
        super().setUp()
        logs = tempfile.TemporaryDirectory()
        self.addCleanup(logs.cleanup)
        self.logs = Path(logs.name) / "run_suite_logs"
        patcher = mock.patch.object(run_suite, "LOGS_DIR", self.logs)
        patcher.start()
        self.addCleanup(patcher.stop)


class ShardTimeoutTests(_TempLogsMixin):
    def test_an_overrunning_shard_and_its_group_are_killed(self) -> None:
        result = run_suite._run_shard(
            "hang", [sys.executable, "-c", _HANGING_SHARD], timeout=2
        )

        self.assertTrue(result["timed_out"])
        self.assertEqual(run_suite._verdict(result), "TIMEOUT")
        self.assertEqual(result["returncode"], -9)
        line = next(
            line for line in result["output"].splitlines() if "grandchild" in line
        )
        grandchild = int(line.split()[1])
        deadline = time.monotonic() + 10
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), "the grandchild outlived the timeout")
        report = run_suite.failure_report(result, verbose=False, timeout=2)
        self.assertIn("TIMEOUT: shard hang killed after 2s", report)
        self.assertIn("rc=-9 (SIGKILL)", report)
        self.assertIn("grandchild", report)

    def test_a_finished_shard_is_not_a_timeout(self) -> None:
        result = run_suite._run_shard(
            "quick", [sys.executable, "-c", "print('Ran 3 tests')"], timeout=60
        )

        self.assertFalse(result["timed_out"])
        self.assertEqual(run_suite._verdict(result), "ok")
        self.assertEqual(result["tests"], 3)

    def test_the_timeout_comes_from_the_environment_or_the_default(self) -> None:
        with mock.patch.dict(os.environ, {run_suite.TIMEOUT_ENV: "12.5"}):
            self.assertEqual(run_suite._env_timeout(), 12.5)
        with mock.patch.dict(os.environ, {run_suite.TIMEOUT_ENV: ""}):
            self.assertEqual(run_suite._env_timeout(), run_suite.DEFAULT_SHARD_TIMEOUT)
        with (
            mock.patch.dict(os.environ, {run_suite.TIMEOUT_ENV: "soon"}),
            self.assertRaises(SystemExit),
        ):
            run_suite._env_timeout()

    def test_a_negative_or_unbounded_timeout_is_rejected(self) -> None:
        for raw in ("-1", "-0.5", "inf", "nan"):
            with self.subTest(raw=raw):
                with self.assertRaises(argparse.ArgumentTypeError):
                    run_suite.shard_timeout(raw)
                with (
                    mock.patch.dict(os.environ, {run_suite.TIMEOUT_ENV: raw}),
                    self.assertRaises(SystemExit) as raised,
                ):
                    run_suite._env_timeout()
                self.assertIn(run_suite.TIMEOUT_ENV, str(raised.exception))
                self.assertIn("non-negative", str(raised.exception))
        self.assertEqual(run_suite.shard_timeout("0"), 0.0)
        with (
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as raised,
        ):
            run_suite.main(["--fast", "--shard-timeout", "-5"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--shard-timeout", stderr.getvalue())


class ShardReportTests(_TempLogsMixin):
    def test_a_signal_return_code_is_named(self) -> None:
        self.assertEqual(run_suite.describe_returncode(-9), "rc=-9 (SIGKILL)")
        self.assertEqual(run_suite.describe_returncode(-15), "rc=-15 (SIGTERM)")
        self.assertEqual(run_suite.describe_returncode(1), "rc=1")

    def test_the_last_ran_line_is_the_shard_count(self) -> None:
        output = "Ran 7 tests in 0.1s\nOK\n...\nRan 42 tests in 3.0s\nOK\n"

        self.assertEqual(run_suite._tests_ran(output), 42)
        self.assertEqual(run_suite._tests_ran("no summary"), 0)

    def _failed(self, lines: int) -> dict[str, object]:
        return {
            "name": "tests.test_example",
            "command": [sys.executable, "-m", "unittest", "tests.test_example"],
            "returncode": 1,
            "timed_out": False,
            "output": "".join(f"line {number}\n" for number in range(lines)),
        }

    def test_a_long_failure_shows_only_its_tail_unless_verbose(self) -> None:
        result = self._failed(run_suite.FAILED_OUTPUT_LINES + 50)

        with mock.patch.object(run_suite, "_keep_log", return_value=None):
            capped = run_suite.failure_report(result, verbose=False)
        full = run_suite.failure_report(result, verbose=True)

        self.assertNotIn("line 49\n", capped)
        self.assertIn("line 50\n", capped)
        self.assertIn("50 earlier lines omitted", capped)
        self.assertIn("-m unittest tests.test_example", capped)
        self.assertIn("rc=1", capped)
        self.assertIn("line 0\n", full)
        self.assertNotIn("omitted", full)

    def test_a_short_failure_is_shown_whole(self) -> None:
        report = run_suite.failure_report(self._failed(3), verbose=False)

        self.assertIn("line 0", report)
        self.assertNotIn("omitted", report)

    def test_every_failure_keeps_its_full_log(self) -> None:
        # Short or long, verbose or not: CI uploads the log directory from a
        # failed job, so a failure missing from it would read as a pass.
        for lines, verbose in ((3, False), (3, True), (250, False)):
            with self.subTest(lines=lines, verbose=verbose):
                result = self._failed(lines)
                report = run_suite.failure_report(result, verbose=verbose)
                log = run_suite._log_path("tests.test_example")
                self.assertEqual(log.read_text(encoding="utf-8"), result["output"])
                self.assertIn(f"--- full output kept in: {log}", report)
                log.unlink()

    def test_a_passing_shard_clears_its_old_log(self) -> None:
        run_suite._keep_log(self._failed(3))
        log = run_suite._log_path("tests.test_example")
        self.assertTrue(log.is_file())

        run_suite._clear_log({"name": "tests.test_example"})
        run_suite._clear_log({"name": "tests.test_example"})

        self.assertFalse(log.exists())


class ShardSliceTests(unittest.TestCase):
    def _names(self, slice_: tuple[int, int]) -> list[str]:
        return [name for name, _ in run_suite._shard_commands("all", {}, slice_)]

    def test_the_slices_run_every_shard_exactly_once(self) -> None:
        whole = self._names((1, 1))
        sliced = [name for index in (1, 2, 3, 4) for name in self._names((index, 4))]

        self.assertEqual(sorted(sliced), sorted(whole))
        self.assertEqual(len(sliced), len(set(sliced)))

    def test_only_the_first_slice_checks_registration(self) -> None:
        self.assertIn(run_suite.REGISTRATION_SHARD, self._names((1, 3)))
        self.assertNotIn(run_suite.REGISTRATION_SHARD, self._names((2, 3)))
        self.assertNotIn(run_suite.REGISTRATION_SHARD, self._names((3, 3)))

    def test_recorded_durations_do_not_move_a_shard_between_slices(self) -> None:
        names = self._names((1, 1))
        skewed = {name: float(rank) for rank, name in enumerate(names)}

        for index in (1, 2):
            with self.subTest(slice=index):
                unrecorded = self._names((index, 2))
                recorded = [
                    name
                    for name, _ in run_suite._shard_commands("all", skewed, (index, 2))
                ]
                self.assertEqual(sorted(recorded), sorted(unrecorded))

    def test_a_slice_is_k_of_n_counted_from_one(self) -> None:
        self.assertEqual(run_suite.parse_slice("2/4"), (2, 4))
        for value in ("0/4", "5/4", "2", "a/b", "1/0", "/4"):
            with (
                self.subTest(value=value),
                self.assertRaises(run_suite.argparse.ArgumentTypeError),
            ):
                run_suite.parse_slice(value)


if __name__ == "__main__":
    unittest.main()
