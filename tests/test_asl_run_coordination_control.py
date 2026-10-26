"""Test asl run coordination control."""

from __future__ import annotations

import threading
import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    mailbox,
    mailbox_vocabulary,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    run_coordination_mailbox_poll,
)
from tests import support_asl_run_coordination as fixtures


class ControlDrainTests(fixtures.ControlDrainTestCase):
    """Every accepted command leaves exactly one receipt, before its transition."""

    def test_the_receipt_precedes_the_transition_it_causes(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        self.drain()

        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
        )
        self.assertTrue(self.control_payload["stop_after_current"])
        self.assertTrue(self.payload["control"]["stop_after_current"])

    def test_the_receipt_carries_the_command_identity_asl_control_tails(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        self.drain()

        receipt = self.persistence.payloads(events.RUN_CONTROL_EVENT)[0]
        self.assertEqual(receipt["command_id"], "cmd-1")
        self.assertEqual(
            receipt["queue_command"], control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT
        )
        self.assertEqual(receipt["session"], fixtures.SESSION_ID)
        self.assertEqual(receipt["turn_index"], 1)

    def test_a_duplicate_id_in_one_queue_file_is_accepted_once(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        result = self.drain()

        self.assertEqual([command.id for command in result.accepted], ["cmd-1"])
        self.assertEqual(
            [rejected.reason for rejected in result.rejected],
            [control_queue.REJECT_DUPLICATE_ID],
        )
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT).count(
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
            ),
            1,
        )

    def test_a_replayed_id_after_the_drain_leaves_no_second_receipt(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        self.drain()
        before = len(self.persistence.events)
        # An external producer rewrites the queue with the same command.
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")

        result = self.drain()

        self.assertEqual(result.accepted, ())
        self.assertEqual(len(self.persistence.events), before)

    def test_an_already_applied_request_still_leaves_a_receipt(self) -> None:
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        self.drain()
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-2")

        self.drain()

        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
            ],
        )

    def test_a_harness_selection_names_both_harnesses(self) -> None:
        self.queue(
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            command_id="cmd-1",
            harness="claude",
        )

        self.drain(context={"harness": "codex"})

        selected = self.persistence.payloads(events.RUN_CONTROL_EVENT)[-1]
        self.assertEqual(selected["action"], events.RUN_CONTROL_ACTION_HARNESS_SELECTED)
        self.assertEqual(selected["current_harness"], "codex")
        self.assertEqual(selected["selected_harness"], "claude")

    def test_an_unreadable_queue_degrades_to_an_empty_drain(self) -> None:
        with mock.patch.object(
            run_coordination_control_drain.control_queue,
            "drain_queue",
            side_effect=OSError("nope"),
        ):
            result = self.drain()

        self.assertEqual(result.accepted, ())
        self.assertEqual(self.persistence.events, [])

    def test_a_wakeup_is_cleared_before_the_read(self) -> None:
        wakeup = threading.Event()
        wakeup.set()
        observed: list[bool] = []
        real_drain = control_queue.drain_queue

        def watching_drain(*args: Any, **kwargs: Any) -> control_queue.DrainResult:
            observed.append(wakeup.is_set())
            return real_drain(*args, **kwargs)

        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        with mock.patch.object(
            run_coordination_control_drain.control_queue, "drain_queue", watching_drain
        ):
            self.drain(wakeup=wakeup)

        self.assertEqual(observed, [False])

    def test_two_concurrent_drains_accept_the_command_once(self) -> None:
        """Deterministic because the queue file is read-and-truncated under flock.

        Whichever drain wins the lock takes the command with it; the other
        finds an empty queue. Either interleaving leaves one receipt.
        """
        self.queue(control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT, command_id="cmd-1")
        release = threading.Barrier(2)
        failures: list[BaseException] = []

        def drain_once() -> None:
            try:
                release.wait(timeout=5.0)
                self.drain()
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=drain_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(failures, [])
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
        )


class AbortCancellationTests(fixtures.ControlDrainTestCase):
    """An abort either arms a kill or degrades; it never disappears."""

    def test_an_abort_with_a_turn_in_flight_arms_the_kill_and_warns_the_worker(
        self,
    ) -> None:
        abort = harnesses.AbortSignal()
        self.queue(
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            command_id="cmd-1",
            grace_seconds=30,
            reason="rethinking the plan",
        )

        self.drain(abort=abort, mailbox_enabled=True)

        self.assertTrue(abort.armed)
        self.assertEqual(abort.reason, "rethinking the plan")
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
            ],
        )
        self.assertIsNotNone(self.control_payload["abort_deadline"])
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        guidance = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_GUIDANCE
        ]
        self.assertEqual(len(guidance), 1)
        self.assertIn("rethinking the plan", guidance[0].text or "")

    def test_an_abort_with_no_turn_in_flight_degrades_to_stop_after_current(
        self,
    ) -> None:
        self.queue(
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            command_id="cmd-1",
            reason="never mind",
        )

        self.drain(abort=None)

        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_DEGRADED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
        )
        self.assertTrue(self.control_state.stop_after_current)
        self.assertFalse(self.control_payload["abort_turn_requested"])
        self.assertIsNone(self.control_payload["abort_deadline"])
        degraded = self.persistence.payloads(events.RUN_CONTROL_EVENT)[2]
        self.assertIn("no turn in flight", degraded["message"])

    def test_a_finalization_abort_arms_immediately_and_warns_nobody(self) -> None:
        abort = harnesses.AbortSignal()
        self.queue(
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            command_id="cmd-1",
            grace_seconds=120,
            reason="stop auditing",
        )

        self.drain(abort=abort, mailbox_enabled=True, finalization=True)

        self.assertTrue(abort.armed)
        self.assertEqual(
            self.persistence.actions(events.RUN_CONTROL_EVENT),
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
            ],
        )
        honored = self.persistence.payloads(events.RUN_CONTROL_EVENT)[-1]
        self.assertIn("cancelling the terminal audit", honored["message"])
        view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir_arg)
        self.assertEqual(
            [
                message
                for message in view.messages
                if message.kind == mailbox_vocabulary.KIND_GUIDANCE
            ],
            [],
        )

    def test_an_abort_carrying_no_reason_still_reads_as_an_explanation(self) -> None:
        abort = harnesses.AbortSignal()
        action = run_control.RunControlState().request_abort_turn()

        run_coordination_control_drain.arm_requested_abort(
            action,
            abort=abort,
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir_arg,
            control_payload=self.control_payload,
            mailbox_enabled=False,
        )

        self.assertEqual(
            abort.reason, run_coordination_control_drain.ABORT_TURN_DEFAULT_REASON
        )

    def test_a_failed_guidance_append_does_not_lose_the_armed_abort(self) -> None:
        abort = harnesses.AbortSignal()
        action = run_control.RunControlState().request_abort_turn(reason="stop")

        with mock.patch.object(
            run_coordination_mailbox_poll.mailbox_appenders,
            "append_guidance",
            side_effect=OSError("mailbox gone"),
        ):
            run_coordination_control_drain.arm_requested_abort(
                action,
                abort=abort,
                plan_dir=self.plan_dir,
                state_dir_arg=self.state_dir_arg,
                control_payload=self.control_payload,
                mailbox_enabled=True,
            )

        self.assertTrue(abort.armed)
        self.assertEqual(abort.reason, "stop")


class HarnessNameGuardTests(unittest.TestCase):
    """The restated literals must keep agreeing with the runtime membership."""

    def test_the_restated_literals_cover_every_known_harness(self) -> None:
        self.assertEqual(
            {name for name in harnesses.HARNESS_NAMES},
            {"codex", "claude", "copilot"},
        )

    def test_every_known_harness_narrows_to_itself(self) -> None:
        for name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=name):
                self.assertEqual(run_coordination_control.harness_name(name), name)

    def test_an_unknown_name_degrades_to_no_harness(self) -> None:
        self.assertIsNone(run_coordination_control.harness_name("gpt-9"))
        self.assertIsNone(run_coordination_control.harness_name(None))
        self.assertIsNone(run_coordination_control.harness_name(""))


if __name__ == "__main__":
    unittest.main()
