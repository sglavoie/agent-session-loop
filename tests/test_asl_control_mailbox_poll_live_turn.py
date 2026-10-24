"""MailboxPollTests, LiveTurnSchedulerTests, LiveTurnWakeupTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    control_queue,
    control_signal,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_constants,
    mailbox_models,
    run_commit,
    run_control,
    run_coordination_control,
    run_coordination_mailbox_poll,
    run_persistence,
    turn_execution_constants,
)
from tests.asl_control_support import live_active_turn
from tests.support import (
    read_jsonl,
)


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
            "control": run_coordination_control.initial_control_payload(),
        }
        self.control_payload = dict(self.payload["control"])  # type: ignore[arg-type]
        self.persistence = run_persistence._RunPersistence()
        self.poll = run_coordination_mailbox_poll.MailboxPoll(
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
        historical = mailbox_appenders.append_guidance(
            self.plan_dir,
            "already handled",
            state_dir=self.state_dir,
        )
        mailbox_appenders.append_acknowledgement(
            self.plan_dir,
            historical.seq,
            state_dir=self.state_dir,
        )
        sensitive_text = "Use the token s3cr3t-α without logging it."
        pending = mailbox_appenders.append_guidance(
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
                "digest": hashlib.sha256(sensitive_text.encode("utf-8")).hexdigest(),
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

    def test_unchanged_mailbox_skips_full_reads_until_append(self) -> None:
        with mock.patch.object(
            mailbox,
            "read_mailbox",
            wraps=mailbox.read_mailbox,
        ) as read:
            self.poll()
            self.poll()
            pending = mailbox_appenders.append_guidance(
                self.plan_dir,
                "new guidance",
                state_dir=self.state_dir,
            )
            self.poll()
            self.poll()

        self.assertEqual(2, read.call_count)
        self.assertEqual(
            [pending.id],
            [record["payload"]["message_id"] for record in self.mailbox_events()],
        )

    def test_unavailable_change_token_forces_full_reads(self) -> None:
        with (
            mock.patch.object(
                mailbox,
                "mailbox_change_token",
                return_value=mailbox_constants.MAILBOX_CHANGE_TOKEN_UNAVAILABLE,
            ),
            mock.patch.object(
                mailbox,
                "read_mailbox",
                wraps=mailbox.read_mailbox,
            ) as read,
        ):
            self.poll()
            self.poll()

        self.assertEqual(2, read.call_count)

    def test_new_runner_does_not_event_acknowledged_historical_guidance(
        self,
    ) -> None:
        guidance = mailbox_appenders.append_guidance(
            self.plan_dir,
            "historical",
            state_dir=self.state_dir,
        )
        mailbox_appenders.append_acknowledgement(
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
        poll = run_coordination_mailbox_poll.MailboxPoll(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=control_state,
            seen_control_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            run_control_lock=threading.Lock(),
        )

        for error in (
            OSError("unreadable"),
            mailbox_models.MailboxError(
                mailbox_constants.ERROR_MAILBOX_READ, "unreadable"
            ),
        ):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(
                    mailbox,
                    "read_mailbox",
                    side_effect=error,
                ),
            ):
                poll()

        self.assertTrue(control_state.stop_after_current)
        run_control_events = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                events.RUN_CONTROL_ACTION_STOP_AFTER_CURRENT_REQUESTED,
            ],
            [record["payload"]["action"] for record in run_control_events],
        )
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

    active_turn = staticmethod(live_active_turn)

    def test_polling_disabled_preserves_heartbeat_cadence_and_count(self) -> None:
        clock = self.Clock()
        progress = mock.Mock()
        live_turn = run_commit._LiveTurn(
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
        live_turn: run_commit._LiveTurn

        def poll() -> None:
            acquired = live_turn._lock.acquire(blocking=False)
            poll_observed_unlocked.append(acquired)
            if acquired:
                live_turn._lock.release()
            poll_times.append(clock.now)
            if len(poll_times) == 1:
                clock.now += 31.0

        live_turn = run_commit._LiveTurn(
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

    def test_a_failing_tick_never_ends_the_heartbeat_loop(self) -> None:
        # One OSError from a state write or a queue drain used to kill this
        # daemon thread, and with it stop/pause handling and hang sampling for
        # the rest of the turn.
        clock = self.Clock()
        progress = mock.Mock()
        poll_times: list[float] = []

        def poll() -> None:
            poll_times.append(clock.now)
            if len(poll_times) == 1:
                raise OSError(28, "No space left on device")

        live_turn = run_commit._LiveTurn(
            payload={},
            event_path=Path("/tmp/events.jsonl"),
            progress=progress,
            active_turn=self.active_turn(),
            heartbeat_interval=30.0,
            poll_callback=poll,
            poll_interval=10.0,
            monotonic=clock.monotonic,
            wait=clock.wait,
        )
        heartbeat_times: list[float] = []

        def record_heartbeat() -> dict[str, float]:
            heartbeat_times.append(clock.now)
            if len(heartbeat_times) == 1:
                raise OSError(5, "Input/output error")
            if len(heartbeat_times) == 3:
                live_turn._stop.set()
            return {"at": clock.now}

        stderr = io.StringIO()
        with (
            mock.patch.object(
                live_turn, "_record_heartbeat", side_effect=record_heartbeat
            ),
            redirect_stderr(stderr),
        ):
            live_turn._heartbeat_loop()

        self.assertEqual([30.0, 60.0, 90.0], heartbeat_times)
        self.assertEqual([10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0], poll_times)
        self.assertEqual(2, progress.heartbeat.call_count)
        degraded = live_turn._active_turn["heartbeat_degraded"]
        self.assertEqual(2, degraded["failures"])
        self.assertEqual("heartbeat", degraded["last_step"])
        self.assertIn("Input/output error", degraded["last_error"])
        # Announced once, not once per failing tick.
        self.assertEqual(1, stderr.getvalue().count("step failed"))

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

            live_turn = run_commit._LiveTurn(
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
                live_turn.finish(datetime(2026, 7, 24, 12, 1, tzinfo=UTC))
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


class LiveTurnWakeupTests(unittest.TestCase):
    """The poll thread drains on a wakeup and still keeps the 5s backstop."""

    class Clock(LiveTurnSchedulerTests.Clock):
        """Clock whose ``wait`` returns early while the wakeup is set."""

        def __init__(self, wakeup: threading.Event, wake_after: float) -> None:
            super().__init__()
            self.wakeup = wakeup
            self.wake_after = wake_after

        def wait(self, delay: float) -> bool:
            self.waits.append(delay)
            if self.wakeup.is_set():
                self.now += min(self.wake_after, delay)
                return True
            self.now += delay
            return False

    def test_wakeup_polls_immediately_and_clears_the_event(self) -> None:
        wakeup = threading.Event()
        wakeup.set()
        clock = self.Clock(wakeup, wake_after=2.0)
        poll_times: list[float] = []
        set_at_poll: list[bool] = []
        live_turn: run_commit._LiveTurn

        def poll() -> None:
            poll_times.append(clock.now)
            set_at_poll.append(wakeup.is_set())
            if len(poll_times) == 2:
                live_turn._stop.set()

        live_turn = run_commit._LiveTurn(
            payload={},
            event_path=Path("/tmp/events.jsonl"),
            progress=mock.Mock(),
            active_turn=LiveTurnSchedulerTests.active_turn(),
            heartbeat_interval=30.0,
            poll_callback=poll,
            poll_interval=turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
            monotonic=clock.monotonic,
            wait=clock.wait,
            wakeup=wakeup,
        )
        live_turn._heartbeat_loop()

        # First poll lands 2s in, on the wake, not at the 5s deadline; the
        # backstop then reschedules from the wake.
        self.assertEqual([2.0, 7.0], poll_times)
        self.assertEqual([False, False], set_at_poll)
        self.assertEqual([5.0, 5.0], clock.waits)

    def test_unsignalled_turn_still_polls_on_the_backstop_deadline(self) -> None:
        wakeup = threading.Event()
        clock = LiveTurnSchedulerTests.Clock()
        poll_times: list[float] = []
        live_turn: run_commit._LiveTurn

        def poll() -> None:
            poll_times.append(clock.now)
            if len(poll_times) == 2:
                live_turn._stop.set()

        live_turn = run_commit._LiveTurn(
            payload={},
            event_path=Path("/tmp/events.jsonl"),
            progress=mock.Mock(),
            active_turn=LiveTurnSchedulerTests.active_turn(),
            heartbeat_interval=30.0,
            poll_callback=poll,
            poll_interval=turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
            monotonic=clock.monotonic,
            wait=clock.wait,
            wakeup=wakeup,
        )
        live_turn._heartbeat_loop()

        self.assertEqual([5.0, 10.0], poll_times)

    def test_real_wakeup_drains_well_inside_the_backstop_interval(self) -> None:
        # End to end over the production transport: a producer's byte, the
        # runner's endpoint reader, and the poll thread it has to wake.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            wakeup = threading.Event()
            polled = threading.Event()
            live_turn = run_commit._LiveTurn(
                payload={"state_path": str(tmp_dir / "state.json")},
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=LiveTurnSchedulerTests.active_turn(),
                heartbeat_interval=60.0,
                poll_callback=polled.set,
                poll_interval=turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
                wakeup=wakeup,
            )
            with control_signal.open_wakeup_endpoint(tmp_dir, wakeup) as path:
                if path is None:
                    self.skipTest("this filesystem cannot host a wakeup FIFO")
                live_turn.start()
                self.addCleanup(live_turn.stop)
                started = time.monotonic()
                self.assertTrue(control_signal._deliver_wakeup(path).delivered)
                self.assertTrue(polled.wait(timeout=2.0))
            self.assertLess(
                time.monotonic() - started,
                turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
            )

    def test_stop_wakes_the_poll_thread_without_a_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            wakeup = threading.Event()
            live_turn = run_commit._LiveTurn(
                payload={"state_path": str(tmp_dir / "state.json")},
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=LiveTurnSchedulerTests.active_turn(),
                heartbeat_interval=60.0,
                poll_callback=lambda: None,
                poll_interval=turn_execution_constants.LIVE_CONTROL_POLL_INTERVAL_SECONDS,
                wakeup=wakeup,
            )
            live_turn.start()
            started = time.monotonic()
            live_turn.stop()

            self.assertLess(time.monotonic() - started, 2.0)
            self.assertFalse(live_turn._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
