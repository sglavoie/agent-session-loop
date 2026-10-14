"""What happens to a correction when the process holding it stops existing.

Every earlier redirect test drives a live runner. These drive the instants a
crash can land on, by building the journal a crash would have left and then
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
* a crash inside the window between binding a correction to a prompt and
  launching the worker that would hold it is recoverable rather than uncertain:
  the carry is handed back at startup, because the launch is written down
  separately and its absence is therefore evidence;
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
    plans_snapshot,
    redirect_ack,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
    run_loop_carried_corrections,
    run_loop_control_redirects,
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
) -> redirect_store_models.RedirectTarget:
    return redirect_store_models.RedirectTarget(
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
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(turn_index, run_id=run_id, session_id=session_id),
            feedback=feedback,
            state_dir=self.state_dir,
            **kwargs,
        )

    def accept(
        self, request: redirect_store_models.RedirectRequest
    ) -> redirect_store_models.RedirectRequest:
        return redirect_store_requests.accept_request(
            self.plan_dir,
            request.operation_id,
            target=request.target,
            state_dir=self.state_dir,
        )

    def read(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def journal_path(self) -> Path:
        return redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        )

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
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(2),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.deliver_request(
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

    def test_a_prompt_that_never_launched_stays_owed_to_the_next_attempt(
        self,
    ) -> None:
        request = self.accept(self.submit())
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(2),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)

        # Carried into a prompt, and then nothing ran. There is no ambiguity to
        # report: no worker has seen these words, so the status is the same one
        # a never-carried correction gets.
        self.assertEqual(
            self.status_of(request.operation_id),
            redirect_recovery.STATUS_ACCEPTED,
        )

    def test_a_receipt_settles_the_status(self) -> None:
        request = self.accept(self.submit())
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(2),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.acknowledge_request(
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
        request = self.submit(ttl_seconds=redirect_store_vocab.REDIRECT_MIN_TTL_SECONDS)
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
            redirect_store_vocab.STATE_SUBMITTED,
        )

    def test_the_report_never_carries_the_operator_words(self) -> None:
        self.accept(self.submit(FIRST))
        self.write_lock(pid=self.DEAD_PID)

        rendered = json.dumps(self.inspect().to_payload())
        self.assertNotIn(FIRST, rendered)
        self.assertIn(redirect_store_validation.feedback_digest(FIRST), rendered)


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
            redirect_store_vocab.STATE_SUBMITTED,
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
            redirect_store_vocab.STATE_SUBMITTED,
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
            if line["kind"] == redirect_store_vocab.KIND_CONTINUED
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


class CrashBeforeLaunchTests(RecoveryTestCase):
    """A crash inside the window between building a prompt and starting a worker.

    The prefix a crash leaves here is the whole difficulty: acceptance, then a
    carry, then nothing. Read as delivery it is indistinguishable from a worker
    that ran and said nothing -- which is uncertain, and uncertain is terminal
    for the words. What makes it recoverable is that the launch is written down
    separately, so its absence is evidence rather than silence.
    """

    def carried_but_unlaunched(self) -> redirect_store_models.RedirectRequest:
        """Accepted, bound into the next attempt's prompt, and then the crash."""
        request = self.accept(self.submit(turn_index=7))
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)
        return request

    def test_the_unlaunched_carry_is_handed_back_at_startup(self) -> None:
        request = self.carried_but_unlaunched()

        report = self.reconcile()

        self.assertEqual((request.operation_id,), report.released)
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertFalse(stored.carried)
        self.assertTrue(stored.released)
        self.assertFalse(stored.delivered)

    def test_it_is_offered_rather_than_reported_uncertain(self) -> None:
        request = self.carried_but_unlaunched()

        report = self.reconcile()

        self.assertEqual((request.operation_id,), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.uncertain_for(SESSION_ID))
        self.assertEqual(
            redirect_recovery.STATUS_ACCEPTED, self.status_of(request.operation_id)
        )

    def test_the_successor_carries_the_words_themselves(self) -> None:
        request = self.carried_but_unlaunched()
        report = self.reconcile()

        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=redirect_recovery.recoverable_operation_ids(
                report, SESSION_ID
            ),
            state_dir=self.state_dir,
        )

        self.assertEqual((request.operation_id,), carried.operation_ids())
        self.assertEqual(FIRST, carried.corrections[0].text)
        self.assertTrue(carried.corrections[0].recovered)
        self.assertEqual((), carried.refused)

    def test_a_carry_that_did_reach_a_worker_is_left_alone(self) -> None:
        """The other side of the same evidence, and the reason it is separate.

        Identical journals up to the carry. This one has the launch recorded, so
        what a model may have read is genuinely unknown and the correction is
        reported as uncertain rather than handed to a second worker.
        """
        request = self.accept(self.submit(turn_index=7))
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.deliver_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile()

        self.assertEqual((), report.released)
        self.assertEqual((request.operation_id,), report.uncertain_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for(SESSION_ID))

    def test_reconciling_twice_releases_once(self) -> None:
        request = self.carried_but_unlaunched()
        self.reconcile()
        lines = len(self.journal_lines())

        again = self.reconcile()

        self.assertEqual((), again.released)
        self.assertEqual((request.operation_id,), again.recoverable_for(SESSION_ID))
        self.assertEqual(lines, len(self.journal_lines()))

    def test_a_blocked_reconciliation_releases_nothing(self) -> None:
        """The refusal comes first, as it does for every other write here."""
        request = self.carried_but_unlaunched()
        self.write_lock(pid=self.LIVE_FOREIGN_PID)

        report = self.reconcile()

        self.assertTrue(report.blocked)
        self.assertEqual((), report.released)
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.carried)

    def test_the_release_is_reported_without_the_operators_words(self) -> None:
        self.carried_but_unlaunched()

        payload = self.reconcile().to_payload()

        self.assertNotIn(FIRST, json.dumps(payload))
        self.assertEqual(1, len(payload["released"]))


class CrashInsideTheLaunchWindowTests(RecoveryTestCase):
    """A crash between declaring the launch and hearing what the worker did.

    This is the narrow window the launch record exists to name. The prompt was
    written and handed towards a process; whether it left is exactly what the
    crash destroyed the answer to. Nothing here may be read as proof either
    way, so the carry stands: not released, not stranded, not replayed, and
    reported to the operator as the only party who can find out.
    """

    def carried_and_launching(self) -> redirect_store_models.RedirectRequest:
        """Accepted, bound into a prompt, declared outbound, and then the crash."""
        request = self.accept(self.submit(turn_index=7))
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        redirect_store_carry.launch_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)
        return request

    def test_the_launching_carry_is_not_handed_back(self) -> None:
        request = self.carried_and_launching()

        report = self.reconcile()

        self.assertEqual((), report.released)
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.carried)
        self.assertFalse(stored.released)
        self.assertFalse(stored.delivered)
        self.assertTrue(stored.launch_ambiguous)

    def test_it_is_reported_uncertain_rather_than_recoverable(self) -> None:
        request = self.carried_and_launching()

        report = self.reconcile()

        self.assertEqual((request.operation_id,), report.uncertain_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual(
            redirect_recovery.STATUS_UNCERTAIN, self.status_of(request.operation_id)
        )

    def test_no_route_through_recovery_replays_it(self) -> None:
        """Not offered, and not carried by a successor that names it anyway."""
        request = self.carried_and_launching()
        report = self.reconcile()

        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=(request.operation_id,),
            state_dir=self.state_dir,
        )

        self.assertEqual(
            (), redirect_recovery.recoverable_operation_ids(report, SESSION_ID)
        )
        self.assertEqual((), carried.operation_ids())
        self.assertNotIn(FIRST, json.dumps(report.to_payload()))

    def test_the_ambiguity_is_not_settled_as_stranded_either(self) -> None:
        """A finished session does not convert the unknown into an answer."""
        request = self.carried_and_launching()
        lines = len(self.journal_lines())

        report = self.reconcile(pending=())

        self.assertEqual((), report.stranded)
        self.assertEqual((request.operation_id,), report.uncertain_for(SESSION_ID))
        self.assertEqual(lines, len(self.journal_lines()))
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertTrue(stored.carried)

    def test_reconciling_twice_appends_nothing(self) -> None:
        request = self.carried_and_launching()
        self.reconcile()
        lines = len(self.journal_lines())

        again = self.reconcile()

        self.assertEqual((), again.released)
        self.assertEqual((request.operation_id,), again.uncertain_for(SESSION_ID))
        self.assertEqual(lines, len(self.journal_lines()))

    def test_a_refusal_recorded_before_the_crash_is_still_recoverable(self) -> None:
        """The launch window closes both ways, and only one of them is unknown.

        A run that declared the launch and then learned the prompt never went
        out says so by releasing the carry. That release is evidence, so the
        correction survives the same crash as an offer rather than a doubt.
        """
        request = self.accept(self.submit(turn_index=7))
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        redirect_store_carry.launch_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        redirect_store_delivery.release_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile()

        self.assertEqual((), report.uncertain_for(SESSION_ID))
        self.assertEqual((request.operation_id,), report.recoverable_for(SESSION_ID))
        self.assertEqual(
            redirect_recovery.STATUS_ACCEPTED, self.status_of(request.operation_id)
        )


class NoSilentRedeliveryTests(RecoveryTestCase):
    """A crash after delivery is ambiguous, and ambiguity is reported."""

    def delivered_but_unreceipted(self) -> redirect_store_models.RedirectRequest:
        request = self.accept(self.submit(turn_index=7))
        redirect_store_carry.carry_request(
            self.plan_dir,
            request.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )
        # The launch mark, not just the carry: a carry on its own says a prompt
        # was assembled, and the ambiguity these tests are about begins only
        # once a worker was started holding the words.
        redirect_store_delivery.deliver_request(
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


class LegacyJournalUpgradeTests(RecoveryTestCase):
    """Journals written before the records this module now reasons from existed.

    Each upgrade moves what a line already on disk means, and both of them move
    it towards more evidence than the old writer ever had. Version 1 appended
    ``continued`` from a run whose worker was already up, so its journals hold
    the crash-before-launch shape without ever having been in that state.
    Version 2 split the carry off from the delivery but had no way to say a
    prompt was outbound, so its carries are silent about the one thing the
    release rule now reads their silence as proving. Under the current rule
    either one would become an unlaunched carry, which is the single shape this
    module releases and offers for automatic replay. The version on the line is
    what stops that, and these tests are the guarantee neither upgrade reverses
    itself: ambiguity stays ambiguous, and only the operator resolves it.
    """

    LEGACY_OP = "legacy-op"
    LEGACY_TEXT = "the correction the dead run already handed over"

    def write_legacy_carry(
        self,
        *,
        acknowledged: bool = False,
        version: int = redirect_store_vocab.REDIRECT_JOURNAL_LEGACY_DELIVERY_VERSION,
    ) -> None:
        """Lay down the journal a writer of that version would have left behind."""
        created = clock.utc_now()
        continuation = target(2).to_payload()
        rows: list[dict[str, Any]] = [
            {
                "version": version,
                "kind": "submitted",
                "id": self.LEGACY_OP,
                "created_at": clock.isoformat(created),
                "expires_at": clock.isoformat(created + timedelta(seconds=120)),
                **target(1).to_payload(),
                "text": self.LEGACY_TEXT,
            },
            {
                "version": version,
                "kind": "accepted",
                "id": self.LEGACY_OP,
                "created_at": clock.isoformat(created + timedelta(seconds=1)),
            },
            {
                "version": version,
                "kind": "continued",
                "id": self.LEGACY_OP,
                "created_at": clock.isoformat(created + timedelta(seconds=2)),
                "continuation": continuation,
            },
        ]
        if acknowledged:
            rows.append(
                {
                    "version": version,
                    "kind": "acknowledged",
                    "id": self.LEGACY_OP,
                    "created_at": clock.isoformat(created + timedelta(seconds=3)),
                    "continuation": continuation,
                }
            )
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        self.write_lock(pid=self.DEAD_PID)

    def test_the_whole_legacy_journal_still_replays(self) -> None:
        """Nothing about the upgrade makes an old line unreadable."""
        self.write_legacy_carry()

        view = self.read()

        self.assertEqual((), view.rejected)
        stored = view.request(self.LEGACY_OP)
        assert stored is not None
        self.assertTrue(stored.accepted)
        self.assertTrue(stored.carried)
        self.assertTrue(stored.delivered_legacy)

    def test_a_legacy_carry_is_not_released_as_an_unlaunched_one(self) -> None:
        self.write_legacy_carry()

        report = self.reconcile()

        self.assertEqual((), report.released)
        stored = self.read().request(self.LEGACY_OP)
        assert stored is not None
        self.assertTrue(stored.carried)
        self.assertFalse(stored.released)

    def test_it_is_reported_uncertain_rather_than_recoverable(self) -> None:
        self.write_legacy_carry()

        report = self.reconcile()

        self.assertEqual((self.LEGACY_OP,), report.uncertain_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual(
            redirect_recovery.STATUS_UNCERTAIN, self.status_of(self.LEGACY_OP)
        )

    def test_no_route_through_recovery_replays_it(self) -> None:
        """Not offered, and not deliverable to a caller that names it anyway."""
        self.write_legacy_carry()
        report = self.reconcile()

        carried = redirect_ack.carry_corrections(
            self.plan_dir,
            continuation=target(1, run_id=NEW_RUN),
            recovered_operation_ids=(self.LEGACY_OP,),
            state_dir=self.state_dir,
        )

        self.assertEqual(
            (), redirect_recovery.recoverable_operation_ids(report, SESSION_ID)
        )
        self.assertEqual((), carried.operation_ids())
        self.assertNotIn(self.LEGACY_TEXT, json.dumps(report.to_payload()))

    def test_a_legacy_receipt_survives_the_upgrade(self) -> None:
        """An acknowledged old correction is settled, not resurrected."""
        self.write_legacy_carry(acknowledged=True)

        report = self.reconcile()

        self.assertEqual((), report.uncertain_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual((), report.released)
        self.assertEqual(
            redirect_recovery.STATUS_ACKNOWLEDGED, self.status_of(self.LEGACY_OP)
        )

    def test_a_pre_launch_carry_is_uncertain_too(self) -> None:
        """Version 2 could split the carry off, but not say a prompt was outbound.

        Its carries end the same way whether the run died before the worker
        started or between submitting the prompt and writing the receipt, so
        the release rule has nothing to read and the operator gets the question.
        """
        self.write_legacy_carry(
            version=redirect_store_vocab.REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION
        )

        report = self.reconcile()

        self.assertEqual((), report.released)
        self.assertEqual((self.LEGACY_OP,), report.uncertain_for(SESSION_ID))
        self.assertEqual((), report.recoverable_for(SESSION_ID))
        self.assertEqual(
            redirect_recovery.STATUS_UNCERTAIN, self.status_of(self.LEGACY_OP)
        )

    def test_a_pre_launch_carry_is_not_read_as_a_delivery_either(self) -> None:
        """The two upgrades are separate claims, and only one of them applies.

        Version 2 did distinguish a carry from a delivery; what it lacked was
        the launch. Reading its silence as a delivery would settle receipts
        that were never earned, so the carry stays carried and unacknowledged.
        """
        self.write_legacy_carry(
            version=redirect_store_vocab.REDIRECT_JOURNAL_LEGACY_LAUNCH_VERSION
        )

        stored = self.read().request(self.LEGACY_OP)

        assert stored is not None
        self.assertTrue(stored.carried)
        self.assertFalse(stored.delivered)
        self.assertFalse(stored.delivered_legacy)
        self.assertTrue(stored.launch_unrecorded)
        self.assertTrue(stored.launch_ambiguous)

    def test_the_new_format_is_still_recovered_beside_it(self) -> None:
        """The upgrade preserves the distinction rather than collapsing it.

        One journal, both shapes: the legacy carry that stands for a delivery,
        and a current carry whose missing launch record is real evidence.
        Only the second one is handed back.
        """
        self.write_legacy_carry()
        fresh = self.accept(self.submit(SECOND, turn_index=7))
        redirect_store_carry.carry_request(
            self.plan_dir,
            fresh.operation_id,
            continuation=target(8),
            state_dir=self.state_dir,
        )

        report = self.reconcile()

        self.assertEqual((fresh.operation_id,), report.released)
        self.assertEqual((fresh.operation_id,), report.recoverable_for(SESSION_ID))
        self.assertEqual((self.LEGACY_OP,), report.uncertain_for(SESSION_ID))


class StrandedSettlementTests(RecoveryTestCase):
    """A correction no future attempt can carry gets a durable answer."""

    def test_an_unanswered_correction_for_a_finished_session_is_settled(self) -> None:
        request = self.submit(session_id="4")
        self.write_lock(pid=self.DEAD_PID)

        report = self.reconcile(pending=(SESSION_ID,))

        self.assertEqual(report.settled, (request.operation_id,))
        stored = self.read().request(request.operation_id)
        assert stored is not None
        self.assertEqual(stored.state, redirect_store_vocab.STATE_REJECTED)
        self.assertEqual(stored.reason, redirect_store_vocab.REASON_RUN_ENDED)

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
        self.assertEqual(stored.state, redirect_store_vocab.STATE_ACCEPTED)

    def test_run_ended_is_a_different_answer_from_expired(self) -> None:
        request = self.submit(session_id="4")
        self.write_lock(pid=self.DEAD_PID)

        self.reconcile(pending=())

        stored = self.read().request(request.operation_id)
        assert stored is not None
        # Its window had not closed. What ended was the process that owed the
        # operator an answer, and saying "expired" would blame the clock.
        self.assertEqual(stored.reason, redirect_store_vocab.REASON_RUN_ENDED)
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
        request = self.submit(ttl_seconds=redirect_store_vocab.REDIRECT_MIN_TTL_SECONDS)
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
        self.assertEqual(stored.state, redirect_store_vocab.STATE_EXPIRED)

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
            "version": redirect_store_vocab.REDIRECT_JOURNAL_VERSION,
            "kind": redirect_store_vocab.KIND_CONTINUED,
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
            redirect_store_vocab.REJECT_INVALID_CONTINUATION,
            [item.reason for item in self.read().rejected],
        )

    def test_an_unmarked_cross_run_continuation_is_still_refused(self) -> None:
        request = self.accept(self.submit(turn_index=7))
        unmarked = {
            "version": redirect_store_vocab.REDIRECT_JOURNAL_VERSION,
            "kind": redirect_store_vocab.KIND_CONTINUED,
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

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_carry.carry_request(
                self.plan_dir,
                request.operation_id,
                continuation=target(1, run_id=NEW_RUN),
                state_dir=self.state_dir,
            )

        self.assertEqual(
            caught.exception.reason, redirect_store_vocab.ERROR_INVALID_CONTINUATION
        )

    def test_the_opt_in_does_not_reach_another_session(self) -> None:
        request = self.accept(self.submit(turn_index=7))

        with self.assertRaises(redirect_store_models.RedirectStoreError) as caught:
            redirect_store_carry.carry_request(
                self.plan_dir,
                request.operation_id,
                continuation=target(1, run_id=NEW_RUN, session_id="9"),
                state_dir=self.state_dir,
                allow_recovery=True,
            )

        self.assertEqual(
            caught.exception.reason, redirect_store_vocab.ERROR_INVALID_CONTINUATION
        )


class RunLoopRecoveryTests(RecoveryTestCase):
    """Where the run loop meets the crash, and what it records there."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = FakeRunContext(event_path=self.root / "events.jsonl")
        self.snapshot = plans_snapshot.inspect_plan(self.plan_dir)

    def start_run(self, *, dry_run: bool = False) -> None:
        run_loop_control_redirects._reconcile_stranded_redirects(
            self.ctx,
            args=argparse.Namespace(dry_run=dry_run, state_dir=self.state_dir),
            snapshot=self.snapshot,
        )

    def events(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self.ctx.persistence.events)

    def carried(
        self, *, pending: Any = None, turn_index: int = 1
    ) -> redirect_ack.CarriedCorrections | None:
        return run_loop_carried_corrections._carried_corrections(
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
        self.assertEqual(stored.state, redirect_store_vocab.STATE_SUBMITTED)

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
