"""Tests for test asl overseer decisions authority."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    overseer_decisions,
)
from tests import support_asl_overseer_decisions as fixtures
from tests.support_asl_overseer_decisions import (
    EARLY,
    LATE,
    LATEST,
    PLAN,
    PLAN_SCOPE,
    PROJECT,
    RUN,
)


class HoldAndResumeTests(unittest.TestCase):
    """The session's first fixture: "commit then resume" against a hold."""

    def _register(self, *, hold_last: bool) -> overseer_decisions.Register:
        allow_at, hold_at = (
            (EARLY, LATE)
            if hold_last
            else (
                LATE,
                EARLY,
            )
        )
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    excerpts=(
                        fixtures._excerpt(
                            index=0,
                            timestamp=allow_at,
                            text="commit the checkpoint and resume the run",
                        ),
                        fixtures._excerpt(
                            index=1,
                            timestamp=hold_at,
                            text="hold this one, do not touch it until I say so",
                        ),
                    )
                )
            )
        )
        allow, hold = statements
        return overseer_decisions.register(
            statements=statements,
            claims=[
                fixtures._claim(allow.statement_id, reading="commit then resume"),
                fixtures._claim(hold.statement_id, stance="hold", operations=()),
            ],
        )

    def test_a_later_hold_refuses_the_resume_it_replaced(self) -> None:
        outcome = self._register(hold_last=True)

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertFalse(ruling.authorized)
        self.assertEqual(ruling.refusal, "held")
        self.assertIsNotNone(ruling.decision)
        assert ruling.decision is not None
        self.assertEqual(ruling.decision.stance, "hold")
        self.assertEqual([item.stance for item in ruling.superseded], ["allow"])
        self.assertIn("hold", ruling.summary)

    def test_a_later_allowance_supersedes_the_hold(self) -> None:
        outcome = self._register(hold_last=False)

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertTrue(ruling.authorized)
        self.assertIsNone(ruling.refusal)
        assert ruling.decision is not None
        self.assertEqual(ruling.decision.stance, "allow")
        self.assertEqual(ruling.decision.at, LATE)
        self.assertEqual([item.stance for item in ruling.superseded], ["hold"])

    def test_the_two_decisions_stay_distinct_records(self) -> None:
        outcome = self._register(hold_last=True)

        stances = {item.stance for item in outcome.decisions}
        self.assertEqual(stances, {"allow", "hold"})
        self.assertEqual(len({item.decision_id for item in outcome.decisions}), 2)
        self.assertEqual(len({item.text for item in outcome.decisions}), 2)

    def test_a_hold_naming_nothing_holds_every_operation(self) -> None:
        outcome = self._register(hold_last=True)

        for operation in overseer_decisions.OPERATIONS:
            with self.subTest(operation=operation):
                ruling = overseer_decisions.resolve(
                    register=outcome,
                    request=overseer_decisions.Request(
                        operation=operation,
                        scope=PLAN_SCOPE,
                    ),
                )

                self.assertFalse(ruling.authorized)
                self.assertEqual(ruling.refusal, "held")

    def test_an_allowance_reaches_only_what_it_named(self) -> None:
        outcome = self._register(hold_last=False)

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="guidance", scope=PLAN_SCOPE),
        )

        self.assertFalse(ruling.authorized)
        self.assertEqual(ruling.refusal, "held")

    def test_the_governing_decision_names_its_sources(self) -> None:
        outcome = self._register(hold_last=False)

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="commit", scope=PLAN_SCOPE),
        )

        self.assertEqual(len(ruling.sources), 1)
        self.assertTrue(ruling.sources[0].startswith("transcript:"))


class ChangedAcceptanceTests(unittest.TestCase):
    """The session's second fixture: an acceptance decision that changed."""

    def _register(self, *, second_stance: str = "allow") -> overseer_decisions.Register:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    excerpts=(
                        fixtures._excerpt(
                            index=0,
                            timestamp=EARLY,
                            text="a passing unit suite is enough to accept it",
                        ),
                        fixtures._excerpt(
                            index=1,
                            timestamp=LATE,
                            text="changed my mind: it also has to pass the linter",
                        ),
                        fixtures._excerpt(
                            index=2,
                            timestamp=LATEST,
                            text="commit the checkpoint and resume the run",
                        ),
                    )
                )
            )
        )
        first, second, operational = statements
        return overseer_decisions.register(
            statements=statements,
            claims=[
                fixtures._claim(first.statement_id, topic="acceptance", operations=()),
                fixtures._claim(
                    second.statement_id,
                    topic="acceptance",
                    stance=second_stance,
                    operations=(),
                ),
                fixtures._claim(
                    operational.statement_id, operations=("commit", "resume")
                ),
            ],
        )

    def test_the_later_acceptance_governs_and_the_earlier_is_reported(self) -> None:
        settlement = overseer_decisions.settle(
            register=self._register(),
            topic="acceptance",
            scope=PLAN_SCOPE,
        )

        assert settlement.decision is not None
        self.assertEqual(settlement.decision.at, LATE)
        self.assertIn("linter", settlement.decision.text)
        self.assertEqual(len(settlement.superseded), 1)
        self.assertEqual(settlement.superseded[0].at, EARLY)
        self.assertTrue(settlement.settled)

    def test_a_settled_acceptance_lets_the_operation_through(self) -> None:
        ruling = overseer_decisions.resolve(
            register=self._register(),
            request=overseer_decisions.Request(
                operation="commit",
                scope=PLAN_SCOPE,
                raises=("acceptance",),
            ),
        )

        self.assertTrue(ruling.authorized)
        self.assertEqual(ruling.unresolved, ())

    def test_withheld_acceptance_outranks_an_operational_allowance(self) -> None:
        ruling = overseer_decisions.resolve(
            register=self._register(second_stance="hold"),
            request=overseer_decisions.Request(
                operation="commit",
                scope=PLAN_SCOPE,
                raises=("acceptance",),
            ),
        )

        self.assertFalse(ruling.authorized)
        self.assertEqual(ruling.refusal, "human_decision")
        self.assertEqual(ruling.unresolved, ("acceptance",))
        self.assertTrue(ruling.settlements[0].withheld)
        self.assertIsNotNone(ruling.settlements[0].decision)
        self.assertIsNotNone(ruling.decision)

    def test_an_unanswered_human_question_blocks(self) -> None:
        for topic in overseer_decisions.HUMAN_TOPICS:
            with self.subTest(topic=topic):
                ruling = overseer_decisions.resolve(
                    register=self._register(),
                    request=overseer_decisions.Request(
                        operation="commit",
                        scope=PLAN_SCOPE,
                        raises=(topic, "design"),
                    ),
                )

                self.assertFalse(ruling.authorized)
                self.assertEqual(ruling.refusal, "human_decision")
                self.assertIn("design", ruling.unresolved)

    def test_an_acceptance_decision_authorizes_no_operation(self) -> None:
        outcome = self._register()
        acceptance = [item for item in outcome.decisions if item.topic == "acceptance"]

        for decision in acceptance:
            for operation in overseer_decisions.OPERATIONS:
                with self.subTest(decision=decision.decision_id, operation=operation):
                    self.assertFalse(decision.speaks_to(operation))


class DependencyTests(unittest.TestCase):
    """Which human questions a request depends on when nobody named them.

    ``raises`` is the one part of a request a caller has to compose, and a
    caller composing it from nothing can only name questions it already knew
    were open. The register knows something the caller does not: which human
    topics somebody actually spoke to about this run.
    """

    def _register(
        self,
        *,
        topic: str = "safety",
        stance: str = "hold",
        scope: overseer_decisions.Scope | None = None,
    ) -> overseer_decisions.Register:
        operational = fixtures._direct_statement(
            statement_id="operational",
            at=EARLY,
            text="resume routine pauses",
        )
        human = fixtures._direct_statement(
            statement_id="human",
            at=LATE,
            text="I have not settled that question",
        )
        return overseer_decisions.register(
            statements=(operational, human),
            claims=[
                fixtures._claim("operational", operations=("commit", "resume")),
                fixtures._claim(
                    "human", topic=topic, stance=stance, operations=(), scope=scope
                ),
            ],
        )

    def test_a_claimed_human_decision_raises_its_own_topic(self) -> None:
        """The finding: the caller named nothing and the hold still counts."""
        for topic in overseer_decisions.HUMAN_TOPICS:
            for stance in overseer_decisions.STANCES:
                with self.subTest(topic=topic, stance=stance):
                    raised = overseer_decisions.dependencies(
                        register=self._register(topic=topic, stance=stance),
                        scope=PLAN_SCOPE,
                    )

                    self.assertEqual(raised, (topic,))

    def test_an_operational_decision_raises_nothing(self) -> None:
        """Operations are answered by the ruling, not raised as a question."""
        register = overseer_decisions.register(
            statements=(fixtures._direct_statement(statement_id="operational"),),
            claims=[fixtures._claim("operational")],
        )

        self.assertEqual(
            overseer_decisions.dependencies(register=register, scope=PLAN_SCOPE),
            (),
        )

    def test_named_topics_come_first_and_are_never_repeated(self) -> None:
        """A caller that names the topic the register found gets it once."""
        raised = overseer_decisions.dependencies(
            register=self._register(topic="safety"),
            scope=PLAN_SCOPE,
            raises=("design", "safety"),
        )

        self.assertEqual(raised, ("design", "safety"))

    def test_a_decision_about_another_run_raises_nothing_here(self) -> None:
        """Anti-transfer reads both ways: a neighbour's hold is not our question.

        The decision is real and stays in the register as a precedent; what it
        cannot do is make this run answerable to a question asked about a
        different one.
        """
        register = self._register(
            scope=overseer_decisions.Scope(
                project=PROJECT,
                plan_dir=PLAN,
                run_id=RUN,
                incident="incident-1",
            )
        )

        self.assertEqual(
            overseer_decisions.dependencies(register=register, scope=PLAN_SCOPE),
            (),
        )
        self.assertEqual(len(register.decisions), 2)

    def test_a_raised_topic_the_register_found_is_not_thereby_settled(self) -> None:
        """Raising and settling are different questions with the same input."""
        register = self._register(topic="safety", stance="hold")

        ruling = overseer_decisions.resolve(
            register=register,
            request=overseer_decisions.Request(
                operation="resume",
                scope=PLAN_SCOPE,
                raises=overseer_decisions.dependencies(
                    register=register, scope=PLAN_SCOPE
                ),
            ),
        )

        self.assertFalse(ruling.authorized)
        self.assertEqual(ruling.refusal, "human_decision")
        self.assertEqual(ruling.unresolved, ("safety",))


if __name__ == "__main__":
    unittest.main()
