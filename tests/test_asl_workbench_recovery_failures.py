"""Test asl workbench recovery failures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    preflight_runtime_hints,
    workbench_recovery_build,
)
from tests import support_asl_workbench_recovery as fixtures
from tests.support import (
    _discovered,
    _make_workbench_plan,
    _write_state_payload,
)

ASL_TEST_PORTABLE = True


class WorkbenchRecoveryTests(fixtures.WorkbenchRecoveryCase):
    def test_verify_failed_recovery_maps_every_failure_reason_to_runtime_hint(
        self,
    ) -> None:
        failure_reasons = (
            "command-failed",
            "command-timeout",
            "command-not-found",
            "command-error",
            "file-missing",
            "file-error",
            "regex-not-matched",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for failure_reason in failure_reasons:
                with self.subTest(failure_reason=failure_reason):
                    recovery = self._recovery_for_payload(
                        tmp_path,
                        {
                            "ok": False,
                            "paused": True,
                            "reason": "verify-failed",
                            "pause_reason": "verify-failed",
                            "pause_message": "verification failed",
                            "pause_details": {
                                "failures": [
                                    {
                                        "name": "check",
                                        "failure_reason": failure_reason,
                                    }
                                ]
                            },
                            "turns": [
                                {
                                    "session_id": "1",
                                    "turn_index": 1,
                                    "verification": {
                                        "reason": "verify-failed",
                                        "passed": False,
                                        "checks": [
                                            {
                                                "name": "check",
                                                "status": "fail",
                                                "failure_reason": failure_reason,
                                            }
                                        ],
                                    },
                                }
                            ],
                            "run_id": f"run-{failure_reason}",
                        },
                        plan_name=f"hint-{failure_reason}",
                    )
                    hint = preflight_runtime_hints.runtime_repair_hint(
                        "verify-failed", failure_reason=failure_reason
                    )
                    self.assertIsNotNone(hint)
                    assert hint is not None
                    self.assertTrue(
                        any(
                            row.label == "Repair hint"
                            and row.detail == hint.check_id
                            and row.value == hint.message
                            for row in recovery.evidence
                        )
                    )

    def test_generic_recovery_uses_latest_nonzero_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            older_artifact = tmp_path / "runs" / "run-exit" / "turn-0001"
            latest_artifact = tmp_path / "runs" / "run-exit" / "turn-0004"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "reason": "nonzero-exit",
                    "pause_message": "harness exited with return code 7",
                    "run_id": "run-exit",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "returncode": 2,
                            "result_path": str(older_artifact / "result.json"),
                        },
                        {
                            "session_id": "2",
                            "turn_index": 2,
                            "returncode": 0,
                            "result_path": str(
                                tmp_path / "runs" / "run-exit" / "turn-0002"
                            ),
                        },
                        {
                            "session_id": "3",
                            "turn_index": 4,
                            "returncode": 7,
                            "result_path": str(latest_artifact / "result.json"),
                        },
                    ],
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("generic", recovery.kind)
            self.assertEqual("3", recovery.session_id)
            self.assertEqual(4, recovery.turn_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(latest_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(older_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )

    def test_commit_failed_recovery_prefers_failed_commit_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            active_artifact = tmp_path / "runs" / "run-commit" / "turn-0001"
            commit_artifact = tmp_path / "runs" / "run-commit" / "commit-0001-1"
            commit = {
                "run_id": "run-commit",
                "commit_index": 1,
                "scope": "session",
                "session_id": "1",
                "artifact_dir": str(commit_artifact),
                "stdout_path": str(commit_artifact / "stdout.log"),
                "stderr_path": str(commit_artifact / "stderr.log"),
                "result_path": str(commit_artifact / "result.json"),
                "success": False,
                "status_result": "nonzero-exit",
                "returncode": 23,
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "commit-failed",
                    "pause_reason": "commit-failed",
                    "pause_message": "auto-commit failed after session 1",
                    "pause_details": {"commit": commit},
                    "active_turn": {
                        "run_id": "run-commit",
                        "session_id": "1",
                        "turn_index": 1,
                        "artifact_dir": str(active_artifact),
                        "result_path": str(active_artifact / "result.json"),
                    },
                    "commits": [commit],
                    "run_id": "run-commit",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("commit-failed", recovery.kind)
            self.assertEqual(1, recovery.commit_index)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-commit"
                    and artifact["path"] == str(commit_artifact / "result.json")
                    for artifact in artifact_payload
                )
            )
            self.assertFalse(
                any(
                    artifact["source"] == "active-turn" for artifact in artifact_payload
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(action["target"] == "launch" for action in action_payload)
            )
            self.assertTrue(
                any(action["target"] == "profile" for action in action_payload)
            )

    def test_commit_failed_recovery_matches_paused_and_finished_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            cases = (
                ("paused", True, None),
                ("finished", False, "2026-06-19T12:05:00+00:00"),
            )

            for name, paused, finished_at in cases:
                with self.subTest(lifecycle=name):
                    plan_dir = _make_workbench_plan(tmp_path, name=f"commit-{name}")
                    commit_artifact = (
                        tmp_path / "runs" / f"run-commit-{name}" / "commit-0002-1"
                    )
                    commit = {
                        "run_id": f"run-commit-{name}",
                        "commit_index": 1,
                        "scope": "session",
                        "session_id": "2",
                        "artifact_dir": str(commit_artifact),
                        "stdout_path": str(commit_artifact / "stdout.log"),
                        "stderr_path": str(commit_artifact / "stderr.log"),
                        "result_path": str(commit_artifact / "result.json"),
                        "success": False,
                        "status_result": "nonzero-exit",
                        "returncode": 23,
                    }
                    _write_state_payload(
                        plan_dir,
                        state_root,
                        {
                            "ok": False,
                            "paused": paused,
                            "reason": "commit-failed",
                            "pause_reason": "commit-failed" if paused else None,
                            "pause_message": "auto-commit failed after session 2",
                            "pause_details": {"commit": commit},
                            "run_id": f"run-commit-{name}",
                            "run_started_at": "2026-06-19T12:00:00+00:00",
                            "run_finished_at": finished_at,
                            "active_turn": None,
                            "active_verification": None,
                            "commits": [commit],
                        },
                    )

                    recovery = workbench_recovery_build.build_workbench_recovery(
                        _discovered(plan_dir), state_dir=state_root
                    )

                    self.assertEqual("commit-failed", recovery.kind)
                    self.assertEqual("2", recovery.session_id)
                    self.assertEqual(1, recovery.commit_index)
                    action_targets = {action.target for action in recovery.actions}
                    self.assertIn("artifacts", action_targets)
                    self.assertIn("launch", action_targets)
                    self.assertIn("profile", action_targets)
                    artifact_payload = [
                        artifact.to_payload() for artifact in recovery.artifacts
                    ]
                    self.assertTrue(
                        any(
                            artifact["source"] == "failed-commit"
                            and artifact["path"] == str(commit_artifact / "result.json")
                            for artifact in artifact_payload
                        )
                    )

    def test_harness_unavailable_recovery_guides_to_profile_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            config_path = tmp_path / "harnesses.json"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "harness-unavailable",
                    "pause_reason": "harness-unavailable",
                    "pause_message": "claude executable not found",
                    "pause_details": {
                        "session_id": "1",
                        "requested_harness": "claude",
                        "config_path": str(config_path),
                        "error": "claude executable not found on PATH",
                    },
                    "run_id": "run-harness",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("generic", recovery.kind)
            self.assertIn("Harness", recovery.title)
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(action["target"] == "profile" for action in action_payload)
            )
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["kind"] == "config"
                    and artifact["path"] == str(config_path)
                    for artifact in artifact_payload
                )
            )


if __name__ == "__main__":
    unittest.main()
