"""Mutation admission: one accountable attempt, or an honest "nobody can say".

Session 5 puts a durable gate in front of every mutation the Workbench and the
connector can both submit. The claim is narrow and the tests here are the whole
of it: *concurrent submissions and process interruption produce a single
accountable attempt or an explicit uncertain outcome, never an automatic
repeat.* Four properties add up to that, and each has its own class.

**Identity is bound to the action and its target.** :class:`IdentityTests` and
:class:`AdmissionTests` hold the fingerprint rule: the same id resubmitted for
the same request replays, and the same id aimed at a different method or a
different plan is refused rather than quietly overwriting a mutation that may
already have run.

**Admission is serialized across processes.** :class:`CrossProcessTests` races
real child interpreters against one journal -- eight of them submitting the
same id, then eight submitting different ids for one plan -- and requires
exactly one admission out of each race. An in-process lock cannot produce that
result, which is the point: the guarantee is a file fact.

**Interruption is accountable.** :class:`InterruptionTests` kills a child
between admission and dispatch, and again after dispatch, and holds the two
answers apart: ``expired`` means the mutation certainly did not run,
``unconfirmed`` means it may have and nobody can say. The second class of test
then proves the dispatcher is never called a second time on either path --
"never an automatic repeat" is the only part of the claim a user notices.

**The plan lock is still the execution authority.** :class:`PlanLockTests`
holds that admission neither creates, moves, nor removes a lock file, and that
a live run is still refused by the route's own ownership check. Admission stops
a second attempt from being *started*; the lock is what stops a second run from
*executing*, and replacing the second with the first would be a regression.

:class:`PrivacyTests` and :class:`CompanionCompatibilityTests` guard the two
things that would make the journal a liability instead of an asset: prose that
must never reach it, and a vocabulary the Pi already holds rows under.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import plans, state, workbench, workbench_operations
from asl_workbench import companion_inventory, operations
from tests.support import (
    _http_request,
    _write_lock_file,
    _write_state_payload,
    write_plan,
)
from tests.workbench_support import WorkbenchAPIFixture

REPO_ROOT = Path(__file__).resolve().parents[1]

#: A pid this host can prove is not running, so "the owner is gone" is a fact
#: the test establishes rather than a race it hopes for.
DEAD_PID = 4_242_424

_HOSTNAME = workbench_operations._hostname()


def _target(
    plan_key: str = "plan-abc", selector: str = ""
) -> operations.OperationTarget:
    return operations.OperationTarget(plan_key=plan_key, selector=selector)


def _record(**overrides: Any) -> dict[str, Any]:
    record = operations.new_record(
        operation_id="op-1",
        method=operations.METHOD_LAUNCH,
        target=_target(),
        now=1000.0,
        pid=os.getpid(),
        hostname=_HOSTNAME,
    )
    record.update(overrides)
    return record


class VocabularyTests(unittest.TestCase):
    """The status table, and what each status permits."""

    def test_every_status_has_a_transition_row(self) -> None:
        self.assertEqual(
            set(operations.OPERATION_STATUSES),
            set(operations.OPERATION_TRANSITIONS),
        )

    def test_every_transition_target_is_a_known_status(self) -> None:
        for status, allowed in operations.OPERATION_TRANSITIONS.items():
            with self.subTest(status=status):
                self.assertLessEqual(allowed, set(operations.OPERATION_STATUSES))

    def test_terminal_statuses_permit_nothing(self) -> None:
        for status in operations.OPERATION_TERMINAL_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(frozenset(), operations.OPERATION_TRANSITIONS[status])

    def test_only_a_pending_operation_may_be_attempted(self) -> None:
        attemptable = {
            status
            for status in operations.OPERATION_STATUSES
            if operations.attemptable(status)
        }
        self.assertEqual({operations.OPERATION_PENDING}, attemptable)

    def test_unconfirmed_is_outstanding_and_not_terminal(self) -> None:
        # The whole reason the state exists: an uncertain mutation is exactly
        # the case where a second one must not be started, and it stays open to
        # late evidence rather than being closed off as settled.
        self.assertTrue(operations.outstanding(operations.OPERATION_UNCONFIRMED))
        self.assertFalse(operations.terminal(operations.OPERATION_UNCONFIRMED))
        self.assertNotEqual(
            frozenset(),
            operations.OPERATION_TRANSITIONS[operations.OPERATION_UNCONFIRMED],
        )

    def test_an_illegal_transition_is_refused_by_name(self) -> None:
        with self.assertRaises(operations.OperationError) as caught:
            operations.next_status(
                operations.OPERATION_CONSUMED, operations.OPERATION_PENDING
            )
        self.assertEqual(operations.REASON_INVALID_TRANSITION, caught.exception.reason)
        self.assertEqual(409, caught.exception.status)

    def test_an_unknown_method_is_refused(self) -> None:
        with self.assertRaises(operations.OperationError) as caught:
            operations.require_method("plan.detonate")
        self.assertEqual(operations.REASON_INVALID_METHOD, caught.exception.reason)

    def test_a_remote_alias_resolves_to_the_shared_vocabulary(self) -> None:
        # A command from the Pi admits through the same gate as a local click,
        # which is the only reason a *shared* admission is worth having.
        self.assertEqual(
            operations.METHOD_REPLY, operations.require_method("run.resume")
        )

    def test_the_two_spawning_methods_share_one_scope(self) -> None:
        self.assertEqual(
            operations.method_scope(operations.METHOD_LAUNCH),
            operations.method_scope(operations.METHOD_REPLY),
        )
        self.assertNotEqual(
            operations.method_scope(operations.METHOD_LAUNCH),
            operations.method_scope(operations.METHOD_ABORT),
        )

    def test_an_unbounded_operation_id_is_refused(self) -> None:
        for candidate in ("", "   ", "a b", "x" * 65):
            with self.subTest(candidate=candidate):
                with self.assertRaises(operations.OperationError) as caught:
                    operations.require_operation_id(candidate)
                self.assertEqual(
                    operations.REASON_INVALID_OPERATION_ID, caught.exception.reason
                )


class IdentityTests(unittest.TestCase):
    """An operation id names one action against one target, or nothing."""

    def test_the_fingerprint_changes_with_the_method(self) -> None:
        self.assertNotEqual(
            operations.operation_fingerprint(operations.METHOD_LAUNCH, _target()),
            operations.operation_fingerprint(operations.METHOD_ABORT, _target()),
        )

    def test_the_fingerprint_changes_with_the_target(self) -> None:
        self.assertNotEqual(
            operations.operation_fingerprint(operations.METHOD_LAUNCH, _target()),
            operations.operation_fingerprint(
                operations.METHOD_LAUNCH, _target(plan_key="plan-other")
            ),
        )

    def test_the_fingerprint_changes_with_the_selector(self) -> None:
        self.assertNotEqual(
            operations.operation_fingerprint(operations.METHOD_REDIRECT, _target()),
            operations.operation_fingerprint(
                operations.METHOD_REDIRECT, _target(selector="turn-4")
            ),
        )

    def test_the_fingerprint_is_stable(self) -> None:
        first = operations.operation_fingerprint(operations.METHOD_LAUNCH, _target())
        second = operations.operation_fingerprint(operations.METHOD_LAUNCH, _target())
        self.assertEqual(first, second)

    def test_the_fingerprint_carries_no_request(self) -> None:
        # It names the request without holding it: 32 hex characters, which is
        # what lets a journal row refuse a conflicting reuse while storing
        # nothing an operator typed.
        digest = operations.operation_fingerprint(operations.METHOD_LAUNCH, _target())
        self.assertEqual(32, len(digest))
        self.assertTrue(all(character in "0123456789abcdef" for character in digest))

    def test_conflicting_reuse_names_which_half_disagrees(self) -> None:
        recorded = _record()
        self.assertIsNone(
            operations.conflicting_reuse(
                recorded, method=operations.METHOD_LAUNCH, target=_target()
            )
        )
        self.assertEqual(
            operations.CONFLICT_METHOD,
            operations.conflicting_reuse(
                recorded, method=operations.METHOD_ABORT, target=_target()
            ),
        )
        self.assertEqual(
            operations.CONFLICT_TARGET,
            operations.conflicting_reuse(
                recorded,
                method=operations.METHOD_LAUNCH,
                target=_target(plan_key="plan-other"),
            ),
        )

    def test_a_target_needs_a_plan(self) -> None:
        with self.assertRaises(operations.OperationError):
            operations.OperationTarget(plan_key="")


class AdmissionTests(unittest.TestCase):
    """What happens to a submission, given what is already recorded."""

    def _admit(self, records: list[dict[str, Any]], **kwargs: Any) -> Any:
        parameters: dict[str, Any] = {
            "operation_id": "op-1",
            "method": operations.METHOD_LAUNCH,
            "target": _target(),
            "now": 2000.0,
        }
        parameters.update(kwargs)
        return operations.admit(records, **parameters)

    def test_a_fresh_submission_is_admitted_as_pending(self) -> None:
        admission = self._admit([])
        self.assertTrue(admission.admitted)
        self.assertIsNotNone(admission.record)
        self.assertEqual(operations.OPERATION_PENDING, admission.record["status"])
        # Pending, not dispatched: the gap between the two is what makes an
        # interruption answerable at all.
        self.assertIsNone(admission.record["dispatched_at"])

    def test_the_same_request_resubmitted_replays(self) -> None:
        recorded = _record(status=operations.OPERATION_DISPATCHED)
        admission = self._admit([recorded])
        self.assertTrue(admission.replayed)
        self.assertFalse(admission.admitted)
        self.assertEqual(operations.OPERATION_DISPATCHED, admission.record["status"])

    def test_an_id_reused_for_another_method_is_refused(self) -> None:
        admission = self._admit([_record()], method=operations.METHOD_ABORT)
        self.assertTrue(admission.refused)
        self.assertEqual(operations.REASON_OPERATION_CONFLICT, admission.reason)
        self.assertEqual(operations.CONFLICT_METHOD, admission.conflict)
        self.assertEqual(409, admission.error().status)

    def test_an_id_reused_for_another_target_is_refused(self) -> None:
        admission = self._admit([_record()], target=_target(plan_key="plan-other"))
        self.assertTrue(admission.refused)
        self.assertEqual(operations.CONFLICT_TARGET, admission.conflict)

    def test_a_second_submission_in_one_scope_is_refused(self) -> None:
        recorded = _record(status=operations.OPERATION_DISPATCHED)
        admission = self._admit([recorded], operation_id="op-2")
        self.assertTrue(admission.refused)
        self.assertEqual(operations.REASON_OPERATION_OUTSTANDING, admission.reason)

    def test_a_reply_is_refused_while_a_launch_is_outstanding(self) -> None:
        # The two spawning methods contend for the same thing -- a run holding
        # the plan -- so they contend with each other, not only with repeats of
        # themselves.
        recorded = _record(status=operations.OPERATION_PENDING)
        admission = self._admit(
            [recorded], operation_id="op-2", method=operations.METHOD_REPLY
        )
        self.assertTrue(admission.refused)
        self.assertEqual(operations.REASON_OPERATION_OUTSTANDING, admission.reason)

    def test_an_unrelated_method_is_not_blocked(self) -> None:
        recorded = _record(status=operations.OPERATION_DISPATCHED)
        admission = self._admit(
            [recorded], operation_id="op-2", method=operations.METHOD_ABORT
        )
        self.assertTrue(admission.admitted)

    def test_an_uncertain_operation_refuses_with_its_own_reason(self) -> None:
        recorded = _record(status=operations.OPERATION_UNCONFIRMED)
        admission = self._admit([recorded], operation_id="op-2")
        self.assertTrue(admission.refused)
        self.assertEqual(operations.REASON_OPERATION_UNCERTAIN, admission.reason)

    def test_a_settled_operation_blocks_nothing(self) -> None:
        for status in operations.OPERATION_TERMINAL_STATUSES:
            with self.subTest(status=status):
                admission = self._admit([_record(status=status)], operation_id="op-2")
                self.assertTrue(admission.admitted)

    def test_a_refusal_payload_carries_no_plan_path(self) -> None:
        admission = self._admit([_record()], method=operations.METHOD_ABORT)
        payload = json.dumps(admission.to_payload())
        self.assertNotIn("plan_key", payload)

    def test_an_admission_that_is_not_a_refusal_has_no_error(self) -> None:
        with self.assertRaises(operations.OperationError):
            self._admit([]).error()


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


class PrivacyTests(unittest.TestCase):
    """What a journal row may hold, and what it must never."""

    def test_the_reply_text_is_absent_from_the_persist_allowlist(self) -> None:
        for forbidden in ("reply_text", "action_token", "body", "prompt"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, operations.OPERATION_PERSIST_FIELDS)

    def test_the_export_allowlist_is_a_subset_of_the_persist_allowlist(self) -> None:
        self.assertLessEqual(
            operations.OPERATION_EXPORT_FIELDS, operations.OPERATION_PERSIST_FIELDS
        )

    def test_the_plan_key_and_owning_process_stay_off_the_wire(self) -> None:
        for local_only in ("plan_key", "pid", "hostname", "scope", "fingerprint"):
            with self.subTest(field=local_only):
                self.assertIn(local_only, operations.OPERATION_PERSIST_FIELDS)
                self.assertNotIn(local_only, operations.OPERATION_EXPORT_FIELDS)

    def test_projection_drops_everything_unlisted(self) -> None:
        projected = operations.persist_operation(
            {"operation_id": "op-1", "status": "pending", "reply_text": "secret"}
        )
        self.assertEqual({"operation_id", "status"}, set(projected))

    def test_a_nested_body_under_an_allowed_key_is_refused(self) -> None:
        # The allowlist already removes every declared body field. This is the
        # undeclared case: prose smuggled inside an object the allowlist keeps.
        with self.assertRaises(operations.OperationError) as caught:
            operations.persist_operation(
                {"operation_id": {"nested": {"reply_text": "secret"}}}
            )
        self.assertEqual(operations.REASON_BODY_LEAK, caught.exception.reason)

    def test_a_body_inside_a_list_is_found(self) -> None:
        with self.assertRaises(operations.OperationError):
            operations.assert_body_free({"rows": [{"transcript": "..."}]})

    def test_an_allowance_is_explicit_and_narrow(self) -> None:
        operations.assert_body_free(
            {"reply_text": "fine here"}, allow=frozenset({"reply_text"})
        )
        with self.assertRaises(operations.OperationError):
            operations.assert_body_free(
                {"reply_text": "fine", "transcript": "not fine"},
                allow=frozenset({"reply_text"}),
            )


class CompanionCompatibilityTests(unittest.TestCase):
    """The vocabulary matches the one the Pi already holds rows under."""

    def setUp(self) -> None:
        package_root = companion_inventory.locate_companion_package(REPO_ROOT)
        if package_root is None:
            self.skipTest(
                "no Pi checkout reachable; set "
                f"{companion_inventory.ENV_PI_CHECKOUT} to cross-check the "
                "operation vocabulary against the Companion"
            )
        self.pinned = _read_companion_protocol(package_root)

    def test_the_statuses_match(self) -> None:
        self.assertEqual(
            sorted(self.pinned["OPERATION_STATUSES"]),
            sorted(operations.OPERATION_STATUSES),
        )

    def test_the_transition_table_matches(self) -> None:
        self.assertEqual(
            {key: sorted(value) for key, value in self.pinned["transitions"].items()},
            {
                key: sorted(value)
                for key, value in operations.OPERATION_TRANSITIONS.items()
            },
        )

    def test_the_outstanding_and_terminal_sets_match(self) -> None:
        self.assertEqual(
            sorted(self.pinned["OPERATION_OUTSTANDING_STATUSES"]),
            sorted(operations.OPERATION_OUTSTANDING_STATUSES),
        )
        self.assertEqual(
            sorted(self.pinned["OPERATION_TERMINAL_STATUSES"]),
            sorted(operations.OPERATION_TERMINAL_STATUSES),
        )

    def test_the_export_projection_matches_exactly(self) -> None:
        # A dashboard row written by the Companion and one written here have to
        # be the same shape: the Pi holds both under the same schema.
        self.assertEqual(
            sorted(self.pinned["OPERATION_EXPORT_FIELDS"]),
            sorted(operations.OPERATION_EXPORT_FIELDS),
        )

    def test_the_forbidden_keys_match(self) -> None:
        self.assertEqual(
            sorted(self.pinned["FORBIDDEN_PERSISTED_KEYS"]),
            sorted(operations.FORBIDDEN_PERSISTED_KEYS),
        )

    def test_the_persist_allowlist_only_adds_identifiers(self) -> None:
        added = set(operations.OPERATION_PERSIST_FIELDS) - set(
            self.pinned["OPERATION_PERSIST_FIELDS"]
        )
        self.assertEqual({"fingerprint", "scope", "pid", "hostname"}, added)
        self.assertLessEqual(
            set(self.pinned["OPERATION_PERSIST_FIELDS"]),
            set(operations.OPERATION_PERSIST_FIELDS),
        )

    def test_the_companion_detail_codes_are_all_carried(self) -> None:
        self.assertLessEqual(
            set(self.pinned["OPERATION_DETAIL_CODES"]),
            set(operations.OPERATION_DETAIL_CODES),
        )


def _read_companion_protocol(package_root: Path) -> dict[str, Any]:
    """Read the Companion's vocabulary without writing to the Pi checkout.

    A subprocess with bytecode writing disabled, because the Pi repository is a
    read-only dependency here: importing a module from it must not leave a
    ``__pycache__`` behind in someone else's tree.
    """
    script = textwrap.dedent(
        """
        import json, sys
        sys.dont_write_bytecode = True
        sys.path.insert(0, sys.argv[1])
        from companion import protocol
        print(json.dumps({
            "OPERATION_STATUSES": list(protocol.OPERATION_STATUSES),
            "OPERATION_OUTSTANDING_STATUSES":
                sorted(protocol.OPERATION_OUTSTANDING_STATUSES),
            "OPERATION_TERMINAL_STATUSES":
                sorted(protocol.OPERATION_TERMINAL_STATUSES),
            "OPERATION_PERSIST_FIELDS": sorted(protocol.OPERATION_PERSIST_FIELDS),
            "OPERATION_EXPORT_FIELDS": sorted(protocol.OPERATION_EXPORT_FIELDS),
            "FORBIDDEN_PERSISTED_KEYS": sorted(protocol.FORBIDDEN_PERSISTED_KEYS),
            "OPERATION_DETAIL_CODES": list(protocol.OPERATION_DETAIL_CODES),
            "transitions": {
                key: sorted(value)
                for key, value in protocol.OPERATION_TRANSITIONS.items()
            },
        }))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(package_root.parent)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return json.loads(completed.stdout)


class JournalFixture:
    """A plan directory and an isolated state root for one journal."""

    addCleanup: Any

    def install_journal(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.plan_dir = root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.canonical = plans.canonicalize_path(self.plan_dir)
        self.journal_path = workbench_operations.journal_path_for_plan(
            self.canonical, self.state_dir
        )

    def admit(self, operation_id: str, **kwargs: Any) -> Any:
        parameters: dict[str, Any] = {
            "operation_id": operation_id,
            "method": operations.METHOD_LAUNCH,
            "state_dir": self.state_dir,
        }
        parameters.update(kwargs)
        return workbench_operations.admit_operation(self.canonical, **parameters)

    def rows(self) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            self.canonical, state_dir=self.state_dir
        )

    def row(self, operation_id: str) -> dict[str, Any]:
        record = workbench_operations.read_operation(
            self.canonical, operation_id, state_dir=self.state_dir
        )
        assert record is not None
        return record


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
                workbench_operations, "OPERATIONS_JOURNAL_MAX_BYTES", 2400
            ),
            mock.patch.object(workbench_operations, "OPERATIONS_RETAINED_SETTLED", 2),
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

    def test_export_drops_the_local_identifiers(self) -> None:
        self.admit("op-1")
        exported = workbench_operations.export_operations(
            self.canonical, state_dir=self.state_dir
        )
        self.assertEqual(set(operations.OPERATION_EXPORT_FIELDS), set(exported[0]))


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
        handle = state.acquire_lock(self.canonical, state_dir=self.state_dir)
        self.addCleanup(
            lambda: state.release_lock(self.canonical, state_dir=self.state_dir)
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
        state.acquire_lock(self.canonical, state_dir=self.state_dir)
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


_CHILD_ADMIT = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from agent_session_loop import workbench_operations
from asl_workbench import operations

plan_dir, state_dir, operation_id, start_at, mode = sys.argv[2:7]
while time.time() < float(start_at):
    time.sleep(0.001)
try:
    admission = workbench_operations.admit_operation(
        plan_dir,
        operation_id=operation_id,
        method=operations.METHOD_LAUNCH,
        state_dir=state_dir,
    )
except operations.OperationError as error:
    print(json.dumps({"decision": "error", "reason": error.reason}))
    raise SystemExit(0)
if admission.admitted and mode != "admit-only":
    workbench_operations.mark_dispatched(
        plan_dir, operation_id, state_dir=state_dir
    )
    if mode == "hang":
        time.sleep(600)
print(json.dumps({"decision": admission.decision, "reason": admission.reason}))
"""


class CrossProcessTests(JournalFixture, unittest.TestCase):
    """Two processes submitting at once produce one attempt, not two."""

    def setUp(self) -> None:
        self.install_journal()

    def _race(self, ids: list[str]) -> list[dict[str, Any]]:
        start_at = time.time() + 1.0
        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CHILD_ADMIT,
                    str(REPO_ROOT),
                    str(self.canonical),
                    str(self.state_dir),
                    identifier,
                    str(start_at),
                    "admit-only",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            for identifier in ids
        ]
        results: list[dict[str, Any]] = []
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            self.assertEqual(0, child.returncode, stderr)
            results.append(json.loads(stdout.strip()))
        return results

    def test_eight_processes_resending_one_id_admit_once(self) -> None:
        results = self._race(["op-same"] * 8)
        decisions = [result["decision"] for result in results]
        self.assertEqual(1, decisions.count(operations.DECISION_ADMIT))
        self.assertEqual(7, decisions.count(operations.DECISION_REPLAY))
        # One row, so one attempt: the resends found the first one's record
        # rather than starting anything.
        self.assertEqual(1, len(self.rows()))

    def test_eight_processes_with_different_ids_admit_once(self) -> None:
        results = self._race([f"op-{index}" for index in range(8)])
        decisions = [result["decision"] for result in results]
        self.assertEqual(1, decisions.count(operations.DECISION_ADMIT))
        self.assertEqual(7, decisions.count("refuse"))
        reasons = {result["reason"] for result in results if result["reason"]}
        self.assertEqual({operations.REASON_OPERATION_OUTSTANDING}, reasons)
        # One row, because the seven refusals were decided before anything was
        # written: a refused submission leaves no record to account for.
        self.assertEqual(1, len(self.rows()))

    def test_a_killed_child_leaves_an_accountable_record(self) -> None:
        start_at = time.time() + 0.2
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CHILD_ADMIT,
                str(REPO_ROOT),
                str(self.canonical),
                str(self.state_dir),
                "op-killed",
                str(start_at),
                "hang",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.addCleanup(child.kill)
        deadline = time.time() + 60
        while time.time() < deadline:
            record = workbench_operations.read_operation(
                self.canonical, "op-killed", state_dir=self.state_dir
            )
            if record is not None and record["status"] == (
                operations.OPERATION_DISPATCHED
            ):
                break
            time.sleep(0.05)
        else:  # pragma: no cover - only reached if the child never got there
            self.fail("the child never recorded dispatch")
        child.kill()
        child.wait(timeout=30)
        workbench_operations.reconcile_plan(self.canonical, state_dir=self.state_dir)
        # The owner is gone and the mutation was already dispatched: the honest
        # answer is that nobody can say, and the journal says exactly that.
        self.assertEqual(
            operations.OPERATION_UNCONFIRMED, self.row("op-killed")["status"]
        )


class LaunchRouteTests(WorkbenchAPIFixture, unittest.TestCase):
    """The two spawning routes admit through the journal."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.install_workbench_api(Path(self._tmp.name))
        self.plan_dir = self.plans_root / "alpha"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.canonical = plans.canonicalize_path(self.plan_dir)
        self.server = self.start_workbench_server()
        self.journal_path = workbench_operations.journal_path_for_plan(
            self.canonical, self.state_dir
        )

    def _sentinel(self, *, pid: int = 4242, reply: bool = False) -> Any:
        return workbench.LaunchResult(
            pid=pid,
            argv=("python3", "-m", "agent_session_loop", "run"),
            plan_dir=self.canonical,
            profile_payload=workbench.LaunchProfile().to_payload(),
            reply_provided=reply,
            reply_length=5 if reply else None,
            reply_origin="stdin" if reply else None,
            state_dir=self.state_dir,
        )

    def _post(self, route: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, payload, _ = _http_request(
            f"{self.base_url(self.server)}{route}",
            method="POST",
            body=body,
            headers=self.auth_headers(self.server),
        )
        return status, payload

    def _rows(self) -> list[dict[str, Any]]:
        return workbench_operations.read_operations(
            self.canonical, state_dir=self.state_dir
        )

    def _hold_the_plan(self) -> None:
        """Make a live run of this test process: a lock and the state it wrote."""
        _write_state_payload(
            self.canonical,
            self.state_dir,
            {
                "version": 3,
                "plan_dir": str(self.canonical),
                "paused": False,
                "harness": "claude",
                "run_id": "20260619T140000Z-1",
                "turn_index": 1,
                "run_started_at": "2026-06-19T14:00:00+00:00",
            },
        )
        _write_lock_file(
            self.canonical,
            self.state_dir,
            pid=os.getpid(),
            hostname=socket.gethostname(),
        )

    def test_a_launch_is_recorded_and_accounted_for(self) -> None:
        with mock.patch.object(workbench, "launch_plan", return_value=self._sentinel()):
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual(200, status)
        self.assertEqual(4242, body["launch"]["pid"])
        rows = self._rows()
        self.assertEqual(["launch-1"], [row["operation_id"] for row in rows])
        self.assertEqual(operations.OPERATION_CONSUMED, rows[0]["status"])
        self.assertEqual(operations.METHOD_LAUNCH, rows[0]["method"])
        self.assertIsNotNone(rows[0]["dispatched_at"])

    def test_a_launch_without_an_id_is_still_accounted_for(self) -> None:
        with mock.patch.object(workbench, "launch_plan", return_value=self._sentinel()):
            status, _ = self._post("/api/launch", {"plan_dir": str(self.canonical)})
        self.assertEqual(200, status)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(operations.OPERATION_CONSUMED, rows[0]["status"])

    def test_a_resent_launch_id_does_not_spawn_twice(self) -> None:
        with mock.patch.object(
            workbench, "launch_plan", return_value=self._sentinel()
        ) as patched:
            first_status, first = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
            second_status, second = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual((200, 200), (first_status, second_status))
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["launch"], second["launch"])
        patched.assert_called_once()
        self.assertEqual(1, len(self._rows()))

    def test_a_launch_id_reused_for_a_reply_is_refused(self) -> None:
        with mock.patch.object(workbench, "launch_plan", return_value=self._sentinel()):
            self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "shared-id"},
            )
        with mock.patch.object(
            workbench, "resume_plan_with_reply", return_value=self._sentinel(reply=True)
        ) as patched:
            status, body = self._post(
                "/api/reply",
                {
                    "plan_dir": str(self.canonical),
                    "launch_id": "shared-id",
                    "reply_text": "go-ok",
                },
            )
        self.assertEqual(409, status)
        self.assertEqual(operations.REASON_OPERATION_CONFLICT, body["reason"])
        self.assertEqual(operations.CONFLICT_METHOD, body["details"]["conflict"])
        patched.assert_not_called()

    def test_a_refused_launch_is_closed_out_rather_than_left_open(self) -> None:
        error = workbench.LaunchError(
            workbench.LAUNCH_REASON_INVALID_PLAN, "no such plan"
        )
        with mock.patch.object(workbench, "launch_plan", side_effect=error):
            status, _ = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual(400, status)
        rows = self._rows()
        self.assertEqual(operations.OPERATION_REJECTED, rows[0]["status"])
        self.assertEqual(operations.DETAIL_LAUNCH_REFUSED, rows[0]["detail_code"])
        # Closed out, so the next submission is not blocked by a mutation that
        # demonstrably did not happen.
        self.assertEqual(
            [],
            workbench_operations.open_operations(
                self.canonical, state_dir=self.state_dir
            ),
        )

    def test_the_reply_text_never_reaches_the_journal(self) -> None:
        secret = "the-operator-typed-this"
        with mock.patch.object(
            workbench, "resume_plan_with_reply", return_value=self._sentinel(reply=True)
        ):
            status, _ = self._post(
                "/api/reply",
                {
                    "plan_dir": str(self.canonical),
                    "launch_id": "reply-1",
                    "reply_text": secret,
                },
            )
        self.assertEqual(200, status)
        raw = self.journal_path.read_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertIn(b"reply-1", raw)
        for forbidden in operations.FORBIDDEN_PERSISTED_KEYS:
            with self.subTest(field=forbidden):
                self.assertNotIn(f'"{forbidden}"'.encode(), raw)

    def test_the_plan_lock_still_refuses_a_live_run(self) -> None:
        # Admission is a gate in front of the lock, not a replacement: a plan a
        # run already holds is refused by the ownership check, with the reason
        # operators already know.
        self._hold_the_plan()
        with mock.patch.object(
            workbench, "launch_plan", return_value=self._sentinel()
        ) as patched:
            status, body = self._post(
                "/api/launch",
                {"plan_dir": str(self.canonical), "launch_id": "launch-1"},
            )
        self.assertEqual(409, status)
        self.assertEqual("run-already-active", body["reason"])
        patched.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
