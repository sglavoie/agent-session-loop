"""ControlCliTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from agent_session_loop import (
    control_cli,
    control_queue,
    plans,
)
from tests.asl_control_cli_support import ControlCliTestsBase
from tests.asl_control_support import LIVE_WAKEUP
from tests.support import (
    _write_lock_file,
    _write_state_payload,
    read_jsonl,
    write_plan,
)


class ControlCliTests(ControlCliTestsBase):
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

    def test_stop_after_current_reports_no_runner_to_signal(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertEqual({"delivered": False, "reason": "no-runner"}, payload["signal"])

    def test_stop_after_current_reports_the_wakeup_it_could_send(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        # The lock names an endpoint this process is really reading, so the
        # byte is genuinely delivered — into a pipe nothing acts on.
        self.mark_run_active()

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertEqual(LIVE_WAKEUP.to_payload(), payload["signal"])

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
        self.assertFalse(payload["review_continuation_pending"])
        self.assertEqual(
            "current session is the last; stop is a no-op", payload["note"]
        )

    def test_stop_after_current_last_session_reports_pending_review(self) -> None:
        write_plan(self.plan_dir, ("complete", "pending"))
        self.mark_run_active(review_harness="codex")

        _, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(),
        )

        self.assertTrue(payload["run_active"])
        self.assertEqual(0, payload["pending_after_current"])
        self.assertTrue(payload["review_continuation_pending"])
        self.assertEqual(
            "current session is the last; stop also prevents the pending review",
            payload["note"],
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

    def test_abort_enqueues_valid_drainable_line(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        exit_code, payload = self.payload_from(
            control_cli.control_abort_from_args,
            self.args(grace=45, reason="supervisor stop"),
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("control-abort", payload["command"])
        self.assertTrue(payload["enqueued"])
        self.assertTrue(payload["run_active"])
        self.assertTrue(payload["turn_active"])
        self.assertEqual(45, payload["grace_seconds"])
        self.assertEqual("supervisor stop", payload["reason"])
        self.assertEqual("a turn is active; abort will end it", payload["note"])

        lines = read_jsonl(self.queue_path())
        self.assertEqual(1, len(lines))
        self.assertEqual(payload["command_id"], lines[0]["id"])
        self.assertEqual(control_queue.COMMAND_REQUEST_ABORT_TURN, lines[0]["command"])

        result = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), result.rejected)
        self.assertEqual((payload["command_id"],), result.consumed_ids)
        self.assertEqual(45, result.accepted[0].grace_seconds)

    def test_abort_defaults_the_grace_window(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        _, payload = self.payload_from(
            control_cli.control_abort_from_args,
            self.args(),
        )

        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            payload["grace_seconds"],
        )
        self.assertIsNone(payload["reason"])

    def test_abort_without_active_turn_reports_degradation(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active_between_turns()

        _, payload = self.payload_from(
            control_cli.control_abort_from_args,
            self.args(grace=30),
        )

        self.assertTrue(payload["run_active"])
        self.assertFalse(payload["turn_active"])
        self.assertEqual(
            "no turn active; abort degrades to stop-after-current",
            payload["note"],
        )

    def test_abort_while_finalizing_says_it_cancels_the_audit(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active_between_turns()
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260724T100000Z-1",
                "harness": "codex",
                "run_config": {"review_harness": None},
                "reason": "stopped",
                "run_started_at": "2026-07-24T10:00:00+00:00",
                "run_finished_at": None,
                "duration_seconds": None,
                "active_turn": None,
                "active_verification": None,
                "active_finalization": {
                    "run_id": "20260724T100000Z-1",
                    "phase": "assurance-audit",
                    "started_at": "2026-07-24T10:05:00+00:00",
                    "elapsed_seconds": 120.0,
                    "heartbeat_count": 4,
                    "last_heartbeat_at": "2026-07-24T10:07:00+00:00",
                    "cancellable": True,
                },
            },
        )

        _, payload = self.payload_from(
            control_cli.control_abort_from_args,
            self.args(),
        )

        self.assertTrue(payload["run_active"])
        self.assertFalse(payload["turn_active"])
        self.assertEqual(
            "no turn active; the run is finalizing, so abort cancels its "
            "terminal audit",
            payload["note"],
        )

    def test_abort_without_active_run_still_enqueues(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))

        exit_code, payload = self.payload_from(
            control_cli.control_abort_from_args,
            self.args(),
        )

        self.assertEqual(0, exit_code)
        self.assertFalse(payload["run_active"])
        self.assertFalse(payload["turn_active"])
        self.assertEqual(
            "no active run; command applies when a runner next drains the queue",
            payload["note"],
        )
        self.assertTrue(self.queue_path().exists())

    def test_abort_rejects_malformed_grace_and_reason(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        cases = (
            ({"grace": 0}, control_queue.REJECT_INVALID_GRACE),
            ({"grace": -5}, control_queue.REJECT_INVALID_GRACE),
            (
                {"grace": control_queue.MAX_ABORT_GRACE_SECONDS + 1},
                control_queue.REJECT_INVALID_GRACE,
            ),
            ({"reason": ""}, control_queue.REJECT_INVALID_REASON),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                exit_code, payload = self.payload_from(
                    control_cli.control_abort_from_args,
                    self.args(**overrides),
                )
                self.assertEqual(1, exit_code)
                self.assertFalse(payload["ok"])
                self.assertEqual(expected, payload["reason"])

        # A rejected command never reaches the queue.
        self.assertFalse(self.queue_path().exists())

    def test_abort_text_output_is_one_line(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        self.mark_run_active()

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = control_cli.control_abort_from_args(
                self.args(json=False, grace=15)
            )

        self.assertEqual(0, exit_code)
        output = buffer.getvalue().splitlines()
        self.assertEqual(1, len(output))
        self.assertIn("Queued abort for", output[0])
        self.assertIn("a turn is active; abort will end it", output[0])

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
        self.assertEqual("future sessions will use the claude harness", payload["note"])
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
        self.assertEqual("next-implementation-turn", payload["applies_to"])
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
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.rejected)
        self.assertEqual("set", drained.accepted[0].model_operation.operation)
        self.assertEqual("set", drained.accepted[0].effort_operation.operation)

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


if __name__ == "__main__":
    unittest.main()
