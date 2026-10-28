"""The codex app-server transport: the protocol client and the adapter seam.

Two levels, because the two failure families live in different places. The
session tests drive :class:`codex_app_server.CodexAppServerSession` against an
in-process fake whose every answer is scripted, so framing, correlation, and
the exact preconditions on a steer are asserted without a race. The adapter
tests spawn ``tests/fixtures/fake_codex_app_server.py`` for real, so process
ownership, the access profile crossing the wire, and the fallback boundary are
asserted against an actual pipe.

The invariant that gets the most attention here is the one with the sharpest
consequence: a turn is never executed twice. The client may fall back to
``codex exec`` only while ``turn/start`` has not *gone out*, and the tests below
hold both sides of that line -- including the case that separates going out from
being accepted, an app-server that reads the prompt and then dies with its
answer unsent.
"""

from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from agent_session_loop import (
    codex_app_server,
    guidance_delivery,
    harness_codex_transports,
)
from tests import support_asl_codex_app_server as fixtures

ASL_TEST_LANE = "integration"


class HandshakeTests(unittest.TestCase):
    """What the client says before a turn exists, and what it refuses to say."""

    def test_the_handshake_runs_in_order_and_carries_the_plan(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())

        session = fixtures.start_session(process)

        self.assertEqual(
            ["initialize", "initialized", "thread/start", "turn/start"],
            process.methods(),
        )
        thread_start = process.requests[2]["params"]
        self.assertEqual("workspace-write", thread_start["sandbox"])
        self.assertEqual("/work", thread_start["cwd"])
        turn_start = process.requests[3]["params"]
        self.assertEqual("thread-1", turn_start["threadId"])
        self.assertEqual("high", turn_start["effort"])
        self.assertEqual(
            [{"type": "text", "text": fixtures.PROMPT}],
            turn_start["input"],
        )
        self.assertTrue(session.turn_accepted)
        self.assertEqual("fake-model-1", session.observed_model)
        self.assertIsNone(session.fallback_reason)

    def test_a_sandbox_the_server_widened_stops_the_turn_before_it_starts(
        self,
    ) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(applied_sandbox="dangerFullAccess")
        )

        session = fixtures.start_session(process)

        self.assertNotIn("turn/start", process.methods())
        self.assertFalse(session.turn_accepted)
        self.assertIn("expected 'workspaceWrite'", str(session.protocol_error))
        # Refused before any work: the legacy transport, which states the
        # profile in argv, may still run this turn.
        self.assertIsNotNone(session.fallback_reason)

    def test_an_error_response_ends_the_conversation_with_a_fallback(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                thread_error={"code": -32603, "message": "no thread"}
            )
        )

        session = fixtures.start_session(process)

        self.assertIn("no thread", str(session.protocol_error))
        self.assertIsNotNone(session.fallback_reason)

    def test_a_server_that_exits_mid_handshake_is_not_waited_out(self) -> None:
        def die(process: fixtures._FakeProcess, message: dict[str, Any]) -> list[str]:
            process.returncode = 3
            return []

        process = fixtures._FakeProcess(die)

        started = time.monotonic()
        session = fixtures.start_session(
            process, request_timeout=fixtures.WAIT_SECONDS * 10
        )

        self.assertLess(time.monotonic() - started, fixtures.WAIT_SECONDS)
        self.assertIn("exited before answering", str(session.protocol_error))
        self.assertIsNotNone(session.fallback_reason)

    def test_a_handshake_request_that_is_never_answered_times_out(self) -> None:
        process = fixtures._FakeProcess(lambda process, message: [])

        session = fixtures.start_session(process, request_timeout=0.2)

        self.assertEqual("initialize timed out", session.protocol_error)
        self.assertIsNotNone(session.fallback_reason)

    def test_failed_and_cancelled_handshakes_terminate_a_server_ignoring_eof(
        self,
    ) -> None:
        def refuse(*args: Any) -> None:
            raise RuntimeError("operator cancelled submission")

        cases: list[tuple[str, Any, dict[str, Any], str, bool, bool]] = [
            (
                "request timeout",
                lambda process, message: [],
                {"request_timeout": 0},
                "protocol_error",
                True,
                False,
            ),
            (
                "sandbox mismatch",
                fixtures.scripted_responder(applied_sandbox="dangerFullAccess"),
                {},
                "protocol_error",
                True,
                False,
            ),
            (
                "missing conversation",
                fixtures.scripted_responder(
                    resume_error={"code": -32602, "message": "no such thread"}
                ),
                {"resume_conversation_id": "thread-77"},
                "restore_refused",
                False,
                False,
            ),
            (
                "conversation persistence failure",
                fixtures.scripted_responder(),
                {"on_conversation": refuse},
                "conversation_error",
                False,
                False,
            ),
            (
                "submission cancelled",
                fixtures.scripted_responder(),
                {"before_submit": refuse},
                "cancelled_before_submission",
                False,
                False,
            ),
            (
                "turn rejected after dispatch",
                fixtures.scripted_responder(
                    turn_error={"code": -32603, "message": "turn refused"}
                ),
                {},
                "protocol_error",
                False,
                True,
            ),
        ]
        for name, responder, kwargs, reason, fallback, dispatched in cases:
            with self.subTest(failure=name):
                process = fixtures._FakeProcess(responder, exit_on_stdin_close=False)
                session = codex_app_server.CodexAppServerSession(
                    process=process,  # type: ignore[arg-type]
                    plan=fixtures.worker_plan(),
                    prompt=fixtures.PROMPT,
                    shutdown_grace=0,
                    **kwargs,
                )
                process.session = session

                # All responses are synchronous and each handshake fails, so
                # driving directly checks teardown without a scheduling race.
                session._drive()

                self.assertTrue(process.stdin.closed)
                self.assertTrue(process.terminated)
                self.assertIsNotNone(process.poll())
                self.assertTrue(getattr(session, reason))
                self.assertEqual(fallback, session.fallback_reason is not None)
                self.assertEqual(dispatched, session.turn_dispatched)
                self.assertFalse(session.turn_accepted)


class FramingAndCorrelationTests(unittest.TestCase):
    """One byte stream, many messages, answered in whatever order they arrive."""

    def test_a_response_split_across_reads_is_reassembled(self) -> None:
        def fragmented(
            process: fixtures._FakeProcess, message: dict[str, Any]
        ) -> list[str]:
            whole = fixtures.scripted_responder()(process, message)
            if not whole:
                return []
            text = "".join(whole)
            third = max(1, len(text) // 3)
            return [text[:third], text[third : 2 * third], text[2 * third :]]

        process = fixtures._FakeProcess(fragmented)

        session = fixtures.start_session(process)

        self.assertTrue(session.turn_accepted)

    def test_responses_are_routed_by_id_not_by_arrival_order(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder(answer_steer=False))
        session = fixtures.start_session(process)
        outcomes: dict[int, guidance_delivery.GuidanceOutcome] = {}
        ready = threading.Barrier(3, timeout=fixtures.WAIT_SECONDS)

        def steer(index: int) -> None:
            ready.wait()
            outcomes[index] = session.steer(
                fixtures.guidance_request(f"guidance {index}")
            )

        threads = [threading.Thread(target=steer, args=(index,)) for index in (0, 1)]
        for thread in threads:
            thread.start()
        ready.wait()
        deadline = time.monotonic() + fixtures.WAIT_SECONDS
        while len(process.requests) < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        steer_ids = [
            int(message["id"])
            for message in process.requests
            if message.get("method") == "turn/steer"
        ]
        self.assertEqual(2, len(steer_ids), process.requests)
        # Answer the second request first, and make the two answers differ so a
        # client that matched on arrival order would swap them.
        session.feed(
            fixtures._line(
                {"id": steer_ids[1], "error": {"code": -32603, "message": "nope"}}
            )
            + fixtures._line({"id": steer_ids[0], "result": {"turnId": "turn-1"}})
        )
        for thread in threads:
            thread.join(timeout=fixtures.WAIT_SECONDS)

        delivered = [index for index, outcome in outcomes.items() if outcome.delivered]
        deferred = [
            index for index, outcome in outcomes.items() if not outcome.delivered
        ]
        self.assertEqual(1, len(delivered))
        self.assertEqual(1, len(deferred))
        # The delivered one is the thread whose request id was answered with a
        # result, not the one whose answer arrived first.
        sent_first = process.requests[4]["params"]["input"][0]["text"]
        expected_delivered = int(sent_first.rsplit(" ", 1)[1])
        self.assertEqual([expected_delivered], delivered)

    def test_an_answer_to_an_unknown_id_is_recorded_not_misrouted(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.feed(fixtures._line({"id": 9999, "result": {"turnId": "turn-1"}}))

        self.assertEqual("response to unknown request id 9999", session.protocol_error)
        # After acceptance a protocol error is never a licence to re-run.
        self.assertIsNone(session.fallback_reason)

    def test_a_server_to_client_request_is_a_protocol_error(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.feed(fixtures._line({"id": "req-1", "method": "approval/request"}))

        self.assertIn("unroutable", str(session.protocol_error))
        # Still answered, or an app-server waiting on it holds the turn open
        # until the harness timeout.
        reply = process.requests[-1]
        self.assertEqual("req-1", reply["id"])
        self.assertNotIn("method", reply)
        self.assertEqual(-32601, reply["error"]["code"])

    def test_text_that_is_not_protocol_passes_through_to_the_log(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        rendered = session.feed("warning: config file is deprecated\n")

        self.assertEqual("warning: config file is deprecated\n", rendered)


class OutputNormalizationTests(unittest.TestCase):
    """What a turn's stdout log says, and the one thing it must never say."""

    def setUp(self) -> None:
        self.process = fixtures._FakeProcess(fixtures.scripted_responder())
        self.session = fixtures.start_session(self.process)

    def test_deltas_stream_and_the_final_answer_is_captured(self) -> None:
        rendered = self.session.feed(
            fixtures.notification("item/agentMessage/delta", delta="think")
            + fixtures.notification("item/agentMessage/delta", delta="ing\n")
        )
        self.session.feed(
            fixtures.notification(
                "item/completed",
                item={
                    "type": "agentMessage",
                    "text": "the turn is done",
                    "phase": "final_answer",
                },
            )
        )

        self.assertEqual("thinking\n", rendered)
        self.assertEqual("the turn is done", self.session.final_message)

    def test_a_command_execution_is_rendered_with_its_exit_code(self) -> None:
        rendered = self.session.feed(
            fixtures.notification(
                "item/completed",
                item={
                    "type": "commandExecution",
                    "command": ["just", "test-fast"],
                    "exitCode": 1,
                },
            )
        )

        self.assertEqual("$ just test-fast (exit 1)\n", rendered)

    def test_the_echoed_prompt_and_guidance_never_reach_the_log(self) -> None:
        rendered = self.session.feed(
            fixtures.notification(
                "item/completed",
                item={
                    "type": "userMessage",
                    "text": fixtures.PROMPT,
                },
            )
            + fixtures.notification(
                "item/completed",
                item={
                    "type": "userMessage",
                    "text": fixtures.GUIDANCE_BODY,
                },
            )
        )

        self.assertEqual("", rendered)

    def test_a_completed_turn_is_terminal_and_reported(self) -> None:
        rendered = self.session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "completed"}
            )
        )

        self.assertEqual("turn completed\n", rendered)
        self.assertEqual(
            codex_app_server.TURN_STATUS_COMPLETED, self.session.turn_status
        )

    def test_an_unterminated_last_line_is_flushed_by_the_output_processor(
        self,
    ) -> None:
        # ``readline`` only hands over a line without its newline at EOF, so
        # that read is the app-server's last word and must not stay buffered.
        processor = harness_codex_transports._CodexAppServerOutputProcessor(
            self.session
        )
        completed = fixtures.notification(
            "turn/completed", turn={"id": "turn-1", "status": "completed"}
        )

        rendered = processor.process_chunk("stdout", completed.rstrip("\n"))
        prose = processor.process_chunk("stdout", "fatal: no auth.json")

        self.assertEqual("turn completed\n", rendered)
        self.assertTrue(processor.final_result_seen)
        self.assertEqual("fatal: no auth.json\n", prose)

    def test_a_failed_turn_carries_its_reason(self) -> None:
        rendered = self.session.feed(
            fixtures.notification(
                "turn/completed",
                turn={
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "the model refused"},
                },
            )
        )

        self.assertEqual("turn failed: the model refused\n", rendered)
        self.assertEqual("the model refused", self.session.protocol_error)
        self.assertIsNone(self.session.fallback_reason)

    def test_another_turns_completion_is_not_mistaken_for_this_one(self) -> None:
        self.session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-9", "status": "completed"}
            )
        )

        self.assertIsNone(self.session.turn_status)


if __name__ == "__main__":
    unittest.main()
