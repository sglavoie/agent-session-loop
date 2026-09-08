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

import ast
import unittest
from pathlib import Path
from typing import get_args

from agent_session_loop import (
    assurance_draft,
    clock,
    mailbox,
    overseer_context,
    overseer_decisions,
    overseer_history,
    overseer_policy_client,
    run_prompts,
    supervisor_audit,
    task_packet,
)
from tests.support import assert_no_runner_import

PLAN = "/state/agent-session-loop/plan-abc"
OTHER_PLAN = "/state/agent-session-loop/plan-xyz"
PROJECT = "/home/dev/scripts"
OTHER_PROJECT = "/home/dev/api"
RUN = "20260301T090000000000Z-4242"
FINGERPRINT = "fp-0123456789abcdef"
DIGEST = "rd-fedcba9876543210"

EARLY = "2026-03-01T10:00:00+00:00"
LATE = "2026-03-01T12:00:00+00:00"
LATEST = "2026-03-01T14:00:00+00:00"

GRANT = overseer_policy_client.Grant(
    plan_dir=PLAN,
    access=overseer_policy_client.ACCESS_CONTEXT,
    request_digest=DIGEST,
    config_fingerprint=FINGERPRINT,
)

PLAN_SCOPE = overseer_decisions.Scope(project=PROJECT, plan_dir=PLAN, run_id=RUN)


def _excerpt(
    *,
    index: int = 0,
    role: str = "user",
    text: str = "commit the checkpoint and resume",
    timestamp: str = EARLY,
) -> overseer_history.Excerpt:
    return overseer_history.Excerpt(
        index=index,
        role=role,
        timestamp=timestamp,
        text=text,
        truncated=False,
    )


def _conversation(
    *,
    directory: str = PROJECT,
    session_id: str = "sess-1",
    session_class: str = overseer_decisions.SESSION_CLASS_MAIN,
    excerpts: tuple[overseer_history.Excerpt, ...] = (),
) -> overseer_history.Conversation:
    return overseer_history.Conversation(
        harness="claude",
        session_id=session_id,
        profiles=("default",),
        directory=directory,
        projects=("scripts",),
        path=f"{directory}/.transcripts/{session_id}.jsonl",
        started_at=EARLY,
        last_at=LATE,
        session_class=session_class,
        message_count=len(excerpts),
        truncated=False,
        excerpts=excerpts,
    )


def _history(
    *conversations: overseer_history.Conversation,
    allowed: bool = True,
    available: bool = True,
    request_digest: str = DIGEST,
) -> overseer_history.History:
    return overseer_history.History(
        plan_dir=PLAN,
        access=overseer_policy_client.ACCESS_CONTEXT,
        repositories=(),
        limits=overseer_history.Limits(),
        allowed=allowed,
        available=available,
        reason="opted_in" if allowed else "excluded",
        failure=None,
        summary="",
        config_fingerprint=FINGERPRINT,
        request_digest=request_digest,
        contract_version=overseer_history.HISTORY_CONTRACT_VERSION,
        exit_status=0,
        outcomes=(),
        examined=len(conversations),
        truncated=False,
        conversations=conversations,
        withheld=(),
        warnings=(),
    )


def _message(
    *,
    seq: int = 1,
    kind: str = mailbox.KIND_GUIDANCE,
    text: str = "commit the checkpoint and resume",
    created_at: str = EARLY,
) -> mailbox.MailboxMessage:
    return mailbox.MailboxMessage(
        seq=seq,
        id=f"msg-{seq}",
        kind=kind,
        created_at=created_at,
        text=text,
    )


def _direct_statement(
    *,
    statement_id: str = "transcript:claude:sess-1#0",
    voice: str = "user_instruction",
    scope: overseer_decisions.Scope | None = None,
    at: str = EARLY,
    text: str = "commit the checkpoint and resume",
) -> overseer_decisions.Statement:
    """One statement built directly, for shapes no evidence source produces."""
    return overseer_decisions.Statement(
        statement_id=statement_id,
        voice=voice,
        space=overseer_decisions.TRANSCRIPT_SPACE,
        locator="claude:sess-1",
        scope=scope if scope is not None else overseer_decisions.Scope(project=PROJECT),
        at=at,
        text=text,
        moment=clock.parse_datetime(at),
    )


def _claim(
    statement_id: str,
    *,
    topic: str = "operations",
    stance: str = "allow",
    operations: tuple[str, ...] = ("commit", "resume"),
    scope: overseer_decisions.Scope | None = None,
    corroborating: tuple[str, ...] = (),
    reading: str = "",
) -> overseer_decisions.Claim:
    return overseer_decisions.Claim(
        statement_id=statement_id,
        topic=topic,
        stance=stance,
        operations=operations,
        scope=scope if scope is not None else PLAN_SCOPE,
        corroborating=corroborating,
        reading=reading,
    )


class TranscriptProvenanceTests(unittest.TestCase):
    """A role is not an author, and this is where the two come apart."""

    def test_a_plain_user_message_is_a_direct_instruction(self) -> None:
        statements = overseer_decisions.statements_from_history(
            _history(_conversation(excerpts=(_excerpt(),)))
        )

        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0].voice, "user_instruction")
        self.assertTrue(statements[0].authorizing)
        self.assertEqual(statements[0].marker, "")

    def test_an_assistant_message_is_an_interpretation(self) -> None:
        statements = overseer_decisions.statements_from_history(
            _history(
                _conversation(
                    excerpts=(
                        _excerpt(role="assistant", text="so you want a checkpoint"),
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
                    _history(_conversation(excerpts=(_excerpt(role=role),)))
                )

                self.assertEqual(statements[0].voice, "unattributed")

    def test_a_role_is_read_case_and_space_insensitively(self) -> None:
        statements = overseer_decisions.statements_from_history(
            _history(_conversation(excerpts=(_excerpt(role="  User "),)))
        )

        self.assertEqual(statements[0].voice, "user_instruction")

    def test_every_generated_marker_disqualifies_a_user_message(self) -> None:
        """The fixture the session names: an automated prompt under ``user``."""
        for marker in overseer_decisions.GENERATED_PROMPT_MARKERS:
            with self.subTest(marker=marker):
                statements = overseer_decisions.statements_from_history(
                    _history(
                        _conversation(
                            excerpts=(
                                _excerpt(
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
            _history(
                _conversation(session_class="subagent", excerpts=(_excerpt(),)),
                _conversation(
                    session_id="sess-2",
                    session_class=overseer_decisions.SESSION_CLASS_MAIN,
                    excerpts=(_excerpt(),),
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
                    _history(
                        _conversation(
                            session_class=session_class, excerpts=(_excerpt(),)
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
                    _history(
                        _conversation(
                            session_class=session_class, excerpts=(_excerpt(),)
                        )
                    )
                )

                self.assertEqual(statements[0].voice, "unattributed")
                self.assertFalse(statements[0].authorizing)

    def test_a_class_is_read_through_surrounding_space(self) -> None:
        statements = overseer_decisions.statements_from_history(
            _history(_conversation(session_class="  main  ", excerpts=(_excerpt(),)))
        )

        self.assertEqual(statements[0].voice, "user_instruction")

    def test_an_automated_conversation_is_generated_without_a_marker(self) -> None:
        """The text is a plain instruction; the conversation is what refuses."""
        statements = overseer_decisions.statements_from_history(
            _history(
                _conversation(
                    session_class=overseer_decisions.SESSION_CLASS_AUTOMATED,
                    excerpts=(_excerpt(text="commit the checkpoint and resume"),),
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
                    _history(
                        _conversation(
                            session_class=session_class,
                            excerpts=(
                                _excerpt(role="assistant", text="so you want a commit"),
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
                    _history(
                        _conversation(
                            session_class=named,
                            excerpts=(_excerpt(),),
                        )
                    ),
                    generated_classes=(named,),
                )

                self.assertFalse(statements[0].authorizing)

    def test_naming_the_human_class_downgrades_it(self) -> None:
        """A restriction reaches ``main`` too, which is what makes it one."""
        statements = overseer_decisions.statements_from_history(
            _history(
                _conversation(
                    session_class=overseer_decisions.SESSION_CLASS_MAIN,
                    excerpts=(_excerpt(),),
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
                    _history(
                        _conversation(
                            session_class=session_class, excerpts=(_excerpt(),)
                        )
                    )
                )
                register = overseer_decisions.register(
                    statements=statements,
                    claims=tuple(_claim(item.statement_id) for item in statements),
                )
                ruling = overseer_decisions.resolve(
                    register=register,
                    request=overseer_decisions.Request(
                        operation="resume", scope=PLAN_SCOPE
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
            ("refused", _history(_conversation(excerpts=(_excerpt(),)), allowed=False)),
            (
                "unavailable",
                _history(_conversation(excerpts=(_excerpt(),)), available=False),
            ),
            (
                "unidentified",
                _history(_conversation(excerpts=(_excerpt(),)), request_digest=""),
            ),
        ):
            with self.subTest(answer=label):
                self.assertEqual(
                    overseer_decisions.statements_from_history(history), ()
                )

    def test_a_transcript_statement_is_scoped_to_its_project_only(self) -> None:
        statements = overseer_decisions.statements_from_history(
            _history(_conversation(excerpts=(_excerpt(),)))
        )

        self.assertEqual(statements[0].scope, overseer_decisions.Scope(project=PROJECT))
        self.assertEqual(statements[0].scope.plan_dir, "")
        self.assertEqual(statements[0].scope.run_id, "")

    def test_a_statement_is_clipped_and_says_so(self) -> None:
        long_text = "x" * (overseer_decisions.MAX_STATEMENT_CHARS + 10)

        statements = overseer_decisions.statements_from_history(
            _history(_conversation(excerpts=(_excerpt(text=long_text),)))
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
                supervisor_audit.AUDIT_PROMPT_TEMPLATE.splitlines()[0],
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


class SupervisionProvenanceTests(unittest.TestCase):
    """The mailbox records no author, so nothing in it is a user instruction."""

    def test_every_mailbox_kind_is_classified(self) -> None:
        self.assertEqual(
            set(overseer_decisions.SUPERVISION_VOICES),
            set(mailbox.MESSAGE_KINDS),
        )

    def test_no_mailbox_kind_can_authorize(self) -> None:
        for kind in mailbox.MESSAGE_KINDS:
            with self.subTest(kind=kind):
                statements = overseer_decisions.statements_from_supervision(
                    [_message(kind=kind)], grant=GRANT, scope=PLAN_SCOPE
                )

                self.assertFalse(statements[0].authorizing)
                self.assertNotEqual(statements[0].voice, "user_instruction")

    def test_the_instruction_shaped_kinds_are_relayed(self) -> None:
        for kind in (
            mailbox.KIND_GUIDANCE,
            mailbox.KIND_REPLY,
            mailbox.KIND_RESOLUTION,
        ):
            with self.subTest(kind=kind):
                statements = overseer_decisions.statements_from_supervision(
                    [_message(kind=kind)], grant=GRANT, scope=PLAN_SCOPE
                )

                self.assertEqual(statements[0].voice, "relayed_instruction")

    def test_a_worker_question_is_a_request(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [_message(kind=mailbox.KIND_QUESTION, text="may I commit?")],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "worker_request")

    def test_bookkeeping_is_a_machine_record(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [_message(kind=mailbox.KIND_RECEIPT, text="")],
            grant=GRANT,
            scope=PLAN_SCOPE,
        )

        self.assertEqual(statements[0].voice, "machine_record")

    def test_an_unknown_kind_is_unattributed(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [_message(kind="telepathy")], grant=GRANT, scope=PLAN_SCOPE
        )

        self.assertEqual(statements[0].voice, "unattributed")

    def test_a_relayed_machine_prompt_is_a_generated_prompt(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [_message(text=f"{overseer_decisions.TASK_PACKET_MARKER}\n\ncommit")],
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
                _message(
                    kind=mailbox.KIND_QUESTION,
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
                        [_message()], grant=grant, scope=scope
                    ),
                    (),
                )

    def test_a_supervision_statement_carries_the_plan_scope(self) -> None:
        statements = overseer_decisions.statements_from_supervision(
            [_message()], grant=GRANT, scope=PLAN_SCOPE
        )

        self.assertEqual(statements[0].scope, PLAN_SCOPE)
        self.assertEqual(statements[0].space, overseer_decisions.STATE_SPACE)
        self.assertTrue(statements[0].locator.startswith(mailbox.MAILBOX_FILE_NAME))


class ClaimValidationTests(unittest.TestCase):
    """A reading is checked against the record it claims to read."""

    def test_a_claim_on_a_user_instruction_becomes_a_decision(self) -> None:
        statements = overseer_decisions.statements_from_history(
            _history(_conversation(excerpts=(_excerpt(),)))
        )

        outcome = overseer_decisions.register(
            statements=statements,
            claims=[
                _claim(
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
            _history(
                _conversation(
                    excerpts=(_excerpt(), _excerpt(index=1, role="assistant"))
                )
            )
        )

        outcome = overseer_decisions.register(
            statements=statements,
            claims=[
                _claim(
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
            project=PROJECT, plan_dir=PLAN, run_id=RUN, incident="incident-1"
        )
        statements = (
            _direct_statement(statement_id="s-user"),
            _direct_statement(
                statement_id="s-assistant", voice="assistant_interpretation"
            ),
            _direct_statement(statement_id="s-undated", at="whenever"),
            _direct_statement(statement_id="s-narrow", scope=narrow),
        )
        cases: dict[str, overseer_decisions.Claim] = {
            "unknown_statement": _claim("s-missing"),
            "not_user_authored": _claim("s-assistant"),
            "undated": _claim("s-undated"),
            "scope_unnamed": _claim(
                "s-user", scope=overseer_decisions.Scope(project="")
            ),
            "scope_widened": _claim(
                "s-narrow",
                scope=overseer_decisions.Scope(
                    project=PROJECT, plan_dir=PLAN, run_id=RUN, incident="incident-2"
                ),
            ),
            "unknown_topic": _claim("s-user", topic="vibes"),
            "unknown_operation": _claim("s-user", operations=("rewrite-history",)),
            "operations_unnamed": _claim("s-user", operations=()),
            "operations_named": _claim(
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
            claims=[_claim("s-user"), _claim("s-user")],
        )
        self.assertEqual(len(duplicate.decisions), 1)
        self.assertEqual(duplicate.rejections[0].reason, "duplicate_claim")
        seen.add("duplicate_claim")

        self.assertEqual(seen, set(overseer_decisions.CLAIM_REJECTIONS))

    def test_no_voice_but_a_user_instruction_can_carry_a_decision(self) -> None:
        for voice in overseer_decisions.VOICES:
            with self.subTest(voice=voice):
                outcome = overseer_decisions.register(
                    statements=[_direct_statement(statement_id="s-1", voice=voice)],
                    claims=[_claim("s-1")],
                )

                if voice in overseer_decisions.AUTHORIZING_VOICES:
                    self.assertEqual(len(outcome.decisions), 1)
                else:
                    self.assertEqual(outcome.decisions, ())
                    self.assertEqual(outcome.rejections[0].reason, "not_user_authored")

    def test_one_statement_may_decide_two_topics(self) -> None:
        outcome = overseer_decisions.register(
            statements=[_direct_statement(statement_id="s-1")],
            claims=[
                _claim("s-1"),
                _claim("s-1", topic="acceptance", operations=()),
            ],
        )

        self.assertEqual(outcome.rejections, ())
        self.assertEqual(
            [item.topic for item in outcome.decisions], ["operations", "acceptance"]
        )

    def test_a_claim_may_narrow_its_statements_scope(self) -> None:
        outcome = overseer_decisions.register(
            statements=[
                _direct_statement(
                    statement_id="s-1",
                    scope=overseer_decisions.Scope(project=PROJECT),
                )
            ],
            claims=[
                _claim(
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
            statements=[_direct_statement(statement_id="s-1")],
            claims=[
                _claim("s-1", scope=overseer_decisions.Scope(project=OTHER_PROJECT))
            ],
        )

        self.assertEqual(outcome.rejections[0].reason, "scope_widened")

    def test_a_register_reports_the_rejections_for_one_statement(self) -> None:
        outcome = overseer_decisions.register(
            statements=[_direct_statement(statement_id="s-1", voice="worker_request")],
            claims=[_claim("s-1")],
        )

        self.assertEqual(len(outcome.rejected("s-1")), 1)
        self.assertIsNone(outcome.decision("s-1:operations"))


class HoldAndResumeTests(unittest.TestCase):
    """The session's first fixture: "commit then resume" against a hold."""

    def _register(self, *, hold_last: bool) -> overseer_decisions.Register:
        allow_at, hold_at = (EARLY, LATE) if hold_last else (LATE, EARLY)
        statements = overseer_decisions.statements_from_history(
            _history(
                _conversation(
                    excerpts=(
                        _excerpt(
                            index=0,
                            timestamp=allow_at,
                            text="commit the checkpoint and resume the run",
                        ),
                        _excerpt(
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
                _claim(allow.statement_id, reading="commit then resume"),
                _claim(hold.statement_id, stance="hold", operations=()),
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
                        operation=operation, scope=PLAN_SCOPE
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
            _history(
                _conversation(
                    excerpts=(
                        _excerpt(
                            index=0,
                            timestamp=EARLY,
                            text="a passing unit suite is enough to accept it",
                        ),
                        _excerpt(
                            index=1,
                            timestamp=LATE,
                            text="changed my mind: it also has to pass the linter",
                        ),
                        _excerpt(
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
                _claim(first.statement_id, topic="acceptance", operations=()),
                _claim(
                    second.statement_id,
                    topic="acceptance",
                    stance=second_stance,
                    operations=(),
                ),
                _claim(operational.statement_id, operations=("commit", "resume")),
            ],
        )

    def test_the_later_acceptance_governs_and_the_earlier_is_reported(self) -> None:
        settlement = overseer_decisions.settle(
            register=self._register(), topic="acceptance", scope=PLAN_SCOPE
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
                operation="commit", scope=PLAN_SCOPE, raises=("acceptance",)
            ),
        )

        self.assertTrue(ruling.authorized)
        self.assertEqual(ruling.unresolved, ())

    def test_withheld_acceptance_outranks_an_operational_allowance(self) -> None:
        ruling = overseer_decisions.resolve(
            register=self._register(second_stance="hold"),
            request=overseer_decisions.Request(
                operation="commit", scope=PLAN_SCOPE, raises=("acceptance",)
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


class PrecedentTests(unittest.TestCase):
    """The session's fourth fixture: an approval that was about something else."""

    def _elsewhere(
        self, *, scope: overseer_decisions.Scope, project: str = PROJECT
    ) -> overseer_decisions.Register:
        statements = overseer_decisions.statements_from_history(
            _history(
                _conversation(
                    directory=project,
                    session_id="sess-old",
                    excerpts=(
                        _excerpt(
                            timestamp=EARLY,
                            text="yes, commit the checkpoint and resume it",
                        ),
                    ),
                )
            )
        )
        return overseer_decisions.register(
            statements=statements,
            claims=[_claim(statements[0].statement_id, scope=scope)],
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
                project=PROJECT, plan_dir=PLAN, incident="incident-1"
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
            scope=overseer_decisions.Scope(project=PROJECT, plan_dir=OTHER_PLAN)
        )

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(operation="resume", scope=PLAN_SCOPE),
        )

        self.assertEqual(ruling.refusal, "unsupported_recovery")

    def test_a_target_that_names_no_incident_does_not_inherit_one(self) -> None:
        outcome = self._elsewhere(
            scope=overseer_decisions.Scope(
                project=PROJECT, plan_dir=PLAN, incident="incident-1"
            )
        )

        ruling = overseer_decisions.resolve(
            register=outcome,
            request=overseer_decisions.Request(
                operation="resume",
                scope=overseer_decisions.Scope(project=PROJECT, plan_dir=PLAN),
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
                operation="repair_the_application", scope=PLAN_SCOPE
            ),
        )

        self.assertEqual(ruling.refusal, "operation_unsupported")


class ScopeTests(unittest.TestCase):
    """Coverage is one rule, and an empty field is never a wildcard on a target."""

    def test_an_unnamed_project_covers_nothing(self) -> None:
        empty = overseer_decisions.Scope(project="")

        self.assertFalse(empty.covers(overseer_decisions.Scope(project=PROJECT)))
        self.assertFalse(overseer_decisions.Scope(project=PROJECT).covers(empty))

    def test_a_narrowed_field_requires_the_target_to_name_it(self) -> None:
        narrowed = overseer_decisions.Scope(project=PROJECT, run_id=RUN)

        self.assertTrue(narrowed.covers(PLAN_SCOPE))
        self.assertFalse(narrowed.covers(overseer_decisions.Scope(project=PROJECT)))

    def test_within_is_covers_read_from_the_other_side(self) -> None:
        broad = overseer_decisions.Scope(project=PROJECT)

        self.assertTrue(PLAN_SCOPE.within(broad))
        self.assertFalse(broad.within(PLAN_SCOPE))

    def test_comparison_is_exact_rather_than_by_containment(self) -> None:
        """A nested directory is a different project until somebody says so."""
        nested = overseer_decisions.Scope(project=f"{PROJECT}/agent_session_loop")

        self.assertFalse(overseer_decisions.Scope(project=PROJECT).covers(nested))


class VocabularyTests(unittest.TestCase):
    """Each closed vocabulary is published twice and must agree with itself."""

    def test_each_literal_matches_its_tuple(self) -> None:
        for name, literal, published in (
            ("Voice", overseer_decisions.Voice, overseer_decisions.VOICES),
            (
                "Operation",
                overseer_decisions.Operation,
                overseer_decisions.OPERATIONS,
            ),
            ("Topic", overseer_decisions.Topic, overseer_decisions.TOPICS),
            ("Stance", overseer_decisions.Stance, overseer_decisions.STANCES),
            (
                "RejectionReason",
                overseer_decisions.RejectionReason,
                overseer_decisions.CLAIM_REJECTIONS,
            ),
            ("Refusal", overseer_decisions.Refusal, overseer_decisions.REFUSALS),
        ):
            with self.subTest(vocabulary=name):
                self.assertEqual(set(get_args(literal)), set(published))
                self.assertEqual(len(set(published)), len(published))

    def test_only_a_user_instruction_authorizes(self) -> None:
        self.assertEqual(overseer_decisions.AUTHORIZING_VOICES, ("user_instruction",))
        self.assertTrue(
            set(overseer_decisions.AUTHORIZING_VOICES) <= set(overseer_decisions.VOICES)
        )

    def test_the_human_topics_are_topics_and_are_not_operational(self) -> None:
        self.assertTrue(
            set(overseer_decisions.HUMAN_TOPICS) <= set(overseer_decisions.TOPICS)
        )
        self.assertNotIn("operations", overseer_decisions.HUMAN_TOPICS)

    def test_the_public_surface_is_what_the_module_exports(self) -> None:
        for name in overseer_decisions.__all__:
            with self.subTest(name=name):
                self.assertTrue(hasattr(overseer_decisions, name))


class IsolationTests(unittest.TestCase):
    """The boundary has no way to reach a run, a file, or a process."""

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_decisions)

    def test_the_module_imports_nothing_that_touches_the_world(self) -> None:
        """Purity is asserted from the import graph rather than promised.

        A decision derived from evidence must be derived from the evidence it
        was handed. A module that could open a plan's state, run a subprocess or
        write a ledger row could also decide something the caller never showed
        it, so the absence of those imports is the property worth pinning.
        """
        source = Path(overseer_decisions.__file__ or "").read_text(encoding="utf-8")
        permitted = {
            "__future__",
            "collections.abc",
            "dataclasses",
            "datetime",
            "typing",
            "clock",
            "mailbox",
            "overseer_history",
            "overseer_policy_client",
        }
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module is not None:
                    imported.add(node.module)
                if node.level:
                    imported.update(alias.name for alias in node.names)

        self.assertEqual(imported - permitted, set())

    def test_nothing_here_is_convertible_to_permission(self) -> None:
        """A ruling is an answer about evidence, never a grant to act on."""
        for name in ("Ruling", "Decision", "Settlement", "Statement"):
            attribute = getattr(overseer_decisions, name)
            for forbidden in ("grant", "authorization", "permits"):
                with self.subTest(record=name, member=forbidden):
                    self.assertFalse(hasattr(attribute, forbidden))


if __name__ == "__main__":
    unittest.main()
