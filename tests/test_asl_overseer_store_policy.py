"""Unavailable-policy and policy-change tests for the overseer ledger."""

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
)
from tests.asl_overseer_store_support import (
    FINGERPRINT,
    NEXT_FINGERPRINT,
    OTHER_PLAN,
    PLAN,
    _decision,
    _evidence,
    _grant,
    _LedgerCase,
)

ASL_TEST_PORTABLE = True


class UnavailablePolicyTests(_LedgerCase):
    """A decision that could not be obtained confers nothing."""

    def test_a_stored_decision_cannot_become_permission(self) -> None:
        """The substitution the shared requirements forbid is unrepresentable
        rather than merely discouraged: the stored form has no conversion."""
        self.enrolled()
        record = self.ledger.record_decision(_decision())
        self.assertTrue(record.allowed)
        self.assertFalse(hasattr(record, "grant"))
        self.assertFalse(hasattr(record, "authorization"))
        self.assertFalse(hasattr(record, "permits"))
        self.assertNotIsInstance(record, overseer_policy_client.Grant)

    def test_an_unavailable_answer_does_not_move_the_enrollment_bit(self) -> None:
        self.enrolled()
        before = self.ledger.enrollment(PLAN)
        assert before is not None
        self.assertTrue(before.enrolled)

        after = self.ledger.record_enrollment(
            _decision(available=False, failure="client_timeout")
        )
        self.assertTrue(after.enrolled)
        self.assertFalse(after.revoked)
        self.assertEqual(after.last_failure, "client_timeout")
        self.assertEqual(after.config_fingerprint, before.config_fingerprint)
        self.assertEqual(after.first_seen_at, before.first_seen_at)

    def test_an_unavailable_answer_never_enrolls_an_unknown_plan(self) -> None:
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        record = self.ledger.record_enrollment(
            _decision(plan_dir=OTHER_PLAN, available=False, failure="client_missing")
        )
        self.assertFalse(record.enrolled)
        self.assertFalse(record.revoked)
        self.assertEqual(record.last_failure, "client_missing")

    def test_a_failed_decision_is_recorded_as_the_fault_it_was(self) -> None:
        self.enrolled()
        record = self.ledger.record_decision(
            _decision(available=False, failure="client_timeout")
        )
        self.assertFalse(record.allowed)
        self.assertFalse(record.available)
        self.assertEqual(record.failure, "client_timeout")
        self.assertEqual(record.request_digest, "")
        self.assertEqual(record.config_fingerprint, "")

    def test_no_grant_means_no_evidence_and_no_dispatch(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=None)
        self.assertEqual(view.refusal, "unauthorized")
        self.assertEqual(view.records, ())
        self.assertEqual(view.withheld, 1)
        self.assertFalse(view.complete)
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.attempt_action(
                grant=None, plan_dir=PLAN, operation="reply", target="q"
            )
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.open_incident(
                grant=None,
                plan_dir=PLAN,
                run_id="run-1",
                kind="verify_failed",
                summary="failed",
                evidence=_evidence(1),
            )

    def test_a_grant_for_another_plan_or_category_reads_nothing(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        for label, wrong in {
            "another plan": _grant(plan_dir=OTHER_PLAN),
            "another category": _grant(access=overseer_policy_client.ACCESS_ACTION),
        }.items():
            with self.subTest(label):
                view = self.ledger.read_evidence(plan_dir=PLAN, grant=wrong)
                self.assertEqual(view.refusal, "unauthorized")
                self.assertEqual(view.records, ())

    def test_evidence_is_unreadable_before_any_policy_is_observed(self) -> None:
        """Not knowing which configuration applies is never read as any
        configuration applying."""
        fresh = Path(tempfile.mkdtemp(prefix="asl-overseer-store-"))
        ledger = overseer_store_ledger.open_ledger(state_dir=fresh)
        self.addCleanup(ledger.close)
        self.assertFalse(ledger.current_epoch().observed)
        view = ledger.read_evidence(plan_dir=PLAN, grant=_grant())
        self.assertEqual(view.refusal, "policy_unobserved")
        with self.assertRaises(overseer_store_models.LedgerRefused):
            ledger.attempt_action(
                grant=_grant(access=overseer_policy_client.ACCESS_ACTION),
                plan_dir=PLAN,
                operation="reply",
                target="q",
            )


class PolicyChangeTests(_LedgerCase):
    """A configuration edit seals what was gathered under the old one."""

    def test_a_changed_fingerprint_seals_evidence_until_re_authorized(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(2),
        )
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 2
        )

        transition = self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        self.assertTrue(transition.changed)
        self.assertEqual(transition.sealed_evidence, 2)
        self.assertEqual(transition.previous_fingerprint, FINGERPRINT)

        stale = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(stale.refusal, "policy_superseded")
        self.assertEqual(stale.records, ())
        self.assertEqual(stale.withheld, 2)

        fresh = _grant(fingerprint=NEXT_FINGERPRINT, digest="rd-2")
        held = self.ledger.read_evidence(plan_dir=PLAN, grant=fresh)
        self.assertIsNone(held.refusal)
        self.assertEqual(held.records, ())
        self.assertEqual(held.withheld, 2)
        self.assertFalse(held.complete)

        release = self.ledger.release_evidence(plan_dir=PLAN, grant=fresh)
        self.assertEqual(release.released, 2)
        released = self.ledger.read_evidence(plan_dir=PLAN, grant=fresh)
        self.assertEqual(len(released.records), 2)
        self.assertTrue(released.complete)

    def test_observing_the_same_fingerprint_seals_nothing(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        transition = self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.assertFalse(transition.changed)
        self.assertEqual(transition.sealed_evidence, 0)
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 1
        )

    def test_a_superseded_grant_cannot_dispatch(self) -> None:
        """The recheck before dispatch is meaningless if a decision made under
        a configuration that has since changed still opens the door."""
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.attempt_action(
                grant=grant, plan_dir=PLAN, operation="reply", target="q"
            )
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())


if __name__ == "__main__":
    unittest.main()
