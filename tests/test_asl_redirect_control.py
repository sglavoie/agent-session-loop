"""Urgent controller tests: escalation timing, blocked transports, live workers.

Three groups, in the order the design argues for them. Injected clocks prove
*when* the force-stop lands and that nothing on the deadline path waits for a
sample, a protocol call or a journal append. Blocked transports prove the
escalation survives the failure it exists for. Real subprocesses prove the last
claim, which no fake can: that a successor is refused while a predecessor is
still running *or* still unreaped.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    harnesses,
    process_tree,
    redirect_control,
    redirect_store,
)

# Real-process cases wait on other threads; every wait is bounded so a broken
# controller fails the assertion rather than hanging the suite.
PATIENCE = 10.0


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self._lock = threading.Lock()
        self._value = start

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, delta: float) -> None:
        with self._lock:
            self._value += delta


class _FakeCancellation:
    """Structurally :class:`harnesses.AbortSignal`, with the waits observable."""

    def __init__(self, *, completed: bool = False) -> None:
        self.armed: list[tuple[str, float]] = []
        self._done = threading.Event()
        if completed:
            self._done.set()

    def arm(self, reason: str, *, grace_seconds: float = 0.0) -> None:
        self.armed.append((reason, grace_seconds))

    @property
    def completed(self) -> bool:
        return self._done.is_set()

    def wait_completed(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def collect(self) -> None:
        """What a turn's owner calls once the process is reaped."""
        self._done.set()


class _Tools:
    """Recording stand-ins for the four process operations, order preserved."""

    def __init__(
        self,
        *,
        evidence: dict | None = None,
        confirmation: dict | None = None,
        evidence_gate: threading.Event | None = None,
    ) -> None:
        self.order: list[str] = []
        self.force_stopped: list[int] = []
        self.confirm_calls: list[int] = []
        self._lock = threading.Lock()
        self._evidence = (
            evidence
            if evidence is not None
            else {
                "root_pid": 4242,
                "root_pgid": 4242,
                "entries": [],
                "escaped_pids": [],
                "collected": True,
                "disabled": None,
                "sample_error": None,
            }
        )
        self._confirmation = (
            confirmation
            if confirmation is not None
            else {
                "pgid": 4242,
                "group": process_tree.GROUP_STOPPED,
                "remaining_pids": [],
                "pid_reused": [],
                "unverified_pids": [],
                "ownership_unknown": False,
                "complete": True,
                "incomplete_reason": None,
            }
        )
        self._evidence_gate = evidence_gate

    def _note(self, name: str) -> None:
        with self._lock:
            self.order.append(name)

    def collect_evidence(self, root_pid: int, **kwargs) -> dict:
        self._note("collect")
        if self._evidence_gate is not None:
            self._evidence_gate.wait(PATIENCE)
        self._note("collected")
        return dict(self._evidence)

    def force_stop(self, pgid: int, **kwargs) -> dict:
        self._note("force_stop")
        with self._lock:
            self.force_stopped.append(pgid)
        return {
            "pgid": pgid,
            "signal": "SIGKILL",
            "signalled": True,
            "already_gone": False,
            "refused": None,
            "error": None,
        }

    def terminate_escapees(self, evidence, **kwargs) -> dict:
        self._note("cleanup")
        return {
            "policy": "identity_checked_sigkill",
            "candidate_pids": [],
            "killed": [],
        }

    def confirm(self, *, pgid: int, evidence, **kwargs) -> dict:
        self._note("confirm")
        with self._lock:
            self.confirm_calls.append(pgid)
        return dict(self._confirmation)

    def bundle(self) -> redirect_control.TerminationTools:
        return redirect_control.TerminationTools(
            collect_evidence=self.collect_evidence,
            force_stop=self.force_stop,
            terminate_escapees=self.terminate_escapees,
            confirm=self.confirm,
        )


def _controller(
    *,
    clock: _Clock,
    tools: _Tools,
    cancellation=None,
    transport=None,
    window_seconds: float = 2.0,
    **kwargs,
) -> redirect_control.UrgentController:
    controller = redirect_control.UrgentController(
        worker=redirect_control.OwnedWorker.session_leader(4242),
        cancellation=cancellation,
        transport=transport,
        window_seconds=window_seconds,
        poll_seconds=0.005,
        decision_grace_seconds=kwargs.pop("decision_grace_seconds", 0.05),
        evidence_wait_seconds=kwargs.pop("evidence_wait_seconds", 1.0),
        collection_wait_seconds=kwargs.pop("collection_wait_seconds", 0.05),
        monotonic=clock,
        tools=tools.bundle(),
        environ={},
        **kwargs,
    )
    return controller


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
        self.target = redirect_store.RedirectTarget(
            run_id="run-1", session_id="10", turn_index=1, harness="codex"
        )

    def _submit(self, operation_id: str = "op-1") -> None:
        redirect_store.submit_request(
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

        self.assertEqual(redirect_store.STATE_ACCEPTED, request.state)
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

        with self.assertRaises(redirect_store.RedirectStoreError):
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


# A worker that puts a grandchild in its *own* session, which is exactly what a
# harness shell that daemonizes a helper does -- and exactly what a group kill
# aimed at the worker cannot reach.
_WORKER_SOURCE = """
import subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    start_new_session=True,
)
sys.stdout.write("%d\\n" % child.pid)
sys.stdout.flush()
time.sleep(120)
"""


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "needs POSIX process groups")
class LiveWorkerTerminationTests(unittest.TestCase):
    """Real processes: a successor cannot run beside an uncollected predecessor."""

    def setUp(self) -> None:
        self.worker = subprocess.Popen(
            [sys.executable, "-c", _WORKER_SOURCE],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self._reap)
        line = self.worker.stdout.readline().strip()
        self.assertTrue(line.isdigit(), "worker never reported its grandchild")
        self.escapee = int(line)
        self.addCleanup(self._kill_escapee)

    def _kill_escapee(self) -> None:
        with contextlib.suppress(OSError):
            os.kill(self.escapee, signal.SIGKILL)

    def _reap(self) -> None:
        with contextlib.suppress(OSError):
            os.killpg(self.worker.pid, signal.SIGKILL)
        if self.worker.stdout is not None:
            self.worker.stdout.close()
        try:
            self.worker.wait(timeout=PATIENCE)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            self.worker.kill()
            self.worker.wait(timeout=PATIENCE)

    def _controller(self, cancellation) -> redirect_control.UrgentController:
        controller = redirect_control.UrgentController(
            worker=redirect_control.OwnedWorker.session_leader(self.worker.pid),
            cancellation=cancellation,
            # Long enough for a real ``ps`` to finish while the worker lives,
            # short enough to keep the test quick. The assertion on
            # ``collected`` below makes a lost race fail loudly rather than
            # silently weakening the case.
            window_seconds=0.5,
            poll_seconds=0.01,
            evidence_wait_seconds=PATIENCE,
            collection_wait_seconds=0.1,
        )
        self.addCleanup(controller.join, PATIENCE)
        return controller

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _wait_until_clear(self, controller, timeout: float = PATIENCE):
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                return controller.require_successor_allowed()
            except redirect_control.SuccessorBlockedError as exc:
                last = exc
                time.sleep(0.05)
        raise AssertionError(f"successor never cleared: {last}")

    def test_the_worker_group_and_its_escapee_are_both_stopped(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()

        self.assertTrue(controller.wait_escalated(PATIENCE))
        report = controller.termination

        self.assertIsNotNone(report)
        self.assertTrue(report.forced)
        self.assertTrue(report.group_stop["signalled"])
        # The sample has to have happened while the worker was alive, or the
        # escapee below would be invisible to any descendant walk.
        self.assertTrue(report.evidence["collected"], report.evidence)
        self.assertIn(self.escapee, report.evidence["escaped_pids"])
        self.assertIn(self.escapee, report.cleanup["killed"])

        self._reap()
        deadline = time.monotonic() + PATIENCE
        while self._alive(self.escapee) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self._alive(self.escapee))

    def test_a_successor_is_refused_while_the_predecessor_is_unreaped(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()
        self.assertTrue(controller.wait_escalated(PATIENCE))

        # Signalled, dead, and still this process's child: the pid is not free
        # and the transport is not released until the owner waits on it.
        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            controller.require_successor_allowed()

        self.assertIn(
            caught.exception.reason,
            {
                "predecessor-uncollected",
                process_tree.INCOMPLETE_OWNERSHIP_UNKNOWN,
                process_tree.INCOMPLETE_GROUP_NOT_STOPPED,
            },
        )

    def test_the_successor_is_admitted_only_after_the_owner_collects(self) -> None:
        cancellation = _FakeCancellation()
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()
        self.assertTrue(controller.wait_escalated(PATIENCE))
        with self.assertRaises(redirect_control.SuccessorBlockedError):
            controller.require_successor_allowed()

        self._reap()
        cancellation.collect()

        report = self._wait_until_clear(controller)
        self.assertTrue(report.permits_continuation)
        self.assertIsNone(report.blocking_reason)
        self.assertEqual(process_tree.GROUP_STOPPED, report.confirmation["group"])

    def test_an_unreaped_group_leader_is_not_reported_as_stopped(self) -> None:
        # The mechanism the gate rests on, asserted directly: a killed but
        # uncollected leader keeps its process group answering signal 0.
        cancellation = _FakeCancellation(completed=True)
        controller = self._controller(cancellation)
        controller.arm(operation_id="op-live")
        controller.confirm()
        self.assertTrue(controller.wait_escalated(PATIENCE))

        report = controller.termination

        self.assertNotEqual(process_tree.GROUP_STOPPED, report.confirmation["group"])
        self.assertFalse(report.complete)


if __name__ == "__main__":
    unittest.main()
