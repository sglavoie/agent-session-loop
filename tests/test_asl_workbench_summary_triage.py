"""Test asl workbench summary triage."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans_files,
    plans_models,
    plans_progress,
    plans_session_ids,
    state,
    workbench_registry_models,
    workbench_summary,
    workbench_summary_model,
)
from tests import support_asl_workbench_summary as fixtures
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)


class WorkbenchSummaryTests(fixtures.WorkbenchSummaryCase):
    def test_triage_fields_for_invalid_missing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing = tmp_path / "ghost"
            discovered = _discovered(missing, exists=False)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("invalid", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual(
                workbench_summary_model.URGENCY_RANK_BY_STATE["invalid"],
                summary.urgency_rank,
            )
            self.assertEqual(
                workbench_summary_model.ACTIVITY_LABEL_BY_STATE["invalid"],
                summary.activity_label,
            )
            self.assertIn(str(missing), summary.concise_reason)
            self.assertIsNone(summary.last_activity_at)
            self.assertIsNone(summary.last_activity_source)
            self.assertIn("invalid", summary.searchable_text)
            self.assertIn("repair-plan", summary.searchable_text)

    def test_triage_fields_for_running_plan(self) -> None:
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
                    "run_id": "20260619T120000Z-1",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": {
                        "session_id": "1",
                        "turn_index": 4,
                        "started_at": "2026-06-19T12:00:01+00:00",
                        "last_activity_at": "2026-06-19T12:01:30+00:00",
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
            self.assertEqual("in-progress", summary.urgency_group)
            self.assertEqual(
                workbench_summary_model.URGENCY_RANK_BY_STATE["running"],
                summary.urgency_rank,
            )
            self.assertEqual("2026-06-19T12:01:30+00:00", summary.last_activity_at)
            self.assertEqual("active-turn", summary.last_activity_source)
            self.assertIn("turn 4", summary.concise_reason)
            self.assertIn("claude", summary.searchable_text)
            self.assertIn("20260619t120000z-1", summary.searchable_text)

    def test_triage_fields_for_verifying_plan(self) -> None:
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
                        "started_at": "2026-06-19T13:00:05+00:00",
                        "current_check_started_at": "2026-06-19T13:00:20+00:00",
                    },
                },
            )
            _write_lock_file(plan_dir, state_root, pid=os.getpid())
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("verifying", summary.primary_state)
            self.assertEqual("in-progress", summary.urgency_group)
            self.assertEqual("2026-06-19T13:00:20+00:00", summary.last_activity_at)
            self.assertEqual("active-verification", summary.last_activity_source)
            self.assertIn("pytest-suite", summary.concise_reason)
            self.assertIn("2/2", summary.concise_reason)

    def test_triage_fields_for_paused_for_reply(self) -> None:
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
                    "run_started_at": "2026-06-19T14:00:00+00:00",
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
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual(
                workbench_summary_model.URGENCY_RANK_BY_STATE["paused-for-reply"],
                summary.urgency_rank,
            )
            self.assertEqual("2026-06-19T14:00:00+00:00", summary.last_activity_at)
            self.assertEqual("turn-record", summary.last_activity_source)
            self.assertEqual("Awaiting confirmation", summary.concise_reason)
            self.assertIn("v1 column", summary.searchable_text)
            self.assertIn("agent-question", summary.searchable_text)

    def test_triage_fields_for_paused_without_last_message(self) -> None:
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
                    "run_started_at": "2026-06-19T14:00:00+00:00",
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
            self.assertEqual("2026-06-19T14:00:00+00:00", summary.last_activity_at)
            self.assertEqual("run-started", summary.last_activity_source)
            self.assertEqual("agent-question", summary.concise_reason)

    def test_triage_fields_for_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "run_id": "20260619T120000Z-2",
                    "harness": "claude",
                    "reason": "verify-failed",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("failed", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual("2026-06-19T12:05:00+00:00", summary.last_activity_at)
            self.assertEqual("run-finished", summary.last_activity_source)
            self.assertEqual("verify-failed", summary.concise_reason)
            self.assertIn("verify-failed", summary.searchable_text)

    def test_triage_fields_for_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            _write_lock_file(plan_dir, state_root, pid=_WORKBENCH_DEAD_PID)
            discovered = _discovered(plan_dir)

            with mock.patch.object(
                state,
                "is_pid_running",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                summary = workbench_summary.build_workbench_summary(
                    discovered, state_dir=state_root
                )

            self.assertEqual("stale", summary.primary_state)
            self.assertEqual("needs-action", summary.urgency_group)
            self.assertEqual("2026-06-19T00:00:00+00:00", summary.last_activity_at)
            self.assertEqual("lock-created", summary.last_activity_source)

    def test_triage_fields_for_recent_run(self) -> None:
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
                    "run_id": "20260619T120000Z-4",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:30:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("recent", summary.primary_state)
            self.assertEqual("ready", summary.urgency_group)
            self.assertEqual("2026-06-19T12:30:00+00:00", summary.last_activity_at)
            self.assertEqual("run-finished", summary.last_activity_source)
            self.assertIn("12:30:00", summary.concise_reason)

    def test_triage_fields_for_complete_uses_progress_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-plan"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            plans_progress.append_progress_entry(
                canonical,
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00+00:00",
                ),
            )
            plans_progress.append_progress_entry(
                canonical,
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("2"),
                    status="complete",
                    completed_at="2026-06-19T15:10:00+00:00",
                ),
            )
            discovered = _discovered(canonical)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("complete", summary.urgency_group)
            self.assertEqual("2026-06-19T15:10:00+00:00", summary.last_activity_at)
            self.assertEqual("progress-journal", summary.last_activity_source)
            self.assertEqual(
                workbench_summary_model.URGENCY_RANK_BY_STATE["complete"],
                summary.urgency_rank,
            )

    def test_searchable_text_lowercases_and_includes_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = workbench_registry_models.DiscoveredPlan(
                plan_dir=plan_dir,
                sources=("state",),
                label="Workbench Alpha",
                profile=None,
                exists=True,
            )

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertIn("workbench alpha", summary.searchable_text)
            self.assertNotIn("Workbench Alpha", summary.searchable_text)
            self.assertIn("state", summary.searchable_text)

    def test_payload_exposes_triage_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            payload = summary.to_payload()

            self.assertEqual(summary.urgency_rank, payload["urgency_rank"])
            self.assertEqual(summary.urgency_group, payload["urgency_group"])
            self.assertEqual(summary.activity_label, payload["activity_label"])
            self.assertEqual(summary.concise_reason, payload["concise_reason"])
            self.assertEqual(summary.last_activity_at, payload["last_activity_at"])
            self.assertEqual(
                summary.last_activity_source, payload["last_activity_source"]
            )
            self.assertEqual(summary.searchable_text, payload["searchable_text"])
            self.assertEqual(summary.recovery, payload["recovery"])
            self.assertEqual("none", payload["recovery"]["kind"])


if __name__ == "__main__":
    unittest.main()
