"""Abort cases for ControlQueueDrainInRunnerTests."""

from __future__ import annotations

import time
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    run_control,
    run_coordination_control_drain,
)
from tests.support import (
    read_jsonl,
)

from .asl_control_drain_support import ControlQueueDrainInRunnerTestsBase


class ControlQueueDrainInRunnerTests(ControlQueueDrainInRunnerTestsBase):
    def test_abort_arms_the_turn_signal_and_records_the_request(self) -> None:
        self.queue_abort(grace_seconds=30, reason="operator needs the machine")
        abort = harnesses.AbortSignal()
        rc_state = run_control.RunControlState(current_harness="codex")

        before = time.monotonic()
        self.drain(rc_state, abort=abort)
        after = time.monotonic()

        self.assertTrue(abort.armed)
        self.assertEqual("operator needs the machine", abort.reason)
        expiry = abort.expires_at(before)
        self.assertIsNotNone(expiry)
        assert expiry is not None
        # The window starts when the poll thread observes the command, so the
        # deadline sits one grace period past the drain, not past the run.
        self.assertGreaterEqual(expiry, before + 30.0)
        self.assertLessEqual(expiry, after + 30.0)

        self.assertTrue(self.control_payload["abort_turn_requested"])
        self.assertEqual(30, self.control_payload["abort_grace_seconds"])
        self.assertEqual(
            "operator needs the machine",
            self.control_payload["abort_reason"],
        )
        self.assertIsNotNone(self.control_payload["abort_deadline"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
            ],
            self.run_control_actions(),
        )

    def test_abort_while_finalizing_arms_at_once_and_names_itself(self) -> None:
        # The grace window buys a worker time to leave usable work behind. The
        # terminal audit is advisory and read-only, and the run holds the plan
        # lock while it waits, so the cancellation lands immediately.
        self.queue_abort(grace_seconds=120)
        abort = harnesses.AbortSignal()

        before = time.monotonic()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
            finalization=True,
        )

        self.assertTrue(abort.armed)
        expiry = abort.expires_at(before)
        assert expiry is not None
        self.assertLessEqual(expiry, time.monotonic())
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
            ],
            self.run_control_actions(),
        )

    def test_abort_request_event_carries_the_deadline(self) -> None:
        self.queue_abort(grace_seconds=45)
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=harnesses.AbortSignal(),
        )

        event_payload = [
            record["payload"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
            if record["payload"]["action"]
            == events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        ][0]
        self.assertTrue(event_payload["abort_requested"])
        self.assertEqual(45, event_payload["abort_grace_seconds"])
        # Session 7 renders the remaining grace from the ledger, so the deadline
        # has to be stamped before the request is recorded.
        self.assertEqual(
            self.control_payload["abort_deadline"],
            event_payload["abort_deadline"],
        )

    def test_omitted_grace_uses_the_producer_default(self) -> None:
        self.queue_abort()
        abort = harnesses.AbortSignal()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
        )

        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            self.control_payload["abort_grace_seconds"],
        )
        self.assertEqual(
            run_coordination_control_drain.ABORT_TURN_DEFAULT_REASON, abort.reason
        )

    def test_abort_appends_mailbox_guidance_naming_the_deadline(self) -> None:
        self.queue_abort(grace_seconds=15, reason="rebooting the host")
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=harnesses.AbortSignal(),
            mailbox_enabled=True,
        )

        pending = mailbox.read_mailbox(
            self.plan_dir, state_dir=self.state_dir
        ).pending_guidance
        self.assertEqual(1, len(pending))
        text = pending[0].text
        self.assertIn("rebooting the host", text)
        self.assertIn("15s grace", text)
        self.assertIn(str(self.control_payload["abort_deadline"]), text)
        self.assertIn("RUNNER_PAUSE: supervisor requested abort", text)

    def test_abort_without_mailbox_does_not_create_one(self) -> None:
        self.queue_abort()
        abort = harnesses.AbortSignal()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
        )

        # Appending would create the mailbox file and silently flip activation
        # for every later turn, so a disabled mailbox stays absent.
        self.assertEqual(
            [],
            list(
                mailbox.read_mailbox(
                    self.plan_dir, state_dir=self.state_dir
                ).pending_guidance
            ),
        )
        self.assertTrue(abort.armed)

    def test_failed_guidance_append_still_arms_the_abort(self) -> None:
        self.queue_abort(grace_seconds=5)
        abort = harnesses.AbortSignal()
        with mock.patch.object(
            mailbox_appenders,
            "append_guidance",
            side_effect=OSError("read-only mailbox"),
        ):
            self.drain(
                run_control.RunControlState(current_harness="codex"),
                abort=abort,
                mailbox_enabled=True,
            )

        self.assertTrue(abort.armed)
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
            ],
            self.run_control_actions(),
        )

    def test_abort_with_no_turn_in_flight_degrades_to_stop(self) -> None:
        self.queue_abort(reason="drain the queue")
        rc_state = run_control.RunControlState(current_harness="codex")

        self.drain(rc_state)

        # Nothing to kill, but dropping the command would make `asl control
        # abort` a silent no-op between turns.
        self.assertTrue(rc_state.stop_after_current)
        self.assertFalse(rc_state.snapshot()["abort_turn_requested"])
        self.assertTrue(self.control_payload["stop_after_current"])
        self.assertFalse(self.control_payload["abort_turn_requested"])
        self.assertIsNone(self.control_payload["abort_deadline"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_DEGRADED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
            self.run_control_actions(),
        )

    def test_repeated_abort_command_does_not_restart_the_window(self) -> None:
        self.queue_abort(command_id="abort-a", grace_seconds=60)
        abort = harnesses.AbortSignal()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
        )
        first_deadline = self.control_payload["abort_deadline"]
        rc_state = run_control.RunControlState(current_harness="codex")
        rc_state.request_abort_turn(grace_seconds=60)

        self.queue_abort(command_id="abort-b", grace_seconds=1)
        self.drain(rc_state, abort=abort)

        # The second command's shorter grace must not shorten a running window.
        self.assertEqual(first_deadline, self.control_payload["abort_deadline"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
            ],
            self.run_control_actions(),
        )
