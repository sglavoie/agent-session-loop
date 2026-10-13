#!/usr/bin/env python3
"""Tests for detached autopilot launches."""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import tempfile
import time
import unittest
from contextlib import redirect_stdout, suppress

from asl_autopilot_test_support import (  # noqa: F401
    autopilot,
    setUpModule,
    supervision_liveness,
    tearDownModule,
)

from agent_session_loop import mailbox_vocabulary


class DetachLaunchTest(unittest.TestCase):
    """`--detach` hands supervision to a new session and returns a handshake."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-detach-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan_dir = os.path.join(self.tmp, "plan")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.plan_dir)
        self.command = f"asl run {self.plan_dir} --state-dir {self.state_dir}"
        self.launched: list = []

    def _patch(self, owner, name: str, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _launcher(self, pid: int = 4242):
        def launcher(child, *, stdout_path, stderr_path):
            self.launched.append(
                {
                    "child": child,
                    "stdout_path": stdout_path,
                    "stderr_path": stderr_path,
                }
            )
            return pid

        return launcher

    def _refusing_launcher(self, child, **kwargs):  # pragma: no cover - never runs
        raise AssertionError("the supervision must not be forked away")

    def _main(self, *argv: str, launcher=None, stdin=None) -> tuple:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(
                ["--command", self.command, *argv],
                launcher=self._launcher() if launcher is None else launcher,
                stdin=stdin,
            )
        return exit_code, buffer.getvalue()

    def _log_dir(self, supervision_id: str) -> str:
        return os.path.join(
            autopilot._state.plan_state_dir(self.plan_dir, self.state_dir),
            "supervision",
            supervision_id,
        )

    def test_handshake_is_one_json_object_and_exits_zero(self) -> None:
        exit_code, stdout = self._main("--detach")

        self.assertEqual(0, exit_code)
        self.assertEqual(1, len(stdout.strip().splitlines()))
        payload = json.loads(stdout)
        # Exactly the launch handshake: nothing here is a run verdict, because
        # the run has not produced one yet.
        self.assertEqual(
            {
                "ok",
                "supervision_id",
                "pid",
                "plan_dir",
                "result_path",
                "stderr_path",
            },
            set(payload),
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(4242, payload["pid"])
        self.assertEqual(self.plan_dir, payload["plan_dir"])
        log_dir = self._log_dir(payload["supervision_id"])
        self.assertEqual(os.path.join(log_dir, "result.json"), payload["result_path"])
        self.assertEqual(os.path.join(log_dir, "stderr.log"), payload["stderr_path"])
        self.assertTrue(os.path.isdir(log_dir))
        self.assertEqual(
            (payload["result_path"], payload["stderr_path"]),
            (
                str(self.launched[0]["stdout_path"]),
                str(self.launched[0]["stderr_path"]),
            ),
        )

    def test_supervision_log_dir_overrides_the_default(self) -> None:
        override = os.path.join(self.tmp, "logs", "run-1")
        _, stdout = self._main("--detach", "--supervision-log-dir", override)

        payload = json.loads(stdout)
        # Reported absolute: the handshake is read by processes that do not
        # share this one's working directory.
        resolved = os.path.realpath(override)
        self.assertEqual(os.path.join(resolved, "result.json"), payload["result_path"])
        self.assertEqual(os.path.join(resolved, "stderr.log"), payload["stderr_path"])
        self.assertTrue(os.path.isdir(override))

    def test_relative_supervision_log_dir_is_reported_absolute(self) -> None:
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.tmp)
        _, stdout = self._main("--detach", "--supervision-log-dir", "detached-logs")

        payload = json.loads(stdout)
        self.assertEqual(
            os.path.join(os.path.realpath(self.tmp), "detached-logs", "result.json"),
            payload["result_path"],
        )

    def test_unparseable_command_still_fails_synchronously(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(
                ["--command", f"{self.command} | cat", "--json", "--detach"],
                launcher=self._refusing_launcher,
            )

        payload = json.loads(buffer.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual(autopilot.PARSE_STAGE, payload["stage"])
        self.assertEqual("shell-syntax", payload["reason"])
        self.assertEqual([], self.launched)

    def test_empty_reply_still_fails_synchronously(self) -> None:
        exit_code, stdout = self._main(
            "--json",
            "--detach",
            "--reply-stdin",
            launcher=self._refusing_launcher,
            stdin=io.StringIO("   \n"),
        )

        payload = json.loads(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("empty-reply", payload["reason"])
        self.assertEqual([], self.launched)

    def test_reply_is_read_before_the_fork_and_passed_in_memory(self) -> None:
        captured: dict = {}

        def fake_run_autopilot(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {
                "ok": True,
                "stage": autopilot.RUN_STAGE,
                "reason": "complete",
                "command": autopilot.parse_command(self.command),
            }

        exit_code, _ = self._main(
            "--detach", "--reply-stdin", stdin=io.StringIO("use the cached parser\n")
        )
        self.assertEqual(0, exit_code)

        self._patch(autopilot, "run_autopilot", fake_run_autopilot)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, self.launched[0]["child"]())

        # The fork happens after the read, so the detached supervision resumes
        # from text held in memory rather than from a stdin it cannot reach.
        self.assertEqual("use the cached parser\n", captured["kwargs"]["stdin"].read())
        self.assertTrue(captured["args"].reply_stdin)
        # The result lands in a file named `result.json`, so the detached leg
        # always serializes, and it must not try to detach a second time.
        self.assertTrue(captured["args"].json)
        self.assertFalse(captured["args"].detach)

    def test_failed_fork_reports_a_launch_failure(self) -> None:
        def failing_launcher(child, **kwargs):
            raise OSError("cannot fork")

        exit_code, stdout = self._main("--detach", launcher=failing_launcher)

        payload = json.loads(stdout)
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual(autopilot.LAUNCH_STAGE, payload["stage"])
        self.assertEqual("detach-failed", payload["reason"])
        self.assertIn("cannot fork", payload["error"])


class DetachRealForkTest(unittest.TestCase):
    """One real double fork, end to end, with a trivial fake supervision."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="asl-autopilot-detach-fork-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.plan_dir = os.path.join(self.tmp, "plan")
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.plan_dir)
        self.gate = os.path.join(self.tmp, "gate")
        self.command = f"asl run {self.plan_dir} --state-dir {self.state_dir}"

    def _patch(self, owner, name: str, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def _wait_for(self, produce, message: str, timeout: float = 60.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            produced = produce()
            if produced:
                return produced
            time.sleep(0.05)
        raise AssertionError(message)

    def _reap(self, pid: int) -> None:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)

    def test_parent_returns_while_the_grandchild_keeps_supervising(self) -> None:
        real_run_autopilot = autopilot.run_autopilot

        def supervision(args, **kwargs):
            def gated_supervisor(parsed, **_):
                self._wait_for(
                    lambda: os.path.exists(self.gate),
                    "the launching test never released the supervision",
                )
                return {
                    "ok": True,
                    "stage": autopilot.RUN_STAGE,
                    "reason": "complete",
                    "chunks": 1,
                }

            return real_run_autopilot(args, supervisor=gated_supervisor, **kwargs)

        self._patch(autopilot, "run_autopilot", supervision)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = autopilot.main(["--command", self.command, "--detach"])

        payload = json.loads(buffer.getvalue())
        pid = payload["pid"]
        self.addCleanup(self._reap, pid)
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertNotEqual(os.getpid(), pid)

        # The parent is back with the supervision still gated: the grandchild
        # is alive, and `setsid` put it in a session of its own so nothing aimed
        # at the launching shell's process group can reach it.
        os.kill(pid, 0)
        self.assertNotEqual(os.getpgid(os.getpid()), os.getpgid(pid))

        record = self._wait_for(
            lambda: supervision_liveness.read_record(
                self.plan_dir, payload["supervision_id"], state_dir=self.state_dir
            ),
            "the grandchild never wrote a liveness record",
        )
        self.assertEqual(pid, record.pid)
        self.assertNotEqual(
            supervision_liveness.DEAD, supervision_liveness.probe(record)
        )

        with open(self.gate, "w", encoding="utf-8"):
            pass

        result = self._wait_for(
            lambda: self._read_result(payload["result_path"]),
            "the grandchild never wrote its result",
        )
        self.assertTrue(result["ok"])
        self.assertEqual("complete", result["reason"])
        self.assertEqual(payload["supervision_id"], result["supervision_id"])
        self.assertEqual(0, os.path.getsize(payload["stderr_path"]))
        self._wait_for(
            lambda: self._supervision_end_ids(),
            "the grandchild never marked the end of supervision",
        )
        self.assertEqual(
            [payload["supervision_id"]],
            self._supervision_end_ids(),
        )

    def _read_result(self, path: str):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.loads(handle.read())
        except OSError, ValueError:
            return None

    def _supervision_end_ids(self) -> list:
        view = autopilot._mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        return [
            message.supervision_id
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_SUPERVISION_END
        ]


if __name__ == "__main__":
    unittest.main()
