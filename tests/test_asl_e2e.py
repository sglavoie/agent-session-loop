"""End-to-end QA, verification repair loop, and acceptance tests.

Split from tests/test_agent_session_loop.py."""

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
    plans,
    prepare,
    review,
    runner,
    state,
    task_packet,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    KILN_TWIN_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    _write_loop_ready_plan,
    git_stdout,
    init_git_repo,
    init_multi_repo_workspace,
    install_fake_codex,
    install_fake_harness,
    read_jsonl,
    write_multi_repo_plan,
    write_plan,
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


class EndToEndQATests(unittest.TestCase):
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

    def test_completed_review_reopens_and_rereviews_through_cli(self) -> None:
        """A higher absolute cap resumes the persisted review in a new run."""

        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        outcomes = "findings,all-clear"

        first, first_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=0,
            mode="success-with-review",
            review_outcomes=outcomes,
        )
        # The zero cap leaves the finding open, so the first run reports it.
        self.assertEqual(1, first.returncode, first.stderr)
        self.assertEqual(
            ["1", "2"],
            [turn["session_id"] for turn in first_payload["turns"]],
        )
        self.assertEqual(
            "higher-cap-required", first_payload["review_resume"]["blocker"]
        )

        second, second_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=1,
            mode="success-with-review",
            review_outcomes=outcomes,
        )

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(
            ["2.1", "3"],
            [turn["session_id"] for turn in second_payload["turns"]],
        )
        self.assertEqual("all-clear", second_payload["review_fix_disposition"])
        self.assertEqual("terminal", second_payload["review_resume"]["action"])
        reopen = second_payload["review_reopen"]
        self.assertTrue(reopen["materialized"])
        self.assertEqual("2", reopen["source_review_session_id"])
        self.assertEqual(["2.1"], reopen["repair_session_ids"])
        self.assertTrue((self.plan_dir / "session-3.md").is_file())

    def test_autopilot_reopens_exact_legacy_review_history_after_normalization(
        self,
    ) -> None:
        """Legacy not-loop-ready + digestless malformed history resumes."""

        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        seeded, seeded_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=1,
            mode="success-with-review",
            review_outcomes="findings,all-clear",
        )
        self.assertEqual(0, seeded.returncode, seeded.stderr)
        self.assertEqual(
            ["1", "2", "2.1", "3"],
            [turn["session_id"] for turn in seeded_payload["turns"]],
        )

        # Reproduce the motivating persisted history: the first source did
        # materialize a child but stopped not-loop-ready, and the later source
        # recorded malformed repair output before artifact digests existed.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        records = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        cycles = [
            record["payload"]
            for record in records
            if record["name"] == events.REVIEW_CYCLE_EVENT
        ]
        self.assertEqual(2, len(cycles))
        cycles[0]["disposition"] = "not-loop-ready"
        cycles[0].pop("review_output_sha256", None)
        cycles[0].pop("review_artifact_sha256", None)
        cycles[1].update(
            {
                "outcome": "findings",
                "disposition": "malformed-review-repair",
                "fingerprints": ["finding:legacy-nine-groups"],
                "introduced_findings": ["finding:legacy-nine-groups"],
                "closed_findings": [],
                "repeated_findings": [],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            }
        )
        cycles[1].pop("review_output_sha256", None)
        cycles[1].pop("review_artifact_sha256", None)
        event_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

        findings = []
        for index in range(1, 10):
            findings.append(
                {
                    "id": f"legacy-{index}",
                    "severity": "high",
                    "title": f"Legacy finding {index}",
                    "evidence": [
                        {
                            "path": f"agent_session_loop/legacy_{index}.py:{index}",
                            "symbol": f"target_{index}",
                        }
                    ],
                    "explanation": "Compatibility fixture finding.",
                    "repair_group": f"group-{index}",
                    "done_criteria": ["The compatibility defect is fixed."],
                    "verification_checks": [
                        {
                            "type": "command",
                            "name": f"legacy-check-{index}",
                            "argv": ["python3", "-c", "pass"],
                        }
                    ],
                }
            )
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": findings},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- Legacy review repair grouping requires normalization.\n"
            "Status: 9 Findings (9 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )

        invalid, invalid_payload = self._run_autopilot_review(
            max_review_fix_cycles=2,
            review_outcomes="findings,all-clear,all-clear",
        )
        self.assertEqual(1, invalid.returncode, invalid.stderr)
        self.assertEqual("review_findings", invalid_payload["reason"])
        self.assertEqual(0, invalid_payload["chunks"])
        self.assertEqual(
            "artifact-invalid", invalid_payload["review_resume"]["blocker"]
        )
        self.assertIn(
            "exceed the maximum of 8",
            invalid_payload["review_resume"]["artifact_error"],
        )
        self.assertEqual(
            1, invalid_payload["review_resume"]["historical_materializations"]
        )

        # Artifact correction is external. Preserve all nine findings while
        # normalizing them from nine repair groups to the supported eight.
        findings[-1]["repair_group"] = "group-8"
        artifact_path.write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": findings},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        resumed, resumed_payload = self._run_autopilot_review(
            max_review_fix_cycles=2,
            review_outcomes="findings,all-clear,all-clear",
        )
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertTrue(resumed_payload["ok"])
        self.assertEqual("complete", resumed_payload["reason"])
        self.assertEqual(1, resumed_payload["chunks"])
        self.assertEqual("all-clear", resumed_payload["review"]["outcome"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        for index in range(1, 9):
            child = self.plan_dir / f"session-3.{index}.md"
            self.assertTrue(child.is_file(), child)
            self.assertIn("- **Status**: complete", child.read_text(encoding="utf-8"))
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-4.md").read_text(encoding="utf-8"),
        )

        final_records = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        reopen = [
            record["payload"]
            for record in final_records
            if record["name"] == events.REVIEW_REPAIR_REOPENED_EVENT
            and record["payload"].get("materialized") is True
            and record["payload"].get("source_review_session_id") == "3"
        ]
        self.assertEqual(1, len(reopen))
        self.assertEqual(1, reopen[0]["historical_materializations_before"])
        self.assertEqual(2, reopen[0]["historical_materializations_after"])
        self.assertIsNone(reopen[0]["source_review_output_sha256"])
        self.assertIsNone(reopen[0]["source_review_artifact_sha256"])
        self.assertEqual(
            [f"3.{index}" for index in range(1, 9)],
            reopen[0]["repair_session_ids"],
        )

    def _write_legacy_plan(self) -> None:
        """A pre-loop-readiness plan: no manifest readiness, no contract."""
        write_plan(self.plan_dir, ("pending",))

    def _repair_legacy_plan_to_draft(self) -> None:
        """Repair legacy content while leaving readiness for preflight to mark."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="draft",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_full_lifecycle_repairs_legacy_plan_and_runs_to_completion(
        self,
    ) -> None:
        """Legacy plan → preflight red → repair → mark → run → verify pass."""
        self._write_legacy_plan()
        path_value = self._path_value("codex", "spec", "amx")

        # Step 1: preflight surfaces red findings on the legacy plan.
        before, before_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, before.returncode)
        self.assertFalse(before_payload["ok"])
        before_categories = {f["category"] for f in before_payload["findings"]}
        self.assertIn("manifest-readiness", before_categories)
        self.assertIn("missing-contract", before_categories)
        # Repair hints stay aligned with findings 1:1 for the guided skill.
        self.assertEqual(
            len(before_payload["findings"]),
            len(before_payload["repair_hints"]),
        )

        # Step 2: repair (add contract, write draft readiness).
        self._repair_legacy_plan_to_draft()
        readiness_before_mark = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("draft", readiness_before_mark.state)

        # Step 3: --mark-loop-ready promotes a clean draft plan.
        marked, marked_payload = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, marked.returncode, marked.stderr)
        self.assertTrue(marked_payload["ok"])
        self.assertEqual("loop-ready-marked", marked_payload["reason"])
        self.assertTrue(marked_payload["marked_loop_ready"])
        readiness_after_mark = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness_after_mark)
        self.assertEqual("loop-ready", readiness_after_mark.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            readiness_after_mark.readiness_sha256,
        )
        self.assertNotEqual(readiness_before_mark.state, readiness_after_mark.state)

        # Step 4: asl run completes the plan with verification passing.
        run_completed, run_payload = self._run_run()
        self.assertEqual(0, run_completed.returncode, run_completed.stderr)
        self.assertTrue(run_payload["ok"])
        self.assertFalse(run_payload["paused"])
        self.assertEqual("all-complete", run_payload["reason"])
        self.assertEqual("complete", run_payload["readiness_state_final"])
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", session_text)
        verify_payload = run_payload["turns"][0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verification_failure_pauses_then_resumes_after_repair(self) -> None:
        """`verify-failed` reverts status; re-running after repair completes."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "later.txt", "name": "later"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        # First run: verify fails, status reverts to pending.
        first_completed, first_payload = self._run_run()
        self.assertEqual(1, first_completed.returncode)
        self.assertTrue(first_payload["paused"])
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        pause_details = first_payload["pause_details"]
        self.assertEqual(["later"], pause_details["recipe"]["names"])
        self.assertEqual("file-missing", pause_details["failures"][0]["failure_reason"])
        session_path = self.plan_dir / "session-1.md"
        session_text = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        self.assertIn("verification failed", session_text)

        # A verify-failed pause flips readiness to `reviewed`; after the
        # underlying issue is repaired, preflight can re-mark the plan.
        reviewed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", reviewed_metadata.state)

        # Repair: create the missing file so the contract will now pass.
        (self.repo_root / "later.txt").write_text("done\n", encoding="utf-8")

        remark_completed, remark_payload = self._run_preflight(mark_loop_ready=True)
        self.assertEqual(0, remark_completed.returncode, remark_completed.stderr)
        self.assertTrue(remark_payload["marked_loop_ready"])
        self.assertEqual("loop-ready", remark_payload["readiness"]["state"])

        # Second run: the still-pending session is picked up and completes.
        second_completed, second_payload = self._run_run()
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertFalse(second_payload["paused"])
        self.assertEqual("all-complete", second_payload["reason"])
        final_session = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", final_session)
        # Readiness lifecycle resolves to complete after the resumed run.
        final_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", final_metadata.state)

    def test_stale_lock_surfaces_in_preflight_cli_with_repair_hint(
        self,
    ) -> None:
        """A stale lock JSON-fingerprints to env-lock.stale via the CLI."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        lock_findings = [f for f in payload["findings"] if f["category"] == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0]["severity"])
        self.assertIn("stale lock", lock_findings[0]["message"])
        # Each finding pairs with a stable repair hint check id.
        lock_hints = [
            hint
            for hint, finding in zip(
                payload["repair_hints"], payload["findings"], strict=False
            )
            if finding["category"] == "env-lock"
        ]
        self.assertEqual(1, len(lock_hints))
        self.assertEqual("env-lock.stale", lock_hints[0]["check_id"])

    def test_commit_mode_dirty_worktree_emits_yellow_via_preflight_cli(
        self,
    ) -> None:
        """`--commit-per-session` plus a dirty worktree surfaces env-git yellow."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        (self.repo_root / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        git_findings = [f for f in payload["findings"] if f["category"] == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0]["severity"])
        self.assertIn("dirty", git_findings[0]["message"])
        # Marking loop-ready must be refused while yellow findings remain.
        completed_mark, payload_mark = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(1, completed_mark.returncode)
        self.assertFalse(payload_mark["marked_loop_ready"])

    def test_json_contracts_share_stable_keys_across_preflight_and_verify(
        self,
    ) -> None:
        """The JSON shape published in the docs must hold at the CLI surface."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        path_value = self._path_value("codex", "spec", "amx")

        # Preflight JSON: documented top-level keys.
        preflight_completed, preflight_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(0, preflight_completed.returncode)
        for key in (
            "ok",
            "command",
            "reason",
            "findings",
            "findings_by_severity",
            "repair_hints",
            "readiness",
            "expected_readiness_sha256",
            "mark_loop_ready_requested",
            "marked_loop_ready",
        ):
            self.assertIn(key, preflight_payload)
        self.assertEqual("preflight", preflight_payload["command"])

        # Verify JSON: documented top-level keys including stable reason enum.
        verify_argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(self.plan_dir),
            "--session",
            "1",
            "--cwd",
            str(self.repo_root),
            "--json",
        ]
        verify_completed = run_to_completion(
            verify_argv,
            cwd=REPO_ROOT,
            env=self._base_env(),
            budget_seconds=20,
            label="verify",
        )
        self.assertEqual(0, verify_completed.returncode, verify_completed.stderr)
        verify_payload = json.loads(verify_completed.stdout)
        for key in (
            "ok",
            "command",
            "reason",
            "passed",
            "recipe",
            "checks",
            "session",
            "cwd",
            "plan_dir",
        ):
            self.assertIn(key, verify_payload)
        self.assertEqual("verify", verify_payload["command"])
        self.assertEqual("ok", verify_payload["reason"])


class MultiRepositoryEndToEndQATests(unittest.TestCase):
    """Public-CLI acceptance matrix for a four-repository workspace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.config_home = self.tmp_path / "config"
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

    def _env(self, **updates: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "PATH": str(self.tmp_path) + os.pathsep + env.get("PATH", ""),
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                "FAKE_CODEX_MODE": "success",
                **updates,
            }
        )
        return env

    def _cli(
        self,
        *args: str,
        env_updates: dict[str, str] | None = None,
        budget_seconds: float = 60,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        completed = run_to_completion(
            [sys.executable, "-m", "agent_session_loop", *args],
            cwd=REPO_ROOT,
            env=self._env(**(env_updates or {})),
            budget_seconds=budget_seconds,
            label=f"cli {args[0] if args else ''}".strip(),
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    @staticmethod
    def _source_text(*, first_writes: tuple[str, ...]) -> str:
        registry_rows = "\n".join(
            f"| {alias} | {path} | {origin} |"
            for alias, path, origin in DEFAULT_MULTI_REPO_SPECS
        )
        aliases = ", ".join(alias for alias, _path, _origin in DEFAULT_MULTI_REPO_SPECS)
        writes = ", ".join(first_writes) if first_writes else "(none)"
        return (
            "# Four Repository E2E Plan\n\n"
            "Shared public-CLI acceptance context.\n\n"
            "## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{registry_rows}\n\n"
            "## Session 1: Multi-write implementation\n\n"
            f"**Reads repositories**: {aliases}\n"
            f"**Writes repositories**: {writes}\n\n"
            "Exercise qualified paths across the registered workspace.\n\n"
            "## Session 2: Read-only validation\n\n"
            f"**Reads repositories**: {aliases}\n"
            "**Writes repositories**: (none)\n\n"
            "Validate the completed workspace without mutating repositories.\n"
        )

    @staticmethod
    def _contracts() -> dict[str, Any]:
        first_checks: list[dict[str, Any]] = []
        for number, (alias, relative_path, _origin) in enumerate(
            DEFAULT_MULTI_REPO_SPECS, start=1
        ):
            first_checks.append(
                {
                    "name": f"repo-cwd-{number}",
                    "type": "command",
                    "repo": alias,
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "raise SystemExit(Path.cwd().name != "
                            f"{relative_path!r})"
                        ),
                    ],
                    "timeout_seconds": 30,
                    "baseline_expectation": "pass",
                }
            )
        return {
            "1": {
                "done_when": ["All registered command checks pass."],
                "verify": {"session": "1", "checks": first_checks},
            },
            "2": {
                "done_when": ["Read-only repository evidence is valid."],
                "verify": {
                    "session": "2",
                    "checks": [
                        {
                            "name": "kiln-readme",
                            "type": "file_exists",
                            "repo": "Kiln",
                            "path": "README.md",
                            "baseline_expectation": "pass",
                        }
                    ],
                },
            },
        }

    def _validation_payload(self, plan_dir: Path, workspace: Path) -> dict:
        packet = prepare.build_validation_packet(
            plan_dir,
            ("1", "2"),
            codebase_root=workspace,
        )
        sessions = []
        for session_id, alias in (("1", "SVC"), ("2", "Kiln")):
            sessions.append(
                {
                    "session_id": session_id,
                    "title": (
                        "Multi-write implementation"
                        if session_id == "1"
                        else "Read-only validation"
                    ),
                    "claims": [
                        {
                            "claim": "README.md exists",
                            "claim_type": "P",
                            "location": "README.md",
                            "line": 1,
                            "to_be_created": False,
                            "repo": alias,
                        }
                    ],
                    "issues": [],
                    "details": [],
                    "patterns": [],
                    "dependencies": [],
                    "contract_checked": True,
                    "contract_problems": [],
                    "corrections": [],
                }
            )
        return {
            "version": prepare.PREPARE_VALIDATION_VERSION,
            "workspace_root": str(workspace.resolve()),
            "repositories": [repo.to_dict() for repo in packet.repositories],
            "sessions": sessions,
        }

    def _prepare_case(
        self,
        name: str,
        *,
        first_writes: tuple[str, ...] = ("SVC", "OAK", "OWL"),
        dirty_registered: bool = False,
        dirty_unregistered: bool = False,
    ) -> tuple[Path, Path, Path, dict[str, Path], Path]:
        case_dir = self.tmp_path / name
        case_dir.mkdir()
        workspace = case_dir / "workspace"
        repositories = init_multi_repo_workspace(workspace, DEFAULT_MULTI_REPO_SPECS)
        unregistered = workspace / "unregistered"
        init_git_repo(unregistered)
        if dirty_registered:
            (repositories["OAK"] / "preexisting.txt").write_text(
                "registered dirty state\n", encoding="utf-8"
            )
        if dirty_unregistered:
            (unregistered / "scratch.txt").write_text(
                "unregistered dirty state\n", encoding="utf-8"
            )
        self.assertFalse((workspace / ".git").exists())

        source = case_dir / "plan.md"
        source.write_text(
            self._source_text(first_writes=first_writes), encoding="utf-8"
        )
        contracts_path = case_dir / "contracts.json"
        contracts_path.write_text(json.dumps(self._contracts()), encoding="utf-8")
        split, split_payload = self._cli(
            "split",
            "--plan-file",
            str(source),
            "--contracts-file",
            str(contracts_path),
            "--base-dir",
            str(case_dir / "plans"),
            "--skill-prefix",
            "codex",
            "--json",
        )
        self.assertEqual(0, split.returncode, split.stderr or split.stdout)
        self.assertEqual(
            ["SVC", "OAK", "OWL", "Kiln"],
            [row["alias"] for row in split_payload["repositories"]],
        )
        plan_dir = Path(str(split_payload["output_dir"]))

        batch_path = case_dir / "validation.json"
        batch_path.write_text(
            json.dumps(self._validation_payload(plan_dir, workspace)),
            encoding="utf-8",
        )
        ingest, ingest_payload = self._cli(
            "expand-ingest",
            str(plan_dir),
            "--batch-file",
            str(batch_path),
            "--json",
        )
        self.assertEqual(0, ingest.returncode, ingest.stderr or ingest.stdout)
        self.assertEqual(["1", "2"], ingest_payload["sessions"])

        state_dir = case_dir / "state"
        prepared, prepared_payload = self._cli(
            "prepare",
            str(plan_dir),
            "--resume",
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--validator-harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            budget_seconds=120,
        )
        self.assertEqual(0, prepared.returncode, prepared.stderr or prepared.stdout)
        self.assertTrue(prepared_payload["marked_loop_ready"])
        self.assertTrue((plan_dir / "session-1.expand.md").is_file())
        self.assertTrue((plan_dir / "session-2.expand.md").is_file())
        return workspace, plan_dir, state_dir, repositories, unregistered

    def _run_plan(
        self,
        plan_dir: Path,
        workspace: Path,
        state_dir: Path,
        *extra: str,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        return self._cli(
            "run",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            *extra,
            env_updates=env_updates,
            budget_seconds=120,
        )

    @staticmethod
    def _plan_file_bytes(plan_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(plan_dir).as_posix(): path.read_bytes()
            for path in sorted(plan_dir.rglob("*"))
            if path.is_file()
        }

    def _rebind_plan(
        self,
        plan_dir: Path,
        workspace: Path,
        state_dir: Path,
        *,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        return self._cli(
            "prepare",
            str(plan_dir),
            "--resume",
            "--cwd",
            str(workspace),
            "--rebind-workspace",
            "--harness",
            "codex",
            "--validator-harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            env_updates=env_updates,
            budget_seconds=120,
        )

    def test_four_repository_public_core_lifecycle(self) -> None:
        workspace, plan_dir, state_dir, repositories, unregistered = self._prepare_case(
            "core", dirty_unregistered=True
        )

        baseline_completed, baseline_payload = self._cli(
            "baseline",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--json",
        )
        self.assertEqual(0, baseline_completed.returncode, baseline_completed.stderr)
        self.assertEqual(5, baseline_payload["check_count"])
        self.assertEqual(
            ["SVC", "OAK", "OWL", "Kiln", "Kiln"],
            [check["repo"] for check in baseline_payload["checks"]],
        )

        preflight_completed, preflight_payload = self._cli(
            "preflight",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
        )
        self.assertEqual(0, preflight_completed.returncode, preflight_completed.stderr)
        self.assertTrue(preflight_payload["ok"])

        dry_completed, dry_payload = self._run_plan(
            plan_dir, workspace, state_dir, "--dry-run"
        )
        self.assertEqual(0, dry_completed.returncode, dry_completed.stderr)
        self.assertEqual(str(workspace.resolve()), dry_payload["workspace_root"])
        self.assertIn(
            "Writable aliases: SVC, OAK, OWL", dry_payload["planned"]["prompt"]
        )

        mutations = [
            {
                "path": str(repositories[alias] / "session-1.txt"),
                "text": f"{alias}\n",
            }
            for alias in ("SVC", "OAK", "OWL")
        ]
        run_completed, run_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {
                        "1": [
                            "SVC:session-1.txt",
                            "OAK:session-1.txt",
                            "OWL:session-1.txt",
                        ],
                        "2": [],
                    }
                ),
            },
        )
        self.assertEqual(
            0, run_completed.returncode, run_completed.stderr or run_completed.stdout
        )
        self.assertEqual("all-complete", run_payload["reason"])
        self.assertEqual([1, 2], run_payload["completed_sessions"])
        self.assertTrue(run_payload["turns"][0]["workspace_write_guard"]["passed"])
        self.assertEqual(
            ["SVC", "OAK", "OWL"],
            run_payload["turns"][0]["workspace_write_guard"]["authorized_aliases"],
        )
        read_only_guard = run_payload["turns"][1]["workspace_write_guard"]
        self.assertTrue(read_only_guard["passed"])
        self.assertEqual([], read_only_guard["write_aliases"])
        self.assertTrue((unregistered / "scratch.txt").is_file())

        for session_id in ("1", "2"):
            verified, verify_payload = self._cli(
                "verify",
                str(plan_dir),
                "--session",
                session_id,
                "--cwd",
                str(workspace),
                "--json",
            )
            self.assertEqual(0, verified.returncode, verified.stderr)
            self.assertTrue(verify_payload["passed"])

    def test_dirty_registered_preflight_and_workspace_guard_matrix(self) -> None:
        workspace, plan_dir, state_dir, _repositories, unregistered = (
            self._prepare_case(
                "dirty-preflight",
                dirty_registered=True,
                dirty_unregistered=True,
            )
        )
        clean_mode, clean_payload = self._cli(
            "preflight",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
        )
        self.assertEqual(0, clean_mode.returncode, clean_mode.stderr)
        self.assertTrue(clean_payload["ok"])

        commit_mode, commit_payload = self._cli(
            "preflight",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--commit-per-session",
            "--state-dir",
            str(state_dir),
            "--json",
        )
        self.assertEqual(1, commit_mode.returncode)
        dirty_messages = "\n".join(
            finding["message"]
            for finding in commit_payload["findings"]
            if finding["category"] == "env-git"
        )
        self.assertIn("OAK", dirty_messages)
        self.assertNotIn(unregistered.name, dirty_messages)

        mutation_cases = (
            (
                "tracked",
                lambda repos, _workspace, _unregistered: {
                    "path": str(repos["OAK"] / "README.md"),
                    "text": "tracked unauthorized\n",
                },
                "OAK:README.md",
            ),
            (
                "staged",
                lambda repos, _workspace, _unregistered: {
                    "path": str(repos["OAK"] / "staged.txt"),
                    "action": "stage",
                },
                "OAK:staged.txt",
            ),
            (
                "untracked",
                lambda repos, _workspace, _unregistered: {
                    "path": str(repos["OAK"] / "untracked.txt"),
                },
                "OAK:untracked.txt",
            ),
            (
                "repository-removed",
                lambda repos, case_workspace, _unregistered: {
                    "path": str(repos["Kiln"]),
                    "action": "rename",
                    "destination": str(case_workspace.parent / "retained"),
                },
                "repository-removed",
            ),
        )
        for case_name, mutation_factory, expected in mutation_cases:
            with self.subTest(case=case_name):
                (
                    case_workspace,
                    case_plan,
                    case_state,
                    case_repositories,
                    case_unregistered,
                ) = self._prepare_case(f"guard-{case_name}", first_writes=("SVC",))
                mutation = mutation_factory(
                    case_repositories, case_workspace, case_unregistered
                )
                completed, payload = self._run_plan(
                    case_plan,
                    case_workspace,
                    case_state,
                    "--max-sessions",
                    "1",
                    env_updates={"FAKE_CODEX_MUTATIONS_JSON": json.dumps([mutation])},
                )
                self.assertEqual(1, completed.returncode, completed.stderr)
                self.assertEqual(
                    events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                    payload["pause_reason"],
                )
                guard = payload["turns"][0]["workspace_write_guard"]
                self.assertFalse(guard["passed"])
                if expected.startswith("OAK:"):
                    self.assertEqual(
                        "read-only-repository", guard["violations"][0]["kind"]
                    )
                    self.assertIn(expected, guard["violations"][0]["qualified_paths"])
                    status = git_stdout(
                        case_repositories["OAK"], "status", "--porcelain"
                    )
                    self.assertIn(Path(str(mutation["path"])).name, status)
                else:
                    self.assertEqual(expected, guard["violations"][0]["kind"])
                    retained = Path(str(mutation["destination"]))
                    self.assertTrue((retained / ".git").is_dir())
                self.assertIn(
                    "- **Status**: pending",
                    (case_plan / "session-1.md").read_text(encoding="utf-8"),
                )

    def test_per_session_and_end_commit_modes_use_manifest_order(self) -> None:
        for commit_flag, scope in (
            ("--commit-per-session", "session"),
            ("--commit-at-end", "end"),
        ):
            with self.subTest(commit_flag=commit_flag):
                name = commit_flag.removeprefix("--")
                workspace, plan_dir, state_dir, repositories, _unregistered = (
                    self._prepare_case(name)
                )
                commit_log = self.tmp_path / f"{name}.jsonl"
                mutations = [
                    {
                        "path": str(repositories[alias] / "committed.txt"),
                        "text": f"{alias}\n",
                    }
                    for alias in ("SVC", "OAK", "OWL")
                ]
                completed, payload = self._run_plan(
                    plan_dir,
                    workspace,
                    state_dir,
                    commit_flag,
                    env_updates={
                        "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                        "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                            {
                                "1": [
                                    "SVC:committed.txt",
                                    "OAK:committed.txt",
                                    "OWL:committed.txt",
                                ],
                                "2": [],
                            }
                        ),
                        "FAKE_CODEX_COMMIT_LOG": str(commit_log),
                    },
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(
                    ["SVC", "OAK", "OWL"],
                    [commit["repository_alias"] for commit in payload["commits"]],
                )
                group = next(
                    group
                    for group in payload["commit_groups"]
                    if group["ordered_aliases"]
                )
                self.assertEqual(scope, group["scope"])
                self.assertEqual(["SVC", "OAK", "OWL"], group["ordered_aliases"])
                self.assertEqual(
                    [
                        str(repositories[alias].resolve())
                        for alias in ("SVC", "OAK", "OWL")
                    ],
                    [record["cwd"] for record in read_jsonl(commit_log)],
                )

    def test_partial_commit_failure_recovers_in_a_new_process(self) -> None:
        workspace, plan_dir, state_dir, repositories, _unregistered = (
            self._prepare_case("partial-commit")
        )
        commit_log = self.tmp_path / "partial-commit.jsonl"
        mutations = [
            {
                "path": str(repositories[alias] / "partial.txt"),
                "text": f"{alias}\n",
            }
            for alias in ("SVC", "OAK", "OWL")
        ]
        first, first_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--commit-per-session",
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {
                        "1": [
                            "SVC:partial.txt",
                            "OAK:partial.txt",
                            "OWL:partial.txt",
                        ]
                    }
                ),
                "FAKE_CODEX_COMMIT_LOG": str(commit_log),
                "FAKE_CODEX_COMMIT_FAIL_CWD_BASENAME": repositories["OAK"].name,
            },
        )
        # The refused commit no longer stops the run; it leaves the group owed
        # and says so, which is what the next process recovers from.
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertFalse(first_payload["paused"])
        self.assertEqual(
            ["OAK"],
            [
                summary["failed_alias"]
                for summary in first_payload["unresolved_commit_groups"]
            ],
        )
        failed_group = first_payload["commit_groups"][0]
        self.assertEqual("OAK", failed_group["failed_alias"])
        self.assertEqual(["OAK", "OWL"], failed_group["remaining_aliases"])
        self.assertEqual(
            ["SVC"],
            [row["alias"] for row in failed_group["successful_repositories"]],
        )
        svc_head = git_stdout(repositories["SVC"], "rev-parse", "HEAD")

        dry, dry_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--resume",
            "--dry-run",
            env_updates={"FAKE_CODEX_COMMIT_LOG": str(commit_log)},
        )
        self.assertEqual(0, dry.returncode, dry.stderr)
        self.assertTrue(dry_payload["commit_group_recovery"]["pending"])

        resumed, resumed_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--resume",
            env_updates={"FAKE_CODEX_COMMIT_LOG": str(commit_log)},
        )
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertTrue(resumed_payload["commit_group_recovery"]["success"])
        # SVC committed once and OAK refused three times in the first run —
        # the commit itself plus the two in-run retries the group is worth —
        # before the second process completed the group it was still owed.
        self.assertEqual(
            [
                repositories["SVC"].name,
                repositories["OAK"].name,
                repositories["OAK"].name,
                repositories["OAK"].name,
                repositories["OAK"].name,
                repositories["OWL"].name,
            ],
            [Path(record["cwd"]).name for record in read_jsonl(commit_log)],
        )
        self.assertEqual(svc_head, git_stdout(repositories["SVC"], "rev-parse", "HEAD"))
        for repository in repositories.values():
            self.assertEqual("", git_stdout(repository, "status", "--porcelain"))

    def test_combined_review_repairs_rereviews_and_rejects_mutation(self) -> None:
        workspace, plan_dir, state_dir, _repositories, _unregistered = (
            self._prepare_case("review-cycle")
        )
        completed, payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--review-harness",
            "claude",
            "--max-review-fix-cycles",
            "1",
            env_updates={
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps({"1": [], "2": []}),
            },
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            ["1", "2", "3", "3.1", "4"],
            [turn["session_id"] for turn in payload["turns"]],
        )
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        review_artifact = json.loads(
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(3, review_artifact["version"])
        repair_body = (plan_dir / "session-3.1.md").read_text(encoding="utf-8")
        self.assertIn("**Reads repositories**: SVC, OAK, OWL, Kiln", repair_body)
        self.assertIn("**Writes repositories**: SVC", repair_body)
        for session_id in ("3", "3.1", "4"):
            self.assertTrue(
                next(
                    turn["workspace_write_guard"]["passed"]
                    for turn in payload["turns"]
                    if turn["session_id"] == session_id
                )
            )

        (
            mutation_workspace,
            mutation_plan,
            mutation_state,
            mutation_repositories,
            _mutation_unregistered,
        ) = self._prepare_case("review-mutation")
        rejected, rejected_payload = self._run_plan(
            mutation_plan,
            mutation_workspace,
            mutation_state,
            "--review-harness",
            "claude",
            "--max-review-fix-cycles",
            "0",
            env_updates={
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_REVIEW_OUTCOMES": "all-clear",
                "FAKE_CODEX_REVIEW_MUTATION_PATH": str(
                    mutation_repositories["OAK"] / "review-mutation.txt"
                ),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps({"1": [], "2": []}),
            },
        )
        self.assertEqual(1, rejected.returncode, rejected.stderr)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            rejected_payload["pause_reason"],
        )
        rejected_guard = rejected_payload["turns"][-1]["workspace_write_guard"]
        self.assertFalse(rejected_guard["passed"])
        self.assertEqual(
            "read-only-repository", rejected_guard["violations"][0]["kind"]
        )
        self.assertIn(
            "OAK:review-mutation.txt",
            rejected_guard["violations"][0]["qualified_paths"],
        )
        self.assertIn(
            "- **Status**: pending",
            (mutation_plan / "session-3.md").read_text(encoding="utf-8"),
        )

    def test_rebind_transport_equivalence_and_failure_stages_are_atomic(self) -> None:
        old_workspace, plan_dir, state_dir, _repositories, _unregistered = (
            self._prepare_case("rebind-source")
        )
        before = self._plan_file_bytes(plan_dir)

        wrong_specs = list(DEFAULT_MULTI_REPO_SPECS)
        alias, relative_path, _origin = wrong_specs[0]
        wrong_specs[0] = (alias, relative_path, "https://example.com/wrong/repo.git")
        invalid_workspace = self.tmp_path / "rebind-invalid-origin"
        init_multi_repo_workspace(invalid_workspace, tuple(wrong_specs))
        invalid, invalid_payload = self._rebind_plan(
            plan_dir, invalid_workspace, state_dir
        )
        self.assertEqual(1, invalid.returncode, invalid.stderr)
        self.assertEqual(
            prepare.PREPARE_STAGE_WORKSPACE,
            invalid_payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(plan_dir))

        validator_workspace = self.tmp_path / "rebind-validator-failure"
        init_multi_repo_workspace(validator_workspace, DEFAULT_MULTI_REPO_SPECS)
        validator_failed, validator_payload = self._rebind_plan(
            plan_dir,
            validator_workspace,
            state_dir,
            env_updates={"FAKE_CODEX_VALIDATOR_MODE": "failure"},
        )
        self.assertEqual(1, validator_failed.returncode, validator_failed.stderr)
        self.assertEqual(
            prepare.PREPARE_STAGE_VALIDATION,
            validator_payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(plan_dir))

        baseline_workspace = self.tmp_path / "rebind-baseline-failure"
        baseline_repositories = init_multi_repo_workspace(
            baseline_workspace, DEFAULT_MULTI_REPO_SPECS
        )
        (baseline_repositories["Kiln"] / "README.md").unlink()
        baseline_failed, baseline_payload = self._rebind_plan(
            plan_dir, baseline_workspace, state_dir
        )
        self.assertEqual(1, baseline_failed.returncode, baseline_failed.stderr)
        self.assertEqual(
            prepare.PREPARE_STAGE_BASELINE,
            baseline_payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(plan_dir))

        equivalent_specs = tuple(
            (
                alias,
                relative_path,
                origin.replace("git@github.com:", "https://github.com/"),
            )
            for alias, relative_path, origin in DEFAULT_MULTI_REPO_SPECS
        )
        equivalent_workspace = self.tmp_path / "rebind-equivalent"
        init_multi_repo_workspace(equivalent_workspace, equivalent_specs)
        rebound, rebound_payload = self._rebind_plan(
            plan_dir, equivalent_workspace, state_dir
        )
        self.assertEqual(0, rebound.returncode, rebound.stderr)
        rebind = rebound_payload["workspace_rebind"]
        self.assertEqual(str(old_workspace.resolve()), rebind["old"]["workspace_root"])
        self.assertEqual(
            str(equivalent_workspace.resolve()), rebind["new"]["workspace_root"]
        )
        self.assertIsNone(rebind["failure_stage"])
        metadata = plans.load_plan_metadata(plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(str(equivalent_workspace.resolve()), metadata.workspace_root)
        self.assertNotIn("git@", json.dumps(rebind))


class KilnTwinCloneRegressionTests(unittest.TestCase):
    """The original failure: a legacy plan reaching a same-origin sibling.

    ``tundra_Kiln`` and ``tundra_Kiln_testing`` are two checkouts of
    one remote sitting side by side. ``workspaces.origins_equal`` holds between
    them, so origin comparison cannot tell them apart; only the resolved Git
    top-level can. These tests pin that the refusal rests on the resolved root,
    that it survives a loop-ready plan whose readiness skips core preflight, and
    that a registry naming both clones still executes.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.config_home = self.tmp_path / "config"
        fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        self.workspace = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, KILN_TWIN_REPO_SPECS
        )
        self.kiln = self.repositories["Kiln"]
        self.kiln_testing = self.repositories["KilnTesting"]
        self.state_dir = self.tmp_path / "state"

    def _run(
        self,
        plan_dir: Path,
        cwd: Path,
        *extra: str,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                "FAKE_CODEX_MODE": "success",
                **(env_updates or {}),
            }
        )
        completed = run_to_completion(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "run",
                str(plan_dir),
                "--cwd",
                str(cwd),
                "--harness",
                "codex",
                "--state-dir",
                str(self.state_dir),
                "--json",
                *extra,
            ],
            cwd=REPO_ROOT,
            env=env,
            budget_seconds=120,
            label="run",
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    def _mark_loop_ready(self, plan_dir: Path) -> None:
        plans.write_readiness_metadata(
            plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _assert_clones_untouched(self, *markers: Path) -> None:
        for marker in markers:
            self.assertFalse(marker.exists(), f"{marker} was written")
        for repository in (self.kiln, self.kiln_testing):
            self.assertEqual(
                "",
                git_stdout(repository, "status", "--porcelain"),
                f"{repository.name} was mutated",
            )

    def _sibling_mutations(self) -> list[dict[str, str]]:
        return [
            {"path": str(self.kiln / "harness-ran.txt"), "text": "kiln\n"},
            {
                "path": str(self.kiln_testing / "harness-ran.txt"),
                "text": "testing\n",
            },
        ]

    def test_legacy_plan_declaring_the_sibling_clone_launches_no_harness(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "legacy-plan"
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(self.kiln_testing / "README.md"),
                            "name": "sibling-readme",
                        }
                    ),
                    session="1",
                )
            },
        )
        self._mark_loop_ready(plan_dir)

        # ``--allow-not-loop-ready`` waives readiness, which is the other gate
        # that could refuse this run. What is left is the hard gate.
        completed, payload = self._run(
            plan_dir,
            self.kiln,
            "--allow-not-loop-ready",
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(self._sibling_mutations())
            },
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])
        self.assertEqual(
            sorted(
                str(plans.canonicalize_path(path))
                for path in (self.kiln, self.kiln_testing)
            ),
            sorted(report["git_roots"]),
        )
        self._assert_clones_untouched(
            *(Path(mutation["path"]) for mutation in self._sibling_mutations())
        )

    def test_expansion_note_redirect_to_the_sibling_clone_launches_no_harness(
        self,
    ) -> None:
        # The plan-level scan skips expansion notes, so a redirect that lives
        # only there reaches the worker through the task packet. The per-turn
        # recheck is the only surface that sees it.
        plan_dir = self.tmp_path / "expansion-plan"
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": "README.md",
                            "name": "kiln-readme",
                        }
                    ),
                    session="1",
                )
            },
        )
        (plan_dir / "session-1.expand.md").write_text(
            "# Session 1 expansion\n\n"
            "The integration fixture actually lives in the testing checkout: "
            f"write it to {self.kiln_testing}/src/fixture.py.\n",
            encoding="utf-8",
        )
        self._mark_loop_ready(plan_dir)

        completed, payload = self._run(
            plan_dir,
            self.kiln,
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(self._sibling_mutations())
            },
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            2, payload["pause_details"]["repository_intent"]["git_root_count"]
        )
        self._assert_clones_untouched(
            *(Path(mutation["path"]) for mutation in self._sibling_mutations())
        )

    def test_refusal_diagnostics_name_roots_without_leaking_credentials(
        self,
    ) -> None:
        secret = "s3cr3t-clone-token"
        plan_dir = self.tmp_path / "credential-plan"
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(self.kiln_testing / "README.md"),
                            "name": "sibling-readme",
                        }
                    ),
                    session="1",
                )
            },
        )
        session_path = plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.",
                "Instructions. The testing checkout came from "
                f"https://kiln-bot:{secret}@github.com/tundra/Kiln.git.",
            ),
            encoding="utf-8",
        )
        self._mark_loop_ready(plan_dir)

        completed, payload = self._run(plan_dir, self.kiln)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        # The diagnostic identifies the conflict by filesystem root, so nothing
        # it carries can echo a remote URL back to the operator.
        self.assertNotIn(secret, completed.stdout)
        self.assertNotIn(secret, completed.stderr)
        self.assertIn(
            str(plans.canonicalize_path(self.kiln_testing)),
            payload["pause_details"]["repository_intent"]["git_roots"],
        )

    def test_registered_workspace_targeting_the_testing_clone_succeeds(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "registered-plan"
        write_multi_repo_plan(
            plan_dir,
            ("pending",),
            specs=KILN_TWIN_REPO_SPECS,
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "repo": "KilnTesting",
                            "path": "fixture.py",
                            "name": "testing-fixture",
                        }
                    ),
                    session="1",
                )
            },
        )
        session_path = plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Writes repositories**: Kiln, KilnTesting",
                "**Writes repositories**: KilnTesting",
            ),
            encoding="utf-8",
        )
        registry = plans.read_repository_registry(plan_dir)
        assert registry is not None
        plans.save_workspace_plan_metadata(plan_dir, self.workspace, registry.digest)
        fixture = self.kiln_testing / "fixture.py"

        completed, payload = self._run(
            plan_dir,
            self.workspace,
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(
                    [{"path": str(fixture), "text": "fixture = True\n"}]
                ),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {"1": ["KilnTesting:fixture.py"]}
                ),
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(["KilnTesting"], guard["authorized_aliases"])
        self.assertTrue(fixture.is_file())
        self.assertEqual("", git_stdout(self.kiln, "status", "--porcelain"))


class VerificationRepairLoopTests(unittest.TestCase):
    """Packet mode + fingerprint-aware verification repair (Session 10).

    Covers three guarantees: autonomous runs use the Python-built task packet
    instead of the ``session-run`` skill (with an ``ASL_WORKER_MODE=skill``
    rollback), a relaunch injects prior-failure repair context into the packet,
    and an identical verification failure is fingerprinted and marked exhausted
    so it cannot loop while a changed failure stays eligible.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(self, *, path: str = "MISSING.md", name: str = "missing") -> None:
        contracts_by_id = {
            "1": _contract_block(
                f'{{"type": "file_exists", "path": "{path}", "name": "{name}"}}',
                session="1",
            )
        }
        write_plan(self.plan_dir, ("pending",), contracts_by_id=contracts_by_id)
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(
        self, *, worker_mode: str | None = None
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
            "--allow-not-loop-ready",
        ]
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        if worker_mode is not None:
            env["ASL_WORKER_MODE"] = worker_mode
        completed = run_to_completion(
            argv,
            cwd=REPO_ROOT,
            env=env,
            budget_seconds=20,
            label="run",
        )
        return completed, json.loads(completed.stdout)

    @staticmethod
    def _turn_prompt(payload: dict) -> str:
        # Codex passes the prompt as the final argv token.
        return str(payload["turns"][0]["argv"][-1])

    # --- packet vs skill prompt wiring -----------------------------------

    def test_packet_mode_is_default_and_avoids_session_run(self) -> None:
        self._write_plan(path="README.md", name="readme")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        prompt = self._turn_prompt(payload)
        self.assertTrue(prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
        self.assertNotIn("session-run", prompt)

    def test_skill_worker_mode_restores_session_run_invocation(self) -> None:
        self._write_plan(path="README.md", name="readme")

        completed, payload = self._run(worker_mode="skill")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        prompt = self._turn_prompt(payload)
        self.assertTrue(prompt.startswith("$session-run "))
        self.assertIn("--runner-managed", prompt)

    # --- fingerprint recording + repair context --------------------------

    def test_first_failure_records_fingerprint_and_attempt(self) -> None:
        self._write_plan()

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        details = payload["pause_details"]
        self.assertIsInstance(details["verification_fingerprint"], str)
        self.assertTrue(
            details["verification_fingerprint"].startswith("verify-failed:")
        )
        self.assertEqual(1, details["verification_repair_attempt"])
        self.assertFalse(details["verification_repair_exhausted"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual(
            details["verification_fingerprint"],
            verify_payload["verification_fingerprint"],
        )

    def test_relaunch_injects_prior_failure_into_packet(self) -> None:
        self._write_plan()

        _first_completed, first_payload = self._run()
        fingerprint = first_payload["pause_details"]["verification_fingerprint"]

        _second_completed, second_payload = self._run()

        prompt = self._turn_prompt(second_payload)
        self.assertTrue(prompt.startswith(task_packet.TASK_PACKET_HEADING_PREFIX))
        self.assertIn("Verification repair references", prompt)
        self.assertIn("Prior verification attempt 1 failed", prompt)
        self.assertIn(fingerprint, prompt)
        self.assertIn("Failed check `missing`", prompt)

    def test_identical_failure_marks_repair_exhausted(self) -> None:
        self._write_plan()

        _first_completed, first_payload = self._run()
        _second_completed, second_payload = self._run()

        first_details = first_payload["pause_details"]
        second_details = second_payload["pause_details"]
        self.assertEqual(
            first_details["verification_fingerprint"],
            second_details["verification_fingerprint"],
        )
        self.assertEqual(2, second_details["verification_repair_attempt"])
        self.assertTrue(second_details["verification_repair_exhausted"])

    # --- pure helper coverage --------------------------------------------

    def test_fingerprint_is_stable_and_output_sensitive(self) -> None:
        base = {
            "failures": [
                {
                    "name": "unit",
                    "kind": "command",
                    "failure_reason": "command-failed",
                    "returncode": 1,
                    "stdout_sha256": "abc",
                    "stderr_sha256": "",
                }
            ]
        }
        same = json.loads(json.dumps(base))
        changed = json.loads(json.dumps(base))
        changed["failures"][0]["stdout_sha256"] = "def"

        self.assertEqual(
            runner._verification_fingerprint(base),
            runner._verification_fingerprint(same),
        )
        self.assertNotEqual(
            runner._verification_fingerprint(base),
            runner._verification_fingerprint(changed),
        )
        self.assertIsNone(runner._verification_fingerprint({"failures": []}))
        self.assertIsNone(runner._verification_fingerprint(None))

    def test_repair_exhaustion_decision(self) -> None:
        # An identical fingerprint that already appeared is exhausted.
        self.assertTrue(
            runner._is_verification_repair_exhausted(
                "verify-failed:x", ("verify-failed:x",), 2
            )
        )
        # A changed fingerprint under the attempt cap stays eligible.
        self.assertFalse(
            runner._is_verification_repair_exhausted(
                "verify-failed:y", ("verify-failed:x",), 2
            )
        )
        # Exceeding the attempt cap is exhausted regardless of fingerprint.
        self.assertTrue(
            runner._is_verification_repair_exhausted(
                "verify-failed:z",
                ("verify-failed:x", "verify-failed:y"),
                runner.MAX_VERIFICATION_REPAIR_ATTEMPTS + 1,
            )
        )
        # No fingerprint on the first attempt is not exhausted.
        self.assertFalse(runner._is_verification_repair_exhausted(None, (), 1))

    def test_read_prior_verification_failures_filters_by_session(self) -> None:
        event_path = self.tmp_path / "events.jsonl"

        def _pause(session: str, reason: str, sha: str) -> None:
            events.append_event(
                event_path,
                events.Event(
                    name=events.RUN_PAUSED_EVENT,
                    payload={
                        "reason": reason,
                        "details": {
                            "session": session,
                            "failures": [
                                {
                                    "name": "missing",
                                    "kind": "file_exists",
                                    "failure_reason": "file-missing",
                                    "returncode": None,
                                    "stdout_sha256": sha,
                                    "stderr_sha256": "",
                                }
                            ],
                        },
                    },
                ),
            )

        _pause("1", "verify-failed", "a")
        _pause("2", "verify-failed", "b")
        _pause("1", "timeout", "c")
        _pause("1", "verify-failed", "d")

        priors = runner._read_prior_verification_failures(event_path, "1")
        self.assertEqual(2, len(priors))
        self.assertEqual(
            ["a", "d"], [p["failures"][0]["stdout_sha256"] for p in priors]
        )

        state_info = runner._verification_repair_state(event_path, "1")
        self.assertEqual(3, state_info["attempt"])
        self.assertEqual(2, len(state_info["prior_fingerprints"]))
        references = state_info["references"]
        self.assertTrue(
            any("Prior verification attempt 2 failed" in ref for ref in references)
        )
        self.assertTrue(any("Failed check `missing`" in ref for ref in references))


if __name__ == "__main__":
    unittest.main()
