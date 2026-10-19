"""Test asl workbench summary active."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    qna,
    state,
    workbench_summary,
)
from tests import support_asl_workbench_summary as fixtures
from tests.support import (
    _discovered,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)


class WorkbenchSummaryTests(fixtures.WorkbenchSummaryCase):
    def test_running_classification_with_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "artifacts" / "turn-1"
            artifact_dir.mkdir(parents=True)
            stdout = artifact_dir / "stdout.log"
            stdout.write_text("running", encoding="utf-8")
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": {
                        "session_id": "1",
                        "turn_index": 4,
                        "artifact_dir": str(artifact_dir),
                        "stdout_path": str(stdout),
                        "last_message_path": str(artifact_dir / "last-message.txt"),
                    },
                    "active_verification": None,
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("running", summary.primary_state)
            self.assertEqual("watch", summary.recommended_action)
            self.assertIn("session 1", summary.next_action_hint)
            self.assertEqual("20260619T120000Z-1", summary.run_id)
            self.assertEqual("claude", summary.harness)
            self.assertEqual(artifact_dir, summary.artifact_dir)
            self.assertEqual(stdout, summary.stdout_path)

    def test_verifying_classification_with_active_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T130000Z-1",
                    "harness": "codex",
                    "run_started_at": "2026-06-19T13:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": {
                        "session_id": "1",
                        "turn_index": 2,
                        "check_count": 2,
                        "current_check_index": 1,
                        "current_check_name": "pytest-suite",
                        "current_check_kind": "command",
                    },
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("verifying", summary.primary_state)
            self.assertEqual("watch", summary.recommended_action)
            self.assertIn("2/2", summary.next_action_hint)
            self.assertIn("pytest-suite", summary.next_action_hint)

    def test_verifying_takes_priority_over_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run",
                    "harness": "claude",
                    "run_finished_at": None,
                    "active_turn": {"session_id": "1", "turn_index": 1},
                    "active_verification": {
                        "session_id": "1",
                        "turn_index": 1,
                        "check_count": 1,
                        "current_check_index": 0,
                        "current_check_name": "compile",
                    },
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verifying", summary.primary_state)

    def test_paused_for_reply_with_readable_last_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = state.plan_state_dir(plan_dir, state_root) / "runs"
            artifact_dir.mkdir(parents=True)
            last_message = artifact_dir / "last-message.txt"
            last_message.write_text(
                "Need a decision: should we drop the v1 column?",
                encoding="utf-8",
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "pause_reason": "agent-question",
                    "pause_message": "Awaiting confirmation",
                    "pause_details": {"category": "decision"},
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "last_message_path": str(last_message),
                            "harness": "claude",
                            "started_at": "2026-06-19T14:00:00+00:00",
                        }
                    ],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("reply", summary.recommended_action)
            self.assertEqual(last_message, summary.last_message_path)
            self.assertIsNotNone(summary.last_message_preview)
            self.assertIn("v1 column", summary.last_message_preview)
            self.assertEqual("agent-question", summary.pause_reason)
            self.assertEqual("Awaiting confirmation", summary.pause_message)
            self.assertEqual((), summary.warnings)

    def test_paused_for_reply_without_last_message_emits_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "agent-question",
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "harness": "claude",
                        }
                    ],
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("reply", summary.recommended_action)
            self.assertIsNone(summary.last_message_preview)
            self.assertEqual(1, len(summary.warnings))
            self.assertIn("paused-q&a context unreadable", summary.warnings[0])

    def test_pause_without_a_question_takes_the_recovery_diagnosis(self) -> None:
        # A run that stopped on an unready review repair is recorded as paused,
        # but no session waits on an answer. It was offered as "reply needed
        # for session ?" with a reply button that could only be refused; the
        # recovery diagnosis already says what it needs.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan-alpha"
            write_plan(plan_dir, ("complete", "complete"))
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "not-loop-ready",
                    "run_id": "20260619T140000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T14:00:00+00:00",
                    "run_finished_at": "2026-06-19T14:10:00+00:00",
                },
            )

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("not-loop-ready", summary.recovery["kind"])
            self.assertEqual("failed", summary.primary_state)
            self.assertEqual("inspect-failure", summary.recommended_action)
            self.assertEqual(summary.recovery["title"], summary.next_action_hint)
            self.assertNotIn("?", summary.next_action_hint)
            self.assertEqual((), summary.warnings)

    def test_expired_wait_matches_qna_reply_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = state.plan_state_dir(plan_dir, state_root) / "runs"
            artifact_dir.mkdir(parents=True)
            last_message = artifact_dir / "last-message.txt"
            last_message.write_text(
                "Use the strict or lenient parser?\n",
                encoding="utf-8",
            )
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "run_id": "20260724T120000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-07-24T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 2,
                            "last_message_path": str(last_message),
                            "harness": "claude",
                        }
                    ],
                    "waiting_for_reply": {
                        "question_id": "question-2",
                        "session_id": "1",
                        "turn_index": 2,
                        "started_at": "2026-07-24T12:00:00+00:00",
                        "base_deadline": "2026-07-24T12:05:00+00:00",
                        "hard_deadline": "2026-07-24T12:15:00+00:00",
                        "last_lease_activity_at": None,
                    },
                },
            )

            qna_context = qna.load_qna_context(
                plan_dir,
                state_dir=state_root,
            )
            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir),
                state_dir=state_root,
            )

            self.assertEqual(qna.EXPIRED_WAIT_PAUSE_REASON, qna_context.pause_reason)
            self.assertEqual("paused-for-reply", summary.primary_state)
            self.assertEqual("reply", summary.recommended_action)
            self.assertEqual(qna_context.pause_reason, summary.pause_reason)
            self.assertEqual("reply-needed", summary.recovery["kind"])

    def test_live_wait_between_turns_remains_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": None,
                    "paused": False,
                    "run_id": "20260724T120000Z-1",
                    "harness": "codex",
                    "run_started_at": "2026-07-24T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                    "waiting_for_reply": {
                        "question_id": "question-2",
                        "session_id": "1",
                        "turn_index": 2,
                        "started_at": "2026-07-24T12:05:00+00:00",
                        "base_deadline": "2099-07-24T12:10:00+00:00",
                        "hard_deadline": "2099-07-24T12:15:00+00:00",
                        "last_lease_activity_at": None,
                    },
                },
            )

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("running", summary.primary_state)
            self.assertEqual("watch", summary.recommended_action)
            self.assertEqual("Awaiting reply for session 1", summary.concise_reason)
            self.assertEqual("2026-07-24T12:00:00+00:00", summary.last_activity_at)
            self.assertEqual("run-started", summary.last_activity_source)


if __name__ == "__main__":
    unittest.main()
