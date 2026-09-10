"""Claude's bidirectional stream-JSON transport and its PreToolUse hook.

The property every test here defends is the same one: a successful write to a
pipe is not a delivery. The session may only report ``delivered-live`` from
evidence the model took the message up -- a protocol echo on stdout or a
receipt written by the ASL-owned hook -- and must say ``awaiting-confirmation``
until then.

The hook is exercised as the CLI runs it: a real subprocess, by absolute path,
with a hook payload on stdin. That is a spawned process, so this module is a
fast-lane rather than a portable-lane suite.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_session_loop import claude_stream, guidance_delivery, harnesses

GUIDANCE_ID = "11111111-1111-4111-8111-111111111111"

#: A wedged hook would hang this module rather than fail it, so every hook
#: invocation is bounded well above its real cost.
HOOK_WAIT_SECONDS = 30


def request_for(
    *,
    guidance_id: str = GUIDANCE_ID,
    seq: int = 1,
    text: str = "prefer the smaller change",
) -> guidance_delivery.GuidanceRequest:
    return guidance_delivery.GuidanceRequest(
        guidance_id=guidance_id,
        seq=seq,
        text=text,
        turn=guidance_delivery.TurnIdentity(
            run_id="run-1",
            session_id="7",
            turn_index=2,
            harness="claude",
        ),
    )


class _Stdin(io.StringIO):
    """A stdin pipe that can be made to fail the way a dead CLI's does."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False
        self.closed_by_asl = False
        #: Set by the fake process that owns this pipe.
        self.process: Any = None
        #: Stands in for the output-processing thread reaching the session
        #: while a write is still in flight.
        self.on_flush: Any = None

    def write(self, text: str) -> int:  # type: ignore[override]
        if self.broken:
            raise BrokenPipeError("the harness is gone")
        return super().write(text)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("the harness is gone")
        super().flush()
        if self.on_flush is not None:
            self.on_flush()

    def close(self) -> None:
        self.closed_by_asl = True
        # Left open so a test can still read what was written to it.

    def messages(self) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in self.getvalue().splitlines() if line.strip()
        ]


class _Process(SimpleNamespace):
    """The CLI as the session sees it: a stdin pipe and a liveness answer."""

    def __init__(self, stdin: _Stdin) -> None:
        super().__init__(stdin=stdin)
        self.returncode: int | None = None
        # So a test holding only the pipe can still make the CLI die.
        stdin.process = self

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, code: int = 0) -> None:
        self.returncode = code


def session_with(
    *,
    prompt: str = "do the work",
    spool: claude_stream.ClaudeGuidanceSpool | None = None,
    session_id: str | None = None,
    interrupt_timeout: float = claude_stream.INTERRUPT_TIMEOUT_SECONDS,
) -> tuple[claude_stream.ClaudeStreamSession, _Stdin]:
    stdin = _Stdin()
    session = claude_stream.ClaudeStreamSession(
        process=_Process(stdin),
        prompt=prompt,
        spool=spool,
        session_id=session_id,
        interrupt_timeout=interrupt_timeout,
    )
    return session, stdin


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class GuidanceSpoolTests(TempDirTestCase):
    """The at-most-once handoff between ASL and the hook."""

    def spool(self) -> claude_stream.ClaudeGuidanceSpool:
        return claude_stream.ClaudeGuidanceSpool(self.tmp / "spool")

    def test_an_offer_is_a_complete_record_or_nothing(self) -> None:
        spool = self.spool()

        spool.offer(guidance_id=GUIDANCE_ID, seq=3, text="rebase first")

        pending = spool.root / "pending" / f"{GUIDANCE_ID}.json"
        self.assertEqual(
            {"id": GUIDANCE_ID, "seq": 3, "text": "rebase first"},
            json.loads(pending.read_text(encoding="utf-8")),
        )
        # No temporary file is left where the hook could read a partial record.
        self.assertEqual(
            [pending.name], [entry.name for entry in pending.parent.iterdir()]
        )

    def test_withdrawing_an_unclaimed_offer_succeeds_once(self) -> None:
        spool = self.spool()
        spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase first")

        self.assertTrue(spool.withdraw(GUIDANCE_ID))
        self.assertFalse(spool.withdraw(GUIDANCE_ID))

    def test_a_receipt_is_absent_until_the_hook_writes_one(self) -> None:
        spool = self.spool()
        spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase first")

        self.assertFalse(spool.receipt(GUIDANCE_ID))

        (spool.root / "receipts" / f"{GUIDANCE_ID}.json").write_text(
            "{}", encoding="utf-8"
        )
        self.assertTrue(spool.receipt(GUIDANCE_ID))

    def test_the_overlay_arms_one_pretooluse_hook_and_nothing_else(self) -> None:
        spool = self.spool()

        payload = spool.settings_payload(executable="/usr/bin/python3")

        self.assertEqual(["hooks"], list(payload))
        self.assertEqual([claude_stream.HOOK_EVENT], list(payload["hooks"]))
        [matcher] = payload["hooks"][claude_stream.HOOK_EVENT]
        self.assertEqual("*", matcher["matcher"])
        [hook] = matcher["hooks"]
        self.assertEqual("command", hook["type"])
        self.assertEqual(claude_stream.HOOK_TIMEOUT_SECONDS, hook["timeout"])
        self.assertIn(str(claude_stream.hook_script_path()), hook["command"])
        self.assertIn(str(spool.root), hook["command"])

    def test_the_overlay_file_is_the_path_settings_takes(self) -> None:
        spool = self.spool()
        overlay_dir = self.tmp / "overlay"
        overlay_dir.mkdir()

        path = claude_stream.write_settings_overlay(
            overlay_dir, spool, executable=sys.executable
        )

        self.assertEqual(overlay_dir / "settings.json", path)
        self.assertEqual(
            spool.settings_payload(executable=sys.executable),
            json.loads(path.read_text(encoding="utf-8")),
        )

    def test_removing_the_overlay_leaves_the_hook_nothing_to_claim(self) -> None:
        overlay_dir = self.tmp / "overlay"
        overlay_dir.mkdir()
        spool = claude_stream.ClaudeGuidanceSpool(overlay_dir / "spool")
        spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase first")

        claude_stream.remove_overlay(overlay_dir)

        self.assertFalse(overlay_dir.exists())
        # Idempotent: the turn's ``finally`` runs on paths where it is gone.
        claude_stream.remove_overlay(overlay_dir)
        claude_stream.remove_overlay(None)


class StreamSessionPromptTests(TempDirTestCase):
    """The prompt is a stdin message, and a failed one ends the turn."""

    def test_the_prompt_is_the_conversations_first_user_message(self) -> None:
        session, stdin = session_with(prompt="/session-run PLAN\n\nbody")

        self.assertTrue(session.send_prompt())

        self.assertEqual(
            [claude_stream.user_message("/session-run PLAN\n\nbody")],
            stdin.messages(),
        )
        self.assertIsNone(session.prompt_error)
        self.assertFalse(session.stdin_closed)

    def test_a_prompt_that_never_landed_is_reported_and_closes_stdin(self) -> None:
        session, stdin = session_with()
        stdin.broken = True

        self.assertFalse(session.send_prompt())

        self.assertIn("the harness is gone", session.prompt_error or "")
        # Closed rather than left open: an unfed CLI would otherwise sit on its
        # stdin until the turn's timeout instead of exiting.
        self.assertTrue(session.stdin_closed)
        self.assertTrue(stdin.closed_by_asl)

    def test_stdin_closes_once_every_message_has_been_answered(self) -> None:
        session, stdin = session_with()
        session.send_prompt()

        session.note_result()

        self.assertTrue(session.stdin_closed)
        self.assertTrue(stdin.closed_by_asl)

    def test_a_queued_guidance_turn_keeps_the_conversation_open(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for())

        session.note_result()

        # The prompt's result is not the guidance turn's result, and closing
        # here would discard a message the CLI has not run yet.
        self.assertFalse(session.stdin_closed)

        session.note_result()
        self.assertTrue(session.stdin_closed)

    def test_guidance_taken_up_mid_turn_shares_the_prompts_result(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        session.note_user_echo("do the work")
        session.steer(request_for())
        session.note_user_echo("prefer the smaller change")

        session.note_result()

        self.assertTrue(stdin.closed_by_asl)

    def test_merged_guidance_does_not_settle_a_still_queued_message(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        session.note_user_echo("do the work")
        session.steer(request_for())
        session.note_user_echo("prefer the smaller change")
        session.steer(request_for(guidance_id="second", seq=2, text="check tests"))

        session.note_result()
        self.assertFalse(stdin.closed_by_asl)
        session.note_user_echo("check tests")
        session.note_result()

        self.assertTrue(stdin.closed_by_asl)

    def test_replayed_old_echo_does_not_settle_new_guidance(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        session.note_user_echo("do the work")
        session.steer(request_for())
        session.note_user_echo("prefer the smaller change")
        session.steer(request_for(guidance_id="second", seq=2, text="check tests"))
        session.note_user_echo("prefer the smaller change")

        session.note_result()

        self.assertFalse(stdin.closed_by_asl)

    def test_shutdown_ends_the_conversation(self) -> None:
        session, stdin = session_with()
        session.send_prompt()

        session.shutdown()

        self.assertTrue(session.stdin_closed)
        self.assertTrue(stdin.closed_by_asl)


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


class StreamSessionContinuityTests(unittest.TestCase):
    """The conversation this turn runs on is stated, then re-checked."""

    def test_a_turn_knows_its_conversation_before_it_writes_anything(self) -> None:
        session, stdin = session_with(session_id="sess-1")

        self.assertEqual("sess-1", session.session_id)
        self.assertEqual([], stdin.messages())

    def test_the_reported_session_is_checked_against_the_stated_one(self) -> None:
        session, _ = session_with(session_id="sess-1")

        mismatch = session.note_session_id("sess-2")

        self.assertIsNotNone(mismatch)
        self.assertIn("sess-1", mismatch or "")
        self.assertIn("sess-2", mismatch or "")
        self.assertEqual(mismatch, session.session_mismatch)

    def test_the_first_disagreement_is_the_one_reported(self) -> None:
        session, _ = session_with(session_id="sess-1")

        first = session.note_session_id("sess-2")
        session.note_session_id("sess-3")

        self.assertEqual(first, session.session_mismatch)

    def test_the_session_the_turn_asked_for_is_no_disagreement(self) -> None:
        session, _ = session_with(session_id="sess-1")

        self.assertIsNone(session.note_session_id("sess-1"))
        self.assertIsNone(session.session_mismatch)

    def test_a_turn_that_stated_no_session_has_nothing_to_contradict(self) -> None:
        # An audit turn persists no session, so whatever the CLI names is its
        # own business.
        session, _ = session_with()

        self.assertIsNone(session.note_session_id("sess-2"))
        self.assertIsNone(session.session_mismatch)

    def test_a_mismatch_does_not_stop_the_turn(self) -> None:
        session, stdin = session_with(session_id="sess-1")
        session.send_prompt()

        session.note_session_id("sess-2")

        self.assertFalse(session.stdin_closed)
        self.assertTrue(session.steer(request_for()) is not None)
        self.assertIn("prefer the smaller change", json.dumps(stdin.messages()))


class StreamOutputProcessorTests(unittest.TestCase):
    """The one stdout reader is also the session's acknowledgement channel."""

    def processor(self, session: claude_stream.ClaudeStreamSession) -> Any:
        return harnesses._ClaudeStreamJsonOutputProcessor(session)

    def test_a_user_echo_confirms_without_being_rendered(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps(claude_stream.user_message("rebase before you push")) + "\n",
        )

        # No guidance body reaches the turn log.
        self.assertEqual("", rendered)
        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertTrue(outcome.delivered)

    def test_an_echo_carrying_bare_text_still_confirms(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for(text="rebase before you push"))
        processor = self.processor(session)

        processor.process_chunk(
            "stdout",
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": "rebase before you push",
                    },
                }
            )
            + "\n",
        )

        outcome = session.steer(request_for(text="rebase before you push"))
        self.assertTrue(outcome.delivered)

    def test_a_result_record_is_still_rendered_and_ends_the_turn(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps({"type": "result", "subtype": "success", "result": "done"})
            + "\n",
        )

        self.assertIn("claude result: success", rendered)
        self.assertTrue(processor.final_result_seen)
        self.assertTrue(stdin.closed_by_asl)

    def test_a_control_response_answers_the_session_without_being_rendered(
        self,
    ) -> None:
        session, stdin = session_with()
        session.send_prompt()
        processor = self.processor(session)
        answered: list[bool] = []
        thread = threading.Thread(
            target=lambda: answered.append(session.interrupt()), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + HOOK_WAIT_SECONDS
        request: dict[str, Any] | None = None
        while request is None and time.monotonic() < deadline:
            for message in stdin.messages():
                if message.get("type") == claude_stream.CONTROL_REQUEST:
                    request = message
            time.sleep(0.01)
        assert request is not None

        rendered = processor.process_chunk(
            "stdout",
            json.dumps(
                {
                    "type": claude_stream.CONTROL_RESPONSE,
                    "response": {
                        "subtype": "success",
                        "request_id": request["request_id"],
                    },
                }
            )
            + "\n",
        )

        thread.join(timeout=HOOK_WAIT_SECONDS)
        self.assertEqual([True], answered)
        # Protocol bookkeeping is not turn output.
        self.assertEqual("", rendered)

    def test_the_init_record_is_rechecked_against_the_stated_session(self) -> None:
        session, _ = session_with(session_id="sess-1")
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-2"})
            + "\n",
        )

        self.assertIn("claude session mismatch", rendered)
        self.assertIn("sess-1", rendered)
        self.assertIn("sess-2", rendered)
        self.assertIsNotNone(session.session_mismatch)

    def test_the_session_the_turn_asked_for_is_reported_without_alarm(self) -> None:
        session, _ = session_with(session_id="sess-1")
        processor = self.processor(session)

        rendered = processor.process_chunk(
            "stdout",
            json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"})
            + "\n",
        )

        self.assertIn("session sess-1", rendered)
        self.assertNotIn("mismatch", rendered)
        self.assertIsNone(session.session_mismatch)

    def test_normalization_still_works_without_a_session(self) -> None:
        processor = harnesses._ClaudeStreamJsonOutputProcessor()

        rendered = processor.process_chunk(
            "stdout",
            "\n".join(
                [
                    json.dumps(
                        {"type": "system", "subtype": "init", "model": "opus-x"}
                    ),
                    json.dumps(claude_stream.user_message("the prompt")),
                    json.dumps({"type": "result", "subtype": "success"}),
                ]
            )
            + "\n",
        )

        self.assertIn("claude init; model opus-x", rendered)
        self.assertIn("claude result: success", rendered)
        self.assertNotIn("the prompt", rendered)
        self.assertEqual("opus-x", processor.observed_model)

    def test_a_process_with_merged_guidance_gets_eof_and_exits_successfully(
        self,
    ) -> None:
        # A real pipe reproduces the deadlock: the CLI has consumed both
        # messages in one turn, emits one result, and waits for stdin EOF.
        script = """
import json, sys
for _ in range(2):
    message = json.loads(sys.stdin.readline())
    message['isReplay'] = True
    print(json.dumps(message), flush=True)
print(json.dumps({'type': 'result', 'subtype': 'success'}), flush=True)
sys.stdin.read()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        session = claude_stream.ClaudeStreamSession(process=process, prompt="do work")
        processor = self.processor(session)
        rendered: list[str] = []

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                rendered.append(processor.process_chunk("stdout", line))

        reader = threading.Thread(target=read_output, daemon=True)
        try:
            session.send_prompt()
            session.steer(request_for())
            reader.start()
            self.assertEqual(0, process.wait(timeout=5))
            reader.join(timeout=5)
            self.assertFalse(reader.is_alive())
            self.assertEqual("claude result: success\n", "".join(rendered))
        finally:
            session.shutdown()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


class GuidanceHookScriptTests(TempDirTestCase):
    """The hook as the CLI runs it: a script, a payload, a JSON answer."""

    def setUp(self) -> None:
        super().setUp()
        self.spool = claude_stream.ClaudeGuidanceSpool(self.tmp / "spool")

    def run_hook(
        self, *, spool_root: Path | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(claude_stream.hook_script_path()),
                str(self.spool.root if spool_root is None else spool_root),
            ],
            input=json.dumps({"hook_event_name": claude_stream.HOOK_EVENT}),
            capture_output=True,
            text=True,
            timeout=HOOK_WAIT_SECONDS,
        )

    def test_pending_guidance_becomes_additional_context(self) -> None:
        self.spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase before you push")

        result = self.run_hook()

        self.assertEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        specific = payload["hookSpecificOutput"]
        self.assertEqual(claude_stream.HOOK_EVENT, specific["hookEventName"])
        self.assertIn(
            claude_stream.GUIDANCE_CONTEXT_HEADING, specific["additionalContext"]
        )
        self.assertIn("rebase before you push", specific["additionalContext"])
        self.assertTrue(self.spool.receipt(GUIDANCE_ID))

    def test_guidance_is_injected_at_most_once(self) -> None:
        self.spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase before you push")
        self.run_hook()

        second = self.run_hook()

        self.assertEqual(0, second.returncode)
        self.assertEqual("", second.stdout.strip())

    def test_records_are_injected_in_the_order_they_were_written(self) -> None:
        self.spool.offer(guidance_id="b" * 8, seq=2, text="second thing")
        self.spool.offer(guidance_id="a" * 8, seq=1, text="first thing")

        context = json.loads(self.run_hook().stdout)["hookSpecificOutput"][
            "additionalContext"
        ]

        self.assertLess(context.index("first thing"), context.index("second thing"))

    def test_a_withdrawn_record_is_never_injected(self) -> None:
        self.spool.offer(guidance_id=GUIDANCE_ID, seq=1, text="rebase before you push")
        self.assertTrue(self.spool.withdraw(GUIDANCE_ID))

        result = self.run_hook()

        self.assertEqual("", result.stdout.strip())
        self.assertFalse(self.spool.receipt(GUIDANCE_ID))

    def test_an_empty_spool_says_nothing(self) -> None:
        result = self.run_hook()

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_a_removed_overlay_never_blocks_a_tool_call(self) -> None:
        result = self.run_hook(spool_root=self.tmp / "gone")

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_a_corrupt_record_never_blocks_a_tool_call(self) -> None:
        (self.spool.root / "pending" / "broken.json").write_text(
            "{not json", encoding="utf-8"
        )

        result = self.run_hook()

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())

    def test_a_hook_with_no_spool_argument_exits_quietly(self) -> None:
        result = subprocess.run(
            [sys.executable, str(claude_stream.hook_script_path())],
            input="{}",
            capture_output=True,
            text=True,
            timeout=HOOK_WAIT_SECONDS,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout.strip())


class ClaudeAdapterTransportTests(TempDirTestCase):
    """What the adapter puts on argv, on disk, and behind the seam."""

    def adapter(self) -> harnesses.ClaudeAdapter:
        return harnesses.ClaudeAdapter(executable="claude")

    def invocation(self, **overrides: Any) -> harnesses.HarnessInvocation:
        fields: dict[str, Any] = {
            "cwd": self.tmp,
            "plan_dir": self.tmp / "plan",
            "prompt": "do the work",
        }
        fields.update(overrides)
        return self.adapter().build_invocation(**fields)

    def test_a_claude_turn_declares_the_stream_json_transport(self) -> None:
        invocation = self.invocation()

        self.assertEqual(claude_stream.TRANSPORT_STREAM_JSON, invocation.transport)
        self.assertNotEqual(harnesses.TRANSPORT_EXEC, invocation.transport)
        self.assertIn("--input-format", invocation.argv)
        self.assertIn("--replay-user-messages", invocation.argv)

    def test_opening_the_transport_sends_the_prompt_over_stdin(self) -> None:
        stdin = _Stdin()
        transport = self.adapter().open_transport(
            process=SimpleNamespace(stdin=stdin),
            invocation=self.invocation(),
            prompt="do the work",
        )

        self.assertIsInstance(transport, harnesses._ClaudeStreamTransport)
        self.assertEqual([claude_stream.user_message("do the work")], stdin.messages())
        self.assertIsNotNone(transport.guidance_transport())

    def test_the_transport_carries_the_spool_from_the_overlay(self) -> None:
        overlay_dir = self.tmp / "overlay"
        overlay_dir.mkdir()
        stdin = _Stdin()
        invocation = harnesses.HarnessInvocation(
            name="claude",
            argv=["claude"],
            cwd=self.tmp,
            transport=claude_stream.TRANSPORT_STREAM_JSON,
            overlay_dir=overlay_dir,
        )

        transport = self.adapter().open_transport(
            process=SimpleNamespace(stdin=stdin),
            invocation=invocation,
            prompt="do the work",
        )

        transport.guidance_transport()(request_for())
        pending = overlay_dir / "spool" / "pending" / f"{GUIDANCE_ID}.json"
        self.assertTrue(pending.exists())

    def test_a_non_stream_invocation_opens_no_conversation(self) -> None:
        invocation = harnesses.HarnessInvocation(
            name="claude", argv=["claude"], cwd=self.tmp
        )

        self.assertIsNone(
            self.adapter().open_transport(
                process=SimpleNamespace(stdin=None),
                invocation=invocation,
                prompt="do the work",
            )
        )

    def audit_invocation(self, **overrides: Any) -> harnesses.HarnessInvocation:
        # An audit turn takes read roots instead of a writable plan dir.
        return self.invocation(
            plan_dir=None,
            access=harnesses.audit_read_only_access((self.tmp,)),
            **overrides,
        )

    def test_a_fresh_turn_states_the_session_it_is_opening(self) -> None:
        invocation = self.invocation()

        argv = invocation.argv
        self.assertIn("--session-id", argv)
        assigned = argv[argv.index("--session-id") + 1]
        # A real UUID, because the CLI rejects anything else.
        self.assertEqual(assigned, str(uuid.UUID(assigned)))
        self.assertEqual(assigned, invocation.assigned_conversation_id)

    def test_two_turns_never_open_the_same_conversation(self) -> None:
        self.assertNotEqual(
            self.invocation().assigned_conversation_id,
            self.invocation().assigned_conversation_id,
        )

    def test_a_resume_restores_exactly_the_conversation_asked_for(self) -> None:
        invocation = self.invocation(resume_conversation_id="sess-7")

        argv = invocation.argv
        self.assertIn("--resume", argv)
        self.assertEqual("sess-7", argv[argv.index("--resume") + 1])
        self.assertEqual("sess-7", invocation.assigned_conversation_id)
        self.assertNotIn("--session-id", argv)

    def test_a_resumed_turn_is_never_forked_onto_a_new_conversation(self) -> None:
        # ``--fork-session`` answers "continue conversation X" with a different
        # one, which is the substitution this path exists to prevent.
        self.assertNotIn("--fork-session", self.invocation().argv)
        self.assertNotIn(
            "--fork-session", self.invocation(resume_conversation_id="sess-7").argv
        )

    def test_an_audit_turn_names_no_conversation_at_all(self) -> None:
        invocation = self.audit_invocation()

        self.assertIn("--no-session-persistence", invocation.argv)
        self.assertNotIn("--session-id", invocation.argv)
        self.assertNotIn("--resume", invocation.argv)
        self.assertIsNone(invocation.assigned_conversation_id)

    def test_an_audit_turn_refuses_to_resume_a_conversation(self) -> None:
        with self.assertRaises(harnesses.HarnessError) as raised:
            self.audit_invocation(resume_conversation_id="sess-7")

        self.assertIn("sess-7", str(raised.exception))

    def test_the_session_reaches_the_transport_that_must_re_check_it(self) -> None:
        invocation = self.invocation(resume_conversation_id="sess-7")

        transport = self.adapter().open_transport(
            process=_Process(_Stdin()),
            invocation=invocation,
            prompt="do the work",
        )

        self.assertEqual("sess-7", transport.session.session_id)

    def test_the_conversation_is_recorded_before_the_prompt_is_written(self) -> None:
        stdin = _Stdin()
        invocation = self.invocation()
        written_at_capture: list[list[dict[str, Any]]] = []
        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(
                sink=lambda native_id: written_at_capture.append(stdin.messages())
            )
        )

        self.adapter().open_transport(
            process=_Process(stdin),
            invocation=invocation,
            prompt="do the work",
            lifecycle=lifecycle,
        )

        # Nothing had been submitted yet, which is what makes the capture
        # refusal below able to cost the turn nothing.
        self.assertEqual([[]], written_at_capture)
        self.assertEqual(
            invocation.assigned_conversation_id, lifecycle.conversation.native_id
        )

    def test_a_turn_whose_conversation_cannot_be_recorded_submits_nothing(
        self,
    ) -> None:
        stdin = _Stdin()

        def refuse(native_id: str) -> None:
            raise OSError("the ledger is unwritable")

        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(sink=refuse)
        )

        with self.assertRaises(harnesses.ConversationCaptureError):
            self.adapter().open_transport(
                process=_Process(stdin),
                invocation=self.invocation(),
                prompt="do the work",
                lifecycle=lifecycle,
            )

        self.assertEqual([], stdin.messages())

    def test_an_audit_turn_captures_nothing_and_still_runs(self) -> None:
        stdin = _Stdin()
        captured: list[str] = []
        lifecycle = harnesses.TurnLifecycle(
            conversation=harnesses.TurnConversation(sink=captured.append)
        )

        self.adapter().open_transport(
            process=_Process(stdin),
            invocation=self.audit_invocation(),
            prompt="do the work",
            lifecycle=lifecycle,
        )

        self.assertEqual([], captured)
        self.assertEqual([claude_stream.user_message("do the work")], stdin.messages())

    def test_claude_advertises_the_continuity_it_now_has(self) -> None:
        adapter = self.adapter()

        self.assertTrue(adapter.captures_conversations)
        self.assertTrue(adapter.resumes_conversations)
        self.assertTrue(harnesses.continues_conversations("claude"))

    def test_guidance_never_rides_the_next_prompt_for_claude(self) -> None:
        # Claude can be steered in-turn, so the ledger's verdict is the live
        # attempt's own. Recording a next-turn delivery on top would invent a
        # second receipt for one guidance record.
        self.assertFalse(harnesses.delivers_guidance_next_turn("claude"))


class CopilotSteerRefusalTests(unittest.TestCase):
    """Copilot answers a steer, and the answer is next turn."""

    def transport(self) -> harnesses.HarnessTransport:
        """The one-shot transport, which is the audit profile's.

        The worker profile's ACP transport gives the same answers, proved
        against a live conversation in ``tests/test_asl_copilot_acp.py``.
        """
        adapter = harnesses.CopilotAdapter(executable="copilot")
        transport = adapter.open_transport(
            process=SimpleNamespace(stdin=None),
            invocation=harnesses.HarnessInvocation(
                name="copilot", argv=["copilot"], cwd=Path("/tmp")
            ),
            prompt="do the work",
        )
        assert transport is not None
        return transport

    def test_an_active_steer_is_declined_rather_than_missing(self) -> None:
        steer = self.transport().guidance_transport()
        assert steer is not None

        outcome = steer(request_for())

        self.assertFalse(outcome.delivered)
        self.assertEqual(
            guidance_delivery.REASON_UNSUPPORTED_ACTIVE_STEER, outcome.reason
        )
        # Not retriable: the answer will not change inside this turn.
        self.assertFalse(outcome.retriable_in_turn)

    def test_a_copilot_worker_turn_speaks_acp_and_still_refuses_a_steer(self) -> None:
        invocation = harnesses.CopilotAdapter(executable="copilot").build_invocation(
            cwd=Path("/tmp"), plan_dir=Path("/tmp/plan"), prompt="do the work"
        )

        # A worker turn now has a stdin pipe, but it carries the handshake and
        # one prompt: ``session/cancel`` is not wired, so there is still no
        # channel a steer could travel on and the refusal above stands.
        self.assertEqual(harnesses.TRANSPORT_ACP, invocation.transport)
        self.assertNotIn("do the work", invocation.argv)

    def test_an_audit_turn_stays_a_one_shot_process(self) -> None:
        invocation = harnesses.CopilotAdapter(executable="copilot").build_invocation(
            cwd=Path("/tmp"),
            plan_dir=None,
            prompt="read the evidence",
            access=harnesses.audit_read_only_access([Path("/tmp/evidence")]),
        )

        # The audit profile did not move: its prompt is on argv, no stdin pipe
        # is opened, and nothing can cancel or restart the turn through one.
        self.assertEqual(harnesses.TRANSPORT_EXEC, invocation.transport)
        self.assertIn("read the evidence", invocation.argv)

    def test_declining_leaves_the_guidance_for_the_next_turns_prompt(self) -> None:
        self.assertTrue(harnesses.delivers_guidance_next_turn("copilot"))

    def test_the_output_stream_is_read_exactly_as_it_always_was(self) -> None:
        processor = self.transport().output_processor()

        self.assertIs(type(processor), harnesses._OutputProcessor)
        self.assertEqual(
            "plain text\n", processor.process_chunk("stdout", "plain text\n")
        )


class NextTurnDeliveryOwnershipTests(unittest.TestCase):
    """Only a harness with no in-turn channel may claim a next-turn delivery."""

    def test_codex_and_claude_report_live_attempts_instead(self) -> None:
        for name in ("codex", "claude"):
            with self.subTest(harness=name):
                self.assertFalse(harnesses.delivers_guidance_next_turn(name))

    def test_an_unknown_harness_claims_nothing(self) -> None:
        self.assertFalse(harnesses.delivers_guidance_next_turn("nope"))


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
