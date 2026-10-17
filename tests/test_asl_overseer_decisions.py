"""Provenance and supersession: who decided, over what, and what replaced it.

Nothing here touches a filesystem, a subprocess or a ledger. The module under
test is a pure check on the host agent's readings, so every test builds its
evidence by hand and asserts what the checks refuse.

Four situations shape the file, and each one is a way a plausible reading turns
out to be wrong.

*A hold and a "commit then resume" are the same shape and opposite decisions.*
Both are user instructions in the same conversation about the same project, and
the only thing separating them is which came last. So the pair is asserted in
both orders: a hold after an allowance refuses the resume and names the
allowance it superseded, and an allowance after a hold authorizes it and names
the hold. A hold naming no operation is asserted against every operation there
is, because "stop touching this run" that had to list what it stopped would be
a hold with holes in it.

*An acceptance decision that changed.* The later one governs, the earlier is
reported rather than dropped, and acceptance withheld leaves the topic
unsettled -- an operational allowance cannot outvote it, and a decision that
was made about acceptance can never be spent on an operation.

*A machine prompt under the ``user`` role.* Every marker ASL publishes is
asserted against the module that publishes it, so a reworded prompt fails here
instead of turning a task packet into an instruction. A claim resting on one is
rejected outright, not weighted down.

*A machine's own words under the ``user`` role.* A marker catches what ASL
composed and nothing an orchestrating agent typed itself, so the conversation's
class is asserted separately and ahead of the text -- with no flags set, since
a safeguard that needs remembering is the defect rather than the fix. Only
``main`` authorizes; ``automated`` is a generated prompt, ``unknown``, an
absent class and an unrecognized spelling are unattributable, and every value a
caller can pass restricts further and never the reverse.

*An approval from somewhere else.* An approval in another project, another
plan, or another incident registers fine -- it was a real decision -- and is
reported as a precedent while the ruling refuses. Suggesting a recovery and
authorizing one are kept apart in the type, not in the prose.
"""

from __future__ import annotations

import unittest
from typing import get_args

from agent_session_loop import (
    assurance_draft,
    overseer_context,
    overseer_decisions,
    run_prompts,
    supervisor_audit_agent,
    task_packet,
)
from tests import support_asl_overseer_decisions as fixtures
from tests.support_asl_overseer_decisions import PLAN_SCOPE, PROJECT


class TranscriptProvenanceTests(unittest.TestCase):
    """A role is not an author, and this is where the two come apart."""

    def test_a_plain_user_message_is_a_direct_instruction(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(fixtures._conversation(excerpts=(fixtures._excerpt(),)))
        )

        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0].voice, "user_instruction")
        self.assertTrue(statements[0].authorizing)
        self.assertEqual(statements[0].marker, "")

    def test_an_assistant_message_is_an_interpretation(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    excerpts=(
                        fixtures._excerpt(
                            role="assistant", text="so you want a checkpoint"
                        ),
                    )
                )
            )
        )

        self.assertEqual(statements[0].voice, "assistant_interpretation")
        self.assertFalse(statements[0].authorizing)

    def test_an_unrecognized_role_is_unattributed(self) -> None:
        for role in ("system", "tool", "", "USER-ish"):
            with self.subTest(role=role):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(excerpts=(fixtures._excerpt(role=role),))
                    )
                )

                self.assertEqual(statements[0].voice, "unattributed")

    def test_a_role_is_read_case_and_space_insensitively(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(excerpts=(fixtures._excerpt(role="  User "),))
            )
        )

        self.assertEqual(statements[0].voice, "user_instruction")

    def test_every_generated_marker_disqualifies_a_user_message(self) -> None:
        """The fixture the session names: an automated prompt under ``user``."""
        for marker in overseer_decisions.GENERATED_PROMPT_MARKERS:
            with self.subTest(marker=marker):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(
                            excerpts=(
                                fixtures._excerpt(
                                    text=(
                                        f"{marker}\n\nnow commit the checkpoint "
                                        "and resume the run"
                                    )
                                ),
                            )
                        )
                    )
                )

                self.assertEqual(statements[0].voice, "generated_prompt")
                self.assertEqual(statements[0].marker, marker)
                self.assertFalse(statements[0].authorizing)

    def test_a_marker_is_found_after_a_base_prompt(self) -> None:
        """ASL appends its context blocks, so a prefix test would miss them."""
        text = (
            "continue the session\n\n"
            f"{overseer_decisions.REPLY_CONTEXT_MARKER}\n\n"
            f"{overseer_decisions.REPLY_ENVELOPE_MARKER}\n"
        )

        self.assertEqual(
            overseer_decisions.generated_marker(text),
            overseer_decisions.REPLY_CONTEXT_MARKER,
        )

    def test_a_named_machine_class_downgrades_every_user_message(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    session_class="subagent",
                    excerpts=(fixtures._excerpt(),),
                ),
                fixtures._conversation(
                    session_id="sess-2",
                    session_class=overseer_decisions.SESSION_CLASS_MAIN,
                    excerpts=(fixtures._excerpt(),),
                ),
            ),
            generated_classes=("subagent",),
        )

        self.assertEqual(
            [item.voice for item in statements],
            ["generated_prompt", "user_instruction"],
        )


class SessionClassProvenanceTests(unittest.TestCase):
    """The fixture the finding names: an automated conversation, no flags set.

    AgentMux already says which conversations a person started. The defect was
    that saying so changed nothing unless the host remembered a flag, which
    made a provenance boundary an opt-in safeguard. These tests are written
    against the *default* call, because the default is what was wrong.
    """

    def test_only_a_main_conversation_speaks_for_a_person_by_default(self) -> None:
        expected = {
            overseer_decisions.SESSION_CLASS_MAIN: "user_instruction",
            overseer_decisions.SESSION_CLASS_AUTOMATED: "generated_prompt",
            overseer_decisions.SESSION_CLASS_UNKNOWN: "unattributed",
        }
        for session_class, voice in expected.items():
            with self.subTest(session_class=session_class):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(
                            session_class=session_class,
                            excerpts=(fixtures._excerpt(),),
                        )
                    )
                )

                self.assertEqual(statements[0].voice, voice)
                self.assertEqual(
                    statements[0].authorizing, session_class == "main", session_class
                )

    def test_an_unclassifiable_conversation_authorizes_nothing(self) -> None:
        """Absent, unrecognized and merely differently spelled all refuse.

        An empty class is what an older ``amx`` answer carries and a spelling
        this build does not know is what a newer one could carry. Neither is a
        person, and reading either as one is how the boundary would reopen.
        """
        for session_class in ("", "   ", "interactive", "Main", "MAIN", "subagent"):
            with self.subTest(session_class=session_class):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(
                            session_class=session_class,
                            excerpts=(fixtures._excerpt(),),
                        )
                    )
                )

                self.assertEqual(statements[0].voice, "unattributed")
                self.assertFalse(statements[0].authorizing)

    def test_a_class_is_read_through_surrounding_space(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    session_class="  main  ",
                    excerpts=(fixtures._excerpt(),),
                )
            )
        )

        self.assertEqual(statements[0].voice, "user_instruction")

    def test_an_automated_conversation_is_generated_without_a_marker(self) -> None:
        """The text is a plain instruction; the conversation is what refuses."""
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    session_class=overseer_decisions.SESSION_CLASS_AUTOMATED,
                    excerpts=(
                        fixtures._excerpt(text="commit the checkpoint and resume"),
                    ),
                )
            )
        )

        self.assertEqual(overseer_decisions.generated_marker(statements[0].text), "")
        self.assertEqual(statements[0].voice, "generated_prompt")

    def test_an_assistant_turn_keeps_its_voice_in_any_conversation(self) -> None:
        """Authorship answers a question only the ``user`` role raises."""
        for session_class in overseer_decisions.SESSION_CLASSES:
            with self.subTest(session_class=session_class):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(
                            session_class=session_class,
                            excerpts=(
                                fixtures._excerpt(
                                    role="assistant", text="so you want a commit"
                                ),
                            ),
                        )
                    )
                )

                self.assertEqual(statements[0].voice, "assistant_interpretation")
                self.assertFalse(statements[0].authorizing)

    def test_generated_classes_can_only_restrict(self) -> None:
        """No value a caller passes turns a non-human class into a human one."""
        for named in (
            overseer_decisions.SESSION_CLASS_AUTOMATED,
            overseer_decisions.SESSION_CLASS_UNKNOWN,
            "",
            "interactive",
        ):
            with self.subTest(generated_class=named):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(
                            session_class=named,
                            excerpts=(fixtures._excerpt(),),
                        )
                    ),
                    generated_classes=(named,),
                )

                self.assertFalse(statements[0].authorizing)

    def test_naming_the_human_class_downgrades_it(self) -> None:
        """A restriction reaches ``main`` too, which is what makes it one."""
        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(
                    session_class=overseer_decisions.SESSION_CLASS_MAIN,
                    excerpts=(fixtures._excerpt(),),
                )
            ),
            generated_classes=(overseer_decisions.SESSION_CLASS_MAIN,),
        )

        self.assertEqual(statements[0].voice, "generated_prompt")

    def test_the_published_classes_match_agentmux(self) -> None:
        """The vocabulary is restated, so a drift has to fail somewhere."""
        self.assertEqual(
            overseer_decisions.SESSION_CLASSES,
            ("main", "automated", "unknown"),
        )
        self.assertEqual(
            overseer_decisions.HUMAN_SESSION_CLASSES,
            (overseer_decisions.SESSION_CLASS_MAIN,),
        )
        self.assertEqual(
            set(overseer_decisions.AUTHORSHIPS),
            set(get_args(overseer_decisions.Authorship)),
        )

    def test_an_automated_run_cannot_authorize_a_resume(self) -> None:
        """The whole path, since a voice is only worth what a ruling does.

        The probe in the finding stopped at ``authorized=True``; this asserts
        the same call with each class, so a future change that fixes the voice
        while leaving the ruling permissive still fails.
        """
        for session_class in overseer_decisions.SESSION_CLASSES:
            with self.subTest(session_class=session_class):
                statements = overseer_decisions.statements_from_history(
                    fixtures._history(
                        fixtures._conversation(
                            session_class=session_class,
                            excerpts=(fixtures._excerpt(),),
                        )
                    )
                )
                register = overseer_decisions.register(
                    statements=statements,
                    claims=tuple(
                        fixtures._claim(item.statement_id) for item in statements
                    ),
                )
                ruling = overseer_decisions.resolve(
                    register=register,
                    request=overseer_decisions.Request(
                        operation="resume",
                        scope=PLAN_SCOPE,
                    ),
                )

                self.assertEqual(ruling.authorized, session_class == "main")
                if not ruling.authorized:
                    self.assertEqual(
                        [item.reason for item in register.rejections],
                        ["not_user_authored"],
                    )

    def test_unpermitted_evidence_yields_no_statements(self) -> None:
        for label, history in (
            (
                "refused",
                fixtures._history(
                    fixtures._conversation(excerpts=(fixtures._excerpt(),)),
                    allowed=False,
                ),
            ),
            (
                "unavailable",
                fixtures._history(
                    fixtures._conversation(excerpts=(fixtures._excerpt(),)),
                    available=False,
                ),
            ),
            (
                "unidentified",
                fixtures._history(
                    fixtures._conversation(excerpts=(fixtures._excerpt(),)),
                    request_digest="",
                ),
            ),
        ):
            with self.subTest(answer=label):
                self.assertEqual(
                    overseer_decisions.statements_from_history(history), ()
                )

    def test_a_transcript_statement_is_scoped_to_its_project_only(self) -> None:
        statements = overseer_decisions.statements_from_history(
            fixtures._history(fixtures._conversation(excerpts=(fixtures._excerpt(),)))
        )

        self.assertEqual(
            statements[0].scope,
            overseer_decisions.Scope(project=PROJECT),
        )
        self.assertEqual(statements[0].scope.plan_dir, "")
        self.assertEqual(statements[0].scope.run_id, "")

    def test_a_statement_is_clipped_and_says_so(self) -> None:
        long_text = "x" * (overseer_decisions.MAX_STATEMENT_CHARS + 10)

        statements = overseer_decisions.statements_from_history(
            fixtures._history(
                fixtures._conversation(excerpts=(fixtures._excerpt(text=long_text),))
            )
        )

        self.assertEqual(
            len(statements[0].text), overseer_decisions.MAX_STATEMENT_CHARS
        )
        self.assertTrue(statements[0].truncated)


class PublishedMarkerTests(unittest.TestCase):
    """The markers are copies, so their originals are the test."""

    def test_each_marker_matches_the_module_that_publishes_it(self) -> None:
        for name, published in (
            (
                overseer_decisions.TASK_PACKET_MARKER,
                task_packet.TASK_PACKET_HEADING_PREFIX,
            ),
            (
                overseer_decisions.MAILBOX_PROTOCOL_MARKER,
                run_prompts.MAILBOX_PROTOCOL_HEADING,
            ),
            (
                overseer_decisions.GUIDANCE_ENVELOPE_MARKER,
                run_prompts.MAILBOX_GUIDANCE_BEGIN,
            ),
            (
                overseer_decisions.REPLY_CONTEXT_MARKER,
                run_prompts.REPLY_CONTEXT_HEADING,
            ),
            (
                overseer_decisions.REPLY_ENVELOPE_MARKER,
                run_prompts.REPLY_MESSAGE_BEGIN,
            ),
            (
                overseer_decisions.RESOLUTION_CONTEXT_MARKER,
                run_prompts.RESOLUTION_CONTEXT_HEADING,
            ),
            (
                overseer_decisions.RESOLUTION_ENVELOPE_MARKER,
                run_prompts.RESOLUTION_MESSAGE_BEGIN,
            ),
            (
                overseer_decisions.AUDIT_PROMPT_MARKER,
                supervisor_audit_agent.AUDIT_PROMPT_TEMPLATE.splitlines()[0],
            ),
            (
                overseer_decisions.DRAFT_PROMPT_MARKER,
                assurance_draft.DRAFT_PROMPT_TEMPLATE.splitlines()[0],
            ),
        ):
            with self.subTest(marker=name):
                self.assertEqual(name, published)

    def test_the_state_space_matches_the_packet_builders(self) -> None:
        self.assertEqual(overseer_decisions.STATE_SPACE, overseer_context.STATE_SPACE)

    def test_a_text_with_no_marker_names_none(self) -> None:
        self.assertEqual(
            overseer_decisions.generated_marker("please commit and resume"), ""
        )


if __name__ == "__main__":
    unittest.main()
