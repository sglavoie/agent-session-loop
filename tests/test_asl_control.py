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
    patched_env,
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


class FinishNotificationTests(unittest.TestCase):
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
            completed = subprocess.CompletedProcess(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopFinished"],
                0,
                stdout="",
                stderr="",
            )

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
            completed = subprocess.CompletedProcess(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopStarted"],
                0,
                stdout="",
                stderr="",
            )

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
            self.assertEqual(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopStarted"],
                start_args[0],
            )
            self.assertEqual(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopTurnStarted"],
                turn_args[0],
            )
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
            completed = subprocess.CompletedProcess(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopFinished"],
                0,
                stdout="",
                stderr="",
            )

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
            self.assertEqual(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopFinished"],
                args[0],
            )
            self.assertTrue(kwargs["text"])
            self.assertEqual(subprocess.PIPE, kwargs["stdout"])
            self.assertEqual(subprocess.PIPE, kwargs["stderr"])
            self.assertEqual(
                runner.AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS,
                kwargs["timeout"],
            )
            self.assertFalse(kwargs["check"])
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
                    "run_started_at": "2026-06-11T12:00:00Z",
                    "run_finished_at": "2026-06-11T12:00:03Z",
                },
                json.loads(kwargs["input"]),
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

            notify.assert_called_once_with(payload)
            self.assertIsNotNone(payload["run_finished_at"])
            self.assertIsNone(payload["active_turn"])
            self.assertTrue(Path(payload["state_path"]).exists())
            records = read_jsonl(event_path)
            self.assertEqual("run_finished", records[0]["name"])
            self.assertEqual(
                payload["run_finished_at"],
                records[0]["payload"]["run_finished_at"],
            )


if __name__ == "__main__":
    unittest.main()
