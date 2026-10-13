"""Run verification enforcement tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import config, events, plans, state, verification
from tests.support import (
    REPO_ROOT,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    read_jsonl,
    write_plan,
)


class RunVerificationEnforcementTests(unittest.TestCase):
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

    def _write_plan(
        self,
        statuses: tuple[str, ...],
        *,
        contracts_by_id: dict[str, str] | None,
        readiness: str | None = "loop-ready",
    ) -> None:
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
        if readiness is None:
            return
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(
        self,
        *,
        allow_not_loop_ready: bool = True,
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
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_verify_passing_contract_allows_session_to_complete(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", text)
        turns = payload["turns"]
        self.assertEqual(1, len(turns))
        verify_payload = turns[0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verify_passing_contract_emits_verification_phase_events(
        self,
    ) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        final_snapshot = payload["final_snapshot"]
        self.assertEqual(1, final_snapshot["counts"]["complete"])
        self.assertEqual(0, final_snapshot["counts"]["pending"])
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        phase_events = [
            record["name"]
            for record in event_records
            if record["name"]
            in {
                events.VERIFICATION_STARTED_EVENT,
                events.VERIFICATION_CHECK_STARTED_EVENT,
                events.VERIFICATION_CHECK_FINISHED_EVENT,
                events.VERIFICATION_FINISHED_EVENT,
            }
        ]
        self.assertEqual(
            [
                events.VERIFICATION_STARTED_EVENT,
                events.VERIFICATION_CHECK_STARTED_EVENT,
                events.VERIFICATION_CHECK_FINISHED_EVENT,
                events.VERIFICATION_FINISHED_EVENT,
            ],
            phase_events,
        )
        started = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_STARTED_EVENT
        )
        self.assertEqual(1, started["check_count"])
        self.assertEqual(["readme"], started["check_names"])
        check_started = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_CHECK_STARTED_EVENT
        )
        self.assertEqual("readme", check_started["name"])
        self.assertEqual("file_exists", check_started["kind"])
        check_finished = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_CHECK_FINISHED_EVENT
        )
        self.assertEqual("readme", check_finished["name"])
        self.assertEqual(verification.CHECK_ROLE_GATE, check_finished["role"])
        finished = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        )
        self.assertTrue(finished["passed"])
        self.assertEqual(1, finished["passed_count"])
        self.assertEqual("ok", finished["reason"])

    def test_verify_failing_contract_pauses_and_reverts_status(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", "name": "missing"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertIn("verification failed", payload["pause_message"])
        pause_details = payload["pause_details"]
        self.assertFalse(pause_details["passed"])
        self.assertEqual(["missing"], pause_details["recipe"]["names"])
        self.assertEqual(1, len(pause_details["failures"]))
        failure = pause_details["failures"][0]
        self.assertEqual("missing", failure["name"])
        self.assertEqual("file_exists", failure["kind"])
        self.assertEqual("file-missing", failure["failure_reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertNotIn("- **Status**: complete", text)
        self.assertIn("verification failed", text)
        self.assertIn("file-missing", text)
        final_snapshot = payload["final_snapshot"]
        self.assertEqual(1, final_snapshot["counts"]["pending"])
        self.assertEqual(0, final_snapshot["counts"]["complete"])

    def test_verify_failing_contract_clears_active_phases_and_finishes_run(
        self,
    ) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", "name": "missing"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsInstance(payload["run_finished_at"], str)
        self.assertNotEqual("", payload["run_finished_at"])

    def test_verify_parallel_contract_completes_and_marks_event(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "command", "name": "quick", '
                '"argv": ["python3", "-c", "pass"], "timeout_seconds": 60},'
                '{"type": "command", "name": "slow", '
                '"argv": ["python3", "-c", "import time; time.sleep(0.2)"], '
                '"timeout_seconds": 60},'
                '{"type": "file_exists", "path": "README.md", '
                '"name": "readme"}',
                session="1",
                parallel=True,
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        started = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_STARTED_EVENT
        )
        self.assertTrue(started["parallel"])
        self.assertEqual(["quick", "slow", "readme"], started["check_names"])
        finished = next(
            record["payload"]
            for record in event_records
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        )
        self.assertTrue(finished["passed"])
        self.assertEqual(3, finished["passed_count"])

    def test_verify_parallel_contract_pauses_with_declared_order_details(
        self,
    ) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "command", "name": "alpha", '
                '"argv": ["python3", "-c", "import sys; sys.exit(3)"], '
                '"timeout_seconds": 60},'
                '{"type": "command", "name": "beta", '
                '"argv": ["python3", "-c", '
                '"import sys, time; time.sleep(0.2); sys.exit(4)"], '
                '"timeout_seconds": 60}',
                session="1",
                parallel=True,
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        pause_details = payload["pause_details"]
        self.assertFalse(pause_details["passed"])
        self.assertEqual(["alpha", "beta"], pause_details["recipe"]["names"])
        # `alpha` fails first in wall-clock order but must still be reported
        # in its declared position, ahead of the slower `beta`.
        self.assertEqual(
            ["alpha", "beta"],
            [failure["name"] for failure in pause_details["failures"]],
        )
        self.assertEqual(
            [3, 4], [failure["returncode"] for failure in pause_details["failures"]]
        )
        text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)

    def test_verify_missing_contract_pauses_when_gate_enforced(self) -> None:
        self._write_plan(("pending",), contracts_by_id=None)

        completed, payload = self._run(allow_not_loop_ready=False)

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        # Preflight gate fails first because contracts are required for
        # loop-ready plans, so the runner pauses on `not-loop-ready` before
        # verification runs.
        self.assertEqual("not-loop-ready", payload["pause_reason"])

    def test_verify_missing_contract_is_skipped_when_gate_bypassed(self) -> None:
        self._write_plan(("pending",), contracts_by_id=None, readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual("no-contract", verify_payload["reason"])
        self.assertFalse(verify_payload["enforced"])

    def test_verify_malformed_contract_pauses_with_malformed_contract(self) -> None:
        bad_contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- recipe runs",
                "",
                "### Verify",
                "",
                "```json",
                '{"session": "1", "checks": [{"type": "command", '
                '"argv": ["echo"], "timeout_seconds": -1}]}',
                "```",
            ]
        )
        self._write_plan(
            ("pending",),
            contracts_by_id={"1": bad_contract},
            readiness=None,
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-contract", payload["pause_reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertIn("verification skipped", text)

    def test_verify_failed_pause_is_listed_in_pause_reasons(self) -> None:
        self.assertIn("verify-failed", events.PAUSE_REASONS)
        self.assertIn("no-contract", events.PAUSE_REASONS)
        self.assertIn("malformed-contract", events.PAUSE_REASONS)


if __name__ == "__main__":
    unittest.main()
