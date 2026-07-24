"""Control queue, run control, and finish notification tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import dataclasses
from datetime import datetime, timezone
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
            control_queue.COMMAND_SELECT_FUTURE_HARNESS,
            state_dir=self.state_dir,
            harness="codex",
            command_id="c",
        )
        result = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertEqual(
            ["a", "b", "c"],
            [cmd.id for cmd in result.accepted],
        )
        self.assertEqual(
            [None, "claude", "codex"],
            [cmd.harness for cmd in result.accepted],
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
            "control": {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "consumed": False,
                "last_action": None,
            },
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
