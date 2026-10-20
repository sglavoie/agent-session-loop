"""Focused test_asl_redirect_recovery test group 3."""

from __future__ import annotations

from .support_asl_redirect_recovery_base import (
    NEW_RUN,
    SECOND,
    SESSION_ID,
    Any,
    RecoveryTestCase,
    clock,
    json,
    redirect_ack_carry,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_delivery,
    redirect_store_models,
    redirect_store_vocab,
    target,
    timedelta,
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

        carried = redirect_ack_carry.carry_corrections(
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
        carried = redirect_ack_carry.carry_corrections(
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

        carried = redirect_ack_carry.carry_corrections(
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
