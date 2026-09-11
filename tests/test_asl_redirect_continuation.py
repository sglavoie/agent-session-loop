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
  failure budget being spent, and without a commit.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    config,
    conversations,
    events,
    handoff_guards,
    harnesses,
    pause_policy,
    plans,
    redirect_admission,
    redirect_continuation,
    redirect_control,
    redirect_store,
    review_guard,
    run_commit,
    run_control,
    run_loop,
    run_selection,
    run_termination,
    state,
    turn_dispositions,
    workspaces,
)

from .support import (
    REPO_ROOT,
    FakeRunControlListener,
    init_git_repo,
    install_fake_harness,
    patched_env,
    read_jsonl,
    wait_until,
    write_plan,
)
from .test_asl_redirect_control import _FakeCancellation, _Tools
from .test_asl_turn_dispositions import FakeRunContext

RUN_ID = "run-redirect-1"
SESSION_ID = "1"
SESSION_LABEL = "Session 1"
FEEDBACK = "the parser you picked cannot see the second column"

# Nothing here signals anything: every controller in this module is built with
# stand-in process tools, so the pid is only an identity the report carries.
WORKER_PID = 424242

# A plan image the caller did not pass, which is not the same as one it passed
# as ``None`` -- an uninspectable plan is a case the branch has to handle.
UNCHANGED = object()


def target(turn_index: int = 1, *, harness: str | None = "codex", run_id: str = RUN_ID):
    return redirect_store.RedirectTarget(
        run_id=run_id,
        session_id=SESSION_ID,
        turn_index=turn_index,
        harness=harness,
    )


def termination_report(
    *, collected: bool = True, complete: bool = True
) -> redirect_control.TerminationReport:
    """A report shaped like the escalation's, with its verdict dialled."""
    return redirect_control.TerminationReport(
        operation_id="op-1",
        worker=redirect_control.OwnedWorker.session_leader(WORKER_PID),
        reason=redirect_control.REASON_REDIRECT,
        deadline=1000.0,
        escalated_at=1002.0,
        forced=True,
        collected=collected,
        confirmation={"complete": complete, "incomplete_reason": None},
    )


class FakeController:
    """Structurally an :class:`redirect_control.UrgentController`.

    Journal-truthful on purpose: ``accept`` delegates to the real admission
    append, because the half of the serialization these tests are about is
    whether the *record* exists, and a fake that only remembered the call in
    memory could not tell an accepted correction from a refused one.
    """

    def __init__(
        self,
        *,
        report: redirect_control.TerminationReport | None = None,
        blocked: Exception | None = None,
    ) -> None:
        self.accepted: list[str] = []
        self.released = 0
        self.waits: list[float | None] = []
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self._report = report
        self._blocked = blocked

    def accept(
        self,
        plan_dir: str | Path,
        operation_id: str,
        *,
        target: redirect_store.RedirectTarget,
        state_dir: str | Path | None = None,
    ) -> redirect_store.RedirectRequest:
        self.entered.set()
        self.gate.wait(10.0)
        self.accepted.append(operation_id)
        return redirect_admission.accept_redirect(
            plan_dir, operation_id, target=target, state_dir=state_dir
        )

    def release(self) -> None:
        self.released += 1

    def wait_escalated(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return True

    def require_successor_allowed(self) -> redirect_control.TerminationReport | None:
        if self._blocked is not None:
            raise self._blocked
        return self._report

    def to_payload(self) -> dict[str, Any]:
        return {"phase": "confirmed", "operation_id": "op-1"}


class JournalTestCase(unittest.TestCase):
    """A plan directory with a redirect journal and nothing else live."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.state_dir = self.tmp_dir / "state"
        self.plan_dir.mkdir(parents=True, exist_ok=True)

    def submit(
        self,
        operation_id: str = "op-1",
        *,
        turn_index: int = 1,
        feedback: str = FEEDBACK,
    ) -> redirect_store.RedirectRequest:
        return redirect_store.submit_request(
            self.plan_dir,
            target=target(turn_index),
            feedback=feedback,
            state_dir=self.state_dir,
            operation_id=operation_id,
        )

    def journal(self) -> redirect_store.RedirectJournalView:
        return redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)

    def gate(
        self,
        *,
        controller: FakeController | None = None,
        wakeup: threading.Event | None = None,
        bind_wait_seconds: float = 0.05,
        turn_index: int = 1,
    ) -> tuple[redirect_continuation.TurnRedirect, FakeController]:
        built = controller or FakeController(report=termination_report())
        turn_redirect = redirect_continuation.TurnRedirect(
            plan_dir=self.plan_dir,
            target=target(turn_index),
            state_dir=self.state_dir,
            wakeup=wakeup,
            build_controller=lambda _worker: built,
            poll_seconds=0.01,
            bind_wait_seconds=bind_wait_seconds,
            report_wait_seconds=0.5,
        )
        self.addCleanup(turn_redirect.close)
        return turn_redirect, built


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
            redirect_store.STATE_ACCEPTED, self.journal().request("op-1").state
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
        self.assertEqual(redirect_store.STATE_SUBMITTED, pending[0].state)

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
            redirect_store.STATE_SUBMITTED, self.journal().request("op-1").state
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
            side_effect=redirect_store.RedirectStoreError(
                redirect_store.ERROR_REQUEST_EXPIRED, "too late"
            )
        )
        turn_redirect, _ = self.gate(controller=controller)
        turn_redirect.bind_worker(WORKER_PID)

        self.assertIsNone(turn_redirect.accept(request))

        self.assertEqual(
            [
                {
                    "operation_id": "op-1",
                    "reason": redirect_store.ERROR_REQUEST_EXPIRED,
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
        expired = redirect_store.submit_request(
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
            redirect_store.STATE_ACCEPTED, self.journal().request("op-valid").state
        )
        self.assertEqual(1, len(cancellation.armed))
        self.assertTrue(controllers[0].wait_escalated(10.0))
        self.assertEqual([WORKER_PID], tools.force_stopped)
        self.assertEqual(
            redirect_store.ERROR_REQUEST_EXPIRED,
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
            redirect_store.STATE_ACCEPTED, self.journal().request("op-1").state
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

    def _blocked_cleanup_gate(self, cleanup_gate: threading.Event):
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
            report_wait_seconds=0.05,
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
        turn_redirect, _, cancellation = self._blocked_cleanup_gate(cleanup)
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


class PendingRedirectTests(unittest.TestCase):
    """What survives the turn, and what deliberately does not."""

    def accepted(
        self, operation_id: str = "op-1", *, feedback: str = FEEDBACK
    ) -> redirect_continuation.AcceptedRedirect:
        return redirect_continuation.AcceptedRedirect(
            operation_id=operation_id,
            target=target(),
            accepted_at=clock.utc_iso_now(),
            feedback_digest=redirect_store.feedback_digest(feedback),
            feedback_bytes=len(feedback.encode("utf-8")),
        )

    def test_a_turn_that_accepted_nothing_carries_the_previous_correction(self) -> None:
        previous = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted(),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        unchanged = redirect_continuation.PendingRedirect.after_turn(
            (),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=2,
            previous=previous,
        )

        self.assertIs(previous, unchanged)
        self.assertIsNone(
            redirect_continuation.PendingRedirect.after_turn(
                (),
                session_id=SESSION_ID,
                harness="codex",
                conversation_id=None,
                turn_index=1,
            )
        )

    def test_a_second_correction_extends_the_lineage_rather_than_restarting_it(
        self,
    ) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-1"),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        second = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-2", feedback="and rerun the check"),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=2,
            previous=first,
        )

        assert first is not None and second is not None
        self.assertEqual(1, first.attempt)
        self.assertEqual(2, second.attempt)
        self.assertEqual(("op-1", "op-2"), second.operation_ids)
        self.assertEqual(2, len(second.feedback_digests))
        self.assertEqual(
            first.feedback_bytes + len(b"and rerun the check"),
            second.feedback_bytes,
        )
        self.assertEqual(2, second.predecessor_turn_index)

    def test_a_correction_from_another_session_starts_its_own_lineage(self) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-1"),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        other = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-2"),),
            session_id="2",
            harness="claude",
            conversation_id="conv-2",
            turn_index=2,
            previous=first,
        )

        assert other is not None
        self.assertEqual(1, other.attempt)
        self.assertEqual(("op-2",), other.operation_ids)

    def test_one_correction_is_folded_once(self) -> None:
        folded = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted("op-1"), self.accepted("op-1")),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        assert folded is not None
        self.assertEqual(("op-1",), folded.operation_ids)
        self.assertEqual(len(FEEDBACK.encode()), folded.feedback_bytes)

    def test_the_words_never_leave_the_journal(self) -> None:
        pending = redirect_continuation.PendingRedirect.after_turn(
            (self.accepted(),),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        assert pending is not None
        serialized = json.dumps(pending.to_payload())
        self.assertNotIn(FEEDBACK, serialized)
        self.assertIn(redirect_store.feedback_digest(FEEDBACK), serialized)
        self.assertEqual(len(FEEDBACK.encode()), pending.to_payload()["feedback_bytes"])

    def test_an_id_the_conversation_ledger_would_refuse_is_dropped_not_raised(
        self,
    ) -> None:
        # The journal's id rule is broader than the ledger's, and a correction
        # whose identifier the ledger cannot store is still a correction: the
        # worker reads its words out of the journal either way.
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=("op-1", "not a valid id", "op-2"),
            feedback_digests=("a", "b", "c"),
            feedback_bytes=3,
        )

        self.assertEqual(("op-1", "op-2"), pending.resumable_operation_ids())

    def test_the_ledger_s_own_bound_is_honoured(self) -> None:
        ids = tuple(
            f"op-{index}"
            for index in range(conversations.MAX_INTERRUPTED_OPERATIONS + 5)
        )
        pending = redirect_continuation.PendingRedirect(
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=ids,
            feedback_digests=ids,
            feedback_bytes=0,
        )

        resumable = pending.resumable_operation_ids()

        self.assertEqual(conversations.MAX_INTERRUPTED_OPERATIONS, len(resumable))
        # Accepted by the ledger, which is the only reason to narrow at all.
        conversations.resume_intent(
            conversations.ConversationReference(
                binding=conversations.ConversationBinding(
                    plan_dir="/plan",
                    session_id=SESSION_ID,
                    harness="codex",
                    workspace=workspaces.WorkspaceBinding(
                        workspace_root="/plan", registry_digest="0" * 64
                    ),
                    access_profile="worker",
                ),
                native_id="conv-1",
            ),
            interrupted_operations=resumable,
        )


class DispositionTests(unittest.TestCase):
    """The disposition is read off the acceptance, not off the exit."""

    def payload(self, **overrides: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "target": target().to_payload(),
            "accepted": [{"operation_id": "op-1"}],
            "refused": [],
            "settled": True,
            "control": None,
        }
        record.update(overrides)
        return {redirect_continuation.TURN_PAYLOAD_KEY: record}

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        values: dict[str, Any] = {
            "argv": ["codex"],
            "returncode": 137,
            "stdout": "",
            "stderr": "",
            "aborted": True,
        }
        values.update(overrides)
        return harnesses.HarnessResult(**values)

    def test_an_acceptance_makes_the_turn_redirected(self) -> None:
        self.assertTrue(redirect_continuation.turn_was_redirected(self.payload()))

    def test_a_turn_with_no_gate_record_is_not_redirected(self) -> None:
        self.assertFalse(redirect_continuation.turn_was_redirected({}))
        self.assertFalse(
            redirect_continuation.turn_was_redirected({"redirect": "nonsense"})
        )

    def test_a_refusal_alone_is_not_a_redirect(self) -> None:
        payload = self.payload(
            accepted=[],
            refused=[{"operation_id": "op-1", "reason": "turn-already-settled"}],
        )

        self.assertFalse(redirect_continuation.turn_was_redirected(payload))

    def disposition(self, result: harnesses.HarnessResult, **overrides: Any):
        return handoff_guards._terminating_result_disposition(
            result, None, self.payload(**overrides)
        )

    def test_a_redirect_outranks_the_abort_it_shares_a_signal_with(self) -> None:
        # A redirect arms the same abort signal an operator stop does, so the
        # result cannot tell them apart and the record has to.
        self.assertEqual(
            redirect_continuation.DISPOSITION_REDIRECTED,
            self.disposition(self.result()),
        )

    def test_a_redirect_outranks_a_timeout(self) -> None:
        self.assertEqual(
            redirect_continuation.DISPOSITION_REDIRECTED,
            self.disposition(
                self.result(returncode=124, aborted=False, timed_out=True)
            ),
        )

    def test_a_worker_that_finished_inside_the_window_is_still_redirected(self) -> None:
        self.assertEqual(
            redirect_continuation.DISPOSITION_REDIRECTED,
            self.disposition(self.result(returncode=0, aborted=False)),
        )

    def test_the_operator_s_own_interrupt_outranks_a_redirect(self) -> None:
        self.assertEqual("interrupted", self.disposition(self.result(interrupted=True)))

    def test_a_refused_correction_leaves_the_turn_to_its_own_result(self) -> None:
        self.assertEqual(
            "aborted",
            self.disposition(
                self.result(),
                accepted=[],
                refused=[{"operation_id": "op-1", "reason": "turn-already-settled"}],
            ),
        )

    def test_the_successor_block_is_not_an_answerable_pause(self) -> None:
        reason = events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED
        self.assertIn(reason, events.PAUSE_REASONS)
        self.assertIn(reason, pause_policy.UNANSWERABLE_OUTCOME_PAUSE_REASONS)
        policy = pause_policy.classify_pause(reason)
        # Non-recoverable is what the automatic paused-work commit is gated on,
        # so this pause cannot produce a commit of its own, and there is no
        # answer an operator could send that would clear it.
        self.assertFalse(policy["recoverable"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, policy["user_action"])


class SettleRedirectedTurnTests(JournalTestCase):
    """The branch that turns one correction into the next attempt."""

    def setUp(self) -> None:
        super().setUp()
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans.inspect_plan(self.plan_dir)
        self.after = plans.inspect_plan(self.plan_dir)
        self.turn_payload: dict[str, Any] = {"turn_index": 1}
        self.session_details: dict[str, Any] = {"session_id": SESSION_ID}
        self.listener = mock.Mock()
        self.listener.state.abort_turn_requested = False
        self.listener.state.stop_after_current = False

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "result": self.artifact_dir / "result.json",
        }

    def redirected_gate(
        self, **controller_kwargs: Any
    ) -> redirect_continuation.TurnRedirect:
        """A gate that has already accepted one correction."""
        request = self.submit()
        controller_kwargs.setdefault("report", termination_report())
        turn_redirect, _ = self.gate(controller=FakeController(**controller_kwargs))
        turn_redirect.bind_worker(WORKER_PID)
        turn_redirect.accept(request)
        return turn_redirect

    def settle(
        self,
        *,
        after: plans.PlanSnapshot | None | object = UNCHANGED,
        redirect: redirect_continuation.TurnRedirect | None = None,
        pending: redirect_continuation.PendingRedirect | None = None,
        returncode: int = 137,
    ) -> redirect_continuation.PendingRedirect | Any:
        return turn_dispositions.settle_redirected_turn(
            self.ctx,
            self.listener,
            before=self.before,
            after=self.after if after is UNCHANGED else after,
            redirect=redirect if redirect is not None else self.redirected_gate(),
            harness="codex",
            conversation_id="conv-1",
            session_id=SESSION_ID,
            session_label=SESSION_LABEL,
            turn_index=1,
            result=harnesses.HarnessResult(
                argv=["codex"],
                returncode=returncode,
                stdout="",
                stderr="",
                aborted=returncode == 137,
            ),
            turn_payload=self.turn_payload,
            paths=self.paths(),
            session_details=self.session_details,
            pending=pending,
        )

    def continuations(self) -> list[dict[str, Any]]:
        return [
            payload
            for name, payload in self.ctx.persistence.events
            if name == events.REDIRECT_CONTINUATION_EVENT
        ]

    def claimed_complete(self) -> plans.PlanSnapshot:
        write_plan(self.plan_dir, ("complete",))
        return plans.inspect_plan(self.plan_dir)

    def plan_status(self) -> str:
        """What the session file says now, rather than what a snapshot said."""
        return plans.inspect_plan(self.plan_dir).sessions[0].status

    def test_the_correction_becomes_the_next_attempt_of_the_same_session(self) -> None:
        outcome = self.settle()

        assert outcome is not None
        self.assertEqual(SESSION_ID, outcome.pending.session_id)
        self.assertEqual("codex", outcome.pending.harness)
        self.assertEqual("conv-1", outcome.pending.conversation_id)
        self.assertEqual(("op-1",), outcome.pending.operation_ids)
        self.assertEqual(1, outcome.pending.attempt)
        self.assertIs(self.after, outcome.snapshot)
        self.assertEqual([], self.ctx.pauses)

    def test_the_continuation_is_published_before_anything_acts_on_it(self) -> None:
        self.settle()

        records = self.continuations()
        self.assertEqual(
            [turn_dispositions.REDIRECT_OUTCOME_CONTINUED],
            [record["outcome"] for record in records],
        )
        self.assertEqual(("op-1",), tuple(records[0]["redirect"]["operation_ids"]))
        self.assertNotIn(FEEDBACK, json.dumps(records[0]))
        # The turn's own record carries the same claim, and the run state was
        # written under the same hold.
        self.assertEqual(
            turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
            self.turn_payload["redirect_continuation"]["outcome"],
        )
        written = json.loads(self.paths()["result"].read_text(encoding="utf-8"))
        self.assertEqual(
            turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
            written["redirect_continuation"]["outcome"],
        )
        self.assertEqual(1, len(self.ctx.persistence.states))

    def test_a_completion_the_turn_never_settled_is_taken_back(self) -> None:
        outcome = self.settle(after=self.claimed_complete(), returncode=0)

        assert outcome is not None
        self.assertEqual("pending", self.plan_status())
        record = self.turn_payload["completion_reconciliation"]
        self.assertEqual(turn_dispositions.REDIRECT_COMPLETION_REASON, record["reason"])
        self.assertIs(True, record["reverted"])
        # The plan image handed back is the reconciled one, so the next attempt
        # runs against the session as it now is rather than as it was claimed.
        assert outcome.snapshot is not None
        self.assertEqual("pending", outcome.snapshot.sessions[0].status)
        self.assertEqual(record, self.continuations()[0]["completion_reconciliation"])

    def test_a_session_that_claimed_nothing_is_left_alone(self) -> None:
        self.settle()

        self.assertNotIn("completion_reconciliation", self.turn_payload)
        self.assertEqual(
            [],
            [
                name
                for name, _ in self.ctx.persistence.events
                if name == events.SESSION_REOPENED_EVENT
            ],
        )

    def test_an_uninspectable_plan_still_carries_the_correction(self) -> None:
        outcome = self.settle(after=None)

        assert outcome is not None
        self.assertIsNone(outcome.snapshot)
        self.assertEqual(("op-1",), outcome.pending.operation_ids)
        self.assertNotIn("completion_reconciliation", self.turn_payload)

    def test_a_predecessor_that_could_not_be_shown_gone_pauses_the_run(self) -> None:
        blocked = redirect_control.SuccessorBlockedError(
            "predecessor-uncollected", termination_report(collected=False)
        )

        outcome = self.settle(redirect=self.redirected_gate(blocked=blocked))

        self.assertIsNone(outcome)
        pause = self.ctx.pauses[0]
        self.assertEqual(
            events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED, pause["reason"]
        )
        self.assertIn("predecessor-uncollected", pause["message"])
        self.assertEqual(
            "predecessor-uncollected",
            pause["details"]["redirect_continuation"]["reason"],
        )
        self.assertEqual([self.after], self.ctx.final_snapshots)
        self.assertEqual(
            [turn_dispositions.REDIRECT_OUTCOME_BLOCKED],
            [record["outcome"] for record in self.continuations()],
        )

    def test_a_block_is_decided_before_the_completion_is_touched(self) -> None:
        blocked = redirect_control.SuccessorBlockedError(
            "termination-unconfirmed", termination_report(complete=False)
        )

        self.settle(
            after=self.claimed_complete(),
            redirect=self.redirected_gate(blocked=blocked),
            returncode=0,
        )

        # Nothing may run beside a live predecessor, including the reverter.
        self.assertNotIn("completion_reconciliation", self.turn_payload)
        self.assertEqual("complete", self.plan_status())

    def test_an_abort_queued_after_the_correction_outranks_it(self) -> None:
        self.listener.state.abort_turn_requested = True

        with mock.patch.object(run_termination, "_honor_abort_turn") as honor:
            outcome = self.settle()

        self.assertIsNone(outcome)
        honor.assert_called_once()
        self.assertIs(self.listener, honor.call_args.args[0])
        self.assertIs(self.after, honor.call_args.kwargs["snapshot"])
        self.assertEqual([], self.ctx.pauses)
        record = self.continuations()[0]
        self.assertEqual(turn_dispositions.REDIRECT_OUTCOME_STOPPED, record["outcome"])
        self.assertEqual("abort-turn", record["stopped_by"])
        self.assertTrue(record["termination"]["permits_continuation"])

    def test_a_stop_queued_after_the_correction_outranks_it(self) -> None:
        self.listener.state.stop_after_current = True

        with mock.patch.object(run_termination, "_honor_stop_after_current") as honor:
            outcome = self.settle()

        self.assertIsNone(outcome)
        honor.assert_called_once()
        record = self.continuations()[0]
        self.assertEqual(turn_dispositions.REDIRECT_OUTCOME_STOPPED, record["outcome"])
        self.assertEqual("stop-after-current", record["stopped_by"])

    def test_a_stopped_run_still_takes_back_the_completion_claim(self) -> None:
        self.listener.state.stop_after_current = True

        with mock.patch.object(run_termination, "_honor_stop_after_current") as honor:
            self.settle(after=self.claimed_complete(), returncode=0)

        self.assertEqual("pending", self.plan_status())
        self.assertEqual(
            "pending", honor.call_args.kwargs["snapshot"].sessions[0].status
        )

    def test_a_second_correction_rides_the_lineage_of_the_first(self) -> None:
        first = redirect_continuation.PendingRedirect.after_turn(
            (
                redirect_continuation.AcceptedRedirect(
                    operation_id="op-0",
                    target=target(),
                    accepted_at=clock.utc_iso_now(),
                    feedback_digest="digest-0",
                    feedback_bytes=4,
                ),
            ),
            session_id=SESSION_ID,
            harness="codex",
            conversation_id="conv-1",
            turn_index=1,
        )

        outcome = self.settle(pending=first)

        assert outcome is not None
        self.assertEqual(("op-0", "op-1"), outcome.pending.operation_ids)
        self.assertEqual(2, outcome.pending.attempt)


class RedirectedHarnessTests(unittest.TestCase):
    """A queued harness switch is about the next session, not this attempt."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = FakeRunContext(event_path=Path(self._tmp.name) / "events.jsonl")

    def pending(
        self, *, harness: str | None = "codex", session_id: str = SESSION_ID
    ) -> redirect_continuation.PendingRedirect:
        return redirect_continuation.PendingRedirect(
            session_id=session_id,
            harness=harness,
            conversation_id="conv-1",
            predecessor_turn_index=1,
            attempt=1,
            operation_ids=("op-1",),
            feedback_digests=("digest",),
            feedback_bytes=4,
        )

    def choose(
        self,
        pending: redirect_continuation.PendingRedirect | None,
        *,
        selected: str = "claude",
        session_id: str = SESSION_ID,
    ) -> str:
        return run_loop._redirected_turn_harness(
            self.ctx,
            pending=pending,
            selected=selected,
            session_id=session_id,
            turn_index=2,
        )

    def deferrals(self) -> list[dict[str, Any]]:
        return [
            payload
            for name, payload in self.ctx.persistence.events
            if name == events.REDIRECT_CONTINUATION_EVENT
        ]

    def test_the_corrected_conversation_keeps_its_harness(self) -> None:
        self.assertEqual("codex", self.choose(self.pending()))

        record = self.deferrals()[0]
        self.assertEqual("harness-deferred", record["outcome"])
        self.assertEqual("claude", record["deferred_harness"])
        self.assertEqual("codex", record["redirect"]["harness"])

    def test_a_turn_carrying_no_correction_takes_the_selection(self) -> None:
        self.assertEqual("claude", self.choose(None))
        self.assertEqual([], self.deferrals())

    def test_a_correction_from_another_session_does_not_pin_this_one(self) -> None:
        self.assertEqual("claude", self.choose(self.pending(session_id="2")))
        self.assertEqual([], self.deferrals())

    def test_a_selection_that_changes_nothing_is_not_a_deferral(self) -> None:
        self.assertEqual("codex", self.choose(self.pending(), selected="codex"))
        self.assertEqual("claude", self.choose(self.pending(harness=None)))
        self.assertEqual([], self.deferrals())


class RedirectedRunTests(unittest.TestCase):
    """A fake worker, corrected mid-turn, resuming with what it was told.

    The one case that cannot be made from parts: the loop has to reach the
    disposition, carry the correction across the iteration, and not mistake any
    of it for a failure. Everything real is real here -- the plan, the journal,
    the run state, the conversation ledger and a git repository with commits
    enabled -- and only the worker and the four process operations are fakes.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.repo_root = self.tmp / "repo"
        self.config_home = self.tmp / "config"
        init_git_repo(self.repo_root)
        harness_config = {
            name: {"executable": str(install_fake_harness(self.tmp, name))}
            for name in ("codex", "claude")
        }
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(harness_config), encoding="utf-8")

        self.wakeup = threading.Event()
        self.harness_calls: list[str] = []
        self.commit_calls: list[dict[str, Any]] = []
        self.commits_before_resume: int | None = None
        self.status_at_resume: str | None = None
        self.submitted: redirect_store.RedirectRequest | None = None

    # -- the fakes -----------------------------------------------------------

    def _session_status(self) -> str:
        return plans.read_session_status(
            plans.SessionFile(
                plans.parse_session_id(SESSION_ID),
                self.plan_dir / plans.session_id_to_filename(SESSION_ID),
            )
        ).status

    def _complete_session(self) -> None:
        path = self.plan_dir / plans.session_id_to_filename(SESSION_ID)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace("- **Status**: pending", "- **Status**: complete", 1),
            encoding="utf-8",
        )

    def _controller_factory(self):
        """The real controller with stand-in process tools.

        Nothing may signal a real process group from a test, and the escalation
        is not what this case is about: what matters is that the report the
        controller publishes is the one the successor gate reads.
        """
        real = redirect_control.UrgentController
        worker = {
            "root_pid": WORKER_PID,
            "root_pgid": WORKER_PID,
            "entries": [],
            "escaped_pids": [],
            "collected": True,
            "disabled": None,
            "sample_error": None,
        }
        tools = redirect_control.TerminationTools(
            collect_evidence=lambda root_pid, **_kwargs: dict(worker),
            force_stop=lambda pgid, **_kwargs: {
                "pgid": pgid,
                "signal": "SIGKILL",
                "signalled": True,
                "already_gone": False,
                "refused": None,
                "error": None,
            },
            terminate_escapees=lambda evidence, **_kwargs: {
                "policy": "identity_checked_sigkill",
                "candidate_pids": [],
                "killed": [],
            },
            confirm=lambda *, pgid, evidence, **_kwargs: {
                "pgid": pgid,
                "group": "stopped",
                "remaining_pids": [],
                "pid_reused": [],
                "unverified_pids": [],
                "ownership_unknown": False,
                "complete": True,
                "incomplete_reason": None,
            },
        )

        def build(**kwargs: Any) -> redirect_control.UrgentController:
            kwargs.setdefault("tools", tools)
            kwargs.setdefault("window_seconds", 0.05)
            kwargs.setdefault("evidence_wait_seconds", 2.0)
            kwargs.setdefault("collection_wait_seconds", 5.0)
            return real(**kwargs)

        return build

    def _fake_worker(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        """A worker that is corrected on its first turn and finishes on its next."""
        self.harness_calls.append(name)
        abort = kwargs["abort"]
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(WORKER_PID)
        on_conversation = kwargs.get("on_conversation")
        if on_conversation is not None:
            on_conversation("conv-1")
        if len(self.harness_calls) == 1:
            # What the urgent endpoint's reader thread does when a producer's
            # correction lands: append it, then wake the turn. Admission itself
            # is exercised by its own module's tests and would refuse this run
            # for having no endpoint served in-process.
            self.submitted = redirect_store.submit_request(
                self.plan_dir,
                target=redirect_store.RedirectTarget(
                    run_id=RUN_ID,
                    session_id=SESSION_ID,
                    turn_index=1,
                    harness=name,
                ),
                feedback=FEEDBACK,
                state_dir=self.state_dir,
                operation_id="op-1",
            )
            self.wakeup.set()
            wait_until(
                lambda: (
                    redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)
                    .request("op-1")
                    .accepted
                ),
                timeout=10.0,
            )
            # What the real turn's owner reports once the process is reaped.
            abort.release()
            return harnesses.HarnessResult(
                name=name,
                argv=[name],
                returncode=137,
                stdout="",
                stderr="",
                aborted=True,
                conversation_id="conv-1",
            )
        self.commits_before_resume = len(self.commit_calls)
        self.status_at_resume = self._session_status()
        (self.repo_root / "worked.txt").write_text("done\n", encoding="utf-8")
        self._complete_session()
        abort.release()
        return harnesses.HarnessResult(
            name=name,
            argv=[name],
            returncode=0,
            stdout="codex completed\n",
            stderr="",
            conversation_id="conv-1",
        )

    def _fake_commit(self, **kwargs: Any) -> dict[str, Any]:
        """Commit for real, without shelling back out to ``asl commit``."""
        self.commit_calls.append(dict(kwargs))
        root = Path(str(kwargs["cwd"]))
        review_guard._run_git(root, ["add", "-A"])
        review_guard._run_git(
            root, ["commit", "--no-verify", "-m", f"session {kwargs['session_id']}"]
        )
        head = review_guard._run_git(root, ["rev-parse", "HEAD"]).stdout.strip()
        return {
            "commit_index": kwargs["commit_index"],
            "group_id": kwargs["group_id"],
            "repository_key": kwargs["repository_key"],
            "repository_alias": kwargs["repository_alias"],
            "repository_path": kwargs["repository_path"],
            "session_id": kwargs["session_id"],
            "success": True,
            "status_result": "success",
            "returncode": 0,
            "head_after": head,
            "clean_after": True,
            "commit_sha": head,
            "duration_seconds": 0.0,
        }

    # -- the run -------------------------------------------------------------

    def run_loop_once(self) -> dict[str, Any]:
        write_plan(self.plan_dir, ("pending",))
        listener = FakeRunControlListener(current_harness="codex")
        real_state_dir = state.plan_state_dir(self.plan_dir, self.state_dir)
        listener.state_path = real_state_dir / events.STATE_FILE_NAME
        listener.events_path = real_state_dir / events.EVENTS_FILE_NAME

        def fake_for_run(*_args: object, **kwargs: object):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            review_harness=None,
            state_dir=str(self.state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
            harness_model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=self._fake_worker),
            mock.patch.object(run_commit, "_run_asl_commit", self._fake_commit),
            mock.patch.object(
                redirect_control, "UrgentController", self._controller_factory()
            ),
            mock.patch.object(
                run_control.RunControlListener, "for_run", side_effect=fake_for_run
            ),
        ):
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                    lock_info=lock_info,
                    run_id=RUN_ID,
                    redirect_wakeup=self.wakeup,
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        self.assertEqual(0, exit_code, payload)
        return payload

    def events_named(self, name: str) -> list[dict[str, Any]]:
        path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"] for record in read_jsonl(path) if record["name"] == name
        ]

    # -- the claims ----------------------------------------------------------

    def test_the_corrected_session_is_attempted_again_with_its_correction(
        self,
    ) -> None:
        payload = self.run_loop_once()

        self.assertEqual(["codex", "codex"], self.harness_calls)
        continuations = self.events_named(events.REDIRECT_CONTINUATION_EVENT)
        # `carried` falls between the two because binding a correction to the
        # attempt that will deliver it happens while that attempt's prompt is
        # being assembled — after the predecessor is settled, before the
        # resumed turn is planned.
        self.assertEqual(
            [
                turn_dispositions.REDIRECT_OUTCOME_CONTINUED,
                "carried",
                "resumed",
            ],
            [record["outcome"] for record in continuations],
        )
        carried = continuations[1]
        self.assertEqual(
            ["op-1"], [item["operation_id"] for item in carried["carried"]]
        )
        self.assertEqual(f"{RUN_ID}/{SESSION_ID}/2", carried["attempt"])
        resumed = continuations[2]
        self.assertEqual(SESSION_ID, resumed["session_id"])
        self.assertEqual(2, resumed["turn_index"])
        self.assertEqual(["op-1"], resumed["interrupted_operations"])
        self.assertEqual("codex", resumed["harness"])
        self.assertEqual(1, resumed["redirect"]["attempt"])
        self.assertNotIn(FEEDBACK, json.dumps(continuations))
        self.assertFalse(payload["paused"])
        self.assertEqual([1], payload["completed_sessions"])

    def test_the_resumed_attempt_continues_the_conversation_it_corrected(self) -> None:
        self.run_loop_once()

        continuation = self.events_named(events.CONVERSATION_CONTINUATION_EVENT)[1]
        self.assertEqual("resumed", continuation["disposition"])
        self.assertEqual("conv-1", continuation["conversation"])
        self.assertEqual("codex", continuation["harness"])
        self.assertEqual(["op-1"], continuation["intent"]["interrupted_operations"])

    def test_the_redirected_turn_advances_nothing(self) -> None:
        payload = self.run_loop_once()

        # The second turn started with the session exactly as the first one
        # left it: the correction bought another attempt, not progress.
        self.assertEqual("pending", self.status_at_resume)
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        self.assertEqual(
            [SESSION_ID, SESSION_ID], [str(turn["session"]) for turn in turns]
        )
        self.assertTrue(
            redirect_continuation.turn_was_redirected(turns[0]),
            turns[0].get("redirect"),
        )
        self.assertFalse(redirect_continuation.turn_was_redirected(turns[1]))

    def test_the_redirected_turn_spends_no_failure_budget(self) -> None:
        self.run_loop_once()

        # A redirect is not a failed turn, so none of the recoveries a failed
        # turn earns may be charged for it.
        for name in (
            events.TURN_HANDOFF_CORRECTION_EVENT,
            events.SESSION_REOPENED_EVENT,
            events.TURN_REJECTED_EVENT,
            "spark_fallback",
        ):
            with self.subTest(event=name):
                self.assertEqual([], self.events_named(name))

    def test_the_redirected_turn_creates_no_commit(self) -> None:
        self.run_loop_once()

        # Commits are enabled for this run, so the absence is the branch's and
        # not the mode's: the one commit belongs to the session that completed.
        self.assertEqual(0, self.commits_before_resume)
        self.assertEqual(
            [SESSION_ID], [call["session_id"] for call in self.commit_calls]
        )


if __name__ == "__main__":
    unittest.main()
