"""Behavioral tests for the shared one-shot Spark command."""

from __future__ import annotations

import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SPARK = REPO_ROOT / "bin/.local/bin/spark"

FAKE_CODEX = """#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.argv[1:] == ["app-server", "--stdio"]:
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("method") == "initialize":
            print(json.dumps({"id": request["id"], "result": {}}), flush=True)
        elif request.get("method") == "account/rateLimits/read":
            mode = os.environ.get("FAKE_LIMIT_MODE", "available")
            if mode == "failed":
                print(json.dumps({"id": request["id"],
                    "error": {"message": "failed"}}), flush=True)
                continue
            bucket = {"limitId": "opaque-id", "limitName": "GPT-5.3-Codex-Spark",
                "primary": {"usedPercent": 100 if mode == "blocked" else 20,
                    "resetsAt": 4102444800},
                "secondary": {"usedPercent": 100 if mode == "blocked" else 10,
                    "resetsAt": 4102448400},
                "rateLimitReachedType": None}
            if mode == "reached":
                bucket["rateLimitReachedType"] = "primary"
            if mode == "missing":
                bucket["limitName"] = "Other Model"
            print(json.dumps({"id": request["id"], "result": {
                "rateLimitsByLimitId": {"opaque-id": bucket}}}), flush=True)
    sys.exit(0)

Path(os.environ["FAKE_CAPTURE"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "prompt": sys.stdin.read(),
    "cwd": os.getcwd(),
    "active": os.environ.get("SPARK_HELPER_ACTIVE"),
}), encoding="utf-8")
mode = os.environ.get("FAKE_MODE", "success")
if mode == "wait":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    Path(os.environ["FAKE_CHILD_PID"]).write_text(str(child.pid), encoding="utf-8")
    time.sleep(30)
elif mode == "error":
    print("provider rejected request", file=sys.stderr)
    sys.exit(7)
elif mode == "empty":
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}))
else:
    print(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "Found the target."}}))
    print(json.dumps({"type": "turn.completed", "usage": {
        "input_tokens": 12, "output_tokens": 4}}))
"""


class SparkHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake_dir = self.root / "fake bin"
        self.fake_dir.mkdir()
        fake = self.fake_dir / "codex"
        fake.write_text(FAKE_CODEX, encoding="utf-8")
        fake.chmod(0o755)
        self.capture = self.root / "capture.json"
        self.child_pid = self.root / "child.pid"
        self.work = self.root / "repo with spaces"
        self.work.mkdir()
        self.env = {
            **os.environ,
            "PATH": str(self.fake_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_CAPTURE": str(self.capture),
            "FAKE_CHILD_PID": str(self.child_pid),
            "XDG_STATE_HOME": str(self.root / "state-home"),
        }
        self.env.pop("SPARK_HELPER_ACTIVE", None)

    def run_spark(
        self, *args: str, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SPARK), *args],
            input=input,
            text=True,
            capture_output=True,
            env=self.env,
            cwd=self.root,
            timeout=10,
            check=False,
        )

    def capture_payload(self) -> dict[str, Any]:
        return json.loads(self.capture.read_text(encoding="utf-8"))

    def assert_child_gone(self) -> None:
        pid = int(self.child_pid.read_text(encoding="utf-8"))
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail(f"child process {pid} survived")

    def test_argument_prompt_default_sandbox_and_json_usage(self) -> None:
        prompt = "Find `odd` $(literal) in source"
        completed = self.run_spark("--cwd", str(self.work), "--json", prompt)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"], "Found the target.")
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 4})
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        capture = self.capture_payload()
        self.assertEqual(capture["prompt"], prompt)
        self.assertEqual(capture["cwd"], str(self.work.resolve()))
        self.assertEqual(capture["active"], "1")
        argv = capture["argv"]
        self.assertEqual(argv[:2], ["exec", "--model"])
        self.assertIn("gpt-5.3-codex-spark", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", argv)
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[-1], "-")

    def test_status_reads_named_bucket_and_blocks_without_exec(self) -> None:
        for mode, state, allowed in (
            ("available", "available", True),
            ("blocked", "blocked", False),
            ("reached", "blocked", False),
            ("missing", "unknown", True),
            ("failed", "unknown", True),
        ):
            with self.subTest(mode=mode):
                self.env["FAKE_LIMIT_MODE"] = mode
                result = self.run_spark("--status", "--json")
                payload = json.loads(result.stdout)
                self.assertEqual(state, payload["state"])
                self.assertEqual(allowed, payload["attempt_allowed"])
                self.assertEqual(0 if allowed else 1, result.returncode)
                self.assertEqual("codex-app-server", payload["source"])
                self.assertFalse(self.capture.exists())
                self.assertFalse((self.root / "state-home").exists())
                if mode == "blocked":
                    self.assertEqual("2100-01-01T01:00:00Z", payload["retry_at"])
                if mode == "reached":
                    self.assertEqual("2100-01-01T00:00:00Z", payload["retry_at"])

    def test_blocked_wrapper_never_launches_exec(self) -> None:
        self.env["FAKE_LIMIT_MODE"] = "blocked"
        result = self.run_spark("--json", "Find tests")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("Spark is blocked", json.loads(result.stdout)["error"])
        self.assertFalse(self.capture.exists())

    def test_unknown_live_read_keeps_existing_cooldown(self) -> None:
        ledger_root = self.root / "state-home" / "agent-session-loop"
        ledger_root.mkdir(parents=True)
        (ledger_root / "model-availability.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "models": {
                        "gpt-5.3-codex-spark": {
                            "reason": "quota-exhausted",
                            "retry_at": "2100-01-01T01:00:00Z",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env["FAKE_LIMIT_MODE"] = "missing"
        result = self.run_spark("--status", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual("blocked", payload["state"])
        self.assertEqual("model-availability-ledger", payload["source"])
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.capture.exists())

        self.env["FAKE_LIMIT_MODE"] = "available"
        result = self.run_spark("--status", "--json")
        self.assertEqual("available", json.loads(result.stdout)["state"])
        self.assertEqual(0, result.returncode)

    def test_stdin_edit_and_effort(self) -> None:
        completed = self.run_spark(
            "--edit",
            "--effort",
            "high",
            "--cwd",
            str(self.work),
            "-",
            input="Edit the typo.",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "Found the target.")
        capture = self.capture_payload()
        self.assertEqual(capture["prompt"], "Edit the typo.")
        argv = capture["argv"]
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_implicit_stdin_prompt(self) -> None:
        completed = self.run_spark(input="Locate reconnect tests.")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.capture_payload()["prompt"], "Locate reconnect tests.")

    def test_multiple_argument_words_are_prompt_data(self) -> None:
        completed = self.run_spark(
            "Locate the tests", "src/a file.py", "`literal`", "$(literal)"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.capture_payload()["prompt"],
            "Locate the tests src/a file.py `literal` $(literal)",
        )

    def test_recursion_and_invalid_input_never_start_codex(self) -> None:
        self.env["SPARK_HELPER_ACTIVE"] = "1"
        result = self.run_spark("--json", "Find tests")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recursive", json.loads(result.stdout)["error"])
        self.assertFalse(self.capture.exists())
        del self.env["SPARK_HELPER_ACTIVE"]
        for args in (("--json", ""), ("--json", "--timeout", "0", "Find tests")):
            with self.subTest(args=args):
                result = self.run_spark(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_failed_and_empty_responses_are_errors(self) -> None:
        for mode, expected in (
            ("error", "provider rejected request"),
            ("empty", "no final answer"),
        ):
            with self.subTest(mode=mode):
                self.env["FAKE_MODE"] = mode
                result = self.run_spark("--json", "Find tests")
                self.assertNotEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertFalse(payload["ok"])
                self.assertIn(expected, payload["error"])

    def test_timeout_stops_descendant(self) -> None:
        self.env["FAKE_MODE"] = "wait"
        result = self.run_spark("--timeout", "1", "Find tests")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assert_child_gone()

    def test_cancellation_stops_descendant(self) -> None:
        self.env["FAKE_MODE"] = "wait"
        process = subprocess.Popen(
            [sys.executable, str(SPARK), "Find tests"],
            cwd=self.root,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(100):
                if self.child_pid.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(self.child_pid.exists())
            process.send_signal(signal.SIGTERM)
            _out, err = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("cancelled", err)
            self.assert_child_gone()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_stop_tree_never_signals_a_reaped_process_group(self) -> None:
        stop_tree = runpy.run_path(str(SPARK), run_name="spark_helper_test")[
            "_stop_tree"
        ]

        class FakeProcess:
            pid = 42_000

            def __init__(self, *, exits_during_grace_period: bool) -> None:
                self.exits_during_grace_period = exits_during_grace_period
                self.wait_timeouts: list[float | None] = []

            def wait(self, timeout: float | None = None) -> int:
                self.wait_timeouts.append(timeout)
                if timeout is not None and not self.exits_during_grace_period:
                    raise subprocess.TimeoutExpired("spark", timeout)
                return 0

        reaped_process = FakeProcess(exits_during_grace_period=True)
        reaped_signals: list[signal.Signals] = []

        def reject_recycled_group(_pid: int, signum: signal.Signals) -> None:
            reaped_signals.append(signum)
            if signum == signal.SIGKILL:
                raise AssertionError("signalled the reaped process group")

        with mock.patch("os.killpg", side_effect=reject_recycled_group):
            stop_tree(reaped_process)
        self.assertEqual(reaped_signals, [signal.SIGTERM])
        self.assertEqual(reaped_process.wait_timeouts, [0.5])

        running_process = FakeProcess(exits_during_grace_period=False)
        running_signals: list[signal.Signals] = []

        def lose_process_group(_pid: int, signum: signal.Signals) -> None:
            running_signals.append(signum)
            if signum == signal.SIGKILL:
                raise PermissionError("process group was recycled")

        with mock.patch("os.killpg", side_effect=lose_process_group):
            stop_tree(running_process)
        self.assertEqual(running_signals, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(running_process.wait_timeouts, [0.5, None])


if __name__ == "__main__":
    unittest.main()
