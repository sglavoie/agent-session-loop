"""Focused portion of tests/test_asl_overseer_checkpoint_groups.py."""

from .asl_overseer_checkpoint_groups_support import *


class RevocationBetweenCommits(_GroupCase):
    """Permission is asked again per commit, and its withdrawal costs no record."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def _revoked(self) -> tuple[overseer_dispatch.Action, overseer_dispatch.Receipt]:
        """Prepare under a full grant, then withdraw beta before its commit."""
        action = self.action()
        adapter = self.adapter(
            decisions=(
                self.decision(),
                self.decision(),
                self.decision(refused=("beta",)),
            )
        )
        self.assertTrue(adapter.prepare(action).ready)
        return action, adapter.execute(action)

    def test_the_withdrawn_repository_is_not_committed(self) -> None:
        action, receipt = self._revoked()
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("repository_excluded", receipt.detail)
        self.assertEqual(self.carrying("beta", self.identity(action, "beta")), [])

    def test_the_commit_made_before_the_revocation_is_not_rolled_back(self) -> None:
        action, _ = self._revoked()
        self.assertEqual(len(self.carrying("alpha", self.identity(action, "alpha"))), 1)

    def test_the_ledger_still_holds_the_evidence_of_that_commit(self) -> None:
        action, _ = self._revoked()
        stored = self.group(action)
        assert stored is not None
        self.assertEqual(stored.committed, ("alpha",))
        self.assertEqual(stored.outstanding, ("beta", "gamma"))
        self.assertEqual(stored.phase, "repository-failed")

    def test_permission_is_asked_once_per_commit_and_once_to_prepare(self) -> None:
        self._revoked()
        self.assertEqual(len(self.asked), 3)

    def test_a_policy_that_moves_mid_group_stops_the_next_commit(self) -> None:
        action = self.action()
        adapter = self.adapter(
            decisions=(
                self.decision(),
                self.decision(),
                self.decision(fingerprint="fp-second"),
            )
        )
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("policy_superseded", receipt.detail)
        self.assertEqual(len(self.carrying("alpha", self.identity(action, "alpha"))), 1)
        self.assertEqual(self.carrying("gamma", self.identity(action, "gamma")), [])

    def test_a_decision_replaced_between_preparation_and_execution_refuses(
        self,
    ) -> None:
        action = self.action()
        adapter = self.adapter(
            rulings=(self.ruling(), self.ruling(decision_id="d-2")),
        )
        self.assertTrue(adapter.prepare(action).ready)
        receipt = adapter.execute(action)
        self.assertEqual(receipt.outcome, "failed")
        self.assertIn("authority_superseded", receipt.detail)
        for alias in ALIASES:
            self.assertEqual(self.carrying(alias, self.identity(action, alias)), [])

    def test_an_authority_that_refuses_never_prepares(self) -> None:
        for name, kwargs in (
            ("authority_absent", {"with_decision": False}),
            ("authority_refused", {"authorized": False}),
            ("authority_held", {"stance": "deny"}),
        ):
            with self.subTest(name):
                adapter = self.adapter(rulings=(self.ruling(**kwargs),))
                declined = adapter.prepare(self.action())
                self.assertFalse(declined.ready)
                self.assertIn(name, declined.detail)


class RestartAtEachBoundary(_GroupCase):
    """A crash at every persistence point is recovered without duplication."""

    def setUp(self) -> None:
        super().setUp()
        self.prime()

    def _crash_after(self, boundaries: int) -> None:
        """Let the group persist ``boundaries`` times, then kill the process."""
        real = self.ledger.save_checkpoint_group
        seen = 0

        def guarded(**kwargs: Any) -> Any:
            nonlocal seen
            seen += 1
            if seen > boundaries:
                raise _Crash(str(kwargs.get("phase")))
            return real(**kwargs)

        self.ledger.save_checkpoint_group = guarded  # type: ignore[method-assign]

    def _restart_after(self, boundaries: int) -> None:
        """Crash at one boundary, then finish the group in a fresh process."""
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        self._crash_after(boundaries)
        with self.assertRaises(_Crash):
            adapter.execute(action)

        successor = self.adapter(ledger=self.open_ledger())
        receipt = successor.execute(action)
        self.assertEqual(receipt.outcome, "executed", msg=receipt.detail)
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))),
                1,
                msg=f"{alias} after {boundaries} boundaries",
            )
            self.assertEqual(self.porcelain(alias), [], msg=alias)
        stored = self.group(action, self.ledgers[-1])
        assert stored is not None
        self.assertTrue(stored.settled)
        self.assertEqual(set(stored.committed), set(ALIASES))

    def test_restart_before_any_repository_is_reached(self) -> None:
        self._restart_after(0)

    def test_restart_after_the_group_was_resumed(self) -> None:
        self._restart_after(1)

    def test_restart_after_the_first_repository_committed(self) -> None:
        self._restart_after(2)

    def test_restart_after_the_second_repository_committed(self) -> None:
        self._restart_after(3)

    def test_restart_after_the_last_repository_committed(self) -> None:
        self._restart_after(4)

    def test_a_full_pass_persists_at_every_boundary_it_publishes(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        phases: list[str] = []
        real = self.ledger.save_checkpoint_group

        def watched(**kwargs: Any) -> Any:
            phases.append(str(kwargs["phase"]))
            return real(**kwargs)

        self.ledger.save_checkpoint_group = watched  # type: ignore[method-assign]
        adapter.execute(action)
        self.assertEqual(len(phases), FULL_RUN_BOUNDARIES)
        self.assertLessEqual(set(phases), set(overseer_checkpoint_groups.GROUP_PHASES))

    def test_a_restart_that_re_prepares_recovers_rather_than_repeats(self) -> None:
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        self._crash_after(2)
        with self.assertRaises(_Crash):
            adapter.execute(action)
        self.assertEqual(len(self.carrying("alpha", self.identity(action, "alpha"))), 1)

        successor = self.adapter(ledger=self.open_ledger())
        self.assertTrue(successor.prepare(action).ready)
        receipt = successor.execute(action)
        self.assertEqual(receipt.outcome, "executed", msg=receipt.detail)
        for alias in ALIASES:
            self.assertEqual(
                len(self.carrying(alias, self.identity(action, alias))), 1, msg=alias
            )
        stored = self.group(action, self.ledgers[-1])
        assert stored is not None
        self.assertEqual(set(stored.committed), set(ALIASES))

    def test_a_settled_group_is_never_prepared_a_second_time(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        declined = self.adapter().prepare(action)
        self.assertFalse(declined.ready)
        self.assertIn("group_settled", declined.detail)

    def test_executing_a_settled_group_again_commits_nothing_more(self) -> None:
        action = self.action()
        adapter = self.adapter()
        adapter.prepare(action)
        adapter.execute(action)
        heads = {alias: self.head(alias) for alias in ALIASES}
        receipt = self.adapter(ledger=self.open_ledger()).execute(action)
        self.assertEqual(receipt.outcome, "executed")
        for alias in ALIASES:
            self.assertEqual(self.head(alias), heads[alias], msg=alias)

    def test_a_registry_that_changed_since_preparation_is_refused(self) -> None:
        action = self.action()
        adapter = self.adapter()
        self.assertTrue(adapter.prepare(action).ready)
        narrowed = self.repositories[:2]
        declined = self.adapter(repositories=narrowed).prepare(
            self.action(repositories=narrowed)
        )
        self.assertFalse(declined.ready)
        self.assertIn("registry_changed", declined.detail)


__all__ = ["contextlib", "json", "shutil", "sqlite3", "tempfile", "unittest", "datetime", "Path", "Any", "get_args", "commit_groups", "overseer_checkpoint", "overseer_checkpoint_groups", "overseer_decisions", "overseer_dispatch", "overseer_policy_client", "overseer_store_ledger", "overseer_store_models", "state", "support", "assert_no_runner_import", "PROJECT", "RUN", "ALIASES", "FINGERPRINT", "DIGEST", "MOMENT", "MESSAGE", "FULL_RUN_BOUNDARIES", "_Crash", "_GroupCase", "RevocationBetweenCommits", "RestartAtEachBoundary"]  # fmt: skip
