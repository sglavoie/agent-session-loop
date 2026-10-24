"""Redirect recovery from damaged evidence, and the run loop's startup recovery."""

from __future__ import annotations

from .support_asl_redirect_recovery_base import (
    FIRST,
    NEW_RUN,
    SESSION_ID,
    Any,
    FakeRunContext,
    RecoveryTestCase,
    argparse,
    clock,
    conversations,
    json,
    os,
    plans_snapshot,
    redirect_ack_carry,
    redirect_recovery,
    redirect_store_carry,
    redirect_store_models,
    redirect_store_vocab,
    run_loop_carried_corrections,
    run_loop_control_redirects,
    state,
    target,
)


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
        carried = redirect_ack_carry.carry_corrections(
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
    ) -> redirect_ack_carry.CarriedCorrections | None:
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
