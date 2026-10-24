"""Claude stream steering confirmation and interrupts."""

from __future__ import annotations

from .support_asl_claude_stream_base import (
    GUIDANCE_ID,
    HOOK_WAIT_SECONDS,
    Any,
    SimpleNamespace,
    TempDirTestCase,
    _Stdin,
    claude_stream,
    guidance_delivery,
    json,
    request_for,
    session_with,
    threading,
    time,
)


class StreamSessionSteerTests(TempDirTestCase):
    """A write is not a delivery; evidence is."""

    def spool(self) -> claude_stream.ClaudeGuidanceSpool:
        return claude_stream.ClaudeGuidanceSpool(self.tmp / "spool")

    def test_a_successful_write_alone_is_never_a_delivery(self) -> None:
        spool = self.spool()
        session, stdin = session_with(spool=spool)
        session.send_prompt()

        outcome = session.steer(request_for(text="rebase before you push"))

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_AWAITING_CONFIRMATION, outcome.reason)
        self.assertTrue(outcome.retriable_in_turn)
        # Both routes are armed by that one attempt.
        self.assertEqual(
            claude_stream.user_message("rebase before you push"),
            stdin.messages()[-1],
        )
        self.assertTrue((spool.root / "pending" / f"{GUIDANCE_ID}.json").exists())

    def test_the_protocol_echo_confirms_the_delivery(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))

        session.note_user_echo("rebase before you push")

        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertTrue(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_DELIVERED, outcome.reason)

    def test_an_unrelated_echo_confirms_nothing(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))

        session.note_user_echo("tool result for some other message")
        session.note_user_echo("")

        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertFalse(outcome.delivered)

    def test_an_overlapping_but_unrelated_echo_confirms_nothing(self) -> None:
        # The prompt spans the guidance phrase, so a containment match would
        # hand the guidance a receipt for a message written before the
        # supervisor ever said it.
        session, _ = session_with(
            prompt="do the work, and rebase before you push when you are done"
        )
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))

        # A late prompt echo is still the prompt's own.
        session.note_user_echo(
            "do the work, and rebase before you push when you are done"
        )
        # So is any other message that merely quotes the guidance.
        session.note_user_echo("the operator asked me to rebase before you push?")

        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_AWAITING_CONFIRMATION, outcome.reason)

        # The guidance's own echo, whenever it comes, still confirms it.
        session.note_user_echo("rebase before you push")
        self.assertTrue(
            session.steer(request_for(text="rebase before you push")).delivered
        )

    def test_one_echo_confirms_only_the_guidance_that_carried_that_body(self) -> None:
        other_id = "22222222-2222-4222-8222-222222222222"
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))
        session.steer(request_for(guidance_id=other_id, seq=2, text="rebase"))

        session.note_user_echo("rebase")

        self.assertFalse(
            session.steer(request_for(text="rebase before you push")).delivered
        )
        other = session.steer(request_for(guidance_id=other_id, seq=2, text="rebase"))
        self.assertTrue(other.delivered)

    def test_a_rewrapped_echo_of_the_same_words_still_confirms(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before\nyou push"))

        session.note_user_echo("  rebase before you push  ")

        outcome = session.steer(request_for(text="rebase before\nyou push"))
        self.assertTrue(outcome.delivered)

    def test_an_echo_observed_during_flush_confirms_its_guidance(self) -> None:
        # The output processor runs on its own thread and can see the CLI take
        # the message up before ``flush`` has even returned. That echo has to
        # find the expectation already published, or the guidance stays
        # unacknowledged forever even though the model read it.
        session, stdin = session_with()
        session.send_prompt()
        stdin.on_flush = lambda: session.note_user_echo("rebase before you push")

        outcome = session.steer(request_for(text="rebase before you push"))

        self.assertTrue(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_DELIVERED, outcome.reason)

    def test_an_echo_during_a_failed_write_is_not_left_for_a_later_message(
        self,
    ) -> None:
        # The mirror of the case above: bytes that never reached the pipe leave
        # no expectation behind, so a later unrelated echo of the same words
        # cannot be spent on the guidance the CLI never received.
        session, stdin = session_with()
        session.send_prompt()
        stdin.broken = True

        first = session.steer(request_for(text="rebase before you push"))
        self.assertFalse(first.delivered)

        session.note_user_echo("rebase before you push")

        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertFalse(outcome.delivered)

    def test_a_hook_receipt_confirms_the_delivery(self) -> None:
        spool = self.spool()
        session, _ = session_with(spool=spool)
        session.send_prompt()
        session.steer(request_for())

        (spool.root / "receipts" / f"{GUIDANCE_ID}.json").write_text(
            json.dumps({"id": GUIDANCE_ID}), encoding="utf-8"
        )

        outcome = session.steer(request_for())
        self.assertTrue(outcome.delivered)

    def test_a_closed_conversation_still_leaves_the_hook_route(self) -> None:
        spool = self.spool()
        session, _ = session_with(spool=spool)
        session.send_prompt()
        session.note_result()
        self.assertTrue(session.stdin_closed)

        outcome = session.steer(request_for())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_AWAITING_CONFIRMATION, outcome.reason)
        self.assertTrue((spool.root / "pending" / f"{GUIDANCE_ID}.json").exists())

    def test_a_broken_pipe_with_no_hook_ends_the_attempt(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        stdin.broken = True

        outcome = session.steer(request_for())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_TRANSPORT_UNAVAILABLE, outcome.reason)
        self.assertFalse(outcome.retriable_in_turn)

    def test_a_broken_pipe_keeps_waiting_while_the_hook_holds_the_offer(
        self,
    ) -> None:
        spool = self.spool()
        session, stdin = session_with(spool=spool)
        session.send_prompt()
        stdin.broken = True

        outcome = session.steer(request_for())

        self.assertEqual(guidance_delivery.REASON_AWAITING_CONFIRMATION, outcome.reason)

    def test_a_process_without_a_stdin_pipe_is_not_a_conversation(self) -> None:
        session = claude_stream.ClaudeStreamSession(
            process=SimpleNamespace(stdin=None), prompt="do the work"
        )

        self.assertFalse(session.send_prompt())
        self.assertIn("stdin", session.prompt_error or "")


class StreamSessionInterruptTests(TempDirTestCase):
    """Stopping a turn is a request that must be answered, by name.

    ``interrupt()`` blocks on a reply that only the stdout reader can deliver,
    so every test here runs it on a thread and plays the reader itself. That is
    also how the real turn works: the daemon thread beside the forced kill
    sends it while the readers are still alive.
    """

    def spool(self) -> claude_stream.ClaudeGuidanceSpool:
        return claude_stream.ClaudeGuidanceSpool(self.tmp / "spool")

    def started(
        self, **kwargs: Any
    ) -> tuple[claude_stream.ClaudeStreamSession, _Stdin]:
        session, stdin = session_with(**kwargs)
        session.send_prompt()
        return session, stdin

    def interrupt_async(
        self, session: claude_stream.ClaudeStreamSession
    ) -> tuple[threading.Thread, list[bool]]:
        answered: list[bool] = []
        thread = threading.Thread(
            target=lambda: answered.append(session.interrupt()), daemon=True
        )
        thread.start()
        return thread, answered

    def sent_control_request(self, stdin: _Stdin) -> dict[str, Any]:
        """Wait for the control request and return it, failing if none comes."""
        deadline = time.monotonic() + HOOK_WAIT_SECONDS
        while time.monotonic() < deadline:
            for message in stdin.messages():
                if message.get("type") == claude_stream.CONTROL_REQUEST:
                    return message
            time.sleep(0.01)
        self.fail("no control request was written to stdin")

    def answer(
        self,
        session: claude_stream.ClaudeStreamSession,
        request_id: str,
        subtype: str = "success",
    ) -> None:
        session.note_control_response({"subtype": subtype, "request_id": request_id})

    def settle(self, thread: threading.Thread, answered: list[bool]) -> bool:
        thread.join(timeout=HOOK_WAIT_SECONDS)
        self.assertFalse(thread.is_alive())
        return answered[0]

    def test_an_interrupt_is_a_correlated_control_request(self) -> None:
        session, stdin = self.started()

        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])

        self.assertTrue(self.settle(thread, answered))
        self.assertEqual(claude_stream.SUBTYPE_INTERRUPT, request["request"]["subtype"])
        self.assertTrue(request["request_id"])
        self.assertTrue(session.interrupt_requested)

    def test_an_answer_naming_another_request_acknowledges_nothing(self) -> None:
        session, stdin = self.started(interrupt_timeout=0.2)

        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, f"{request['request_id']}-other")

        # Arrival order would have made this the answer. Correlation is the
        # whole reason it is not.
        self.assertFalse(self.settle(thread, answered))

    def test_an_error_response_is_not_an_acknowledgement(self) -> None:
        session, stdin = self.started()

        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"], subtype="error")

        self.assertFalse(self.settle(thread, answered))

    def test_a_dead_cli_ends_the_wait_without_spending_the_timeout(self) -> None:
        # The timeout is far longer than this test may take, so finishing at
        # all proves the wait noticed the process rather than expiring.
        session, stdin = self.started(interrupt_timeout=600.0)

        thread, answered = self.interrupt_async(session)
        self.sent_control_request(stdin)
        stdin.process.exit()

        self.assertFalse(self.settle(thread, answered))

    def test_an_unanswered_request_gives_up_at_the_timeout(self) -> None:
        session, stdin = self.started(interrupt_timeout=0.1)

        thread, answered = self.interrupt_async(session)
        self.sent_control_request(stdin)

        self.assertFalse(self.settle(thread, answered))

    def test_a_turn_is_asked_to_stop_exactly_once(self) -> None:
        session, stdin = self.started()

        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])
        self.assertTrue(self.settle(thread, answered))

        self.assertFalse(session.interrupt())
        requests = [
            message
            for message in stdin.messages()
            if message.get("type") == claude_stream.CONTROL_REQUEST
        ]
        self.assertEqual(1, len(requests))

    def test_a_turn_that_submitted_nothing_has_nothing_to_stop(self) -> None:
        session, stdin = session_with()

        self.assertFalse(session.interrupt())
        self.assertEqual([], stdin.messages())

    def test_a_closed_conversation_has_nothing_to_stop(self) -> None:
        session, _ = self.started()
        session.note_result()
        self.assertTrue(session.stdin_closed)

        self.assertFalse(session.interrupt())

    def test_a_broken_pipe_leaves_no_request_outstanding(self) -> None:
        session, stdin = self.started(interrupt_timeout=600.0)
        stdin.broken = True

        # Returns rather than waiting out the timeout: the request never left.
        self.assertFalse(session.interrupt())

    def test_stray_control_traffic_never_disturbs_the_accounting(self) -> None:
        session, stdin = self.started()

        session.note_control_response({"subtype": "success", "request_id": "req_9"})
        session.note_control_response({"subtype": "success"})
        session.note_control_response("not a record")

        session.note_result()
        self.assertTrue(stdin.closed_by_asl)
        self.assertFalse(session.turn_interrupted)

    def test_the_interrupted_result_is_drained_rather_than_credited(self) -> None:
        session, stdin = self.started()
        session.steer(request_for())
        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])
        self.settle(thread, answered)

        session.note_result()

        # Queued guidance would ordinarily hold the conversation open for its
        # own result. There is no such turn coming.
        self.assertTrue(session.turn_interrupted)
        self.assertTrue(stdin.closed_by_asl)

    def test_a_later_result_is_still_not_a_resumed_turns_result(self) -> None:
        session, stdin = self.started()
        session.steer(request_for())
        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])
        self.settle(thread, answered)

        session.note_result()
        session.note_user_echo("prefer the smaller change")
        session.note_result()

        outcome = session.steer(request_for())
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)

    def test_an_echo_drained_on_the_way_out_confirms_nothing(self) -> None:
        session, stdin = self.started()
        session.steer(request_for())
        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])
        self.settle(thread, answered)

        session.note_user_echo("prefer the smaller change")

        outcome = session.steer(request_for())
        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)

    def test_guidance_offered_after_an_interrupt_waits_for_the_next_turn(
        self,
    ) -> None:
        session, stdin = self.started(spool=self.spool())
        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])
        self.settle(thread, answered)

        outcome = session.steer(request_for())

        self.assertFalse(outcome.delivered)
        self.assertEqual(guidance_delivery.REASON_STALE_TURN, outcome.reason)
        bodies = [
            message
            for message in stdin.messages()
            if message.get("type") == "user"
            and "prefer the smaller change" in json.dumps(message)
        ]
        self.assertEqual([], bodies)

    def test_a_receipt_the_hook_already_wrote_still_counts(self) -> None:
        spool = self.spool()
        session, stdin = self.started(spool=spool)
        session.steer(request_for())
        # The hook handed the text to a tool call that then ran; the interrupt
        # came afterwards and cannot unrun it.
        (spool.root / "receipts" / f"{GUIDANCE_ID}.json").write_text(
            json.dumps({"id": GUIDANCE_ID}), encoding="utf-8"
        )
        thread, answered = self.interrupt_async(session)
        request = self.sent_control_request(stdin)
        self.answer(session, request["request_id"])
        self.settle(thread, answered)

        outcome = session.steer(request_for())

        self.assertTrue(outcome.delivered)
