"""Test recovery card state."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    recovery_card_build,
    state,
)
from tests import support_asl_recovery_card as fixtures
from tests.support import (
    _SUMMARY_DEAD_PID,
    _discovered,
    _make_summary_plan,
    _write_lock_file,
    _write_state_payload,
)

ASL_TEST_PORTABLE = True


class RecoveryCardTests(fixtures.RecoveryCardCase):
    def test_reply_needed_recovery_uses_latest_paused_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_summary_plan(tmp_path)
            state_root = tmp_path / "state"
            older_artifact = tmp_path / "runs" / "run-reply" / "turn-0001"
            latest_artifact = tmp_path / "runs" / "run-reply" / "turn-0002"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "pause_reason": "agent-question",
                    "pause_message": "Need a decision",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "last_message_path": str(
                                older_artifact / "last-message.txt"
                            ),
                            "result_path": str(older_artifact / "result.json"),
                        },
                        {
                            "session_id": "2",
                            "turn_index": 2,
                            "last_message_path": str(
                                latest_artifact / "last-message.txt"
                            ),
                            "result_path": str(latest_artifact / "result.json"),
                        },
                    ],
                    "run_id": "run-reply",
                },
            )

            recovery = recovery_card_build.build_recovery_card(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("reply-needed", recovery.kind)
            self.assertEqual("2", recovery.session_id)
            self.assertEqual(2, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "paused-turn"
                    and artifact["path"] == str(latest_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "paused-turn"
                    and artifact["path"] == str(older_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )

    def test_stale_lock_recovery_unlocks_through_existing_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_summary_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_lock_file(plan_dir, state_root, pid=_SUMMARY_DEAD_PID)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                recovery = recovery_card_build.build_recovery_card(
                    _discovered(plan_dir), state_dir=state_root
                )

            self.assertEqual("stale-lock", recovery.kind)

    def test_stale_state_recovery_unlocks_orphaned_runner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_summary_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run-orphan",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                },
            )

            recovery = recovery_card_build.build_recovery_card(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("stale-state", recovery.kind)
            self.assertIn("no lock file", recovery.summary)

    def test_stale_state_precedes_stale_lock_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_summary_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run-orphan",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=_SUMMARY_DEAD_PID)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                recovery = recovery_card_build.build_recovery_card(
                    _discovered(plan_dir), state_dir=state_root
                )

            self.assertEqual("stale-state", recovery.kind)
            self.assertIn("lock is stale", recovery.summary)

    def test_active_run_precedes_paused_recovery_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_summary_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-active" / "turn-0002"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed",
                    "run_id": "run-active",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": {
                        "session_id": "1",
                        "turn_index": 2,
                        "result_path": str(artifact_dir / "result.json"),
                    },
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())

            recovery = recovery_card_build.build_recovery_card(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("none", recovery.kind)
            self.assertEqual("Run in progress", recovery.title)
            self.assertFalse(recovery.recoverable)
            self.assertEqual("1", recovery.session_id)
            self.assertEqual(2, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "active-turn"
                    and artifact["path"] == str(artifact_dir / "result.json")
                    for artifact in artifact_payload
                )
            )


if __name__ == "__main__":
    unittest.main()
