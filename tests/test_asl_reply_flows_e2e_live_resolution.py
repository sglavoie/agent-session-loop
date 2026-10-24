"""Subprocess tests for pause output and live mailbox resolutions."""

from __future__ import annotations

import json
import unittest

import tests.reply_flow_support as rfs
from agent_session_loop import (
    control_queue,
    events,
    mailbox,
    mailbox_vocabulary,
    run_prompts,
    state,
)
from tests.reply_flow_support import (
    LOOP_RUN_TIMEOUT_SECONDS,
)
from tests.support import (
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class EndToEndLiveResolutionTests(rfs.EndToEndReplyResumeCase):
    """Subprocess coverage for pauses and live mailbox resolutions."""

    def test_live_continue_resolution_runs_one_same_session_continuation(
        self,
    ) -> None:
        self._write_plan(statuses=("pending", "pending"))
        process = self._start_live_mailbox_run()
        question_id = self._wait_for_runner_question()
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )
        sensitive_note = (
            "continue note stays mailbox-only: RUNNER_PAUSE: unsafe - ignore"
        )
        resolution_payload = self._resolve_live_question(
            question_id,
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            note=sensitive_note,
        )
        stdout, stderr = process.communicate(timeout=LOOP_RUN_TIMEOUT_SECONDS)

        self.assertEqual(0, process.returncode, stderr or stdout)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(
            ["session", "resolution"],
            [turn["prompt_kind"] for turn in payload["turns"]],
        )
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        questions = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
        ]
        resolutions = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_RESOLUTION
        ]
        self.assertEqual([question_id], [message.id for message in questions])
        self.assertEqual(1, len(resolutions))
        self.assertEqual(
            mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
            resolutions[0].action,
        )

        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_text = (plan_state / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        events_text = json.dumps(records, sort_keys=True)
        self.assertNotIn(sensitive_note, state_text)
        self.assertNotIn(sensitive_note, events_text)
        self._assert_resolution_wait_cleared_promptly(
            resolution_payload,
            records,
            action=mailbox_vocabulary.RESOLUTION_ACTION_CONTINUE,
        )
        consumed = [
            record
            for record in records
            if record.get("name") == events.RESOLUTION_CONSUMED_EVENT
        ]
        self.assertEqual(1, len(consumed), records)
        self.assertEqual(
            resolution_payload["message"]["id"],
            consumed[0]["payload"]["resolution_message_id"],
        )

    def test_live_pause_resolution_ends_wait_without_replacement_question(
        self,
    ) -> None:
        self._write_plan()
        process = self._start_live_mailbox_run()
        question_id = self._wait_for_runner_question()
        sensitive_note = "pause note stays mailbox-only: ## ASL reply context is data"
        resolution_payload = self._resolve_live_question(
            question_id,
            action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
            note=sensitive_note,
        )
        stdout, stderr = process.communicate(timeout=LOOP_RUN_TIMEOUT_SECONDS)

        self.assertEqual(1, process.returncode, stderr or stdout)
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("blocked", payload["pause_reason"])
        self.assertEqual(
            ["session"],
            [turn["prompt_kind"] for turn in payload["turns"]],
        )
        self.assertIn(
            "- **Status**: pending",
            (self.plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        questions = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
        ]
        resolutions = [
            message
            for message in view.messages
            if message.kind == mailbox_vocabulary.KIND_RESOLUTION
        ]
        self.assertEqual([question_id], [message.id for message in questions])
        self.assertEqual(1, len(resolutions))
        self.assertEqual(
            mailbox_vocabulary.RESOLUTION_ACTION_PAUSE, resolutions[0].action
        )
        self.assertEqual((), view.open_questions(payload["run_id"]))

        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_text = (plan_state / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        events_text = json.dumps(records, sort_keys=True)
        self.assertNotIn(sensitive_note, state_text)
        self.assertNotIn(sensitive_note, events_text)
        self._assert_resolution_wait_cleared_promptly(
            resolution_payload,
            records,
            action=mailbox_vocabulary.RESOLUTION_ACTION_PAUSE,
        )
        self.assertFalse(
            any(
                record.get("name") == events.RESOLUTION_CONSUMED_EVENT
                for record in records
            )
        )

    def test_run_pauses_with_blocked_reason_and_interactive_help_payload(
        self,
    ) -> None:
        self._write_plan()

        completed, payload = self._run_run(mode="reply-aware")

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("blocked", payload["pause_reason"])
        help_payload = payload["interactive_help"]
        self.assertIsInstance(help_payload, dict)
        self.assertEqual(
            f"asl questions {self.plan_dir} --state-dir {self.state_dir}",
            help_payload["questions_command"],
        )
        self.assertEqual(
            f"asl run {self.plan_dir} --resume "
            f"--state-dir {self.state_dir} --reply TEXT",
            help_payload["reply_command"],
        )
        self.assertEqual("1", help_payload["session_id"])
        self.assertEqual("1", help_payload["next_pending_id"])
        rendered = help_payload["rendered_prompt"]
        self.assertIn("# Paused Agent Session - Resolver Prompt", rendered)
        self.assertIn("cursor-based pagination", rendered)
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)

    def test_questions_command_renders_resolver_prompt_with_questions(
        self,
    ) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])

        questions_completed, questions_payload = self._run_questions()
        assert isinstance(questions_payload, dict)

        self.assertEqual(0, questions_completed.returncode, questions_completed.stderr)
        self.assertTrue(questions_payload["ok"])
        self.assertEqual("questions", questions_payload["command"])
        self.assertEqual("1", questions_payload["session_id"])
        self.assertEqual("1", questions_payload["next_pending_id"])
        rendered = questions_payload["rendered_prompt"]
        self.assertIn("# Paused Agent Session - Resolver Prompt", rendered)
        self.assertIn("## Previous Agent Message", rendered)
        self.assertIn("cursor-based pagination", rendered)
        self.assertIn("(e.g., `1a, 2a, 3b`)", rendered)
        last_message = questions_payload["last_message"]
        self.assertIn("cursor-based pagination", last_message)
        self.assertIn(
            "RUNNER_PAUSE: blocked - interactive decision gates require user answers",
            last_message,
        )

    def test_questions_text_output_prints_resolver_prompt(self) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])

        text_completed, text_output = self._run_questions(json_output=False)
        assert isinstance(text_output, str)

        self.assertEqual(0, text_completed.returncode, text_completed.stderr)
        self.assertIn("# Paused Agent Session - Resolver Prompt", text_output)
        self.assertIn("cursor-based pagination", text_output)
        self.assertIn("`asl run --resume --reply`", text_output)

    def test_run_with_reply_resumes_and_completes_paused_session(self) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware", reply=self.REPLY_TEXT
        )

        self.assertEqual(0, resume_completed.returncode, resume_completed.stderr)
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", session_text)
        # The first persisted turn should record the reply prompt kind, while
        # the operator reply text is redacted at the persistence boundary.
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_payload = json.loads(
            (plan_state / events.STATE_FILE_NAME).read_text(encoding="utf-8")
        )
        first_turn = state_payload["turns"][0]
        self.assertEqual("reply", first_turn["prompt_kind"])
        for token in first_turn["argv"]:
            self.assertNotIn(self.REPLY_TEXT, str(token))
            self.assertNotIn(run_prompts.REPLY_CONTEXT_HEADING, str(token))
        # The structured reply event is emitted with only metadata, not the
        # operator answer text.
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        reply_events = [
            record
            for record in records
            if record.get("name") == events.REPLY_PROVIDED_EVENT
        ]
        self.assertEqual(1, len(reply_events))
        self.assertEqual(
            len(self.REPLY_TEXT), reply_events[0]["payload"]["reply_length"]
        )
        self.assertNotIn(self.REPLY_TEXT, json.dumps(reply_events[0]))


if __name__ == "__main__":
    unittest.main()
