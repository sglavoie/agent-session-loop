"""One turn's correction, and the attempt that carries it.

``redirect_continuation`` joins two halves that session 9 and session 10 built
without either of them knowing what a *run* does afterwards, so these tests are
about the join rather than about either half: which of a settlement and an
acceptance wins when they race, what survives the turn that was interrupted,
and what the next attempt is allowed to do with it.

The claims, in the order the module argues them:

* the latch is one-sided both ways -- an acceptance that landed first denies its
  turn a completion, and a settlement that landed first refuses every later
  correction *without* settling it in the journal, so the request expires on the
  deadline its producer was told about;
* what survives is a handle and never the words: operation ids, digests, the
  conversation and the attempt lineage, with the feedback left behind the
  journal's single reader;
* the disposition is decided from the acceptance rather than from the harness
  result, which is what makes a worker killed mid-turn and one that exited
  cleanly a millisecond later the same outcome -- and what keeps an operator's
  own Ctrl-C ahead of both;
* the branch continues the same session, hands back a plan image with the
  completion claim withdrawn, pauses rather than starting a successor beside a
  predecessor that could not be shown gone, and gives way to a stop or an abort
  queued after the correction;
* a harness switch queued for the next session does not quietly take the
  conversation the correction was written against: it is deferred and recorded;
* and, end to end through the run loop with a fake worker: the corrected
  session is attempted again on the conversation it was corrected on, told
  which operations were interrupted, without the plan advancing, without a
  failure budget being spent, and without a commit;
* and, when that next attempt never launches -- its conversation could not be
  confirmed, so the run pauses between assembling the prompt and spawning a
  worker -- the correction is handed back rather than counted as delivered, and
  the authorized fresh start that follows is the one that says it.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    redirect_continuation,
    redirect_control,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
)

from .asl_redirect_continuation_support import (
    FEEDBACK,
    WORKER_PID,
    FakeController,
    JournalTestCase,
    target,
    termination_report,
)
from .asl_redirect_control_support import _FakeCancellation, _Tools
from .support import (
    wait_until,
)

ASL_TEST_PORTABLE = True


class SerializationTests(JournalTestCase):
    """Completion and acceptance contend for one latch, and only one wins."""

    def test_a_correction_that_beat_the_settlement_denies_the_completion(self) -> None:
        request = self.submit()
        turn_redirect, controller = self.gate()
        turn_redirect.bind_worker(WORKER_PID)

        accepted = turn_redirect.accept(request)

        assert accepted is not None
        self.assertEqual("op-1", accepted.operation_id)
        self.assertEqual(["op-1"], controller.accepted)
        self.assertTrue(turn_redirect.redirected)
        # The settlement still happens -- the turn did end -- but it reports
        # that it did not close a clean turn, which is the completion claim
        # being taken away rather than a second chance to make one.
        self.assertFalse(turn_redirect.settle())
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED, self.journal().request("op-1").state
        )

    def test_a_settled_turn_refuses_a_late_correction_without_settling_it(self) -> None:
        request = self.submit()
        turn_redirect, controller = self.gate()
        turn_redirect.bind_worker(WORKER_PID)

        self.assertTrue(turn_redirect.settle())
        self.assertIsNone(turn_redirect.accept(request))

        self.assertFalse(turn_redirect.redirected)
        self.assertEqual([], controller.accepted)
        payload = turn_redirect.to_payload()
        self.assertEqual(
            [
                {
                    "operation_id": "op-1",
                    "reason": redirect_continuation.REFUSE_TURN_SETTLED,
                }
            ],
            payload["refused"],
        )
        # Refused by this turn, not answered by the run: the request is still
        # pending, so it expires on its producer's deadline and stays visible
        # to the operator who sent it.
        pending = self.journal().pending()
        self.assertEqual(("op-1",), tuple(item.operation_id for item in pending))
        self.assertEqual(redirect_store_vocab.STATE_SUBMITTED, pending[0].state)

    def test_the_settlement_waits_for_an_acceptance_already_appending(self) -> None:
        """The latch is held across the journal append, not around it."""
        request = self.submit()
        controller = FakeController(report=termination_report())
        controller.gate.clear()
        turn_redirect, _ = self.gate(controller=controller)
        turn_redirect.bind_worker(WORKER_PID)
        outcome: list[Any] = []

        accepting = threading.Thread(
            target=lambda: outcome.append(turn_redirect.accept(request))
        )
        accepting.start()
        try:
            self.assertTrue(controller.entered.wait(10.0))
            settled: list[bool] = []
            settling = threading.Thread(
                target=lambda: settled.append(turn_redirect.settle())
            )
            settling.start()
            # The settlement cannot land while the append is in flight: it is
            # still running a second later, and releasing the append is what
            # lets it finish -- reporting, then, that the turn was not clean.
            settling.join(0.2)
            self.assertTrue(settling.is_alive())
            self.assertEqual([], settled)
            controller.gate.set()
            settling.join(10.0)
            self.assertEqual([False], settled)
        finally:
            controller.gate.set()
            accepting.join(10.0)
        self.assertIsNotNone(outcome[0])
        self.assertTrue(turn_redirect.redirected)

    def test_settling_twice_reports_the_first_answer(self) -> None:
        turn_redirect, _ = self.gate()
        turn_redirect.bind_worker(WORKER_PID)

        self.assertTrue(turn_redirect.settle())
        self.assertTrue(turn_redirect.settle())

    def test_a_respawned_worker_rebinds_until_a_correction_arms(self) -> None:
        # Codex's app-server fallback spawns `codex exec` through the same
        # callback once the first process has exited.
        workers: list[int] = []
        controllers: list[FakeController] = []

        def build(worker: Any) -> FakeController:
            workers.append(worker.pid)
            controllers.append(FakeController(report=termination_report()))
            return controllers[-1]

        turn_redirect = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(1),
            state_dir=self.state_dir,
            build_controller=build,
            poll_seconds=0.01,
            bind_wait_seconds=0.05,
            report_wait_seconds=0.5,
        )
        self.addCleanup(turn_redirect.close)

        turn_redirect.bind_worker(WORKER_PID)
        turn_redirect.bind_worker(WORKER_PID + 1)
        self.assertIsNotNone(turn_redirect.accept(self.submit()))
        turn_redirect.bind_worker(WORKER_PID + 2)

        self.assertEqual([WORKER_PID, WORKER_PID + 1], workers)
        self.assertEqual(1, controllers[0].released)
        self.assertEqual([], controllers[0].accepted)
        self.assertEqual(["op-1"], controllers[1].accepted)

    def test_a_correction_with_no_worker_to_stop_is_refused(self) -> None:
        request = self.submit()
        turn_redirect, controller = self.gate()

        self.assertIsNone(turn_redirect.accept(request))

        self.assertEqual([], controller.accepted)
        self.assertEqual(
            [
                {
                    "operation_id": "op-1",
                    "reason": redirect_continuation.REFUSE_WORKER_UNBOUND,
                }
            ],
            turn_redirect.to_payload()["refused"],
        )
        self.assertEqual(
            redirect_store_vocab.STATE_SUBMITTED, self.journal().request("op-1").state
        )

    def test_an_unreadable_channel_is_refused_rather_than_swallowed(self) -> None:
        turn_redirect = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(),
            state_dir=self.state_dir,
            read_pending=mock.Mock(side_effect=OSError("journal is gone")),
            build_controller=lambda _worker: FakeController(),
            poll_seconds=0.01,
            bind_wait_seconds=0.05,
            report_wait_seconds=0.1,
        )
        self.addCleanup(turn_redirect.close)

        turn_redirect._drain()

        self.assertEqual(
            [
                {
                    "operation_id": None,
                    "reason": redirect_continuation.REFUSE_CHANNEL_UNREADABLE,
                }
            ],
            turn_redirect.to_payload()["refused"],
        )
        self.assertFalse(turn_redirect.redirected)

    def test_a_store_refusal_is_recorded_under_the_store_s_own_reason(self) -> None:
        request = self.submit()
        controller = FakeController()
        controller.accept = mock.Mock(  # type: ignore[method-assign]
            side_effect=redirect_store_models.RedirectStoreError(
                redirect_store_vocab.ERROR_REQUEST_EXPIRED, "too late"
            )
        )
        turn_redirect, _ = self.gate(controller=controller)
        turn_redirect.bind_worker(WORKER_PID)

        self.assertIsNone(turn_redirect.accept(request))

        self.assertEqual(
            [
                {
                    "operation_id": "op-1",
                    "reason": redirect_store_vocab.ERROR_REQUEST_EXPIRED,
                }
            ],
            turn_redirect.to_payload()["refused"],
        )

    def test_a_refused_correction_leaves_the_next_one_a_live_channel(self) -> None:
        """The gate keeps one controller per turn, so a refusal may not spend it.

        Driven against the real
        :class:`~agent_session_loop.redirect_control.UrgentController` rather
        than :class:`FakeController`, because this is exactly the seam a
        stand-in cannot hold: a fake would record both ``accept`` calls and
        report the second correction accepted while nothing at all counted down
        behind the journal record.
        """
        expired = redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(),
            feedback=FEEDBACK,
            state_dir=self.state_dir,
            operation_id="op-expired",
            ttl_seconds=1,
            now=clock.utc_now() - timedelta(seconds=2),
        )
        valid = self.submit("op-valid")
        tools = _Tools()
        cancellation = _FakeCancellation()
        controllers: list[redirect_control.UrgentController] = []

        def build(worker):
            controller = redirect_control.UrgentController(
                worker=worker,
                cancellation=cancellation,
                tools=tools.bundle(),
                window_seconds=0.02,
                collection_wait_seconds=0.0,
                environ={},
            )
            controllers.append(controller)
            # LIFO: the join is registered first so it runs after the release.
            self.addCleanup(controller.join, 10.0)
            self.addCleanup(controller.release)
            return controller

        turn_redirect = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(),
            state_dir=self.state_dir,
            build_controller=build,
            poll_seconds=0.01,
            bind_wait_seconds=0.5,
            report_wait_seconds=1.0,
        )
        self.addCleanup(turn_redirect.close)
        turn_redirect.bind_worker(WORKER_PID)

        self.assertIsNone(turn_redirect.accept(expired))
        accepted = turn_redirect.accept(valid)

        self.assertIsNotNone(accepted)
        self.assertEqual("op-valid", accepted.operation_id)
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED,
            self.journal().request("op-valid").state,
        )
        self.assertEqual(1, len(cancellation.armed))
        self.assertTrue(controllers[0].wait_escalated(10.0))
        self.assertEqual([WORKER_PID], tools.force_stopped)
        self.assertEqual(
            redirect_store_vocab.ERROR_REQUEST_EXPIRED,
            turn_redirect.to_payload()["refused"][0]["reason"],
        )


class UrgentWatchTests(JournalTestCase):
    """The watch is woken, and it stops with the turn."""

    def test_a_correction_that_lands_before_the_turn_starts_is_still_found(
        self,
    ) -> None:
        self.submit()
        wakeup = threading.Event()
        turn_redirect, _ = self.gate(wakeup=wakeup, bind_wait_seconds=5.0)
        turn_redirect.bind_worker(WORKER_PID)

        turn_redirect.start()

        # No wakeup is set here on purpose: the first pass drains
        # unconditionally, because a submission can land between the endpoint
        # opening and this turn beginning.
        wait_until(lambda: turn_redirect.redirected, timeout=5.0)
        self.assertEqual(
            redirect_store_vocab.STATE_ACCEPTED, self.journal().request("op-1").state
        )

    def test_a_correction_that_lands_mid_turn_is_answered_on_the_wakeup(self) -> None:
        wakeup = threading.Event()
        turn_redirect, _ = self.gate(wakeup=wakeup, bind_wait_seconds=5.0)
        turn_redirect.bind_worker(WORKER_PID)
        turn_redirect.start()

        self.submit("op-mid")
        wakeup.set()

        wait_until(lambda: turn_redirect.redirected, timeout=5.0)
        self.assertEqual(
            ("op-mid",),
            tuple(record.operation_id for record in turn_redirect.accepted),
        )

    def test_a_watch_with_no_wakeup_watches_nothing(self) -> None:
        # The ordinary state of a run whose redirect endpoint could not be
        # opened: admission refuses such a run outright, so there is nothing
        # for a watch to find and a thread would only be a thread.
        self.submit()
        turn_redirect, _ = self.gate(wakeup=None)
        turn_redirect.bind_worker(WORKER_PID)

        turn_redirect.start()

        self.assertIsNone(turn_redirect._watch)
        self.assertFalse(turn_redirect.redirected)

    def test_closing_releases_the_controller_and_stops_the_watch(self) -> None:
        wakeup = threading.Event()
        turn_redirect, controller = self.gate(wakeup=wakeup)
        turn_redirect.bind_worker(WORKER_PID)
        turn_redirect.start()

        turn_redirect.close()

        self.assertTrue(turn_redirect.settled)
        self.assertIsNone(turn_redirect._watch)
        self.assertEqual(1, controller.released)
        # Nothing was armed, so there is no watchdog that will ever publish a
        # report; waiting for one would spend the whole timeout on every
        # ordinary turn.
        self.assertEqual([], controller.waits)

    def test_a_redirected_turn_waits_for_the_escalation_s_report(self) -> None:
        request = self.submit()
        turn_redirect, controller = self.gate()
        turn_redirect.bind_worker(WORKER_PID)
        turn_redirect.accept(request)

        turn_redirect.close()

        self.assertEqual(1, controller.released)
        self.assertEqual([0.5], controller.waits)


class SuccessorGateTests(JournalTestCase):
    """What the gate lets through once the turn it interrupted is over.

    The wait in ``close`` is bounded and the escalation's cleanup is not: it
    probes descendants against a real process table, per pid, after the kill. So
    "the report is not here" is a state an ordinary redirect can reach, and the
    only safe reading of it is that nothing has been proven about the worker.
    """

    def _blocked_cleanup_gate(
        self, cleanup_gate: threading.Event, *, report_wait_seconds: float = 0.05
    ):
        """A gate over a real controller whose escalation cannot finish."""
        tools = _Tools(cleanup_gate=cleanup_gate)
        cancellation = _FakeCancellation()
        controllers: list[redirect_control.UrgentController] = []

        def build(worker):
            controller = redirect_control.UrgentController(
                worker=worker,
                cancellation=cancellation,
                tools=tools.bundle(),
                window_seconds=0.02,
                collection_wait_seconds=0.0,
                environ={},
            )
            controllers.append(controller)
            # LIFO: the join is registered first so it runs after the release.
            self.addCleanup(controller.join, 10.0)
            self.addCleanup(controller.release)
            return controller

        turn_redirect = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(),
            state_dir=self.state_dir,
            build_controller=build,
            poll_seconds=0.01,
            bind_wait_seconds=0.5,
            # Shorter than the cleanup this test holds open, which is the whole
            # case: the teardown gives up and the turn is over anyway.
            report_wait_seconds=report_wait_seconds,
        )
        self.addCleanup(turn_redirect.close)
        return turn_redirect, tools, cancellation

    def test_an_escalation_that_never_reported_launches_no_successor(self) -> None:
        """The report wait is exhausted mid-cleanup, and nothing is admitted.

        Driven against the real
        :class:`~agent_session_loop.redirect_control.UrgentController`, because
        the fact under test is that a *bounded* wait and an *unbounded* cleanup
        disagree, and a stand-in for either one would decide that by fiat.
        """
        cleanup = threading.Event()
        self.addCleanup(cleanup.set)
        turn_redirect, tools, cancellation = self._blocked_cleanup_gate(cleanup)
        turn_redirect.bind_worker(WORKER_PID)
        accepted = turn_redirect.accept(self.submit())
        self.assertIsNotNone(accepted)
        self.assertTrue(tools.cleanup_entered.wait(10.0), "cleanup never began")
        # The turn's owner has finished with the process; the only thing still
        # missing is the proof that nothing of it is left running.
        cancellation.collect()

        turn_redirect.close()

        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            turn_redirect.require_successor_allowed()
        self.assertEqual(redirect_control.BLOCK_UNREPORTED, caught.exception.reason)
        self.assertIsNone(caught.exception.report)
        self.assertTrue(
            turn_redirect.to_payload()["report_wait_exhausted"],
            "the teardown dropped the report wait it lost",
        )

    def test_a_complete_report_still_admits_the_successor(self) -> None:
        """The same path with the cleanup allowed to finish: continuation."""
        cleanup = threading.Event()
        cleanup.set()
        # This case asserts a completed cleanup, not the short timeout the
        # blocked-cleanup case exercises. Let the watchdog publish its report
        # even when the suite's other shards delay its scheduling.
        turn_redirect, _, cancellation = self._blocked_cleanup_gate(
            cleanup, report_wait_seconds=10.0
        )
        turn_redirect.bind_worker(WORKER_PID)
        self.assertIsNotNone(turn_redirect.accept(self.submit()))
        cancellation.collect()

        turn_redirect.close()

        report = turn_redirect.require_successor_allowed()
        assert report is not None
        self.assertTrue(report.permits_continuation)

    def test_a_controller_that_answers_nothing_cannot_admit_a_redirect(self) -> None:
        """The gate's own record outranks a controller that lost track of it.

        Belt and braces on purpose: the controller refuses this case itself, and
        this asserts the gate does not depend on that. The two know different
        things -- only the latch knows which corrections this turn was
        interrupted for -- so a disagreement between them is not the gate's to
        resolve in favour of running a second worker.
        """
        turn_redirect, controller = self.gate(controller=FakeController(report=None))
        turn_redirect.bind_worker(WORKER_PID)
        self.assertIsNotNone(turn_redirect.accept(self.submit()))

        with self.assertRaises(redirect_control.SuccessorBlockedError) as caught:
            turn_redirect.require_successor_allowed()

        self.assertEqual(redirect_control.BLOCK_UNREPORTED, caught.exception.reason)

    def test_a_turn_nothing_corrected_is_left_to_its_successor(self) -> None:
        """The unredirected turn keeps its no-report answer, which is every turn."""
        turn_redirect, _ = self.gate(controller=FakeController(report=None))
        turn_redirect.bind_worker(WORKER_PID)

        self.assertIsNone(turn_redirect.require_successor_allowed())
        turn_redirect.close()
        self.assertIsNone(turn_redirect.require_successor_allowed())
