"""Tests for test asl overseer decisions claims."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    mailbox_constants,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_policy_client,
)
from tests import support_asl_overseer_decisions as fixtures
from tests.support_asl_overseer_decisions import (
    DIGEST,
    FINGERPRINT,
    GRANT,
    OTHER_PLAN,
    OTHER_PROJECT,
    PLAN,
    PLAN_SCOPE,
    PROJECT,
    RUN,
)


class SupervisionProvenanceTests(unittest.TestCase):
    """The mailbox records no author, so nothing in it is a user instruction."""

    def test_every_mailbox_kind_is_classified(self) -> None:
        self.assertEqual(
            set(overseer_decisions.SUPERVISION_VOICES),
            set(mailbox_vocabulary.MESSAGE_KINDS),
        )

    def test_no_mailbox_kind_can_authorize(self) -> None:
        for kind in mailbox_vocabulary.MESSAGE_KINDS:
            with self.subTest(kind=kind):
                statements = overseer_decisions.statements_from_supervision(
                    [fixtures._message(kind=kind)],
                    grant=GRANT,
                    scope=PLAN_SCOPE,
                )

                self.assertFalse(statements[0].authorizing)
                self.assertNotEqual(statements[0].voice, "user_instruction")

    def test_the_instruction_shaped_kinds_are_relayed(self) -> None:
        for kind in (
            mailbox_vocabulary.KIND_GUIDANCE,
            mailbox_vocabulary.KIND_REPLY,
            mailbox_vocabulary.KIND_RESOLUTION,
        ):
            with self.subTest(kind=kind):
                statements = overseer_decisions.statements_from_supervision(
                    [fixtures._message(kind=kind)],
                    grant=GRANT,
                    scope=PLAN_SCOPE,
                )

                self.assertEqual(statements[0].voice, "relayed_instruction")

    def test_a_worker_question_is_a_request(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [
                fixtures._message(
                    kind=mailbox_vocabulary.KIND_QUESTION, text="may I commit?"
                )
            ],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "worker_request")

    def test_bookkeeping_is_a_machine_record(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [fixtures._message(kind=mailbox_vocabulary.KIND_RECEIPT, text="")],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "machine_record")

    def test_an_unknown_kind_is_unattributed(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [fixtures._message(kind="telepathy")],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "unattributed")

    def test_a_relayed_machine_prompt_is_a_generated_prompt(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [
                fixtures._message(
                    text=f"{overseer_decisions.TASK_PACKET_MARKER}\n\ncommit"
                )
            ],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "generated_prompt")
        self.assertEqual(statements[0].marker, overseer_decisions.TASK_PACKET_MARKER)

    def test_a_worker_request_keeps_its_voice_when_it_quotes_a_prompt(self) -> None:
        """The marker rule exists to stop machine text being read as an
        instruction, and a worker request was never one."""
        statements = overseer_decisions.statements_from_supervision(
            [
                fixtures._message(
                    kind=mailbox_vocabulary.KIND_QUESTION,
                    text=f"{overseer_decisions.TASK_PACKET_MARKER} says what?",
                )
            ],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "worker_request")

    def test_a_grant_that_does_not_cover_the_scope_yields_nothing(self) -> None:
        for label, grant, scope in (
            ("absent", None, PLAN_SCOPE),
            (
                "another plan",
                overseer_policy_client.Grant(
                    plan_dir=OTHER_PLAN,
                    access=overseer_policy_client.ACCESS_CONTEXT,
                    request_digest=DIGEST,
                    config_fingerprint=FINGERPRINT,
                ),
                PLAN_SCOPE,
            ),
            (
                "another access",
                overseer_policy_client.Grant(
                    plan_dir=PLAN,
                    access=overseer_policy_client.ACCESS_ACTION,
                    request_digest=DIGEST,
                    config_fingerprint=FINGERPRINT,
                ),
                PLAN_SCOPE,
            ),
            (
                "no digest",
                overseer_policy_client.Grant(
                    plan_dir=PLAN,
                    access=overseer_policy_client.ACCESS_CONTEXT,
                    request_digest="",
                    config_fingerprint=FINGERPRINT,
                ),
                PLAN_SCOPE,
            ),
            (
                "no fingerprint",
                overseer_policy_client.Grant(
                    plan_dir=PLAN,
                    access=overseer_policy_client.ACCESS_CONTEXT,
                    request_digest=DIGEST,
                    config_fingerprint="",
                ),
                PLAN_SCOPE,
            ),
            (
                "unplanned scope",
                GRANT,
                overseer_decisions.Scope(project=PROJECT),
            ),
        ):
            with self.subTest(grant=label):
                self.assertEqual(
                    overseer_decisions.statements_from_supervision(
                        [fixtures._message()],
                        grant=grant,
                        scope=scope,
                    ),
                    (),
                )

    def test_a_supervision_statement_carries_the_plan_scope(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [fixtures._message()],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].scope, PLAN_SCOPE)
        self.assertEqual(statements[0].space, overseer_decisions.STATE_SPACE)
        self.assertTrue(
            statements[0].locator.startswith(mailbox_constants.MAILBOX_FILE_NAME)
        )


class ClaimValidationTests(unittest.TestCase):
    """A reading is checked against the record it claims to read."""

    def test_a_claim_on_a_user_instruction_becomes_a_decision(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(fixtures._conversation(excerpts=(fixtures._excerpt(),)))
        )

        outcome = overseer_decisions.register(
            statements=statements,
            claims=[
                fixtures._claim(
                    statements[0].statement_id,
                    reading="the operator allowed a checkpoint commit and a resume",
                )
            ],
        )

        self.assertEqual(outcome.rejections, ())
        self.assertEqual(len(outcome.decisions), 1)
        decision = outcome.decisions[0]
        self.assertEqual(decision.sources, (statements[0].statement_id,))
        self.assertEqual(decision.text, statements[0].text)
        self.assertEqual(
            decision.reading, "the operator allowed a checkpoint commit and a resume"
        )
        self.assertNotEqual(decision.reading, decision.text)

    def test_corroborating_sources_are_recorded(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    excerpts=(
                        fixtures._excerpt(),
                        fixtures._excerpt(index=1, role="assistant"),
                    )
                )
            )
        )

        outcome = overseer_decisions.register(
            statements=statements,
            claims=[
                fixtures._claim(
                    statements[0].statement_id,
                    corroborating=(statements[1].statement_id,),
                )
            ],
        )

        self.assertEqual(
            outcome.decisions[0].sources,
            (statements[0].statement_id, statements[1].statement_id),
        )

    def test_every_rejection_reason_is_reachable(self) -> None:
        narrow = overseer_decisions.Scope(
            project=PROJECT,
            plan_dir=PLAN,
            run_id=RUN,
            incident="incident-1",
        )
        statements = (
            fixtures._direct_statement(statement_id="s-user"),
            fixtures._direct_statement(
                statement_id="s-assistant", voice="assistant_interpretation"
            ),
            fixtures._direct_statement(statement_id="s-undated", at="whenever"),
            fixtures._direct_statement(statement_id="s-narrow", scope=narrow),
        )
        cases: dict[str, overseer_decisions.Claim] = {
            "unknown_statement": fixtures._claim("s-missing"),
            "not_user_authored": fixtures._claim("s-assistant"),
            "undated": fixtures._claim("s-undated"),
            "scope_unnamed": fixtures._claim(
                "s-user", scope=overseer_decisions.Scope(project="")
            ),
            "scope_widened": fixtures._claim(
                "s-narrow",
                scope=overseer_decisions.Scope(
                    project=PROJECT,
                    plan_dir=PLAN,
                    run_id=RUN,
                    incident="incident-2",
                ),
            ),
            "unknown_topic": fixtures._claim("s-user", topic="vibes"),
            "unknown_operation": fixtures._claim(
                "s-user", operations=("rewrite-history",)
            ),
            "operations_unnamed": fixtures._claim("s-user", operations=()),
            "operations_named": fixtures._claim(
                "s-user", topic="acceptance", operations=("commit",)
            ),
        }
        seen: set[str] = set()
        for reason, claim in cases.items():
            with self.subTest(reason=reason):
                outcome = overseer_decisions.register(
                    statements=statements, claims=[claim]
                )

                self.assertEqual(outcome.decisions, ())
                self.assertEqual(len(outcome.rejections), 1)
                self.assertEqual(outcome.rejections[0].reason, reason)
                self.assertTrue(outcome.rejections[0].detail)
                seen.add(reason)

        duplicate = overseer_decisions.register(
            statements=statements,
            claims=[
                fixtures._claim("s-user"),
                fixtures._claim("s-user"),
            ],
        )
        self.assertEqual(len(duplicate.decisions), 1)
        self.assertEqual(duplicate.rejections[0].reason, "duplicate_claim")
        seen.add("duplicate_claim")

        self.assertEqual(seen, set(overseer_decisions.CLAIM_REJECTIONS))

    def test_no_voice_but_a_user_instruction_can_carry_a_decision(self) -> None:
        for voice in overseer_decisions.VOICES:
            with self.subTest(voice=voice):
                outcome = overseer_decisions.register(
                    statements=[
                        fixtures._direct_statement(statement_id="s-1", voice=voice)
                    ],
                    claims=[fixtures._claim("s-1")],
                )

                if voice in overseer_decisions.AUTHORIZING_VOICES:
                    self.assertEqual(len(outcome.decisions), 1)
                else:
                    self.assertEqual(outcome.decisions, ())
                    self.assertEqual(outcome.rejections[0].reason, "not_user_authored")

    def test_one_statement_may_decide_two_topics(self) -> None:
        outcome = overseer_decisions.register(
            statements=[fixtures._direct_statement(statement_id="s-1")],
            claims=[
                fixtures._claim("s-1"),
                fixtures._claim("s-1", topic="acceptance", operations=()),
            ],
        )

        self.assertEqual(outcome.rejections, ())
        self.assertEqual(
            [item.topic for item in outcome.decisions], ["operations", "acceptance"]
        )

    def test_a_claim_may_narrow_its_statements_scope(self) -> None:
        outcome = overseer_decisions.register(
            statements=[
                fixtures._direct_statement(
                    statement_id="s-1",
                    scope=overseer_decisions.Scope(project=PROJECT),
                )
            ],
            claims=[
                fixtures._claim(
                    "s-1",
                    scope=overseer_decisions.Scope(
                        project=PROJECT,
                        plan_dir=PLAN,
                        run_id=RUN,
                        incident="incident-1",
                    ),
                )
            ],
        )

        self.assertEqual(outcome.rejections, ())
        self.assertEqual(outcome.decisions[0].scope.incident, "incident-1")

    def test_a_claim_cannot_move_to_another_project(self) -> None:
        outcome = overseer_decisions.register(
            statements=[fixtures._direct_statement(statement_id="s-1")],
            claims=[
                fixtures._claim(
                    "s-1",
                    scope=overseer_decisions.Scope(project=OTHER_PROJECT),
                )
            ],
        )

        self.assertEqual(outcome.rejections[0].reason, "scope_widened")

    def test_a_register_reports_the_rejections_for_one_statement(self) -> None:
        outcome = overseer_decisions.register(
            statements=[
                fixtures._direct_statement(statement_id="s-1", voice="worker_request")
            ],
            claims=[fixtures._claim("s-1")],
        )

        self.assertEqual(len(outcome.rejected("s-1")), 1)
        self.assertIsNone(outcome.decision("s-1:operations"))


if __name__ == "__main__":
    unittest.main()
