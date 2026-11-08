"""Outcomes cases for AbortTurnOrchestrationTests."""

from __future__ import annotations

from unittest import mock

from agent_session_loop import (
    events,
    mailbox,
    turn_postflight,
)

from .asl_control_abort_turn_support import AbortTurnOrchestrationTestsBase


class AbortTurnOrchestrationTests(AbortTurnOrchestrationTestsBase):
    def test_killed_turn_stops_the_run_and_returns_the_session_to_pending(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
        )

        # A supervisor asked for this, so it is a clean stop and not a fault.
        self.assertEqual("stopped", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertIsNone(payload.get("pause_reason"))
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual("pending", self._session_status("1"))
        self.assertEqual(1, payload["final_snapshot"]["next_pending"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            ],
            self.run_control_actions(),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_killed_turn_records_why_the_session_holds_no_completion(
        self,
    ) -> None:
        self._run_loop(
            statuses=("pending",),
            abort_during_turn_index=1,
            reason="host reboot",
        )

        note = self._session_text("1")
        self.assertIn("turn aborted by supervisor request", note)
        self.assertIn("host reboot", note)
        self.assertIn("holds no completion", note)

    def test_a_guard_pause_still_acknowledges_the_killed_process(self) -> None:
        def refuse(ctx, **kwargs):
            ctx.pause(
                reason=events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
                message="guard evidence unavailable",
                details=kwargs["session_details"],
            )
            return turn_postflight.PostTurnGuards(
                stopped=True, terminating_disposition=None
            )

        with mock.patch.object(
            turn_postflight, "enforce_post_turn_guards", side_effect=refuse
        ):
            payload, harness_calls = self._run_loop(
                statuses=("pending",),
                abort_during_turn_index=1,
                reason="stop the stuck harness",
                expected_exit_code=1,
            )
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        self.assertFalse(payload["ok"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual("pending", self._session_status("1"))
        self.assertEqual(
            1,
            self.run_control_actions().count(
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED
            ),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_killed_turn_leaves_the_abort_guidance_readable(self) -> None:
        self._run_loop(
            statuses=("pending",),
            abort_during_turn_index=1,
            enable_mailbox=True,
            reason="host reboot",
        )

        pending = mailbox.read_mailbox(
            self.plan_dir, state_dir=self.state_dir
        ).pending_guidance
        self.assertEqual(1, len(pending))
        self.assertIn("host reboot", pending[0].text)
        self.assertIn("Supervisor abort requested", pending[0].text)

    def test_compliant_turn_is_recorded_as_satisfied_without_a_kill(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending",),
            abort_during_turn_index=1,
            worker_complies=True,
        )

        # The turn ended on its own inside the window, so nothing was killed
        # and the completion path stays in charge of the outcome.
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual("complete", self._session_status("1"))
        self.assertEqual(["codex"], harness_calls)
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            ],
            self.run_control_actions(),
        )
        self.assertNotIn("turn aborted by supervisor request", self._session_text("1"))
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_compliant_completion_lets_the_run_continue_to_the_next_session(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
            worker_complies=True,
        )

        # Nothing was killed and nothing was reverted, so the abort buys the
        # supervisor no stop at all: the loop moves on to session 2.
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertEqual(["codex", "codex"], harness_calls)
        self.assertEqual("all-complete", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            ],
            self.run_control_actions(),
        )

    def test_worker_that_self_pauses_inside_the_window_pauses_the_run(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
            worker_self_pauses=True,
            expected_exit_code=1,
        )

        # The worker wound down as asked, so its own pause owns the outcome:
        # the run is paused, not stopped, and carries the worker's reason.
        self.assertTrue(payload["paused"])
        self.assertFalse(payload["ok"])
        self.assertEqual("blocked", payload["reason"])
        self.assertEqual("blocked", payload["pause_reason"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual("pending", self._session_status("1"))
        self.assertNotIn("turn aborted by supervisor request", self._session_text("1"))
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            ],
            self.run_control_actions(),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_turn_timeout_inside_the_window_is_not_worker_compliance(
        self,
    ) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
            runner_ends_turn="timed_out",
            expected_exit_code=1,
        )

        # The runner's own turn timeout killed the worker, so crediting the
        # worker would let `asl control abort --wait` report compliance that
        # never happened. The abort is still consumed: nothing stays pending.
        self.assertTrue(payload["paused"])
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_PREEMPTED,
            ],
            self.run_control_actions(),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])

    def test_interrupt_inside_the_window_is_not_worker_compliance(self) -> None:
        payload, harness_calls = self._run_loop(
            statuses=("pending", "pending"),
            abort_during_turn_index=1,
            runner_ends_turn="interrupted",
            expected_exit_code=130,
        )

        self.assertTrue(payload["paused"])
        self.assertEqual(events.PAUSE_REASON_INTERRUPTED, payload["pause_reason"])
        self.assertEqual(["codex"], harness_calls)
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_PREEMPTED,
            ],
            self.run_control_actions(),
        )
        self.assertFalse(payload["control"]["abort_turn_requested"])
