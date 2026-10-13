"""Run readiness lifecycle and plan-artifact guard tests."""

from __future__ import annotations

import unittest

from agent_session_loop import events, plans, preflight_environment
from tests.support_asl_run_readiness import RunReadinessGateFixture


class RunReadinessLifecycleTests(RunReadinessGateFixture):
    """Readiness follows the run lifecycle; packet tampering pauses on the guard."""

    def test_run_rejects_mismatched_prepared_root_without_overwriting_binding(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        previous = self.tmp_path / "previous-repo"
        previous.mkdir()
        plans.save_plan_metadata(self.plan_dir, previous)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("workspace-error", payload["reason"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )
        self.assertEqual(
            str(plans.canonicalize_path(previous)),
            plans.load_plan_metadata(self.plan_dir).cwd,
        )

    def test_lifecycle_returns_to_loop_ready_when_max_sessions_leaves_pending(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_run_uses_max_pending_sessions_override_for_readiness_gate(
        self,
    ) -> None:
        pending_count = preflight_environment.TURN_BUDGET_YELLOW_THRESHOLD + 5
        self._write_plan(tuple(["pending"] * pending_count), readiness="loop-ready")

        completed, payload = self._run(
            max_sessions=1,
            max_pending_sessions=pending_count,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual(pending_count, payload["max_pending_sessions"])
        gate = payload["readiness_gate"]
        self.assertTrue(gate["ok"])
        budget_findings = [
            finding
            for finding in gate["findings"]
            if finding["category"] == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0]["severity"])

    def test_note_written_into_another_sessions_packet_pauses_the_run(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(mode="success-future-note")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED, payload["pause_reason"]
        )
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(guard, payload["pause_details"]["plan_artifact_guard"])
        # The turn is refused whole, so the session it did select goes back to
        # pending rather than banking a completion earned against a plan it
        # rewrote underneath itself.
        self.assertEqual(
            "pending",
            plans.inspect_plan(self.plan_dir).status_by_id["1"],
        )

    def test_timeout_that_tampers_with_another_packet_pauses_on_the_guard(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(
            mode="timeout",
            # The deadline runs from the harness spawn, so it has to cover the
            # fake harness reaching its tamper: a freshly copied executable can
            # stall for seconds in Gatekeeper assessment on top of full-suite
            # parallel load, and a deadline that lands first leaves nothing for
            # the guard to see. The sleep stays far beyond the deadline so the
            # turn still times out.
            turn_timeout=30,
            env_overrides={
                "FAKE_CODEX_SLEEP": "120",
                "FAKE_CODEX_TAMPER_SESSION": "session-2.md",
            },
            process_timeout=90,
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["turns"][0]["timed_out"])
        # A bare timeout is a recoverable pause, so reporting one here would
        # relaunch against the packet the timed-out turn rewrote.
        self.assertEqual(
            events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED, payload["pause_reason"]
        )
        self.assertFalse(payload["pause_policy"]["recoverable"])
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertEqual("timeout", guard["result_disposition"])
        self.assertEqual(guard, payload["pause_details"]["plan_artifact_guard"])

    def test_structured_pause_that_tampers_with_another_packet_hits_the_guard(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(
            mode="runner-pause",
            env_overrides={"FAKE_CODEX_TAMPER_SESSION": "session-2.md"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual(
            events.PAUSE_REASON_PLAN_ARTIFACT_TAMPERED, payload["pause_reason"]
        )
        self.assertFalse(payload["pause_policy"]["recoverable"])
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual(["session-2.md"], guard["changed_artifacts"])
        self.assertEqual("runner-pause", guard["result_disposition"])
        self.assertEqual(guard, payload["pause_details"]["plan_artifact_guard"])

    def test_wrong_session_completion_keeps_its_status_diagnosis(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(mode="unexpected-progress")

        # Completing the wrong session also changes that session's artifact,
        # so the ordering below the status guards is what keeps this sharper
        # than a bare tamper report.
        self.assertEqual(1, completed.returncode)
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        # The plan-artifact guard still adjudicates the write (ASI-119), but
        # defers to the status diagnosis rather than replacing it.
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertTrue(guard["adjudicated_only"])
        self.assertFalse(guard["passed"])
        self.assertEqual("unexpected-status-change", guard["deferred_to_pause_reason"])

    def test_selected_session_completion_passes_the_plan_artifact_guard(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        guard = payload["turns"][0]["plan_artifact_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(1, guard["artifact_count"])

    def test_lifecycle_keeps_the_running_sha_in_sync_across_a_turn(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        self.assertIsNone(payload.get("readiness_state_refresh_error"))
        self.assertIsNotNone(payload.get("readiness_state_refreshed_at"))
        refreshed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", refreshed_metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            refreshed_metadata.readiness_sha256,
        )

    def test_lifecycle_marks_reviewed_when_run_pauses_on_failure(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run(mode="failure")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("reviewed", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", metadata.state)


if __name__ == "__main__":
    unittest.main()
