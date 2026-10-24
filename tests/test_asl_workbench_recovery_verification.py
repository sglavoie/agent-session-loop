"""Test asl workbench recovery verification."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
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
    def test_verify_failed_recovery_uses_runtime_hint_and_failed_turn_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-verify" / "turn-0001"
            result_path = artifact_dir / "result.json"
            turn = {
                "run_id": "run-verify",
                "session_id": "1",
                "turn_index": 1,
                "artifact_dir": str(artifact_dir),
                "stdout_path": str(artifact_dir / "stdout.log"),
                "stderr_path": str(artifact_dir / "stderr.log"),
                "last_message_path": str(artifact_dir / "last-message.txt"),
                "result_path": str(result_path),
                "verification": {
                    "session": "1",
                    "reason": "verify-failed",
                    "passed": False,
                    "checks": [
                        {
                            "name": "missing-file",
                            "status": "fail",
                            "failure_reason": "file-missing",
                        }
                    ],
                },
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed for session 1",
                    "pause_details": {
                        "failures": [
                            {
                                "name": "missing-file",
                                "failure_reason": "file-missing",
                            }
                        ]
                    },
                    "turns": [turn],
                    "run_id": "run-verify",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verify-failed", recovery.kind)
            self.assertEqual("1", recovery.session_id)
            self.assertEqual(1, recovery.turn_index)
            command_payload = [command.to_payload() for command in recovery.commands]
            self.assertTrue(
                any("asl verify" in command["command"] for command in command_payload)
            )
            self.assertTrue(
                any(
                    "asl preflight" in command["command"] for command in command_payload
                )
            )
            evidence_values = [row.value for row in recovery.evidence]
            self.assertIn("file-missing", evidence_values)
            self.assertTrue(
                any(
                    row.value and "Create the expected artifact" in row.value
                    for row in recovery.evidence
                )
            )
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(result_path)
                    for artifact in artifact_payload
                )
            )
            self.assertTrue(
                any(
                    row.label == "Failed check" and row.value == "missing-file"
                    for row in recovery.evidence
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/launch" and action["target"] == "launch"
                    for action in action_payload
                )
            )

    def test_verify_failed_recovery_follows_v2_check_artifact_references(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-verify" / "turn-0001"
            verification_dir = artifact_dir / "verification"
            stdout_artifact = verification_dir / "c1-stdout.gz"
            stderr_artifact = verification_dir / "c1-stderr.gz"
            turn = {
                "run_id": "run-verify",
                "session_id": "1",
                "turn_index": 1,
                "result_path": str(artifact_dir / "result.json"),
                "verification": {
                    "session": "1",
                    "reason": "verify-failed",
                    "passed": False,
                    "checks": [
                        {
                            "name": "c1",
                            "status": "fail",
                            "failure_reason": "command-failed",
                            "stdout_artifact_path": str(stdout_artifact),
                            "stderr_artifact_path": str(stderr_artifact),
                        }
                    ],
                },
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed for session 1",
                    "turns": [turn],
                    "run_id": "run-verify",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            check_refs = [
                artifact
                for artifact in artifact_payload
                if artifact["source"] == "verification-check"
            ]
            self.assertEqual(2, len(check_refs))
            paths = {artifact["path"] for artifact in check_refs}
            self.assertIn(str(stdout_artifact), paths)
            self.assertIn(str(stderr_artifact), paths)
            self.assertTrue(
                all(
                    artifact["kind"] == "verification-output" for artifact in check_refs
                )
            )

    def test_verify_failed_recovery_uses_latest_matching_turn_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            older_artifact = tmp_path / "runs" / "run-verify" / "turn-0001"
            latest_artifact = tmp_path / "runs" / "run-verify" / "turn-0005"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                    "pause_message": "verification failed",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "result_path": str(older_artifact / "result.json"),
                            "verification": {
                                "reason": "verify-failed",
                                "passed": False,
                                "checks": [
                                    {
                                        "name": "old",
                                        "status": "fail",
                                        "failure_reason": "command-failed",
                                    }
                                ],
                            },
                        },
                        {
                            "session_id": "2",
                            "turn_index": 5,
                            "result_path": str(latest_artifact / "result.json"),
                            "verification": {
                                "reason": "verify-failed",
                                "passed": False,
                                "checks": [
                                    {
                                        "name": "new",
                                        "status": "fail",
                                        "failure_reason": "file-error",
                                    }
                                ],
                            },
                        },
                    ],
                    "run_id": "run-verify",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verify-failed", recovery.kind)
            self.assertEqual("2", recovery.session_id)
            self.assertEqual(5, recovery.turn_index)
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

    def test_finished_verify_failed_uses_verification_failure_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-verify" / "turn-0003"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": False,
                    "reason": "verify-failed",
                    "pause_message": "verification failed after runner exit",
                    "run_id": "run-verify",
                    "run_started_at": "2026-06-19T12:00:00+00:00",
                    "run_finished_at": "2026-06-19T12:05:00+00:00",
                    "active_turn": None,
                    "active_verification": None,
                    "turns": [
                        {
                            "session_id": "2",
                            "turn_index": 3,
                            "result_path": str(artifact_dir / "result.json"),
                            "verification": {
                                "reason": "verify-failed",
                                "passed": False,
                                "checks": [
                                    {
                                        "name": "unit",
                                        "status": "fail",
                                        "failure_reason": "command-failed",
                                    }
                                ],
                            },
                        }
                    ],
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("verify-failed", recovery.kind)
            self.assertEqual("2", recovery.session_id)
            self.assertEqual(3, recovery.turn_index)
            command_payload = [command.to_payload() for command in recovery.commands]
            self.assertTrue(
                any("asl verify" in command["command"] for command in command_payload)
            )
            action_targets = {action.target for action in recovery.actions}
            self.assertIn("artifacts", action_targets)
            self.assertIn("launch", action_targets)
            artifact_payload = [
                artifact.to_payload() for artifact in recovery.artifacts
            ]
            self.assertTrue(
                any(
                    artifact["source"] == "failed-turn"
                    and artifact["path"] == str(artifact_dir / "result.json")
                    for artifact in artifact_payload
                )
            )


if __name__ == "__main__":
    unittest.main()
