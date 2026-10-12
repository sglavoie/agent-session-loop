#!/usr/bin/env python3
"""Tests for invoke_run heartbeats and the events tailer."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from asl_autopilot_test_fakes import FakeTimeoutPopen
from asl_autopilot_test_payloads import _running_status, _turn_heartbeat_record
from asl_autopilot_test_support import (  # noqa: F401
    _batch_follower,
    _clock_from,
    _counter_clock,
    autopilot,
    setUpModule,
    tearDownModule,
)


class InvokeRunHeartbeatTest(unittest.TestCase):
    """Exercise the per-tick status probe / stall detection in `_invoke_run`."""

    FINAL_JSON = '{"ok": true, "paused": false, "reason": "all-complete"}'

    def _parsed(self) -> dict:
        return {"argv": ["asl", "run", "/tmp/plan", "--json"], "plan_dir": "/tmp/plan"}

    def _run(self, *, popen, status_probe=None, stall_threshold=None):
        beats: list = []
        kwargs = {}
        if stall_threshold is not None:
            kwargs["stall_threshold"] = stall_threshold
        payload = autopilot._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=beats.append,
            heartbeat_interval=0.01,
            popen=popen,
            status_probe=status_probe,
            **kwargs,
        )
        return payload, beats

    def test_enriched_running_line_has_session_label_and_idle(self) -> None:
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(
            popen=popen,
            status_probe=lambda: _running_status(34),
        )
        self.assertEqual(payload["reason"], "all-complete")
        # First beat is the "started" line; the tick beat is the enriched one.
        tick = beats[1]
        self.assertIn("session tidy-db-writes", tick)
        self.assertIn("(3/8)", tick)
        self.assertIn("idle 34s", tick)
        self.assertNotIn("running, elapsed", tick)

    def test_idle_over_threshold_emits_stall_warning(self) -> None:
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(
            popen=popen,
            status_probe=lambda: _running_status(96),
            stall_threshold=90.0,
        )
        tick = beats[1]
        self.assertIn("WARN possibly stalled", tick)
        self.assertIn("idle 96s (no output)", tick)

    def test_running_line_includes_pending_outbox_count(self) -> None:
        status = _running_status(34)
        status["mailbox"] = {"open_questions": 3}
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, status_probe=lambda: status)
        self.assertIn("pending outbox 3", beats[1])

    def test_probe_that_raises_falls_back_to_plain_line(self) -> None:
        def boom():
            raise RuntimeError("status blew up")

        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, status_probe=boom)
        # Run still returns its final payload despite the probe failure.
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("running, elapsed", tick)
        self.assertNotIn("session", tick)

    def test_status_error_payload_falls_back_to_plain_line(self) -> None:
        error = {"ok": False, "stage": autopilot.RUN_STAGE, "reason": "status-error"}
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, status_probe=lambda: error)
        tick = beats[1]
        self.assertIn("running, elapsed", tick)
        self.assertNotIn("session", tick)

    def test_review_phase_snapshot_renders_verifying(self) -> None:
        verifying = {
            "ok": True,
            "runner_state": {
                "paused": False,
                "active_turn": None,
                "active_verification": {"session_id": "1", "phase": "verify"},
            },
        }
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, status_probe=lambda: verifying)
        tick = beats[1]
        self.assertIn("verifying", tick)
        self.assertNotIn("session tidy-db-writes", tick)

    def test_no_probe_reproduces_legacy_plain_line(self) -> None:
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, status_probe=None)
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("running, elapsed", tick)
        self.assertNotIn("session", tick)
        self.assertNotIn("|", tick)


class EventsTailerTest(unittest.TestCase):
    """`_EventsTailer` folds appended events into per-tick heartbeat snapshots."""

    def test_fresh_events_suppress_stall_despite_high_idle(self) -> None:
        # Construction reads the clock once, then one snapshot per call.
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(96)]]),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["idle_seconds"], 96)
        # A just-seen event keeps the stream fresh even though idle >= threshold.
        self.assertFalse(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", message)
        self.assertIn("idle 96s", message)

    def test_event_silence_past_threshold_triggers_stall(self) -> None:
        clock = _clock_from([0.0, 1.0, 200.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(96)]]),
        )
        # First snapshot folds the heartbeat and marks the stream fresh (t=1).
        tailer.snapshot()
        # Second snapshot at t=200 sees no new events -> silent for 199s.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["idle_seconds"], 96)
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("WARN possibly stalled", message)
        self.assertIn("idle 96s (no output)", message)

    def test_below_threshold_idle_then_silence_triggers_stall(self) -> None:
        # Regression for F4: a heartbeat sampled with idle *below* the threshold
        # must still surface a stall once the event stream goes silent past the
        # threshold. The frozen sub-threshold idle value must not suppress it.
        clock = _clock_from([0.0, 1.0, 200.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(30)]]),
        )
        # First snapshot folds the heartbeat (idle 30, below threshold) at t=1.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["idle_seconds"], 30)
        self.assertFalse(events_stale)
        fresh = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", fresh)
        # Second snapshot at t=200 sees no new events -> silent for 199s. The
        # idle sample is still frozen at 30 (< threshold), yet event silence
        # alone must now drive the stall verdict.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["idle_seconds"], 30)
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("WARN possibly stalled", message)
        self.assertIn("no new events for 90s+ (no output)", message)

    def test_fresh_events_below_threshold_idle_stay_quiet(self) -> None:
        # A fresh stream with a below-threshold idle sample is a healthy running
        # tick — no stall, enriched line carries the idle fragment.
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[_turn_heartbeat_record(30)]]),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertFalse(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", message)
        self.assertIn("idle 30s", message)

    def test_verification_and_pause_records_change_state(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower(
                [
                    [{"name": "verification_heartbeat", "payload": {}}],
                    [{"name": "run_paused", "payload": {"reason": "quota-exhausted"}}],
                ]
            ),
        )
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "verifying")
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "paused")
        self.assertEqual(snapshot["reason"], "quota-exhausted")

    def test_wait_started_is_healthy_even_when_event_stream_goes_silent(self) -> None:
        clock = _clock_from([0.0, 1.0, 200.0])
        wait_started = {
            "name": autopilot._events.MAILBOX_EVENT,
            "payload": {
                "action": autopilot._events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-12",
                "session_id": "12",
                "session_number": 12,
                "turn_index": 2,
                "pending_outbox": 3,
                "base_deadline": "2099-07-24T19:05:00+00:00",
                "hard_deadline": "2099-07-24T19:15:00+00:00",
            },
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower([[wait_started]]),
        )

        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "waiting_for_reply")
        self.assertEqual(snapshot["pending_outbox"], 3)
        self.assertFalse(events_stale)
        snapshot, events_stale = tailer.snapshot()
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 200.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("waiting for reply for session 12", message)
        self.assertIn("pending outbox 3", message)
        self.assertNotIn("WARN possibly stalled", message)
        self.assertNotEqual(snapshot["state"], "paused")

    def test_wait_cleared_ends_wait_and_preserves_actual_outbox_count(self) -> None:
        wait_started = {
            "name": autopilot._events.MAILBOX_EVENT,
            "payload": {
                "action": autopilot._events.MAILBOX_ACTION_WAIT_STARTED,
                "question_id": "question-12",
                "session_id": "12",
                "turn_index": 2,
                "pending_outbox": 3,
                "hard_deadline": "2099-07-24T19:15:00+00:00",
            },
        }
        wait_cleared = {
            "name": autopilot._events.MAILBOX_EVENT,
            "payload": {
                "action": autopilot._events.MAILBOX_ACTION_WAIT_CLEARED,
                "question_id": "question-12",
                "session_id": "12",
                "turn_index": 2,
                "reason": "reply",
                "pending_outbox": 2,
            },
        }
        turn_started = {
            "name": "turn_started",
            "payload": {
                "session_id": "12",
                "session_number": 12,
                "total_sessions": 14,
            },
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=_clock_from([0.0, 1.0, 2.0, 3.0]),
            follow=_batch_follower([[wait_started], [wait_cleared], [turn_started]]),
        )

        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "waiting_for_reply")
        self.assertEqual(snapshot["pending_outbox"], 3)
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "unknown")
        self.assertEqual(snapshot["pending_outbox"], 2)
        snapshot, _ = tailer.snapshot()
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["pending_outbox"], 2)

    def test_supervised_wait_past_its_anchor_stays_healthy(self) -> None:
        # The persisted anchor lapsed two hours ago, but the last lease proved
        # a supervisor alive well inside the ceiling, so the wait rolls on.
        snapshot = {
            "state": "waiting_for_reply",
            "session_id": "12",
            "pending_outbox": 2,
            "unsupervised_deadline_at": "2026-07-24T19:15:00+00:00",
            "unsupervised_ceiling_seconds": 1800.0,
            "last_lease_activity_at": "2026-07-24T21:00:00+00:00",
        }

        message = autopilot._format_heartbeat(
            1,
            900.0,
            snapshot,
            90.0,
            events_stale=True,
            now=datetime(2026, 7, 24, 21, 15, tzinfo=UTC),
        )

        self.assertNotIn("WARN possibly stalled", message)
        self.assertIn("waiting for reply for session 12", message)

    def test_unwatched_wait_past_its_ceiling_triggers_stall_warning(
        self,
    ) -> None:
        snapshot = {
            "state": "waiting_for_reply",
            "session_id": "12",
            "pending_outbox": 2,
            "unsupervised_deadline_at": "2026-07-24T19:15:00+00:00",
            "unsupervised_ceiling_seconds": 1800.0,
            "last_lease_activity_at": None,
        }

        message = autopilot._format_heartbeat(
            1,
            900.0,
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 15, tzinfo=UTC),
        )

        self.assertIn("WARN possibly stalled", message)
        self.assertIn("unsupervised ceiling elapsed", message)

    def test_elapsed_wait_hard_deadline_triggers_stall_warning(self) -> None:
        snapshot = {
            "state": "waiting_for_reply",
            "session_id": "12",
            "pending_outbox": 2,
            "hard_deadline": "2026-07-24T19:15:00+00:00",
        }

        message = autopilot._format_heartbeat(
            1,
            900.0,
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 15, tzinfo=UTC),
        )

        self.assertIn("WARN possibly stalled", message)
        self.assertIn("hard deadline elapsed", message)
        self.assertIn("pending outbox 2", message)

    def test_fresh_verification_stream_stays_quiet(self) -> None:
        # Regression for F4: a verifying snapshot from a fresh event stream keeps
        # the normal verifying heartbeat (no stall warning).
        clock = _clock_from([0.0, 1.0, 2.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower(
                [[{"name": "verification_heartbeat", "payload": {}}]]
            ),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "verifying")
        self.assertFalse(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertNotIn("WARN", message)
        self.assertIn("verifying", message)

    def test_silent_verification_stream_triggers_stall(self) -> None:
        # Regression for F4: once a verification heartbeat is folded and the
        # stream goes silent past the threshold, the verifying snapshot must
        # surface a stall warning instead of a healthy verifying heartbeat.
        clock = _clock_from([0.0, 1.0, 200.0])
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            stall_threshold=90.0,
            follow=_batch_follower(
                [[{"name": "verification_heartbeat", "payload": {}}]]
            ),
        )
        # First snapshot folds the verification heartbeat and marks it fresh.
        tailer.snapshot()
        # Second snapshot at t=200 sees no new events -> silent for 199s.
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "verifying")
        self.assertTrue(events_stale)
        message = autopilot._format_heartbeat(
            1, 10.0, snapshot, 90.0, events_stale=events_stale
        )
        self.assertIn("WARN possibly stalled", message)
        self.assertIn("no new events for 90s+ (no output)", message)

    def test_starts_unknown_before_any_folded_event(self) -> None:
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=_clock_from([0.0, 1.0]),
            follow=_batch_follower([[]]),
        )
        snapshot, events_stale = tailer.snapshot()
        self.assertEqual(snapshot["state"], "unknown")
        self.assertFalse(events_stale)


if __name__ == "__main__":
    unittest.main()
