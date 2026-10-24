"""Partial-write and revocation tests for the overseer ledger."""

import sqlite3
import unittest

from agent_session_loop import (
    overseer_policy_client,
    overseer_store_models,
)
from tests.asl_overseer_store_support import (
    FINGERPRINT,
    NEXT_FINGERPRINT,
    OTHER_PLAN,
    PLAN,
    _decision,
    _evidence,
    _FlakyConnection,
    _grant,
    _LedgerCase,
)

ASL_TEST_PORTABLE = True


class PartialWriteTests(_LedgerCase):
    """A write that fails partway leaves nothing behind."""

    def _make_flaky(self, *, fail_on: str) -> _FlakyConnection:
        real = self.ledger._connection
        flaky = _FlakyConnection(real, fail_on=fail_on)
        self.ledger._connection = flaky
        self.addCleanup(setattr, self.ledger, "_connection", real)
        return flaky

    def test_an_incident_whose_evidence_fails_is_not_written_at_all(self) -> None:
        grant = self.enrolled()
        flaky = self._make_flaky(fail_on="INSERT INTO evidence")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.open_incident(
                grant=grant,
                plan_dir=PLAN,
                run_id="run-1",
                kind="verify_failed",
                summary="two checks failed",
                evidence=_evidence(2),
            )
        self.assertTrue(flaky.failed)
        self.ledger._connection = flaky._connection

        self.assertEqual(self.ledger.incidents(plan_dir=PLAN), ())
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(view.records, ())
        self.assertEqual(view.withheld, 0)

        # The rollback also has to leave the connection usable, or the next
        # write would fail on an transaction nobody closed.
        recorded = self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="two checks failed",
            evidence=_evidence(2),
        )
        self.assertEqual(recorded.status, "open")
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 2
        )

    def test_a_receipt_that_fails_leaves_the_action_attempted(self) -> None:
        """The status change and the receipt are one transaction, so there is
        no state in which an action reads executed with nothing backing it."""
        self.enrolled()
        grant = _grant(access=overseer_policy_client.ACCESS_ACTION, digest="rd-act")
        action = self.ledger.attempt_action(
            grant=grant, plan_dir=PLAN, operation="reply", target="question-4"
        )
        flaky = self._make_flaky(fail_on="INSERT INTO receipt")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.confirm_action(action_id=action.action_id, outcome="delivered")
        self.ledger._connection = flaky._connection

        still = self.ledger.action(action.action_id)
        assert still is not None
        self.assertEqual(still.status, "attempted")
        self.assertTrue(still.in_doubt)
        self.assertIsNone(still.receipt)
        self.assertEqual(
            [item.action_id for item in self.reopen().in_doubt_actions()],
            [action.action_id],
        )

    def test_a_failed_policy_bump_leaves_the_epoch_and_the_seals_together(self) -> None:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        flaky = self._make_flaky(fail_on="INSERT INTO policy_epoch")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        self.ledger._connection = flaky._connection

        epoch = self.ledger.current_epoch()
        self.assertEqual(epoch.config_fingerprint, FINGERPRINT)
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(len(view.records), 1)
        self.assertEqual(view.withheld, 0)

    def test_a_failed_commit_rolls_back_and_frees_the_connection(self) -> None:
        """A COMMIT that fails must not leave the transaction open: every later
        ``BEGIN IMMEDIATE`` on the connection would then refuse."""
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        flaky = self._make_flaky(fail_on="COMMIT")
        with self.assertRaises(sqlite3.OperationalError):
            self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)

        self.assertFalse(flaky._connection.in_transaction)
        self.assertEqual(self.ledger.current_epoch().config_fingerprint, FINGERPRINT)
        self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        self.assertEqual(
            self.ledger.current_epoch().config_fingerprint, NEXT_FINGERPRINT
        )

    def test_a_rollback_that_cannot_run_does_not_mask_the_failure(self) -> None:
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        real = self.ledger._connection

        class _NoRollback(_FlakyConnection):
            def execute(self, sql, parameters=()):
                if sql.strip() == "ROLLBACK":
                    self._connection.execute(sql, parameters)
                    raise sqlite3.OperationalError("no transaction is active")
                return super().execute(sql, parameters)

        self.ledger._connection = _NoRollback(real, fail_on="INSERT INTO policy_epoch")
        self.addCleanup(setattr, self.ledger, "_connection", real)
        with self.assertRaises(sqlite3.OperationalError) as raised:
            self.ledger.note_policy(config_fingerprint=NEXT_FINGERPRINT)
        self.assertEqual("disk I/O error", str(raised.exception))


class RevocationTests(_LedgerCase):
    """A withdrawn plan yields nothing, and keeps yielding nothing."""

    def _stocked(self) -> overseer_policy_client.Grant:
        grant = self.enrolled()
        self.ledger.open_incident(
            grant=grant,
            plan_dir=PLAN,
            run_id="run-1",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(2),
        )
        return grant

    def test_an_explicit_revocation_withholds_evidence_through_a_restart(self) -> None:
        grant = self._stocked()
        sealed = self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")
        self.assertEqual(sealed, 2)

        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(view.refusal, "enrollment_revoked")
        self.assertEqual(view.records, ())
        self.assertEqual(view.withheld, 2)

        release = self.ledger.release_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(release.refusal, "enrollment_revoked")
        self.assertEqual(release.released, 0)

        reopened = self.reopen()
        after = reopened.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(after.refusal, "enrollment_revoked")
        self.assertEqual(after.records, ())
        record = reopened.enrollment(PLAN)
        assert record is not None
        self.assertTrue(record.revoked)
        self.assertFalse(record.enrolled)

    def test_an_answered_refusal_revokes_and_withholds(self) -> None:
        """AgentMux saying no is the ordinary revocation path, and it has to
        reach stored evidence rather than only the next read of the source."""
        grant = self._stocked()
        self.ledger.record_enrollment(_decision(allowed=False, digest="rd-no"))
        view = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(view.refusal, "enrollment_revoked")
        with self.assertRaises(overseer_store_models.LedgerRefused):
            self.ledger.attempt_action(
                grant=_grant(access=overseer_policy_client.ACCESS_ACTION),
                plan_dir=PLAN,
                operation="reply",
                target="q",
            )

    def test_revocation_does_not_reach_another_plan(self) -> None:
        first = self._stocked()
        second = self.enrolled(plan_dir=OTHER_PLAN)
        self.ledger.open_incident(
            grant=second,
            plan_dir=OTHER_PLAN,
            run_id="run-2",
            kind="verify_failed",
            summary="failed",
            evidence=_evidence(1),
        )
        self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")

        self.assertEqual(
            self.ledger.read_evidence(plan_dir=PLAN, grant=first).refusal,
            "enrollment_revoked",
        )
        other = self.ledger.read_evidence(plan_dir=OTHER_PLAN, grant=second)
        self.assertIsNone(other.refusal)
        self.assertEqual(len(other.records), 1)

    def test_re_enrolment_restores_the_plan_but_not_its_sealed_evidence(self) -> None:
        """Coming back is one live allowance; reading what was gathered before
        the withdrawal is a second, explicit one."""
        grant = self._stocked()
        self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")
        self.ledger.record_enrollment(_decision(digest="rd-again"))

        record = self.ledger.enrollment(PLAN)
        assert record is not None
        self.assertTrue(record.enrolled)
        self.assertFalse(record.revoked)

        held = self.ledger.read_evidence(plan_dir=PLAN, grant=grant)
        self.assertIsNone(held.refusal)
        self.assertEqual(held.records, ())
        self.assertEqual(held.withheld, 2)

        release = self.ledger.release_evidence(plan_dir=PLAN, grant=grant)
        self.assertEqual(release.released, 2)
        self.assertEqual(
            len(self.ledger.read_evidence(plan_dir=PLAN, grant=grant).records), 2
        )

    def test_a_revoked_plan_keeps_its_operational_record(self) -> None:
        """Incidents and receipts say what the overseer did, not what the
        project contains, so revocation seals the evidence and not the log."""
        self._stocked()
        self.ledger.revoke(plan_dir=PLAN, reason="operator withdrew the plan")
        incidents = self.ledger.incidents(plan_dir=PLAN)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].status, "open")
        settled = self.ledger.settle_incident(
            incident_id=incidents[0].incident_id, status="escalated"
        )
        self.assertEqual(settled.status, "escalated")
        self.assertIsNotNone(settled.settled_at)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
