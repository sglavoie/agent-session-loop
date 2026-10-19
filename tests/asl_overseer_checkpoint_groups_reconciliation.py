"""Focused portion of tests/test_asl_overseer_checkpoint_groups.py."""

from .asl_overseer_checkpoint_groups_recovery import *


class Reconciliation(_GroupCase):
    """What a dead process did is answered from the repositories themselves."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_a_finished_group_reconciles_as_executed(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        record = self.attempted(action)
        receipt = self.adapter(ledger=self.open_ledger()).reconcile(record)
        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(receipt.locator, self.key(action))

    def test_a_group_nobody_started_reconciles_as_absent(self) -> None:
        action = self.action()
        record = self.attempted(action)
        receipt = self.adapter().reconcile(record)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("commit_absent", receipt.detail)

    def test_a_partial_group_names_what_landed_and_what_did_not(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        (self.repos["beta"] / "work.txt").write_text(
            "somebody else is still editing\n", encoding="utf-8"
        )
        adapter.execute(action)
        receipt = self.adapter().reconcile(self.attempted(action))
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("committed alpha", receipt.detail)
        self.assertIn("outstanding", receipt.detail)
        self.assertIn("beta", receipt.detail)

    def test_a_repository_that_cannot_be_read_is_uncertain_not_absent(self) -> None:
        action = self.action()
        self.adapter().prepare(action)
        shutil.rmtree(self.repos["gamma"])
        receipt = self.adapter().reconcile(self.attempted(action))
        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("repository_unreadable", receipt.detail)

    def test_a_settled_group_is_answered_from_its_own_record(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        # First-hand evidence outlives the repository it was made in: the group
        # recorded the commits it made, so a repository nobody can open any more
        # cannot turn a finished checkpoint back into an open question.
        shutil.rmtree(self.repos["gamma"])
        receipt = self.adapter().reconcile(self.attempted(action))
        self.assertEqual(receipt.outcome, "executed")

    def test_reconciling_another_operation_is_uncertain(self) -> None:
        action = self.action()
        record = self.attempted(action)
        adapter = self.adapter()
        receipt = adapter.reconcile(
            overseer_store_models.ActionRecord(
                **{**record.__dict__, "operation": "resume"},
            )
        )
        self.assertEqual(receipt.outcome, "uncertain")
        self.assertIn("payload_mismatch", receipt.detail)

    def test_reconciliation_writes_nothing_into_the_durable_group(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        before = self.group(action)
        assert before is not None
        self.adapter().reconcile(self.attempted(action))
        after = self.group(action)
        assert after is not None
        self.assertEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.phase, before.phase)


class PayloadAndMessage(_GroupCase):
    """The two things every checkpoint needs before any of this can start."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_a_payload_that_is_not_a_commit_is_refused(self) -> None:
        action = overseer_dispatch.Action(
            plan_dir=self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=overseer_dispatch.Resume(),
            token="token-1",
            scope="sha256:scope",
            repositories=self.repositories,
        )
        adapter = self.adapter()
        self.assertIn("payload_mismatch", adapter.prepare(action).detail)
        self.assertIn("payload_mismatch", adapter.execute(action).detail)

    def test_a_message_of_whitespace_is_not_a_message(self) -> None:
        declined = self.adapter().prepare(self.action(message="   \n"))
        self.assertFalse(declined.ready)
        self.assertIn("message_empty", declined.detail)


if __name__ == "__main__":
    unittest.main()

__all__ = ["contextlib", "json", "shutil", "sqlite3", "tempfile", "unittest", "datetime", "Path", "Any", "get_args", "commit_groups", "overseer_checkpoint", "overseer_checkpoint_groups", "overseer_decisions", "overseer_dispatch", "overseer_policy_client", "overseer_store_ledger", "overseer_store_models", "state", "support", "assert_no_runner_import", "PROJECT", "RUN", "ALIASES", "FINGERPRINT", "DIGEST", "MOMENT", "MESSAGE", "FULL_RUN_BOUNDARIES", "_Crash", "_GroupCase", "ClosedVocabulary", "DerivedIdentity", "CommitsTheWholeRegistry", "MixedPermission", "PartialStaging", "AFailedSecondCommit", "RevocationBetweenCommits", "RestartAtEachBoundary", "Reconciliation", "PayloadAndMessage"]  # fmt: skip
