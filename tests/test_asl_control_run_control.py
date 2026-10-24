"""RunControlTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import argparse
import os
import threading
import unittest
from unittest import mock

from agent_session_loop import (
    config,
    events,
    run_control,
    run_selection,
)
from tests.support import (
    TTYText,
)


class RunControlTests(unittest.TestCase):
    def run_args(self, *, json_output: bool = False, dry_run: bool = False):
        return mock.Mock(json=json_output, dry_run=dry_run)

    def test_run_control_state_toggles_and_consumes_stop_after_current(self) -> None:
        state = run_control.RunControlState()

        self.assertFalse(state.stop_after_current)
        self.assertEqual(
            {
                "stop_after_current": False,
                "abort_turn_requested": False,
                "abort_grace_seconds": None,
                "abort_reason": None,
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

    def test_run_control_state_abort_request_is_idempotent(self) -> None:
        state = run_control.RunControlState()

        self.assertFalse(state.abort_turn_requested)
        self.assertIsNone(state.consume_abort_turn())

        requested = state.request_abort_turn(grace_seconds=45, reason="supervisor stop")
        self.assertIsNotNone(requested)
        assert requested is not None
        self.assertEqual("abort-requested", requested.action)
        self.assertTrue(requested.abort_requested)
        self.assertEqual(45, requested.abort_grace_seconds)
        self.assertEqual("supervisor stop", requested.abort_reason)
        self.assertEqual(45, state.abort_grace_seconds)
        self.assertEqual("supervisor stop", state.abort_reason)

        # A repeat cannot restart the window or emit a duplicate request.
        self.assertIsNone(state.request_abort_turn(grace_seconds=1, reason="again"))
        self.assertEqual(45, state.abort_grace_seconds)
        self.assertEqual("supervisor stop", state.abort_reason)

        self.assertEqual(
            {
                "abort_turn_requested": True,
                "abort_grace_seconds": 45,
                "abort_reason": "supervisor stop",
            },
            {
                key: value
                for key, value in state.snapshot().items()
                if key.startswith("abort")
            },
        )

    def test_run_control_state_abort_is_consumed_exactly_once(self) -> None:
        state = run_control.RunControlState()
        state.request_abort_turn(grace_seconds=10, reason="hurry")

        consumed = state.consume_abort_turn()
        self.assertIsNotNone(consumed)
        assert consumed is not None
        self.assertEqual("abort-consumed", consumed.action)
        # The consumer still learns what it has to act on.
        self.assertEqual(10, consumed.abort_grace_seconds)
        self.assertEqual("hurry", consumed.abort_reason)

        self.assertIsNone(state.consume_abort_turn())
        self.assertFalse(state.abort_turn_requested)
        self.assertIsNone(state.abort_grace_seconds)
        self.assertIsNone(state.abort_reason)

        # Stop-after-current is independent state.
        self.assertFalse(state.stop_after_current)

    def test_run_control_listener_passes_abort_consumption_through(self) -> None:
        state = run_control.RunControlState()
        state.request_abort_turn(grace_seconds=7, reason="stop now")
        actions: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            on_action=actions.append,
        )

        consumed = listener.consume_abort_turn()

        self.assertIsNotNone(consumed)
        assert consumed is not None
        self.assertEqual("abort-consumed", consumed.action)
        self.assertEqual(7, consumed.abort_grace_seconds)
        self.assertEqual([consumed], actions)

        self.assertIsNone(listener.consume_abort_turn())
        self.assertEqual([consumed], actions)

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
                model=run_control.OverrideOperation.set("claude-sonnet-4-5"),
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
            run_selection._requested_model(
                args,
                harness="codex",
                implementation_overrides=overrides,
            ),
        )
        self.assertEqual(
            "xhigh",
            run_selection._requested_effort(
                args,
                harness="codex",
                implementation_overrides=overrides,
            ),
        )
        selected = run_selection._selected_model_effort_payload(
            args,
            harness="codex",
            implementation_overrides=overrides,
        )
        self.assertEqual("gpt-5-control", selected["model"])
        self.assertEqual(config.MODEL_SOURCE_CONTROL, selected["model_source"])
        self.assertEqual("xhigh", selected["effort"])
        self.assertEqual(config.EFFORT_SOURCE_CONTROL, selected["effort_source"])

        cleared = run_selection._selected_model_effort_payload(
            args,
            harness="codex",
            implementation_overrides={},
        )
        self.assertEqual("gpt-5-flag", cleared["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, cleared["model_source"])
        self.assertEqual("medium", cleared["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, cleared["effort_source"])

        review = run_selection._selected_model_effort_payload(
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
                "[2026-06-11 12:00:00] control: live tail enabled; press t to disable",
                "[2026-06-11 12:00:00] control: next harness already codex",
                "[2026-06-11 12:00:00] control: next harness selected: claude",
                "[2026-06-11 12:00:00] control: next harness already claude",
            ],
            stderr.getvalue().splitlines(),
        )

    def test_listener_handles_every_key_in_one_read_and_stops_at_eof(self) -> None:
        # A paste (or two quick presses) arrives as one read; every key in it
        # must be handled now rather than once more input happens to arrive.
        # Digits inside an escape sequence must not select a harness, and end of
        # input ends the listener instead of spinning on a readable descriptor.
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        listener = run_control.RunControlListener(
            enabled=False,
            state=run_control.RunControlState(current_harness="codex"),
            stderr=TTYText(),
            poll_interval=0.01,
            timestamp=lambda: "2026-06-11 12:00:00",
        )
        handled: list[str] = []
        listener._fd = read_fd
        with mock.patch.object(listener, "handle_key", side_effect=handled.append):
            os.write(write_fd, b"pt\x1b[1;5A")
            os.close(write_fd)
            thread = threading.Thread(target=listener._listen, daemon=True)
            thread.start()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "the listener kept running after EOF")
        self.assertEqual(["p", "t"], handled)

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
        self.assertIn("abort-turn-requested", events.RUN_CONTROL_ACTIONS)
        self.assertIn("abort-turn-honored", events.RUN_CONTROL_ACTIONS)
        self.assertIn(
            "abort-turn-satisfied-by-worker",
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


if __name__ == "__main__":
    unittest.main()
