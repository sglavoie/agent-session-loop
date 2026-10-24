"""Subprocess tests for reply resume, races, and stale context."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
from typing import Any

import tests.reply_flow_support as rfs
from agent_session_loop import (
    control_queue,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_leases,
    mailbox_models,
    mailbox_vocabulary,
    plans_readiness,
    qna,
    run_prompts,
    state,
)
from agent_session_loop import run_coordination_constants as wait_budgets
from tests.reply_flow_support import (
    HELPER_THREAD_JOIN_TIMEOUT_SECONDS,
    LOOP_RUN_TIMEOUT_SECONDS,
    QUESTION_PUBLISH_TIMEOUT_SECONDS,
)
from tests.support import (
    REPO_ROOT,
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class EndToEndReplyResumeTests(rfs.EndToEndReplyResumeCase):
    """Subprocess coverage for reply resumes and races."""

    def test_concurrent_mailbox_reply_race_continues_same_session_before_stop(
        self,
    ) -> None:
        self._write_plan(statuses=("pending", "pending"))
        mailbox_appenders.append_guidance(
            self.plan_dir,
            "Keep the current session scoped.",
            state_dir=self.state_dir,
        )
        mailbox_leases.write_lease(
            self.plan_dir,
            "00000000-0000-0000-0000-000000000001",
            state_dir=self.state_dir,
        )
        env = self._base_env("reply-aware")
        env.update(
            {
                wait_budgets.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "3",
                wait_budgets.MAILBOX_REPLY_WAIT_UNSUPERVISED_CEILING_ENV_VAR: "5",
                wait_budgets.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "0.02",
                wait_budgets.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "0.1",
            }
        )
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--allow-not-loop-ready",
            "--json",
        ]
        process = subprocess.Popen(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)

        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        deadline = time.monotonic() + QUESTION_PUBLISH_TIMEOUT_SECONDS
        waiting: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            if state_path.exists():
                try:
                    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
                except OSError, json.JSONDecodeError:
                    state_payload = {}
                raw_waiting = state_payload.get("waiting_for_reply")
                if isinstance(raw_waiting, dict):
                    waiting = raw_waiting
                    break
            time.sleep(0.02)
        self.assertIsNotNone(waiting)
        assert waiting is not None
        question_id = waiting["question_id"]
        self.assertIsInstance(question_id, str)

        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
        )
        race_barrier = threading.Barrier(2)
        reply_successes: list[mailbox_models.MailboxMessage] = []
        reply_failures: list[str] = []
        result_lock = threading.Lock()

        def race_reply() -> None:
            race_barrier.wait(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)
            try:
                reply = mailbox_appenders.append_reply(
                    self.plan_dir,
                    question_id,
                    self.REPLY_TEXT,
                    state_dir=self.state_dir,
                )
            except mailbox_models.MailboxError as error:
                with result_lock:
                    reply_failures.append(error.reason)
            else:
                with result_lock:
                    reply_successes.append(reply)

        reply_threads = [
            threading.Thread(target=race_reply),
            threading.Thread(target=race_reply),
        ]
        for thread in reply_threads:
            thread.start()
        for thread in reply_threads:
            thread.join(timeout=HELPER_THREAD_JOIN_TIMEOUT_SECONDS)
            self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(reply_successes))
        self.assertEqual(
            [mailbox_constants.REJECT_QUESTION_ALREADY_ANSWERED],
            reply_failures,
        )
        stdout, stderr = process.communicate(timeout=LOOP_RUN_TIMEOUT_SECONDS)

        self.assertEqual(0, process.returncode, stderr or stdout)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(2, len(payload["turns"]))
        self.assertEqual("session", payload["turns"][0]["prompt_kind"])
        self.assertEqual("reply", payload["turns"][1]["prompt_kind"])
        self.assertNotIn("waiting_for_reply", payload)

        session_one = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        session_two = (self.plan_dir / "session-2.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", session_one)
        self.assertIn("- **Status**: pending", session_two)

        mailbox_view = mailbox.read_mailbox(self.plan_dir, state_dir=self.state_dir)
        questions = [
            message
            for message in mailbox_view.messages
            if message.kind == mailbox_vocabulary.KIND_QUESTION
        ]
        self.assertEqual(1, len(questions))
        self.assertEqual(question_id, questions[0].id)
        replies = [
            message
            for message in mailbox_view.messages
            if message.kind == mailbox_vocabulary.KIND_REPLY
        ]
        self.assertEqual(reply_successes, replies)

        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        wait_events = [
            record
            for record in records
            if record.get("name") == events.MAILBOX_EVENT
            and record.get("payload", {}).get("action")
            == events.MAILBOX_ACTION_WAIT_STARTED
        ]
        cleared_events = [
            record
            for record in records
            if record.get("name") == events.MAILBOX_EVENT
            and record.get("payload", {}).get("action")
            == events.MAILBOX_ACTION_WAIT_CLEARED
        ]
        reply_events = [
            record
            for record in records
            if record.get("name") == events.REPLY_PROVIDED_EVENT
        ]
        pause_events = [
            record
            for record in records
            if record.get("name") == events.RUN_PAUSED_EVENT
        ]
        self.assertEqual(1, len(wait_events), records)
        self.assertEqual(1, len(cleared_events), records)
        self.assertEqual("reply", cleared_events[0]["payload"]["reason"])
        self.assertEqual(0, cleared_events[0]["payload"]["pending_outbox"])
        self.assertEqual(1, len(reply_events), records)
        self.assertEqual([], pause_events)
        self.assertEqual(
            run_prompts.REPLY_ORIGIN_MAILBOX,
            reply_events[0]["payload"]["origin"],
        )
        self.assertEqual(
            question_id,
            reply_events[0]["payload"]["question_id"],
        )
        reply_source = reply_events[0]["payload"]["source"]
        self.assertEqual(run_prompts.REPLY_ORIGIN_MAILBOX, reply_source["origin"])
        self.assertEqual(question_id, reply_source["question_id"])
        self.assertNotIn(self.REPLY_TEXT, json.dumps(reply_events[0]))

        control_actions = [
            record["payload"]["action"]
            for record in records
            if record.get("name") == events.RUN_CONTROL_EVENT
        ]
        self.assertIn(
            events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            control_actions,
        )
        self.assertIn(
            events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_CONSUMED,
            control_actions,
        )

    def test_dry_run_reply_preview_preserves_paused_state_for_resume(self) -> None:
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        events_path = plan_state / events.EVENTS_FILE_NAME
        paused_state = state_path.read_text(encoding="utf-8")
        paused_events = events_path.read_text(encoding="utf-8")

        dry_completed, dry_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
            dry_run=True,
        )

        self.assertEqual(0, dry_completed.returncode, dry_completed.stderr)
        self.assertTrue(dry_payload["ok"])
        self.assertEqual("dry-run", dry_payload["reason"])
        self.assertEqual("reply", dry_payload["planned"]["prompt_kind"])
        self.assertEqual(
            paused_state,
            state_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            paused_events,
            events_path.read_text(encoding="utf-8"),
        )
        context = qna.load_qna_context(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("1", context.session_id)

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
        )

        self.assertEqual(0, resume_completed.returncode, resume_completed.stderr)
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])

    def test_reply_resume_after_auto_repair_fixes_readiness_and_succeeds(
        self,
    ) -> None:
        # The first run is allow-not-loop-ready so the missing readiness
        # metadata does not block the initial pause on the operator
        # question. The reply attempt must run with the gate enforced
        # so the runner-side auto-repair fires before consuming the
        # paused reply context.
        self._write_plan()
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])
        self.assertIsNone(plans_readiness.read_readiness_metadata(self.plan_dir))

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
            allow_not_loop_ready=False,
        )

        self.assertEqual(0, resume_completed.returncode, resume_completed.stderr)
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])
        repair_payload = resume_payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        self.assertTrue(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        self.assertEqual(
            "loop-ready", repair_payload["auto_repairs"][0]["target_state"]
        )
        self.assertEqual("reply", resume_payload["turns"][0]["prompt_kind"])
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", session_text)
        # The reply was actually consumed: only metadata, not the operator
        # text, leaks into the persisted reply event.
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        records = read_jsonl(plan_state / events.EVENTS_FILE_NAME)
        reply_events = [
            record
            for record in records
            if record.get("name") == events.REPLY_PROVIDED_EVENT
        ]
        self.assertEqual(1, len(reply_events))
        self.assertNotIn(self.REPLY_TEXT, json.dumps(reply_events[0]))

    def test_failed_reply_readiness_gate_preserves_paused_context_for_retry(
        self,
    ) -> None:
        self._write_plan()
        # Inject a yellow ambiguity sentinel so the readiness gate retains a
        # non-repairable blocker; otherwise the runner-side auto-repair would
        # rebuild the missing readiness metadata and let the reply through.
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.", "Instructions. TODO: revisit later"
            ),
            encoding="utf-8",
        )
        first_completed, first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)
        self.assertTrue(first_payload["paused"])
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        state_path = plan_state / events.STATE_FILE_NAME
        events_path = plan_state / events.EVENTS_FILE_NAME
        paused_state = state_path.read_bytes()
        paused_events = events_path.read_bytes()

        failed_completed, failed_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
            allow_not_loop_ready=False,
        )

        self.assertEqual(1, failed_completed.returncode, failed_completed.stderr)
        self.assertFalse(failed_payload["ok"])
        self.assertTrue(failed_payload["paused"])
        self.assertEqual("not-loop-ready", failed_payload["pause_reason"])
        self.assertEqual(paused_state, state_path.read_bytes())
        self.assertEqual(paused_events, events_path.read_bytes())
        context = qna.load_qna_context(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("1", context.session_id)

        resume_completed, resume_payload = self._run_run(
            mode="reply-aware",
            reply=self.REPLY_TEXT,
        )

        self.assertEqual(0, resume_completed.returncode, resume_completed.stderr)
        self.assertTrue(resume_payload["ok"])
        self.assertFalse(resume_payload["paused"])
        self.assertEqual("all-complete", resume_payload["reason"])

    def test_run_with_reply_rejects_stale_paused_context(self) -> None:
        # Plan session 1 is already complete, but the paused state.json still
        # points at session 1 - the reply must be rejected with the stable
        # reply-context-mismatch reason, not silently replayed.
        self._write_plan(statuses=("complete", "pending"))
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        last_message_path = self.tmp_path / "last-message.txt"
        last_message_path.write_text(
            "Should we ship now? RUNNER_PAUSE: blocked - need answer\n",
            encoding="utf-8",
        )
        turn = {
            "run_id": "run-stale",
            "turn_index": 1,
            "session_id": "1",
            "session": 1,
            "session_number": 1,
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": None,
            "effort_source": "unresolved",
            "artifact_dir": str(plan_state / "runs" / "run-stale" / "turn-0001"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        state_payload = {
            "ok": False,
            "paused": True,
            "reason": "runner-pause",
            "pause_reason": "blocked",
            "pause_message": "blocked on operator answers",
            "pause_details": {"session": 1},
            "details": {"session": 1},
            "run_id": "run-stale",
            "run_dir": str(plan_state / "runs" / "run-stale"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": None,
            "turns": [turn],
        }
        (plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(state_payload), encoding="utf-8"
        )

        completed, payload = self._run_run(mode="reply-aware", reply=self.REPLY_TEXT)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("run", payload["command"])
        self.assertEqual(qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"])
        self.assertEqual("1", payload["details"]["paused_session_id"])
        self.assertEqual("2", payload["details"]["next_pending_id"])
        # The pending session is not advanced when the reply is rejected.
        session_two = (self.plan_dir / "session-2.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_two)

    def test_run_with_empty_reply_returns_stable_empty_reply_reason(
        self,
    ) -> None:
        self._write_plan()
        first_completed, _first_payload = self._run_run(mode="reply-aware")
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)

        completed, payload = self._run_run(mode="reply-aware", reply="   \n  ")

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual("run", payload["command"])
        self.assertEqual("empty-reply", payload["reason"])
        self.assertFalse(payload["paused"])

    def test_paused_json_payload_exposes_stable_scriptable_keys(self) -> None:
        self._write_plan()

        completed, payload = self._run_run(mode="reply-aware")

        self.assertEqual(1, completed.returncode, completed.stderr)
        # Stable top-level keys the docs publish for scripted consumers.
        for key in (
            "ok",
            "command",
            "paused",
            "pause_reason",
            "pause_message",
            "interactive_help",
            "turns",
            "state_path",
            "events_path",
        ):
            self.assertIn(key, payload)
        self.assertEqual("run", payload["command"])
        self.assertEqual("blocked", payload["pause_reason"])
        # interactive_help is the stable shape that operators script against.
        help_payload = payload["interactive_help"]
        for key in (
            "questions_command",
            "reply_command",
            "session_id",
            "next_pending_id",
            "turn_index",
            "artifact_paths",
            "rendered_prompt",
        ):
            self.assertIn(key, help_payload)
        # Artifact paths reuse qna.artifact_paths_payload so consumers can
        # share the shape across asl questions and asl run pause output.
        artifact_keys = {
            "run_dir",
            "artifact_dir",
            "stdout_path",
            "stderr_path",
            "last_message_path",
            "result_path",
        }
        self.assertEqual(
            artifact_keys,
            set(help_payload["artifact_paths"].keys()),
        )


if __name__ == "__main__":
    unittest.main()
