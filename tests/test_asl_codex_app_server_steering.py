"""Test asl codex app server steering."""

from __future__ import annotations

import time
import unittest

from agent_session_loop import (
    codex_app_server,
    guidance_delivery,
)
from tests import support_asl_codex_app_server as fixtures


class SteeringTests(unittest.TestCase):
    """Same-turn delivery, and every way it is allowed to fail."""

    def test_a_successful_steer_states_the_thread_and_the_expected_turn(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        outcome = session.steer(fixtures.guidance_request())

        self.assertTrue(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_DELIVERED, outcome.reason)
        params = process.requests[-1]["params"]
        self.assertEqual("thread-1", params["threadId"])
        self.assertEqual("turn-1", params["expectedTurnId"])
        self.assertEqual(
            [{"type": "text", "text": fixtures.GUIDANCE_BODY}],
            params["input"],
        )

    def test_a_turn_that_cannot_be_steered_defers_without_quoting_the_body(
        self,
    ) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                steer_error={"code": -32603, "message": "activeTurnNotSteerable"}
            )
        )
        session = fixtures.start_session(process)

        outcome = session.steer(fixtures.guidance_request())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertEqual(codex_app_server.ACTIVE_TURN_NOT_STEERABLE, outcome.detail)
        self.assertNotIn(fixtures.GUIDANCE_BODY, str(outcome.detail))

    def test_a_turn_that_moved_on_defers_as_stale(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(
                steer_error={
                    "code": -32600,
                    "message": "expected active turn id `turn-1` but found `turn-2`",
                }
            )
        )
        session = fixtures.start_session(process)

        outcome = session.steer(fixtures.guidance_request())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertEqual("expected turn turn-1 is no longer active", outcome.detail)

    def test_a_steer_that_is_never_answered_defers_on_its_own_deadline(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder(answer_steer=False))
        session = fixtures.start_session(process, steer_timeout=0.2)

        started = time.monotonic()
        outcome = session.steer(fixtures.guidance_request())

        self.assertLess(time.monotonic() - started, fixtures.WAIT_SECONDS)
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertEqual("turn/steer timed out", outcome.detail)

    def test_a_steer_before_a_turn_exists_defers_with_no_active_turn(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder(answer_steer=False))
        session = codex_app_server.CodexAppServerSession(
            process=process,  # type: ignore[arg-type]
            plan=fixtures.worker_plan(),
            prompt=fixtures.PROMPT,
        )

        outcome = session.steer(fixtures.guidance_request())

        self.assertEqual(guidance_delivery.REASON_NO_ACTIVE_TURN, outcome.reason)
        self.assertEqual([], process.requests)

    def test_a_steer_after_the_turn_completed_defers_as_stale(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)
        session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "completed"}
            )
        )

        outcome = session.steer(fixtures.guidance_request())

        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        self.assertNotIn(
            "turn/steer", [message.get("method") for message in process.requests]
        )

    def test_a_steer_never_raises_when_the_pipe_is_gone(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)
        process.stdin.closed = True

        outcome = session.steer(fixtures.guidance_request())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertNotIn(fixtures.GUIDANCE_BODY, str(outcome.detail))


class ShutdownTests(unittest.TestCase):
    """Letting go of the conversation, on both the calm and the wedged path."""

    def test_shutdown_closes_stdin_and_lets_the_server_exit(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "completed"}
            )
        )
        session.shutdown()

        self.assertTrue(process.stdin.closed)
        self.assertFalse(process.terminated)
        self.assertEqual(0, process.returncode)

    def test_a_server_that_will_not_exit_is_terminated_after_the_grace(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(), exit_on_stdin_close=False
        )
        session = fixtures.start_session(process, shutdown_grace=0.2)

        session.feed(
            fixtures.notification(
                "turn/completed", turn={"id": "turn-1", "status": "completed"}
            )
        )
        session.shutdown()
        deadline = time.monotonic() + fixtures.WAIT_SECONDS
        while not process.terminated and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertTrue(process.terminated)

    def test_a_steer_after_shutdown_defers_instead_of_writing(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)
        session.shutdown()

        outcome = session.steer(fixtures.guidance_request())

        self.assertFalse(outcome.delivered)


class FallbackBoundaryTests(unittest.TestCase):
    """The exact line between "never started" and "must not be repeated"."""

    def test_a_failure_before_acceptance_offers_a_fallback(self) -> None:
        process = fixtures._FakeProcess(
            fixtures.scripted_responder(thread_error={"code": -1, "message": "boom"})
        )

        session = fixtures.start_session(process)

        self.assertFalse(session.turn_accepted)
        self.assertFalse(session.work_observed)
        self.assertIsNotNone(session.fallback_reason)

    def test_acceptance_forecloses_the_fallback_forever(self) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder())
        session = fixtures.start_session(process)

        session.feed("not json at all\n")
        session.feed(fixtures._line({"id": 4242, "result": {}}))

        self.assertIsNotNone(session.protocol_error)
        self.assertIsNone(session.fallback_reason)

    def test_emitted_work_forecloses_the_fallback_even_without_acceptance(
        self,
    ) -> None:
        process = fixtures._FakeProcess(fixtures.scripted_responder(answer_steer=False))
        session = codex_app_server.CodexAppServerSession(
            process=process,  # type: ignore[arg-type]
            plan=fixtures.worker_plan(),
            prompt=fixtures.PROMPT,
        )

        session.feed(
            fixtures.notification("item/agentMessage/delta", delta="already working")
        )
        session.feed(fixtures._line({"id": 4242, "result": {}}))

        self.assertTrue(session.work_observed)
        self.assertIsNone(session.fallback_reason)

    def test_a_dispatched_prompt_forecloses_the_fallback_without_an_answer(
        self,
    ) -> None:
        """The gap the other two miss: sent, never accepted, no work seen.

        An app-server that read ``turn/start`` and died before answering leaves
        no acceptance and no emitted work, so the two checks above both pass it
        -- and the fallback would re-run, on ``codex exec``, a prompt the
        provider may already be working on.
        """
        process = fixtures._FakeProcess(fixtures.lost_turn_response())

        session = fixtures.start_lost_session(process)

        self.assertTrue(session.turn_dispatched)
        self.assertFalse(session.turn_accepted)
        self.assertFalse(session.work_observed)
        self.assertIsNone(session.fallback_reason)


if __name__ == "__main__":
    unittest.main()
