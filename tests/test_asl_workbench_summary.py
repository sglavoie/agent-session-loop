"""Workbench summary tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_files,
    plans_metadata,
    plans_registry,
    plans_snapshot,
    state_status,
    workbench_recovery_build,
    workbench_summary,
)
from tests import support_asl_workbench_summary as fixtures
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _discovered,
    _make_workbench_plan,
    _structured_review_finding,
    _write_state_payload,
    init_multi_repo_workspace,
    write_completed_review_plan,
    write_multi_repo_plan,
    write_plan,
    write_review_cycle_event,
)

ASL_TEST_PORTABLE = True


class WorkbenchSummaryTests(fixtures.WorkbenchSummaryCase):
    def test_registered_summary_exposes_workspace_and_ordered_repositories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_path / "workspace"
            init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_path / "plan"
            write_multi_repo_plan(plan_dir, ("pending",), specs=specs)
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir,
                workspace_root,
                registry.digest,
            )
            metadata_path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            legacy = json.loads(metadata_path.read_text(encoding="utf-8"))
            legacy["workspace_inventory_excludes"] = ["Library/Protected.dv"]
            metadata_path.write_text(json.dumps(legacy), encoding="utf-8")

            payload = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            ).to_payload()

            self.assertEqual(str(workspace_root.resolve()), payload["cwd"])
            self.assertEqual(str(workspace_root.resolve()), payload["workspace_root"])
            self.assertEqual(2, payload["repository_count"])
            self.assertNotIn("workspace_inventory_excludes", payload)
            self.assertEqual(
                ["SVC", "OAK"], [row["alias"] for row in payload["repositories"]]
            )

    def test_a_legacy_run_state_still_names_its_working_directory(self) -> None:
        # A single-repository run recorded only ``cwd``; without it the catalog
        # has nothing to name the project from and falls back to "Unknown".
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "plan"
            write_plan(plan_dir, ("complete", "complete"))
            repo = tmp_path / "raspberry-pi"
            repo.mkdir()
            _write_state_payload(
                plan_dir,
                tmp_path / "state",
                {
                    "ok": True,
                    "paused": False,
                    "run_id": "run-legacy",
                    "cwd": str(repo),
                    "active_turn": None,
                    "turns": [],
                },
            )

            payload = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            ).to_payload()

            self.assertEqual(str(repo), payload["cwd"])
            self.assertIsNone(payload.get("workspace_root"))

    def test_invalid_when_plan_directory_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing = tmp_path / "ghost"
            discovered = _discovered(missing, exists=False)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("invalid", summary.primary_state)
            self.assertEqual("repair-plan", summary.recommended_action)
            self.assertFalse(summary.exists)
            self.assertIsNotNone(summary.detail)
            self.assertIn(str(missing), summary.detail)

    def test_invalid_when_plan_files_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "broken"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# C\n", encoding="utf-8")
            discovered = _discovered(plans_files.canonicalize_path(plan_dir))

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("invalid", summary.primary_state)
            self.assertEqual("repair-plan", summary.recommended_action)
            self.assertTrue(summary.exists)
            self.assertIn("manifest.md", summary.detail or "")
            self.assertIsNotNone(summary.state_path)
            self.assertIsNotNone(summary.progress_path)

    def test_ready_state_without_runner_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            discovered = _discovered(plan_dir)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("ready", summary.primary_state)
            self.assertEqual("run", summary.recommended_action)
            self.assertEqual("1", summary.next_pending_session_id)
            self.assertIsNotNone(summary.next_pending_session_path)
            self.assertEqual(2, summary.counts["pending"])
            self.assertIsNone(summary.run_id)
            self.assertEqual((), summary.warnings)

    def test_complete_when_no_pending_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = tmp_path / "done-plan"
            write_plan(plan_dir, ("complete", "complete"))
            canonical = plans_files.canonicalize_path(plan_dir)
            discovered = _discovered(canonical)

            summary = workbench_summary.build_workbench_summary(
                discovered, state_dir=tmp_path / "state"
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("view", summary.recommended_action)
            self.assertIsNone(summary.next_pending_session_id)
            self.assertEqual(2, summary.counts["complete"])

    def test_review_resume_cap_guidance_matches_status_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "review-budget",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "cycles-disabled",
                    "fingerprints": ["finding:legacy"],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            )
            plan_before = {
                path.relative_to(plan_dir): path.read_bytes()
                for path in plan_dir.rglob("*")
                if path.is_file()
            }
            state_before = {
                path.relative_to(state_root): path.read_bytes()
                for path in state_root.rglob("*")
                if path.is_file()
            }

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            status_payload = state_status.status_payload(
                plans_snapshot.inspect_plan(plan_dir), state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("view", summary.recommended_action)
            self.assertIn("cap of 1", summary.next_action_hint)
            self.assertEqual(status_payload["review_resume"], summary.review_resume)
            self.assertEqual(summary.review_resume, summary.recovery["review_resume"])
            self.assertEqual("higher-cap-required", summary.review_resume["blocker"])
            self.assertEqual(
                plan_before,
                {
                    path.relative_to(plan_dir): path.read_bytes()
                    for path in plan_dir.rglob("*")
                    if path.is_file()
                },
            )
            self.assertEqual(
                state_before,
                {
                    path.relative_to(state_root): path.read_bytes()
                    for path in state_root.rglob("*")
                    if path.is_file()
                },
            )

    def test_review_resume_without_recorded_cap_requests_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "review-cap-required",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("higher-cap-required", summary.review_resume["blocker"])
            self.assertIsNone(summary.review_resume["selected_cap"])
            self.assertEqual(
                "Review repairs require a review-fix cap to resume",
                summary.detail,
            )
            self.assertIn("Supply a review-fix cap of 1", summary.next_action_hint)
            self.assertNotIn("exhaust", summary.detail.lower())
            self.assertEqual("Review-fix cap required", recovery.title)
            self.assertIn("after supplying", recovery.summary)
            self.assertIn("--review-harness", recovery.actions[0].description)
            self.assertNotIn("exhaust", recovery.summary.lower())
            self.assertEqual(recovery.summary, summary.recovery["summary"])

    def test_missing_review_repairs_surface_inconsistent_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "review-missing-repairs",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            write_review_cycle_event(
                plan_dir,
                state_root,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": "repairs-materialized",
                    "fingerprints": ["finding:legacy"],
                    "repair_session_ids": ["2.1"],
                    "max_review_fix_cycles": 1,
                },
            )

            summary = workbench_summary.build_workbench_summary(
                _discovered(plan_dir), state_dir=state_root
            )
            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("complete", summary.primary_state)
            self.assertEqual("terminal", summary.review_resume["action"])
            self.assertEqual(
                "repair-sessions-missing", summary.review_resume["blocker"]
            )
            self.assertEqual("error", recovery.severity)
            self.assertEqual("Review continuation is inconsistent", recovery.title)
            self.assertEqual(recovery.title, summary.next_action_hint)
            self.assertEqual(recovery.summary, summary.detail)
            self.assertNotEqual("No pending sessions remain", summary.detail)


if __name__ == "__main__":
    unittest.main()
