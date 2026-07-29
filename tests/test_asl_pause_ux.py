"""Artifact safety and pause UX tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_queue,
    events,
    harnesses,
    install_verify,
    mailbox,
    pause_policy,
    plans,
    preflight,
    prepare,
    progress_cli,
    progress_repair,
    qna,
    review,
    review_repair,
    run_control,
    runner,
    state,
    tail,
    verification,
    workbench,
    workbench_server,
)
from agent_session_loop.formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from tests.support import (
    write_plan,
    read_jsonl,
)


class ArtifactSafetyAndPauseUxTests(unittest.TestCase):
    def _seed_paused_plan(
        self,
        tmp_dir: Path,
        *,
        last_message_text: str = "Should we use 1a or 1b?\n",
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
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
            "run_id": "run-prev",
            "turn_index": 2,
            "session_id": paused_session_id,
            "session": int(paused_session_id),
            "session_number": int(paused_session_id),
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": "high",
            "effort_source": "flag",
            "artifact_dir": str(plan_state / "runs" / "run-prev" / "turn-0002"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": False,
            "paused": True,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": int(paused_session_id)},
            "details": {"session": int(paused_session_id)},
            "run_id": "run-prev",
            "run_dir": str(plan_state / "runs" / "run-prev"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": "high",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir, last_message_path

    def _paused_args(
        self,
        plan_dir: Path,
        state_dir: Path | None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(plan_dir),
            state_dir=str(state_dir) if state_dir is not None else None,
        )

    def test_set_pause_attaches_pause_policy_block(self) -> None:
        payload: dict[str, object] = {}
        runner._set_pause(
            payload, reason="verify-failed", message="checks failed"
        )
        policy = payload["pause_policy"]
        self.assertEqual(pause_policy.classify_pause("verify-failed"), policy)
        self.assertTrue(policy["recoverable"])
        self.assertFalse(policy["requires_user"])
        self.assertEqual(2, policy["max_retries"])
        self.assertEqual([], policy["backoff_seconds"])

    def test_set_pause_pause_policy_marks_user_required_harness_failure(
        self,
    ) -> None:
        payload: dict[str, object] = {}
        runner._set_pause(
            payload, reason="quota-exhausted", message="out of quota"
        )
        policy = payload["pause_policy"]
        self.assertFalse(policy["recoverable"])
        self.assertTrue(policy["requires_user"])
        self.assertEqual(0, policy["max_retries"])

    def test_waiting_state_helpers_have_no_pause_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_path = tmp_dir / events.STATE_FILE_NAME
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            payload = {
                "run_id": "run-1",
                "ok": True,
                "paused": False,
                "reason": "",
                "state_path": str(state_path),
            }
            started_at = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
            waiting = runner._waiting_for_reply_payload(
                question_id="question-1",
                session_id="9",
                turn_index=3,
                started_at=started_at,
                base_deadline=started_at + timedelta(minutes=5),
                hard_deadline=started_at + timedelta(minutes=15),
                last_lease_activity_at=started_at + timedelta(minutes=1),
            )
            persistence = runner._RunPersistence()

            with mock.patch.object(
                runner, "_notify_agent_mux_loop_paused"
            ) as notify_paused:
                runner._set_waiting_for_reply(
                    event_path,
                    payload,
                    waiting,
                    pending_outbox=2,
                    persistence=persistence,
                )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            records = read_jsonl(event_path)
            self.assertEqual(waiting, payload["waiting_for_reply"])
            self.assertEqual(waiting, persisted["waiting_for_reply"])
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("", payload["reason"])
            self.assertNotIn("pause_reason", payload)
            self.assertNotIn("pause_message", payload)
            self.assertEqual(1, len(records))
            self.assertEqual(events.MAILBOX_EVENT, records[0]["name"])
            self.assertEqual(
                events.MAILBOX_ACTION_WAIT_STARTED,
                records[0]["payload"]["action"],
            )
            self.assertEqual(2, records[0]["payload"]["pending_outbox"])
            self.assertNotIn(
                events.RUN_PAUSED_EVENT,
                [record["name"] for record in records],
            )
            notify_paused.assert_not_called()

            runner._clear_waiting_for_reply(
                event_path,
                payload,
                reason="reply",
                pending_outbox=1,
                persistence=persistence,
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("waiting_for_reply", payload)
            self.assertNotIn("waiting_for_reply", persisted)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            records = read_jsonl(event_path)
            self.assertEqual(2, len(records))
            self.assertEqual(
                events.MAILBOX_ACTION_WAIT_CLEARED,
                records[1]["payload"]["action"],
            )
            self.assertEqual("reply", records[1]["payload"]["reason"])
            self.assertEqual(1, records[1]["payload"]["pending_outbox"])
            self.assertEqual("question-1", records[1]["payload"]["question_id"])

    def test_rebase_waiting_deadlines_rewrites_state_without_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            state_path = tmp_dir / events.STATE_FILE_NAME
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            payload = {
                "run_id": "run-1",
                "ok": True,
                "paused": False,
                "reason": "",
                "state_path": str(state_path),
            }
            started_at = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
            waiting = runner._waiting_for_reply_payload(
                question_id="question-1",
                session_id="9",
                turn_index=3,
                started_at=started_at,
                base_deadline=started_at + timedelta(minutes=5),
                hard_deadline=started_at + timedelta(minutes=30),
                last_lease_activity_at=None,
            )
            persistence = runner._RunPersistence()
            runner._set_waiting_for_reply(
                event_path,
                payload,
                waiting,
                pending_outbox=1,
                persistence=persistence,
            )

            # Eight hours of suspend later, only the deadlines move.
            wake = started_at + timedelta(hours=8, seconds=2)
            runner._rebase_waiting_deadlines(
                payload,
                question_id="question-1",
                base_deadline=wake + timedelta(minutes=3),
                hard_deadline=wake + timedelta(minutes=28),
                persistence=persistence,
            )

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            records = read_jsonl(event_path)
            rebased = payload["waiting_for_reply"]
            self.assertEqual(rebased, persisted["waiting_for_reply"])
            self.assertEqual(
                (wake + timedelta(minutes=3)).isoformat(),
                rebased["base_deadline"],
            )
            self.assertEqual(
                (wake + timedelta(minutes=28)).isoformat(),
                rebased["hard_deadline"],
            )
            self.assertEqual(waiting["started_at"], rebased["started_at"])
            self.assertFalse(payload["paused"])
            self.assertEqual(1, len(records))
            self.assertEqual(
                events.MAILBOX_ACTION_WAIT_STARTED,
                records[0]["payload"]["action"],
            )

            lock_info = state.LockInfo(
                path=tmp_dir / "plan-lock.json",
                pid=os.getpid(),
                plan_dir=str(tmp_dir),
                created_at="2026-07-24T12:00:00+00:00",
                stale=False,
                reason="active",
                raw={},
            )
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_LIVE,
                state.waiting_for_reply_status(
                    rebased,
                    lock_info=lock_info,
                    now=wake,
                ),
            )
            self.assertEqual(
                state.WAITING_FOR_REPLY_STATUS_EXPIRED,
                state.waiting_for_reply_status(
                    waiting,
                    lock_info=lock_info,
                    now=wake,
                ),
            )

    def test_mailbox_reply_wait_settings_fail_safe_on_bad_overrides(self) -> None:
        settings = runner._mailbox_reply_wait_settings(
            {
                runner.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "nan",
                runner.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_ENV_VAR: "-1",
                runner.MAILBOX_REPLY_WAIT_HARD_ENV_VAR: "0",
                runner.MAILBOX_REPLY_WAIT_POLL_ENV_VAR: "not-a-number",
                runner.MAILBOX_REPLY_WAIT_IO_GRACE_ENV_VAR: "inf",
            }
        )
        self.assertEqual(
            runner.MAILBOX_REPLY_WAIT_BASE_SECONDS,
            settings.base_seconds,
        )
        self.assertEqual(
            runner.MAILBOX_REPLY_WAIT_LEASE_EXTENSION_SECONDS,
            settings.lease_extension_seconds,
        )
        self.assertEqual(
            runner.MAILBOX_REPLY_WAIT_HARD_SECONDS,
            settings.hard_seconds,
        )
        self.assertEqual(
            runner.MAILBOX_REPLY_WAIT_POLL_SECONDS,
            settings.poll_seconds,
        )
        self.assertEqual(
            runner.MAILBOX_REPLY_WAIT_IO_GRACE_SECONDS,
            settings.io_grace_seconds,
        )

        clamped = runner._mailbox_reply_wait_settings(
            {
                runner.MAILBOX_REPLY_WAIT_BASE_ENV_VAR: "20",
                runner.MAILBOX_REPLY_WAIT_HARD_ENV_VAR: "10",
            }
        )
        self.assertEqual(10.0, clamped.base_seconds)
        self.assertEqual(10.0, clamped.hard_seconds)

    def test_mailbox_question_identity_is_stable_uuid5_per_turn(self) -> None:
        first = runner._mailbox_question_id(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            session_id="10",
            turn_index=3,
        )
        repeated = runner._mailbox_question_id(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            session_id="10",
            turn_index=3,
        )
        next_turn = runner._mailbox_question_id(
            plan_dir=Path("/tmp/plan"),
            run_id="run-1",
            session_id="10",
            turn_index=4,
        )
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, next_turn)
        self.assertEqual(5, uuid.UUID(first).version)

    def test_mailbox_reply_wait_uses_lease_extensions_until_hard_bound(
        self,
    ) -> None:
        question_id = "00000000-0000-0000-0000-000000000010"
        question = mailbox.MailboxMessage(
            seq=1,
            id=question_id,
            kind=mailbox.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="Which option?",
        )
        view = mailbox.MailboxView(
            mailbox_path=Path("/tmp/mailbox.jsonl"),
            messages=(question,),
            last_seq=1,
        )
        lease_record = mailbox.MailboxLease(
            supervisor_id="00000000-0000-0000-0000-000000000011",
            updated_at="2026-07-24T12:00:00+00:00",
        )
        lease = mailbox.MailboxLeaseView(
            lease_path=Path("/tmp/mailbox-lease.json"),
            lease=lease_record,
            fresh=True,
        )
        settings = runner._MailboxReplyWaitSettings(
            base_seconds=2.0,
            lease_extension_seconds=1.0,
            hard_seconds=5.0,
            poll_seconds=0.5,
            io_grace_seconds=1.0,
        )
        now = [0.0]

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        payload = {
            "waiting_for_reply": {
                "question_id": question_id,
                "last_lease_activity_at": None,
            }
        }
        persistence = runner._RunPersistence(
            write_state=False,
            append_events=False,
        )
        with (
            mock.patch.object(
                runner.mailbox, "read_mailbox", return_value=view
            ),
            mock.patch.object(
                runner.mailbox, "read_lease", return_value=lease
            ),
        ):
            result = runner._wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload=payload,
                question_id=question_id,
                run_id="RUN-TEST",
                settings=settings,
                persistence=persistence,
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertIsNone(result.reply)
        self.assertEqual("timeout", result.reason)
        self.assertEqual(5.0, now[0])
        self.assertEqual(
            lease_record.updated_at,
            payload["waiting_for_reply"]["last_lease_activity_at"],
        )

    def test_mailbox_reply_wait_degrades_after_sustained_io_failure(
        self,
    ) -> None:
        settings = runner._MailboxReplyWaitSettings(
            base_seconds=10.0,
            lease_extension_seconds=1.0,
            hard_seconds=20.0,
            poll_seconds=1.0,
            io_grace_seconds=3.0,
        )
        now = [0.0]

        def monotonic() -> float:
            return now[0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        with mock.patch.object(
            runner.mailbox,
            "read_mailbox",
            side_effect=mailbox.MailboxError(mailbox.ERROR_MAILBOX_READ),
        ):
            result = runner._wait_for_mailbox_reply(
                plan_dir=Path("/tmp/plan"),
                state_dir_arg=None,
                payload={},
                question_id="00000000-0000-0000-0000-000000000012",
                run_id="RUN-TEST",
                settings=settings,
                persistence=runner._RunPersistence(
                    write_state=False,
                    append_events=False,
                ),
                monotonic=monotonic,
                sleep=sleep,
            )

        self.assertIsNone(result.reply)
        self.assertEqual(mailbox.ERROR_MAILBOX_READ, result.reason)
        self.assertEqual(3.0, now[0])

    def test_mailbox_reply_wait_gate_excludes_recoverable_and_failed_turns(
        self,
    ) -> None:
        success = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=0,
            stdout="",
            stderr="",
        )
        failure = harnesses.HarnessResult(
            name="codex",
            argv=["codex"],
            returncode=1,
            stdout="",
            stderr="quota exhausted",
        )
        self.assertTrue(
            runner._runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="blocked",
                result=success,
            )
        )
        self.assertFalse(
            runner._runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="verify-failed",
                result=success,
            )
        )
        self.assertFalse(
            runner._runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=True,
                pause_reason="blocked",
                result=failure,
            )
        )
        self.assertFalse(
            runner._runner_pause_can_wait_for_mailbox_reply(
                mailbox_enabled=False,
                pause_reason="blocked",
                result=success,
            )
        )

    def test_questions_payload_carries_pause_policy_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            context = qna.load_qna_context(
                str(plan_dir), state_dir=str(state_dir)
            )
            payload = qna.questions_payload(context)
            self.assertIn("pause_policy", payload)
            self.assertEqual(
                pause_policy.classify_pause(payload["pause_reason"]),
                payload["pause_policy"],
            )

    def test_runner_state_payload_classifies_paused_reason(self) -> None:
        # An older paused state file with no persisted pause_policy block still
        # surfaces one, classified from its reason (fallback path).
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(
                Path(tmp), statuses=("pending",)
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            runner_state = state._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertEqual(
                pause_policy.classify_pause("runner-pause"),
                runner_state["pause_policy"],
            )

    def test_runner_state_payload_prefers_persisted_pause_policy(self) -> None:
        # When the state file already carries a pause_policy block (written by
        # the runner), status echoes it verbatim rather than reclassifying.
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(
                Path(tmp), statuses=("pending",)
            )
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            persisted = {
                "reason": "runner-pause",
                "recoverable": False,
                "requires_user": True,
                "max_retries": 7,
                "backoff_seconds": [1.0],
            }
            raw["pause_policy"] = persisted
            state_path.write_text(json.dumps(raw), encoding="utf-8")
            runner_state = state._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertEqual(persisted, runner_state["pause_policy"])

    def test_runner_state_payload_omits_pause_policy_when_not_paused(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_state = tmp_dir / "state"
            plan_state.mkdir(parents=True)
            state_path = plan_state / events.STATE_FILE_NAME
            state_path.write_text(
                json.dumps({"ok": True, "paused": False, "reason": None}),
                encoding="utf-8",
            )
            runner_state = state._runner_state_payload(
                state_path,
                lock_info=None,
            )
            self.assertIsNone(runner_state["pause_policy"])

    def test_questions_command_text_includes_state_dir_when_provided(self) -> None:
        self.assertEqual(
            "asl questions /tmp/plan",
            runner._questions_command_text("/tmp/plan", None),
        )
        self.assertEqual(
            "asl questions /tmp/plan --state-dir /tmp/state",
            runner._questions_command_text("/tmp/plan", "/tmp/state"),
        )

    def test_reply_command_text_includes_state_dir_when_provided(self) -> None:
        self.assertEqual(
            "asl run /tmp/plan --resume --reply TEXT",
            runner._reply_command_text("/tmp/plan", None),
        )
        self.assertEqual(
            "asl run /tmp/plan --resume --state-dir /tmp/state --reply TEXT",
            runner._reply_command_text("/tmp/plan", "/tmp/state"),
        )

    def test_interactive_help_commands_shell_quote_dynamic_paths(self) -> None:
        questions = runner._questions_command_text(
            "/tmp/plan dir",
            "/tmp/state dir",
        )
        reply = runner._reply_command_text(
            "/tmp/plan dir",
            "/tmp/state dir",
        )

        self.assertEqual(
            "asl questions '/tmp/plan dir' --state-dir '/tmp/state dir'",
            questions,
        )
        self.assertEqual(
            "asl run '/tmp/plan dir' --resume "
            "--state-dir '/tmp/state dir' --reply TEXT",
            reply,
        )
        self.assertEqual(
            [
                "asl",
                "questions",
                "/tmp/plan dir",
                "--state-dir",
                "/tmp/state dir",
            ],
            shlex.split(questions),
        )
        self.assertEqual(
            [
                "asl",
                "run",
                "/tmp/plan dir",
                "--resume",
                "--state-dir",
                "/tmp/state dir",
                "--reply",
                "TEXT",
            ],
            shlex.split(reply),
        )

    def test_attach_interactive_help_populates_payload_when_qna_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, last_message_path = self._seed_paused_plan(
                Path(tmp)
            )
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            runner._attach_interactive_help(args, payload)

            help_payload = payload["interactive_help"]
            self.assertIsInstance(help_payload, dict)
            self.assertEqual(
                runner._questions_command_text(str(plan_dir), str(state_dir)),
                help_payload["questions_command"],
            )
            self.assertEqual(
                runner._reply_command_text(str(plan_dir), str(state_dir)),
                help_payload["reply_command"],
            )
            self.assertEqual("1", help_payload["session_id"])
            self.assertEqual("1", help_payload["next_pending_id"])
            self.assertEqual(2, help_payload["turn_index"])
            self.assertEqual(
                str(last_message_path),
                help_payload["artifact_paths"]["last_message_path"],
            )
            self.assertIn(
                "# Paused Agent Session - Resolver Prompt",
                help_payload["rendered_prompt"],
            )

    def test_attach_interactive_help_omits_when_no_paused_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": True,
                "plan_dir": str(plan_dir),
            }

            runner._attach_interactive_help(args, payload)

            self.assertNotIn("interactive_help", payload)

    def test_attach_interactive_help_skips_non_paused_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir, state_dir, _ = self._seed_paused_plan(Path(tmp))
            args = self._paused_args(plan_dir, state_dir)
            payload: dict[str, object] = {
                "paused": False,
                "plan_dir": str(plan_dir),
            }

            runner._attach_interactive_help(args, payload)

            self.assertNotIn("interactive_help", payload)

    def test_paused_text_output_prints_questions_hint_after_last_message(
        self,
    ) -> None:
        payload = {
            "ok": False,
            "paused": True,
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "plan_dir": "/tmp/plan",
            "harness": None,
            "completed_sessions": [],
            "commits": [],
            "turns": [
                {
                    "stdout_path": "/tmp/plan/.state/stdout.log",
                    "stderr_path": "/tmp/plan/.state/stderr.log",
                    "last_message_path": "/tmp/plan/.state/last-message.txt",
                }
            ],
            "interactive_help": {
                "questions_command": (
                    "asl questions /tmp/plan --state-dir /tmp/state"
                ),
            },
        }
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            runner._print_text_outcome(payload)

        err_text = err.getvalue()
        self.assertIn(
            "Last message: /tmp/plan/.state/last-message.txt", err_text
        )
        self.assertIn(
            "Questions: asl questions /tmp/plan --state-dir /tmp/state",
            err_text,
        )
        last_message_position = err_text.index("Last message:")
        questions_position = err_text.index("Questions:")
        self.assertLess(last_message_position, questions_position)

    def test_paused_text_output_omits_questions_hint_without_help(self) -> None:
        payload = {
            "ok": False,
            "paused": True,
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "plan_dir": "/tmp/plan",
            "harness": None,
            "completed_sessions": [],
            "commits": [],
            "turns": [
                {
                    "stdout_path": "/tmp/plan/.state/stdout.log",
                    "stderr_path": "/tmp/plan/.state/stderr.log",
                    "last_message_path": "/tmp/plan/.state/last-message.txt",
                }
            ],
        }
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            runner._print_text_outcome(payload)

        self.assertNotIn("Questions:", err.getvalue())

    def test_reply_turn_redacts_full_prompt_in_persisted_argv_and_events(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, _ = ArtifactSafetyAndPauseUxTests._seed_paused_plan(
                self,
                tmp_dir,
                last_message_text="Pick 1a or 1b?\n",
            )
            reply_text = "use 1a verbatim secret"

            def fake_run_harness(name, **kwargs):
                plan = Path(kwargs["plan_dir"])
                for session_file in sorted(plan.glob("session-*.md")):
                    text = session_file.read_text(encoding="utf-8")
                    if "- **Status**: pending" in text:
                        session_file.write_text(
                            text.replace(
                                "- **Status**: pending",
                                "- **Status**: complete",
                                1,
                            ),
                            encoding="utf-8",
                        )
                        break
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, "--prompt", str(kwargs.get("prompt", ""))],
                    returncode=0,
                    stdout="ok\n",
                    stderr="",
                )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_dir),
                harness="codex",
                review_harness=None,
                state_dir=str(state_dir),
                json=True,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
                reply=reply_text,
            )

            with mock.patch.object(
                runner.harnesses, "run_harness", side_effect=fake_run_harness
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)
            self.assertEqual(0, code, out.getvalue())

            placeholder = runner._reply_argv_placeholder(len(reply_text))

            plan_state = state.plan_state_dir(plan_dir, state_dir)
            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("reply", first_turn["prompt_kind"])
            for token in first_turn["argv"]:
                self.assertNotIn(reply_text, str(token))
                self.assertNotIn(runner.REPLY_CONTEXT_HEADING, str(token))
            self.assertIn(placeholder, first_turn["argv"])

            result_path = Path(first_turn["result_path"])
            per_turn = json.loads(result_path.read_text(encoding="utf-8"))
            for token in per_turn["argv"]:
                self.assertNotIn(reply_text, str(token))
            self.assertIn(placeholder, per_turn["argv"])

            events_path = plan_state / events.EVENTS_FILE_NAME
            records = read_jsonl(events_path)
            turn_started = [
                record
                for record in records
                if record.get("name") == "turn_started"
            ]
            self.assertTrue(turn_started)
            first_turn_started = turn_started[0]["payload"]
            self.assertEqual(placeholder, first_turn_started["prompt"])
            self.assertNotIn(reply_text, first_turn_started["prompt"])

            turn_finished = [
                record
                for record in records
                if record.get("name") == "turn_finished"
            ]
            self.assertTrue(turn_finished)
            first_turn_finished = turn_finished[0]["payload"]
            for token in first_turn_finished["argv"]:
                self.assertNotIn(reply_text, str(token))

            reply_events = [
                record
                for record in records
                if record.get("name") == events.REPLY_PROVIDED_EVENT
            ]
            self.assertEqual(1, len(reply_events))
            self.assertEqual(
                len(reply_text),
                reply_events[0]["payload"]["reply_length"],
            )
            self.assertNotIn(reply_text, json.dumps(reply_events[0]))

    def test_non_reply_turn_argv_is_not_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)

            captured_prompts: list[str] = []

            def fake_run_harness(name, **kwargs):
                captured_prompts.append(str(kwargs.get("prompt", "")))
                plan = Path(kwargs["plan_dir"])
                for session_file in sorted(plan.glob("session-*.md")):
                    text = session_file.read_text(encoding="utf-8")
                    if "- **Status**: pending" in text:
                        session_file.write_text(
                            text.replace(
                                "- **Status**: pending",
                                "- **Status**: complete",
                                1,
                            ),
                            encoding="utf-8",
                        )
                        break
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, "--prompt", str(kwargs.get("prompt", ""))],
                    returncode=0,
                    stdout="ok\n",
                    stderr="",
                )

            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_dir),
                harness="codex",
                review_harness=None,
                state_dir=str(state_dir),
                json=True,
                dry_run=False,
                unsafe_bypass=True,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                model=None,
                effort=None,
                note=None,
                allow_not_loop_ready=True,
                reply=None,
            )

            with mock.patch.object(
                runner.harnesses, "run_harness", side_effect=fake_run_harness
            ):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = runner.run_from_args(args)

            self.assertEqual(0, code, out.getvalue())
            state_path = plan_state / events.STATE_FILE_NAME
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            first_turn = state_payload["turns"][0]
            self.assertEqual("session", first_turn["prompt_kind"])
            self.assertEqual(captured_prompts[0], first_turn["argv"][-1])
            self.assertNotIn(
                "<asl reply prompt redacted",
                json.dumps(first_turn["argv"]),
            )


if __name__ == "__main__":
    unittest.main()
