"""Claude stream guidance spool and prompt delivery."""

from __future__ import annotations

from .support_asl_claude_stream_base import (
    GUIDANCE_ID,
    Any,
    TempDirTestCase,
    claude_stream,
    harness_claude,
    json,
    request_for,
    session_with,
    sys,
)


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

    def test_background_reviews_require_a_followup_result_before_eof(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        processor = harness_claude._ClaudeStreamJsonOutputProcessor(session)

        def emit(record: dict[str, Any]) -> None:
            processor.process_chunk("stdout", json.dumps(record) + "\n")

        for task_id in ("code", "tests", "docs"):
            emit(dict(type="system", subtype="task_started", task_id=task_id))
        emit(dict(type="result", subtype="success", result="reviews running"))
        self.assertFalse(stdin.closed_by_asl)
        # Terminal for the main turn even though stdin stays open for the
        # tasks: the hang sentinel must learn a result arrived (ASI-151).
        self.assertTrue(processor.final_result_seen)
        self.assertEqual("background-tasks", session.stdin_hold_reason)
        for task_id in ("code", "tests"):
            emit(dict(type="system", subtype="task_notification", task_id=task_id))
            emit(dict(type="result", origin={"kind": "task-notification"}))
            self.assertFalse(stdin.closed_by_asl)
            self.assertFalse(processor.final_result_seen)
        emit(dict(type="system", subtype="task_notification", task_id="docs"))
        self.assertFalse(stdin.closed_by_asl)
        emit(dict(type="result", origin={"kind": "task-notification"}))
        self.assertTrue(stdin.closed_by_asl)
        self.assertTrue(processor.final_result_seen)
        self.assertIsNone(session.stdin_hold_reason)

    def test_result_before_an_unnotified_task_still_fires_the_final_hook(
        self,
    ) -> None:
        # ASI-151: a task the CLI announced and never reported finished held
        # stdin open, the terminal result never reached the hang sentinel, and
        # the idle root sat for 5142s. The result is terminal regardless.
        session, stdin = session_with()
        session.send_prompt()
        processor = harness_claude._ClaudeStreamJsonOutputProcessor(session)
        self.assertIsNone(session.stdin_hold_reason)

        def emit(record: dict[str, Any]) -> None:
            processor.process_chunk("stdout", json.dumps(record) + "\n")

        emit(dict(type="system", subtype="task_started", task_id="stale"))
        emit(dict(type="result", subtype="success", result="done"))

        self.assertTrue(processor.final_result_seen)
        self.assertFalse(stdin.closed_by_asl)
        self.assertEqual("background-tasks", session.stdin_hold_reason)

        self.assertTrue(session.release_stdin())
        self.assertTrue(stdin.closed_by_asl)
        self.assertIsNone(session.stdin_hold_reason)
        self.assertFalse(session.release_stdin())

    def test_unanswered_messages_name_their_hold(self) -> None:
        session, _ = session_with()
        session.send_prompt()
        session.steer(request_for())
        self.assertFalse(session.note_result())
        self.assertEqual("unanswered-messages", session.stdin_hold_reason)

    def test_task_followup_does_not_answer_queued_guidance(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        session.note_background_task("review", finished=False)
        session.steer(request_for())
        session.note_result()
        session.note_background_task("review", finished=True)
        self.assertFalse(session.note_result(background_followup=True))
        self.assertFalse(stdin.closed_by_asl)
        self.assertTrue(session.note_result())

    def test_main_turn_can_consume_task_results_inline(self) -> None:
        session, stdin = session_with()
        session.send_prompt()
        session.note_background_task("review", finished=False)
        session.note_background_task("review", finished=True)
        self.assertFalse(stdin.closed_by_asl)
        self.assertTrue(session.note_result())
        self.assertTrue(stdin.closed_by_asl)

    def test_error_and_interruption_end_a_background_conversation(self) -> None:
        for error in (True, False):
            with self.subTest(error=error):
                session, stdin = session_with()
                session.send_prompt()
                session.note_background_task("review", finished=False)
                if not error:
                    session._interrupt_requested = True
                self.assertTrue(session.note_result(is_error=error))
                self.assertTrue(stdin.closed_by_asl)

    def test_real_process_waits_for_background_review_followup(self) -> None:
        for origin in ({"kind": "task-notification"}, None):
            with self.subTest(origin=origin):
                self._run_background_process(origin)

    def _run_background_process(self, origin: dict[str, str] | None) -> None:
        script = self.tmp / "fake-claude"
        script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, select, sys",
                    "json.loads(sys.stdin.readline())",
                    "def emit(**record): print(json.dumps(record), flush=True)",
                    "emit(type='system', subtype='task_started', task_id='review')",
                    "emit(type='result', subtype='success', result='review running')",
                    "if select.select([sys.stdin], [], [], 0.1)[0]: sys.exit(71)",
                    "emit(type='system', subtype='task_notification',",
                    "     task_id='review')",
                    "if select.select([sys.stdin], [], [], 0.1)[0]: sys.exit(72)",
                    "emit(type='result', subtype='success', result='review finished',",
                    f"     origin={origin!r})",
                    "assert sys.stdin.read() == ''",
                ]
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        fired: list[bool] = []
        message = self.tmp / "last-message.txt"
        result = harness_claude.ClaudeAdapter(executable=str(script)).run(
            cwd=self.tmp,
            plan_dir=self.tmp,
            prompt="review the work",
            timeout=5,
            on_final_result=lambda: fired.append(True),
            last_message_path=message,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(result.timed_out)
        # Once for the main turn's result, and once more for the follow-up
        # after the task notification opened a new request; stdin still
        # waited for the follow-up, which is what the script asserts.
        self.assertEqual([True, True], fired)
        self.assertEqual("review finished\n", result.last_message)
