"""Test asl workbench summary state."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans_metadata,
    plans_models,
    plans_progress,
    plans_session_ids,
    state,
    workbench_registry_models,
    workbench_summary,
    workbench_summary_helpers,
    workbench_summary_model,
)
from tests import support_asl_workbench_summary as fixtures
from tests.support import (
    _WORKBENCH_DEAD_PID,
    _discovered,
    _make_workbench_plan,
    _write_lock_file,
    _write_state_payload,
    write_multi_repo_plan,
)

ASL_TEST_PORTABLE = True


class WorkbenchSummaryTests(fixtures.WorkbenchSummaryCase):
    def test_failed_when_last_run_finished_with_ok_false(self) -> None:
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
            self.assertEqual("inspect-failure", summary.recommended_action)
            self.assertIn("verify-failed", summary.next_action_hint)

    def test_stale_when_runner_state_unfinished_without_lock(self) -> None:
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
                    "run_id": "20260619T120000Z-3",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": None,
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("stale", summary.primary_state)
            self.assertEqual("unlock", summary.recommended_action)
            self.assertIn("no lock file is present", summary.detail or "")

    def test_stale_when_lock_is_dead(self) -> None:
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
            self.assertEqual("unlock", summary.recommended_action)

    def test_recent_when_finished_ok_with_pending_sessions(self) -> None:
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
            self.assertEqual("run", summary.recommended_action)
            self.assertEqual("1", summary.next_pending_session_id)
            self.assertEqual(
                workbench_summary_model.ACTIVITY_LABEL_BY_STATE["recent"],
                summary.activity_label,
            )
            self.assertIn("2026-06-19T12:30:00+00:00", summary.detail or "")

    def test_preserves_registry_label_profile_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            (plan_dir / workbench_summary_helpers.FINAL_REVIEW_FILE_NAME).write_text(
                "# Final review\n", encoding="utf-8"
            )
            plans_progress.append_progress_entry(
                plan_dir,
                plans_models.ProgressEntry(
                    session_id=plans_session_ids.parse_session_id("1"),
                    status="complete",
                    completed_at="2026-06-19T14:22:00Z",
                ),
            )
            discovered = workbench_registry_models.DiscoveredPlan(
                plan_dir=plan_dir,
                sources=("state", "registry"),
                label="Alpha",
                profile=workbench_registry_models.LaunchProfile(
                    harness="claude", timeout=900
                ),
                exists=True,
            )

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertEqual("Alpha", summary.label)
            self.assertIsNotNone(summary.profile)
            self.assertEqual("claude", summary.profile.harness)
            self.assertEqual(900, summary.profile.timeout)
            self.assertEqual(("state", "registry"), summary.sources)
            self.assertIsNotNone(summary.final_review_path)
            self.assertTrue(summary.progress_path.name == "progress.md")

            payload = summary.to_payload()
            self.assertEqual("ready", payload["primary_state"])
            self.assertEqual("run", payload["recommended_action"])
            self.assertEqual(["state", "registry"], payload["sources"])
            self.assertEqual("Alpha", payload["label"])
            self.assertEqual("claude", payload["profile"]["harness"])
            self.assertEqual(str(plan_dir), payload["plan_dir"])
            self.assertEqual(str(summary.progress_path), payload["progress_path"])
            self.assertEqual(
                str(plan_dir / workbench_summary_helpers.FINAL_REVIEW_FILE_NAME),
                payload["final_review_path"],
            )

    def test_build_workbench_summaries_orders_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_a = _make_workbench_plan(tmp_path, "alpha")
            plan_b = _make_workbench_plan(tmp_path, "beta")
            discovered = [_discovered(plan_a), _discovered(plan_b)]

            summaries = workbench_summary.build_workbench_summaries(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual(2, len(summaries))
            self.assertEqual(plan_a, summaries[0].plan_dir)
            self.assertEqual(plan_b, summaries[1].plan_dir)
            for summary in summaries:
                self.assertEqual("ready", summary.primary_state)

    def test_corrupt_workspace_metadata_is_isolated_in_summary_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            healthy = _make_workbench_plan(tmp_path, "healthy")
            broken = tmp_path / "broken"
            write_multi_repo_plan(broken, ("pending",))
            (broken / plans_metadata.PLAN_METADATA_FILE_NAME).write_text(
                "{not-json", encoding="utf-8"
            )

            summaries = workbench_summary.build_workbench_summaries(
                [_discovered(healthy), _discovered(broken)],
                state_dir=tmp_path / "state",
            )

            self.assertEqual(2, len(summaries))
            self.assertEqual("ready", summaries[0].primary_state)
            self.assertEqual("invalid", summaries[1].primary_state)
            self.assertIn("workspace metadata unreadable", summaries[1].detail or "")
            self.assertEqual(
                "Workspace metadata cannot be inspected",
                summaries[1].recovery["title"],
            )

    def test_triage_fields_for_ready_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertEqual(
                workbench_summary_model.URGENCY_RANK_BY_STATE["ready"],
                summary.urgency_rank,
            )
            self.assertEqual("ready", summary.urgency_group)
            self.assertEqual(
                workbench_summary_model.ACTIVITY_LABEL_BY_STATE["ready"],
                summary.activity_label,
            )
            self.assertIn("session 1", summary.concise_reason)
            self.assertIsNone(summary.last_activity_at)
            self.assertIsNone(summary.last_activity_source)
            self.assertIn(plan_dir.name.lower(), summary.searchable_text)
            self.assertIn("ready", summary.searchable_text)
            self.assertIn("session 1", summary.searchable_text)


if __name__ == "__main__":
    unittest.main()
