"""Test asl codex app server interrupt."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from agent_session_loop import (
    codex_app_server,
    harnesses,
)
from tests import support_asl_codex_app_server as fixtures

ASL_TEST_LANE = "integration"


class InterruptTests(unittest.TestCase):
    """Stopping a turn is a thing said to the conversation, not just a kill."""

    def test_an_interrupt_names_the_thread_and_the_turn(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        self.assertTrue(session.interrupt())

        self.assertEqual("turn/interrupt", process.methods()[-1])
        params = process.requests[-1]["params"]
        self.assertEqual("thread-1", params["threadId"])
        self.assertEqual("turn-1", params["turnId"])
        self.assertTrue(session.interrupt_requested)

    def test_an_interrupt_before_a_turn_exists_sends_nothing(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = codex_app_server.CodexAppServerSession(
            process=process,  # type: ignore[arg-type]
            plan=fixtures.worker_plan(),
            prompt=fixtures.PROMPT,
        )

        self.assertFalse(session.interrupt())

        self.assertEqual([], process.methods())
        self.assertFalse(session.interrupt_requested)

    def test_an_interrupt_after_the_turn_is_over_sends_nothing(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)
        session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "completed"}
            )
        )

        self.assertFalse(session.interrupt())

        self.assertNotIn("turn/interrupt", process.methods())

    def test_a_refused_interrupt_is_not_a_protocol_error(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                interrupt_error={"code": -32600, "message": "no such turn"}
            )
        )
        session = fixtures.start_session(process)

        self.assertFalse(session.interrupt())

        # The turn is ending regardless. Recording an error here would restate
        # a cancelled turn as one the harness failed to carry out.
        self.assertIsNone(session.protocol_error)
        self.assertTrue(session.interrupt_requested)

    def test_an_interrupt_that_is_never_answered_is_bounded(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(answer_interrupt=False)
        )
        session = fixtures.start_session(process, interrupt_timeout=0.2)

        started = time.monotonic()
        self.assertFalse(session.interrupt())

        self.assertLess(time.monotonic() - started, fixtures.WAIT_SECONDS)
        self.assertIsNone(session.protocol_error)

    def test_an_interrupted_turn_is_not_a_failed_turn(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "interrupted"}
            )
        )

        self.assertTrue(session.turn_interrupted)
        self.assertEqual(codex_app_server.TURN_STATUS_INTERRUPTED, session.turn_status)
        self.assertIsNone(session.protocol_error)

    def test_a_failed_turn_still_reports_why_it_failed(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.feed(
            fixtures.notification(
                "turn/completed",
                turn={
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "the model gave up"},
                },
            )
        )

        self.assertFalse(session.turn_interrupted)
        self.assertEqual("the model gave up", session.protocol_error)

    def test_shutdown_interrupts_a_turn_that_is_still_running(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.shutdown()

        self.assertIn("turn/interrupt", process.methods())
        self.assertTrue(process.stdin.closed)

    def test_shutdown_after_a_finished_turn_interrupts_nothing(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)
        session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "completed"}
            )
        )

        session.shutdown()

        self.assertNotIn("turn/interrupt", process.methods())


class TurnPlanTests(unittest.TestCase):
    """The plan is what argv used to be, minus everything private."""

    def test_a_plan_payload_is_json_and_carries_no_prompt(self) -> None:
        invocation = harnesses.get_adapter("codex").build_invocation(
            cwd=Path("/work"),
            plan_dir=Path("/plan"),
            prompt=fixtures.PROMPT,
            last_message_path=Path("/work/last.txt"),
            model="gpt-5.1-codex",
            effort="high",
        )

        assert invocation.turn_plan is not None
        payload = invocation.turn_plan.to_payload()
        rendered = json.dumps(payload)
        self.assertNotIn(fixtures.PROMPT, rendered)
        self.assertNotIn(fixtures.PROMPT, json.dumps(invocation.argv))
        self.assertEqual("gpt-5.1-codex", payload["thread_params"]["model"])
        self.assertEqual("high", payload["turn_params"]["effort"])
        self.assertEqual(
            ["/plan"],
            payload["thread_params"]["config"]["sandbox_workspace_write"][
                "writable_roots"
            ],
        )

    def test_a_worker_turn_without_a_plan_dir_is_refused(self) -> None:
        with self.assertRaises(harnesses.HarnessError):
            harnesses.get_adapter("codex").build_invocation(
                cwd=Path("/work"),
                plan_dir=None,
                prompt=fixtures.PROMPT,
                last_message_path=Path("/work/last.txt"),
            )


if __name__ == "__main__":
    unittest.main()
