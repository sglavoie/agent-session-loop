"""Test asl workbench recovery pause."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    intervention,
    redirect_cli,
    workbench_recovery_build,
    workbench_recovery_models,
)
from tests import support_asl_workbench_recovery as fixtures
from tests.support import (
    _discovered,
    _make_workbench_plan,
    _write_state_payload,
)

ASL_TEST_PORTABLE = True


class WorkbenchRecoveryTests(fixtures.WorkbenchRecoveryCase):
    def test_conversation_recovery_reports_the_decision_and_its_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            recovery = self._recovery_for_payload(
                tmp_path,
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
                    "pause_reason": (
                        events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
                    ),
                    "pause_message": "recorded conversation cannot be resumed",
                    "run_id": "run-fresh",
                },
                plan_name="fresh-conversation",
            )

            self.assertEqual("conversation-recovery", recovery.kind)
            self.assertTrue(recovery.recoverable)
            expected = redirect_cli.recovery_commands(
                recovery.plan_dir,
                str(state_root),
                decision=intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
            )
            self.assertEqual(
                [expected["inspect_command"], expected["authorization_command"]],
                [command.command for command in recovery.commands],
            )
            # The card never performs the recovery: consuming a one-use
            # authorization has to be a deliberate command, not a POST the
            # browser can repeat.
            self.assertNotIn("endpoint", {action.kind for action in recovery.actions})
            self.assertIn(
                redirect_cli.FRESH_CONVERSATION_NOTE,
                [row.detail for row in recovery.evidence],
            )
            self.assertIn(
                intervention.RECOVERY_DECISION_FRESH_CONVERSATION,
                [row.value for row in recovery.evidence],
            )

    def test_blocked_successor_recovery_offers_no_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recovery = self._recovery_for_payload(
                tmp_path,
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
                    "pause_reason": events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED,
                    "pause_message": "predecessor run could not be shown stopped",
                    "run_id": "run-blocked",
                },
                plan_name="successor-blocked",
            )

            self.assertEqual("conversation-recovery", recovery.kind)
            self.assertEqual("error", recovery.severity)
            # One command, and it is the read. Offering a second would imply an
            # authorization exists for a pause that has none.
            self.assertEqual(1, len(recovery.commands))
            self.assertIn("redirect-status", recovery.commands[0].command)
            self.assertNotIn(
                redirect_cli.FRESH_CONVERSATION_NOTE,
                [row.detail for row in recovery.evidence],
            )
            self.assertIn(
                intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE,
                [row.value for row in recovery.evidence],
            )

    def test_submission_recovery_offers_inspection_and_an_explicit_decision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recovery = self._recovery_for_payload(
                Path(tmp),
                {
                    "ok": False,
                    "paused": True,
                    "reason": events.PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
                    "pause_reason": events.PAUSE_REASON_REDIRECT_SUBMISSION_UNRESOLVED,
                    "pause_message": "The native submission outcome is unknown",
                    "run_id": "run-uncertain",
                },
                plan_name="submission-uncertain",
            )
            self.assertEqual("conversation-recovery", recovery.kind)
            self.assertEqual(2, len(recovery.commands))
            self.assertIn("redirect-status", recovery.commands[0].command)
            self.assertIn("--outcome OUTCOME", recovery.commands[1].command)
            self.assertIn("resolve-redirect", recovery.commands[1].command)
            self.assertTrue(
                all(action.kind != "endpoint" for action in recovery.actions)
            )

    def test_registered_pause_reasons_have_explicit_recovery_mapping(self) -> None:
        mapped_reasons = set(
            workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON
        )
        generic_reasons = set(workbench_recovery_models.GENERIC_RECOVERY_PAUSE_REASONS)
        registered_reasons = set(events.PAUSE_REASONS)

        self.assertFalse(mapped_reasons & generic_reasons)
        self.assertEqual(registered_reasons, mapped_reasons | generic_reasons)
        self.assertNotIn(
            "generic",
            set(
                workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON.values()
            ),
        )
        self.assertLessEqual(
            set(
                workbench_recovery_models.PAUSE_REASON_RECOVERY_KIND_BY_REASON.values()
            ),
            set(workbench_recovery_models.RECOVERY_KINDS) - {"generic"},
        )
        self.assertIn(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            generic_reasons,
        )

    def test_generic_pause_reason_allowlist_uses_generic_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            artifact_dir = tmp_path / "runs" / "run-exit" / "turn-0001"
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "nonzero-exit",
                    "pause_reason": "nonzero-exit",
                    "pause_message": "harness exited with status 2",
                    "turns": [
                        {
                            "session_id": "1",
                            "turn_index": 1,
                            "returncode": 2,
                            "result_path": str(artifact_dir / "result.json"),
                        }
                    ],
                    "run_id": "run-exit",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("generic", recovery.kind)
            action_targets = {action.target for action in recovery.actions}
            self.assertNotIn("reply", action_targets)

    def test_unknown_pause_reason_falls_back_by_runner_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_root = tmp_path / "state"
            unknown_reason = "new-runner-reason"
            cases = (
                (
                    "paused",
                    "reply-needed",
                    {
                        "ok": False,
                        "paused": True,
                        "reason": unknown_reason,
                        "pause_reason": unknown_reason,
                        "pause_message": "operator input required",
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 2,
                                "last_message_path": str(
                                    tmp_path
                                    / "runs"
                                    / "run-paused"
                                    / "turn-0002"
                                    / "last-message.txt"
                                ),
                            }
                        ],
                        "run_id": "run-paused",
                    },
                    "reply",
                ),
                (
                    "finished",
                    "generic",
                    {
                        "ok": False,
                        "paused": False,
                        "reason": unknown_reason,
                        "pause_message": "runner stopped with a new reason",
                        "run_id": "run-finished",
                        "run_started_at": "2026-06-19T12:00:00+00:00",
                        "run_finished_at": "2026-06-19T12:05:00+00:00",
                        "turns": [
                            {
                                "session_id": "1",
                                "turn_index": 2,
                                "returncode": 2,
                                "result_path": str(
                                    tmp_path
                                    / "runs"
                                    / "run-finished"
                                    / "turn-0002"
                                    / "result.json"
                                ),
                            }
                        ],
                    },
                    "launch",
                ),
            )

            for name, expected_kind, payload, expected_target in cases:
                with self.subTest(lifecycle=name):
                    plan_dir = _make_workbench_plan(tmp_path, name=f"unknown-{name}")
                    _write_state_payload(plan_dir, state_root, payload)

                    recovery = workbench_recovery_build.build_workbench_recovery(
                        _discovered(plan_dir), state_dir=state_root
                    )

                    self.assertEqual(expected_kind, recovery.kind)
                    self.assertEqual("1", recovery.session_id)
                    self.assertEqual(2, recovery.turn_index)
                    action_targets = {action.target for action in recovery.actions}
                    self.assertIn(expected_target, action_targets)
                    if expected_kind == "generic":
                        self.assertNotIn("reply", action_targets)

    def test_not_loop_ready_recovery_surfaces_gate_and_repair_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan_dir = _make_workbench_plan(tmp_path)
            state_root = tmp_path / "state"
            gate = {
                "ok": False,
                "readiness_state": "reviewed",
                "expected_readiness_sha256": "e" * 64,
                "recorded_readiness_sha256": "r" * 64,
                "findings_by_severity": {"red": 1, "yellow": 0, "green": 0},
                "findings": [
                    {
                        "severity": "red",
                        "category": "manifest-readiness",
                        "message": "readiness state is reviewed",
                    }
                ],
            }
            _write_state_payload(
                plan_dir,
                state_root,
                {
                    "ok": False,
                    "paused": True,
                    "reason": "not-loop-ready",
                    "pause_reason": "not-loop-ready",
                    "pause_message": "preflight failed: 1 red finding(s)",
                    "pause_details": gate,
                    "readiness_gate": gate,
                    "run_id": "run-ready",
                },
            )

            recovery = workbench_recovery_build.build_workbench_recovery(
                _discovered(plan_dir), state_dir=state_root
            )

            self.assertEqual("not-loop-ready", recovery.kind)
            self.assertTrue(recovery.recoverable)
            self.assertGreaterEqual(len(recovery.commands), 2)
            self.assertIn("--mark-loop-ready", recovery.commands[0].command)
            self.assertTrue(
                any(
                    "plan-preflight-iterate" in command.command
                    for command in recovery.commands
                )
            )
            action_payload = [action.to_payload() for action in recovery.actions]
            self.assertTrue(
                any(
                    action["endpoint"] == "/api/launch" and action["target"] == "launch"
                    for action in action_payload
                )
            )
            evidence_payload = [row.to_payload() for row in recovery.evidence]
            self.assertTrue(
                any(row["value"] == "manifest-readiness" for row in evidence_payload)
            )


if __name__ == "__main__":
    unittest.main()
