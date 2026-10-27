"""Durable journal transitions, plan locks, and interruption accounting."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest import mock

from agent_session_loop import (
    state,
    state_locks,
    workbench_operation_journal,
    workbench_operations,
)
from asl_workbench import operations

from .support_workbench_operations import DEAD_PID, JournalFixture, _record

ASL_TEST_LANE = "integration"


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


class DurableJournalTests(JournalFixture, unittest.TestCase):
    """The receipt is a file, not a process's memory."""

    def setUp(self) -> None:
        self.install_journal()

    def test_an_admission_survives_a_fresh_reader(self) -> None:
        self.admit("op-1")
        # Nothing cached: this reads the file from scratch, which is what a
        # restarted server or a second process would do.
        self.assertEqual(["op-1"], [row["operation_id"] for row in self.rows()])

    def test_the_journal_lives_beside_the_redirect_journal(self) -> None:
        self.admit("op-1")
        self.assertEqual(
            state.plan_state_dir(self.canonical, self.state_dir),
            self.journal_path.parent,
        )
        self.assertEqual("operations.jsonl", self.journal_path.name)

    def test_the_journal_is_owner_only(self) -> None:
        self.admit("op-1")
        self.assertEqual(0o600, self.journal_path.stat().st_mode & 0o777)

    def test_a_replay_answers_from_the_file(self) -> None:
        self.admit("op-1")
        replayed = self.admit("op-1")
        self.assertTrue(replayed.replayed)
        self.assertEqual("op-1", replayed.record["operation_id"])

    def test_a_conflicting_reuse_is_refused_from_the_file(self) -> None:
        self.admit("op-1")
        refused = self.admit("op-1", method=operations.METHOD_ABORT)
        self.assertTrue(refused.refused)
        self.assertEqual(operations.REASON_OPERATION_CONFLICT, refused.reason)

    def test_dispatch_is_a_separate_durable_write(self) -> None:
        self.admit("op-1")
        self.assertIsNone(self.row("op-1")["dispatched_at"])
        workbench_operations.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        stored = self.row("op-1")
        self.assertEqual(operations.OPERATION_DISPATCHED, stored["status"])
        self.assertIsNotNone(stored["dispatched_at"])

    def test_confirming_settles_and_frees_the_scope(self) -> None:
        self.admit("op-1")
        workbench_operations.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        workbench_operations.confirm_operation(
            self.canonical, "op-1", run_id="run-1", state_dir=self.state_dir
        )
        self.assertEqual(operations.OPERATION_CONSUMED, self.row("op-1")["status"])
        self.assertEqual([], self.open_rows())
        self.assertTrue(self.admit("op-2").admitted)

    def open_rows(self) -> list[dict[str, Any]]:
        return workbench_operations.open_operations(
            self.canonical, state_dir=self.state_dir
        )

    def test_an_unknown_operation_cannot_be_advanced(self) -> None:
        self.admit("op-1")
        with self.assertRaises(operations.OperationError) as caught:
            workbench_operations.mark_dispatched(
                self.canonical, "op-missing", state_dir=self.state_dir
            )
        self.assertEqual(operations.REASON_UNKNOWN_OPERATION, caught.exception.reason)

    def test_a_torn_tail_does_not_make_a_plan_unmutatable(self) -> None:
        self.admit("op-1")
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write('{"version":1,"seq":99,"operat')
        # The half-written line is skipped, the complete one still stands, and
        # the next append lands on its own line rather than merging onto it.
        self.assertEqual(["op-1"], [row["operation_id"] for row in self.rows()])
        workbench_operations.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        self.assertEqual(operations.OPERATION_DISPATCHED, self.row("op-1")["status"])

    def test_overdeep_json_does_not_block_reads_or_later_writes(self) -> None:
        self.admit("op-1")
        with self.journal_path.open("ab") as handle:
            handle.write(b"[" * 1_000_000 + b"\n")
        self.assertEqual(["op-1"], [row["operation_id"] for row in self.rows()])
        workbench_operations.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        self.assertEqual(operations.OPERATION_DISPATCHED, self.row("op-1")["status"])
        self.assertTrue(self.admit("op-1").replayed)

    def _append_raw(self, operation: dict[str, Any]) -> None:
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"version": 1, "seq": 50, "operation": operation}) + "\n"
            )

    def test_a_declared_body_field_never_survives_a_read(self) -> None:
        self.admit("op-1")
        self._append_raw(
            {
                "operation_id": "op-leak",
                "status": "pending",
                "method": operations.METHOD_LAUNCH,
                "reply_text": "secret",
            }
        )
        leaked = self.row("op-leak")
        self.assertNotIn("reply_text", leaked)

    def test_a_row_smuggling_a_body_is_disowned(self) -> None:
        # The allowlist handles the declared fields. This is the undeclared
        # case -- prose nested under a key the allowlist keeps -- and trusting
        # the rest of a record that already broke the rule is how a leak
        # becomes permanent, so the whole row goes.
        self.admit("op-1")
        self._append_raw(
            {
                "operation_id": "op-leak",
                "status": "pending",
                "method": {"nested": {"reply_text": "secret"}},
            }
        )
        self.assertEqual(["op-1"], [row["operation_id"] for row in self.rows()])

    def test_compaction_keeps_the_open_operations_and_the_inode(self) -> None:
        self.admit("op-1")
        inode = self.journal_path.stat().st_ino
        with (
            mock.patch.object(
                workbench_operation_journal, "OPERATIONS_JOURNAL_MAX_BYTES", 2400
            ),
            mock.patch.object(
                workbench_operation_journal, "OPERATIONS_RETAINED_SETTLED", 2
            ),
        ):
            for index in range(2, 12):
                identifier = f"op-{index}"
                previous = f"op-{index - 1}"
                workbench_operations.mark_dispatched(
                    self.canonical, previous, state_dir=self.state_dir
                )
                workbench_operations.confirm_operation(
                    self.canonical, previous, state_dir=self.state_dir
                )
                self.assertTrue(self.admit(identifier).admitted)
        rows = self.rows()
        self.assertLessEqual(self.journal_path.stat().st_size, 2400)
        # Eleven operations went through; the settled tail was dropped and the
        # one still open was not.
        self.assertLessEqual(len(rows), 4)
        self.assertIn("op-11", [row["operation_id"] for row in rows])
        self.assertEqual(["op-11"], [row["operation_id"] for row in self.open_rows()])
        # The lock is held on this fd. Replacing the file would hand two
        # processes two different locks over the same journal.
        self.assertEqual(inode, self.journal_path.stat().st_ino)

    def test_failed_compaction_preserves_receipts_and_admission(self) -> None:
        self.admit("op-1")
        before = self.rows()
        inode = self.journal_path.stat().st_ino
        real_write = os.write
        real_sync = workbench_operation_journal.durability.fullsync
        for failure in ("short-write", "sync"):
            with self.subTest(failure=failure):
                with workbench_operation_journal._locked_journal(self.journal_path) as (
                    fd,
                    replay,
                ):

                    def write(target: int, data: bytes, fault: str = failure) -> int:
                        if target == fd and fault == "short-write":
                            return real_write(target, data[:10])
                        return real_write(target, data)

                    def sync(target: int, fault: str = failure) -> None:
                        if target == fd and fault == "sync":
                            raise OSError("injected journal sync failure")
                        real_sync(target)

                    with (
                        mock.patch.object(os, "write", side_effect=write),
                        mock.patch.object(
                            workbench_operation_journal.durability,
                            "fullsync",
                            side_effect=sync,
                        ),
                        self.assertRaises(operations.OperationError),
                    ):
                        workbench_operation_journal._compact_locked(
                            fd, replay, journal_path=self.journal_path
                        )
                self.assertEqual(before, self.rows())
                self.assertTrue(self.admit("op-1").replayed)
                self.assertEqual(
                    operations.REASON_OPERATION_OUTSTANDING,
                    self.admit("op-2").reason,
                )
                self.assertEqual(inode, self.journal_path.stat().st_ino)

    def test_torn_tail_separator_counts_against_the_size_limit(self) -> None:
        self.admit("op-1")
        with self.journal_path.open("ab") as handle:
            handle.write(b"{")
        with workbench_operation_journal._locked_journal(self.journal_path) as (
            fd,
            replay,
        ):
            row = replay.records["op-1"]
            encoded_size = (
                len(workbench_operation_journal._encode(replay.last_seq + 1, row)) + 1
            )
            bound = self.journal_path.stat().st_size + encoded_size
            with mock.patch.object(
                workbench_operation_journal, "OPERATIONS_JOURNAL_MAX_BYTES", bound
            ):
                workbench_operation_journal._append_locked(
                    fd, replay, row, journal_path=self.journal_path
                )
        self.assertLessEqual(self.journal_path.stat().st_size, bound)

    def test_recovery_syncs_its_replacement_before_truncating(self) -> None:
        self.admit("op-1")
        before = self.journal_path.read_bytes()
        with (
            workbench_operation_journal._locked_journal(self.journal_path) as (
                fd,
                replay,
            ),
            mock.patch.object(
                workbench_operation_journal.durability,
                "sync_directory",
                side_effect=OSError("injected directory sync failure"),
            ),
            self.assertRaises(operations.OperationError),
        ):
            workbench_operation_journal._compact_locked(
                fd, replay, journal_path=self.journal_path
            )
        recovery = self.journal_path.with_name("operations.jsonl.compact")
        self.assertTrue(recovery.exists())
        self.assertEqual(before, self.journal_path.read_bytes())
        # The replacement was renamed but its publication was not flushed.
        # A second failed barrier must leave both recovery sources untouched.
        with (
            mock.patch.object(
                workbench_operation_journal.durability,
                "sync_directory",
                side_effect=OSError("directory still cannot be synced"),
            ),
            self.assertRaises(operations.OperationError),
        ):
            self.admit("op-1")
        self.assertTrue(recovery.exists())
        self.assertEqual(before, self.journal_path.read_bytes())
        self.assertTrue(self.admit("op-1").replayed)
        self.assertFalse(recovery.exists())

    def test_export_drops_the_local_identifiers(self) -> None:
        self.admit("op-1", method=operations.METHOD_ABORT)
        self.admit("op-2")
        exported = {
            record["operation_id"]: record
            for record in workbench_operations.export_operations(
                self.canonical, state_dir=self.state_dir
            )
        }
        self.assertEqual(set(operations.OPERATION_EXPORT_FIELDS), set(exported["op-1"]))
        # A plan-scoped operation (here a launch) has no run: its journal row
        # stores the plan key in the run_key column, which export must not
        # present as a run identity.
        self.assertEqual(
            set(operations.OPERATION_EXPORT_FIELDS) - {"run_key"},
            set(exported["op-2"]),
        )


class PlanLockTests(JournalFixture, unittest.TestCase):
    """ASL's lock stays the execution authority; admission sits in front of it."""

    def setUp(self) -> None:
        self.install_journal()

    def test_admission_creates_no_lock(self) -> None:
        lock_path = state.lock_path_for_plan(self.canonical, self.state_dir)
        self.admit("op-1")
        workbench_operations.mark_dispatched(
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
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
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
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertTrue(lock_path.exists())


class InterruptionTests(JournalFixture, unittest.TestCase):
    """A killed process leaves a record that says which of two things happened."""

    def setUp(self) -> None:
        self.install_journal()

    def _orphan(self, status: str, **extra: Any) -> None:
        """Record an operation owned by a process this host knows is gone."""
        self.admit("op-1")
        if status != operations.OPERATION_PENDING:
            workbench_operations.mark_dispatched(
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
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_EXPIRED, self.row("op-1")["status"])

    def test_interrupted_after_dispatch_is_uncertain(self) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_UNCONFIRMED, self.row("op-1")["status"])

    def test_an_uncertain_operation_is_never_attempted_again(self) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        # Nothing in the vocabulary permits a second attempt, and replaying the
        # same id hands back the uncertain row rather than a fresh pending one.
        self.assertFalse(operations.attemptable(self.row("op-1")["status"]))
        replayed = self.admit("op-1")
        self.assertTrue(replayed.replayed)
        self.assertEqual(operations.OPERATION_UNCONFIRMED, replayed.record["status"])

    def test_a_live_owner_is_never_settled_out_from_under(self) -> None:
        self.admit("op-1")
        workbench_operations.mark_dispatched(
            self.canonical, "op-1", state_dir=self.state_dir
        )
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        self.assertEqual(operations.OPERATION_DISPATCHED, self.row("op-1")["status"])

    def test_a_new_submission_can_follow_an_uncertain_one(self) -> None:
        self._orphan(operations.OPERATION_DISPATCHED)
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        admitted = self.admit("op-2")
        self.assertTrue(admitted.admitted)
        self.assertEqual(operations.OPERATION_SUPERSEDED, self.row("op-1")["status"])
        self.assertEqual(
            operations.DETAIL_REPLACED_UNCERTAIN, self.row("op-1")["detail_code"]
        )


if __name__ == "__main__":
    unittest.main()
