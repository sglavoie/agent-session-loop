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
import unittest
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

    def write(self, text: str) -> int:  # type: ignore[override]
        if self.broken:
            raise BrokenPipeError("the harness is gone")
        return super().write(text)

    def flush(self) -> None:
        if self.broken:
            raise BrokenPipeError("the harness is gone")
        super().flush()

    def close(self) -> None:
        self.closed_by_asl = True
        # Left open so a test can still read what was written to it.

    def messages(self) -> list[dict[str, Any]]:
        return [
            json.loads(line) for line in self.getvalue().splitlines() if line.strip()
        ]


def session_with(
    *,
    prompt: str = "do the work",
    spool: claude_stream.ClaudeGuidanceSpool | None = None,
) -> tuple[claude_stream.ClaudeStreamSession, _Stdin]:
    stdin = _Stdin()
    session = claude_stream.ClaudeStreamSession(
        process=SimpleNamespace(stdin=stdin),
        prompt=prompt,
        spool=spool,
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

    def test_guidance_never_rides_the_next_prompt_for_claude(self) -> None:
        # Claude can be steered in-turn, so the ledger's verdict is the live
        # attempt's own. Recording a next-turn delivery on top would invent a
        # second receipt for one guidance record.
        self.assertFalse(harnesses.delivers_guidance_next_turn("claude"))


class CopilotSteerRefusalTests(unittest.TestCase):
    """Copilot answers a steer, and the answer is next turn."""

    def transport(self) -> harnesses.HarnessTransport:
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

    def test_a_copilot_turn_stays_a_one_shot_process(self) -> None:
        invocation = harnesses.CopilotAdapter(executable="copilot").build_invocation(
            cwd=Path("/tmp"), plan_dir=Path("/tmp/plan"), prompt="do the work"
        )

        # Still exec: no stdin pipe is opened, so nothing can cancel or restart
        # the turn through one.
        self.assertEqual(harnesses.TRANSPORT_EXEC, invocation.transport)
        self.assertIn("do the work", invocation.argv)

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
