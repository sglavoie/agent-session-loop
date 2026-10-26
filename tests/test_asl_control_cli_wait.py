"""ControlCliWaitTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    control_cli,
    control_cli_enqueue,
    control_cli_wait,
    control_queue,
    events,
    harnesses,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    run_persistence,
    state,
)
from tests.asl_control_support import LIVE_WAKEUP
from tests.support import (
    _write_live_lock_file,
    _write_state_payload,
    live_wakeup_endpoint,
    read_jsonl,
    write_plan,
)


class ControlCliWaitTests(unittest.TestCase):
    """`asl control --wait`: receipts, terminal actions, and timeout reasons."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        write_plan(self.plan_dir, ("pending", "pending"))
        self.event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

    def args(self, **overrides) -> argparse.Namespace:
        namespace = {
            "plan_dir": str(self.plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
            "wait": False,
            "wait_timeout": None,
        }
        namespace.update(overrides)
        return argparse.Namespace(**namespace)

    def mark_run_active(self) -> None:
        """A live lock owned by this process, with a turn in flight."""
        endpoint, _ = self.enterContext(
            live_wakeup_endpoint(self.plan_dir, self.state_dir)
        )
        _write_live_lock_file(self.plan_dir, self.state_dir, wakeup_endpoint=endpoint)
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260724T100000Z-1",
                "harness": "codex",
                "run_config": {"review_harness": None},
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

    def mark_run_finalizing(self) -> None:
        """The last turn ended; the run is auditing itself and holds the lock."""
        state_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir) / events.STATE_FILE_NAME
        )
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        payload["active_turn"] = None
        payload["active_finalization"] = {
            "run_id": "20260724T100000Z-1",
            "phase": "assurance-audit",
            "started_at": "2026-07-24T10:05:00+00:00",
            "elapsed_seconds": 92.0,
            "heartbeat_count": 3,
            "last_heartbeat_at": "2026-07-24T10:06:30+00:00",
            "cancellable": True,
        }
        state_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def payload_from(self, handler, args) -> tuple[int, dict]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = handler(args)
        return exit_code, json.loads(buffer.getvalue())

    def queued_command_ids(self) -> list[str]:
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        if not queue_path.exists():
            return []
        return [record["id"] for record in read_jsonl(queue_path)]

    def run_control_actions(self) -> list[str]:
        if not self.event_path.exists():
            return []
        return [
            record["payload"]["action"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
        ]

    def append_run_finished(self) -> None:
        """Stand in for the runner releasing the lock after its terminal work."""
        events.append_event(
            self.event_path,
            events.Event(
                events.RUN_FINISHED_EVENT,
                {"run_id": "20260724T100000Z-1", "reason": "stopped", "ok": True},
            ),
        )

    @contextmanager
    def background_runner(
        self,
        *,
        control_state: run_control.RunControlState | None = None,
        abort: harnesses.AbortSignal | None = None,
        after_drain: Callable[[], None] | None = None,
    ):
        """Drain the queue from another thread, like a live runner's poll.

        The real ``run_coordination_control_drain`` drain is used so
        the receipt the CLI tails is the one production writes.
        """
        stop = threading.Event()
        errors: list[BaseException] = []
        rc_state = (
            run_control.RunControlState(current_harness="codex")
            if control_state is None
            else control_state
        )
        payload: dict[str, object] = {
            "run_id": "20260724T100000Z-1",
            "control": run_coordination_control.initial_control_payload(),
            "harness": "codex",
        }
        control_payload = dict(payload["control"])  # type: ignore[arg-type]
        seen_ids: set[str] = set()

        def poll() -> None:
            try:
                while not stop.is_set():
                    result = (
                        run_coordination_control_drain.drain_external_control_queue(
                            plan_dir=self.plan_dir,
                            state_dir_arg=str(self.state_dir),
                            control_state=rc_state,
                            seen_ids=seen_ids,
                            payload=payload,
                            event_path=self.event_path,
                            control_payload=control_payload,
                            context=None,
                            persistence=run_persistence._RunPersistence(
                                write_state=False
                            ),
                            run_control_lock=threading.Lock(),
                            wakeup=None,
                            abort=abort,
                            mailbox_enabled=False,
                        )
                    )
                    if result.accepted and after_drain is not None:
                        after_drain()
                    stop.wait(0.02)
            except BaseException as error:  # surfaced on the main thread
                errors.append(error)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=10)
        if errors:
            raise errors[0]

    def test_wait_returns_the_receipt_from_a_live_runner(self) -> None:
        self.mark_run_active()

        with self.background_runner():
            exit_code, payload = self.payload_from(
                control_cli.control_stop_after_current_from_args,
                self.args(wait=True, wait_timeout=10.0),
            )

        self.assertEqual(0, exit_code, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enqueued"])
        self.assertEqual(LIVE_WAKEUP.to_payload(), payload["signal"])
        self.assertTrue(payload["wait"]["observed"])
        self.assertIsNone(payload["wait"]["terminal_action"])
        self.assertEqual(10.0, payload["wait"]["timeout_seconds"])
        self.assertLessEqual(payload["wait"]["elapsed_seconds"], 10.0)
        # The receipt the CLI saw names the command it just enqueued.
        receipts = [
            record["payload"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
            and record["payload"]["action"]
            == events.RUN_CONTROL_ACTION_COMMAND_OBSERVED
        ]
        self.assertEqual([payload["command_id"]], [r["command_id"] for r in receipts])

    def test_wait_is_satisfied_by_a_command_that_changes_no_state(self) -> None:
        self.mark_run_active()
        already_stopping = run_control.RunControlState(current_harness="codex")
        already_stopping.toggle_stop_after_current()

        with self.background_runner(control_state=already_stopping):
            exit_code, payload = self.payload_from(
                control_cli.control_stop_after_current_from_args,
                self.args(wait=True, wait_timeout=10.0),
            )

        self.assertEqual(0, exit_code, payload)
        self.assertTrue(payload["wait"]["observed"])
        # The command was already satisfied, so delivery is the only evidence
        # the runner records — and it is enough to end the wait.
        self.assertEqual(
            [events.RUN_CONTROL_ACTION_COMMAND_OBSERVED],
            self.run_control_actions(),
        )

    def test_wait_text_output_names_the_observation(self) -> None:
        self.mark_run_active()

        with self.background_runner():
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                exit_code = control_cli.control_stop_after_current_from_args(
                    self.args(json=False, wait=True, wait_timeout=10.0)
                )

        self.assertEqual(0, exit_code)
        output = buffer.getvalue().splitlines()
        self.assertEqual(1, len(output))
        self.assertIn("observed by the runner", output[0])

    def test_wait_timeout_without_a_runner_reports_no_runner(self) -> None:
        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(wait=True, wait_timeout=0.0),
        )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("no-runner", payload["reason"])
        self.assertTrue(payload["details"]["enqueued"])
        self.assertFalse(payload["details"]["wait"]["observed"])
        self.assertEqual(payload["details"]["command_id"], self.queued_command_ids()[0])
        # The command outlives the failed observation.
        self.assertEqual(1, len(self.queued_command_ids()))

    def test_wait_timeout_with_a_wedged_runner_reports_receipt_timeout(self) -> None:
        # The wakeup lands, but nothing drains the queue, so the wait still
        # times out: a delivered byte promises a drain sooner, never one at all.
        self.mark_run_active()

        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(wait=True, wait_timeout=0.0),
        )

        self.assertEqual(1, exit_code)
        self.assertEqual("receipt-timeout", payload["reason"])
        self.assertEqual(LIVE_WAKEUP.to_payload(), payload["details"]["signal"])
        self.assertFalse(payload["details"]["wait"]["observed"])
        self.assertEqual(1, len(self.queued_command_ids()))

    def test_abort_wait_blocks_until_the_degraded_abort_finishes_the_run(
        self,
    ) -> None:
        # No abort signal is armed, so the drain degrades the abort to
        # stop-after-current immediately — the terminal action the CLI is
        # waiting for, recorded as degraded because nothing was killed. The
        # wait ends on the run finishing, not on that action: the run still
        # finalizes, holding the lock.
        self.mark_run_active()

        with self.background_runner(after_drain=self.append_run_finished):
            exit_code, payload = self.payload_from(
                control_cli.control_abort_from_args,
                self.args(
                    wait=True,
                    wait_timeout=10.0,
                    grace_seconds=None,
                    reason=None,
                ),
            )

        self.assertEqual(0, exit_code, payload)
        self.assertTrue(payload["wait"]["observed"])
        self.assertTrue(payload["wait"]["run_finished"])
        self.assertEqual(
            events.RUN_CONTROL_ACTION_ABORT_TURN_DEGRADED,
            payload["wait"]["terminal_action"],
        )

    def test_abort_wait_blocks_until_a_worker_satisfies_the_turn(self) -> None:
        self.mark_run_active()
        abort = harnesses.AbortSignal()

        def satisfied_by_worker() -> None:
            # Stands in for the worker that stopped on its own inside the grace
            # window; only the runner records this action.
            events.append_event(
                self.event_path,
                events.Event(
                    events.RUN_CONTROL_EVENT,
                    {
                        "run_id": "20260724T100000Z-1",
                        "action": (
                            events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER
                        ),
                        "message": "abort satisfied by worker",
                    },
                ),
            )
            self.append_run_finished()

        with self.background_runner(abort=abort, after_drain=satisfied_by_worker):
            exit_code, payload = self.payload_from(
                control_cli.control_abort_from_args,
                self.args(
                    wait=True,
                    wait_timeout=10.0,
                    grace_seconds=None,
                    reason=None,
                ),
            )

        self.assertEqual(0, exit_code, payload)
        self.assertTrue(payload["wait"]["run_finished"])
        self.assertEqual(
            events.RUN_CONTROL_ACTION_ABORT_TURN_SATISFIED_BY_WORKER,
            payload["wait"]["terminal_action"],
        )

    def test_abort_wait_timeout_after_delivery_reports_abort_timeout(self) -> None:
        self.mark_run_active()
        real_wait = control_cli_wait._wait_for_receipt

        def receipt_without_outcome(plan_dir, **kwargs):
            # Emulate a runner that acknowledged the command but whose grace
            # window outlasts the wait: append only the receipt, then let the
            # real tail run against a zero-length deadline.
            events.append_event(
                self.event_path,
                events.Event(
                    events.RUN_CONTROL_EVENT,
                    {
                        "run_id": "20260724T100000Z-1",
                        "action": events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                        "command_id": kwargs["command_id"],
                        "message": "control command observed",
                    },
                ),
            )
            return real_wait(plan_dir, **kwargs)

        with mock.patch.object(
            control_cli_enqueue, "_wait_for_receipt", receipt_without_outcome
        ):
            exit_code, payload = self.payload_from(
                control_cli.control_abort_from_args,
                self.args(
                    wait=True,
                    wait_timeout=0.0,
                    grace_seconds=None,
                    reason=None,
                ),
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("abort-timeout", payload["reason"])
        self.assertTrue(payload["details"]["wait"]["observed"])
        self.assertIsNone(payload["details"]["wait"]["terminal_action"])

    def test_abort_wait_timeout_while_finalizing_says_the_run_is_finalizing(
        self,
    ) -> None:
        # The turn ended and the abort was honored, but the run is still
        # auditing itself and holds the lock: reporting success here is what
        # taught a supervisor to read a finalizing run as a hang.
        self.mark_run_active()
        self.mark_run_finalizing()
        real_wait = control_cli_wait._wait_for_receipt

        def receipt_then_honored(plan_dir, **kwargs):
            for action in (
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED,
            ):
                events.append_event(
                    self.event_path,
                    events.Event(
                        events.RUN_CONTROL_EVENT,
                        {
                            "run_id": "20260724T100000Z-1",
                            "action": action,
                            "command_id": kwargs["command_id"],
                            "message": "abort turn honored",
                        },
                    ),
                )
            return real_wait(plan_dir, **kwargs)

        with mock.patch.object(
            control_cli_enqueue, "_wait_for_receipt", receipt_then_honored
        ):
            exit_code, payload = self.payload_from(
                control_cli.control_abort_from_args,
                self.args(
                    wait=True,
                    wait_timeout=0.0,
                    grace_seconds=None,
                    reason=None,
                ),
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("finalization-timeout", payload["reason"])
        wait = payload["details"]["wait"]
        self.assertTrue(wait["observed"])
        self.assertEqual(
            events.RUN_CONTROL_ACTION_ABORT_TURN_HONORED, wait["terminal_action"]
        )
        self.assertFalse(wait["run_finished"])
        self.assertEqual("assurance-audit", wait["finalizing"]["phase"])
        self.assertTrue(wait["finalizing"]["cancellable"])
        self.assertIn("repeat `asl control abort`", wait["note"])

    def test_default_enqueue_neither_waits_nor_reports_a_wait(self) -> None:
        for handler, extra in (
            (control_cli.control_stop_after_current_from_args, {}),
            (
                control_cli.control_abort_from_args,
                {"grace_seconds": None, "reason": None},
            ),
            (
                control_cli.control_select_harness_from_args,
                {"harness": "claude"},
            ),
            (
                control_cli.control_set_model_from_args,
                {
                    "harness": None,
                    "harness_model": "gpt-5.4",
                    "clear_harness_model": False,
                    "effort": None,
                    "clear_effort": False,
                },
            ),
        ):
            with self.subTest(handler=handler.__name__):
                exit_code, payload = self.payload_from(handler, self.args(**extra))

                self.assertEqual(0, exit_code, payload)
                self.assertNotIn("wait", payload)

    def test_timeout_without_wait_is_rejected_before_enqueue(self) -> None:
        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(wait_timeout=5.0),
        )

        self.assertEqual(1, exit_code)
        self.assertEqual("invalid-timeout", payload["reason"])
        self.assertEqual([], self.queued_command_ids())

    def test_unbounded_and_negative_timeouts_are_rejected(self) -> None:
        for requested in (-1.0, float("inf"), float("nan"), 1801.0):
            with self.subTest(timeout=requested):
                exit_code, payload = self.payload_from(
                    control_cli.control_stop_after_current_from_args,
                    self.args(wait=True, wait_timeout=requested),
                )

                self.assertEqual(1, exit_code)
                self.assertEqual("invalid-timeout", payload["reason"])
                self.assertEqual([], self.queued_command_ids())


class UndecodableWaitInputTests(unittest.TestCase):
    # Bytes that are not UTF-8 used to escape as UnicodeDecodeError out of a
    # receipt wait instead of reading like a torn line or a torn state.

    def test_a_non_utf8_event_line_is_skipped_and_its_neighbors_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / events.EVENTS_FILE_NAME
            first = json.dumps({"name": events.RUN_CONTROL_EVENT, "payload": {}})
            last = json.dumps({"name": events.RUN_FINISHED_EVENT, "payload": {}})
            path.write_bytes(
                first.encode() + b'\n{"name": "\xff"}\n' + last.encode() + b"\n"
            )

            records = control_cli_wait._read_wait_records(path)

        self.assertEqual(
            [events.RUN_CONTROL_EVENT, events.RUN_FINISHED_EVENT],
            [record["name"] for record in records],
        )

    def test_a_non_utf8_state_reports_no_active_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            state_path = (
                state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_bytes(b'{"active_finalization": {"phase": "\xff"}}')

            self.assertIsNone(
                control_cli_wait._active_finalization(plan_dir, str(state_dir))
            )


if __name__ == "__main__":
    unittest.main()
