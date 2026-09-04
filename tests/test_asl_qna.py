"""Q&A context loader and questions CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

from agent_session_loop import (
    events,
    plans,
    qna,
    run_coordination,
    run_persistence,
    state,
)
from tests.support import (
    REPO_ROOT,
    write_plan,
)


class QnAContextLoaderTests(unittest.TestCase):
    def _seed_plan(self, tmp_dir: Path) -> tuple[Path, Path, Path]:
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, ("pending", "pending"))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        return plan_dir, state_dir, state_path

    def _write_state(
        self,
        state_path: Path,
        *,
        paused: bool = True,
        session_id: str = "1",
        last_message_path: str | None = None,
        turn_overrides: dict[str, object] | None = None,
        payload_overrides: dict[str, object] | None = None,
        turns: list[dict[str, object]] | None = None,
    ) -> None:
        if last_message_path is None:
            last_message_path = str(state_path.parent / "last-message.txt")
        turn: dict[str, object] = {
            "run_id": "run-1",
            "turn_index": 1,
            "session_id": session_id,
            "session": 1,
            "session_number": 1,
            "harness": "fake",
            "model": "test-model",
            "model_source": "default",
            "effort": "medium",
            "effort_source": "default",
            "artifact_dir": str(state_path.parent / "turn-0001"),
            "stdout_path": str(state_path.parent / "stdout.log"),
            "stderr_path": str(state_path.parent / "stderr.log"),
            "last_message_path": last_message_path,
            "result_path": str(state_path.parent / "result.json"),
        }
        if turn_overrides:
            turn.update(turn_overrides)
        payload: dict[str, object] = {
            "ok": not paused,
            "paused": paused,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": 1},
            "details": {"session": 1},
            "run_id": "run-1",
            "run_dir": str(state_path.parent / "runs" / "run-1"),
            "harness": "fake",
            "model": "test-model",
            "effort": "medium",
            "turns": [turn] if turns is None else turns,
        }
        if payload_overrides:
            payload.update(payload_overrides)
        state_path.write_text(json.dumps(payload), encoding="utf-8")

    def _write_last_message(self, path: str, text: str = "Question?\n") -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_load_qna_context_returns_structured_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(state_path, last_message_path=last_message_path)
            self._write_last_message(last_message_path, "Which option, 1a or 1b?\n")

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual("1", context.session_id)
            self.assertEqual("1", context.next_pending_id)
            self.assertEqual("run-1", context.run_id)
            self.assertEqual("runner-pause", context.pause_reason)
            self.assertEqual("agent asked a question", context.pause_message)
            self.assertEqual({"session": 1}, context.pause_details)
            self.assertEqual("fake", context.harness)
            self.assertEqual("test-model", context.model)
            self.assertEqual("medium", context.effort)
            self.assertEqual(1, context.turn_index)
            self.assertEqual(Path(last_message_path), context.last_message_path)
            self.assertEqual("Which option, 1a or 1b?\n", context.last_message)
            self.assertEqual(state_path, context.state_path)
            self.assertEqual(plans.canonicalize_path(plan_dir), context.plan_dir)

    def test_load_qna_context_accepts_live_waiting_question(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 1,
                "started_at": "2999-07-24T12:00:00+00:00",
                "base_deadline": "2999-07-24T12:05:00+00:00",
                "hard_deadline": "2999-07-24T12:15:00+00:00",
                "last_lease_activity_at": "2999-07-24T12:01:00+00:00",
            }
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
                payload = qna.questions_payload(context)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual("waiting", context.context_kind)
            self.assertEqual("question-1", context.question_id)
            self.assertEqual(waiting, context.waiting_for_reply)
            self.assertIsNone(context.pause_reason)
            self.assertIsNone(context.pause_message)
            self.assertEqual({}, context.pause_details)
            self.assertEqual("waiting", payload["context_kind"])
            self.assertEqual("question-1", payload["question_id"])
            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertNotIn("resume_command", payload)
            self.assertNotIn("pause_reason", payload)
            plan = str(plans.canonicalize_path(plan_dir))
            self.assertEqual(
                f"asl inbox reply {plan} --to question-1 "
                f"--state-dir {state_dir} --text TEXT "
                "--wait-consumed --wait-timeout 300",
                payload["reply_command"],
            )
            self.assertEqual(
                f"asl inbox reply {plan} --to question-1 "
                f"--state-dir {state_dir} --text-stdin "
                "--wait-consumed --wait-timeout 300",
                payload["reply_stdin_command"],
            )
            self.assertEqual(
                f"asl inbox resolve {plan} --to question-1 "
                f"--action continue --state-dir {state_dir} "
                "--wait-consumed --wait-timeout 300",
                payload["continue_command"],
            )
            self.assertEqual(
                f"asl inbox resolve {plan} --to question-1 "
                f"--action pause --state-dir {state_dir} "
                "--wait-consumed --wait-timeout 300",
                payload["pause_command"],
            )
            self.assertIn(
                "# Live Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )
            self.assertIn("## Resolution Choices", payload["rendered_prompt"])
            self.assertIn(payload["reply_command"], payload["rendered_prompt"])
            self.assertIn(payload["continue_command"], payload["rendered_prompt"])
            self.assertIn(payload["pause_command"], payload["rendered_prompt"])
            self.assertIn("Choose blue or green?", payload["rendered_prompt"])
            self.assertIn(
                "- Hard deadline: 2999-07-24T12:15:00+00:00",
                payload["rendered_prompt"],
            )
            self.assertNotIn("Unsupervised", payload["rendered_prompt"])

    def test_supervised_wait_past_its_anchor_still_offers_waiting_path(
        self,
    ) -> None:
        # The persisted anchor lapsed long ago; only the lease activity and
        # the ceiling span keep this wait live.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 1,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                "unsupervised_ceiling_seconds": 1800.0,
                "last_lease_activity_at": "2999-07-24T12:01:00+00:00",
            }
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
                payload = qna.questions_payload(context)
                status_payload = state.status_payload(
                    plans.inspect_plan(plan_dir),
                    state_dir,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual("waiting", context.context_kind)
            self.assertEqual("question-1", context.question_id)
            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_LIVE,
                status_payload["waiting_for_reply_status"],
            )
            self.assertIn(
                "- Unsupervised deadline: 2026-07-24T12:30:00+00:00 "
                "(rolls forward with every lease that proves a supervisor "
                "alive)",
                payload["rendered_prompt"],
            )
            self.assertIn(
                "- Unsupervised ceiling: 1800.0s",
                payload["rendered_prompt"],
            )
            self.assertNotIn("Hard deadline", payload["rendered_prompt"])

    def test_unwatched_wait_past_its_ceiling_recovers_as_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            waiting = {
                "question_id": "question-1",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 1,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                "unsupervised_ceiling_seconds": 1800.0,
                "last_lease_activity_at": None,
            }
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual("pause", context.context_kind)
            self.assertEqual(
                qna.EXPIRED_WAIT_PAUSE_REASON,
                context.pause_reason,
            )
            self.assertEqual(
                {
                    "question_id": "question-1",
                    "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                    "unsupervised_ceiling_seconds": 1800.0,
                    "last_lease_activity_at": None,
                },
                context.pause_details,
            )

    def test_rebased_wait_after_suspend_still_offers_waiting_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            # The wait started long ago and its original deadlines are far in
            # the past; a detected suspend re-anchored only the deadlines.
            raw_payload = {
                "waiting_for_reply": {
                    "question_id": "question-1",
                    "session": 1,
                    "session_id": "1",
                    "session_number": 1,
                    "turn_index": 1,
                    "started_at": "2026-07-24T12:00:00+00:00",
                    "base_deadline": "2026-07-24T12:05:00+00:00",
                    "unsupervised_deadline_at": "2026-07-24T12:30:00+00:00",
                    "unsupervised_ceiling_seconds": 1800.0,
                    "last_lease_activity_at": None,
                }
            }
            run_coordination.rebase_waiting_deadlines(
                raw_payload,
                question_id="question-1",
                base_deadline=datetime(2999, 7, 24, 20, 3, 0, tzinfo=UTC),
                unsupervised_deadline_at=datetime(2999, 7, 24, 20, 28, 0, tzinfo=UTC),
                persistence=run_persistence._RunPersistence(write_state=False),
            )
            waiting = raw_payload["waiting_for_reply"]
            self.assertEqual(
                "2999-07-24T20:03:00+00:00",
                waiting["base_deadline"],
            )
            self.assertEqual(
                "2999-07-24T20:28:00+00:00",
                waiting["unsupervised_deadline_at"],
            )
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": waiting,
                    "run_config": {"state_dir_override": str(state_dir)},
                },
            )
            self._write_last_message(last_message_path, "Choose blue or green?\n")
            state.acquire_lock(plan_dir, state_dir)

            try:
                context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual("waiting", context.context_kind)
            self.assertEqual("question-1", context.question_id)
            self.assertEqual(waiting, context.waiting_for_reply)
            self.assertIsNone(context.pause_reason)

    def test_live_waiting_identity_must_match_latest_completed_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
                payload_overrides={
                    "waiting_for_reply": {
                        "question_id": "question-1",
                        "session": 1,
                        "session_id": "1",
                        "turn_index": 2,
                        "started_at": "2999-07-24T12:00:00+00:00",
                        "base_deadline": "2999-07-24T12:05:00+00:00",
                        "hard_deadline": "2999-07-24T12:15:00+00:00",
                        "last_lease_activity_at": None,
                    }
                },
            )
            state.acquire_lock(plan_dir, state_dir)

            try:
                with self.assertRaises(qna.QnAError) as ctx:
                    qna.load_qna_context(plan_dir, state_dir=state_dir)
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(
                qna.REASON_REPLY_CONTEXT_MISMATCH,
                ctx.exception.reason,
            )
            self.assertEqual(2, ctx.exception.details["waiting_turn_index"])
            self.assertEqual(1, ctx.exception.details["turn_index"])

    def test_qna_context_payload_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(state_path, last_message_path=last_message_path)
            self._write_last_message(last_message_path)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            payload = qna.qna_context_payload(context)
            # Round-trip through json to confirm no Path leakage.
            encoded = json.dumps(payload, sort_keys=True)
            decoded = json.loads(encoded)
            self.assertEqual("1", decoded["session_id"])
            self.assertEqual("1", decoded["next_pending_id"])
            self.assertEqual(last_message_path, decoded["last_message_path"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), decoded["plan_dir"]
            )

    def test_questions_payload_carries_resume_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(
                state_path,
                last_message_path=last_message_path,
                payload_overrides={
                    "run_config": {"state_dir_override": str(state_dir)}
                },
            )
            self._write_last_message(last_message_path)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            payload = qna.questions_payload(context)

            plan = str(plans.canonicalize_path(plan_dir))
            self.assertNotIn("context_kind", payload)
            self.assertNotIn("waiting_for_reply", payload)
            self.assertNotIn("question_id", payload)
            self.assertEqual(
                f"asl run {plan} --resume --state-dir {state_dir} --reply TEXT",
                payload["resume_command"],
            )
            self.assertEqual(
                f"asl run {plan} --resume --state-dir {state_dir} --reply-stdin",
                payload["reply_stdin_command"],
            )

    def test_questions_payload_resume_omits_state_dir_without_override(
        self,
    ) -> None:
        # Legacy state (or a default state root) records no state_dir_override,
        # so the resume guidance must not fabricate a --state-dir argument.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_state(state_path, last_message_path=last_message_path)
            self._write_last_message(last_message_path)

            context = qna.load_qna_context(plan_dir, state_dir=state_dir)
            payload = qna.questions_payload(context)

            plan = str(plans.canonicalize_path(plan_dir))
            self.assertEqual(
                f"asl run {plan} --resume --reply TEXT",
                payload["resume_command"],
            )
            self.assertNotIn("--state-dir", payload["reply_stdin_command"])

    def test_load_qna_context_raises_no_paused_run_when_state_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _state_path = self._seed_plan(tmp_dir)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertIn("state", ctx.exception.error.lower())

    def test_load_qna_context_raises_no_paused_run_when_state_invalid_json(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            state_path.write_text("not json", encoding="utf-8")

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertEqual(str(state_path), ctx.exception.details["state_path"])

    def test_load_qna_context_raises_no_paused_run_when_not_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(
                state_path,
                paused=False,
                last_message_path=last_message_path,
            )

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertFalse(ctx.exception.details["paused"])

    def test_load_qna_context_raises_no_paused_run_when_no_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            self._write_state(state_path, turns=[])

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)
            self.assertIn("turn", ctx.exception.error.lower())

    def test_load_qna_context_raises_missing_question_context_when_artifact_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "missing.txt")
            self._write_state(state_path, last_message_path=last_message_path)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)
            self.assertEqual(
                last_message_path, ctx.exception.details["last_message_path"]
            )

    def test_load_qna_context_raises_missing_question_context_when_artifact_empty(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            last_message_path = str(tmp_dir / "empty.txt")
            self._write_last_message(last_message_path, "   \n")
            self._write_state(state_path, last_message_path=last_message_path)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)

    def test_load_qna_context_raises_missing_question_context_when_path_unset(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path = self._seed_plan(tmp_dir)
            self._write_state(
                state_path,
                turn_overrides={"last_message_path": ""},
            )

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_MISSING_QUESTION_CONTEXT, ctx.exception.reason)

    def test_load_qna_context_raises_reply_context_mismatch_when_session_advanced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            # Session 1 already complete; pending is now session 2.
            write_plan(plan_dir, ("complete", "pending"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(
                state_path,
                session_id="1",
                last_message_path=last_message_path,
            )

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_REPLY_CONTEXT_MISMATCH, ctx.exception.reason)
            self.assertEqual("1", ctx.exception.details["paused_session_id"])
            self.assertEqual("2", ctx.exception.details["next_pending_id"])

    def test_load_qna_context_raises_no_paused_run_when_no_pending_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("complete", "complete"))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            last_message_path = str(tmp_dir / "last-message.txt")
            self._write_last_message(last_message_path)
            self._write_state(state_path, last_message_path=last_message_path)

            with self.assertRaises(qna.QnAError) as ctx:
                qna.load_qna_context(plan_dir, state_dir=state_dir)

            self.assertEqual(qna.REASON_NO_PAUSED_RUN, ctx.exception.reason)

    def test_qna_error_payload_exposes_stable_keys(self) -> None:
        error = qna.QnAError(
            qna.REASON_REPLY_CONTEXT_MISMATCH,
            "mismatch",
            details={"paused_session_id": "1", "next_pending_id": "2"},
        )

        payload = qna.qna_error_payload(error)

        self.assertEqual(
            {
                "reason": "reply-context-mismatch",
                "error": "mismatch",
                "details": {"paused_session_id": "1", "next_pending_id": "2"},
            },
            payload,
        )


class QnAQuestionsCliTests(unittest.TestCase):
    def _seed_paused_plan(
        self,
        tmp_dir: Path,
        *,
        last_message_text: str = "Should we use 1a or 1b?\n",
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
        paused: bool = True,
    ) -> tuple[Path, Path, Path]:
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, statuses)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        last_message_path = tmp_dir / "last-message.txt"
        last_message_path.write_text(last_message_text, encoding="utf-8")
        turn = {
            "run_id": "run-1",
            "turn_index": 2,
            "session_id": paused_session_id,
            "session": int(paused_session_id) if paused_session_id.isdigit() else 1,
            "session_number": (
                int(paused_session_id) if paused_session_id.isdigit() else 1
            ),
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": "high",
            "effort_source": "flag",
            "artifact_dir": str(plan_state / "runs" / "run-1" / "turn-0002"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": not paused,
            "paused": paused,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": 1},
            "details": {"session": 1},
            "run_id": "run-1",
            "run_dir": str(plan_state / "runs" / "run-1"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": "high",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir, last_message_path

    def _run_questions(
        self,
        plan_dir: Path,
        state_dir: Path,
        *,
        json_output: bool = False,
    ) -> tuple[int, str, str]:
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            state_dir=str(state_dir),
            json=json_output,
        )
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = qna.questions_from_args(args)
        return code, out.getvalue(), err.getvalue()

    def test_text_output_renders_resolver_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Should we use 1a or 1b?\n",
            )

            code, out, err = self._run_questions(plan_dir, state_dir)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            self.assertIn("# Paused Agent Session - Resolver Prompt", out)
            self.assertIn("## Plan", out)
            self.assertIn("## Harness", out)
            self.assertIn("## Pause", out)
            self.assertIn("## Artifacts", out)
            self.assertIn("## Previous Agent Message", out)
            self.assertIn("Should we use 1a or 1b?", out)
            self.assertIn("## Instructions", out)
            self.assertIn("`asl run --resume --reply`", out)
            self.assertIn(str(last_message_path), out)
            self.assertIn("- Session ID: 1", out)

    def test_json_output_exposes_stable_top_level_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Pick 1a, 2b, or 3c?\n",
            )

            code, out, err = self._run_questions(plan_dir, state_dir, json_output=True)

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertEqual("questions", payload["command"])
            for key in (
                "plan_dir",
                "state_path",
                "run_id",
                "session_id",
                "turn_index",
                "pause_reason",
                "pause_message",
                "artifact_paths",
                "last_message",
                "rendered_prompt",
            ):
                self.assertIn(key, payload)
            self.assertEqual("1", payload["session_id"])
            self.assertEqual("1", payload["next_pending_id"])
            self.assertEqual("run-1", payload["run_id"])
            self.assertEqual(2, payload["turn_index"])
            self.assertEqual("runner-pause", payload["pause_reason"])
            self.assertEqual("agent asked a question", payload["pause_message"])
            self.assertEqual("Pick 1a, 2b, or 3c?\n", payload["last_message"])
            self.assertEqual(
                str(last_message_path),
                payload["artifact_paths"]["last_message_path"],
            )
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )
            self.assertIn("Pick 1a, 2b, or 3c?", payload["rendered_prompt"])

    def test_json_output_recovers_from_expired_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Use the strict or lenient parser?\n",
                paused=False,
            )
            state_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
            )
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["waiting_for_reply"] = {
                "question_id": "question-2",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 2,
                "started_at": "2026-07-24T12:00:00+00:00",
                "base_deadline": "2026-07-24T12:05:00+00:00",
                "hard_deadline": "2026-07-24T12:15:00+00:00",
                "last_lease_activity_at": None,
            }
            raw["run_config"] = {"state_dir_override": str(state_dir)}
            state_path.write_text(json.dumps(raw), encoding="utf-8")

            code, out, err = self._run_questions(
                plan_dir,
                state_dir,
                json_output=True,
            )

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            self.assertNotIn("context_kind", payload)
            self.assertNotIn("question_id", payload)
            self.assertNotIn("reply_command", payload)
            self.assertEqual(
                qna.EXPIRED_WAIT_PAUSE_REASON,
                payload["pause_reason"],
            )
            self.assertIn("asl run", payload["resume_command"])
            self.assertIn("--resume", payload["resume_command"])
            self.assertIn("--reply TEXT", payload["resume_command"])
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )

            status_payload = state.status_payload(
                plans.inspect_plan(plan_dir),
                state_dir,
            )
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                status_payload["waiting_for_reply_status"],
            )
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                status_payload["runner_state"]["waiting_for_reply_status"],
            )
            status_output = io.StringIO()
            with redirect_stdout(status_output):
                state._print_text_status(status_payload)
            self.assertIn("Status: expired", status_output.getvalue())

    def test_json_output_recovers_from_orphaned_future_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _last_message_path = self._seed_paused_plan(
                Path(tmp),
                last_message_text="Use the strict or lenient parser?\n",
                paused=False,
            )
            state_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
            )
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["waiting_for_reply"] = {
                "question_id": "question-2",
                "session": 1,
                "session_id": "1",
                "session_number": 1,
                "turn_index": 2,
                "started_at": "2999-07-24T12:00:00+00:00",
                "base_deadline": "2999-07-24T12:05:00+00:00",
                "hard_deadline": "2999-07-24T12:15:00+00:00",
                "last_lease_activity_at": None,
            }
            raw["run_config"] = {"state_dir_override": str(state_dir)}
            state_path.write_text(json.dumps(raw), encoding="utf-8")

            code, out, err = self._run_questions(
                plan_dir,
                state_dir,
                json_output=True,
            )

            self.assertEqual(0, code, err)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertNotIn("context_kind", payload)
            self.assertNotIn("question_id", payload)
            self.assertNotIn("reply_command", payload)
            self.assertEqual(
                qna.EXPIRED_WAIT_PAUSE_REASON,
                payload["pause_reason"],
            )
            self.assertIn("--resume", payload["resume_command"])
            self.assertIn("--reply TEXT", payload["resume_command"])

            status_payload = state.status_payload(
                plans.inspect_plan(plan_dir),
                state_dir,
            )
            self.assertFalse(status_payload["lock"]["present"])
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                status_payload["waiting_for_reply_status"],
            )

    def test_text_error_prints_to_stderr_when_no_paused_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)

            code, out, err = self._run_questions(plan_dir, state_dir)

            self.assertEqual(1, code)
            self.assertEqual("", out)
            self.assertIn("agent-session-loop questions:", err)

    def test_json_error_payload_uses_stable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)

            code, out, err = self._run_questions(plan_dir, state_dir, json_output=True)

            self.assertEqual(1, code)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("questions", payload["command"])
            self.assertEqual(qna.REASON_NO_PAUSED_RUN, payload["reason"])
            self.assertFalse(payload["paused"])
            self.assertIn("plan_dir", payload)

    def test_json_error_for_reply_context_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = self._seed_paused_plan(
                tmp_dir,
                statuses=("complete", "pending"),
                paused_session_id="1",
            )

            code, out, err = self._run_questions(plan_dir, state_dir, json_output=True)

            self.assertEqual(1, code)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual(qna.REASON_REPLY_CONTEXT_MISMATCH, payload["reason"])
            self.assertEqual("1", payload["details"]["paused_session_id"])
            self.assertEqual("2", payload["details"]["next_pending_id"])

    def test_json_error_for_malformed_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            missing_plan = tmp_dir / "does-not-exist"
            state_dir = tmp_dir / "state"

            code, out, err = self._run_questions(
                missing_plan, state_dir, json_output=True
            )

            self.assertEqual(1, code)
            self.assertEqual("", err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            self.assertEqual("questions", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])

    def test_subprocess_questions_command_wired_into_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = self._seed_paused_plan(tmp_dir)

            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "questions",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("questions", payload["command"])
            self.assertEqual("1", payload["session_id"])
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                payload["rendered_prompt"],
            )


if __name__ == "__main__":
    unittest.main()
