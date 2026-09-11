"""What happens to a correction when the process holding it stops existing.

Every earlier redirect test drives a live runner. These drive the five instants
a crash can land on, by building the journal a crash would have left and then
asking what the next run does with it. Fault injection here means writing the
exact prefix of records the dead process got through -- nothing is mocked into
failing, because the thing under test is a file, and the file after a crash is
just a shorter file.

The claims, in the order the module argues them:

* status is reconstructed from durable evidence and from nothing else: the same
  journal line reads as ``submitted`` while a run can still answer it and
  ``unanswered`` once none can, and neither reading writes anything;
* reading is inert. ``inspect_plan_recovery`` settles nothing, appends nothing,
  reclaims no lock, and starts no worker, so an operator checking twice gets
  the same answer twice and changes nothing by asking;
* reconciliation refuses before it writes. A plan a live process owns, or one
  owned by a host this machine cannot evaluate, is left exactly as it was;
* no correction is lost at any boundary: one accepted and never delivered is
  carried by the run that resumes the session, across a run id that changed and
  a turn index that went backwards, and the journal records that it did;
* no correction is silently re-delivered either. Delivered-but-unreceipted is
  reported as uncertain and never acted on automatically, because the journal
  records what was handed to a worker and not what a worker read;
* an unanswered correction whose session will never run again is settled
  ``run-ended`` rather than left to look pending forever, and one the dead run
  had already accepted is reported instead of having its answer overwritten;
* the damaged-file cases -- a torn tail, an unattributable line, an unreadable
  ledger -- are reported as incompleteness rather than answered around;
* and a run that starts beside an owner it cannot rule out reconciles nothing,
  which is what keeps two processes from giving one correction two answers.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import (
    clock,
    conversations,
    plans,
    redirect_ack,
    redirect_recovery,
    redirect_store,
    run_loop,
    state,
)

from .support import write_plan
from .test_asl_turn_dispositions import FakeRunContext

SESSION_ID = "1"
DEAD_RUN = "run-crashed"
NEW_RUN = "run-successor"

FIRST = "stop rewriting the fixture and read the contract"
SECOND = "the second column is the one that matters"


def target(
    turn_index: int = 1,
    *,
    run_id: str = DEAD_RUN,
    session_id: str = SESSION_ID,
    harness: str | None = "claude",
) -> redirect_store.RedirectTarget:
    return redirect_store.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )


class RecoveryTestCase(unittest.TestCase):
    """A plan with its own state root and a real journal file behind it."""

    # Above every pid ceiling this runs on, so `is_pid_running` is false for it
    # without spawning and reaping anything -- which is what keeps this module
    # in the portable lane. The repo's other stale-lock tests use the same one.
    DEAD_PID = 2**30
    # A process that is certainly alive and certainly not this one. `os.kill(1, 0)`
    # answers either cleanly or with a permission error, and `is_pid_running`
    # reads both as running, so this needs no privileges and spawns nothing.
    LIVE_FOREIGN_PID = 1

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan_dir = self.root / "plan"
        self.state_dir = self.root / "state"
        write_plan(self.plan_dir, ("pending",))

    # -- journal construction ------------------------------------------------

    def submit(
        self,
        feedback: str = FIRST,
        *,
        turn_index: int = 1,
        session_id: str = SESSION_ID,
        run_id: str = DEAD_RUN,
        **kwargs: Any,
    ) -> redirect_store.RedirectRequest:
        return redirect_store.submit_request(
            self.plan_dir,
            target=target(turn_index, run_id=run_id, session_id=session_id),
            feedback=feedback,
            state_dir=self.state_dir,
            **kwargs,
        )

    def accept(
        self, request: redirect_store.RedirectRequest
    ) -> redirect_store.RedirectRequest:
        return redirect_store.accept_request(
            self.plan_dir,
            request.operation_id,
            target=request.target,
            state_dir=self.state_dir,
        )

    def read(self) -> redirect_store.RedirectJournalView:
        return redirect_store.read_journal(self.plan_dir, state_dir=self.state_dir)

    def journal_path(self) -> Path:
        return redirect_store.journal_path_for_plan(self.plan_dir, self.state_dir)

    def journal_lines(self) -> list[dict[str, Any]]:
        path = self.journal_path()
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # -- lock construction ---------------------------------------------------

    def write_lock(
        self,
        *,
        pid: int | None = None,
        hostname: str | None = None,
        run_id: str = DEAD_RUN,
    ) -> Path:
        """Publish a lock by hand.

        Written rather than acquired because every interesting case is a lock
        the acquiring API would refuse to produce: a pid that is not running, a
        pid belonging to another machine's process table.
        """
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid() if pid is None else pid,
                    "plan_dir": str(self.plan_dir.resolve()),
                    "created_at": clock.utc_iso_now(),
                    "hostname": socket.gethostname() if hostname is None else hostname,
                    "run_id": run_id,
                    "redirect_endpoint": str(plan_state / "redirect-wakeup.fifo"),
                }
            ),
            encoding="utf-8",
        )
        return lock_path

    # -- convenience ---------------------------------------------------------

    def reconcile(
        self,
        *,
        run_id: str = NEW_RUN,
        pending: tuple[str, ...] = (SESSION_ID,),
        owner_pid: int | None = None,
    ) -> redirect_recovery.ReconciliationReport:
        return redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id=run_id,
            pending_session_ids=pending,
            state_dir=self.state_dir,
            owner_pid=owner_pid,
        )

    def inspect(self) -> redirect_recovery.RecoveryReport:
        return redirect_recovery.inspect_plan_recovery(
            self.plan_dir, state_dir=self.state_dir
        )

    def status_of(self, operation_id: str) -> str:
        report = self.inspect()
        for item in report.operations:
            if item.operation_id == operation_id:
                return item.status
        raise AssertionError(f"{operation_id} is not in the report")


class StatusProjectionTests(RecoveryTestCase):
    """Reconstructing what an operation means from what is written down."""

    def test_pending_request_reads_as_submitted_while_a_run_can_answer_it(self) -> None:
        request = self.submit()
        self.write_lock()

        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_SUBMITTED,
        )

    def test_same_line_reads_as_unanswered_once_no_run_can_answer_it(self) -> None:
        request = self.submit()
        self.write_lock(pid=self.DEAD_PID)

        # Identical journal contents, opposite meaning. What changed is the
        # only thing that decides whether an operator is waiting or stranded.
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_UNANSWERED,
        )

    def test_a_lock_on_another_host_is_never_read_as_a_dead_owner(self) -> None:
        request = self.submit()
        self.write_lock(hostname="some-other-machine")

        owner = redirect_recovery.inspect_owner(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(owner.liveness, redirect_recovery.OWNER_UNKNOWN)
        self.assertTrue(owner.answerable)
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_SUBMITTED,
        )

    def test_accepted_and_undelivered_is_its_own_status(self) -> None:
        request = self.accept(self.submit())
        self.write_lock(pid=self.DEAD_PID)

        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_ACCEPTED,
        )

    def test_delivered_without_a_receipt_is_uncertain_rather_than_delivered(
        self,
    ) -> None:
        request = self.accept(self.submit())
        redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)

        # The journal knows what was handed to a worker. Whether a model read
        # it is the question the acknowledgment protocol exists because nothing
        # cheaper answers, so recovery declines to answer it here too.
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_UNCERTAIN,
        )

    def test_a_receipt_settles_the_status(self) -> None:
        request = self.accept(self.submit())
        redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(2),
            state_dir=self.state_dir,
        )
        redirect_store.acknowledge_request(
            self.plan_dir,
            request.operation_id,
            attempt=target(2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)

        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_ACKNOWLEDGED,
        )

    def test_a_past_deadline_reads_as_expired_before_any_sweep_records_it(self) -> None:
        request = self.submit(ttl_seconds=redirect_store.REDIRECT_MIN_TTL_SECONDS)
        self.write_lock(pid=self.DEAD_PID)
        later = clock.utc_now() + timedelta(days=1)

        report = redirect_recovery.inspect_plan_recovery(
            self.plan_dir, state_dir=self.state_dir, now=later
        )
        statuses = {item.operation_id: item.status for item in report.operations}
        self.assertEqual(
            statuses[request.operation_id], redirect_recovery.STATUS_EXPIRED
        )
        # And the read did not record it: reporting a refusal is not making one.
        self.assertEqual(
            self.read().request(request.operation_id).state,  # type: ignore[union-attr]
            redirect_store.STATE_SUBMITTED,
        )

    def test_the_report_never_carries_the_operator_words(self) -> None:
        self.accept(self.submit(FIRST))
        self.write_lock(pid=self.DEAD_PID)

        rendered = json.dumps(self.inspect().to_payload())
        self.assertNotIn(FIRST, rendered)
        self.assertIn(redirect_store.feedback_digest(FIRST), rendered)


class ReadingIsInertTests(RecoveryTestCase):
    """An operator asking what happened must not change what happened."""

    def test_inspecting_appends_nothing_and_settles_nothing(self) -> None:
        request = self.submit()
        self.write_lock(pid=self.DEAD_PID)
        before = self.journal_path().read_bytes()

        self.inspect()
        self.inspect()

        self.assertEqual(self.journal_path().read_bytes(), before)
        self.assertEqual(
            self.read().request(request.operation_id).state,  # type: ignore[union-attr]
            redirect_store.STATE_SUBMITTED,
        )

    def test_inspecting_does_not_reclaim_the_dead_lock_it_classifies(self) -> None:
        self.submit()
        lock_path = self.write_lock(pid=self.DEAD_PID)

        report = self.inspect()

        self.assertEqual(report.owner.liveness, redirect_recovery.OWNER_GONE)
        # `read_lock_info` would have deleted this file. A question about a run
        # must not be the thing that erases the evidence about it.
        self.assertTrue(lock_path.exists())

    def test_inspecting_opens_no_redirect_endpoint(self) -> None:
        self.submit()
        self.write_lock(pid=self.DEAD_PID)
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        fifo = plan_state / "redirect-wakeup.fifo"
        os.mkfifo(fifo)

        # A FIFO with no reader blocks an opener forever, so a read that
        # touched the endpoint would hang here rather than fail an assertion.
        report = self.inspect()

        self.assertEqual(
            report.owner.redirect_endpoint,
            str(fifo),
        )

    def test_inspecting_a_plan_with_no_journal_reports_an_empty_history(self) -> None:
        report = self.inspect()

        self.assertEqual(report.operations, ())
        self.assertTrue(report.complete)
        self.assertFalse(self.journal_path().exists())


class ReconciliationRefusalTests(RecoveryTestCase):
    """No mutation until the lock evidence agrees."""

    def test_a_live_owner_that_is_not_us_blocks_every_write(self) -> None:
        request = self.submit()
        self.write_lock()  # this process, which is very much alive
        before = self.journal_path().read_bytes()

        report = self.reconcile(owner_pid=os.getpid() + 1)

        self.assertTrue(report.blocked)
        self.assertEqual(report.blocked_reason, redirect_recovery.BLOCKED_LIVE_OWNER)
        self.assertEqual(self.journal_path().read_bytes(), before)
        self.assertEqual(
            self.read().request(request.operation_id).state,  # type: ignore[union-attr]
            redirect_store.STATE_SUBMITTED,
        )

    def test_a_plan_with_no_lock_at_all_is_reconciled(self) -> None:
        # The ordinary shape by the time the run loop asks: `acquire_lock` has
        # already reclaimed the dead run's stale lock, and a plan whose lock was
        # cleaned up by anything else looks the same. No owner is not an
        # unknown owner, and refusing here would strand every real crash.
        request = self.accept(self.submit())

        report = self.reconcile()

        self.assertFalse(report.blocked)
        assert report.owner is not None
        self.assertEqual(report.owner.liveness, redirect_recovery.OWNER_GONE)
        self.assertEqual(report.recoverable_for(SESSION_ID), (request.operation_id,))

    def test_our_own_lock_does_not_block_us(self) -> None:
        self.submit(session_id="9")
        self.write_lock()

        report = self.reconcile(owner_pid=os.getpid(), pending=(SESSION_ID,))

        self.assertFalse(report.blocked)
        self.assertTrue(report.reconciled)

    def test_a_foreign_host_lock_blocks_reconciliation(self) -> None:
        self.submit()
        self.write_lock(hostname="some-other-machine")
        before = self.journal_path().read_bytes()

        report = self.reconcile()

        self.assertTrue(report.blocked)
        self.assertEqual(report.blocked_reason, redirect_recovery.BLOCKED_FOREIGN_OWNER)
        self.assertEqual(self.journal_path().read_bytes(), before)

    def test_a_blocked_report_offers_nothing_to_carry(self) -> None:
        self.accept(self.submit())
        self.write_lock(hostname="some-other-machine")

        report = self.reconcile()

        self.assertEqual(
            redirect_recovery.recoverable_operation_ids(report, SESSION_ID), ()
        )


class NoLostCorrectionTests(RecoveryTestCase):
    """A crash between acceptance and delivery must not eat the correction."""

    def test_an_accepted_undelivered_correction_is_offered_to_the_next_run(
        self,
    ) -> None:
        request = self.accept(self.submit())
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile()

        self.assertEqual(report.recoverable_for(SESSION_ID), (request.operation_id,))
        self.assertEqual(
            redirect_recovery.recoverable_operation_ids(report, SESSION_ID),
            (request.operation_id,),
        )

    def test_a_never_accepted_correction_is_offered_too(self) -> None:
        # Submitted while the run was dying, so nobody ever accepted it. The
        # operator was told it was written down; this is what honours that.
        request = self.submit()
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile()

        self.assertEqual(report.recoverable_for(SESSION_ID), (request.operation_id,))

    def test_the_successor_run_carries_it_across_the_crash(self) -> None:
        request = self.accept(self.submit(FIRST, turn_index=7))
        self.write_lock(pid=self.DEAD_PID)
        report = self.reconcile()

        # A new run mints a fresh id and numbers its turns from one, so the
        # continuation has a different run id *and* a lower turn index than the
        # target. Both are exactly what the ordinary rule refuses.
        continuation = target(1, run_id=NEW_RUN)
        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=continuation,
            recovered_operation_ids=redirect_recovery.recoverable_operation_ids(
                report, SESSION_ID
            ),
            state_dir=self.state_dir,
        )

        self.assertEqual(carried.operation_ids(), (request.operation_id,))
        self.assertEqual(carried.corrections[0].text, FIRST)
        self.assertTrue(carried.corrections[0].recovered)
        self.assertEqual(carried.refused, ())

    def test_the_journal_records_that_delivery_crossed_a_crash(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        self.write_lock(pid=self.DEAD_PID)
        report = self.reconcile()
        continuation = target(1, run_id=NEW_RUN)
        redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=continuation,
            recovered_operation_ids=redirect_recovery.recoverable_operation_ids(
                report, SESSION_ID
            ),
            state_dir=self.state_dir,
        )

        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.recovered)
        continued = [
            line
            for line in self.journal_lines()
            if line["kind"] == redirect_store.KIND_CONTINUED
        ]
        self.assertEqual([line.get("recovered") for line in continued], [True])

    def test_a_recovered_delivery_can_still_be_receipted(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        self.write_lock(pid=self.DEAD_PID)
        report = self.reconcile()
        continuation = target(1, run_id=NEW_RUN)
        redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=continuation,
            recovered_operation_ids=redirect_recovery.recoverable_operation_ids(
                report, SESSION_ID
            ),
            state_dir=self.state_dir,
        )

        receipt = redirect_ack.acknowledge_correction(
            self.plan_dir,
            request.operation_id,
            token=redirect_ack.attempt_token(continuation),
            live_attempt=continuation,
            state_dir=self.state_dir,
        )

        self.assertEqual(receipt.operation_id, request.operation_id)
        self.assertFalse(receipt.repeated)
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_ACKNOWLEDGED,
        )

    def test_the_operators_order_survives_the_crash(self) -> None:
        first = self.accept(self.submit(FIRST, turn_index=7))
        second = self.submit(SECOND, turn_index=7)
        self.write_lock(pid=self.DEAD_PID)
        report = self.reconcile()

        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=redirect_recovery.recoverable_operation_ids(
                report, SESSION_ID
            ),
            state_dir=self.state_dir,
        )

        self.assertEqual(
            carried.operation_ids(), (first.operation_id, second.operation_id)
        )

    def test_carrying_across_a_crash_happens_once(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        self.write_lock(pid=self.DEAD_PID)
        report = self.reconcile()
        recovered = redirect_recovery.recoverable_operation_ids(report, SESSION_ID)
        redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=recovered,
            state_dir=self.state_dir,
        )
        lines_after_first = len(self.journal_lines())

        # A second attempt of the same session in the same run. The correction
        # is already in the pinned conversation's history; re-rendering it would
        # tell the worker its first correction is new.
        again = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(2, run_id=NEW_RUN),
            recovered_operation_ids=recovered,
            state_dir=self.state_dir,
        )

        self.assertEqual(again.operation_ids(), ())
        self.assertEqual(len(self.journal_lines()), lines_after_first)
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.continuation, target(1, run_id=NEW_RUN))


class NoSilentRedeliveryTests(RecoveryTestCase):
    """A crash after delivery is ambiguous, and ambiguity is reported."""

    def delivered_but_unreceipted(self) -> redirect_store.RedirectRequest:
        request = self.accept(self.submit(turn_index=7))
        redirect_store.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)
        return request

    def test_it_is_reported_as_uncertain_and_not_offered(self) -> None:
        request = self.delivered_but_unreceipted()

        report = self.reconcile()

        self.assertEqual(report.uncertain_for(SESSION_ID), (request.operation_id,))
        self.assertEqual(report.recoverable_for(SESSION_ID), ())
        self.assertEqual(
            redirect_recovery.recoverable_operation_ids(report, SESSION_ID), ()
        )

    def test_it_is_not_settled_either(self) -> None:
        request = self.delivered_but_unreceipted()

        self.reconcile()

        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.accepted)
        self.assertIsNone(stored.reason)

    def test_naming_it_by_hand_still_cannot_deliver_it_twice(self) -> None:
        request = self.delivered_but_unreceipted()
        self.reconcile()
        lines_before = len(self.journal_lines())

        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=(request.operation_id,),
            state_dir=self.state_dir,
        )

        # Even a caller that insists: the coalescing boundary drops an id an
        # earlier attempt carried, so no route through recovery can get the
        # journal to record one delivery twice.
        self.assertEqual(carried.operation_ids(), ())
        self.assertEqual(len(self.journal_lines()), lines_before)
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.continuation, target(8))

    def test_the_operator_restores_it_by_saying_it_again(self) -> None:
        stale = self.delivered_but_unreceipted()
        report = self.reconcile()

        # Having read the report, the operator resubmits. A fresh request, its
        # own id, its own acceptance -- and the session's next attempt carries
        # it while the ambiguous one stays exactly as the crash left it.
        fresh = self.accept(self.submit(SECOND, turn_index=8))
        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=(fresh.operation_id,),
            state_dir=self.state_dir,
        )

        self.assertEqual(carried.operation_ids(), (fresh.operation_id,))
        self.assertEqual(carried.corrections[0].text, SECOND)
        self.assertEqual(report.uncertain_for(SESSION_ID), (stale.operation_id,))
        stored = self.read().request(stale.operation_id)
        assert stored is not None
        self.assertFalse(stored.acknowledged)


class StrandedSettlementTests(RecoveryTestCase):
    """A correction no future attempt can carry gets a durable answer."""

    def test_an_unanswered_correction_for_a_finished_session_is_settled(self) -> None:
        request = self.submit(session_id="4")
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile(pending=(SESSION_ID,))

        self.assertEqual(report.settled, (request.operation_id,))
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.state, redirect_store.STATE_REJECTED)
        self.assertEqual(stored.reason, redirect_store.REASON_RUN_ENDED)

    def test_an_accepted_one_is_reported_stranded_rather_than_overwritten(self) -> None:
        request = self.accept(self.submit(session_id="4"))
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile(pending=(SESSION_ID,))

        # Its answer is already written, and first terminal wins. Recording a
        # rejection over an acceptance would be this module reversing the dead
        # runner's decision about a turn it never saw.
        self.assertEqual(report.settled, ())
        self.assertEqual(
            [item.operation_id for item in report.stranded], [request.operation_id]
        )
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.state, redirect_store.STATE_ACCEPTED)

    def test_run_ended_is_a_different_answer_from_expired(self) -> None:
        request = self.submit(session_id="4")
        self.write_lock(pid=self.DEAD_PID)

        self.reconcile(pending=())

        stored = self.read().request(request.operation_id)
        assert stored is not None
        # Its window had not closed. What ended was the process that owed the
        # operator an answer, and saying "expired" would blame the clock.
        self.assertEqual(stored.reason, redirect_store.REASON_RUN_ENDED)
        self.assertFalse(stored.is_expired_at(clock.utc_now()))

    def test_the_settlement_survives_the_process_that_made_it(self) -> None:
        request = self.submit(session_id="4")
        self.write_lock(pid=self.DEAD_PID)
        self.reconcile(pending=())

        # A second run reading the same file reaches the same answer without
        # re-deciding anything, which is what makes the record the authority.
        second = self.reconcile(run_id="run-third", pending=(SESSION_ID,))

        self.assertEqual(second.settled, ())
        self.assertEqual(second.recoverable, ())
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_REJECTED,
        )

    def test_due_requests_are_expired_in_the_journal(self) -> None:
        request = self.submit(ttl_seconds=redirect_store.REDIRECT_MIN_TTL_SECONDS)
        self.write_lock(pid=self.DEAD_PID)

        report = redirect_recovery.reconcile_startup(
            self.plan_dir,
            run_id=NEW_RUN,
            pending_session_ids=(SESSION_ID,),
            state_dir=self.state_dir,
            now=clock.utc_now() + timedelta(days=1),
        )

        self.assertEqual(report.expired, (request.operation_id,))
        self.assertEqual(report.recoverable, ())
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.state, redirect_store.STATE_EXPIRED)

    def test_this_runs_own_requests_are_never_reconciled(self) -> None:
        request = self.accept(self.submit(run_id=NEW_RUN))
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile(run_id=NEW_RUN, pending=())

        # Settling the live run's own accepted correction would be this module
        # deciding a turn it knows nothing about is over.
        self.assertEqual(report.settled, ())
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.accepted)


class DamagedEvidenceTests(RecoveryTestCase):
    """Incompleteness is reported rather than answered around."""

    def test_a_torn_tail_is_visible_as_incompleteness(self) -> None:
        self.accept(self.submit())
        with self.journal_path().open("a", encoding="utf-8") as handle:
            # A writer that died mid-record: the bytes it managed, no newline.
            handle.write('{"version":1,"kind":"acce')

        report = self.inspect()

        self.assertTrue(report.torn_tail)
        self.assertFalse(report.complete)

    def test_the_next_append_terminates_the_torn_line_instead_of_merging(self) -> None:
        request = self.accept(self.submit())
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write('{"version":1,"kind":"acce')
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile()
        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=redirect_recovery.recoverable_operation_ids(
                report, SESSION_ID
            ),
            state_dir=self.state_dir,
        )

        self.assertEqual(carried.operation_ids(), (request.operation_id,))
        self.assertFalse(self.inspect().torn_tail)

    def test_an_unattributable_line_is_counted_rather_than_applied(self) -> None:
        request = self.accept(self.submit())
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write("{not json at all}\n")

        report = self.inspect()

        self.assertEqual(report.unreadable_lines, 1)
        self.assertFalse(report.complete)
        # The request it sits beside is unaffected: a sequence is consumed, not
        # a transition applied.
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_ACCEPTED,
        )

    def test_an_unreadable_ledger_withholds_every_recovery(self) -> None:
        self.accept(self.submit())
        self.write_lock(pid=self.DEAD_PID)
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        conversations.conversation_ledger_path(plan_state).write_text(
            "not a conversation record\n", encoding="utf-8"
        )

        report = self.reconcile()

        # The turn this correction would be delivered into cannot be selected
        # until the ledger is repaired, so handing it over now would mark it
        # delivered to a prompt that is never going to be built.
        assert report.conversations is not None
        self.assertFalse(report.conversations.usable)
        self.assertEqual(
            redirect_recovery.recoverable_operation_ids(report, SESSION_ID), ()
        )
        # And it stays recoverable for the run that starts after the repair.
        self.assertNotEqual(report.recoverable_for(SESSION_ID), ())

    def test_a_hand_written_recovery_marker_cannot_forge_a_delivery(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        forged = {
            "version": redirect_store.REDIRECT_JOURNAL_VERSION,
            "kind": redirect_store.KIND_CONTINUED,
            "id": request.operation_id,
            "created_at": clock.utc_iso_now(),
            # A different session, dressed up as a recovery.
            "continuation": {
                "run_id": NEW_RUN,
                "session_id": "9",
                "turn_index": 1,
                "harness": None,
            },
            "recovered": True,
        }
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged) + "\n")

        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertFalse(stored.carried)
        self.assertIn(
            redirect_store.REJECT_INVALID_CONTINUATION,
            [item.reason for item in self.read().rejected],
        )

    def test_an_unmarked_cross_run_continuation_is_still_refused(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        unmarked = {
            "version": redirect_store.REDIRECT_JOURNAL_VERSION,
            "kind": redirect_store.KIND_CONTINUED,
            "id": request.operation_id,
            "created_at": clock.utc_iso_now(),
            "continuation": {
                "run_id": NEW_RUN,
                "session_id": SESSION_ID,
                "turn_index": 1,
                "harness": None,
            },
        }
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(unmarked) + "\n")

        stored = self.read().request(request.operation_id)
        assert stored is not None
        # Recovery's latitude is reachable only from a line that asked for it.
        self.assertFalse(stored.carried)

    def test_carrying_across_runs_without_the_opt_in_is_refused(self) -> None:
        request = self.accept(self.submit(turn_index=7))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.carry_request(
                self.plan_dir,
                request.operation_id,
                continuation=target(1, run_id=NEW_RUN),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            caught.exception.reason, redirect_store.ERROR_INVALID_CONTINUATION
        )

    def test_the_opt_in_does_not_reach_another_session(self) -> None:
        request = self.accept(self.submit(turn_index=7))

        with self.assertRaises(redirect_store.RedirectStoreError) as caught:
            redirect_store.carry_request(
                self.plan_dir,
                request.operation_id,
                continuation=target(1, run_id=NEW_RUN, session_id="9"),
                state_dir=self.state_dir,
                allow_recovery=True,
            )

        self.assertEqual(
            caught.exception.reason, redirect_store.ERROR_INVALID_CONTINUATION
        )


class RunLoopRecoveryTests(RecoveryTestCase):
    """Where the run loop meets the crash, and what it records there."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = FakeRunContext(event_path=self.root / "events.jsonl")
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def start_run(self, *, dry_run: bool = False) -> None:
        run_loop._reconcile_stranded_redirects(
            self.ctx,
            args=argparse.Namespace(dry_run=dry_run, state_dir=self.state_dir),
            snapshot=self.snapshot,
        )

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.ctx.persistence.events)

    def carried(
        self, *, pending: Any = None, turn_index: int = 1
    ) -> redirect_ack.CarriedCorrections | None:
        return run_loop._carried_corrections(
            self.ctx,
            plan_dir=self.plan_dir,
            state_dir_arg=self.state_dir,
            pending=pending,
            session_id=SESSION_ID,
            turn_index=turn_index,
            harness="claude",
            recovery=self.ctx.redirect_recovery_report,
        )

    def test_an_ordinary_start_records_nothing(self) -> None:
        self.start_run()

        # Nothing was stranded and nothing is in doubt: the overwhelming
        # majority of runs, and an event here would be noise in all of them.
        self.assertEqual([], self.events())

    def test_a_start_that_found_leftovers_says_so(self) -> None:
        request = self.accept(self.submit())
        self.write_lock(pid=self.DEAD_PID)

        self.start_run()

        name, payload = self.events()[-1]
        self.assertEqual("redirect_continuation", name)
        self.assertEqual("recovered", payload["outcome"])
        self.assertEqual(
            [request.operation_id],
            [item["operation_id"] for item in payload["recovery"]["recoverable"]],
        )
        self.assertNotIn(FIRST, json.dumps(payload))

    def test_the_recovered_correction_reaches_the_first_prompt(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        self.write_lock(pid=self.DEAD_PID)
        self.start_run()

        # `pending` is None: this run was never redirected, and without the
        # recovery report the session would start over as though the operator
        # had said nothing. That silent fresh start is the failure.
        carried = self.carried()

        assert carried is not None
        self.assertEqual(carried.operation_ids(), (request.operation_id,))
        self.assertEqual(carried.corrections[0].text, FIRST)
        name, payload = self.events()[-1]
        self.assertEqual("carried", payload["outcome"])
        self.assertTrue(payload["redirect"]["recovered"])

    def test_the_later_turns_of_that_session_carry_nothing(self) -> None:
        self.accept(self.submit(turn_index=7))
        self.write_lock(pid=self.DEAD_PID)
        self.start_run()
        self.carried(turn_index=1)
        events_after_delivery = len(self.events())

        # The report is a fact about the crash and still names this session, so
        # a second turn asks again. One correction, handed over once, reported
        # once -- not a delivery claimed on every turn that follows it.
        self.assertIsNone(self.carried(turn_index=2))
        self.assertEqual(events_after_delivery, len(self.events()))

    def test_a_dry_run_settles_nothing_and_carries_nothing(self) -> None:
        request = self.submit(session_id="4")
        self.write_lock(pid=self.DEAD_PID)
        before = self.journal_path().read_bytes()

        self.start_run(dry_run=True)

        self.assertIsNone(self.ctx.redirect_recovery_report)
        self.assertEqual(self.journal_path().read_bytes(), before)
        self.assertEqual([], self.events())
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.state, redirect_store.STATE_SUBMITTED)

    def test_a_live_owner_is_recorded_and_the_run_still_starts(self) -> None:
        self.accept(self.submit())
        # The run loop reconciles under its own lock, so "live owner" can only
        # mean a *different* live process -- one whose reconciliation this run
        # must not duplicate.
        self.write_lock(pid=self.LIVE_FOREIGN_PID)

        self.start_run()

        name, payload = self.events()[-1]
        self.assertEqual("recovery-blocked", payload["outcome"])
        self.assertEqual(
            redirect_recovery.BLOCKED_LIVE_OWNER,
            payload["recovery"]["blocked_reason"],
        )
        # Blocked recovery is not a blocked run: nothing here pauses.
        self.assertEqual([], self.ctx.pauses)
        self.assertIsNone(self.carried())

    def test_an_unreadable_journal_is_recorded_and_the_run_still_starts(self) -> None:
        self.accept(self.submit())
        self.write_lock(pid=self.DEAD_PID)
        journal = self.journal_path()
        journal.chmod(0o000)
        self.addCleanup(journal.chmod, 0o600)
        if os.access(journal, os.R_OK):
            self.skipTest("running as a user that ignores file permissions")

        self.start_run()

        # A plan whose journal cannot be read still has sessions worth running,
        # and pausing here would turn an undelivered correction into a run that
        # never started.
        name, payload = self.events()[-1]
        self.assertEqual("recovery-blocked", payload["outcome"])
        self.assertEqual(
            redirect_recovery.BLOCKED_JOURNAL_UNREADABLE,
            payload["recovery"]["blocked_reason"],
        )
        self.assertEqual([], self.ctx.pauses)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
