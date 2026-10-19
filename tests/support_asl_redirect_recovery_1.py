"""Focused test_asl_redirect_recovery test group 1."""

from __future__ import annotations

from .support_asl_redirect_recovery_base import (
    FIRST,
    SESSION_ID,
    RecoveryTestCase,
    clock,
    json,
    os,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_validation,
    redirect_store_vocab,
    state,
    target,
    timedelta,
)


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
