"""ControlQueueTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    control_queue,
    control_queue_drain,
    durability,
)
from tests.support import (
    read_jsonl,
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

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
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
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
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
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
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

    def test_abort_round_trip_preserves_grace_and_reason(self) -> None:
        appended = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            state_dir=self.state_dir,
            grace_seconds=30,
            reason="supervisor asked for a hard stop",
            command_id="abort-1",
        )
        self.assertEqual(30, appended.grace_seconds)
        self.assertEqual("supervisor asked for a hard stop", appended.reason)
        self.assertIsNone(appended.harness)

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual((), result.rejected)
        self.assertEqual(("abort-1",), result.consumed_ids)
        command = result.accepted[0]
        self.assertEqual(control_queue.COMMAND_REQUEST_ABORT_TURN, command.command)
        self.assertEqual(30, command.grace_seconds)
        self.assertEqual("supervisor asked for a hard stop", command.reason)
        self.assertEqual(30, command.to_payload()["grace_seconds"])

    def test_abort_without_optional_fields_gets_the_default_grace(self) -> None:
        appended = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            state_dir=self.state_dir,
            command_id="abort-default",
        )
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS, appended.grace_seconds
        )
        self.assertIsNone(appended.reason)

        # The written line is self-describing: the consumer never has to know
        # the producer's default.
        lines = read_jsonl(self.queue_path())
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            lines[0]["grace_seconds"],
        )
        self.assertNotIn("reason", lines[0])

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            result.accepted[0].grace_seconds,
        )
        self.assertIsNone(result.accepted[0].reason)

    def test_drain_preserves_order_with_abort_interleaved(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="claude",
            command_id="harness",
        )
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            state_dir=self.state_dir,
            grace_seconds=5,
            command_id="abort",
        )
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop",
        )

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(
            ["harness", "abort", "stop"],
            [cmd.id for cmd in result.accepted],
        )
        self.assertEqual(
            [None, 5, None],
            [cmd.grace_seconds for cmd in result.accepted],
        )

    def test_append_rejects_malformed_abort_grace_and_reason(self) -> None:
        cases = (
            ({"grace_seconds": 0}, control_queue.REJECT_INVALID_GRACE),
            ({"grace_seconds": -1}, control_queue.REJECT_INVALID_GRACE),
            (
                {"grace_seconds": (control_queue.MAX_ABORT_GRACE_SECONDS + 1)},
                control_queue.REJECT_INVALID_GRACE,
            ),
            # bool is an int subclass; `True` must not read as 1 second.
            ({"grace_seconds": True}, control_queue.REJECT_INVALID_GRACE),
            ({"reason": ""}, control_queue.REJECT_INVALID_REASON),
        )
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(control_queue.ControlQueueError) as raised:
                    control_queue.append_command(
                        self.plan_dir,
                        control_queue.COMMAND_REQUEST_ABORT_TURN,
                        state_dir=self.state_dir,
                        **kwargs,
                    )
                self.assertEqual(expected, str(raised.exception))

        # Nothing malformed reached the queue file.
        self.assertFalse(self.queue_path().exists())

    def test_drain_rejects_malformed_abort_grace_and_reason(self) -> None:
        queue_path = self.queue_path()
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        records = (
            {"id": "zero", "command": "request-abort-turn", "grace_seconds": 0},
            {
                "id": "negative",
                "command": "request-abort-turn",
                "grace_seconds": -30,
            },
            {
                "id": "absurd",
                "command": "request-abort-turn",
                "grace_seconds": control_queue.MAX_ABORT_GRACE_SECONDS + 1,
            },
            {
                "id": "not-a-number",
                "command": "request-abort-turn",
                "grace_seconds": "120",
            },
            {
                "id": "boolean",
                "command": "request-abort-turn",
                "grace_seconds": True,
            },
            {
                "id": "empty-reason",
                "command": "request-abort-turn",
                "reason": "",
            },
            {
                "id": "ok",
                "command": "request-abort-turn",
                "grace_seconds": 45,
                "reason": "wind down",
            },
        )
        queue_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

        self.assertEqual(("ok",), result.consumed_ids)
        self.assertEqual(
            [
                control_queue.REJECT_INVALID_GRACE,
                control_queue.REJECT_INVALID_GRACE,
                control_queue.REJECT_INVALID_GRACE,
                control_queue.REJECT_INVALID_GRACE,
                control_queue.REJECT_INVALID_GRACE,
                control_queue.REJECT_INVALID_REASON,
            ],
            [command.reason for command in result.rejected],
        )
        self.assertEqual(45, result.accepted[0].grace_seconds)
        self.assertEqual("wind down", result.accepted[0].reason)

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

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

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

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)

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
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaises(control_queue.ControlQueueError),
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
            'not-json\n{"id":"ok","command":"request-stop-after-current"}\n',
            encoding="utf-8",
        )
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
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
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
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
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), result.accepted)
        self.assertEqual(
            [
                control_queue.REJECT_INVALID_HARNESS,
                control_queue.REJECT_MISSING_HARNESS,
            ],
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
        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
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

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(sorted(ids), sorted(cmd.id for cmd in result.accepted))
        self.assertEqual((), result.rejected)

    def test_requeue_puts_lines_ahead_of_newer_appends(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="unreached",
        )
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="newer",
        )

        control_queue.requeue_lines(
            self.queue_path(), [command.raw for command in drained.accepted]
        )

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual(("unreached", "newer"), result.consumed_ids)

    def test_a_failed_requeue_restores_the_commands_appended_since(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="newer",
        )
        before = self.queue_path().read_bytes()
        real_write = os.write
        calls = 0

        def fail_first(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(fd, data)

        with (
            mock.patch.object(control_queue_drain.os, "write", side_effect=fail_first),
            self.assertRaises(OSError) as raised,
        ):
            control_queue.requeue_lines(self.queue_path(), ['{"id": "unreached"}'])

        # The rewrite failed after the truncate; what producers had appended
        # must still be queued rather than lost with the requeued lines.
        self.assertEqual(errno.ENOSPC, raised.exception.errno)
        self.assertEqual(before, self.queue_path().read_bytes())

    def test_queue_writes_use_the_full_device_barrier(self) -> None:
        with mock.patch.object(durability, "fullsync") as fullsync:
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=self.state_dir,
                command_id="first",
            )
            control_queue.requeue_lines(self.queue_path(), ['{"id": "second"}'])
        self.assertEqual(2, fullsync.call_count)


if __name__ == "__main__":
    unittest.main()
