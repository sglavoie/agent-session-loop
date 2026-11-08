"""Unlock cases for JsonDoctorUnlockSessionsCliTests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_files,
    state_locks,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
)
from tests.support_asl_cli_json import JsonCliHelpers


class JsonDoctorUnlockSessionsCliTests(JsonCliHelpers, unittest.TestCase):
    def test_unlock_json_success_reports_lock_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "unlock",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("unlock", payload["command"])
            self.assertFalse(payload["had_lock"])
            self.assertFalse(payload["removed"])
            self.assertFalse(payload["active"])
            self.assertFalse(payload["stale"])
            self.assertTrue(payload["lock_path"].endswith("/lock.json"))
            self.assertIn("no lock present", payload["message"])

    def test_unlock_refuses_a_missing_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-plan"
            state_dir = Path(tmp) / "state"

            def _unlock(*extra: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "unlock",
                        str(missing),
                        "--state-dir",
                        str(state_dir),
                        *extra,
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            text = _unlock()
            self.assertEqual(1, text.returncode)
            self.assertEqual("", text.stdout)
            self.assertIn("asl unlock: plan directory does not exist", text.stderr)

            as_json = _unlock("--json")
            self.assertEqual(1, as_json.returncode)
            payload = json.loads(as_json.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("plan-not-found", payload["reason"])
            self.assertIn("plan directory does not exist", payload["error"])
            self.assertFalse(state_dir.exists())

    def test_unlock_refuses_an_empty_plan_argument_instead_of_the_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # The cwd is a real plan, so resolving "" to it would succeed.
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            state_dir = Path(tmp) / "state"
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}

            def _unlock(*extra: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "unlock",
                        "",
                        "--state-dir",
                        str(state_dir),
                        *extra,
                    ],
                    cwd=plan_dir,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            text = _unlock()
            self.assertEqual(1, text.returncode)
            self.assertIn("asl unlock: PLAN_DIR must not be empty", text.stderr)

            as_json = _unlock("--json")
            self.assertEqual(1, as_json.returncode)
            payload = json.loads(as_json.stdout)
            self.assertEqual("input-error", payload["reason"])
            self.assertFalse(state_dir.exists())

    def test_plan_commands_refuse_an_empty_plan_argument_inside_a_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # The cwd is a real plan, so resolving "" to it would succeed.
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            state_dir = Path(tmp) / "state"
            env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
            commands = (
                ("status", "--state-dir", str(state_dir)),
                ("preflight", "--state-dir", str(state_dir)),
                ("baseline",),
                ("contract-check",),
                ("verify", "--session", "1"),
                ("doctor", "--harness", "claude"),
            )
            for command, *extra in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-m", "agent_session_loop", command, ""]
                        + [*extra, "--json"],
                        cwd=plan_dir,
                        env=env,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(1, result.returncode, result.stderr)
                    payload = json.loads(result.stdout)
                    self.assertEqual(command, payload["command"])
                    self.assertEqual("input-error", payload["reason"])
                    self.assertEqual("PLAN_DIR must not be empty", payload["error"])

    def test_unlock_refuses_a_directory_that_is_not_a_split_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_a_plan = Path(tmp) / "empty"
            not_a_plan.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "unlock",
                    str(not_a_plan),
                    "--state-dir",
                    str(Path(tmp) / "state"),
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, completed.returncode)
            self.assertIn("asl unlock: missing context.md", completed.stderr)

    def test_unlock_json_active_lock_failure_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            lock_info = state_locks.acquire_lock(plan_dir, state_dir)

            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "unlock",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                state_locks.release_lock(plan_dir, state_dir)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("unlock", payload["command"])
            self.assertEqual("active-lock", payload["reason"])
            self.assertIn("active lock", payload["error"])
            self.assertEqual(str(lock_info.path), payload["lock_path"])
            self.assertEqual(str(lock_info.path), payload["details"]["lock_path"])
            self.assertEqual(
                str(plans_files.canonicalize_path(plan_dir)),
                payload["details"]["plan_dir"],
            )
