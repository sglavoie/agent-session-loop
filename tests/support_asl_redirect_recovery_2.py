"""Focused test_asl_redirect_recovery test group 2."""

from __future__ import annotations

from .support_asl_redirect_recovery_base import (
    FIRST,
    NEW_RUN,
    SECOND,
    SESSION_ID,
    RecoveryTestCase,
    json,
    redirect_ack,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_models,
    redirect_store_vocab,
    target,
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
