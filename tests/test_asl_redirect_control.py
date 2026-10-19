"""OwnedWorkerTests, EscalationTimingTests, ArmBeforeAcceptanceTests coverage."""

from __future__ import annotations

import threading
import unittest

from agent_session_loop import (
    harnesses,
    redirect_control,
)
from tests.asl_redirect_control_support import (
    PATIENCE,
    _Clock,
    _controller,
    _FakeCancellation,
    _Tools,
)


class OwnedWorkerTests(unittest.TestCase):
    def test_a_session_leader_is_its_own_process_group(self) -> None:
        worker = redirect_control.OwnedWorker.session_leader(4242)

        self.assertEqual(4242, worker.pid)
        self.assertEqual(4242, worker.pgid)
        self.assertEqual({"pid": 4242, "pgid": 4242}, worker.to_payload())

    def test_the_protocols_describe_the_real_harness_types(self) -> None:
        # The controller declares structural protocols rather than importing
        # the harness layer; if the real types drift, this is where it shows.
        signal_ = harnesses.AbortSignal()
        self.assertTrue(hasattr(signal_, "arm"))
        self.assertTrue(hasattr(signal_, "completed"))
        self.assertTrue(hasattr(signal_, "wait_completed"))
        self.assertTrue(hasattr(harnesses.HarnessTransport, "interrupt"))


class EscalationTimingTests(unittest.TestCase):
    """Injected clocks: when the force-stop lands, and what it does not wait for."""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.tools = _Tools()

    def _armed(self, **kwargs) -> redirect_control.UrgentController:
        controller = _controller(clock=self.clock, tools=self.tools, **kwargs)
        # LIFO: the join is registered first so it runs last, after the
        # release it is waiting on.
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        return controller

    def test_the_deadline_is_the_window_measured_from_arming(self) -> None:
        controller = self._armed()

        deadline = controller.arm(operation_id="op-1")

        self.assertAlmostEqual(1002.0, deadline)
        self.assertAlmostEqual(1002.0, controller.deadline)
        self.assertEqual(redirect_control.PHASE_ARMED, controller.phase)

    def test_nothing_is_stopped_before_the_deadline(self) -> None:
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.confirm()

        self.clock.advance(1.9)

        self.assertFalse(controller.wait_escalated(0.2))
        self.assertEqual([], self.tools.force_stopped)
        self.assertIsNone(controller.termination)

    def test_the_worker_is_force_stopped_once_the_deadline_passes(self) -> None:
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.confirm()

        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        report = controller.termination
        self.assertIsNotNone(report)
        self.assertTrue(report.forced)
        self.assertIsNone(report.skipped)
        self.assertEqual(redirect_control.PHASE_ESCALATED, controller.phase)

    def test_the_window_runs_from_arming_not_from_acceptance(self) -> None:
        # A slow journal append eats into the operator's window rather than
        # extending it: the promise was made when the clock started.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        self.clock.advance(1.9)
        controller.confirm()

        self.clock.advance(0.2)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)

    def test_the_abort_signal_is_armed_with_the_remaining_window(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._armed(cancellation=cancellation)
        controller.arm(operation_id="op-1")
        self.clock.advance(0.5)

        controller.confirm()

        self.assertEqual(1, len(cancellation.armed))
        reason, grace = cancellation.armed[0]
        self.assertEqual(redirect_control.REASON_REDIRECT, reason)
        self.assertAlmostEqual(1.5, grace)

    def test_a_second_correction_rides_the_deadline_already_running(self) -> None:
        controller = self._armed()
        first = controller.arm(operation_id="op-1")
        self.clock.advance(1.0)

        second = controller.arm(operation_id="op-2")

        self.assertEqual(first, second)
        self.assertEqual("op-1", controller.operation_id)

    def test_the_kill_does_not_wait_for_the_evidence_sample(self) -> None:
        # The whole point of splitting collection from the kill: a ``ps`` that
        # never answers must not become the escalation's latency.
        gate = threading.Event()
        tools = _Tools(evidence_gate=gate)
        controller = _controller(
            clock=self.clock, tools=tools, evidence_wait_seconds=0.05
        )
        # LIFO: the join is registered first so it runs last, after the
        # release it is waiting on.
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        # Registered last so it runs first: the join above must not wait out
        # the blocked sampler it exists to demonstrate.
        self.addCleanup(gate.set)
        controller.arm(operation_id="op-1")
        controller.confirm()

        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))

        self.assertEqual([4242], tools.force_stopped)
        # Still blocked, so the kill demonstrably overtook it.
        self.assertNotIn("collected", tools.order)
        self.assertLess(tools.order.index("force_stop"), tools.order.index("cleanup"))

    def test_evidence_is_collected_before_the_kill_and_checked_after_it(self) -> None:
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.confirm()

        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))

        self.assertEqual(
            ["collect", "collected", "force_stop", "cleanup", "confirm"],
            self.tools.order,
        )

    def test_the_escalation_records_how_late_it_landed(self) -> None:
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.confirm()
        self.clock.advance(2.3)

        self.assertTrue(controller.wait_escalated(PATIENCE))

        payload = controller.to_payload()["termination"]
        self.assertAlmostEqual(1002.0, payload["deadline"])
        self.assertAlmostEqual(0.3, payload["escalated_after_seconds"], places=3)
        self.assertEqual("op-1", payload["operation_id"])


class ArmBeforeAcceptanceTests(unittest.TestCase):
    """The revocable arming: armed first, binding only once acceptance is durable."""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.tools = _Tools()

    def _armed(self, **kwargs) -> redirect_control.UrgentController:
        controller = _controller(clock=self.clock, tools=self.tools, **kwargs)
        # LIFO: the join is registered first so it runs last, after the
        # release it is waiting on.
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        return controller

    def test_an_abandoned_arming_stops_nothing(self) -> None:
        controller = self._armed()
        controller.arm(operation_id="op-1")

        controller.abandon()
        self.clock.advance(5.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([], self.tools.force_stopped)
        self.assertIsNone(controller.termination)
        self.assertEqual(redirect_control.PHASE_ABANDONED, controller.phase)

    def test_a_confirmed_arming_cannot_be_abandoned(self) -> None:
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.confirm()

        controller.abandon()
        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)

    def test_a_deadline_reached_undecided_kills_nothing(self) -> None:
        # Reached only when the acceptance append is itself stalled: the
        # producer has been promised nothing, so nothing is destroyed.
        controller = self._armed()
        controller.arm(operation_id="op-1")

        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([], self.tools.force_stopped)
        self.assertIsNone(controller.termination)
        self.assertEqual(
            redirect_control.SKIP_UNDECIDED, controller.to_payload()["skipped"]
        )

    def test_an_acceptance_that_arrives_late_still_force_stops(self) -> None:
        # The append finished after the watchdog gave up waiting for it. The
        # journal now says "accepted", so the stop is owed -- late, but owed.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([], self.tools.force_stopped)

        controller.confirm()

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        report = controller.termination
        self.assertIsNotNone(report)
        self.assertTrue(report.forced)
        self.assertIsNone(report.skipped)
        # The window it missed, not a fresh one granted by the delay.
        self.assertAlmostEqual(1002.0, report.deadline)

    def test_the_superseded_watchdog_cannot_report_the_restart_finished(self) -> None:
        # The two race by construction: the watchdog that gave up undecided can
        # still be unwinding when the late acceptance clears the flag, and a
        # stale ``set`` landing after that clear would tell a caller the
        # restarted escalation finished before it began.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        controller.confirm()
        controller._finished.clear()

        controller._mark_finished(0)

        self.assertFalse(controller.wait_escalated(0.05))

    def test_a_turn_that_ended_first_is_not_killed_but_is_still_reported(self) -> None:
        # Its pid may already belong to something else, so nothing is signalled
        # -- and a successor is still owed an answer about what is left.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.confirm()

        controller.release()

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([], self.tools.force_stopped)
        report = controller.termination
        self.assertIsNotNone(report)
        self.assertEqual(redirect_control.SKIP_TURN_ENDED, report.skipped)
        self.assertFalse(report.forced)
        self.assertEqual([4242], self.tools.confirm_calls)

    def test_a_worker_that_honored_the_interrupt_is_not_killed(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._armed(cancellation=cancellation)
        controller.arm(operation_id="op-1")
        controller.confirm()

        cancellation.collect()
        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([], self.tools.force_stopped)
        report = controller.termination
        self.assertEqual(redirect_control.SKIP_TURN_ENDED, report.skipped)
        self.assertTrue(report.collected)

    def test_an_unarmed_turn_released_leaves_no_report(self) -> None:
        controller = self._armed()

        controller.release()

        self.assertIsNone(controller.termination)
        self.assertIsNone(controller.require_successor_allowed())

    def test_an_abandoned_arming_can_be_armed_again_on_its_own_deadline(self) -> None:
        # Nothing was accepted, so there is no promised window to preserve --
        # and the worker is still alive, so the next correction is owed one.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.abandon()
        self.clock.advance(1.5)

        self.assertAlmostEqual(1003.5, controller.arm(operation_id="op-2"))

        self.assertEqual(redirect_control.PHASE_ARMED, controller.phase)
        self.assertEqual("op-2", controller.operation_id)
        self.assertIsNone(controller.to_payload()["skipped"])
        controller.confirm()
        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        self.assertEqual("op-2", controller.termination.operation_id)

    def test_the_stood_down_watchdog_cannot_escalate_the_arming_that_replaced_it(
        self,
    ) -> None:
        # The abandoned watchdog is still inside its poll when the rearming
        # clears the events it waits on, so it wakes into a deadline that is no
        # longer its own. One arming is one escalation, however many watchdogs
        # the turn has outlived.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.abandon()
        controller.arm(operation_id="op-2")
        controller.confirm()

        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        self.assertEqual(redirect_control.PHASE_ESCALATED, controller.phase)

    def test_a_released_turn_is_not_armed_again(self) -> None:
        # An abandoned arming is rearmable because its worker is still alive.
        # Once the turn is over that pid may belong to anything.
        controller = self._armed()
        controller.arm(operation_id="op-1")
        controller.abandon()
        controller.release()

        controller.arm(operation_id="op-2")
        controller.confirm()

        self.clock.advance(5.0)
        self.assertEqual(redirect_control.PHASE_RELEASED, controller.phase)
        self.assertEqual([], self.tools.force_stopped)
        self.assertIsNone(controller.termination)


if __name__ == "__main__":
    unittest.main()
