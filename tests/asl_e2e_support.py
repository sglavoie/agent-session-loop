"""Shared helpers and base fixture for the end-to-end QA test modules.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_session_loop import (
    config,
    events,
    state,
)
from tests.support import (
    REPO_ROOT,
    init_git_repo,
    install_fake_codex,
    install_fake_harness,
)

# Completing a child run
# ----------------------
#
# These tests drive the real CLI as a child process. What they assert is *what*
# a run does: which sessions it ran, what its JSON payload says, and what the
# durable run state and event log record afterwards. How long that takes is a
# property of the machine. The suite runs its shards in parallel, so a healthy
# run routinely takes several times its quiet-machine wall clock, and a build
# in another window does the same again.
#
# Encoding those quiet-machine wall clocks as per-call subprocess timeouts made
# contention indistinguishable from a hang: a run that would have completed
# correctly was killed mid-flight and reported as a failure of the code under
# test. So the ceiling below is a hang detector -- generous by design, shared by
# every call -- and completion is decided by two things the run owns: the child
# exiting, and the terminal record the run wrote for itself.
#
# The budgets did not disappear. Set ASL_E2E_TIME_BUDGETS=1 -- as
# `just reliability-stress` does, on an otherwise quiet machine -- to assert
# each call's budget and surface a genuine performance regression.
E2E_HANG_CEILING_SECONDS = 900
E2E_TIME_BUDGET_ENV = "ASL_E2E_TIME_BUDGETS"


def time_budgets_enforced() -> bool:
    """Report whether measured call durations are asserted this run."""
    return os.environ.get(E2E_TIME_BUDGET_ENV) == "1"


def run_to_completion(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    budget_seconds: float,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI child to completion under the shared hang ceiling.

    ``budget_seconds`` is what this call is expected to take on a quiet
    machine. It never bounds the wait -- only ``E2E_HANG_CEILING_SECONDS``
    does -- and is asserted only when time budgets are enforced.
    """
    started = time.monotonic()
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=E2E_HANG_CEILING_SECONDS,
        check=False,
    )
    elapsed = time.monotonic() - started
    if time_budgets_enforced() and elapsed > budget_seconds:
        raise AssertionError(
            f"{label} took {elapsed:.1f}s, over its {budget_seconds}s budget"
        )
    return completed


def assert_run_reached_terminal_state(
    testcase: unittest.TestCase,
    plan_dir: Path,
    state_dir: Path,
) -> dict[str, Any]:
    """Assert the run's own durable state records a finished run.

    This is the state-based half of completion. A child that exited proves the
    process is gone; only ``run_finished_at`` with no turn or verification
    still open proves the run reached its end rather than being cut short, and
    it is the same record every other ASL consumer reads.
    """
    state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
    testcase.assertTrue(state_path.is_file(), f"no run state at {state_path}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    testcase.assertIsNotNone(
        payload.get("run_finished_at"),
        f"run state at {state_path} never recorded run_finished_at: {payload}",
    )
    testcase.assertIsNone(payload.get("active_turn"), payload)
    testcase.assertIsNone(payload.get("active_verification"), payload)
    return payload


class EndToEndQABase(unittest.TestCase):
    """Synthetic end-to-end coverage that wires preflight + run + verify.

    Unit-level coverage already exercises every preflight finding, every
    runner pause reason, and every verify outcome individually. These tests
    intentionally chain those subsystems together through the CLI surface,
    because the loop-readiness contract introduced in this plan only holds
    when the pieces compose correctly. Each test is the integration analog
    of a smoke step from ``docs/agent-session-loop.md``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        self.path_dir = self.tmp_path / "path"
        self.path_dir.mkdir()
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        self.fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(self.fake_codex)},
                    "claude": {"executable": str(self.fake_claude)},
                }
            ),
            encoding="utf-8",
        )

    def _path_value(self, *names: str) -> str:
        for name in names:
            target = self.path_dir / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        return os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])

    def _base_env(self, *, mode: str = "success") -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_dir),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        return env

    def _run_preflight(
        self,
        *,
        mark_loop_ready: bool = False,
        commit_mode: str | None = None,
        path_value: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "preflight",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if commit_mode == "per-session":
            argv.append("--commit-per-session")
        elif commit_mode == "end":
            argv.append("--commit-at-end")
        if mark_loop_ready:
            argv.append("--mark-loop-ready")
        env = self._base_env()
        if path_value is not None:
            env["PATH"] = path_value
        completed = run_to_completion(
            argv,
            cwd=REPO_ROOT,
            env=env,
            budget_seconds=20,
            label="preflight",
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    def _run_run(
        self,
        *,
        allow_not_loop_ready: bool = False,
        max_sessions: int | None = None,
        review_harness: str | None = None,
        max_review_fix_cycles: int | None = None,
        mode: str = "success",
        review_outcomes: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if max_sessions is not None:
            argv.extend(["--max-sessions", str(max_sessions)])
        if review_harness is not None:
            argv.extend(["--review-harness", review_harness])
        if max_review_fix_cycles is not None:
            argv.extend(["--max-review-fix-cycles", str(max_review_fix_cycles)])
        env = self._base_env(mode=mode)
        if review_outcomes is not None:
            env["FAKE_CODEX_REVIEW_OUTCOMES"] = review_outcomes
        completed = run_to_completion(
            argv,
            cwd=REPO_ROOT,
            env=env,
            budget_seconds=20,
            label="run",
        )
        payload = json.loads(completed.stdout)
        if completed.returncode == 0:
            assert_run_reached_terminal_state(self, self.plan_dir, self.state_dir)
        return completed, payload

    def _run_autopilot_review(
        self,
        *,
        max_review_fix_cycles: int,
        review_outcomes: str,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        """Supervise the real CLI through the standalone Autopilot helper."""

        asl_wrapper = self.tmp_path / "asl"
        asl_wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "from agent_session_loop.cli import main\n"
            "raise SystemExit(main())\n",
            encoding="utf-8",
        )
        asl_wrapper.chmod(asl_wrapper.stat().st_mode | stat.S_IXUSR)
        pythonpath = str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")
        command = shlex.join(
            [
                f"XDG_CONFIG_HOME={self.config_home}",
                f"XDG_STATE_HOME={self.state_dir}",
                "FAKE_CODEX_MODE=success-with-review",
                f"FAKE_CODEX_REVIEW_OUTCOMES={review_outcomes}",
                f"PYTHONPATH={pythonpath}",
                str(asl_wrapper),
                "run",
                str(self.plan_dir),
                "--cwd",
                str(self.repo_root),
                "--harness",
                "codex",
                "--review-harness",
                "claude",
                "--max-review-fix-cycles",
                str(max_review_fix_cycles),
                "--state-dir",
                str(self.state_dir),
                "--json",
            ]
        )
        completed = run_to_completion(
            [
                sys.executable,
                str(REPO_ROOT / "skills" / "asl" / "scripts" / "asl-autopilot.py"),
                "--command",
                command,
                "--json",
            ],
            cwd=REPO_ROOT,
            env=self._base_env(mode="success-with-review"),
            budget_seconds=60,
            label="autopilot review",
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        if completed.returncode == 0:
            assert_run_reached_terminal_state(self, self.plan_dir, self.state_dir)
        return completed, payload
