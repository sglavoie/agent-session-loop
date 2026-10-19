"""Operation vocabulary, identity, admission, and privacy contracts."""

from __future__ import annotations

import json
import unittest
from typing import Any

from asl_workbench import operations

from .support_workbench_operations import _record, _target


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


if __name__ == "__main__":
    unittest.main()
