"""Focused portion of tests/test_asl_overseer_checkpoint_groups.py."""

from .asl_overseer_checkpoint_groups_support import *


class MixedPermission(_GroupCase):
    """A registry AgentMux permits only part of is refused as a whole."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_one_excluded_repository_refuses_the_whole_group(self) -> None:
        adapter = self.adapter(decisions=(self.decision(refused=("beta",)),))
        declined = adapter.prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("repository_excluded", declined.detail)
        self.assertIn("beta", declined.detail)

    def test_a_mixed_answer_stages_and_commits_nothing_anywhere(self) -> None:
        action = self.action()
        adapter = self.adapter(decisions=(self.decision(refused=("beta",)),))
        adapter.prepare(action)
        for alias in ALIASES:
            self.assertEqual(self.staged(alias), [], msg=alias)
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])
        self.assertIsNone(self.group(action))

    def test_the_permitted_repositories_are_left_exactly_as_they_were(self) -> None:
        heads = {alias: self.head(alias) for alias in ALIASES}
        adapter = self.adapter(decisions=(self.decision(refused=("gamma",)),))
        adapter.prepare(self.action())
        for alias in ALIASES:
            self.assertEqual(self.head(alias), heads[alias], msg=alias)
            self.assertEqual(self.porcelain(alias), ["?? work.txt"], msg=alias)

    def test_a_policy_that_cannot_be_asked_is_not_a_permission(self) -> None:
        adapter = self.adapter(decisions=(self.decision(available=False),))
        declined = adapter.prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("policy_unavailable", declined.detail)

    def test_the_whole_registry_is_the_question_that_gets_asked(self) -> None:
        adapter = self.adapter()
        adapter.prepare(self.action())
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(
            tuple(repository.alias for repository in self.asked[0]["repositories"]),
            ALIASES,
        )
        self.assertEqual(self.asked[0]["access"], overseer_policy_client.ACCESS_ACTION)


class PartialStaging(_GroupCase):
    """Staging stops partway, and says so rather than hiding what it did."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_a_repository_that_cannot_be_staged_refuses_the_group(self) -> None:
        self.lock_index("beta")
        declined = self.adapter().prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("staging_failed", declined.detail)
        self.assertIn("beta", declined.detail)

    def test_the_refusal_names_the_repositories_already_staged(self) -> None:
        self.lock_index("beta")
        declined = self.adapter().prepare(self.action())
        self.assertIn("already staged alpha", declined.detail)
        self.assertEqual(self.staged("alpha"), ["work.txt"])

    def test_a_partly_staged_registry_opens_no_durable_group(self) -> None:
        self.lock_index("beta")
        action = self.action()
        self.adapter().prepare(action)
        self.assertIsNone(self.group(action))

    def test_nothing_is_committed_after_staging_stops(self) -> None:
        self.lock_index("beta")
        action = self.action()
        heads = {alias: self.head(alias) for alias in ALIASES}
        self.adapter().prepare(action)
        for alias in ALIASES:
            self.assertEqual(self.head(alias), heads[alias], msg=alias)
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])

    def test_a_repository_after_the_failure_is_never_touched(self) -> None:
        self.lock_index("beta")
        self.adapter().prepare(self.action())
        self.assertEqual(self.staged("gamma"), [])

    def test_execution_without_a_preparation_refuses(self) -> None:
        self.lock_index("beta")
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("not_prepared", receipt.detail)


class AFailedSecondCommit(_GroupCase):
    """The first commit stands, the failure is reported, nothing is undone."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()
        self.action_under_test = self.action()
        self.adapter_under_test = self.adapter()
        self.assertTrue(self.adapter_under_test.prepare(self.action_under_test).ready)
        # Move beta out from under what preparation measured, which is the
        # ordinary way a middle repository refuses: somebody is still editing an
        # owned file while the group is being made.
        (self.repos["beta"] / "work.txt").write_text(
            "somebody else is still editing\n", encoding="utf-8"
        )
        self.receipt = self.adapter_under_test.execute(self.action_under_test)

    def test_the_group_reports_a_failure_rather_than_an_execution(self) -> None:
        self.assertEqual(self.receipt.outcome, "failed")
        self.assertIn("worktree_drifted", self.receipt.detail)
        self.assertIn("beta", self.receipt.detail)

    def test_the_first_repository_keeps_the_commit_it_earned(self) -> None:
        self.assertEqual(
            len(self.carrying("alpha", self.identity(self.action_under_test, "alpha"))),
            1,
        )
        self.assertEqual(self.porcelain("alpha"), [])

    def test_the_receipt_reports_the_partial_result(self) -> None:
        self.assertIn("committed alpha", self.receipt.detail)
        self.assertIn("outstanding", self.receipt.detail)
        self.assertIn("gamma", self.receipt.detail)

    def test_the_repository_after_the_failure_is_neither_committed_nor_cleaned(
        self,
    ) -> None:
        self.assertEqual(
            self.carrying("gamma", self.identity(self.action_under_test, "gamma")), []
        )
        self.assertEqual(self.porcelain("gamma"), ["A  work.txt"])

    def test_the_durable_group_keeps_the_success_and_names_what_is_owed(self) -> None:
        stored = self.group(self.action_under_test)
        assert stored is not None
        self.assertFalse(stored.settled)
        self.assertEqual(stored.phase, "repository-failed")
        self.assertEqual(stored.committed, ("alpha",))
        self.assertEqual(stored.outstanding, ("beta", "gamma"))

    def test_the_failed_repository_is_named_in_the_durable_group(self) -> None:
        stored = self.group(self.action_under_test)
        assert stored is not None
        self.assertEqual(stored.record["failed_alias"], "beta")

    def test_a_retry_finishes_the_group_without_committing_alpha_again(self) -> None:
        # Re-attribute beta from where it now is, which is what a supervisor
        # would do after reconciling the repository somebody else moved.
        self.publish()
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed", msg=receipt.detail)
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))), 1, msg=alias
            )


__all__ = ["contextlib", "json", "shutil", "sqlite3", "tempfile", "unittest", "datetime", "Path", "Any", "get_args", "commit_groups", "overseer_checkpoint", "overseer_checkpoint_groups", "overseer_decisions", "overseer_dispatch", "overseer_policy_client", "overseer_store_ledger", "overseer_store_models", "state", "support", "assert_no_runner_import", "PROJECT", "RUN", "ALIASES", "FINGERPRINT", "DIGEST", "MOMENT", "MESSAGE", "FULL_RUN_BOUNDARIES", "_Crash", "_GroupCase", "MixedPermission", "PartialStaging", "AFailedSecondCommit"]  # fmt: skip
