"""TerminationReportTests, SuccessorGateTests coverage."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from agent_session_loop import (
    process_tree,
    redirect_control,
)
from tests.asl_redirect_control_support import (
    PATIENCE,
    _Clock,
    _controller,
    _FakeCancellation,
    _Tools,
)


class TerminationReportTests(unittest.TestCase):
    @staticmethod
    def _report(**overrides) -> redirect_control.TerminationReport:
        fields = {
            "operation_id": "op-1",
            "worker": redirect_control.OwnedWorker.session_leader(4242),
            "reason": redirect_control.REASON_REDIRECT,
            "deadline": 1002.0,
            "escalated_at": 1002.1,
            "forced": True,
            "collected": True,
            "confirmation": {"complete": True, "incomplete_reason": None},
        }
        fields.update(overrides)
        return redirect_control.TerminationReport(**fields)

    def test_a_confirmed_and_collected_worker_permits_a_successor(self) -> None:
        report = self._report()

        self.assertTrue(report.complete)
        self.assertTrue(report.permits_continuation)
        self.assertIsNone(report.blocking_reason)

    def test_an_uncollected_worker_blocks_even_when_every_probe_is_clean(self) -> None:
        report = self._report(collected=False)

        self.assertFalse(report.permits_continuation)
        self.assertEqual("predecessor-uncollected", report.blocking_reason)

    def test_an_incomplete_confirmation_reports_its_own_reason(self) -> None:
        report = self._report(
            confirmation={
                "complete": False,
                "incomplete_reason": process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            }
        )

        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING, report.blocking_reason
        )

    def test_a_bare_kill_never_permits_continuation(self) -> None:
        report = self._report(collected=False, confirmation={})

        self.assertTrue(report.forced)
        self.assertFalse(report.permits_continuation)

    def test_the_payload_omits_descendant_command_lines(self) -> None:
        report = self._report(
            evidence={
                "root_pid": 4242,
                "root_pgid": 4242,
                "collected": True,
                "entries": [
                    {"pid": 4243, "command": "codex --prompt /secret/plan.md"},
                ],
                "escaped_pids": [4244],
            }
        )

        payload = report.to_payload()

        self.assertNotIn("secret", repr(payload["evidence"]))
        self.assertEqual(1, payload["evidence"]["descendant_count"])
        self.assertEqual([4244], payload["evidence"]["escaped_pids"])


class SuccessorGateTests(unittest.TestCase):
    """What licenses a successor, and everything that does not.

    The report is the licence. Three of these cases have no report at all, and
    they are the ones that matter most: the report is published after
    per-pid cleanup and confirmation, so an escalation that is slow, wedged or
    dead all look alike from here -- like an acceptance with nothing behind it.
    """

    def setUp(self) -> None:
        self.clock = _Clock()

    def _escalated(self, confirmation: dict, *, cancellation=None):
        tools = _Tools(confirmation=confirmation)
        controller = _controller(
            clock=self.clock, tools=tools, cancellation=cancellation
        )
        self.addCleanup(controller.release)
        controller.arm(operation_id="op-1")
        controller.confirm()
        self.clock.advance(2.0)
        self.assertTrue(controller.wait_escalated(PATIENCE))
        return controller, tools

    def test_a_live_descendant_blocks_the_successor(self) -> None:
        cancellation = _FakeCancellation(completed=True)
        controller, _ = self._escalated(
            {
                "complete": False,
                "incomplete_reason": process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            },
            cancellation=cancellation,
        )

        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()

        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING, caught.exception.reason
        )
        self.assertIsNotNone(caught.exception.report)
        self.assertEqual(
            process_tree.INCOMPLETE_DESCENDANTS_RUNNING,
            caught.exception.to_payload()["report"]["blocking_reason"],
        )

    def test_an_uncollected_predecessor_blocks_the_successor(self) -> None:
        controller, _ = self._escalated(
            {"complete": True, "incomplete_reason": None},
            cancellation=_FakeCancellation(),
        )

        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()

        self.assertEqual("predecessor-uncollected", caught.exception.reason)

    def test_the_gate_clears_once_the_owner_reaps_the_worker(self) -> None:
        cancellation = _FakeCancellation()
        controller, tools = self._escalated(
            {"complete": True, "incomplete_reason": None}, cancellation=cancellation
        )
        with self.assertRaises(redirect_control.SuccessorBlockedError):
            controller.require_successor_allowed()

        cancellation.collect()
        report = controller.require_successor_allowed()

        self.assertIsNotNone(report)
        self.assertTrue(report.permits_continuation)
        # Re-checked against the machine as it is now, not trusted from before.
        self.assertEqual(3, len(tools.confirm_calls))

    def test_the_sweep_opt_out_blocks_rather_than_assuming_success(self) -> None:
        controller, _ = self._escalated(
            {
                "complete": False,
                "incomplete_reason": process_tree.INCOMPLETE_SWEEP_OPT_OUT,
            },
            cancellation=_FakeCancellation(completed=True),
        )

        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()

        self.assertEqual(process_tree.INCOMPLETE_SWEEP_OPT_OUT, caught.exception.reason)

    def _cleanup_blocked(self, **tool_kwargs):
        """A confirmed escalation held inside the escaped-descendant cleanup."""
        tools = _Tools(**tool_kwargs)
        cancellation = _FakeCancellation()
        controller = _controller(
            clock=self.clock, tools=tools, cancellation=cancellation
        )
        # LIFO: the join is registered first so it runs after the release.
        self.addCleanup(controller.join, PATIENCE)
        self.addCleanup(controller.release)
        controller.arm(operation_id="op-1")
        controller.confirm()
        self.clock.advance(2.0)
        self.assertTrue(tools.cleanup_entered.wait(PATIENCE), "cleanup never began")
        return controller, tools, cancellation

    def test_an_unfinished_escalation_refuses_the_successor(self) -> None:
        """The kill landed, the cleanup is still running, nothing is proven.

        This is the ordinary shape of the failure rather than an exotic one:
        cleanup and confirmation probe every descendant pid against a real
        process table, so a handful of them outlasting the teardown's bounded
        wait needs nothing to have gone wrong at all.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)
        controller, tools, _ = self._cleanup_blocked(cleanup_gate=gate)

        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()

        self.assertEqual(redirect_control.BLOCK_UNREPORTED, caught.exception.reason)
        self.assertIsNone(caught.exception.report)
        self.assertIsNone(caught.exception.to_payload()["report"])
        self.assertIsNone(controller.termination)
        self.assertEqual([4242], tools.force_stopped)
        self.assertTrue(controller.to_payload()["acceptance_durable"])

    def test_the_successor_is_admitted_once_the_held_cleanup_finishes(self) -> None:
        """The same escalation, allowed to finish: a complete report admits."""
        gate = threading.Event()
        controller, _, cancellation = self._cleanup_blocked(cleanup_gate=gate)
        with self.assertRaises(redirect_control.SuccessorBlockedError):
            controller.require_successor_allowed()

        gate.set()
        cancellation.collect()

        self.assertTrue(controller.wait_escalated(PATIENCE))
        report = controller.require_successor_allowed()
        self.assertIsNotNone(report)
        self.assertTrue(report.permits_continuation)
        self.assertIsNone(report.blocking_reason)

    def test_an_escalation_that_died_refuses_the_successor(self) -> None:
        """A watchdog that raised leaves the same nothing a wedged one does.

        It runs on a daemon thread nobody joins, so the exception reaches only
        the interpreter's excepthook -- silenced here because the traceback is
        the expected outcome, not a failure to read.
        """
        with mock.patch.object(threading, "excepthook"):
            controller, _, _ = self._cleanup_blocked(
                cleanup_error=RuntimeError("the process table went away")
            )

            self.assertTrue(controller.wait_escalated(PATIENCE))
            with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
                controller.require_successor_allowed()

        self.assertEqual(redirect_control.BLOCK_FAILED, caught.exception.reason)
        self.assertIsNone(controller.termination)
        self.assertIn("the process table went away", controller.to_payload()["failure"])

    def test_a_refused_acceptance_leaves_the_successor_gate_alone(self) -> None:
        """Nothing durable was promised, so there is no predecessor to prove.

        The complement of the three cases above, and the reason they are keyed
        on the acceptance rather than on the arming: an arming the store refused
        never interrupted anything, and a turn that ran to its own end owes its
        successor no report.
        """
        tools = _Tools()
        controller = _controller(clock=self.clock, tools=tools)
        self.addCleanup(controller.release)
        controller.arm(operation_id="op-1")
        controller.abandon()
        self.clock.advance(2.0)

        self.assertTrue(controller.wait_escalated(PATIENCE))
        self.assertIsNone(controller.require_successor_allowed())
        self.assertEqual([], tools.force_stopped)
        self.assertIsNone(controller.termination)


if __name__ == "__main__":
    unittest.main()
