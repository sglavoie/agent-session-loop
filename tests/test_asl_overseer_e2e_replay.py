"""Test asl overseer e2e replay."""

from __future__ import annotations

import unittest

from agent_session_loop import (
    mailbox,
    mailbox_appenders,
    mailbox_vocabulary,
    overseer_cli,
    overseer_decisions,
)
from tests import support
from tests import support_asl_overseer_assertions as assertions
from tests import support_asl_overseer_e2e as fixtures
from tests.support_asl_overseer_e2e import setUpModule as setUpModule
from tests.support_asl_overseer_e2e import tearDownModule as tearDownModule

ASL_TEST_LANE = "integration"


class HistoricalReplayTests(assertions._WorldCase):
    """The reviewed incident shapes, replayed through the whole loop."""

    def test_the_checkpoint_recovery_commits_the_runs_own_work(self) -> None:
        """The `tt` incident: a run paused with work nobody had committed."""
        self.world.change(fixtures.AUTHORIZED)
        self.world.publish_turn(self.plan, (fixtures.AUTHORIZED,))
        packet = self.observed()

        status, decision = self.decide(packet, "commit", self.allowing("commit"))
        self.assertEqual(overseer_cli.EXIT_OK, status, decision)
        self.assertTrue(decision["authorized"], decision)

        status, payload = self.act(
            packet,
            "commit",
            self.allowing("commit"),
            "--message",
            "chore: checkpoint work in progress",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIn(
            "work.txt",
            support.git_stdout(
                self.world.repos[fixtures.AUTHORIZED],
                "show",
                "--name-only",
                "--format=",
                "HEAD",
            ),
        )
        self.assertEqual([], self._porcelain(fixtures.AUTHORIZED))

    def test_the_dirty_worktree_pause_is_answered_with_guidance(self) -> None:
        self.world.change(fixtures.AUTHORIZED)
        packet = self.observed()

        status, payload = self.act(
            packet,
            "guidance",
            self.allowing("guidance"),
            "--text",
            "commit what you have and carry on",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertIn(
            mailbox_vocabulary.KIND_GUIDANCE,
            [message.kind for message in self.world.messages(self.plan)],
        )

    def test_an_open_question_is_answered_where_the_person_authorized_it(self) -> None:
        question = self.world.ask_question(self.plan, "which lint profile?")
        packet = self.observed()

        status, payload = self.act(
            packet,
            "reply",
            self.allowing("reply"),
            "--question-id",
            question,
            "--text",
            "the repository default",
        )

        self.assertEqual(overseer_cli.EXIT_OK, status, payload)
        self.assertTrue(payload["dispatch"]["performed"], payload["dispatch"]["detail"])
        self.assertEqual(
            (),
            mailbox.unanswered_questions(self.plan, state_dir=self.world.state_root),
        )

    def test_the_stale_question_resume_stays_refused_while_it_is_open(self) -> None:
        """The stale-question incident: a pause an answer, not a restart, ends."""
        self.world.write_run(
            "plan-tt",
            pause_reason="waiting_for_reply",
            **{"pause_policy": {"user_action": "reply"}},
        )
        self.world.hold_lock(self.plan, self.harness.pid)
        self.world.ask_question(self.plan, "which lint profile?")
        packet = self.observed()

        status, payload = self.act(packet, "resume", self.allowing("resume"))

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["dispatch"]["performed"])
        self.assertEqual("preparation_declined", payload["dispatch"]["refusal"])
        self.assertIn("answer_required", payload["dispatch"]["detail"])
        self.assertIsNotNone(payload["escalation"])

    def test_the_resume_becomes_available_once_the_question_is_resolved(self) -> None:
        """The same run, after the question it waited on was superseded.

        Driven through :func:`overseer_dispatch.dispatch` rather than the leaf:
        the leaf's registry has no ``launch`` seam, so an authorized resume there
        would spawn a real runner. Everything else -- the ledger, the lease, the
        policy client, the binary, the state corpus -- is the same as the leaf's.
        """
        self.world.write_run(
            "plan-tt",
            pause_reason="waiting_for_reply",
            **{"pause_policy": {"user_action": "reply"}},
        )
        self.world.release_lock(self.plan)
        question = self.world.ask_question(self.plan, "which lint profile?")
        mailbox_appenders.append_resolution(
            self.plan,
            question,
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            expected_run_id=f"{self.plan.name}-run-1",
            note="the question was superseded before anyone answered it",
            state_dir=self.world.state_root,
        )
        packet = self.observed()

        dispatch = self._dispatch_resume(packet)

        self.assertTrue(dispatch.performed, dispatch.detail)
        self.assertFalse(dispatch.escalated, dispatch.detail)
        self.assertTrue(self.launched, "the resume never reached the launcher")

    def test_an_explicit_hold_keeps_the_operation_blocked(self) -> None:
        """A person who said hold is a decision, not an absence of one."""
        packet = self.observed()
        claims = [
            {
                "statement_id": self.world.statement_id(self.session, 0),
                "topic": "safety",
                "stance": "hold",
                "reading": "the user wants to look at this before anything moves",
            }
        ]

        status, payload = self.decide(packet, "commit", claims)

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertIsNotNone(payload["escalation"])
        self.assertTrue(payload["escalation"]["question"])
        self.assertFalse(payload["escalation"]["blocked"] == [])

    def test_a_later_word_supersedes_the_earlier_one(self) -> None:
        """Two turns, the second withdrawing the first; the second governs."""
        session = self.world.write_transcript(
            fixtures.AUTHORIZED,
            [
                ("user", fixtures.STANDING_INSTRUCTION),
                ("user", fixtures.LATER_HOLD),
            ],
            session_id="tt-superseded",
        )
        self.world.change(fixtures.AUTHORIZED)
        self.world.publish_turn(self.plan, (fixtures.AUTHORIZED,))
        packet = self.observed()
        claims = [
            {
                "statement_id": self.world.statement_id(session, 0),
                "topic": "operations",
                "stance": "allow",
                "operations": ["commit"],
                "reading": "the user authorized checkpoints",
            },
            {
                "statement_id": self.world.statement_id(session, 1),
                "topic": "operations",
                "stance": "hold",
                "reading": "the user later withdrew that for tt",
            },
        ]

        status, payload = self.decide(packet, "commit", claims)

        self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
        self.assertFalse(payload["authorized"])
        self.assertEqual(
            [self.world.statement_id(session, 0)],
            [item["statement_id"] for item in payload["ruling"]["superseded"]],
        )
        self.assertEqual([], self._porcelain_committed(fixtures.AUTHORIZED))

    def test_a_machine_conversation_authorizes_nothing_it_says(self) -> None:
        """The same sentence, in a conversation no person was in.

        Nothing here names a session class: the fixture carries the markers a
        harness writes, the built binary classifies them, and ASL reads that
        classification. Both halves of that contract are under test, which is
        why this lives with the real binary rather than beside a stub.
        """
        for session_class in (
            overseer_decisions.SESSION_CLASS_AUTOMATED,
            overseer_decisions.SESSION_CLASS_UNKNOWN,
        ):
            with self.subTest(session_class=session_class):
                session = self.world.write_transcript(
                    fixtures.AUTHORIZED,
                    [("user", fixtures.STANDING_INSTRUCTION)],
                    session_id=f"tt-{session_class}",
                    session_class=session_class,
                )
                packet = self.observed()
                claims = [
                    {
                        "statement_id": self.world.statement_id(session, 0),
                        "topic": "operations",
                        "stance": "allow",
                        "operations": ["commit"],
                        "reading": "read as if a person had authorized checkpoints",
                    }
                ]

                status, payload = self.decide(packet, "commit", claims)

                self.assertEqual(overseer_cli.EXIT_REFUSED, status, payload)
                self.assertFalse(payload["authorized"])
                self.assertEqual(
                    ["not_user_authored"],
                    [item["reason"] for item in payload["rejected_claims"]],
                )
                self.assertEqual([], self._porcelain_committed(fixtures.AUTHORIZED))


if __name__ == "__main__":
    unittest.main()
