# ruff: noqa: F403, F405
from .asl_overseer_dispatch_support import *


class BaselineTests(_DispatchCase):
    """What a permitted dispatch looks like, so the refusals mean something."""

    def test_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_dispatch)

    def test_a_permitted_action_is_attempted_then_confirmed(self) -> None:
        result = self.dispatch()

        self.assertTrue(result.performed, result.detail)
        self.assertIsNone(result.refusal)
        self.assertEqual(len(self.adapter.executions), 1)
        assert result.record is not None
        self.assertEqual(result.record.status, "executed")
        self.assertTrue(result.record.confirmed)
        self.assertEqual(result.record.target, QUESTION)
        self.assertEqual(result.record.progress_token, self.expected_progress())
        assert result.record.receipt is not None
        self.assertEqual(result.record.receipt.locator, "msg-1")

    def test_permission_is_asked_twice_and_under_the_action_category(self) -> None:
        """Once before preparation and once after, per the requirement that a
        decision made before a wait is not evidence about the moment after."""
        self.dispatch()

        self.assertEqual(len(self.world.asked), 2)
        for question in self.world.asked:
            self.assertEqual(question["access"], overseer_policy_client.ACCESS_ACTION)
            self.assertEqual(
                question["repositories"],
                overseer_policy_client.repositories_for(REPOSITORIES),
            )

    def test_the_attempt_is_written_before_the_adapter_is_called(self) -> None:
        """A crash inside the adapter has to leave a durable trace, so the row
        must already exist when the adapter runs."""
        seen: list[int] = []

        def execute(action: overseer_dispatch.Action) -> overseer_dispatch.Receipt:
            seen.append(len(self.ledger.actions(plan_dir=PLAN)))
            return overseer_dispatch.Receipt.executed()

        self.adapter.execute = execute  # type: ignore[method-assign]
        self.dispatch()

        self.assertEqual(seen, [1])

    def expected_progress(self) -> str:
        return overseer_dispatch.progress_token(self.world.facts)


class StaleActionTests(_DispatchCase):
    """A picture of the run that the run has moved on from."""

    def test_a_token_from_an_earlier_snapshot_is_refused(self) -> None:
        action = self.action(token="sha256:stale")

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "stale_action")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_replaced_run_is_refused_even_with_a_matching_plan(self) -> None:
        self.world.inventory = _inventory(_candidate(run_id="run-2"))

        result = self.dispatch()

        self.assertEqual(result.refusal, "run_replaced")
        self.assertEqual(self.adapter.executions, [])

    def test_a_run_that_moves_during_preparation_is_refused(self) -> None:
        """The whole reason there is a second pass: everything checked before
        preparation was true before a wait, not after it."""

        def move(_action: overseer_dispatch.Action) -> None:
            self.world.facts = _facts(turn_index=4)

        self.world.on_prepare = move

        result = self.dispatch()

        self.assertEqual(result.refusal, "stale_action")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_lost_lease_is_refused(self) -> None:
        self.now += 400.0

        result = self.dispatch()

        self.assertEqual(result.refusal, "ownership_lost")

    def test_another_watchers_lease_is_refused(self) -> None:
        result = self.dispatch(watcher_id="watcher-2")

        self.assertEqual(result.refusal, "ownership_lost")

    def test_a_question_that_is_no_longer_open_is_refused(self) -> None:
        self.world.facts = _facts(question_id="q-2", unanswered=("q-2",))
        action = self.action(token=intervention.action_token(self.world.facts))

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "question_unknown")
        self.assertEqual(self.adapter.executions, [])

    def test_an_incident_that_is_not_open_is_refused(self) -> None:
        self.ledger.settle_incident(incident_id=self.incident_id, status="resolved")

        result = self.dispatch()

        self.assertEqual(result.refusal, "incident_unknown")

    def test_an_unobservable_plan_is_refused(self) -> None:
        self.world.inventory = _inventory(_candidate(record_observed=False))

        result = self.dispatch()

        self.assertEqual(result.refusal, "plan_unobserved")


class RevokedPermissionTests(_DispatchCase):
    """Fail closed, every time, and name which kind of no it was."""

    def test_a_refusal_from_agentmux_stops_the_dispatch(self) -> None:
        self.world.decisions = [_decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_an_unreachable_agentmux_is_a_fault_not_a_refusal(self) -> None:
        self.world.decisions = [
            _decision(allowed=False, available=False, failure="client_missing")
        ]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_unavailable")
        self.assertEqual(self.adapter.executions, [])

    def test_permission_withdrawn_during_preparation_is_refused(self) -> None:
        """The first answer was yes. It is the second one that governs."""
        self.world.decisions = [_decision(), _decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(self.adapter.executions, [])
        self.assertEqual(self.ledger.actions(plan_dir=PLAN), ())

    def test_a_policy_that_moves_during_preparation_is_refused(self) -> None:
        self.world.decisions = [_decision(), _decision(fingerprint="fp-second")]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_superseded")
        self.assertEqual(self.adapter.executions, [])

    def test_every_answer_is_recorded_including_the_refusals(self) -> None:
        self.world.decisions = [_decision(allowed=False)]

        self.dispatch()

        recorded = self.ledger.decisions(plan_dir=PLAN)
        self.assertTrue(any(not row.allowed for row in recorded))


class PermissionBeforeContentTests(_DispatchCase):
    """A no from AgentMux costs a discovery walk, and nothing is opened.

    "Fail closed" is about what a refused dispatch does not *do*; this is about
    what a refused dispatch does not *see*. A revoked opt-in that is discovered
    after the run's state and mailbox have already been read has protected
    nothing, however correct the eventual refusal is, so every assertion here
    is about ordering rather than about the outcome.
    """

    def test_permission_precedes_every_read_of_the_run(self) -> None:
        self.dispatch()

        self.assertEqual(["ask", "read", "ask", "read"], self.world.events)

    def test_a_refused_policy_opens_nothing(self) -> None:
        self.world.decisions = [_decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(["ask"], self.world.events)

    def test_an_unavailable_policy_opens_nothing(self) -> None:
        """Fail closed reaches the reads too: no answer is not a quiet yes."""
        self.world.decisions = [
            _decision(allowed=False, available=False, failure="client_missing")
        ]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_unavailable")
        self.assertEqual(["ask"], self.world.events)

    def test_permission_withdrawn_during_preparation_stops_the_second_read(
        self,
    ) -> None:
        """The recheck is the pass a mid-wait revocation lands in, so it is the
        pass that must not read first and ask afterwards."""
        self.world.decisions = [_decision(), _decision(allowed=False)]

        result = self.dispatch()

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(["ask", "read", "ask"], self.world.events)

    def test_a_policy_that_moves_during_preparation_stops_the_second_read(
        self,
    ) -> None:
        self.world.decisions = [_decision(), _decision(fingerprint="fp-second")]

        result = self.dispatch()

        self.assertEqual(result.refusal, "policy_superseded")
        self.assertEqual(["ask", "read", "ask"], self.world.events)

    def test_a_refusal_leaves_an_already_dispatched_action_recorded(self) -> None:
        """Revocation stops what is queued; it does not unmake what happened."""
        self.dispatch()
        self.world.facts = _facts(turn_index=4)
        self.world.decisions = [_decision(allowed=False)]

        result = self.dispatch(
            self.action(token=intervention.action_token(_facts(turn_index=4)))
        )

        self.assertEqual(result.refusal, "unauthorized")
        self.assertEqual(len(self.adapter.executions), 1)
        self.assertEqual(
            [row.status for row in self.ledger.actions(plan_dir=PLAN)], ["executed"]
        )

    def test_reconciliation_cannot_be_run_without_a_grant_for_this_plan(self) -> None:
        elsewhere = _decision().grant()
        assert elsewhere is not None

        with self.assertRaises(overseer_dispatch.DispatchError) as caught:
            overseer_dispatch.reconcile(
                plan_dir="/state/agent-session-loop/plan-other",
                ledger=self.ledger,
                adapters=self.registry,
                grant=elsewhere,
            )

        self.assertIn("plan-other", str(caught.exception))

    def test_reconciliation_cannot_be_run_under_a_context_grant(self) -> None:
        """The easier question, answered instead of the harder one, is how a
        read-only permission turns into an action."""
        context = replace(
            _decision(), access=overseer_policy_client.ACCESS_CONTEXT
        ).grant()
        assert context is not None

        with self.assertRaises(overseer_dispatch.DispatchError) as caught:
            overseer_dispatch.reconcile(
                plan_dir=PLAN,
                ledger=self.ledger,
                adapters=self.registry,
                grant=context,
            )

        self.assertIn(overseer_policy_client.ACCESS_ACTION, str(caught.exception))


class ScopeExpansionTests(_DispatchCase):
    """A permission for these repositories is not a permission for those."""

    def test_an_action_naming_an_undeclared_repository_is_refused(self) -> None:
        declared = overseer_policy_client.repositories_for(REPOSITORIES)
        action = self.action(repositories=declared + (EXTRA_REPOSITORY,))

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "scope_expanded")
        self.assertIn("secrets", result.detail)
        self.assertEqual(self.world.asked, [])

    def test_a_scope_digest_that_no_longer_matches_is_refused(self) -> None:
        action = self.action(scope="sha256:elsewhere")

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "scope_expanded")
        self.assertEqual(self.adapter.executions, [])

    def test_a_repository_added_to_the_run_invalidates_the_action(self) -> None:
        """Scope grows under a live action: the digest the action carries was
        taken against the smaller set, so it stops matching."""
        grown = REPOSITORIES + (
            overseer_discovery.RepositoryDeclaration(
                alias="secrets",
                path="secrets",
                origin="workspace",
                resolved_root="/home/dev/secrets",
            ),
        )
        action = self.action()
        self.world.inventory = _inventory(_candidate(repositories=grown))

        result = self.dispatch(action)

        self.assertEqual(result.refusal, "scope_expanded")
        self.assertEqual(self.adapter.executions, [])


__all__ = tuple(name for name in globals() if not name.startswith("__"))
