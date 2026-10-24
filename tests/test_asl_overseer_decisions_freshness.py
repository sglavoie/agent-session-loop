"""Tests for test asl overseer decisions freshness."""

from __future__ import annotations

import unittest
from collections.abc import Sequence

from agent_session_loop import (
    mailbox_vocabulary,
    overseer_decisions,
)
from tests import support_asl_overseer_decisions as fixtures
from tests.support_asl_overseer_decisions import (
    EARLY,
    GRANT,
    LATE,
    OTHER_PLAN,
    OTHER_PROJECT,
    PLAN,
    PLAN_SCOPE,
    PROJECT,
    RUN,
)

ASL_TEST_PORTABLE = True


class PrecedentTests(unittest.TestCase):
    """The session's fourth fixture: an approval that was about something else."""

    def _elsewhere(
        self,
        *,
        scope: overseer_decisions.Scope,
        project: str = PROJECT,
    ) -> overseer_decisions.Register:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    directory=project,
                    session_id="sess-old",
                    excerpts=(
                        fixtures._excerpt(
                            timestamp=EARLY,
                            text="yes, commit the checkpoint and resume it",
                        ),
                    ),
                )
            )
        )
        return overseer_decisions.register(
            statements=statements,
            claims=[fixtures._claim(statements[0].statement_id, scope=scope)],
        )

    def test_an_approval_in_another_project_never_authorizes_this_one(self) -> None:
        outcome = self._elsewhere(
            project=OTHER_PROJECT,
            scope=overseer_decisions.Scope(project=OTHER_PROJECT),
        )

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertEqual(len(outcome.decisions), 1)
        self.assertFalse(ruling.authorized)
        self.assertEqual(ruling.refusal, "unsupported_recovery")
        self.assertEqual(len(ruling.precedents), 1)
        self.assertIsNone(ruling.decision)

    def test_an_approval_for_another_incident_is_a_precedent(self) -> None:
        outcome = self._elsewhere(
            scope=overseer_decisions.Scope(
                project=PROJECT,
                plan_dir=PLAN,
                incident="incident-1",
            )
        )

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(
                operation="resume",
                scope=overseer_decisions.Scope(
                    project=PROJECT,
                    plan_dir=PLAN,
                    run_id=RUN,
                    incident="incident-2",
                ),
            ),
        )

        self.assertEqual(ruling.refusal, "unsupported_recovery")
        self.assertEqual(ruling.precedents[0].scope.incident, "incident-1")

    def test_an_approval_for_another_plan_is_a_precedent(self) -> None:
        outcome = self._elsewhere(
            scope=overseer_decisions.Scope(
                project=PROJECT,
                plan_dir=OTHER_PLAN,
            )
        )

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertEqual(ruling.refusal, "unsupported_recovery")

    def test_a_target_that_names_no_incident_does_not_inherit_one(self) -> None:
        outcome = self._elsewhere(
            scope=overseer_decisions.Scope(
                project=PROJECT,
                plan_dir=PLAN,
                incident="incident-1",
            )
        )

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(
                operation="resume",
                scope=overseer_decisions.Scope(
                    project=PROJECT,
                    plan_dir=PLAN,
                ),
            ),
        )

        self.assertEqual(ruling.refusal, "unsupported_recovery")

    def test_a_decision_that_narrows_nothing_covers_its_project(self) -> None:
        outcome = self._elsewhere(scope=overseer_decisions.Scope(project=PROJECT))

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertTrue(ruling.authorized)
        self.assertEqual(ruling.precedents, ())

    def test_silence_is_not_a_precedent(self) -> None:
        ruling = overseer_decisions.resolve(
            register=overseer_decisions.register(statements=[], claims=[]),
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertEqual(ruling.refusal, "no_decision")
        self.assertEqual(ruling.precedents, ())

    def test_a_request_that_names_no_project_is_refused_first(self) -> None:
        ruling = overseer_decisions.resolve(
            register=self._elsewhere(scope=overseer_decisions.Scope(project=PROJECT)),
            request=overseer_decisions.Request(
                operation="resume", scope=overseer_decisions.Scope(project="")
            ),
        )

        self.assertEqual(ruling.refusal, "target_unscoped")

    def test_an_operation_outside_the_scope_is_refused(self) -> None:
        ruling = overseer_decisions.resolve(
            register=self._elsewhere(scope=overseer_decisions.Scope(project=PROJECT)),
            request=overseer_decisions.Request(
                operation="repair_the_application",
                scope=PLAN_SCOPE,
            ),
        )

        self.assertEqual(ruling.refusal, "operation_unsupported")


class EvidenceFreshnessTests(unittest.TestCase):
    """A reading is about a record, and a record that moved is a different one.

    Supersession orders the statements a host *claimed*, so it cannot see the
    one nobody claimed because nobody had seen it. That is exactly the shape of
    a hold typed while an action was in flight: every claim still stands, the
    allowance is still the last word among them, and the run is nonetheless
    supposed to stop. These tests are about the check that closes that gap and
    about the things that must *not* trip it, since a freshness check that fires
    on ASL's own bookkeeping would be a check an operator learns to ignore.
    """

    def _reviewed(self) -> tuple[overseer_decisions.Statement, ...]:
        """The record a host reads: one instruction, one relayed corroboration."""
        return (
            fixtures._direct_statement(text="commit the checkpoint and resume"),
            *overseer_decisions.statements_from_supervision(
                [fixtures._message()],
                grant=GRANT,
                scope=PLAN_SCOPE,
            ),
        )

    def _allowed(
        self,
        statements: Sequence[overseer_decisions.Statement],
    ) -> overseer_decisions.Register:
        return overseer_decisions.register(
            statements=statements,
            claims=[fixtures._claim("transcript:claude:sess-1#0")],
        )

    def test_a_digest_answers_to_the_statements_a_host_would_have_read(self) -> None:
        statements = self._reviewed()

        digest = overseer_decisions.evidence_digest(statements, scope=PLAN_SCOPE)

        self.assertTrue(digest.startswith("ev1-"))
        self.assertNotEqual(
            digest,
            overseer_decisions.evidence_digest((), scope=PLAN_SCOPE),
        )

    def test_the_digest_does_not_depend_on_the_order_evidence_arrived_in(self) -> None:
        """A boundary that reorders its answer has not changed the record."""
        statements = self._reviewed()

        self.assertEqual(
            overseer_decisions.evidence_digest(statements, scope=PLAN_SCOPE),
            overseer_decisions.evidence_digest(
                tuple(reversed(statements)),
                scope=PLAN_SCOPE,
            ),
        )

    def test_asl_s_own_bookkeeping_does_not_move_the_digest(self) -> None:
        """The ack of the message being sent is not a change in what a human said.

        ASL writes a machine record for traffic it handles, including the very
        action under way. A digest answering to those would report a change on
        every action, and a check that always fires stops being read.
        """
        statements = self._reviewed()
        noisy = statements + overseer_decisions.statements_from_supervision(
            [
                fixtures._message(
                    seq=2,
                    kind=mailbox_vocabulary.KIND_ACK,
                    text="delivered",
                    created_at=LATE,
                ),
                fixtures._message(
                    seq=3,
                    kind=mailbox_vocabulary.KIND_QUESTION,
                    text="which branch?",
                    created_at=LATE,
                ),
            ],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(
            overseer_decisions.evidence_digest(statements, scope=PLAN_SCOPE),
            overseer_decisions.evidence_digest(noisy, scope=PLAN_SCOPE),
        )

    def test_a_statement_about_another_project_does_not_move_the_digest(self) -> None:
        """The anti-transfer rule again: elsewhere is not this run's record."""
        statements = self._reviewed()
        elsewhere = statements + (
            fixtures._direct_statement(
                statement_id="transcript:claude:sess-9#0",
                scope=overseer_decisions.Scope(project="/home/dev/other"),
                at=LATE,
                text="hold everything",
            ),
        )

        self.assertEqual(
            overseer_decisions.evidence_digest(statements, scope=PLAN_SCOPE),
            overseer_decisions.evidence_digest(elsewhere, scope=PLAN_SCOPE),
        )

    def test_a_fresh_human_hold_moves_the_digest(self) -> None:
        statements = self._reviewed()
        held = statements + (
            fixtures._direct_statement(
                statement_id="transcript:claude:sess-1#1",
                at=LATE,
                text="hold this run, do not resume",
            ),
        )

        self.assertNotEqual(
            overseer_decisions.evidence_digest(statements, scope=PLAN_SCOPE),
            overseer_decisions.evidence_digest(held, scope=PLAN_SCOPE),
        )

    def test_a_relayed_instruction_moves_the_digest(self) -> None:
        """A relay authorizes nothing and is still a change in the record.

        It cannot carry a decision, so it can never appear as a claim; what it
        can do is tell a host that the plan's supervision channel now says
        something it did not say when the host read it.
        """
        statements = self._reviewed()
        relayed = statements + overseer_decisions.statements_from_supervision(
            [
                fixtures._message(
                    seq=2,
                    text="stop before committing",
                    created_at=LATE,
                )
            ],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertNotEqual(
            overseer_decisions.evidence_digest(statements, scope=PLAN_SCOPE),
            overseer_decisions.evidence_digest(relayed, scope=PLAN_SCOPE),
        )

    def test_a_request_naming_no_evidence_is_answered_as_before(self) -> None:
        """The binding is opt-in, so every existing request keeps its meaning."""
        statements = self._reviewed()

        ruling = overseer_decisions.resolve(
            register=self._allowed(statements),
            request=overseer_decisions.Request(operation="commit", scope=PLAN_SCOPE),
        )

        self.assertTrue(ruling.authorized)

    def test_unchanged_reviewed_evidence_remains_usable(self) -> None:
        statements = self._reviewed()

        ruling = overseer_decisions.resolve(
            register=self._allowed(statements),
            request=overseer_decisions.Request(
                operation="commit",
                scope=PLAN_SCOPE,
                reviewed=overseer_decisions.evidence_digest(
                    statements, scope=PLAN_SCOPE
                ),
            ),
        )

        self.assertTrue(ruling.authorized)
        self.assertIsNone(ruling.refusal)

    def test_a_hold_nobody_claimed_refuses_the_reading_that_predates_it(self) -> None:
        """The finding itself: the claims stand and the action stops anyway.

        Every claim in this register is valid, in force, and unsuperseded --
        re-resolving them answers `authorized` no matter how many times it is
        asked. What changed is the record, and naming the record is what lets a
        re-check say so.
        """
        reviewed = self._reviewed()
        digest = overseer_decisions.evidence_digest(reviewed, scope=PLAN_SCOPE)
        held = reviewed + (
            fixtures._direct_statement(
                statement_id="transcript:claude:sess-1#1",
                at=LATE,
                text="hold this run, do not resume",
            ),
        )
        register = self._allowed(held)

        stale = overseer_decisions.resolve(
            register=register,
            request=overseer_decisions.Request(
                operation="commit",
                scope=PLAN_SCOPE,
                reviewed=digest,
            ),
        )
        unbound = overseer_decisions.resolve(
            register=register,
            request=overseer_decisions.Request(operation="commit", scope=PLAN_SCOPE),
        )

        self.assertFalse(stale.authorized)
        self.assertEqual(stale.refusal, "evidence_changed")
        self.assertIn(digest, stale.summary)
        self.assertTrue(
            unbound.authorized, "the claims themselves never stopped holding"
        )

    def test_changed_evidence_outranks_a_governing_allowance(self) -> None:
        """Ordering: the reading is answered before what the reading said."""
        self.assertLess(
            overseer_decisions.REFUSALS.index("evidence_changed"),
            overseer_decisions.REFUSALS.index("held"),
        )

    def test_a_question_that_cannot_be_asked_outranks_changed_evidence(self) -> None:
        """An unsupported operation is refused before its evidence is weighed."""
        ruling = overseer_decisions.resolve(
            register=self._allowed(self._reviewed()),
            request=overseer_decisions.Request(
                operation="repair_the_application",
                scope=PLAN_SCOPE,
                reviewed="ev1-something-else",
            ),
        )

        self.assertEqual(ruling.refusal, "operation_unsupported")

    def test_nothing_about_a_digest_is_a_permission(self) -> None:
        """It summarizes evidence and cannot be turned into an authorization."""
        ruling = overseer_decisions.resolve(
            register=overseer_decisions.register(
                statements=self._reviewed(), claims=[]
            ),
            request=overseer_decisions.Request(
                operation="commit",
                scope=PLAN_SCOPE,
                reviewed=overseer_decisions.evidence_digest(
                    self._reviewed(), scope=PLAN_SCOPE
                ),
            ),
        )

        self.assertFalse(ruling.authorized)
        self.assertEqual(ruling.refusal, "no_decision")


if __name__ == "__main__":
    unittest.main()
