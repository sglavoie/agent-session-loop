"""ControlQueueDrainInRunnerTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    mailbox,
    mailbox_appenders,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    run_persistence,
)
from tests.support import (
    read_jsonl,
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
            "control": run_coordination_control.initial_control_payload(),
            "harness": "codex",
        }
        self.control_payload = dict(self.payload["control"])  # type: ignore[arg-type]
        self.event_path = self.state_dir / "events.jsonl"
        self.event_path.parent.mkdir(parents=True, exist_ok=True)

    def drain(
        self,
        control_state: run_control.RunControlState,
        seen_ids: set[str] | None = None,
        wakeup: threading.Event | None = None,
        abort: harnesses.AbortSignal | None = None,
        mailbox_enabled: bool = False,
        finalization: bool = False,
    ) -> tuple[control_queue.DrainResult, set[str]]:
        seen = set() if seen_ids is None else seen_ids
        result = run_coordination_control_drain.drain_external_control_queue(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=control_state,
            seen_ids=seen,
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            context=None,
            persistence=run_persistence._RunPersistence(),
            run_control_lock=threading.Lock(),
            wakeup=wakeup,
            abort=abort,
            mailbox_enabled=mailbox_enabled,
            finalization=finalization,
        )
        return result, seen

    def run_control_actions(self) -> list[str]:
        if not self.event_path.exists():
            return []
        return [
            record["payload"]["action"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
        ]

    def test_drain_clears_the_wakeup_before_reading_the_queue(self) -> None:
        wakeup = threading.Event()
        wakeup.set()
        set_at_read: list[bool] = []
        real_drain_queue = control_queue.drain_queue

        def record(*args, **kwargs):
            set_at_read.append(wakeup.is_set())
            return real_drain_queue(*args, **kwargs)

        with mock.patch.object(control_queue, "drain_queue", record):
            self.drain(
                run_control.RunControlState(current_harness="codex"),
                wakeup=wakeup,
            )

        # Clearing after the read would swallow a signal for a command appended
        # while the read was in flight.
        self.assertEqual([False], set_at_read)

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
        # The receipt precedes the transition it acknowledges.
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
            [event["action"] for event in run_control_events],
        )
        self.assertEqual("stop-1", run_control_events[0]["command_id"])
        self.assertEqual(
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            run_control_events[0]["queue_command"],
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
        # The receipt is the only trace a duplicate leaves: it changes no state,
        # so it records no transition, but a supervisor still learns it landed.
        self.assertEqual(
            [events.RUN_CONTROL_ACTION_COMMAND_OBSERVED],
            self.run_control_actions(),
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
        self.assertEqual(
            [events.RUN_CONTROL_ACTION_COMMAND_OBSERVED],
            self.run_control_actions(),
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

        run_coordination_control.record_run_control_action(
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            action=action,
            persistence=run_persistence._RunPersistence(),
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
        run_coordination_control.record_run_control_action(
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            action=clear_action,
            persistence=run_persistence._RunPersistence(),
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

    def test_a_failed_record_puts_the_unreached_commands_back(self) -> None:
        for command_id in ("first", "second", "third"):
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=self.state_dir,
                command_id=command_id,
            )
        real_observed = run_coordination_control.record_control_command_observed
        calls = 0

        def fail_on_second(**kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            real_observed(**kwargs)

        seen: set[str] = set()
        with (
            mock.patch.object(
                run_coordination_control,
                "record_control_command_observed",
                side_effect=fail_on_second,
            ),
            self.assertRaises(OSError),
        ):
            self.drain(run_control.RunControlState(current_harness="codex"), seen)

        # The command that raised is not replayed, since how far it got is
        # unknown; the one after it was never reached and must still be queued.
        self.assertEqual({"first", "second"}, seen)
        result, _ = self.drain(
            run_control.RunControlState(current_harness="codex"), seen
        )
        self.assertEqual(("third",), result.consumed_ids)

    def test_a_failed_requeue_is_reported_without_masking_the_error(self) -> None:
        for command_id in ("first", "second"):
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
                state_dir=self.state_dir,
                command_id=command_id,
            )

        with (
            mock.patch.object(
                run_coordination_control,
                "record_control_command_observed",
                side_effect=RuntimeError("record failed"),
            ),
            mock.patch.object(
                control_queue, "requeue_lines", side_effect=OSError("disk full")
            ),
            mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            self.assertRaises(RuntimeError) as raised,
        ):
            self.drain(run_control.RunControlState(current_harness="codex"))

        # The record failure still surfaces; the command it never reached is
        # named on stderr and on the error instead of vanishing silently.
        self.assertIn("could not requeue 1 unapplied control", stderr.getvalue())
        self.assertIn("second", stderr.getvalue())
        self.assertTrue(
            any("could not requeue" in note for note in raised.exception.__notes__)
        )

    def test_rejected_lines_are_reported_on_stderr(self) -> None:
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text("not json\n", encoding="utf-8")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            result, _ = self.drain(run_control.RunControlState(current_harness="codex"))
        self.assertEqual(1, len(result.rejected))
        self.assertIn("ignored control queue line 1", stderr.getvalue())
        self.assertIn(control_queue.REJECT_INVALID_JSON, stderr.getvalue())

    def test_a_pathologically_nested_line_is_rejected_alone(self) -> None:
        # The decoder raises RecursionError, not a JSON error, for deep nesting;
        # it escaped after the truncate and lost every command in the batch.
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_STOP_AFTER_CURRENT,
            state_dir=self.state_dir,
            command_id="stop-1",
        )
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        with queue_path.open("a", encoding="utf-8") as handle:
            handle.write("[" * 1_000_000 + "\n")
        with mock.patch("sys.stderr", new_callable=io.StringIO):
            result, _ = self.drain(run_control.RunControlState(current_harness="codex"))
        self.assertEqual(("stop-1",), result.consumed_ids)
        self.assertEqual(
            [control_queue.REJECT_INVALID_JSON],
            [rejected.reason for rejected in result.rejected],
        )

    def queue_abort(
        self,
        *,
        command_id: str = "abort-1",
        grace_seconds: int | None = None,
        reason: str | None = None,
    ) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            state_dir=self.state_dir,
            grace_seconds=grace_seconds,
            reason=reason,
            command_id=command_id,
        )

    def test_abort_arms_the_turn_signal_and_records_the_request(self) -> None:
        self.queue_abort(grace_seconds=30, reason="operator needs the machine")
        abort = harnesses.AbortSignal()
        rc_state = run_control.RunControlState(current_harness="codex")

        before = time.monotonic()
        self.drain(rc_state, abort=abort)
        after = time.monotonic()

        self.assertTrue(abort.armed)
        self.assertEqual("operator needs the machine", abort.reason)
        expiry = abort.expires_at(before)
        self.assertIsNotNone(expiry)
        assert expiry is not None
        # The window starts when the poll thread observes the command, so the
        # deadline sits one grace period past the drain, not past the run.
        self.assertGreaterEqual(expiry, before + 30.0)
        self.assertLessEqual(expiry, after + 30.0)

        self.assertTrue(self.control_payload["abort_turn_requested"])
        self.assertEqual(30, self.control_payload["abort_grace_seconds"])
        self.assertEqual(
            "operator needs the machine",
            self.control_payload["abort_reason"],
        )
        self.assertIsNotNone(self.control_payload["abort_deadline"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
            ],
            self.run_control_actions(),
        )

    def test_abort_while_finalizing_arms_at_once_and_names_itself(self) -> None:
        # The grace window buys a worker time to leave usable work behind. The
        # terminal audit is advisory and read-only, and the run holds the plan
        # lock while it waits, so the cancellation lands immediately.
        self.queue_abort(grace_seconds=120)
        abort = harnesses.AbortSignal()

        before = time.monotonic()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
            finalization=True,
        )

        self.assertTrue(abort.armed)
        expiry = abort.expires_at(before)
        assert expiry is not None
        self.assertLessEqual(expiry, time.monotonic())
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_FINALIZATION_ABORT_HONORED,
            ],
            self.run_control_actions(),
        )

    def test_abort_request_event_carries_the_deadline(self) -> None:
        self.queue_abort(grace_seconds=45)
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=harnesses.AbortSignal(),
        )

        event_payload = [
            record["payload"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
            if record["payload"]["action"]
            == events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED
        ][0]
        self.assertTrue(event_payload["abort_requested"])
        self.assertEqual(45, event_payload["abort_grace_seconds"])
        # Session 7 renders the remaining grace from the ledger, so the deadline
        # has to be stamped before the request is recorded.
        self.assertEqual(
            self.control_payload["abort_deadline"],
            event_payload["abort_deadline"],
        )

    def test_omitted_grace_uses_the_producer_default(self) -> None:
        self.queue_abort()
        abort = harnesses.AbortSignal()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
        )

        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            self.control_payload["abort_grace_seconds"],
        )
        self.assertEqual(
            run_coordination_control_drain.ABORT_TURN_DEFAULT_REASON, abort.reason
        )

    def test_abort_appends_mailbox_guidance_naming_the_deadline(self) -> None:
        self.queue_abort(grace_seconds=15, reason="rebooting the host")
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=harnesses.AbortSignal(),
            mailbox_enabled=True,
        )

        pending = mailbox.read_mailbox(
            self.plan_dir, state_dir=self.state_dir
        ).pending_guidance
        self.assertEqual(1, len(pending))
        text = pending[0].text
        self.assertIn("rebooting the host", text)
        self.assertIn("15s grace", text)
        self.assertIn(str(self.control_payload["abort_deadline"]), text)
        self.assertIn("RUNNER_PAUSE: supervisor requested abort", text)

    def test_abort_without_mailbox_does_not_create_one(self) -> None:
        self.queue_abort()
        abort = harnesses.AbortSignal()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
        )

        # Appending would create the mailbox file and silently flip activation
        # for every later turn, so a disabled mailbox stays absent.
        self.assertEqual(
            [],
            list(
                mailbox.read_mailbox(
                    self.plan_dir, state_dir=self.state_dir
                ).pending_guidance
            ),
        )
        self.assertTrue(abort.armed)

    def test_failed_guidance_append_still_arms_the_abort(self) -> None:
        self.queue_abort(grace_seconds=5)
        abort = harnesses.AbortSignal()
        with mock.patch.object(
            mailbox_appenders,
            "append_guidance",
            side_effect=OSError("read-only mailbox"),
        ):
            self.drain(
                run_control.RunControlState(current_harness="codex"),
                abort=abort,
                mailbox_enabled=True,
            )

        self.assertTrue(abort.armed)
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
            ],
            self.run_control_actions(),
        )

    def test_abort_with_no_turn_in_flight_degrades_to_stop(self) -> None:
        self.queue_abort(reason="drain the queue")
        rc_state = run_control.RunControlState(current_harness="codex")

        self.drain(rc_state)

        # Nothing to kill, but dropping the command would make `asl control
        # abort` a silent no-op between turns.
        self.assertTrue(rc_state.stop_after_current)
        self.assertFalse(rc_state.snapshot()["abort_turn_requested"])
        self.assertTrue(self.control_payload["stop_after_current"])
        self.assertFalse(self.control_payload["abort_turn_requested"])
        self.assertIsNone(self.control_payload["abort_deadline"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_DEGRADED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
            self.run_control_actions(),
        )

    def test_repeated_abort_command_does_not_restart_the_window(self) -> None:
        self.queue_abort(command_id="abort-a", grace_seconds=60)
        abort = harnesses.AbortSignal()
        self.drain(
            run_control.RunControlState(current_harness="codex"),
            abort=abort,
        )
        first_deadline = self.control_payload["abort_deadline"]
        rc_state = run_control.RunControlState(current_harness="codex")
        rc_state.request_abort_turn(grace_seconds=60)

        self.queue_abort(command_id="abort-b", grace_seconds=1)
        self.drain(rc_state, abort=abort)

        # The second command's shorter grace must not shorten a running window.
        self.assertEqual(first_deadline, self.control_payload["abort_deadline"])
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_REQUESTED,
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
            ],
            self.run_control_actions(),
        )


if __name__ == "__main__":
    unittest.main()
