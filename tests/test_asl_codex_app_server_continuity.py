"""Test asl codex app server continuity."""

from __future__ import annotations

import unittest
from typing import Any

from tests import support_asl_codex_app_server as fixtures

ASL_TEST_LANE = "integration"


class LostSubmissionResponseTests(unittest.TestCase):
    """``turn/start`` left this process and nothing came back.

    The request that carries the prompt is the one request whose *write* decides
    what happened, because its answer can be lost in every way a protocol can
    lose one: never sent, refused, unintelligible. What the session owes a
    caller in all of those is the same, and it is not "nothing was submitted" --
    a caller holding an operator's correction acts on that by handing the words
    to somebody else.
    """

    def _session(self, process: fixtures._FakeProcess, **kwargs: Any) -> Any:
        return fixtures.start_lost_session(process, **kwargs)

    def test_the_prompt_is_reported_out_before_it_is_written(self) -> None:
        order: list[str] = []
        scripted = fixtures.scripted_responder()

        def responder(
            process: fixtures._FakeProcess, message: dict[str, Any]
        ) -> list[str]:
            if message.get("method") == "turn/start":
                order.append("written")
                process.returncode = 9
                return []
            return scripted(process, message)

        self._session(
            fixtures._FakeProcess(responder),
            on_submission_in_flight=lambda: order.append("in-flight"),
        )

        # Strictly before, not merely "also": the report has to be true even of
        # a write that fails halfway through, and a report made afterwards
        # cannot be.
        self.assertEqual(["in-flight", "written"], order)

    def test_a_lost_answer_is_not_a_prompt_that_stayed_home(self) -> None:
        submitted: list[str] = []
        in_flight: list[str] = []

        session = self._session(
            fixtures._FakeProcess(fixtures.lost_turn_response()),
            on_submitted=lambda: submitted.append("submitted"),
            on_submission_in_flight=lambda: in_flight.append("in-flight"),
        )

        # The turn failed -- there is no acceptance to report -- but the failure
        # is silence about a prompt that left, which is what the first sink says
        # and the second one must not be made to say.
        self.assertIsNotNone(session.protocol_error)
        self.assertEqual(["in-flight"], in_flight)
        self.assertEqual([], submitted)
        self.assertTrue(session.turn_dispatched)

    def test_a_refused_turn_is_still_a_prompt_the_server_read(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                turn_error={"code": -32602, "message": "turn/start refused"}
            )
        )

        session = self._session(process)

        # The server answered, and what it answered is "no" -- but it answered
        # *after* reading the prompt off the wire. Whether anything was done
        # with the words is the server's business and unknowable here.
        self.assertIsNotNone(session.protocol_error)
        self.assertTrue(session.turn_dispatched)
        self.assertFalse(session.turn_accepted)

    def test_an_unintelligible_answer_is_still_a_prompt_the_server_read(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(turn_result={"turn": {}})
        )

        session = self._session(process)

        self.assertIsNotNone(session.protocol_error)
        self.assertTrue(session.turn_dispatched)
        self.assertFalse(session.turn_accepted)

    def test_a_write_that_broke_partway_counts_as_a_prompt_that_left(self) -> None:
        """Why the report is made before the write rather than after it.

        A pipe that breaks during the write has already carried some of the
        prompt, and nothing on this side knows how much. The only reading that
        is never wrong is the one this makes: the words may be out there.
        """
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        original = process.stdin.write

        def write(line: str) -> int:
            if '"turn/start"' in line:
                process.returncode = 9
                raise BrokenPipeError("the app-server is gone")
            return original(line)

        process.stdin.write = write  # type: ignore[method-assign]

        session = self._session(process)

        self.assertIsNotNone(session.protocol_error)
        self.assertTrue(session.turn_dispatched)
        self.assertIsNone(session.fallback_reason)

    def test_a_handshake_that_never_reached_the_turn_dispatched_nothing(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(thread_error={"code": -1, "message": "boom"})
        )

        session = fixtures.start_session(process)

        # The other end of the same rule. Nothing about the prompt was written,
        # so this turn is free to be run again -- which is what keeps a failed
        # handshake cheap.
        self.assertFalse(session.turn_dispatched)
        self.assertIsNotNone(session.fallback_reason)


class ResumeTests(unittest.TestCase):
    """Restoring a conversation is exact, re-checked, and never approximated."""

    def resume(self, process: fixtures._FakeProcess, **kwargs: Any) -> Any:
        return fixtures.start_session(
            process, resume_conversation_id="thread-77", **kwargs
        )

    def test_a_resume_targets_the_named_thread_and_starts_none(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())

        session = self.resume(process)

        self.assertEqual(
            ["initialize", "initialized", "thread/resume", "turn/start"],
            process.methods(),
        )
        resume_params = process.requests[2]["params"]
        self.assertEqual("thread-77", resume_params["threadId"])
        # The plan still travels: a restored thread is told what this turn is
        # allowed to do rather than inheriting whatever it last ran under.
        self.assertEqual("workspace-write", resume_params["sandbox"])
        self.assertEqual("/work", resume_params["cwd"])
        self.assertEqual("thread-77", process.requests[3]["params"]["threadId"])
        self.assertEqual("thread-77", session.thread_id)
        self.assertTrue(session.turn_accepted)

    def test_a_thread_nobody_asked_for_is_refused(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(restored_thread_id="thread-other")
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("thread-other", str(session.protocol_error))
        self.assertIn("thread-77", str(session.protocol_error))

    def test_a_restored_thread_that_runs_elsewhere_is_refused(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(applied_settings={"cwd": "/somewhere/else"})
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("cwd", str(session.protocol_error))
        self.assertIn("/somewhere/else", str(session.protocol_error))

    def test_a_restored_approval_policy_that_drifted_is_refused(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                applied_settings={"approvalPolicy": "on-request"}
            )
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("approvalPolicy", str(session.protocol_error))

    def test_a_restored_sandbox_that_widened_is_refused(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(applied_sandbox="dangerFullAccess")
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("applied sandbox", str(session.protocol_error))

    def test_a_fresh_thread_is_not_held_to_the_resume_checks(self) -> None:
        """A ``thread/start`` echo cannot disagree about what it was told, so
        the extra checks would only ever fire on a server that answers with
        fields the client never sent."""
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(applied_settings={"cwd": None})
        )

        session = fixtures.start_session(process)

        self.assertTrue(session.turn_accepted)

    def test_a_canonicalised_model_is_reported_rather_than_refused(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(applied_model="gpt-5.1-codex-max")
        )

        session = self.resume(process)

        self.assertTrue(session.turn_accepted)
        self.assertEqual("gpt-5.1-codex-max", session.observed_model)

    def test_a_failed_resume_never_licenses_a_fresh_conversation(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                resume_error={"code": -32602, "message": "no such thread"}
            )
        )

        session = self.resume(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("no such thread", str(session.protocol_error))
        # The legacy transport starts a fresh session, so running it here would
        # answer "continue thread-77" with a turn that continues nothing.
        self.assertIsNone(session.fallback_reason)

    def test_no_fallback_survives_a_resume_even_before_the_thread_call(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(thread_error={"code": -1, "message": "boom"})
        )

        fresh = fixtures.start_session(
            fixtures._FakeProcess(
                fixtures.scripted_responder(
                    thread_error={"code": -1, "message": "boom"}
                )
            )
        )
        resumed = fixtures.start_session(process, resume_conversation_id="thread-77")

        # Same failure window, opposite verdicts: only the fresh start may be
        # re-run, because only it has nothing to lose by starting over.
        self.assertIsNotNone(fresh.fallback_reason)
        self.assertIsNone(resumed.fallback_reason)


class ConversationCaptureTests(unittest.TestCase):
    """The identifier is durable before the work exists, or there is no work."""

    def test_the_conversation_is_captured_before_the_turn_is_submitted(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        seen: list[tuple[str, list[str]]] = []

        session = fixtures.start_session(
            process,
            on_conversation=lambda native_id: seen.append(
                (native_id, process.methods())
            ),
        )

        self.assertEqual(1, len(seen))
        native_id, methods_at_capture = seen[0]
        self.assertEqual("thread-1", native_id)
        # The proof of ordering: no turn had been sent when the sink ran.
        self.assertNotIn("turn/start", methods_at_capture)
        self.assertTrue(session.turn_accepted)
        self.assertEqual("thread-1", session.thread_id)

    def test_a_resumed_conversation_is_captured_as_the_thread_it_restored(
        self,
    ) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        captured: list[str] = []

        fixtures.start_session(
            process,
            resume_conversation_id="thread-77",
            on_conversation=captured.append,
        )

        self.assertEqual(["thread-77"], captured)

    def test_a_capture_that_fails_submits_nothing(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())

        def refuse(native_id: str) -> None:
            raise RuntimeError(f"the ledger is unwritable for {native_id}")

        session = fixtures.start_refused_session(process, on_conversation=refuse)

        self.assertNotIn("turn/start", process.methods())
        self.assertIn("the ledger is unwritable", str(session.conversation_error))
        # Not a protocol failure, so it neither becomes one nor licenses a
        # legacy re-run of the work it just withheld.
        self.assertIsNone(session.protocol_error)
        self.assertIsNone(session.fallback_reason)
        self.assertTrue(process.stdin.closed)

    def test_a_gate_that_refuses_submits_nothing(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())

        def refuse() -> None:
            raise RuntimeError("the turn was cancelled")

        session = fixtures.start_refused_session(process, before_submit=refuse)

        self.assertNotIn("turn/start", process.methods())
        self.assertEqual("the turn was cancelled", session.cancelled_before_submission)
        self.assertIsNone(session.protocol_error)
        self.assertIsNone(session.fallback_reason)

    def test_the_gate_runs_after_the_conversation_is_recorded(self) -> None:
        """Ordered so a cancellation cannot cost the record of a thread that
        the app-server has already created."""
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        order: list[str] = []

        fixtures.start_session(
            process,
            on_conversation=lambda native_id: order.append("capture"),
            before_submit=lambda: order.append("gate"),
        )

        self.assertEqual(["capture", "gate"], order)


if __name__ == "__main__":
    unittest.main()
