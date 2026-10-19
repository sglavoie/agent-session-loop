"""Workbench recovery tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    events,
    review_continuation,
    review_repair,
    run_inputs,
    state,
    workbench_launch,
    workbench_recovery_build,
    workbench_registry_models,
)
from tests import support_asl_workbench_recovery as fixtures
from tests.support import (
    _discovered,
    _make_workbench_plan,
    _structured_review_finding,
    write_completed_review_plan,
    write_review_cycle_event,
)


class WorkbenchRecoveryTests(fixtures.WorkbenchRecoveryCase):
    def test_ready_plan_has_no_recovery_needed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=tmp_path / "state"
            )

            self.assertEqual("none", recovery.kind)
            self.assertFalse(recovery.recoverable)
            payload = recovery.to_payload()
            self.assertEqual("none", payload["compact"]["kind"])

    def test_review_budget_recovery_reports_required_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "budget-review",
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
                    "fingerprints": [],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("review-continuation", recovery.kind)
            self.assertEqual("Higher review-fix cap required", recovery.title)
            self.assertIn("1 or higher", recovery.summary)
            self.assertIn("--review-harness", recovery.summary)
            action = recovery.actions[0]
            self.assertEqual("review-profile", action.id)
            self.assertEqual("profile", action.target)
            self.assertIn("--review-harness", action.description)
            self.assertIn("--max-review-fix-cycles", action.description)
            payload = recovery.to_payload()
            self.assertEqual(
                "higher-cap-required",
                payload["review_resume"]["blocker"],
            )
            self.assertEqual(
                payload["review_resume"],
                payload["compact"]["review_resume"],
            )
            launch_argv = workbench_launch.build_launch_argv(
                plan_dir,
                profile=workbench_registry_models.LaunchProfile(
                    harness="codex",
                    review_harness="claude",
                    max_review_fix_cycles=payload["review_resume"][
                        "minimum_required_cap"
                    ],
                ),
            )
            self.assertEqual(
                "claude", launch_argv[launch_argv.index("--review-harness") + 1]
            )
            self.assertEqual(
                str(payload["review_resume"]["minimum_required_cap"]),
                launch_argv[launch_argv.index("--max-review-fix-cycles") + 1],
            )

    def test_completed_review_repairs_recommend_rereview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = write_completed_review_plan(
                tmp_path / "rereview",
                [_structured_review_finding()],
            )
            state_root = tmp_path / "state"
            materialized = review_repair.materialize_review_repair_sessions(
                plan_dir, state_dir=state_root
            )
            self.assertTrue(materialized.ok, materialized.error)
            child = materialized.children[0].path
            child.write_text(
                child.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete"
                ),
                encoding="utf-8",
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("review-continuation", recovery.kind)
            self.assertEqual("rereview", recovery.review_resume["action"])
            self.assertIn("review", recovery.summary.lower())
            self.assertIn("--review-harness", recovery.summary)
            self.assertEqual("review-profile", recovery.actions[0].id)
            self.assertTrue(recovery.recoverable)

    def test_ready_continuation_names_cap_above_runner_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path, "ready-continuation")
            required_cap = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES + 1
            resume = review_continuation.ReviewResume(
                source_cycle=2,
                review_session_id="2",
                source_disposition="cycles-disabled",
                action="materialize-repairs",
                eligible=True,
                blocker=None,
                historical_materializations=2,
                minimum_required_cap=required_cap,
                selected_cap=run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            )

            with mock.patch.object(
                review_continuation,
                "evaluate_review_continuation",
                return_value=resume,
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=tmp_path / "state"
                )

            self.assertEqual("Review continuation ready", recovery.title)
            self.assertIn("--review-harness", recovery.summary)
            self.assertIn(
                f"--max-review-fix-cycles to {required_cap}", recovery.summary
            )
            self.assertIn(f"{required_cap} or higher", recovery.actions[0].description)

    def test_ready_continuation_exhausted_group_avoids_cap_guidance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path, "ready-continuation-exhausted")
            required_cap = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES + 1
            resume = review_continuation.ReviewResume(
                source_cycle=2,
                review_session_id="2",
                source_disposition="cycle-cap-exhausted",
                action="materialize-repairs",
                eligible=True,
                blocker=None,
                historical_materializations=2,
                minimum_required_cap=required_cap,
                selected_cap=run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
                max_repair_group_attempts=2,
                repair_group_attempts={"runner": 2},
                repair_groups=(
                    {
                        "repair_group": "runner",
                        "repair_session_id": "2.1",
                        "finding_ids": ["F1"],
                    },
                ),
            )

            with mock.patch.object(
                review_continuation,
                "evaluate_review_continuation",
                return_value=resume,
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=tmp_path / "state"
                )

            self.assertEqual("Review continuation ready", recovery.title)
            self.assertIn('repair group "runner"', recovery.summary)
            self.assertIn("2 attempts", recovery.summary)
            self.assertIn("fix it manually or re-split", recovery.summary)
            self.assertNotIn(
                f"--max-review-fix-cycles to {required_cap}", recovery.summary
            )
            self.assertEqual("inspect-artifacts", recovery.actions[0].id)
            self.assertNotIn("--max-review-fix-cycles", recovery.actions[0].description)

    def test_ready_rereview_omits_cap_above_runner_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path, "ready-rereview")
            required_cap = run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES + 1
            resume = review_continuation.ReviewResume(
                source_cycle=2,
                review_session_id="2",
                source_disposition="repairs-materialized",
                action="rereview",
                eligible=True,
                blocker=None,
                historical_materializations=2,
                minimum_required_cap=required_cap,
                selected_cap=run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            )

            with mock.patch.object(
                review_continuation,
                "evaluate_review_continuation",
                return_value=resume,
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=tmp_path / "state"
                )

            self.assertEqual("Review continuation ready", recovery.title)
            self.assertIn("--review-harness", recovery.summary)
            self.assertIn("--review-harness", recovery.actions[0].description)
            self.assertNotIn("--max-review-fix-cycles", recovery.summary)
            self.assertNotIn("--max-review-fix-cycles", recovery.actions[0].description)

    def test_state_read_warning_details_cover_stable_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cases = (
                (
                    "state-malformed-json",
                    lambda path: path.write_text("{", encoding="utf-8"),
                    "state.json unreadable:",
                ),
                (
                    "state-unreadable",
                    lambda path: path.mkdir(),
                    "state.json unreadable:",
                ),
                (
                    "state-invalid-json-shape",
                    lambda path: path.write_text("[]", encoding="utf-8"),
                    "state.json did not contain a JSON object",
                ),
            )

            for expected_code, write_state, expected_prefix in cases:
                with self.subTest(code=expected_code):
                    plan_dir = _make_workbench_plan(
                        tmp_path, name=f"warning-{expected_code}"
                    )
                    state_root = tmp_path / "state"
                    state_path = (
                        state.plan_state_dir(plan_dir, state_root)
                        / events.STATE_FILE_NAME
                    )
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    write_state(state_path)

                    recovery = workbench_recovery_build.build_workbench_recovery(
                        _discovered(plan_dir), state_dir=state_root
                    )
                    payload = recovery.to_payload()

                    self.assertEqual(1, len(recovery.warnings))
                    self.assertTrue(recovery.warnings[0].startswith(expected_prefix))
                    self.assertEqual(list(recovery.warnings), payload["warnings"])
                    self.assertEqual(1, len(recovery.warning_details))
                    self.assertEqual(1, len(payload["warning_details"]))
                    self.assertEqual(expected_code, recovery.warning_details[0].code)
                    self.assertEqual(
                        recovery.warnings[0], recovery.warning_details[0].message
                    )
                    self.assertEqual("warning", recovery.warning_details[0].severity)
                    self.assertEqual(
                        {
                            "code": expected_code,
                            "message": recovery.warnings[0],
                            "severity": "warning",
                        },
                        payload["warning_details"][0],
                    )
                    self.assertEqual(1, payload["compact"]["warning_count"])
                    self.assertNotIn("warning_details", payload["compact"])

    def test_state_missing_race_warning_detail_has_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            state_path = (
                state.plan_state_dir(plan_dir, state_root) / events.STATE_FILE_NAME
            )
            original_exists = Path.exists
            original_read_text = Path.read_text

            def exists_with_state_race(path: Path) -> bool:
                if path == state_path:
                    return True
                return original_exists(path)

            def read_text_with_state_race(path: Path, *args, **kwargs) -> str:
                if path == state_path:
                    raise FileNotFoundError(state_path)
                return original_read_text(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "exists", new=exists_with_state_race),
                mock.patch.object(Path, "read_text", new=read_text_with_state_race),
            ):
                recovery = workbench_recovery_build.build_workbench_recovery(
                    _discovered(plan_dir), state_dir=state_root
                )

            payload = recovery.to_payload()

            self.assertEqual(
                ("state.json disappeared before it could be read",),
                recovery.warnings,
            )
            self.assertEqual(
                [
                    {
                        "code": "state-missing-race",
                        "message": recovery.warnings[0],
                        "severity": "warning",
                    }
                ],
                payload["warning_details"],
            )


if __name__ == "__main__":
    unittest.main()
