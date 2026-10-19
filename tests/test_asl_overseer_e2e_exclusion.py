"""Test asl overseer e2e exclusion."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    overseer_policy_client,
)
from tests import support_asl_overseer_assertions as assertions
from tests import support_asl_overseer_e2e as fixtures
from tests.support_asl_overseer_e2e import setUpModule as setUpModule
from tests.support_asl_overseer_e2e import tearDownModule as tearDownModule


class ExclusionInvariantTests(assertions._WorldCase):
    """The excluded project, across every operation, before and after.

    Every test here ends in the same two assertions, because the invariant is
    not about any one operation: the excluded repository and the excluded
    conversation are byte-for-byte and mtime-for-mtime what they were, and
    nothing AgentMux said back carries the secret either of them contains.
    """

    def setUp(self) -> None:
        super().setUp()
        self.before = self._excluded()

    def test_the_excluded_project_is_refused_by_name(self) -> None:
        decision = self.world.authorize(
            plan_dir=str(self.plan),
            access=overseer_policy_client.ACCESS_CONTEXT,
            repositories=(
                overseer_policy_client.Repository(
                    alias=fixtures.EXCLUDED,
                    path=str(self.world.repos[fixtures.EXCLUDED]),
                ),
            ),
            timeout=30.0,
        )

        self.assertFalse(decision.allowed)
        self.assertTrue(decision.available)
        self.assertIsNone(decision.failure)
        self.assertEqual(
            [fixtures.EXCLUDED],
            [item.alias for item in decision.refused_repositories()],
        )
        self.assertIsNone(decision.grant())
        self._assert_untouched()

    def test_the_excluded_conversation_is_withheld_without_being_located(self) -> None:
        history = self.world.read_history(
            plan_dir=str(self.plan),
            repositories=overseer_policy_client.repositories_for(
                self._candidate().repositories
            ),
            timeout=30.0,
        )

        self.assertTrue(history.allowed, history.summary)
        self.assertEqual(
            [self.world.workspace_root],
            [item.directory for item in history.conversations],
        )
        self.assertEqual(
            [(fixtures.HARNESS, self.excluded_session)],
            [(item.harness, item.session_id) for item in history.withheld],
        )
        for item in history.withheld:
            self.assertTrue(item.reason)
            self.assertNotIn(str(self.world.repos[fixtures.EXCLUDED]), item.reason)
        self._assert_untouched()

    def test_no_operation_reaches_the_excluded_project(self) -> None:
        """Every operation in the scope, none of them touching the exclusion."""
        question = self.world.ask_question(self.plan, "which lint profile?")
        self.world.change(fixtures.AUTHORIZED)
        self.world.publish_turn(self.plan, (fixtures.AUTHORIZED,))
        attempts = (
            ("reply", ("--question-id", question, "--text", "the repository default")),
            ("guidance", ("--text", "commit what you have and carry on")),
            ("commit", ("--message", "checkpoint work in progress")),
            ("resume", ()),
            ("verify", ("--check", "lint")),
        )

        refusals = {}
        details = {}
        for operation, extra in attempts:
            # Re-observed each time: every operation that lands moves the run,
            # and an action carrying the previous picture is stale by design.
            _status, payload = self.act(
                self.observed(), operation, self.allowing(operation), *extra
            )
            refusals[operation] = payload["dispatch"]["refusal"]
            details[operation] = payload["dispatch"]["detail"]

        self.assertIsNone(refusals["reply"])
        self.assertIsNone(refusals["guidance"])
        self.assertIsNone(refusals["commit"])
        # This world's sessions carry no completion contract, so there is no
        # check to name and `verify` stops at the plan. That is the whole
        # point of the operation: the argv comes from a declaration or the
        # action is refused, and no `--check` string here becomes a command.
        self.assertEqual("preparation_declined", refusals["verify"])
        self.assertIn("contract_absent", details["verify"])
        # The plan's own worker is alive throughout, so `resume` is refused by
        # the census. Nothing here may reach the leaf's launcher: it has no
        # `launch` seam, and an authorized resume would start a real runner.
        self.assertEqual("preparation_declined", refusals["resume"])
        self.assertEqual(1, len(self._porcelain_committed(fixtures.AUTHORIZED)))
        self._assert_untouched()

    # -- helpers -----------------------------------------------------------

    def _excluded(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The excluded repository and the excluded conversation, as bytes."""
        conversations = self.world.transcript_path(
            fixtures.EXCLUDED, self.excluded_session
        ).parent
        return (
            self.world.snapshot(self.world.repos[fixtures.EXCLUDED]),
            self.world.snapshot(conversations),
        )

    def _assert_untouched(self) -> None:
        self.assertEqual(self.before, self._excluded())
        self.assertNotIn(fixtures.SECRET, self.world.answers())


if __name__ == "__main__":
    unittest.main()
