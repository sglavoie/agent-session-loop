"""AcceptanceOrderingTests, BlockedTransportTests coverage."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    redirect_control,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)
from tests.asl_redirect_control_support import (
    PATIENCE,
    _Clock,
    _controller,
    _FakeCancellation,
    _Tools,
)


class AcceptanceOrderingTests(unittest.TestCase):
    """``accept`` against a real journal: armed before the record exists."""

    def setUp(self) -> None:
        self.clock = _Clock()
        self.tools = _Tools()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.plan_dir = Path(root.name) / "plan"
        self.plan_dir.mkdir()
        self.state_dir = Path(root.name) / "state"
        self.target = redirect_store_models.RedirectTarget(
            run_id="run-1", session_id="10", turn_index=1, harness="codex"
        )

    def _submit(self, operation_id: str = "op-1") -> None:
        redirect_store_requests.submit_request(
            self.plan_dir,
            target=self.target,
            feedback="stop editing the lock file",
            state_dir=self.state_dir,
            operation_id=operation_id,
        )

    def _controller(self, **kwargs) -> redirect_control.UrgentController:
        controller = _controller(clock=self.clock, tools=self.tools, **kwargs)
        # LIFO: the join is registered first so it runs last, after the
        # release it is waiting on.
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        return controller

    def test_the_clock_is_already_running_when_acceptance_is_recorded(self) -> None:
        self._submit()
        controller = self._controller()
        seen: list[tuple[str, float]] = []
        real_accept = redirect_control.redirect_admission.accept_redirect

        def watching(*args, **kwargs):
            seen.append((controller.phase, controller.deadline))
            return real_accept(*args, **kwargs)

        with mock.patch.object(
            redirect_control.redirect_admission, "accept_redirect", watching
        ):
            request = controller.accept(
                self.plan_dir, "op-1", target=self.target, state_dir=self.state_dir
            )

        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, request.state)
        self.assertEqual([(redirect_control.PHASE_ARMED, 1002.0)], seen)
        self.assertEqual(redirect_control.PHASE_CONFIRMED, controller.phase)

    def test_acceptance_initiates_native_cancellation(self) -> None:
        self._submit()
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation=cancellation)

        controller.accept(
            self.plan_dir, "op-1", target=self.target, state_dir=self.state_dir
        )

        self.assertEqual(1, len(cancellation.armed))

    def test_a_refused_acceptance_stands_the_watchdog_down(self) -> None:
        # Never submitted, so the store refuses: the turn keeps running and
        # nothing counts down against it.
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation=cancellation)

        with self.assertRaises(redirect_store_models.RedirectStoreError):
            controller.accept(
                self.plan_dir,
                "op-missing",
                target=self.target,
                state_dir=self.state_dir,
            )

        self.clock.advance(5.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([], self.tools.force_stopped)
        self.assertEqual([], cancellation.armed)
        self.assertEqual(redirect_control.PHASE_ABANDONED, controller.phase)

    def test_a_refusal_leaves_the_next_correction_a_working_channel(self) -> None:
        # The refusals ``accept`` can still raise -- expiry, and a target that
        # moved -- are facts about the *request*, not about the turn. The turn
        # is still live, so the correction that follows has to be able to stop
        # it; a channel disabled by the first refusal would report the second
        # one accepted with nothing counting down behind the record.
        self._submit("op-2")
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation=cancellation)
        with self.assertRaises(redirect_store_models.RedirectStoreError):
            controller.accept(
                self.plan_dir,
                "op-missing",
                target=self.target,
                state_dir=self.state_dir,
            )
        self.assertEqual(redirect_control.PHASE_ABANDONED, controller.phase)

        request = controller.accept(
            self.plan_dir, "op-2", target=self.target, state_dir=self.state_dir
        )

        self.assertEqual(redirect_store_vocab.STATE_ACCEPTED, request.state)
        self.assertEqual(redirect_control.PHASE_CONFIRMED, controller.phase)
        self.assertEqual(1, len(cancellation.armed))
        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        self.assertEqual("op-2", controller.termination.operation_id)

    def test_a_refusal_after_a_durable_acceptance_changes_nothing(self) -> None:
        # The mirror image, and the bound on the repair: the first correction
        # is in the journal, so the deadline its author was promised may not be
        # stood down, re-based, or re-pointed by a later refusal.
        self._submit("op-1")
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation=cancellation)
        controller.accept(
            self.plan_dir, "op-1", target=self.target, state_dir=self.state_dir
        )
        self.clock.advance(1.0)

        with self.assertRaises(redirect_store_models.RedirectStoreError):
            controller.accept(
                self.plan_dir,
                "op-missing",
                target=self.target,
                state_dir=self.state_dir,
            )

        self.assertEqual(redirect_control.PHASE_CONFIRMED, controller.phase)
        self.assertAlmostEqual(1002.0, controller.deadline)
        self.assertEqual(1, len(cancellation.armed))
        self.clock.advance(1.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        self.assertEqual("op-1", controller.termination.operation_id)


class BlockedTransportTests(unittest.TestCase):
    """The failure the dedicated path exists for: a harness that stopped answering."""

    class _WedgedTransport:
        def __init__(self, gate: threading.Event) -> None:
            self.gate = gate
            self.entered = threading.Event()

        def interrupt(self) -> bool:
            self.entered.set()
            self.gate.wait(PATIENCE)
            return True

    def setUp(self) -> None:
        self.clock = _Clock()
        self.tools = _Tools()
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.transport = self._WedgedTransport(self.gate)

    def _controller(self, **kwargs) -> redirect_control.UrgentController:
        controller = _controller(
            clock=self.clock, tools=self.tools, transport=self.transport, **kwargs
        )
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        # LIFO: unwedging first, so the join is not waiting out the very block
        # these cases exist to reproduce.
        self.addCleanup(self.gate.set)
        return controller

    def test_confirm_returns_while_the_interrupt_is_still_blocked(self) -> None:
        controller = self._controller()
        controller.arm(operation_id="op-1")

        controller.confirm()

        self.assertTrue(self.transport.entered.wait(PATIENCE))
        self.assertFalse(self.gate.is_set())
        self.assertEqual(redirect_control.PHASE_CONFIRMED, controller.phase)

    def test_the_deadline_still_lands_with_the_interrupt_wedged(self) -> None:
        controller = self._controller()
        controller.arm(operation_id="op-1")
        controller.confirm()
        self.assertTrue(self.transport.entered.wait(PATIENCE))

        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)
        self.assertFalse(self.gate.is_set())

    def test_the_controller_stays_readable_while_the_interrupt_is_wedged(self) -> None:
        # No lock is held across the protocol call, so a wedged harness cannot
        # make the controller's own state unreadable to anything else.
        controller = self._controller()
        controller.arm(operation_id="op-1")
        controller.confirm()
        self.assertTrue(self.transport.entered.wait(PATIENCE))

        payload = controller.to_payload()

        self.assertEqual(redirect_control.PHASE_CONFIRMED, payload["phase"])
        self.assertEqual("op-1", payload["operation_id"])

    def test_an_interrupt_that_raises_is_not_a_failure(self) -> None:
        class _Refusing:
            def interrupt(self) -> bool:
                raise RuntimeError("transport closed")

        controller = _controller(
            clock=self.clock, tools=self.tools, transport=_Refusing()
        )
        self.addCleanup(controller.release)
        controller.arm(operation_id="op-1")
        controller.confirm()

        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertEqual([4242], self.tools.force_stopped)


if __name__ == "__main__":
    unittest.main()
