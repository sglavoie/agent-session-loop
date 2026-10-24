"""Run verification interrupt and progress journal integration tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    events,
    handoff_guards,
    plans_content_hash,
    plans_models,
    plans_progress,
    plans_readiness,
    run_loop,
    state,
    state_locks,
    turn_execution_constants,
    verification_run,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    patched_env,
    read_jsonl,
    write_plan,
)

ASL_TEST_LANE = "integration"


class RunVerificationInterruptTests(unittest.TestCase):
    """Direct ``run_locked`` invocation tests covering KeyboardInterrupt
    handling for the post-turn verification phase and the outer session loop.

    These exercise the cleanup contract: any KeyboardInterrupt raised after a
    harness turn finishes must leave a coherent state payload with
    ``pause_reason='interrupted'``, ``run_finished_at`` populated, and no
    dangling ``active_turn`` / ``active_verification`` entries.
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

    def _make_args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke_run_locked(
        self,
    ) -> tuple[int, dict[str, Any]]:
        env_updates = {
            "XDG_CONFIG_HOME": str(self.config_home),
            "FAKE_CODEX_MODE": "success",
            "FAKE_CODEX_SLEEP": "5",
            "PYTHONPATH": str(REPO_ROOT)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        args = self._make_args()
        with patched_env(**env_updates):
            lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run",
                )
            finally:
                state_locks.release_lock(self.plan_dir, self.state_dir)
        return exit_code, payload

    def test_keyboard_interrupt_during_verification_pauses_cleanly(
        self,
    ) -> None:
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})

        def raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with mock.patch.object(
            verification_run,
            "run_verification",
            side_effect=raise_keyboard_interrupt,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(turn_execution_constants.INTERRUPTED_EXIT, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("interrupted", payload["pause_reason"])
        self.assertIn("verification", payload["pause_message"])
        self.assertIn("interrupted by user", payload["pause_message"])
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsInstance(payload["run_finished_at"], str)
        self.assertNotEqual("", payload["run_finished_at"])
        # The session was reverted to pending.
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        self.assertNotIn("- **Status**: complete", session_text)
        self.assertIn("verification interrupted by user", session_text)
        # Lock was released.
        self.assertFalse(
            state.lock_path_for_plan(self.plan_dir, self.state_dir).exists()
        )
        # The verification phase emitted a `finished` event with the
        # interrupted reason, keeping the observer contract symmetric.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        finished_records = [
            record
            for record in event_records
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
        ]
        self.assertEqual(1, len(finished_records))
        self.assertEqual("interrupted", finished_records[0]["payload"]["reason"])
        self.assertFalse(finished_records[0]["payload"]["passed"])
        # `run_finished` was still emitted as the final lifecycle event.
        run_finished_records = [
            record for record in event_records if record["name"] == "run_finished"
        ]
        self.assertEqual(1, len(run_finished_records))

    def test_keyboard_interrupt_after_turn_finished_pauses_cleanly(
        self,
    ) -> None:
        """Cover the outer ``run_locked`` KeyboardInterrupt wrapper.

        When a KeyboardInterrupt escapes the post-turn bookkeeping helpers
        (here, ``_append_progress_entry``), the outer ``try/except`` must call
        ``_pause_run('interrupted', ...)`` once, then ``_finish_run`` must
        clear both active phases and stamp ``run_finished_at``.
        """

        # No completion contract: verification is skipped, so the next thing
        # called after `turn_finished` is `_append_progress_entry`, which we
        # mock to raise KeyboardInterrupt.
        write_plan(self.plan_dir, ("pending",))

        def raise_keyboard_interrupt(*_args: object, **_kwargs: object) -> bool:
            raise KeyboardInterrupt

        with mock.patch.object(
            handoff_guards,
            "_append_progress_entry",
            side_effect=raise_keyboard_interrupt,
        ):
            exit_code, payload = self._invoke_run_locked()

        self.assertEqual(turn_execution_constants.INTERRUPTED_EXIT, exit_code)
        self.assertTrue(payload["paused"])
        self.assertEqual("interrupted", payload["pause_reason"])
        self.assertEqual("run interrupted by user", payload["pause_message"])
        # Pause details should carry the in-flight session identity.
        pause_details = payload["pause_details"]
        self.assertEqual(1, pause_details["session"])
        self.assertIsNone(payload["active_turn"])
        self.assertIsNone(payload["active_verification"])
        self.assertIsInstance(payload["run_finished_at"], str)
        self.assertNotEqual("", payload["run_finished_at"])
        # The lock is released even when the outer wrapper catches the KI.
        self.assertFalse(
            state.lock_path_for_plan(self.plan_dir, self.state_dir).exists()
        )
        # Exactly one `run_paused` event was recorded (no duplicate from a
        # cascading handler).
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_records = read_jsonl(event_path)
        pause_records = [
            record
            for record in event_records
            if record["name"] == events.RUN_PAUSED_EVENT
        ]
        self.assertEqual(1, len(pause_records))
        self.assertEqual("interrupted", pause_records[0]["payload"]["reason"])


class RunProgressJournalIntegrationTests(unittest.TestCase):
    """End-to-end coverage for runner-side ``progress.md`` integration."""

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
        contracts_by_id: dict[str, str],
        readiness: str | None = "loop-ready",
    ) -> None:
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
        if readiness is None:
            return
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(self) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
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
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _passing_contracts(self, count: int) -> dict[str, str]:
        return {
            str(i): _contract_block(
                f'{{"type": "file_exists", "path": "README.md", "name": "readme-{i}"}}',
                session=str(i),
            )
            for i in range(1, count + 1)
        }

    def test_two_session_run_writes_progress_journal_in_order(self) -> None:
        self._write_plan(
            ("pending", "pending"),
            contracts_by_id=self._passing_contracts(2),
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])

        entries = plans_progress.read_progress_entries(self.plan_dir)
        self.assertEqual(2, len(entries))
        self.assertEqual(["1", "2"], [entry.session_id.value for entry in entries])
        self.assertEqual(["complete", "complete"], [entry.status for entry in entries])

        self.assertEqual(2, len(payload["turns"]))
        for turn in payload["turns"]:
            self.assertTrue(turn["progress_appended"])
            self.assertNotIn("progress_error", turn)

    def test_verification_failure_short_circuits_progress_append(self) -> None:
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

        self.assertFalse(plans_progress.progress_path(self.plan_dir).exists())
        self.assertNotIn("progress_appended", payload["turns"][0])
        failure_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == "progress-journal-append-failed"
        ]
        self.assertEqual([], failure_events)

    def test_append_failure_records_error_and_continues_run(self) -> None:
        self._write_plan(
            ("pending",),
            contracts_by_id=self._passing_contracts(1),
        )
        # Pre-write progress.md with a future schema version so the real
        # append helper raises PlanError without crashing the run.
        plans_progress.progress_path(self.plan_dir).write_text(
            "# Progress Journal\n<!-- agent-session-loop progress-journal v999 -->\n",
            encoding="utf-8",
        )

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])

        turn = payload["turns"][0]
        self.assertFalse(turn["progress_appended"])
        self.assertIn("v999", turn["progress_error"])

        failure_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == "progress-journal-append-failed"
        ]
        self.assertEqual(1, len(failure_events))
        self.assertEqual("1", failure_events[0]["session"])
        self.assertIn("v999", failure_events[0]["error"])


if __name__ == "__main__":
    unittest.main()
