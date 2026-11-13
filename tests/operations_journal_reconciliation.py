"""Reconciliation tests extracted from test_operations_journal."""

from __future__ import annotations

import json
import unittest
from typing import Any

from agent_session_loop import operation_rules as operations
from agent_session_loop import (
    operation_store,
    state,
    state_locks,
)

from .support_operations import DEAD_PID, JournalFixture, _record


class ReconciliationTests(unittest.TestCase):
    """What an interrupted operation is afterwards, and what it is never."""

    def test_a_settled_operation_is_left_alone(self) -> None:
        for status in operations.OPERATION_TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.assertIsNone(
                    operations.reconcile(_record(status=status), now=3000.0)
                )

    def test_a_live_owner_is_left_alone(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_DISPATCHED),
            operations.Evidence(owner_alive=True),
            now=3000.0,
        )
        self.assertIsNone(verdict)

    def test_pending_from_a_dead_owner_certainly_did_not_run(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_PENDING), now=3000.0
        )
        self.assertEqual(operations.OPERATION_EXPIRED, verdict.status)

    def test_an_overdue_pending_operation_says_so(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_PENDING, deliver_by=2500.0),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_EXPIRED, verdict.status)
        self.assertEqual(operations.DETAIL_DEADLINE_PASSED, verdict.detail_code)

    def test_dispatched_from_a_dead_owner_is_uncertain(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_DISPATCHED), now=3000.0
        )
        self.assertEqual(operations.OPERATION_UNCONFIRMED, verdict.status)

    def test_dispatched_is_accepted_only_when_its_run_can_be_seen(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_DISPATCHED, run_id="run-7"),
            operations.Evidence(run_active=True, run_id="run-7"),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_ACCEPTED, verdict.status)

    def test_someone_elses_run_does_not_account_for_this_one(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_DISPATCHED, run_id="run-7"),
            operations.Evidence(run_active=True, run_id="run-9"),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_UNCONFIRMED, verdict.status)

    def test_success_is_never_inferred_from_a_vanished_run(self) -> None:
        # The risk register's own words. An accepted operation whose run is
        # gone may have finished, crashed, or been killed, and only its own
        # records tell those apart -- so the answer is that nobody can say.
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_ACCEPTED, run_id="run-7"),
            operations.Evidence(run_active=False),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_UNCONFIRMED, verdict.status)
        self.assertNotEqual(operations.OPERATION_CONSUMED, verdict.status)

    def test_success_is_inferred_from_a_run_that_is_there(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_ACCEPTED, run_id="run-7"),
            operations.Evidence(run_active=True, run_id="run-7"),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_CONSUMED, verdict.status)

    def test_a_receipt_from_asl_wins(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_DISPATCHED),
            operations.Evidence(receipt_status=operations.OPERATION_REJECTED),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_REJECTED, verdict.status)

    def test_a_receipt_proposing_an_illegal_move_is_ignored(self) -> None:
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_PENDING),
            operations.Evidence(receipt_status=operations.OPERATION_CONSUMED),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_EXPIRED, verdict.status)

    def test_an_uncertain_operation_is_not_resolved_by_a_passive_sweep(self) -> None:
        self.assertIsNone(
            operations.reconcile(
                _record(status=operations.OPERATION_UNCONFIRMED), now=3000.0
            )
        )

    def test_a_late_reply_witness_resolves_an_uncertain_operation(self) -> None:
        verdict = operations.reconcile(
            _record(
                status=operations.OPERATION_UNCONFIRMED,
                method=operations.METHOD_QUESTION_REPLY,
            ),
            operations.Evidence(reply_consumed=True),
            now=3000.0,
        )
        self.assertEqual(operations.OPERATION_CONSUMED, verdict.status)
        self.assertEqual(operations.DETAIL_REPLY_WITNESSED, verdict.detail_code)

    def test_a_new_submission_replaces_an_uncertain_idle_operation(self) -> None:
        # Not a finding about whether it ran -- nothing can produce one. It is
        # the record that a person submitted something in its place, which is
        # the difference between "never repeat automatically" and "never act
        # again".
        verdict = operations.reconcile(
            _record(status=operations.OPERATION_UNCONFIRMED),
            now=3000.0,
            superseding=True,
        )
        self.assertEqual(operations.OPERATION_SUPERSEDED, verdict.status)
        self.assertEqual(operations.DETAIL_REPLACED_UNCERTAIN, verdict.detail_code)

    def test_an_uncertain_operation_is_not_replaced_while_a_run_is_live(self) -> None:
        self.assertIsNone(
            operations.reconcile(
                _record(status=operations.OPERATION_UNCONFIRMED),
                operations.Evidence(run_active=True, run_id="run-7"),
                now=3000.0,
                superseding=True,
            )
        )


class PlanLockTests(JournalFixture, unittest.TestCase):
    """ASL's lock stays the execution authority; admission sits in front of it."""

    def setUp(self) -> None:
        self.install_journal()

    def test_admission_creates_no_lock(self) -> None:
        lock_path = state.lock_path_for_plan(self.canonical, self.state_dir)
        self.admit("op-1")
        operation_store.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        self.assertFalse(lock_path.exists())

    def test_admission_leaves_an_existing_lock_untouched(self) -> None:
        handle = state_locks.acquire_lock(self.canonical, state_dir=self.state_dir)
        self.addCleanup(
            lambda: state_locks.release_lock(self.canonical, state_dir=self.state_dir)
        )
        del handle
        lock_path = state.lock_path_for_plan(self.canonical, self.state_dir)
        before = lock_path.read_bytes()
        self.admit("op-1")
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(before, lock_path.read_bytes())

    def test_reconciliation_never_reclaims_a_dead_lock(self) -> None:
        # Classifying the lock is how reconciliation gets its evidence.
        # Reclaiming it would make validating an action the thing that changes
        # the corpus being validated.
        state_locks.acquire_lock(self.canonical, state_dir=self.state_dir)
        lock_path = state.lock_path_for_plan(self.canonical, self.state_dir)
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        payload["pid"] = DEAD_PID
        lock_path.write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(lock_path.unlink)
        self.admit("op-1")
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertTrue(lock_path.exists())


class InterruptionTests(JournalFixture, unittest.TestCase):
    """A killed process leaves a record that says which of two things happened."""

    def setUp(self) -> None:
        self.install_journal()

    def _orphan(self, status: str, **extra: Any) -> None:
        """Record an operation owned by a process this host knows is gone."""
        self.admit("op-1")
        if status != operations.OPERATION_PENDING:
            operation_store.mark_dispatched(
                self.canonical, "op-1", state_dir=self.state_dir
            )
        row = dict(self.row("op-1"))
        row["pid"] = DEAD_PID
        row.update(extra)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"version": 1, "seq": 900, "operation": row}) + "\n"
            )

    def test_interrupted_before_dispatch_certainly_did_not_run(self) -> None:
        self._orphan(operations.OPERATION_PENDING)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_EXPIRED, self.row("op-1")["status"])

    def test_interrupted_after_dispatch_is_uncertain(self) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_UNCONFIRMED, self.row("op-1")["status"])

    def test_an_uncertain_operation_is_never_attempted_again(self) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        # Nothing in the vocabulary permits a second attempt, and replaying the
        # same id hands back the uncertain row rather than a fresh pending one.
        self.assertFalse(operations.attemptable(self.row("op-1")["status"]))
        replayed = self.admit("op-1")
        self.assertTrue(replayed.replayed)
        self.assertEqual(operations.OPERATION_UNCONFIRMED, replayed.record["status"])

    def test_a_live_owner_is_never_settled_out_from_under(self) -> None:
        self.admit("op-1")
        operation_store.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_DISPATCHED, self.row("op-1")["status"])

    def test_a_new_submission_can_follow_an_uncertain_one(self) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        admitted = self.admit("op-2")
        self.assertTrue(admitted.admitted)
        self.assertEqual(operations.OPERATION_SUPERSEDED, self.row("op-1")["status"])
        self.assertEqual(
            operations.DETAIL_REPLACED_UNCERTAIN, self.row("op-1")["detail_code"]
        )

    def _completed_operation(self, operation_id: str) -> None:
        self.admit(operation_id)
        operation_store.mark_dispatched(
            self.canonical, operation_id, state_dir=self.state_dir
        )
        operation_store.confirm_operation(
            self.canonical, operation_id, state_dir=self.state_dir
        )

    def test_replaying_an_old_request_does_not_replace_a_newer_uncertain_one(
        self,
    ) -> None:
        self._completed_operation("op-old")
        self._orphan(operations.OPERATION_DISPATCHED)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        before = self.journal_path.read_bytes()

        replayed = self.admit("op-old")

        self.assertTrue(replayed.replayed)
        self.assertEqual(operations.OPERATION_UNCONFIRMED, self.row("op-1")["status"])
        self.assertEqual(before, self.journal_path.read_bytes())

    def test_conflicting_reuse_does_not_replace_an_uncertain_operation(self) -> None:
        self._completed_operation("op-old")
        self._orphan(operations.OPERATION_DISPATCHED)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        before = self.journal_path.read_bytes()

        refused = self.admit("op-old", method=operations.METHOD_REPLY)

        self.assertTrue(refused.refused)
        self.assertEqual(operations.REASON_OPERATION_CONFLICT, refused.reason)
        self.assertEqual(operations.OPERATION_UNCONFIRMED, self.row("op-1")["status"])
        self.assertEqual(before, self.journal_path.read_bytes())

    def test_an_unrelated_submission_preserves_uncertainty_and_late_confirmation(
        self,
    ) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        operation_store.reconcile_plan(self.canonical, state_dir=self.state_dir)
        before = self.row("op-1")

        admitted = self.admit(
            "op-question-reply", method=operations.METHOD_QUESTION_REPLY
        )

        self.assertTrue(admitted.admitted)
        self.assertEqual(before, self.row("op-1"))
        operation_store.confirm_operation(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        self.assertEqual(operations.OPERATION_CONSUMED, self.row("op-1")["status"])

    def test_a_resume_can_replace_an_uncertain_launch_in_the_shared_scope(
        self,
    ) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)

        admitted = self.admit("op-resume", method=operations.METHOD_REPLY)

        self.assertTrue(admitted.admitted)
        self.assertEqual(operations.OPERATION_SUPERSEDED, self.row("op-1")["status"])
