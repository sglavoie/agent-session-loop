# mypy: ignore-errors
from .asl_overseer_history_support import *
from .asl_overseer_history_support import (  # noqa: F401
    _CannedAmx,
    _conversation,
    _excerpt,
    _outcome,
    _payload,
    _read,
)


class HistoryMismatchTests(unittest.TestCase):
    """One good answer, mutated one field at a time."""

    def test_an_answer_to_another_question_is_refused(self) -> None:
        other = overseer_policy_client.Repository(
            alias="scripts", path="/home/dev/elsewhere"
        )
        wrong_path = _payload()
        wrong_path["data"]["repositories"][0] = _outcome(other, allowed=True)
        short_set = _payload()
        short_set["data"]["repositories"] = short_set["data"]["repositories"][:1]
        extra_set = _payload()
        extra_set["data"]["repositories"].append(_outcome(other, allowed=True))
        reordered = _payload()
        reordered["data"]["repositories"].reverse()
        inconsistent = _payload()
        inconsistent["data"]["repositories"][1]["allowed"] = False
        refused_with_evidence = _payload(allowed=False)
        refused_with_evidence["data"]["conversations"] = [_conversation()]

        cases = {
            "another plan": _payload(plan_dir="/state/agent-session-loop/other"),
            "another access category": _payload(
                access=overseer_policy_client.ACCESS_ACTION
            ),
            "another repository path": wrong_path,
            "a shorter repository set": short_set,
            "a longer repository set": extra_set,
            "a reordered repository set": reordered,
            "an allowance over a refused repository": inconsistent,
            "a refusal that still carried evidence": refused_with_evidence,
        }
        for label, payload in cases.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertFalse(history.allowed)
                self.assertFalse(history.available)
                self.assertEqual(history.conversations, ())

    def test_an_answer_taken_under_looser_bounds_is_not_the_answer(self) -> None:
        """A bound is part of the question. Reporting a larger one than was
        asked for means the evidence was gathered under a different request."""
        asked = overseer_history.Limits(conversations=2, messages=3, excerpt_chars=50)
        cases = {
            "more conversations": overseer_history.Limits(
                conversations=10, messages=3, excerpt_chars=50
            ),
            "more messages": overseer_history.Limits(
                conversations=2, messages=20, excerpt_chars=50
            ),
            "longer excerpts": overseer_history.Limits(
                conversations=2, messages=3, excerpt_chars=2000
            ),
        }
        for label, applied in cases.items():
            with self.subTest(label):
                history, _ = _read(payload=_payload(limits=applied), limits=asked)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertEqual(history.conversations, ())

    def test_evidence_exceeding_the_bounds_it_reports_is_refused(self) -> None:
        """The bounds must have been applied, not merely announced."""
        applied = overseer_history.Limits(
            conversations=1, messages=2, excerpt_chars=DEFAULT_LIMITS.excerpt_chars
        )
        too_many_conversations = _payload(
            limits=applied,
            conversations=[
                _conversation(session_id="sess-1"),
                _conversation(session_id="sess-9"),
            ],
        )
        too_many_messages = _payload(
            limits=applied,
            conversations=[
                _conversation(excerpts=[_excerpt(1), _excerpt(2), _excerpt(3)])
            ],
        )
        too_long_excerpt = _payload(
            limits=overseer_history.Limits(
                conversations=1, messages=2, excerpt_chars=4
            ),
            conversations=[_conversation(excerpts=[_excerpt(1, text="far too long")])],
        )

        for label, payload in {
            "more conversations than the bound": too_many_conversations,
            "more excerpts than the bound": too_many_messages,
            "a longer excerpt than the bound": too_long_excerpt,
        }.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertEqual(history.conversations, ())

    def test_a_conversation_without_a_working_directory_is_refused(self) -> None:
        """The directory is what was authorized. Evidence that cannot say where
        it came from is evidence nothing decided about."""
        payload = _payload(conversations=[_conversation(directory="")])
        history, _ = _read(payload=payload)
        self.assertEqual(history.failure, "answer_mismatch")
        self.assertEqual(history.conversations, ())

    def test_a_refusal_that_locates_what_it_withheld_is_rejected(self) -> None:
        """The path, the directory, and the project are exactly what an
        exclusion withholds. An answer that reports them under the heading of
        explaining a refusal is not read past."""
        for field in overseer_history.WITHHELD_FORBIDDEN_FIELDS:
            with self.subTest(field):
                payload = _payload()
                payload["data"]["withheld"][0][field] = "/home/dev/private"
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "answer_mismatch")
                self.assertFalse(history.available)
                self.assertEqual(history.conversations, ())
                self.assertEqual(history.withheld, ())
                self.assertIn("withheld", history.summary)


class HistoryMalformedAnswerTests(unittest.TestCase):
    """One unusable part rejects the whole answer rather than shrinking it."""

    def test_an_unreadable_conversation_rejects_the_whole_answer(self) -> None:
        """Dropping it would turn a broken answer into a smaller-looking good
        one, and "fewer conversations than there are" is indistinguishable from
        a legitimate bound."""
        cases: dict[str, Any] = {}
        nameless = _payload()
        nameless["data"]["conversations"][0]["id"] = ""
        cases["a conversation with no session id"] = nameless
        not_an_object = _payload()
        not_an_object["data"]["conversations"] = ["sess-1"]
        cases["a conversation that is not an object"] = not_an_object
        bad_excerpts = _payload()
        bad_excerpts["data"]["conversations"][0]["excerpts"] = [{"role": "user"}]
        cases["an excerpt with no text"] = bad_excerpts
        negative_index = _payload()
        negative_index["data"]["conversations"][0]["excerpts"] = [
            {"index": -1, "role": "user", "text": "hi", "truncated": False}
        ]
        cases["an excerpt with no place in the conversation"] = negative_index

        for label, payload in cases.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "response_malformed")
                self.assertEqual(history.conversations, ())
                self.assertIsNone(history.grant())

    def test_an_unreadable_withheld_entry_rejects_the_whole_answer(self) -> None:
        payload = _payload()
        payload["data"]["withheld"][1]["reason"] = ""
        history, _ = _read(payload=payload)
        self.assertEqual(history.failure, "response_malformed")
        self.assertEqual(history.withheld, ())

    def test_an_answer_without_readable_bounds_is_refused(self) -> None:
        """Without them there is nothing to check the evidence against, which is
        the same as having no bounds at all."""
        missing = _payload()
        del missing["data"]["limits"]
        zeroed = _payload()
        zeroed["data"]["limits"]["messages"] = 0
        for label, payload in {
            "no limits block": missing,
            "a zero bound": zeroed,
        }.items():
            with self.subTest(label):
                history, _ = _read(payload=payload)
                self.assertEqual(history.failure, "response_malformed")
                self.assertEqual(history.conversations, ())

    def test_a_descriptive_list_may_be_absent_without_failing_the_answer(self) -> None:
        """Profiles, projects, and warnings are descriptive; nothing is
        authorized on them, so an older ``amx`` that omits one still answers."""
        payload = _payload()
        del payload["data"]["warnings"]
        del payload["data"]["conversations"][0]["profiles"]
        del payload["data"]["conversations"][0]["projects"]
        history, _ = _read(payload=payload)
        self.assertTrue(history.allowed)
        self.assertEqual(history.warnings, ())
        self.assertEqual(history.conversations[0].profiles, ())
        self.assertEqual(history.conversations[0].projects, ())
