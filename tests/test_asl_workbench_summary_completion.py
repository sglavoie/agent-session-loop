"""Test asl workbench summary completion."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_files,
    workbench_summary,
)
from tests import support_asl_workbench_summary as fixtures
from tests.support import (
    _discovered,
    _make_workbench_plan,
    _write_state_payload,
    write_plan,
)

ASL_TEST_PORTABLE = True


class WorkbenchSummaryTests(fixtures.WorkbenchSummaryCase):
    def test_assurance_summary_reports_what_the_audit_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "audited"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            state_root = tmp_path / "state"
            self._write_audited_state(
                canonical,
                state_root,
                {
                    "run_id": "20260619T120000Z-9",
                    "state": "findings",
                    "eligible": True,
                    "issue_ids": ["ASI-1", "ASI-2"],
                    "new_issue_count": 1,
                    "recurring_issue_count": 1,
                },
            )

            payload = workbench_summary.build_workbench_summary(
                _discovered(canonical), state_dir=state_root
            ).to_payload()

            self.assertEqual("findings", payload["assurance"]["state"])
            self.assertEqual(["ASI-1", "ASI-2"], payload["assurance"]["issue_ids"])
            self.assertEqual(2, payload["assurance"]["issue_count"])
            self.assertFalse(payload["assurance"]["issue_ids_truncated"])
            self.assertEqual(1, payload["assurance"]["new_issue_count"])
            self.assertEqual("asl assurance list", payload["assurance"]["list_command"])
            json.dumps(payload)

    def test_assurance_summary_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "audited-many"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            state_root = tmp_path / "state"
            self._write_audited_state(
                canonical,
                state_root,
                {
                    "run_id": "20260619T120000Z-9",
                    "state": "findings",
                    "eligible": True,
                    "issue_ids": [f"ASI-{index}" for index in range(1, 41)],
                    "new_issue_count": 40,
                    "recurring_issue_count": 0,
                },
            )

            payload = workbench_summary.build_workbench_summary(
                _discovered(canonical), state_dir=state_root
            ).to_payload()

            self.assertEqual(8, len(payload["assurance"]["issue_ids"]))
            self.assertEqual(40, payload["assurance"]["issue_count"])
            self.assertTrue(payload["assurance"]["issue_ids_truncated"])

    def test_assurance_summary_carries_no_audit_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "audited-controls"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            state_root = tmp_path / "state"
            self._write_audited_state(
                canonical,
                state_root,
                {
                    "run_id": "20260619T120000Z-9",
                    "state": "unavailable",
                    "eligible": True,
                    "reason": "timeout",
                    "role": {
                        "harness": "codex",
                        "source": "config",
                        "timeout_seconds": 600,
                    },
                    "issue_ids": ["ASI-1"],
                },
            )

            payload = workbench_summary.build_workbench_summary(
                _discovered(canonical), state_dir=state_root
            ).to_payload()

            self.assertTrue(payload["assurance"]["partial"])
            self.assertNotIn("role", payload["assurance"])
            self.assertNotIn("attempts", payload["assurance"])
            self.assertNotIn("artifacts", payload["assurance"])

    def test_assurance_summary_is_empty_without_an_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)

            payload = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            ).to_payload()

            self.assertEqual({}, payload["assurance"])

    def test_completed_at_falls_back_to_run_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-runfinished"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            state_root = tmp_path / "state"
            _write_state_payload(
                canonical,
                state_root,
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "20260619T120000Z-9",
                    "harness": "claude",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:45:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                },
            )
            discovered = _discovered(canonical)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("2026-06-19T12:45:00+00:00", summary.completed_at)
            self.assertEqual("run-finished", summary.last_activity_source)

    def test_completed_at_missing_when_no_timestamp_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-bare"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            discovered = _discovered(canonical)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertIsNone(summary.completed_at)
            self.assertIsNone(summary.to_payload()["completed_at"])

    def test_completed_at_null_for_non_complete_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertIsNone(summary.completed_at)
            payload = summary.to_payload()
            self.assertIn("completed_at", payload)
            self.assertIsNone(payload["completed_at"])


if __name__ == "__main__":
    unittest.main()
