"""Exercise the stress driver's log retention without running its load suite."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO_ROOT

_PYTHON_DOUBLE = """#!/bin/sh
echo "stdout: $*"
echo "stderr: $*" >&2
case "$STRESS_TEST_OUTCOME" in
    isolated) exit 7 ;;
    signal) kill -TERM "$PPID" ;;
    timezone) [ "${TZ:-}" != "Asia/Kathmandu" ] || exit 8 ;;
    suite) [ "$*" != "-m tests.run_suite --all" ] || exit 9 ;;
    budget) [ "${ASL_E2E_TIME_BUDGETS:-}" != "1" ] || exit 10 ;;
    shard)
        if [ "${TZ:-}" = "Pacific/Chatham" ]; then
            touch .after-timezones
        elif [ -f .after-timezones ] && [ "$*" != "-m tests.run_suite --all" ]; then
            exit 11
        fi ;;
esac
exit 0
"""


class StressLogRetentionTests(unittest.TestCase):
    def run_script(
        self, outcome: str
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="stress driver ") as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            python = bin_dir / "python3"
            python.write_text(_PYTHON_DOUBLE, encoding="utf-8")
            python.chmod(0o755)
            env = {
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "STRESS_TEST_OUTCOME": outcome,
            }
            env.pop("TZ", None)
            env.pop("ASL_E2E_TIME_BUDGETS", None)
            completed = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts/reliability-stress.sh"), "1", "1"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            logs = {
                path.name: path.read_text(encoding="utf-8")
                for path in (root / ".cache/reliability-stress").glob("run.*/*.log")
            }
            return completed, logs

    def test_a_successful_run_cleans_its_logs(self) -> None:
        completed, logs = self.run_script("success")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("reliability-stress: ok", completed.stdout)
        self.assertNotIn("logs retained", completed.stdout)
        self.assertEqual({}, logs)

    def test_a_failed_phase_keeps_both_streams_and_its_failure_status(self) -> None:
        for outcome, status, filename in (
            ("isolated", 1, "isolated-1.log"),
            ("timezone", 1, "tz-Asia-Kathmandu.log"),
            ("shard", 1, "shard-1.log"),
            ("budget", 10, "budgets.log"),
        ):
            with self.subTest(outcome=outcome):
                completed, logs = self.run_script(outcome)

                self.assertEqual(status, completed.returncode, completed.stderr)
                self.assertIn("logs retained at", completed.stdout)
                self.assertIn("stdout: -m unittest", logs[filename])
                self.assertIn("stderr: -m unittest", logs[filename])
                self.assertNotIn("reliability-stress: ok", completed.stdout)

    def test_a_failed_load_suite_retains_evidence_without_failing_the_gate(
        self,
    ) -> None:
        completed, logs = self.run_script("suite")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("background load suite exited 9", completed.stdout)
        self.assertIn("reliability-stress: ok", completed.stdout)
        self.assertIn("logs retained at", completed.stdout)
        self.assertIn("stdout: -m tests.run_suite --all", logs["suite.log"])
        self.assertIn("stderr: -m tests.run_suite --all", logs["suite.log"])
        self.assertIn("budgets.log", logs)
        self.assertIn("shard-1.log", logs)

    def test_an_interrupted_run_keeps_its_signal_status_and_logs(self) -> None:
        completed, logs = self.run_script("signal")

        self.assertEqual(143, completed.returncode, completed.stderr)
        self.assertIn("logs retained at", completed.stdout)
        self.assertIn("stdout: -m unittest", logs["isolated-1.log"])
        self.assertIn("stderr: -m unittest", logs["isolated-1.log"])


if __name__ == "__main__":
    unittest.main()
