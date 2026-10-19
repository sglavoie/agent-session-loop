"""Focused portion of tests/test_asl_overseer_checkpoint_groups.py."""

from .asl_overseer_checkpoint_groups_support import *


class ClosedVocabulary(_GroupCase):
    """The refusal words and the phases are a contract, not a set of strings."""

    def test_refusals_tuple_matches_the_literal_type(self) -> None:
        self.assertEqual(
            set(overseer_checkpoint_groups.REFUSALS),
            set(get_args(overseer_checkpoint_groups.Refusal)),
        )
        self.assertEqual(
            len(overseer_checkpoint_groups.REFUSALS),
            len(set(overseer_checkpoint_groups.REFUSALS)),
        )

    def test_every_single_repository_refusal_is_sayable_here_too(self) -> None:
        self.assertLessEqual(
            set(overseer_checkpoint.REFUSALS),
            set(overseer_checkpoint_groups.REFUSALS),
        )

    def test_the_registry_holds_the_commit_operation_only(self) -> None:
        registry = overseer_checkpoint_groups.adapters(
            authority=lambda _action: self.ruling(),
            ledger=self.ledger,
            state_dir=self.root,
        )
        self.assertTrue(registry.supports("commit"))
        for operation in ("reply", "guidance", "verify", "resume"):
            self.assertFalse(registry.supports(operation), msg=operation)

    def test_groups_are_written_under_a_scope_commit_groups_knows(self) -> None:
        self.assertIn(
            overseer_checkpoint_groups.GROUP_SCOPE, commit_groups.GROUP_SCOPES
        )
        self.assertNotIn(
            overseer_checkpoint_groups.GROUP_SCOPE, commit_groups.SESSION_GROUP_SCOPES
        )

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_checkpoint_groups)


class DerivedIdentity(_GroupCase):
    """Group and repository identities are functions of the row, not of a run."""

    def test_the_group_key_is_the_same_for_the_same_three_fields(self) -> None:
        base = {"plan_dir": self.plan, "incident_id": self.incident_id, "target": "t-1"}
        self.assertEqual(
            overseer_checkpoint_groups.group_key(**base),
            overseer_checkpoint_groups.group_key(**base),
        )

    def test_the_group_key_moves_with_every_field_it_is_derived_from(self) -> None:
        base: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "target": "t-1",
        }
        original = overseer_checkpoint_groups.group_key(**base)
        for name, value in (
            ("plan_dir", self.new_plan("plan-other")),
            ("incident_id", self.incident_id + 1),
            ("target", "t-2"),
        ):
            moved = dict(base)
            moved[name] = value
            self.assertNotEqual(
                original, overseer_checkpoint_groups.group_key(**moved), msg=name
            )

    def test_each_repository_gets_an_identity_of_its_own(self) -> None:
        action = self.action()
        identities = {alias: self.identity(action, alias) for alias in ALIASES}
        self.assertEqual(len(set(identities.values())), len(ALIASES))

    def test_a_group_commit_is_never_found_by_a_lone_checkpoint_id(self) -> None:
        action = self.action()
        lone = overseer_checkpoint.checkpoint_id(
            plan_dir=action.plan_dir,
            incident_id=action.incident_id,
            target=action.target,
        )
        for alias in ALIASES:
            self.assertNotEqual(lone, self.identity(action, alias), msg=alias)


class CommitsTheWholeRegistry(_GroupCase):
    """The ordinary case: three attributed repositories, one group, one pass."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def test_every_repository_is_committed_once_under_its_own_identity(self) -> None:
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed")
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))), 1, msg=alias
            )
            self.assertEqual(self.porcelain(alias), [], msg=alias)

    def test_the_receipt_locates_the_durable_group(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.locator, self.key(action))

    def test_the_commit_says_what_the_action_asked_and_carries_the_trailer(
        self,
    ) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        body = self.body("alpha")
        self.assertIn(MESSAGE, body)
        self.assertIn(
            f"{overseer_checkpoint.CHECKPOINT_TRAILER}: "
            f"{self.identity(action, 'alpha')}",
            body,
        )

    def test_the_durable_group_is_settled_and_owes_nothing(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        stored = self.group(action)
        assert stored is not None
        self.assertTrue(stored.settled)
        self.assertEqual(stored.phase, "complete")
        self.assertEqual(stored.outstanding, ())
        self.assertEqual(set(stored.committed), set(ALIASES))

    def test_preparation_opens_the_group_before_anything_is_committed(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        stored = self.group(action)
        assert stored is not None
        self.assertEqual(stored.phase, "prepared")
        self.assertFalse(stored.settled)
        self.assertEqual(stored.outstanding, ALIASES)
        self.assertEqual(stored.committed, ())
        for alias in ALIASES:
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])

    def test_the_recorded_phase_is_always_one_the_module_publishes(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        stored = self.group(action)
        assert stored is not None
        self.assertIn(stored.phase, overseer_checkpoint_groups.GROUP_PHASES)

    def test_the_receipt_reports_the_verification_the_run_recorded(self) -> None:
        self.publish(verification={"passed": False, "session": "1", "failures": ["x"]})
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        receipt = adapter.execute(action)
        self.assertIn("verification=failed", receipt.detail)

    def test_a_clean_repository_is_skipped_rather_than_committed(self) -> None:
        self.changes.clear()
        self.turn_index = 0
        shutil.rmtree(self.run_dir(), ignore_errors=True)
        (self.repos["gamma"] / "work.txt").unlink()
        self.prime("alpha", "beta")
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "executed")
        self.assertEqual(self.carrying("gamma", self.identity(action, "gamma")), [])
        stored = self.group(action)
        assert stored is not None
        self.assertEqual(set(stored.committed), {"alpha", "beta"})

    def test_a_registry_with_nothing_dirty_is_refused_before_staging(self) -> None:
        for alias in ALIASES:
            (self.repos[alias] / "work.txt").unlink()
        declined = self.adapter().prepare(self.action())
        self.assertFalse(declined.ready)
        self.assertIn("nothing_to_commit", declined.detail)
        self.assertIsNone(self.group(self.action()))

    def test_an_action_naming_no_repository_is_refused(self) -> None:
        declined = self.adapter().prepare(self.action(repositories=()))
        self.assertFalse(declined.ready)
        self.assertIn("registry_empty", declined.detail)


__all__ = ["contextlib", "json", "shutil", "sqlite3", "tempfile", "unittest", "datetime", "Path", "Any", "get_args", "commit_groups", "overseer_checkpoint", "overseer_checkpoint_groups", "overseer_decisions", "overseer_dispatch", "overseer_policy_client", "overseer_store_ledger", "overseer_store_models", "state", "support", "assert_no_runner_import", "PROJECT", "RUN", "ALIASES", "FINGERPRINT", "DIGEST", "MOMENT", "MESSAGE", "FULL_RUN_BOUNDARIES", "_Crash", "_GroupCase", "ClosedVocabulary", "DerivedIdentity", "CommitsTheWholeRegistry"]  # fmt: skip
