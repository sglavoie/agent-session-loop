"""Tests for test asl native restore recovery diagnosis."""

from __future__ import annotations

import unittest
from typing import Any

from agent_session_loop import (
    restore_diagnosis,
)
from tests.support_asl_native_restore_recovery import (
    CONFIG_FILE_NAME_ERROR,
    METHOD_NAME_ERROR,
    SESSION_FILE_ERROR,
    THREAD_ID,
    UNRELATED_NO_SUCH_ERROR,
)

ASL_TEST_LANE = "integration"


class RestoreDiagnosisTests(unittest.TestCase):
    """The one vocabulary all three harnesses weigh a refusal against.

    The adapter claims above prove each transport asks the question; these prove
    what the answer is, on the inputs a transport test cannot reach -- a code
    and a message that disagree, a reason buried in ``data``, a provider that
    sent no structure at all.
    """

    def _rejection(self, **error: Any) -> restore_diagnosis.ProviderRejection:
        return restore_diagnosis.ProviderRejection.from_payload(error)

    def test_the_answers_both_providers_actually_give_are_diagnoses(self) -> None:
        for message in ("no such thread", "no such session"):
            with self.subTest(message=message):
                rejection = self._rejection(code=-32602, message=message)

                self.assertTrue(rejection.diagnoses_missing_history())

    def test_a_protocol_fixed_code_is_never_a_diagnosis(self) -> None:
        # Even saying the words. These codes are spent on faults in the request
        # or in the server, and the internal-error one is what a server wraps
        # every unhandled failure in -- a code that can mean anything may not
        # mean a conversation is gone.
        for code in sorted(restore_diagnosis.NON_DIAGNOSTIC_ERROR_CODES):
            with self.subTest(code=code):
                rejection = self._rejection(code=code, message="no such session")

                self.assertFalse(rejection.diagnoses_missing_history())

    def test_a_message_that_explains_itself_as_transient_is_not_a_diagnosis(
        self,
    ) -> None:
        rejection = self._rejection(
            code=-32000,
            message="no such session: authentication expired, retry after sign in",
        )

        self.assertFalse(rejection.diagnoses_missing_history())

    def test_the_reason_is_read_out_of_the_data_blob_as_well(self) -> None:
        rejection = self._rejection(
            code=-32001,
            message="session not found",
            data={"reason": "rate limit exceeded"},
        )

        self.assertFalse(rejection.diagnoses_missing_history())

    def test_the_data_blob_is_never_positive_evidence(self) -> None:
        # A provider that echoes the request into ``data`` would otherwise quote
        # the identifier in question back as proof that it is gone.
        rejection = self._rejection(
            code=-32001,
            message="the request failed",
            data={"detail": "no such session"},
        )

        self.assertFalse(rejection.diagnoses_missing_history())

    def test_an_unstructured_error_is_judged_on_the_words_it_is(self) -> None:
        rejection = restore_diagnosis.ProviderRejection.from_payload("no such session")

        self.assertIsNone(rejection.numeric_code)
        self.assertTrue(rejection.diagnoses_missing_history())

    def test_a_code_that_is_not_a_number_decides_nothing(self) -> None:
        # ``True == 1`` in Python, so a forged boolean would otherwise arrive as
        # a code, and a string code is a code this vocabulary cannot weigh.
        for code in (True, "SESSION_NOT_FOUND"):
            with self.subTest(code=code):
                rejection = self._rejection(code=code, message="the request failed")

                self.assertIsNone(rejection.numeric_code)
                self.assertFalse(rejection.diagnoses_missing_history())

    def test_the_rendering_keeps_everything_the_provider_sent(self) -> None:
        rejection = self._rejection(
            code=-32602, message="no such thread", data={"threadId": "thread-77"}
        )

        self.assertEqual(
            '[-32602] no such thread {"threadId":"thread-77"}', rejection.rendered
        )

    def test_the_prose_form_is_the_same_judgement(self) -> None:
        for line, expected in (
            (
                f"Error: No conversation found with session ID: {THREAD_ID}",
                True,
            ),
            (
                f"Error: could not reach the API while resuming {THREAD_ID}",
                False,
            ),
            (
                f"Error: permission denied for conversation {THREAD_ID}",
                False,
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    expected, restore_diagnosis.text_diagnoses_missing_history(line)
                )

    def test_something_else_being_absent_is_never_a_missing_history(self) -> None:
        # The words "no such" are a sentence about *something* being gone, and
        # almost everything a provider reports gone is not a conversation. None
        # of these is transient either, so nothing in the veto vocabulary could
        # have caught them: the subject has to be required, not excused.
        for line in (
            UNRELATED_NO_SUCH_ERROR["message"],
            "no such model: missing-model-id",
            "no such workspace: /workspace",
            "open /tmp/plan/state.json: no such file or directory",
            "no such tool: shell",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_a_verdict_belongs_to_the_subject_it_was_passed_about(self) -> None:
        # The same trap one clause further along: a message may name a session
        # and then report something else missing. The history noun appearing
        # somewhere in the sentence is not the history being disowned.
        for line in (
            "session/load failed: the settings file does not exist",
            f"while resuming thread {THREAD_ID}: the model endpoint was not found",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_a_history_noun_used_as_an_adjective_is_not_the_history(self) -> None:
        # The same trap with no clause boundary to give it away. English builds
        # compounds by stacking nouns, so every one of these puts "session" or
        # "thread" directly in front of the thing it is actually about -- a
        # config file, a worker pool, an RPC method, a storage backend. Naming
        # the history noun cannot be enough; it has to hold the subject position.
        for line in (
            SESSION_FILE_ERROR["message"],
            "thread pool not found",
            "no such session/load method",
            "unknown session storage backend",
            "session cache directory does not exist",
            "no such thread scheduler",
            "invalid conversation log format",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_punctuation_inside_a_name_is_not_the_end_of_the_sentence(self) -> None:
        # The same compound trap, written the way code writes compounds. A dot
        # or a colon joins two halves of one token in "session.load",
        # "session:load" and "session.json" -- and a dot is also how a sentence
        # ends, so reading one as a boundary reads "no such session" out of a
        # complaint about an RPC method. The clause a history noun owns has to
        # end where a word stops continuing it.
        for line in (
            METHOD_NAME_ERROR["message"],
            "unknown session.load method",
            "unknown session:load method",
            "no such session.json file",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_a_separator_alone_does_not_make_a_word_an_identifier(self) -> None:
        # The trap one layer in from the clause boundary: here the noun really is
        # followed by "not found", and the only question is whether the word
        # between them is this session's name. "config.json" and
        # "storage-backend" are compounds of ordinary words that happen to carry
        # the punctuation an identifier carries -- so a name has to look like a
        # minted one, which is to say it has to carry a digit.
        for line in (
            CONFIG_FILE_NAME_ERROR["message"],
            "session storage-backend not found",
            "thread work-queue does not exist",
        ):
            with self.subTest(line=line):
                self.assertFalse(restore_diagnosis.text_diagnoses_missing_history(line))

    def test_infrastructure_identifiers_are_not_native_history(self) -> None:
        for line in (
            "session config-v2.json not found",
            'session "config.json" not found',
            "session v2-backend does not exist",
            "no such session::load method",
            "no such session() method",
            "no such session[load] method",
            "pthread not found",
            "subsession not found",
            "session config99.toml not found",
            'session "v3-cache" was deleted',
        ):
            with self.subTest(line=line):
                self.assertFalse(
                    restore_diagnosis.ProviderRejection(
                        code=-32000, message=line
                    ).diagnoses_missing_history()
                )

    def test_the_ways_a_provider_says_the_history_itself_is_gone(self) -> None:
        # The other direction of the same rule: insisting on the subject may not
        # cost a genuine diagnosis. Each of these names the history and then
        # says it is gone, with and without the identifier in between.
        for line in (
            "no such thread",
            "no such session",
            "no conversation",
            "session not found",
            f"thread {THREAD_ID} was not found",
            "conversation does not exist",
            f'thread "{THREAD_ID}" was deleted',
            "conversation id=c-3 no longer exists",
            "unknown session",
            f"invalid thread {THREAD_ID}",
            "expired conversation",
            # The subject requirement is about what follows the noun, so these
            # pin the endings it has to keep accepting: punctuation that closes
            # the clause, a parenthesized identifier, and a predicate.
            "no such session.",
            f"no such conversation ({THREAD_ID})",
            "no such thread is available",
            # And the identifier requirement is about what a minted name looks
            # like, so these pin the ones it may not refuse along with the
            # compounds: a hyphenated name, a colon-introduced one, and a dotted
            # one, each carrying the digit an ordinary word does not.
            "session s-1 does not exist",
            f"no such session: {THREAD_ID}",
            "conversation v2.thread-9 was deleted",
        ):
            with self.subTest(line=line):
                self.assertTrue(restore_diagnosis.text_diagnoses_missing_history(line))


if __name__ == "__main__":
    unittest.main()
