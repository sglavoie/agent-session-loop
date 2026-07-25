"""Control queue, run control, and finish notification tests.

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
    agent_mux_delivery,
    artifacts,
    authoring,
    baseline,
    baseline_cache,
    cli,
    compact_state,
    config,
    contracts,
    control_cli,
    control_queue,
    events,
    harnesses,
    install_verify,
    mailbox,
    mailbox_cli,
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
    TTYText,
    _write_lock_file,
    _write_state_payload,
    agent_mux_notify_enabled,
    patched_env,
    read_jsonl,
    write_plan,
)


class ControlQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")
        (self.plan_dir / "session-1.md").write_text(
            "## Handoff\n\n- **Status**: pending\n",
            encoding="utf-8",
        )

    def queue_path(self) -> Path:
        return control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)

    def test_append_then_drain_round_trip(self) -> None:
        cmd = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="cmd-1",
            created_at="2026-06-24T00:00:00+00:00",
        )
        self.assertEqual("cmd-1", cmd.id)
        self.assertEqual(
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            cmd.command,
        )
        self.assertIsNone(cmd.harness)

        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(("cmd-1",), result.consumed_ids)
        self.assertEqual(1, len(result.accepted))
        self.assertEqual(
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            result.accepted[0].command,
        )
        self.assertEqual((), result.rejected)
        # Queue file is left truncated, not deleted.
        self.assertTrue(self.queue_path().exists())
        self.assertEqual("", self.queue_path().read_text(encoding="utf-8"))

    def test_drain_missing_file_is_empty_result(self) -> None:
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual((), result.accepted)
        self.assertEqual((), result.rejected)
        self.assertEqual(self.queue_path(), result.queue_path)

    def test_drain_preserves_order_across_commands(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="a",
        )
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="claude",
            command_id="b",
        )
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=self.state_dir,
            harness_model="claude-sonnet-4-5",
            command_id="model",
        )
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="codex",
            command_id="c",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(
            ["a", "b", "model", "c"],
            [cmd.id for cmd in result.accepted],
        )
        self.assertEqual(
            [None, "claude", None, "codex"],
            [cmd.harness for cmd in result.accepted],
        )
        self.assertEqual(
            "set",
            result.accepted[2].model_operation.operation,
        )

    def test_model_control_round_trip_preserves_explicit_operations(self) -> None:
        appended = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=self.state_dir,
            harness="claude",
            harness_model="claude-sonnet-4-5",
            clear_effort=True,
            command_id="model-1",
        )
        self.assertEqual(
            {"operation": "set", "value": "claude-sonnet-4-5"},
            appended.model_operation.to_payload(),
        )
        self.assertEqual(
            {"operation": "clear", "value": None},
            appended.effort_operation.to_payload(),
        )

        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual(("model-1",), result.consumed_ids)
        command = result.accepted[0]
        self.assertEqual("claude", command.harness)
        self.assertEqual("set", command.model_operation.operation)
        self.assertEqual("claude-sonnet-4-5", command.model_operation.value)
        self.assertEqual("clear", command.effort_operation.operation)
        self.assertEqual(
            "unchanged",
            control_queue.ControlCommand(
                id="other",
                command=control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                harness=None,
                created_at="",
                line_number=1,
            ).model_operation.operation,
        )

    def test_model_control_rejects_missing_conflicting_and_invalid_operations(
        self,
    ) -> None:
        queue_path = self.queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        records = (
            {
                "id": "missing",
                "command": "set-implementation-model",
            },
            {
                "id": "model-conflict",
                "command": "set-implementation-model",
                "harness_model": "gpt-5",
                "clear_harness_model": True,
            },
            {
                "id": "effort-conflict",
                "command": "set-implementation-model",
                "effort": "high",
                "clear_effort": True,
            },
            {
                "id": "invalid-clear",
                "command": "set-implementation-model",
                "clear_effort": "yes",
            },
            {
                "id": "invalid-target",
                "command": "set-implementation-model",
                "harness": "gemini",
                "clear_effort": True,
            },
        )
        queue_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )

        self.assertEqual((), result.accepted)
        self.assertEqual(
            [
                control_queue.REJECT_MISSING_MODEL_CONTROL_OPERATION,
                control_queue.REJECT_CONFLICTING_MODEL_OPERATION,
                control_queue.REJECT_CONFLICTING_EFFORT_OPERATION,
                control_queue.REJECT_INVALID_CLEAR_OPERATION,
                control_queue.REJECT_INVALID_HARNESS,
            ],
            [rejected.reason for rejected in result.rejected],
        )

    def test_append_model_control_validates_operations(self) -> None:
        invalid_kwargs = (
            {},
            {
                "harness_model": "gpt-5",
                "clear_harness_model": True,
            },
            {
                "effort": "high",
                "clear_effort": True,
            },
            {"harness_model": ""},
            {"clear_effort": "yes"},
        )
        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs), self.assertRaises(
                control_queue.ControlQueueError
            ):
                control_queue.append_command(
                    self.plan_dir,
                    control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
                    state_dir=self.state_dir,
                    **kwargs,
                )

    def test_duplicate_id_rejected_via_seen_ids(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="dup",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir, seen_ids={"dup"}
        )
        self.assertEqual((), result.accepted)
        self.assertEqual(1, len(result.rejected))
        self.assertEqual(control_queue.REJECT_DUPLICATE_ID, result.rejected[0].reason)
        self.assertEqual("dup", result.rejected[0].detail)

    def test_invalid_json_line_rejected_other_lines_processed(self) -> None:
        queue_path = self.queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            "not-json\n"
            '{"id":"ok","command":"request-stop-after-current"}\n',
            encoding="utf-8",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(("ok",), result.consumed_ids)
        self.assertEqual(1, len(result.rejected))
        self.assertEqual(control_queue.REJECT_INVALID_JSON, result.rejected[0].reason)
        self.assertEqual(1, result.rejected[0].line_number)

    def test_unknown_command_rejected(self) -> None:
        queue_path = self.queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            '{"id":"x","command":"reboot-the-runner"}\n',
            encoding="utf-8",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual((), result.accepted)
        self.assertEqual(1, len(result.rejected))
        self.assertEqual(
            control_queue.REJECT_UNKNOWN_COMMAND, result.rejected[0].reason
        )

    def test_select_future_harness_requires_known_harness(self) -> None:
        queue_path = self.queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            '{"id":"a","command":"select-future-harness","harness":"gpt5"}\n'
            '{"id":"b","command":"select-future-harness"}\n',
            encoding="utf-8",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual((), result.accepted)
        self.assertEqual(
            [control_queue.REJECT_INVALID_HARNESS, control_queue.REJECT_MISSING_HARNESS],
            [reject.reason for reject in result.rejected],
        )

    def test_missing_id_rejected(self) -> None:
        queue_path = self.queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            '{"command":"request-stop-after-current"}\n'
            '{"id":"","command":"request-stop-after-current"}\n',
            encoding="utf-8",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual((), result.accepted)
        self.assertEqual(
            [control_queue.REJECT_MISSING_ID, control_queue.REJECT_MISSING_ID],
            [reject.reason for reject in result.rejected],
        )

    def test_append_rejects_invalid_harness(self) -> None:
        with self.assertRaises(control_queue.ControlQueueError):
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_SELECT_FUTURE_HARNESS,
                state_dir=self.state_dir,
                harness="gpt5",
            )

    def test_append_rejects_unknown_command(self) -> None:
        with self.assertRaises(control_queue.ControlQueueError):
            control_queue.append_command(
                self.plan_dir,
                "reboot",  # type: ignore[arg-type]
                state_dir=self.state_dir,
            )

    def test_concurrent_appends_preserve_all_lines(self) -> None:
        ids = [f"cmd-{i}" for i in range(50)]

        def append(cid: str) -> None:
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=self.state_dir,
                command_id=cid,
            )

        threads = [threading.Thread(target=append, args=(cid,)) for cid in ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(sorted(ids), sorted(cmd.id for cmd in result.accepted))
        self.assertEqual((), result.rejected)


class MailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")

    def mailbox_path(self) -> Path:
        return mailbox.mailbox_path_for_plan(self.plan_dir, self.state_dir)

    def lease_path(self) -> Path:
        return mailbox.lease_path_for_plan(self.plan_dir, self.state_dir)

    def append_raw(self, payload: object) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as mailbox_file:
            if isinstance(payload, bytes):
                mailbox_file.write(payload)
            else:
                mailbox_file.write(
                    (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
                )

    @staticmethod
    def message_id() -> str:
        return str(uuid.uuid4())

    @staticmethod
    def timestamp() -> str:
        return "2026-07-24T12:00:00+00:00"

    def test_paths_use_canonical_per_plan_state_directory(self) -> None:
        plan_state_dir = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.assertEqual(
            plan_state_dir / mailbox.MAILBOX_FILE_NAME,
            self.mailbox_path(),
        )
        self.assertEqual(
            plan_state_dir / mailbox.MAILBOX_LEASE_FILE_NAME,
            self.lease_path(),
        )

    def test_append_and_non_destructive_read_round_trip(self) -> None:
        guidance = mailbox.append_guidance(
            self.plan_dir,
            "Prefer the existing parser.",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )
        question = mailbox.append_question(
            self.plan_dir,
            "Should this preserve legacy output?",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )
        reply = mailbox.append_reply(
            self.plan_dir,
            question.id,
            "Yes.",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )

        first = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        second = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(first, second)
        self.assertEqual((guidance, question, reply), first.messages)
        self.assertEqual(3, first.last_seq)
        self.assertEqual((), first.rejected)
        self.assertEqual(reply, first.reply_for(question.id))
        self.assertEqual((), first.unanswered_questions)
        self.assertEqual(
            {
                "acknowledgements": 0,
                "guidance": 1,
                "pending_guidance": 1,
                "questions": 1,
                "rejected": 0,
                "replies": 1,
                "unanswered_questions": 0,
            },
            first.counts.to_payload(),
        )
        self.assertGreater(first.size_bytes, 0)
        self.assertEqual(
            stat.S_IMODE(self.mailbox_path().stat().st_mode),
            0o600,
        )

    def test_missing_mailbox_is_an_empty_view_and_read_does_not_create_it(
        self,
    ) -> None:
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual((), view.messages)
        self.assertEqual((), view.rejected)
        self.assertEqual(0, view.last_seq)
        self.assertFalse(self.mailbox_path().exists())

    def test_malformed_lines_keep_physical_sequences_without_poisoning_appends(
        self,
    ) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-json")
        path.chmod(0o644)

        appended = mailbox.append_guidance(
            self.plan_dir,
            "Still valid.",
            state_dir=self.state_dir,
            message_id=self.message_id(),
            created_at=self.timestamp(),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )

        self.assertEqual(2, appended.seq)
        self.assertEqual((appended,), view.messages)
        self.assertEqual(2, view.last_seq)
        self.assertEqual(1, len(view.rejected))
        self.assertEqual(mailbox.REJECT_INVALID_JSON, view.rejected[0].reason)
        self.assertEqual(1, view.rejected[0].seq)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual(2, len(path.read_bytes().splitlines()))

    def test_validation_rejects_bad_records_and_preserves_valid_neighbors(
        self,
    ) -> None:
        valid_id = self.message_id()
        self.append_raw([])
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": self.message_id(),
                "kind": "launch",
                "version": 1,
            }
        )
        self.append_raw(
            {
                "created_at": "not-a-time",
                "id": self.message_id(),
                "kind": "guidance",
                "text": "x",
                "version": 1,
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": valid_id,
                "kind": "guidance",
                "text": "valid",
                "version": 1,
            }
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": valid_id,
                "kind": "guidance",
                "text": "duplicate",
                "version": 1,
            }
        )

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual([4], [message.seq for message in view.messages])
        self.assertEqual(
            [
                mailbox.REJECT_NOT_OBJECT,
                mailbox.REJECT_UNKNOWN_KIND,
                mailbox.REJECT_INVALID_CREATED_AT,
                mailbox.REJECT_DUPLICATE_ID,
            ],
            [record.reason for record in view.rejected],
        )
        self.assertEqual(5, view.last_seq)

    def test_acknowledgements_cannot_consume_their_own_or_future_sequence(
        self,
    ) -> None:
        first = mailbox.append_guidance(
            self.plan_dir,
            "first",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        second = mailbox.append_guidance(
            self.plan_dir,
            "second",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        acknowledged = mailbox.append_acknowledgement(
            self.plan_dir,
            first.seq,
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        self.append_raw(
            {
                "created_at": self.timestamp(),
                "id": self.message_id(),
                "kind": "ack",
                "upto": 4,
                "version": 1,
            }
        )
        third = mailbox.append_guidance(
            self.plan_dir,
            "third",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        with self.assertRaises(mailbox.MailboxError) as raised:
            mailbox.append_acknowledgement(
                self.plan_dir,
                6,
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(
            mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
            raised.exception.reason,
        )

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(acknowledged.seq, 3)
        self.assertEqual(1, view.max_acknowledged_seq)
        self.assertEqual(
            [second.id, third.id],
            [message.id for message in view.pending_guidance],
        )
        self.assertEqual(
            mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
            view.rejected[0].reason,
        )

        final_ack = mailbox.append_acknowledgement(
            self.plan_dir,
            third.seq,
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        self.assertEqual(6, final_ack.seq)
        self.assertEqual(
            (),
            mailbox.pending_guidance(
                self.plan_dir,
                state_dir=self.state_dir,
            ),
        )

    def test_reply_must_target_a_question_and_first_reply_wins_atomically(
        self,
    ) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Which implementation?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        successes: list[mailbox.MailboxMessage] = []
        failures: list[str] = []
        result_lock = threading.Lock()

        def reply(index: int) -> None:
            try:
                result = mailbox.append_reply(
                    self.plan_dir,
                    question.id,
                    f"reply-{index}",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )
            except mailbox.MailboxError as error:
                with result_lock:
                    failures.append(error.reason)
            else:
                with result_lock:
                    successes.append(result)

        threads = [
            threading.Thread(target=reply, args=(index,))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.assertEqual(1, len(successes))
        self.assertEqual(
            [mailbox.REJECT_QUESTION_ALREADY_ANSWERED] * 19,
            sorted(failures),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(successes[0], view.reply_for(question.id))
        self.assertEqual(2, len(view.messages))
        self.assertEqual((), view.unanswered_questions)

        with self.assertRaises(mailbox.MailboxError) as raised:
            mailbox.append_reply(
                self.plan_dir,
                self.message_id(),
                "orphaned",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(
            mailbox.REJECT_QUESTION_NOT_FOUND,
            raised.exception.reason,
        )

    def test_unanswered_questions_and_stateless_since_reads(self) -> None:
        first = mailbox.append_question(
            self.plan_dir,
            "first?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        second = mailbox.append_question(
            self.plan_dir,
            "second?",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        mailbox.append_reply(
            self.plan_dir,
            first.id,
            "answer",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        guidance = mailbox.append_guidance(
            self.plan_dir,
            "new guidance",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )

        self.assertEqual(
            (second,),
            mailbox.unanswered_questions(
                self.plan_dir,
                state_dir=self.state_dir,
            ),
        )
        self.assertEqual(
            (guidance,),
            mailbox.read_messages_since(
                self.plan_dir,
                3,
                state_dir=self.state_dir,
            ),
        )
        self.assertEqual(
            (first, second),
            mailbox.read_messages_since(
                self.plan_dir,
                0,
                state_dir=self.state_dir,
                kinds=(mailbox.KIND_QUESTION,),
            ),
        )

    def test_concurrent_readers_and_writers_observe_only_complete_lines(
        self,
    ) -> None:
        errors: list[str] = []
        error_lock = threading.Lock()

        def append_many(worker: int) -> None:
            for index in range(20):
                mailbox.append_guidance(
                    self.plan_dir,
                    f"{worker}:{index}",
                    state_dir=self.state_dir,
                    created_at=self.timestamp(),
                )

        def read_many() -> None:
            for _ in range(25):
                view = mailbox.read_mailbox(
                    self.plan_dir,
                    state_dir=self.state_dir,
                )
                if view.rejected:
                    with error_lock:
                        errors.extend(record.reason for record in view.rejected)

        writers = [
            threading.Thread(target=append_many, args=(worker,))
            for worker in range(4)
        ]
        readers = [threading.Thread(target=read_many) for _ in range(3)]
        for thread in writers + readers:
            thread.start()
        for thread in writers + readers:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual([], errors)
        self.assertEqual(80, len(view.messages))
        self.assertEqual(list(range(1, 81)), [m.seq for m in view.messages])
        self.assertEqual(80, len(self.mailbox_path().read_bytes().splitlines()))

    def test_size_cap_rejects_without_partial_write(self) -> None:
        path = self.mailbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        original = b"x" * mailbox.MAILBOX_MAX_BYTES
        path.write_bytes(original)

        with self.assertRaises(mailbox.MailboxError) as raised:
            mailbox.append_guidance(
                self.plan_dir,
                "does not fit",
                state_dir=self.state_dir,
                created_at=self.timestamp(),
            )
        self.assertEqual(mailbox.ERROR_MAILBOX_FULL, raised.exception.reason)
        self.assertEqual(mailbox.MAILBOX_MAX_BYTES, path.stat().st_size)
        self.assertEqual(original, path.read_bytes())

    def test_live_input_validation_uses_stable_reasons(self) -> None:
        cases = (
            (
                lambda: mailbox.append_guidance(
                    self.plan_dir,
                    "   ",
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_MISSING_TEXT,
            ),
            (
                lambda: mailbox.append_question(
                    self.plan_dir,
                    "question",
                    state_dir=self.state_dir,
                    message_id="not-a-uuid",
                ),
                mailbox.REJECT_INVALID_ID,
            ),
            (
                lambda: mailbox.append_question(
                    self.plan_dir,
                    "question",
                    state_dir=self.state_dir,
                    created_at="yesterday",
                ),
                mailbox.REJECT_INVALID_CREATED_AT,
            ),
            (
                lambda: mailbox.append_acknowledgement(
                    self.plan_dir,
                    -1,
                    state_dir=self.state_dir,
                ),
                mailbox.REJECT_INVALID_ACKNOWLEDGEMENT,
            ),
        )
        for operation, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(mailbox.MailboxError) as raised:
                    operation()
                self.assertEqual(reason, raised.exception.reason)

    def test_message_and_view_dataclasses_are_frozen(self) -> None:
        message = mailbox.append_guidance(
            self.plan_dir,
            "immutable",
            state_dir=self.state_dir,
            created_at=self.timestamp(),
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            message.text = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            view.last_seq = 99  # type: ignore[misc]

    def test_lease_round_trip_is_locked_fixed_size_and_age_bounded(self) -> None:
        supervisor_id = self.message_id()
        updated = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        lease = mailbox.write_lease(
            self.plan_dir,
            supervisor_id,
            state_dir=self.state_dir,
            updated_at=updated.isoformat(),
        )

        path = self.lease_path()
        self.assertEqual(mailbox.MAILBOX_LEASE_BYTES, path.stat().st_size)
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        fresh = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=updated + timedelta(seconds=30),
        )
        self.assertTrue(fresh.fresh)
        self.assertEqual(lease, fresh.lease)
        self.assertIsNone(fresh.reason)

        expired = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=updated
            + timedelta(seconds=mailbox.MAILBOX_LEASE_FRESH_SECONDS + 1),
        )
        self.assertFalse(expired.fresh)
        self.assertEqual(mailbox.LEASE_EXPIRED, expired.reason)
        self.assertEqual(lease, expired.lease)

    def test_missing_malformed_and_future_leases_are_not_fresh(self) -> None:
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        missing = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(missing.fresh)
        self.assertEqual(mailbox.LEASE_MISSING, missing.reason)

        mailbox.write_lease(
            self.plan_dir,
            self.message_id(),
            state_dir=self.state_dir,
            updated_at=(now + timedelta(seconds=1)).isoformat(),
        )
        future = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(future.fresh)
        self.assertEqual(mailbox.LEASE_FUTURE_UPDATED_AT, future.reason)

        self.lease_path().write_bytes(
            b"{not-json".ljust(mailbox.MAILBOX_LEASE_BYTES, b" ")
        )
        malformed = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(malformed.fresh)
        self.assertEqual(mailbox.LEASE_INVALID_JSON, malformed.reason)

        self.lease_path().write_bytes(b"{}\n")
        wrong_size = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
            now=now,
        )
        self.assertFalse(wrong_size.fresh)
        self.assertEqual(mailbox.LEASE_INVALID_SIZE, wrong_size.reason)

    def test_lease_requires_uuid_and_utc_timestamp(self) -> None:
        with self.assertRaises(mailbox.MailboxError) as invalid_id:
            mailbox.write_lease(
                self.plan_dir,
                "supervisor",
                state_dir=self.state_dir,
            )
        self.assertEqual(
            mailbox.LEASE_INVALID_SUPERVISOR_ID,
            invalid_id.exception.reason,
        )
        with self.assertRaises(mailbox.MailboxError) as invalid_time:
            mailbox.write_lease(
                self.plan_dir,
                self.message_id(),
                state_dir=self.state_dir,
                updated_at="2026-07-24T13:00:00+01:00",
            )
        self.assertEqual(
            mailbox.LEASE_INVALID_UPDATED_AT,
            invalid_time.exception.reason,
        )


class ControlQueueDrainInRunnerTests(unittest.TestCase):
    """Validate the runner-side drain helper translates commands correctly."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")

        self.payload: dict[str, object] = {
            "run_id": "test-run",
            "control": runner._initial_control_payload(),
            "harness": "codex",
        }
        self.control_payload = dict(self.payload["control"])  # type: ignore[arg-type]
        self.event_path = self.state_dir / "events.jsonl"
        self.event_path.parent.mkdir(parents=True, exist_ok=True)

    def drain(
        self,
        control_state: run_control.RunControlState,
        seen_ids: set[str] | None = None,
    ) -> tuple[control_queue.DrainResult, set[str]]:
        seen = set() if seen_ids is None else seen_ids
        result = runner._drain_external_control_queue(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=control_state,
            seen_ids=seen,
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            context=None,
            persistence=runner._RunPersistence(),
            run_control_lock=threading.Lock(),
        )
        return result, seen

    def test_request_stop_after_current_updates_control_and_emits_event(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop-1",
        )
        rc_state = run_control.RunControlState(current_harness="codex")
        result, seen = self.drain(rc_state)

        self.assertEqual(("stop-1",), result.consumed_ids)
        self.assertTrue(rc_state.stop_after_current)
        self.assertTrue(self.control_payload["stop_after_current"])
        self.assertEqual(
            events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            self.control_payload["last_action"],
        )
        self.assertEqual({"stop-1"}, seen)
        records = read_jsonl(self.event_path)
        run_control_events = [
            r["payload"] for r in records if r["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(1, len(run_control_events))
        self.assertEqual(
            events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            run_control_events[0]["action"],
        )

    def test_idempotent_request_when_already_stopping(self) -> None:
        rc_state = run_control.RunControlState(current_harness="codex")
        rc_state.toggle_stop_after_current()  # already True
        self.control_payload["stop_after_current"] = True

        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop-dup",
        )
        result, _seen = self.drain(rc_state)
        self.assertEqual(("stop-dup",), result.consumed_ids)
        records = read_jsonl(self.event_path) if self.event_path.exists() else []
        run_control_events = [
            r for r in records if r["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            0,
            len(run_control_events),
            "duplicate request must not emit an extra run_control event",
        )

    def test_select_future_harness_updates_control_payload(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="claude",
            command_id="sel-1",
        )
        rc_state = run_control.RunControlState(current_harness="codex")
        result, _seen = self.drain(rc_state)
        self.assertEqual(("sel-1",), result.consumed_ids)
        self.assertEqual("claude", self.control_payload["next_harness"])
        self.assertEqual(
            events.RUN_CONTROL_ACTION_HARNESS_SELECTED,
            self.control_payload["last_action"],
        )

    def test_select_future_harness_noop_when_already_selected(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="codex",  # same as current
            command_id="sel-noop",
        )
        rc_state = run_control.RunControlState(current_harness="codex")
        result, _seen = self.drain(rc_state)
        self.assertEqual(("sel-noop",), result.consumed_ids)
        # next_harness stays None because select_harness returns None when
        # the requested harness matches the current one.
        self.assertIsNone(self.control_payload["next_harness"])
        records = read_jsonl(self.event_path) if self.event_path.exists() else []
        self.assertEqual(
            0,
            len([r for r in records if r["name"] == events.RUN_CONTROL_EVENT]),
        )

    def test_model_control_commands_follow_harness_selection_order(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="claude",
            command_id="select-claude",
        )
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            state_dir=self.state_dir,
            harness_model="claude-opus-4-1",
            effort="high",
            command_id="set-claude-model",
        )
        rc_state = run_control.RunControlState(current_harness="codex")

        result, _seen = self.drain(rc_state)

        self.assertEqual(
            ("select-claude", "set-claude-model"),
            result.consumed_ids,
        )
        self.assertEqual("claude", self.control_payload["next_harness"])
        self.assertEqual("claude", self.control_payload["target_harness"])
        self.assertEqual(
            ["model", "effort"],
            self.control_payload["changed_fields"],
        )
        self.assertEqual(
            {
                "claude": {
                    "model": "claude-opus-4-1",
                    "effort": "high",
                }
            },
            self.control_payload["implementation_overrides"],
        )
        model_event = read_jsonl(self.event_path)[-1]["payload"]
        self.assertEqual(
            events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
            model_event["action"],
        )
        self.assertEqual("claude", model_event["target_harness"])

    def test_model_control_action_updates_payload_and_event_fields(self) -> None:
        rc_state = run_control.RunControlState(current_harness="codex")
        action = rc_state.set_implementation_model(
            model=run_control.OverrideOperation.set("gpt-5.4"),
            effort=run_control.OverrideOperation.set("high"),
        )
        self.assertIsNotNone(action)
        assert action is not None

        runner._record_run_control_action(
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            action=action,
            persistence=runner._RunPersistence(),
        )

        expected_overrides = {
            "codex": {
                "model": "gpt-5.4",
                "effort": "high",
            }
        }
        self.assertEqual("codex", self.control_payload["target_harness"])
        self.assertEqual(
            ["model", "effort"],
            self.control_payload["changed_fields"],
        )
        self.assertEqual(
            expected_overrides,
            self.control_payload["implementation_overrides"],
        )
        event_payload = read_jsonl(self.event_path)[0]["payload"]
        self.assertEqual(
            events.RUN_CONTROL_ACTION_IMPLEMENTATION_MODEL_UPDATED,
            event_payload["action"],
        )
        self.assertEqual("codex", event_payload["target_harness"])
        self.assertEqual(["model", "effort"], event_payload["changed_fields"])
        self.assertEqual(
            expected_overrides,
            event_payload["implementation_overrides"],
        )

        clear_action = rc_state.set_implementation_model(
            model=run_control.OverrideOperation.clear(),
            effort=run_control.OverrideOperation.clear(),
        )
        self.assertIsNotNone(clear_action)
        assert clear_action is not None
        runner._record_run_control_action(
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            action=clear_action,
            persistence=runner._RunPersistence(),
        )
        self.assertEqual({}, self.control_payload["implementation_overrides"])
        cleared_event = read_jsonl(self.event_path)[-1]["payload"]
        self.assertEqual("codex", cleared_event["target_harness"])
        self.assertEqual(
            ["model", "effort"],
            cleared_event["changed_fields"],
        )
        self.assertEqual({}, cleared_event["implementation_overrides"])

    def test_seen_ids_carry_across_drain_calls(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="first",
        )
        rc_state = run_control.RunControlState(current_harness="codex")
        seen: set[str] = set()
        self.drain(rc_state, seen_ids=seen)
        self.assertEqual({"first"}, seen)

        # Replay an attacker-style file rewrite using the same id.
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        queue_path.write_text(
            '{"id":"first","command":"request-stop-after-current"}\n',
            encoding="utf-8",
        )
        # Reset the flag so we can detect another transition if it (wrongly)
        # happens.
        rc_state2 = run_control.RunControlState(current_harness="codex")
        result2, _ = self.drain(rc_state2, seen_ids=seen)
        self.assertEqual((), result2.consumed_ids)
        self.assertEqual(1, len(result2.rejected))
        self.assertEqual(
            control_queue.REJECT_DUPLICATE_ID,
            result2.rejected[0].reason,
        )
        self.assertFalse(rc_state2.stop_after_current)


class MailboxPollTests(unittest.TestCase):
    """Validate live mailbox observation is metadata-only and fail-open."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")
        self.event_path = self.tmp / "events.jsonl"
        self.payload: dict[str, object] = {
            "run_id": "run-1",
            "control": runner._initial_control_payload(),
        }
        self.control_payload = dict(self.payload["control"])  # type: ignore[arg-type]
        self.persistence = runner._RunPersistence()
        self.poll = runner._MailboxPoll(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=run_control.RunControlState(current_harness="codex"),
            seen_control_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            persistence=self.persistence,
            run_control_lock=threading.Lock(),
        )
        self.poll.set_context(
            {
                "session": 7,
                "session_id": "7",
                "turn_index": 1,
                "total_sessions": 13,
            }
        )

    def mailbox_events(self) -> list[dict[str, object]]:
        if not self.event_path.exists():
            return []
        return [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.MAILBOX_EVENT
        ]

    def test_pending_guidance_emits_once_with_metadata_only(self) -> None:
        historical = mailbox.append_guidance(
            self.plan_dir,
            "already handled",
            state_dir=self.state_dir,
        )
        mailbox.append_acknowledgement(
            self.plan_dir,
            historical.seq,
            state_dir=self.state_dir,
        )
        sensitive_text = "Use the token s3cr3t-α without logging it."
        pending = mailbox.append_guidance(
            self.plan_dir,
            sensitive_text,
            state_dir=self.state_dir,
        )

        self.poll()
        self.poll()

        records = self.mailbox_events()
        self.assertEqual(1, len(records))
        event_payload = records[0]["payload"]
        self.assertEqual(
            {
                "action": events.MAILBOX_ACTION_GUIDANCE_OBSERVED,
                "digest": hashlib.sha256(
                    sensitive_text.encode("utf-8")
                ).hexdigest(),
                "length": len(sensitive_text),
                "message_id": pending.id,
                "run_id": "run-1",
                "seq": pending.seq,
                "session": 7,
                "session_id": "7",
                "total_sessions": 13,
                "turn_index": 1,
            },
            event_payload,
        )
        self.assertNotIn("text", event_payload)
        self.assertNotIn(sensitive_text, self.event_path.read_text(encoding="utf-8"))

    def test_empty_poll_does_not_write_runner_state(self) -> None:
        with mock.patch.object(
            self.persistence,
            "write_state",
            wraps=self.persistence.write_state,
        ) as write_state:
            self.poll()
        write_state.assert_not_called()
        self.assertFalse(self.event_path.exists())

    def test_new_runner_does_not_event_acknowledged_historical_guidance(
        self,
    ) -> None:
        guidance = mailbox.append_guidance(
            self.plan_dir,
            "historical",
            state_dir=self.state_dir,
        )
        mailbox.append_acknowledgement(
            self.plan_dir,
            guidance.seq,
            state_dir=self.state_dir,
        )

        self.poll()

        self.assertEqual([], self.mailbox_events())

    def test_mailbox_read_errors_fail_open_after_control_drain(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop-1",
        )
        control_state = run_control.RunControlState(current_harness="codex")
        poll = runner._MailboxPoll(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=control_state,
            seen_control_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            persistence=runner._RunPersistence(write_state=False),
            run_control_lock=threading.Lock(),
        )

        for error in (
            OSError("unreadable"),
            mailbox.MailboxError(mailbox.ERROR_MAILBOX_READ, "unreadable"),
        ):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                mailbox,
                "read_mailbox",
                side_effect=error,
            ):
                poll()

        self.assertTrue(control_state.stop_after_current)
        run_control_events = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(1, len(run_control_events))
        self.assertEqual([], self.mailbox_events())


class LiveTurnSchedulerTests(unittest.TestCase):
    """Pin the monotonic scheduler without relying on wall-clock sleeps."""

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.waits: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def wait(self, delay: float) -> bool:
            self.waits.append(delay)
            self.now += delay
            return False

    @staticmethod
    def active_turn() -> dict[str, object]:
        return {
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "total_sessions": 1,
            "harness": "codex",
            "artifact_dir": "/tmp/turn",
            "stdout_path": "/tmp/stdout.log",
            "stderr_path": "/tmp/stderr.log",
            "last_message_path": "/tmp/last-message.txt",
            "result_path": "/tmp/result.json",
            "started_at": "2026-07-24T12:00:00+00:00",
            "last_activity_at": "2026-07-24T12:00:00+00:00",
            "heartbeat_count": 0,
        }

    def test_polling_disabled_preserves_heartbeat_cadence_and_count(self) -> None:
        clock = self.Clock()
        progress = mock.Mock()
        live_turn = runner._LiveTurn(
            payload={},
            event_path=Path("/tmp/events.jsonl"),
            progress=progress,
            active_turn=self.active_turn(),
            heartbeat_interval=30.0,
            monotonic=clock.monotonic,
            wait=clock.wait,
        )
        heartbeat_times: list[float] = []

        def record_heartbeat() -> dict[str, float]:
            heartbeat_times.append(clock.now)
            if len(heartbeat_times) == 3:
                live_turn._stop.set()
            return {"at": clock.now}

        with mock.patch.object(
            live_turn,
            "_record_heartbeat",
            side_effect=record_heartbeat,
        ):
            live_turn._heartbeat_loop()

        self.assertEqual([30.0, 60.0, 90.0], heartbeat_times)
        self.assertEqual([30.0, 30.0, 30.0], clock.waits)
        self.assertEqual(3, progress.heartbeat.call_count)

    def test_long_poll_does_not_rebase_heartbeat_deadline(self) -> None:
        clock = self.Clock()
        progress = mock.Mock()
        poll_times: list[float] = []
        poll_observed_unlocked: list[bool] = []
        live_turn: runner._LiveTurn

        def poll() -> None:
            acquired = live_turn._lock.acquire(blocking=False)
            poll_observed_unlocked.append(acquired)
            if acquired:
                live_turn._lock.release()
            poll_times.append(clock.now)
            if len(poll_times) == 1:
                clock.now += 31.0

        live_turn = runner._LiveTurn(
            payload={},
            event_path=Path("/tmp/events.jsonl"),
            progress=progress,
            active_turn=self.active_turn(),
            heartbeat_interval=30.0,
            poll_callback=poll,
            poll_interval=5.0,
            monotonic=clock.monotonic,
            wait=clock.wait,
        )
        heartbeat_times: list[float] = []

        def record_heartbeat() -> dict[str, float]:
            heartbeat_times.append(clock.now)
            if len(heartbeat_times) == 2:
                live_turn._stop.set()
            return {"at": clock.now}

        with mock.patch.object(
            live_turn,
            "_record_heartbeat",
            side_effect=record_heartbeat,
        ):
            live_turn._heartbeat_loop()

        self.assertEqual([36.0, 60.0], heartbeat_times)
        self.assertEqual(5.0, poll_times[0])
        self.assertTrue(all(poll_observed_unlocked))
        self.assertEqual(2, progress.heartbeat.call_count)

    def test_finish_waits_for_overlapping_poll_before_clearing_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            clock = self.Clock()
            callback_started = threading.Event()
            release_callback = threading.Event()
            finish_done = threading.Event()
            payload: dict[str, object] = {
                "state_path": str(tmp_dir / "state.json"),
                "active_turn": None,
                "callback_state": "pending",
            }

            def poll() -> None:
                callback_started.set()
                release_callback.wait()
                payload["callback_state"] = "complete"

            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=self.active_turn(),
                heartbeat_interval=30.0,
                poll_callback=poll,
                poll_interval=5.0,
                monotonic=clock.monotonic,
                wait=clock.wait,
            )
            live_turn.start()
            self.assertTrue(callback_started.wait(timeout=2))

            def finish() -> None:
                live_turn.finish(
                    datetime(2026, 7, 24, 12, 1, tzinfo=timezone.utc)
                )
                finish_done.set()

            finish_thread = threading.Thread(target=finish)
            finish_thread.start()
            self.assertTrue(live_turn._stop.wait(timeout=2))
            self.assertFalse(finish_done.is_set())
            self.assertIsInstance(payload["active_turn"], dict)

            release_callback.set()
            finish_thread.join(timeout=2)

            self.assertFalse(finish_thread.is_alive())
            self.assertTrue(finish_done.is_set())
            self.assertEqual("complete", payload["callback_state"])
            self.assertIsNone(payload["active_turn"])
            persisted = json.loads(
                Path(str(payload["state_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual("complete", persisted["callback_state"])
            self.assertIsNone(persisted["active_turn"])


class ControlCliTests(unittest.TestCase):
    """`asl control`: the CLI producer surface for the durable control queue.

    The queue itself is covered by ``ControlQueueTests``; these tests pin the
    handler contract (enqueue plus a report of whether the command will take
    effect) and the parser wiring.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"

    def queue_path(self, plan_dir: Path | None = None) -> Path:
        return control_queue.queue_path_for_plan(
            plan_dir if plan_dir is not None else self.plan_dir,
            self.state_dir,
        )

    def args(self, plan_dir: Path | str | None = None, **overrides):
        namespace = {
            "plan_dir": str(
                self.plan_dir if plan_dir is None else plan_dir
            ),
            "state_dir": str(self.state_dir),
            "json": True,
        }
        namespace.update(overrides)
        return argparse.Namespace(**namespace)

    def mark_run_active(self, plan_dir: Path | None = None) -> None:
        """Simulate a live runner: an active lock plus a mid-flight turn."""
        target = plan_dir if plan_dir is not None else self.plan_dir
        _write_lock_file(target, self.state_dir, pid=os.getpid())
        _write_state_payload(
            target,
            self.state_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260724T100000Z-1",
                "harness": "codex",
                "reason": "",
                "run_started_at": "2026-07-24T10:00:00+00:00",
                "run_finished_at": None,
                "duration_seconds": None,
                "active_turn": {
                    "run_id": "20260724T100000Z-1",
                    "turn_index": 1,
                    "session": 1,
                    "session_id": "1",
                    "started_at": "2026-07-24T10:00:00+00:00",
                    "elapsed_seconds": 3.0,
                    "idle_seconds": 1.0,
                },
                "active_verification": None,
            },
        )

    def payload_from(self, handler, args) -> dict:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = handler(args)
        return exit_code, json.loads(buffer.getvalue())

    def test_stop_after_current_enqueues_valid_drainable_line(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))

        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("control-stop-after-current", payload["command"])
        self.assertTrue(payload["enqueued"])
        self.assertEqual(
            str(plans.canonicalize_path(self.plan_dir)), payload["plan_dir"]
        )
        self.assertEqual(str(self.queue_path()), payload["queue_path"])

        lines = read_jsonl(self.queue_path())
        self.assertEqual(1, len(lines))
        self.assertEqual(payload["command_id"], lines[0]["id"])
        self.assertEqual(
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            lines[0]["command"],
        )
        self.assertIsNone(lines[0]["harness"])

        # The runner-side consumer accepts what the CLI wrote.
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), result.rejected)
        self.assertEqual((payload["command_id"],), result.consumed_ids)

    def test_stop_after_current_active_run_with_following_session(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertTrue(payload["run_active"])
        # Session 1 is mid-flight and still reads as pending; session 2 remains.
        self.assertEqual(1, payload["pending_after_current"])
        self.assertEqual("will stop after current session", payload["note"])

    def test_stop_after_current_last_session_is_reported_as_noop(self) -> None:
        write_plan(self.plan_dir, ("complete", "pending"))
        self.mark_run_active()

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertTrue(payload["run_active"])
        self.assertEqual(0, payload["pending_after_current"])
        self.assertEqual(
            "current session is the last; stop is a no-op", payload["note"]
        )

    def test_stop_after_current_without_active_run_still_enqueues(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertFalse(payload["run_active"])
        # No turn is executing, so nothing is subtracted from the pending count.
        self.assertEqual(2, payload["pending_after_current"])
        self.assertEqual(
            "no active run; command applies when a runner next drains the queue",
            payload["note"],
        )
        self.assertTrue(self.queue_path().exists())

    def test_stop_after_current_stale_lock_is_not_an_active_run(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        _write_lock_file(self.plan_dir, self.state_dir, pid=2**30)

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertFalse(payload["run_active"])
        self.assertEqual(
            "no active run; command applies when a runner next drains the queue",
            payload["note"],
        )

    def test_stop_after_current_text_output_is_one_line(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args(json=False)
            )

        self.assertEqual(0, exit_code)
        output = buffer.getvalue().splitlines()
        self.assertEqual(1, len(output))
        self.assertIn("Queued stop-after-current for", output[0])
        self.assertIn("will stop after current session", output[0])

    def test_select_harness_enqueues_and_reports_future_harness(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        exit_code, payload = self.payload_from(
            control_cli.control_select_harness_from_args,
            self.args(harness="claude"),
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("control-select-harness", payload["command"])
        self.assertTrue(payload["enqueued"])
        self.assertEqual("claude", payload["harness"])
        self.assertTrue(payload["run_active"])
        self.assertEqual(
            "future sessions will use the claude harness", payload["note"]
        )
        # Harness selection says nothing about how many sessions remain.
        self.assertNotIn("pending_after_current", payload)

        lines = read_jsonl(self.queue_path())
        self.assertEqual(1, len(lines))
        self.assertEqual(
            control_queue.COMMAND_SELECT_FUTURE_HARNESS, lines[0]["command"]
        )
        self.assertEqual("claude", lines[0]["harness"])

    def test_select_harness_without_active_run_reports_queue_note(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        _, payload = self.payload_from(
            control_cli.control_select_harness_from_args,
            self.args(harness="copilot"),
        )

        self.assertFalse(payload["run_active"])
        self.assertEqual(
            "no active run; command applies when a runner next drains the queue",
            payload["note"],
        )

    def test_set_model_enqueues_operations_for_next_implementation_turn(
        self,
    ) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        exit_code, payload = self.payload_from(
            control_cli.control_set_model_from_args,
            self.args(
                harness="claude",
                harness_model="claude-opus-4-1",
                clear_harness_model=False,
                effort="high",
                clear_effort=False,
            ),
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("control-set-model", payload["command"])
        self.assertEqual("claude", payload["harness"])
        self.assertEqual("claude-opus-4-1", payload["harness_model"])
        self.assertEqual("high", payload["effort"])
        self.assertTrue(payload["run_active"])
        self.assertEqual(
            "next-implementation-turn", payload["applies_to"]
        )
        self.assertIn("next implementation turn", payload["note"])
        self.assertNotIn("active turn", payload["note"])

        record = read_jsonl(self.queue_path())[0]
        self.assertEqual(
            control_queue.COMMAND_SET_IMPLEMENTATION_MODEL,
            record["command"],
        )
        self.assertEqual("claude", record["harness"])
        self.assertEqual("claude-opus-4-1", record["harness_model"])
        self.assertEqual("high", record["effort"])
        drained = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual((), drained.rejected)
        self.assertEqual(
            "set", drained.accepted[0].model_operation.operation
        )
        self.assertEqual(
            "set", drained.accepted[0].effort_operation.operation
        )

    def test_set_model_can_clear_without_an_explicit_target(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        exit_code, payload = self.payload_from(
            control_cli.control_set_model_from_args,
            self.args(
                harness=None,
                harness_model=None,
                clear_harness_model=True,
                effort=None,
                clear_effort=True,
            ),
        )

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload["harness"])
        self.assertTrue(payload["clear_harness_model"])
        self.assertTrue(payload["clear_effort"])
        self.assertFalse(payload["run_active"])
        record = read_jsonl(self.queue_path())[0]
        self.assertIsNone(record["harness"])
        self.assertTrue(record["clear_harness_model"])
        self.assertTrue(record["clear_effort"])

    def test_set_model_requires_at_least_one_operation(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        exit_code, payload = self.payload_from(
            control_cli.control_set_model_from_args,
            self.args(
                harness=None,
                harness_model=None,
                clear_harness_model=False,
                effort=None,
                clear_effort=False,
            ),
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(
            control_queue.REJECT_MISSING_MODEL_CONTROL_OPERATION,
            payload["reason"],
        )
        self.assertFalse(self.queue_path().exists())

    def test_select_harness_rejects_unknown_harness_before_enqueue(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        exit_code, payload = self.payload_from(
            control_cli.control_select_harness_from_args,
            self.args(harness="gemini"),
        )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("control-select-harness", payload["command"])
        self.assertEqual("invalid-harness", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual("gemini", payload["details"]["harness"])
        self.assertFalse(self.queue_path().exists())

    def test_select_harness_invalid_harness_text_goes_to_stderr(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_select_harness_from_args(
                self.args(harness="gemini", json=False)
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn(
            "agent-session-loop control select-harness: unsupported harness",
            stderr.getvalue(),
        )

    def unusable_state_dir(self) -> Path:
        """Return a state dir whose parent is a regular file.

        Queue setup calls ``mkdir(parents=True)``, which raises
        ``NotADirectoryError`` here — the hermetic equivalent of the
        ``--state-dir /dev/null`` failure an operator hits when the state root
        is not a directory.
        """
        blocker = self.tmp / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        return blocker / "state"

    def test_stop_after_current_queue_io_failure_is_one_json_error(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        state_dir = self.unusable_state_dir()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args(state_dir=str(state_dir))
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())
        # Exactly one JSON object, so a supervisor can parse stdout whole.
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual("control-stop-after-current", payload["command"])
        self.assertEqual("os-error", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(
            str(
                control_queue.queue_path_for_plan(
                    plans.canonicalize_path(self.plan_dir), state_dir
                )
            ),
            payload["details"]["queue_path"],
        )
        self.assertIn("cannot write control queue", payload["error"])
        self.assertNotIn("Traceback", payload["error"])

    def test_select_harness_queue_io_failure_is_one_json_error(self) -> None:
        write_plan(self.plan_dir, ("pending",))
        state_dir = self.unusable_state_dir()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_select_harness_from_args(
                self.args(state_dir=str(state_dir), harness="claude")
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual("control-select-harness", payload["command"])
        self.assertEqual("os-error", payload["reason"])
        self.assertIn("cannot write control queue", payload["error"])

    def test_queue_io_failure_text_output_is_one_stderr_line(self) -> None:
        write_plan(self.plan_dir, ("pending",))
        state_dir = self.unusable_state_dir()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args(state_dir=str(state_dir), json=False)
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        lines = stderr.getvalue().splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn(
            "agent-session-loop control stop-after-current: "
            "cannot write control queue",
            lines[0],
        )
        self.assertIn("check that the state directory is writable", lines[0])

    def test_malformed_plan_reference_mirrors_status_error(self) -> None:
        broken = self.tmp / "broken-plan"
        broken.mkdir()

        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(broken),
        )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("control-stop-after-current", payload["command"])
        self.assertEqual("malformed-plan", payload["reason"])
        self.assertEqual(str(broken), payload["plan_dir"])
        self.assertEqual({}, payload["details"])
        self.assertFalse(self.queue_path(broken).exists())

    def test_ambiguous_plan_label_mirrors_status_error(self) -> None:
        first = self.tmp / "a" / "dup-label"
        second = self.tmp / "b" / "dup-label"
        write_plan(first, ("pending",))
        write_plan(second, ("pending",))
        for candidate in (first, second):
            _write_state_payload(
                candidate,
                self.state_dir,
                {"plan_dir": str(plans.canonicalize_path(candidate))},
            )

        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args("dup-label"),
        )

        self.assertEqual(1, exit_code)
        self.assertEqual("ambiguous-plan", payload["reason"])
        self.assertEqual("dup-label", payload["plan_dir"])
        self.assertEqual(
            sorted(
                [
                    str(plans.canonicalize_path(first)),
                    str(plans.canonicalize_path(second)),
                ]
            ),
            payload["details"]["candidates"],
        )
        self.assertEqual(
            str(plans.canonicalize_path(self.state_dir)),
            payload["details"]["state_root"],
        )
        self.assertEqual((), control_queue.drain_queue(
            first, state_dir=self.state_dir
        ).accepted)

    def test_ambiguous_plan_label_text_instructs_full_path(self) -> None:
        first = self.tmp / "a" / "dup-label"
        second = self.tmp / "b" / "dup-label"
        write_plan(first, ("pending",))
        write_plan(second, ("pending",))
        for candidate in (first, second):
            _write_state_payload(
                candidate,
                self.state_dir,
                {"plan_dir": str(plans.canonicalize_path(candidate))},
            )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args("dup-label", json=False)
            )

        self.assertEqual(1, exit_code)
        self.assertIn(
            "pass a full plan directory path to disambiguate", stderr.getvalue()
        )

    def test_cli_registers_control_subcommands_with_lazy_handlers(self) -> None:
        parser = cli.build_parser()

        stop_args = parser.parse_args(
            [
                "control",
                "stop-after-current",
                "/tmp/plan",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )
        self.assertEqual("control", stop_args.command)
        self.assertEqual("stop-after-current", stop_args.control_command)
        self.assertEqual("/tmp/plan", stop_args.plan_dir)
        self.assertEqual("/tmp/state", stop_args.state_dir)
        self.assertTrue(stop_args.json)
        self.assertEqual(
            "control_cli:control_stop_after_current_from_args",
            stop_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(stop_args),
            control_cli.control_stop_after_current_from_args,
        )

        harness_args = parser.parse_args(
            ["control", "select-harness", "/tmp/plan", "--harness", "claude"]
        )
        self.assertEqual("select-harness", harness_args.control_command)
        self.assertEqual("claude", harness_args.harness)
        self.assertFalse(harness_args.json)
        self.assertIsNone(harness_args.state_dir)
        self.assertEqual(
            "control_cli:control_select_harness_from_args",
            harness_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(harness_args),
            control_cli.control_select_harness_from_args,
        )

        model_args = parser.parse_args(
            [
                "control",
                "set-model",
                "/tmp/plan",
                "--harness",
                "copilot",
                "--clear-harness-model",
                "--effort",
                "high",
            ]
        )
        self.assertEqual("set-model", model_args.control_command)
        self.assertEqual("copilot", model_args.harness)
        self.assertIsNone(model_args.harness_model)
        self.assertTrue(model_args.clear_harness_model)
        self.assertEqual("high", model_args.effort)
        self.assertFalse(model_args.clear_effort)
        self.assertEqual(
            "control_cli:control_set_model_from_args",
            model_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(model_args),
            control_cli.control_set_model_from_args,
        )

    def test_cli_control_group_requires_a_subcommand(self) -> None:
        parser = cli.build_parser()

        with redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as raised:
            parser.parse_args(["control"])

        self.assertEqual(2, raised.exception.code)

    def test_cli_select_harness_requires_known_harness_choice(self) -> None:
        parser = cli.build_parser()

        with redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as raised:
            parser.parse_args(
                ["control", "select-harness", "/tmp/plan", "--harness", "gemini"]
            )

        self.assertEqual(2, raised.exception.code)

    def test_cli_model_set_and_clear_flags_are_mutually_exclusive(self) -> None:
        parser = cli.build_parser()
        cases = (
            (
                "--harness-model",
                "gpt-5.4",
                "--clear-harness-model",
            ),
            ("--effort", "high", "--clear-effort"),
        )
        for operations in cases:
            with self.subTest(operations=operations), redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit) as raised:
                parser.parse_args(
                    ["control", "set-model", "/tmp/plan", *operations]
                )
            self.assertEqual(2, raised.exception.code)


class MailboxCliTests(unittest.TestCase):
    """Public mailbox commands, cursor waits, and lazy parser wiring."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        write_plan(self.plan_dir, ("pending",))

    def args(self, **overrides) -> argparse.Namespace:
        values = {
            "plan_dir": str(self.plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
            "text": None,
            "text_stdin": False,
            "to": None,
            "peek": False,
            "question_id": None,
            "wait": False,
            "wait_timeout": mailbox_cli.DEFAULT_ASK_WAIT_TIMEOUT_SECONDS,
            "since": 0,
            "timeout": mailbox_cli.DEFAULT_OUTBOX_WAIT_TIMEOUT_SECONDS,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def payload_from(self, handler, args) -> tuple[int, dict]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = handler(args)
        return exit_code, json.loads(stdout.getvalue())

    def test_guidance_and_reply_append_records_and_renew_lease(self) -> None:
        code, guidance_payload = self.payload_from(
            mailbox_cli.inbox_guidance_from_args,
            self.args(text="Keep the parser lazy."),
        )
        self.assertEqual(0, code)
        self.assertEqual("inbox-guidance", guidance_payload["command"])
        self.assertEqual(
            "guidance", guidance_payload["message"]["kind"]
        )
        self.assertEqual(
            "Keep the parser lazy.", guidance_payload["message"]["text"]
        )
        first_lease = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertTrue(first_lease.fresh)
        self.assertEqual(
            guidance_payload["supervisor_id"], first_lease.supervisor_id
        )

        question = mailbox.append_question(
            self.plan_dir,
            "Which shape?",
            state_dir=self.state_dir,
        )
        code, reply_payload = self.payload_from(
            mailbox_cli.inbox_reply_from_args,
            self.args(text="Use one object.", to=question.id),
        )
        self.assertEqual(0, code)
        self.assertEqual("reply", reply_payload["message"]["kind"])
        self.assertEqual(question.id, reply_payload["message"]["to"])
        self.assertEqual(
            "Use one object.",
            mailbox.find_reply(
                self.plan_dir,
                question.id,
                state_dir=self.state_dir,
            ).text,
        )
        second_lease = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(
            reply_payload["supervisor_id"], second_lease.supervisor_id
        )

    def test_guidance_reads_multiline_stdin_verbatim(self) -> None:
        body = "first line\nsecond line\n"
        with mock.patch("sys.stdin", io.StringIO(body)):
            code, payload = self.payload_from(
                mailbox_cli.inbox_guidance_from_args,
                self.args(text_stdin=True),
            )
        self.assertEqual(0, code)
        self.assertEqual(body, payload["message"]["text"])

    def test_blank_body_is_one_json_error_without_writes(self) -> None:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = mailbox_cli.inbox_guidance_from_args(
                self.args(text=" \n")
            )
        self.assertEqual(1, code)
        self.assertEqual("", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual("missing-text", payload["reason"])
        self.assertFalse(
            mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )
        self.assertFalse(
            mailbox.lease_path_for_plan(
                self.plan_dir, self.state_dir
            ).exists()
        )

    def test_reply_rejects_missing_and_already_answered_questions(self) -> None:
        unknown = str(uuid.uuid4())
        code, missing = self.payload_from(
            mailbox_cli.inbox_reply_from_args,
            self.args(text="answer", to=unknown),
        )
        self.assertEqual(1, code)
        self.assertEqual("question-not-found", missing["reason"])

        question = mailbox.append_question(
            self.plan_dir,
            "question",
            state_dir=self.state_dir,
        )
        mailbox.append_reply(
            self.plan_dir,
            question.id,
            "first",
            state_dir=self.state_dir,
        )
        code, answered = self.payload_from(
            mailbox_cli.inbox_reply_from_args,
            self.args(text="second", to=question.id),
        )
        self.assertEqual(1, code)
        self.assertEqual("question-already-answered", answered["reason"])
        self.assertEqual(
            "first",
            mailbox.find_reply(
                self.plan_dir,
                question.id,
                state_dir=self.state_dir,
            ).text,
        )

    def test_inbox_read_acknowledges_one_nonempty_snapshot_once(self) -> None:
        first = mailbox.append_guidance(
            self.plan_dir,
            "one",
            state_dir=self.state_dir,
        )
        second = mailbox.append_guidance(
            self.plan_dir,
            "two",
            state_dir=self.state_dir,
        )

        code, payload = self.payload_from(
            mailbox_cli.inbox_read_from_args,
            self.args(),
        )
        self.assertEqual(0, code)
        self.assertEqual(
            [first.id, second.id],
            [record["id"] for record in payload["guidance"]],
        )
        self.assertTrue(payload["acknowledged"])
        self.assertEqual(second.seq, payload["acknowledgement"]["upto"])

        code, empty = self.payload_from(
            mailbox_cli.inbox_read_from_args,
            self.args(),
        )
        self.assertEqual(0, code)
        self.assertEqual([], empty["guidance"])
        self.assertFalse(empty["acknowledged"])
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(1, view.counts.acknowledgements)

    def test_inbox_peek_never_appends_acknowledgement(self) -> None:
        guidance = mailbox.append_guidance(
            self.plan_dir,
            "peek at me",
            state_dir=self.state_dir,
        )
        code, payload = self.payload_from(
            mailbox_cli.inbox_read_from_args,
            self.args(peek=True),
        )
        self.assertEqual(0, code)
        self.assertEqual([guidance.id], [item["id"] for item in payload["guidance"]])
        self.assertFalse(payload["acknowledged"])
        self.assertEqual(
            0,
            mailbox.read_mailbox(
                self.plan_dir,
                state_dir=self.state_dir,
            ).counts.acknowledgements,
        )

    def test_ask_new_then_resume_is_idempotent(self) -> None:
        code, created = self.payload_from(
            mailbox_cli.ask_from_args,
            self.args(text="Need a decision"),
        )
        self.assertEqual(0, code)
        self.assertTrue(created["created"])
        question_id = created["question_id"]

        code, resumed = self.payload_from(
            mailbox_cli.ask_from_args,
            self.args(question_id=question_id),
        )
        self.assertEqual(0, code)
        self.assertFalse(resumed["created"])
        self.assertEqual(question_id, resumed["question_id"])
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertEqual(1, view.counts.questions)

    def test_ask_resume_returns_an_existing_reply_without_appending(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "Need a decision",
            state_dir=self.state_dir,
        )
        reply = mailbox.append_reply(
            self.plan_dir,
            question.id,
            "Proceed.",
            state_dir=self.state_dir,
        )
        code, payload = self.payload_from(
            mailbox_cli.ask_from_args,
            self.args(question_id=question.id, wait=True, wait_timeout=0),
        )
        self.assertEqual(0, code)
        self.assertFalse(payload["created"])
        self.assertTrue(payload["answered"])
        self.assertFalse(payload["timed_out"])
        self.assertEqual(reply.id, payload["reply"]["id"])
        self.assertEqual(
            1,
            mailbox.read_mailbox(
                self.plan_dir,
                state_dir=self.state_dir,
            ).counts.questions,
        )

    def test_ask_wait_timeout_zero_performs_one_operation_read(self) -> None:
        with mock.patch.object(
            mailbox_cli.mailbox,
            "read_mailbox",
            wraps=mailbox.read_mailbox,
        ) as read:
            code, payload = self.payload_from(
                mailbox_cli.ask_from_args,
                self.args(
                    text="Anyone there?",
                    wait=True,
                    wait_timeout=0,
                ),
            )
        self.assertEqual(0, code)
        self.assertTrue(payload["timed_out"])
        self.assertFalse(payload["answered"])
        # Plan resolution first reads the authoritative status projection;
        # the zero-timeout wait then performs exactly one operation read.
        self.assertEqual(2, read.call_count)

    def test_ask_resume_requires_an_existing_question(self) -> None:
        code, payload = self.payload_from(
            mailbox_cli.ask_from_args,
            self.args(question_id=str(uuid.uuid4())),
        )
        self.assertEqual(1, code)
        self.assertEqual("question-not-found", payload["reason"])

    def test_outbox_timeout_zero_returns_all_questions_and_monotonic_cursor(
        self,
    ) -> None:
        mailbox.append_guidance(
            self.plan_dir,
            "guidance",
            state_dir=self.state_dir,
        )
        first = mailbox.append_question(
            self.plan_dir,
            "first",
            state_dir=self.state_dir,
        )
        mailbox.append_reply(
            self.plan_dir,
            first.id,
            "answered",
            state_dir=self.state_dir,
        )
        second = mailbox.append_question(
            self.plan_dir,
            "second",
            state_dir=self.state_dir,
        )

        code, payload = self.payload_from(
            mailbox_cli.outbox_wait_from_args,
            self.args(since=1, timeout=0),
        )
        self.assertEqual(0, code)
        self.assertFalse(payload["timed_out"])
        self.assertEqual(
            [first.id, second.id],
            [question["id"] for question in payload["questions"]],
        )
        self.assertEqual(4, payload["next_since"])
        lease = mailbox.read_lease(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        self.assertTrue(lease.fresh)
        self.assertEqual(payload["supervisor_id"], lease.supervisor_id)

        code, empty = self.payload_from(
            mailbox_cli.outbox_wait_from_args,
            self.args(since=99, timeout=0),
        )
        self.assertEqual(0, code)
        self.assertTrue(empty["timed_out"])
        self.assertEqual(99, empty["next_since"])

    def test_outbox_retries_transient_reads(self) -> None:
        question = mailbox.append_question(
            self.plan_dir,
            "eventual",
            state_dir=self.state_dir,
        )
        view = mailbox.read_mailbox(
            self.plan_dir,
            state_dir=self.state_dir,
        )
        transient = mailbox.MailboxError(
            mailbox.ERROR_MAILBOX_READ,
            "temporarily unavailable",
            path=view.mailbox_path,
        )
        with mock.patch.object(
            mailbox_cli.mailbox,
            "read_mailbox",
            side_effect=[transient, view],
        ), mock.patch.object(mailbox_cli.time, "sleep"):
            code, payload = self.payload_from(
                mailbox_cli.outbox_wait_from_args,
                self.args(timeout=1),
            )
        self.assertEqual(0, code)
        self.assertEqual([question.id], [item["id"] for item in payload["questions"]])

    def test_outbox_reports_stable_error_when_no_read_succeeds(self) -> None:
        error = mailbox.MailboxError(
            mailbox.ERROR_MAILBOX_READ,
            "unavailable",
            path=mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            ),
        )
        with mock.patch.object(
            mailbox_cli.mailbox,
            "read_mailbox",
            side_effect=error,
        ):
            code, payload = self.payload_from(
                mailbox_cli.outbox_wait_from_args,
                self.args(timeout=0),
            )
        self.assertEqual(1, code)
        self.assertEqual("mailbox-read-error", payload["reason"])
        self.assertIn("path", payload["details"])

    def test_outbox_renews_lease_after_thirty_seconds(self) -> None:
        empty = mailbox.MailboxView(
            mailbox_path=mailbox.mailbox_path_for_plan(
                self.plan_dir, self.state_dir
            )
        )
        question = mailbox.MailboxMessage(
            seq=1,
            id=str(uuid.uuid4()),
            kind=mailbox.KIND_QUESTION,
            created_at="2026-07-24T12:00:00+00:00",
            text="after renewal",
        )
        found = mailbox.MailboxView(
            mailbox_path=empty.mailbox_path,
            messages=(question,),
            last_seq=1,
        )
        with mock.patch.object(
            mailbox_cli.mailbox,
            "read_mailbox",
            side_effect=[empty, found],
        ), mock.patch.object(
            mailbox_cli,
            "_write_supervisor_lease",
        ) as renew, mock.patch.object(
            mailbox_cli.time,
            "monotonic",
            side_effect=[0.0, 30.0],
        ), mock.patch.object(mailbox_cli.time, "sleep"):
            questions, next_since, timed_out = mailbox_cli._wait_for_questions(
                self.plan_dir,
                state_dir=self.state_dir,
                since=0,
                timeout=60,
                supervisor_id=str(uuid.uuid4()),
            )
        self.assertEqual((question,), questions)
        self.assertEqual(1, next_since)
        self.assertFalse(timed_out)
        self.assertEqual(2, renew.call_count)

    def test_negative_cursor_and_unbounded_timeouts_are_json_errors(self) -> None:
        cases = (
            (
                mailbox_cli.outbox_wait_from_args,
                self.args(since=-1, timeout=0),
                "invalid-since",
            ),
            (
                mailbox_cli.outbox_wait_from_args,
                self.args(timeout=mailbox_cli.MAX_WAIT_TIMEOUT_SECONDS + 1),
                "invalid-timeout",
            ),
            (
                mailbox_cli.ask_from_args,
                self.args(
                    text="question",
                    wait_timeout=mailbox_cli.MAX_WAIT_TIMEOUT_SECONDS + 1,
                ),
                "invalid-timeout",
            ),
        )
        for handler, args, reason in cases:
            with self.subTest(reason=reason):
                code, payload = self.payload_from(handler, args)
                self.assertEqual(1, code)
                self.assertEqual(reason, payload["reason"])

    def test_mailbox_and_control_share_plan_error_envelopes(self) -> None:
        broken = self.tmp / "broken"
        broken.mkdir()
        control_code, control_payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(plan_dir=str(broken)),
        )
        mailbox_code, mailbox_payload = self.payload_from(
            mailbox_cli.inbox_guidance_from_args,
            self.args(plan_dir=str(broken), text="body"),
        )
        self.assertEqual((1, 1), (control_code, mailbox_code))
        for key in (
            "ok",
            "reason",
            "error",
            "paused",
            "details",
            "plan_dir",
        ):
            self.assertEqual(control_payload[key], mailbox_payload[key])

    def test_cli_registers_every_mailbox_handler_lazily(self) -> None:
        parser = cli.build_parser()
        cases = (
            (
                ["inbox", "guidance", "/tmp/plan", "--text", "body"],
                "mailbox_cli:inbox_guidance_from_args",
                mailbox_cli.inbox_guidance_from_args,
            ),
            (
                [
                    "inbox",
                    "reply",
                    "/tmp/plan",
                    "--to",
                    str(uuid.uuid4()),
                    "--text-stdin",
                ],
                "mailbox_cli:inbox_reply_from_args",
                mailbox_cli.inbox_reply_from_args,
            ),
            (
                ["inbox", "read", "/tmp/plan", "--peek"],
                "mailbox_cli:inbox_read_from_args",
                mailbox_cli.inbox_read_from_args,
            ),
            (
                ["ask", "/tmp/plan", "question", "--wait", "--wait-timeout", "0"],
                "mailbox_cli:ask_from_args",
                mailbox_cli.ask_from_args,
            ),
            (
                ["outbox", "wait", "/tmp/plan", "--since", "2", "--timeout", "0"],
                "mailbox_cli:outbox_wait_from_args",
                mailbox_cli.outbox_wait_from_args,
            ),
        )
        for argv, handler_ref, handler in cases:
            with self.subTest(argv=argv):
                parsed = parser.parse_args(argv)
                self.assertEqual(handler_ref, parsed.handler_ref)
                self.assertIs(handler, cli.resolve_handler(parsed))

    def test_parser_requires_message_and_question_sources(self) -> None:
        parser = cli.build_parser()
        cases = (
            ["inbox", "guidance", "/tmp/plan"],
            [
                "inbox",
                "guidance",
                "/tmp/plan",
                "--text",
                "one",
                "--text-stdin",
            ],
            ["ask", "/tmp/plan"],
            [
                "ask",
                "/tmp/plan",
                "question",
                "--question-id",
                str(uuid.uuid4()),
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv), redirect_stderr(
                io.StringIO()
            ), self.assertRaises(SystemExit) as raised:
                parser.parse_args(argv)
            self.assertEqual(2, raised.exception.code)

        with redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as raised:
            parser.parse_args(["control", "select-harness", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)


class RunControlTests(unittest.TestCase):
    def run_args(self, *, json_output: bool = False, dry_run: bool = False):
        return mock.Mock(json=json_output, dry_run=dry_run)

    def test_run_control_state_toggles_and_consumes_stop_after_current(self) -> None:
        state = run_control.RunControlState()

        self.assertFalse(state.stop_after_current)
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "implementation_overrides": {},
            },
            state.snapshot(),
        )
        requested = state.toggle_stop_after_current()
        self.assertEqual("request", requested.action)
        self.assertTrue(requested.stop_after_current)
        self.assertTrue(state.stop_after_current)

        canceled = state.toggle_stop_after_current()
        self.assertEqual("cancel", canceled.action)
        self.assertFalse(canceled.stop_after_current)
        self.assertFalse(state.stop_after_current)
        self.assertIsNone(state.consume_stop_after_current())

        state.toggle_stop_after_current()
        consumed = state.consume_stop_after_current()
        self.assertIsNotNone(consumed)
        assert consumed is not None
        self.assertEqual("consumed", consumed.action)
        self.assertFalse(consumed.stop_after_current)
        self.assertFalse(state.stop_after_current)

    def test_run_control_state_tracks_tail_and_next_harness(self) -> None:
        state = run_control.RunControlState(current_harness="codex")

        status = state.request_status_snapshot()
        self.assertEqual("status", status.action)
        self.assertFalse(status.tail_enabled)
        self.assertIsNone(status.next_harness)

        enabled = state.toggle_tail()
        self.assertEqual("tail-enabled", enabled.action)
        self.assertTrue(enabled.tail_enabled)
        self.assertTrue(state.tail_enabled)

        disabled = state.toggle_tail()
        self.assertEqual("tail-disabled", disabled.action)
        self.assertFalse(disabled.tail_enabled)
        self.assertFalse(state.tail_enabled)

        self.assertIsNone(state.select_harness("codex"))
        selected = state.select_harness("claude")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("harness-selected", selected.action)
        self.assertEqual("claude", selected.next_harness)
        self.assertEqual("claude", state.next_harness)
        self.assertIsNone(state.select_harness("claude"))

    def test_run_control_state_sets_and_clears_harness_overrides(self) -> None:
        state = run_control.RunControlState(current_harness="codex")

        set_action = state.set_implementation_model(
            target_harness="claude",
            model=run_control.OverrideOperation.set("claude-sonnet-4-5"),
            effort=run_control.OverrideOperation.set("high"),
        )

        self.assertIsNotNone(set_action)
        assert set_action is not None
        self.assertEqual("implementation-model-updated", set_action.action)
        self.assertEqual("claude", set_action.target_harness)
        self.assertEqual(("model", "effort"), set_action.changed_fields)
        self.assertEqual(
            {
                "claude": {
                    "model": "claude-sonnet-4-5",
                    "effort": "high",
                }
            },
            state.implementation_overrides,
        )
        self.assertIsNone(
            state.set_implementation_model(
                target_harness="claude",
                model=run_control.OverrideOperation.set(
                    "claude-sonnet-4-5"
                ),
            )
        )

        clear_model = state.set_implementation_model(
            target_harness="claude",
            model=run_control.OverrideOperation.clear(),
        )
        self.assertIsNotNone(clear_model)
        assert clear_model is not None
        self.assertEqual(("model",), clear_model.changed_fields)
        self.assertEqual(
            {"claude": {"effort": "high"}},
            state.implementation_overrides,
        )

        clear_effort = state.set_implementation_model(
            target_harness="claude",
            effort=run_control.OverrideOperation.clear(),
        )
        self.assertIsNotNone(clear_effort)
        self.assertEqual({}, state.implementation_overrides)

    def test_run_control_state_defers_default_target_until_transition(self) -> None:
        state = run_control.RunControlState(current_harness="codex")
        selected = state.select_harness("claude")
        self.assertIsNotNone(selected)

        action = state.set_implementation_model(
            model=run_control.OverrideOperation.set("claude-opus-4-1"),
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual("claude", action.target_harness)
        self.assertEqual(
            {"claude": {"model": "claude-opus-4-1"}},
            action.implementation_overrides,
        )
        explicit = state.set_implementation_model(
            target_harness="codex",
            effort=run_control.OverrideOperation.set("xhigh"),
        )
        self.assertIsNotNone(explicit)
        assert explicit is not None
        self.assertEqual("codex", explicit.target_harness)
        self.assertEqual(
            {
                "claude": {"model": "claude-opus-4-1"},
                "codex": {"effort": "xhigh"},
            },
            state.implementation_overrides,
        )

    def test_runner_model_resolution_prioritizes_controls_and_isolates_review(
        self,
    ) -> None:
        args = argparse.Namespace(
            harness="codex",
            harness_model="gpt-5-flag",
            review_harness_model="claude-review",
            effort="medium",
        )
        overrides = {
            "codex": {"model": "gpt-5-control", "effort": "xhigh"},
            "claude": {"model": "claude-control", "effort": "high"},
        }

        self.assertEqual(
            "gpt-5-control",
            runner._requested_model(
                args,
                harness="codex",
                implementation_overrides=overrides,
            ),
        )
        self.assertEqual(
            "xhigh",
            runner._requested_effort(
                args,
                harness="codex",
                implementation_overrides=overrides,
            ),
        )
        selected = runner._selected_model_effort_payload(
            args,
            harness="codex",
            implementation_overrides=overrides,
        )
        self.assertEqual("gpt-5-control", selected["model"])
        self.assertEqual(config.MODEL_SOURCE_CONTROL, selected["model_source"])
        self.assertEqual("xhigh", selected["effort"])
        self.assertEqual(
            config.EFFORT_SOURCE_CONTROL, selected["effort_source"]
        )

        cleared = runner._selected_model_effort_payload(
            args,
            harness="codex",
            implementation_overrides={},
        )
        self.assertEqual("gpt-5-flag", cleared["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, cleared["model_source"])
        self.assertEqual("medium", cleared["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, cleared["effort_source"])

        review = runner._selected_model_effort_payload(
            args,
            harness="claude",
            is_review_turn=True,
            implementation_overrides=overrides,
        )
        self.assertEqual("claude-review", review["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, review["model_source"])
        self.assertEqual("medium", review["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, review["effort_source"])

    def test_override_operations_keep_unchanged_set_and_clear_distinct(
        self,
    ) -> None:
        self.assertEqual(
            {"operation": "unchanged", "value": None},
            run_control.OverrideOperation.unchanged().to_payload(),
        )
        self.assertEqual(
            {"operation": "set", "value": "gpt-5.4"},
            run_control.OverrideOperation.set("gpt-5.4").to_payload(),
        )
        self.assertEqual(
            {"operation": "clear", "value": None},
            run_control.OverrideOperation.clear().to_payload(),
        )
        with self.assertRaises(ValueError):
            run_control.OverrideOperation("set")
        with self.assertRaises(ValueError):
            run_control.OverrideOperation("clear", "gpt-5.4")

    def test_run_control_help_and_toggle_lines_include_state(self) -> None:
        state = run_control.RunControlState()
        stderr = TTYText()
        actions: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            stderr=stderr,
            on_action=actions.append,
            timestamp=lambda: "2026-06-11 12:00:00",
        )

        listener.handle_key("h")
        listener.handle_key("p")
        listener.handle_key("h")

        lines = stderr.getvalue().splitlines()
        self.assertEqual(
            "[2026-06-11 12:00:00] controls: p toggle stop-after-current "
            "(currently off); s show status; t toggle live tail (currently off); "
            "1 codex, 2 claude, 3 copilot select next harness "
            "(currently unchanged); h show help",
            lines[0],
        )
        self.assertEqual(
            "[2026-06-11 12:00:00] control: stop-after-current requested; "
            "press p to cancel",
            lines[1],
        )
        self.assertEqual(
            "[2026-06-11 12:00:00] controls: p toggle stop-after-current "
            "(currently on); s show status; t toggle live tail (currently off); "
            "1 codex, 2 claude, 3 copilot select next harness "
            "(currently unchanged); h show help",
            lines[2],
        )
        self.assertEqual(["request"], [action.action for action in actions])

    def test_run_control_listener_handles_status_tail_and_harness_keys(self) -> None:
        state = run_control.RunControlState(current_harness="codex")
        stderr = TTYText()
        actions: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            stderr=stderr,
            on_action=actions.append,
            timestamp=lambda: "2026-06-11 12:00:00",
        )

        status = listener.handle_key("s")
        tail_enabled = listener.handle_key("t")
        same_harness = listener.handle_key("1")
        next_harness = listener.handle_key("2")
        repeated_harness = listener.handle_key("2")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual("status", status.action)
        self.assertIsNotNone(tail_enabled)
        assert tail_enabled is not None
        self.assertEqual("tail-enabled", tail_enabled.action)
        self.assertIsNone(same_harness)
        self.assertIsNotNone(next_harness)
        assert next_harness is not None
        self.assertEqual("harness-selected", next_harness.action)
        self.assertEqual("claude", next_harness.next_harness)
        self.assertIsNone(repeated_harness)
        self.assertEqual(
            ["tail-enabled", "harness-selected"],
            [action.action for action in actions],
        )

        self.assertEqual(
            [
                "[2026-06-11 12:00:00] control status: "
                "stop-after-current off; live tail off; next harness unchanged",
                "[2026-06-11 12:00:00] control: live tail enabled; "
                "press t to disable",
                "[2026-06-11 12:00:00] control: next harness already codex",
                "[2026-06-11 12:00:00] control: next harness selected: claude",
                "[2026-06-11 12:00:00] control: next harness already claude",
            ],
            stderr.getvalue().splitlines(),
        )

    def test_run_control_status_callback_does_not_record_action(self) -> None:
        state = run_control.RunControlState()
        stderr = TTYText()
        actions: list[run_control.RunControlAction] = []
        statuses: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            stderr=stderr,
            on_action=actions.append,
            on_status=statuses.append,
            timestamp=lambda: "2026-06-11 12:00:00",
        )

        status = listener.handle_key("s")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual("status", status.action)
        self.assertEqual([status], statuses)
        self.assertEqual([], actions)
        self.assertEqual("", stderr.getvalue())

    def test_run_control_event_contract_includes_runtime_actions(self) -> None:
        self.assertIn("harness-unavailable", events.PAUSE_REASONS)
        self.assertIn("tail-enabled", events.RUN_CONTROL_ACTIONS)
        self.assertIn("tail-disabled", events.RUN_CONTROL_ACTIONS)
        self.assertIn("harness-selected", events.RUN_CONTROL_ACTIONS)
        self.assertIn(
            "implementation-model-updated",
            events.RUN_CONTROL_ACTIONS,
        )
        self.assertEqual("control", config.MODEL_SOURCE_CONTROL)
        self.assertEqual("control", config.EFFORT_SOURCE_CONTROL)

    def test_run_controls_require_text_tty_run(self) -> None:
        args = self.run_args()
        self.assertTrue(
            run_control.should_enable_run_controls(
                args,
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                self.run_args(json_output=True),
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                self.run_args(dry_run=True),
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                args,
                stdin=TTYText(tty=False),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                args,
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=False),
            )
        )

    def test_disabled_run_control_listener_never_touches_terminal(self) -> None:
        listener = run_control.RunControlListener.for_run(
            self.run_args(json_output=True),
            stdin=TTYText(tty=True),
            stderr=TTYText(tty=True),
        )

        with mock.patch.object(run_control.termios, "tcgetattr") as tcgetattr:
            self.assertFalse(listener.start())

        tcgetattr.assert_not_called()

    def test_run_control_start_stop_restores_terminal(self) -> None:
        original_settings = ["original"]
        thread = mock.Mock()
        thread.is_alive.return_value = False

        with (
            mock.patch.object(
                run_control.termios,
                "tcgetattr",
                return_value=original_settings,
            ) as tcgetattr,
            mock.patch.object(run_control.tty, "setcbreak") as setcbreak,
            mock.patch.object(run_control.termios, "tcsetattr") as tcsetattr,
            mock.patch.object(run_control.threading, "Thread", return_value=thread),
        ):
            listener = run_control.RunControlListener(
                enabled=True,
                stdin=TTYText(tty=True, fd=42),
                stderr=TTYText(tty=True),
            )
            self.assertTrue(listener.start())
            listener.stop()

        tcgetattr.assert_called_once_with(42)
        setcbreak.assert_called_once_with(42)
        thread.start.assert_called_once_with()
        tcsetattr.assert_called_once_with(
            42,
            run_control.termios.TCSADRAIN,
            original_settings,
        )

    def test_run_control_start_failure_restores_terminal(self) -> None:
        original_settings = ["original"]
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("thread failed")

        with (
            mock.patch.object(
                run_control.termios,
                "tcgetattr",
                return_value=original_settings,
            ),
            mock.patch.object(run_control.tty, "setcbreak"),
            mock.patch.object(run_control.termios, "tcsetattr") as tcsetattr,
            mock.patch.object(run_control.threading, "Thread", return_value=thread),
        ):
            listener = run_control.RunControlListener(
                enabled=True,
                stdin=TTYText(tty=True, fd=43),
                stderr=TTYText(tty=True),
            )
            self.assertFalse(listener.start())
            self.assertFalse(listener.running)

        tcsetattr.assert_called_once_with(
            43,
            run_control.termios.TCSADRAIN,
            original_settings,
        )


class AgentMuxNotificationIntegrationTests(unittest.TestCase):
    """Notifier-specific coverage for AgentMux hook delivery.

    The suite-wide default (``tests/support.py``) forces
    ``ASL_AGENT_MUX_NOTIFY=off`` so no other test can reach a live daemon;
    every test here that expects delivery re-enables it explicitly and
    substitutes the executable lookup / subprocess layer with fakes.
    """

    def setUp(self) -> None:
        self.enterContext(agent_mux_notify_enabled())
        agent_mux_delivery.reset_legacy_cache()
        self.addCleanup(agent_mux_delivery.reset_legacy_cache)

    @staticmethod
    def accepted_delivery(
        trace_id: str = "trace-test",
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            ["/opt/amx"],
            0,
            stdout=json.dumps(
                {
                    "schema_version": 1,
                    "status": "accepted",
                    "retryable": False,
                    "reason": "accepted",
                    "trace_id": trace_id,
                }
            ),
            stderr="",
        )

    def finish_payload(self, tmp_dir: Path) -> dict[str, object]:
        return {
            "run_id": "run-123",
            "plan_dir": str(tmp_dir / "plan"),
            "cwd": str(tmp_dir / "repo"),
            "harness": "codex",
            "reason": "all-complete",
            "run_started_at": "2026-06-11T12:00:00Z",
            "run_finished_at": "2026-06-11T12:00:03Z",
            "state_path": str(tmp_dir / "state.json"),
            "kitty_window_id": 42,
        }

    def test_kitty_window_id_from_env_accepts_positive_int(self) -> None:
        with patched_env(KITTY_WINDOW_ID="42"):
            self.assertEqual(42, runner._kitty_window_id_from_env())

    def test_kitty_window_id_from_env_ignores_missing_or_invalid_values(self) -> None:
        for raw in (None, "", "0", "-7", "not-an-int"):
            with self.subTest(raw=raw), patched_env(KITTY_WINDOW_ID=raw):
                self.assertIsNone(runner._kitty_window_id_from_env())

    def test_agent_mux_notify_timeout_defaults_without_env_override(self) -> None:
        with patched_env(ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS=None):
            self.assertEqual(
                runner.AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
                runner._agent_mux_notify_timeout(),
            )

    def test_agent_mux_notify_timeout_honors_env_override(self) -> None:
        for raw, expected in (("30", 30.0), (" 2.5 ", 2.5), ("0.1", 0.1)):
            with self.subTest(raw=raw), patched_env(
                ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS=raw
            ):
                self.assertEqual(expected, runner._agent_mux_notify_timeout())

    def test_agent_mux_notify_timeout_fails_safe_on_invalid_override(self) -> None:
        for raw in ("", "0", "-1", "not-a-float", "inf", "nan"):
            with self.subTest(raw=raw), patched_env(
                ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS=raw
            ):
                self.assertEqual(
                    runner.AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
                    runner._agent_mux_notify_timeout(),
                )

    def test_notification_subprocess_uses_env_timeout_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            completed = self.accepted_delivery()

            with (
                patched_env(ASL_AGENT_MUX_NOTIFY_TIMEOUT_SECONDS="30"),
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                runner._notify_agent_mux_loop_finished(payload)

            run.assert_called_once()
            self.assertEqual(30.0, run.call_args.kwargs["timeout"])

    def test_loop_payload_uses_active_turn_harness_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            active_turn = {
                "harness": "claude",
                "turn_index": 2,
                "session": "2.1",
                "session_id": "2.1",
                "total_sessions": 4,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
            }

            hook_payload = runner._agent_mux_loop_payload(
                payload,
                reason=runner.AGENT_MUX_TURN_START_REASON,
                active_turn=active_turn,
            )

            self.assertEqual("agent_session_loop", hook_payload["source"])
            self.assertEqual("turn_started", hook_payload["reason"])
            self.assertEqual("claude", hook_payload["harness"])
            self.assertEqual(payload["state_path"], hook_payload["state_path"])
            self.assertEqual(42, hook_payload["kitty_window_id"])
            self.assertEqual(2, hook_payload["turn_index"])
            self.assertEqual("2.1", hook_payload["session"])
            self.assertEqual("2.1", hook_payload["session_id"])
            self.assertEqual(4, hook_payload["total_sessions"])

    def test_start_and_turn_notifications_build_agent_mux_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            active_turn = {
                "harness": "claude",
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
            }
            completed = self.accepted_delivery()

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                runner._notify_agent_mux_loop_started(payload)
                runner._notify_agent_mux_loop_turn_started(payload, active_turn)

            self.assertEqual(2, run.call_count)
            start_args, start_kwargs = run.call_args_list[0]
            turn_args, turn_kwargs = run.call_args_list[1]
            self.assertEqual("AgentSessionLoopStarted", start_args[0][-1])
            self.assertEqual("AgentSessionLoopTurnStarted", turn_args[0][-1])
            for command in (start_args[0], turn_args[0]):
                self.assertIn("--harness", command)
                self.assertIn("--request-id", command)
                self.assertIn("--json", command)
                self.assertIn("--strict", command)
            self.assertEqual(
                runner.AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
                start_kwargs["timeout"],
            )
            self.assertEqual(
                runner.AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
                turn_kwargs["timeout"],
            )
            start_payload = json.loads(start_kwargs["input"])
            turn_payload = json.loads(turn_kwargs["input"])
            self.assertEqual("run_started", start_payload["reason"])
            self.assertEqual("codex", start_payload["harness"])
            self.assertEqual(payload["state_path"], start_payload["state_path"])
            self.assertEqual(42, start_payload["kitty_window_id"])
            self.assertEqual("turn_started", turn_payload["reason"])
            self.assertEqual("claude", turn_payload["harness"])
            self.assertEqual(42, turn_payload["kitty_window_id"])
            self.assertEqual(1, turn_payload["turn_index"])
            self.assertEqual(1, turn_payload["session"])

    def test_finish_notification_builds_agent_mux_command_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            completed = self.accepted_delivery()

            with (
                mock.patch.object(
                    runner.shutil,
                    "which",
                    return_value="/opt/amx",
                ) as which,
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                runner._notify_agent_mux_loop_finished(payload)

            which.assert_called_once_with("amx")
            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual("AgentSessionLoopFinished", args[0][-1])
            self.assertEqual("codex", args[0][args[0].index("--harness") + 1])
            request_id = args[0][args[0].index("--request-id") + 1]
            self.assertEqual(
                agent_mux_delivery.stable_request_id("run-123:1"), request_id
            )
            self.assertIn("--json", args[0])
            self.assertIn("--strict", args[0])
            self.assertTrue(kwargs["text"])
            self.assertEqual(subprocess.PIPE, kwargs["stdout"])
            self.assertEqual(subprocess.PIPE, kwargs["stderr"])
            self.assertEqual(
                runner.AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS,
                kwargs["timeout"],
            )
            self.assertFalse(kwargs["check"])
            hook_payload = json.loads(kwargs["input"])
            emitted_time = hook_payload.pop("emitted_time")
            self.assertTrue(emitted_time.isdigit())
            self.assertEqual(
                {
                    "source": "agent_session_loop",
                    "reason": "run_finished",
                    "run_id": "run-123",
                    "plan_dir": payload["plan_dir"],
                    "cwd": payload["cwd"],
                    "state_path": payload["state_path"],
                    "kitty_window_id": 42,
                    "harness": "codex",
                    "phase": "finished",
                    "run_started_at": "2026-06-11T12:00:00Z",
                    "run_finished_at": "2026-06-11T12:00:03Z",
                    "source_sequence": "1",
                    "source_event_id": "run-123:1",
                },
                hook_payload,
            )

    def test_finish_notification_skips_when_amx_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(runner.shutil, "which", return_value=None),
                mock.patch.object(runner.subprocess, "run") as run,
            ):
                runner._notify_agent_mux_loop_finished(self.finish_payload(Path(tmp)))

            run.assert_not_called()

    def test_finish_notification_swallows_subprocess_failures(self) -> None:
        failures: tuple[Exception, ...] = (
            subprocess.TimeoutExpired(["/opt/amx"], 1),
            OSError("amx failed"),
            subprocess.SubprocessError("amx failed"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    with (
                        mock.patch.object(
                            runner.shutil,
                            "which",
                            return_value="/opt/amx",
                        ),
                        mock.patch.object(
                            runner.subprocess,
                            "run",
                            side_effect=failure,
                        ),
                        mock.patch.object(
                            agent_mux_delivery, "retry_sleep", return_value=None
                        ),
                    ):
                        runner._notify_agent_mux_loop_finished(payload)

    def test_finish_run_notifies_after_setting_finished_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None

            with mock.patch.object(
                runner,
                "_notify_agent_mux_loop_finished",
            ) as notify:
                runner._finish_run(event_path, payload)

            notify.assert_called_once_with(payload, mock.ANY)
            self.assertIsNotNone(payload["run_finished_at"])
            self.assertIsNone(payload["active_turn"])
            self.assertTrue(Path(payload["state_path"]).exists())
            records = read_jsonl(event_path)
            self.assertEqual("run_finished", records[0]["name"])
            self.assertEqual(
                payload["run_finished_at"],
                records[0]["payload"]["run_finished_at"],
            )

    def test_notify_env_off_disables_delivery_before_executable_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            with (
                patched_env(ASL_AGENT_MUX_NOTIFY="off"),
                mock.patch.object(runner.shutil, "which") as which,
                mock.patch.object(runner.subprocess, "run") as run,
            ):
                runner._notify_agent_mux_loop_finished(payload)

            which.assert_not_called()
            run.assert_not_called()

    def test_notify_env_unknown_value_behaves_as_auto(self) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            for raw in ("banana", "ON", " Auto ", ""):
                with self.subTest(raw=raw):
                    with (
                        patched_env(ASL_AGENT_MUX_NOTIFY=raw),
                        mock.patch.object(
                            runner.shutil, "which", return_value="/opt/amx"
                        ),
                        mock.patch.object(
                            runner.subprocess, "run", return_value=completed
                        ) as run,
                    ):
                        runner._notify_agent_mux_loop_finished(payload)
                    run.assert_called_once()

    def test_lifecycle_notifications_carry_phase_sequence_and_identity(
        self,
    ) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            payload["agent_mux_sequence"] = 0
            payload["pause_reason"] = "verify-failed"
            payload["pause_message"] = "check exploded"
            persistence = runner._RunPersistence()
            active_turn = {
                "harness": "claude",
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
                "heartbeat_count": 3,
                "last_activity_at": "2026-06-11T12:01:30Z",
            }

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess, "run", return_value=completed
                ) as run,
            ):
                runner._notify_agent_mux_loop_started(payload, persistence)
                runner._notify_agent_mux_loop_turn_started(
                    payload, active_turn, persistence
                )
                runner._notify_agent_mux_loop_turn_heartbeat(
                    payload, active_turn, persistence
                )
                runner._notify_agent_mux_loop_turn_finished(
                    payload, active_turn, persistence
                )
                runner._notify_agent_mux_loop_paused(payload, persistence)
                runner._notify_agent_mux_loop_finished(payload, persistence)

            events_seen = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(
                [
                    "AgentSessionLoopStarted",
                    "AgentSessionLoopTurnStarted",
                    "AgentSessionLoopTurnHeartbeat",
                    "AgentSessionLoopTurnFinished",
                    "AgentSessionLoopPaused",
                    "AgentSessionLoopFinished",
                ],
                events_seen,
            )
            hook_payloads = [
                json.loads(call.kwargs["input"]) for call in run.call_args_list
            ]
            self.assertEqual(
                ["between", "turn", "turn", "between", "paused", "finished"],
                [hook_payload["phase"] for hook_payload in hook_payloads],
            )
            # The persisted per-run counter is monotonic across every event
            # and both ordering fields are string-encoded for the daemon.
            self.assertEqual(
                [str(index) for index in range(1, 7)],
                [hook_payload["source_sequence"] for hook_payload in hook_payloads],
            )
            self.assertEqual(
                [f"run-123:{index}" for index in range(1, 7)],
                [hook_payload["source_event_id"] for hook_payload in hook_payloads],
            )
            for hook_payload in hook_payloads:
                self.assertEqual("run-123", hook_payload["run_id"])
                self.assertTrue(hook_payload["emitted_time"].isdigit())
            # Turn-scoped events carry the turn identity, heartbeat count,
            # and activity recency the daemon consumes.
            heartbeat_payload = hook_payloads[2]
            self.assertEqual(1, heartbeat_payload["turn_index"])
            self.assertEqual("1", heartbeat_payload["session_id"])
            self.assertEqual(3, heartbeat_payload["heartbeat_count"])
            self.assertEqual(
                "2026-06-11T12:01:30Z", heartbeat_payload["last_output_at"]
            )
            # Paused carries the attention-mapping reason for the daemon.
            paused_payload = hook_payloads[4]
            self.assertEqual("verify-failed", paused_payload["pause_reason"])
            self.assertEqual("check exploded", paused_payload["pause_message"])
            # The allocated counter is durable in state.json.
            state_payload = json.loads(
                Path(str(payload["state_path"])).read_text(encoding="utf-8")
            )
            self.assertEqual(6, state_payload["agent_mux_sequence"])
            self.assertEqual(6, payload["agent_mux_sequence"])

    def test_retryable_delivery_reuses_request_id_and_records_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            payload["agent_mux_sequence"] = 0
            persistence = runner._RunPersistence()
            retryable = subprocess.CompletedProcess(
                ["/opt/amx"],
                75,
                stdout=json.dumps(
                    {
                        "status": "retryable_failure",
                        "retryable": True,
                        "reason": "daemon_unavailable",
                        "trace_id": "trace-attempt-1",
                    }
                ),
                stderr="",
            )
            accepted = self.accepted_delivery("trace-attempt-3")

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    side_effect=[
                        retryable,
                        subprocess.TimeoutExpired(["/opt/amx"], 1),
                        accepted,
                    ],
                ) as run,
                mock.patch.object(
                    agent_mux_delivery, "retry_sleep", return_value=None
                ) as retry_sleep,
            ):
                runner._notify_agent_mux_loop_finished(payload, persistence)

            self.assertEqual(3, run.call_count)
            retry_sleep.assert_has_calls([mock.call(0.1), mock.call(0.3)])
            commands = [call.args[0] for call in run.call_args_list]
            request_ids = [
                command[command.index("--request-id") + 1]
                for command in commands
            ]
            self.assertEqual(1, len(set(request_ids)))
            self.assertEqual(
                agent_mux_delivery.stable_request_id("run-123:1"),
                request_ids[0],
            )
            hook_payloads = [
                json.loads(call.kwargs["input"]) for call in run.call_args_list
            ]
            self.assertEqual(
                {"run-123:1"},
                {hook_payload["source_event_id"] for hook_payload in hook_payloads},
            )
            records = read_jsonl(Path(str(payload["events_path"])))
            self.assertEqual(1, len(records))
            outcome = records[0]["payload"]
            self.assertEqual("accepted", outcome["final_status"])
            self.assertEqual("trace-attempt-3", outcome["trace_id"])
            self.assertEqual(3, outcome["attempt_count"])
            self.assertEqual(
                [
                    "retryable_failure",
                    "retryable_failure",
                    "accepted",
                ],
                [attempt["status"] for attempt in outcome["attempts"]],
            )

    def test_duplicate_acknowledgement_is_success_without_retry(self) -> None:
        duplicate = subprocess.CompletedProcess(
            ["/opt/amx"],
            0,
            stdout=json.dumps(
                {
                    "status": "duplicate",
                    "retryable": False,
                    "reason": "duplicate",
                    "trace_id": "trace-duplicate",
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess, "run", return_value=duplicate
                ) as run,
            ):
                runner._notify_agent_mux_loop_finished(
                    payload, runner._RunPersistence()
                )
            run.assert_called_once()
            outcome = read_jsonl(Path(str(payload["events_path"])))[0]["payload"]
            self.assertTrue(outcome["acknowledged"])
            self.assertEqual("duplicate", outcome["final_status"])

    def test_permanent_delivery_failure_is_sanitized_and_not_retried(self) -> None:
        permanent = subprocess.CompletedProcess(
            ["/opt/amx"],
            2,
            stdout=json.dumps(
                {
                    "status": "permanent_failure",
                    "retryable": False,
                    "reason": "secret /Users/example/private",
                    "trace_id": "trace-permanent",
                }
            ),
            stderr="sensitive diagnostic detail",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess, "run", return_value=permanent
                ) as run,
            ):
                runner._notify_agent_mux_loop_finished(
                    payload, runner._RunPersistence()
                )
            run.assert_called_once()
            event_text = Path(str(payload["events_path"])).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("/Users/example", event_text)
            self.assertNotIn("sensitive diagnostic", event_text)
            outcome = json.loads(event_text)["payload"]
            self.assertEqual("permanent_failure", outcome["final_status"])
            self.assertEqual("unspecified", outcome["reason"])
            self.assertEqual("permanent_outcome", outcome["error_classification"])

    def test_legacy_binary_falls_back_once_and_caches_capability(self) -> None:
        unsupported = subprocess.CompletedProcess(
            ["/opt/amx"],
            1,
            stdout="",
            stderr="unknown flag: --request-id",
        )
        legacy = subprocess.CompletedProcess(
            ["/opt/amx"], 0, stdout="", stderr=""
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            persistence = runner._RunPersistence()
            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    side_effect=[unsupported, legacy, legacy],
                ) as run,
            ):
                runner._notify_agent_mux_loop_started(payload, persistence)
                runner._notify_agent_mux_loop_finished(payload, persistence)

            self.assertEqual(3, run.call_count)
            self.assertIn("--strict", run.call_args_list[0].args[0])
            self.assertEqual(
                ["/opt/amx", "hook-indicator", runner.AGENT_MUX_START_EVENT],
                run.call_args_list[1].args[0],
            )
            self.assertEqual(
                ["/opt/amx", "hook-indicator", runner.AGENT_MUX_FINISH_EVENT],
                run.call_args_list[2].args[0],
            )
            outcomes = [record["payload"] for record in read_jsonl(
                Path(str(payload["events_path"]))
            )]
            self.assertEqual(2, len(outcomes))
            self.assertEqual(
                ["legacy_unacknowledged", "legacy_unacknowledged"],
                [outcome["final_status"] for outcome in outcomes],
            )
            self.assertEqual([2, 1], [outcome["attempt_count"] for outcome in outcomes])

    def test_exhausted_delivery_is_recorded_without_raising(self) -> None:
        retryable = subprocess.CompletedProcess(
            ["/opt/amx"],
            75,
            stdout=json.dumps(
                {
                    "status": "retryable_failure",
                    "retryable": True,
                    "reason": "daemon_unavailable",
                    "trace_id": "trace-retryable",
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess, "run", return_value=retryable
                ) as run,
                mock.patch.object(
                    agent_mux_delivery, "retry_sleep", return_value=None
                ),
            ):
                runner._notify_agent_mux_loop_finished(
                    payload, runner._RunPersistence()
                )
            self.assertEqual(3, run.call_count)
            outcome = read_jsonl(Path(str(payload["events_path"])))[0]["payload"]
            self.assertEqual("retryable_exhausted", outcome["final_status"])
            self.assertFalse(outcome["acknowledged"])

    def test_pause_run_emits_paused_and_finish_run_skips_finished(self) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            payload["agent_mux_sequence"] = 0
            persistence = runner._RunPersistence()

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess, "run", return_value=completed
                ) as run,
            ):
                runner._pause_run(
                    event_path,
                    payload,
                    reason="question",
                    message="need input",
                    persistence=persistence,
                )
                runner._finish_run(
                    event_path, payload, persistence=persistence
                )

            events_seen = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(["AgentSessionLoopPaused"], events_seen)
            hook_payload = json.loads(run.call_args_list[0].kwargs["input"])
            self.assertEqual("paused", hook_payload["phase"])
            self.assertEqual("question", hook_payload["pause_reason"])
            self.assertEqual("need input", hook_payload["pause_message"])
            # The run still finalized normally on disk.
            self.assertIsNotNone(payload["run_finished_at"])
            self.assertTrue(payload["paused"])

    def test_unpaused_finish_run_emits_finished(self) -> None:
        completed = self.accepted_delivery()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            payload["agent_mux_sequence"] = 0
            persistence = runner._RunPersistence()

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess, "run", return_value=completed
                ) as run,
            ):
                runner._finish_run(
                    event_path, payload, persistence=persistence
                )

            events_seen = [call.args[0][-1] for call in run.call_args_list]
            self.assertEqual(["AgentSessionLoopFinished"], events_seen)
            hook_payload = json.loads(run.call_args_list[0].kwargs["input"])
            self.assertEqual("finished", hook_payload["phase"])
            self.assertEqual("1", hook_payload["source_sequence"])

    def test_cancelled_recheck_skips_emission_without_allocating_sequence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            payload["agent_mux_sequence"] = 0
            persistence = runner._RunPersistence()

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(runner.subprocess, "run") as run,
            ):
                runner._notify_agent_mux_loop(
                    runner.AGENT_MUX_TURN_HEARTBEAT_EVENT,
                    payload,
                    reason=runner.AGENT_MUX_TURN_HEARTBEAT_REASON,
                    persistence=persistence,
                    cancelled=lambda: True,
                )

            run.assert_not_called()
            self.assertEqual(0, payload["agent_mux_sequence"])

    def test_missing_amx_records_outcome_and_preserves_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["events_path"] = str(tmp_dir / "events.jsonl")
            payload["agent_mux_sequence"] = 0
            persistence = runner._RunPersistence()

            with (
                mock.patch.object(runner.shutil, "which", return_value=None),
                mock.patch.object(runner.subprocess, "run") as run,
            ):
                runner._notify_agent_mux_loop_finished(payload, persistence)

            run.assert_not_called()
            self.assertEqual(1, payload["agent_mux_sequence"])
            records = read_jsonl(Path(str(payload["events_path"])))
            self.assertEqual(1, len(records))
            self.assertEqual(events.AGENT_MUX_DELIVERY_EVENT, records[0]["name"])
            outcome = records[0]["payload"]
            self.assertEqual("run-123:1", outcome["source_event_id"])
            self.assertEqual("unavailable", outcome["final_status"])
            self.assertEqual("missing_binary", outcome["error_classification"])


class AgentMuxSuiteIsolationTests(unittest.TestCase):
    """Lock in the suite-wide default: no live AgentMux delivery."""

    def test_suite_environment_forces_notification_off(self) -> None:
        # tests/support.py exports the kill switch at import time; every
        # nested ASL subprocess inherits it, so the full suite produces zero
        # live lifecycle events even with a real amx on PATH.
        self.assertEqual(
            runner.AGENT_MUX_NOTIFY_OFF,
            os.environ.get(runner.AGENT_MUX_NOTIFY_ENV_VAR),
        )
        self.assertFalse(runner._agent_mux_notify_enabled())

    def test_notify_helpers_short_circuit_under_suite_default(self) -> None:
        with (
            mock.patch.object(runner.shutil, "which") as which,
            mock.patch.object(runner.subprocess, "run") as run,
        ):
            runner._notify_agent_mux_loop_started({"run_id": "run-x"})
            runner._notify_agent_mux_loop_finished({"run_id": "run-x"})

        which.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
