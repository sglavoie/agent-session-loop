#!/usr/bin/env python3
"""Tests for events tailer hang detection and its invoke_run integration."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from asl_autopilot_test_fakes import FakeTimeoutPopen, _FakeTailer
from asl_autopilot_test_payloads import (
    _cpu_progress_block,
    _hang_block,
    _running_status,
    _turn_heartbeat_record,
)
from asl_autopilot_test_support import (  # noqa: F401
    _batch_follower,
    _clock_from,
    _counter_clock,
    autopilot,
    setUpModule,
    tearDownModule,
)


class EventsTailerHangTest(unittest.TestCase):
    """The tailer folds hang blocks from heartbeats and one-shot hang events."""

    def test_heartbeat_folds_and_drops_the_hang_block(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0, 3.0])
        with_block = _turn_heartbeat_record(162)
        with_block["payload"]["hang_suspected"] = _hang_block()
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[with_block], [_turn_heartbeat_record(4)]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertEqual(
            snapshot["hang_suspected"]["reason"],
            autopilot.HANG_CLASS_RESULT_RECEIVED,
        )
        # A heartbeat without the block is an implicit retraction.
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)

    def test_one_shot_hang_events_set_and_clear_suspicion(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0, 3.0, 4.0])
        suspected = {
            "name": autopilot._events.TURN_HANG_SUSPECTED_EVENT,
            "payload": {"hang_suspected": _hang_block()},
        }
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {},
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower(
                [[_turn_heartbeat_record(30)], [suspected], [cleared]]
            ),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        snapshot, _ = tailer.snapshot()
        # The one-shot event folds onto the running snapshot in place.
        self.assertEqual(snapshot["state"], "running")
        self.assertEqual(snapshot["session_id"], "tidy-db-writes")
        self.assertIn("hang_suspected", snapshot)
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)

    def test_same_batch_ceiling_heartbeat_then_clear_resets_stale_idle(self) -> None:
        # F-003: a 900s heartbeat and its retraction folded in one batch must
        # not leave the stale idle reading behind for the ceiling check.
        clock = _clock_from([0.0, 1.0, 2.0])
        hung = _turn_heartbeat_record(900)
        hung["payload"]["hang_suspected"] = _hang_block(
            autopilot.HANG_CLASS_ZERO_CPU, idle=900.0
        )
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {
                "idle_seconds": 0.0,
                "hang_suspected": _hang_block(
                    autopilot.HANG_CLASS_ZERO_CPU, idle=900.0
                ),
            },
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[hung, cleared]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertEqual(snapshot["idle_seconds"], 0.0)
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(snapshot))

    def test_same_batch_clear_then_rearm_starts_a_new_episode(self) -> None:
        # F-001: a clear and a re-armed suspicion folded between two polls
        # leave only the new block visible; its different detected_at must
        # reset the probe count instead of inheriting the old episode's.
        clock = _clock_from([0.0, 1.0, 2.0, 3.0])
        first = _turn_heartbeat_record(162)
        first["payload"]["hang_suspected"] = _hang_block()
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {"idle_seconds": 0.0, "hang_suspected": _hang_block()},
        }
        rearmed_block = _hang_block(idle=45.0)
        rearmed_block["detected_at"] = "2026-07-24T19:05:30+00:00"
        rearmed = {
            "name": autopilot._events.TURN_HANG_SUSPECTED_EVENT,
            "payload": {"hang_suspected": dict(rearmed_block)},
        }
        followup = _turn_heartbeat_record(75)
        followup["payload"]["hang_suspected"] = dict(rearmed_block)
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[first], [cleared, rearmed], [followup]]),
        )
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot, _ = tailer.snapshot()
        self.assertIsNone(policy.observe(snapshot))
        snapshot, _ = tailer.snapshot()
        self.assertEqual(
            snapshot["hang_suspected"]["detected_at"],
            "2026-07-24T19:05:30+00:00",
        )
        # One observation of the re-armed episode is probe 1, never probe 2.
        self.assertIsNone(policy.observe(snapshot))
        snapshot, _ = tailer.snapshot()
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(verdict["probes"], 2)

    def test_heartbeat_carries_cpu_progress_without_suspicion(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0, 3.0])
        progressing = _turn_heartbeat_record(400)
        progressing["payload"]["hang_cpu_progress"] = _cpu_progress_block(True)
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[progressing], [_turn_heartbeat_record(4)]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertTrue(snapshot["hang_cpu_progress"]["progressing"])
        # No fresh sample on the next heartbeat: absence must never be read
        # back as a stale "was progressing".
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_cpu_progress", snapshot)

    def test_one_shot_hang_events_carry_and_drop_cpu_progress(self) -> None:
        clock = _clock_from([0.0, 1.0, 2.0, 3.0, 4.0])
        suspected = {
            "name": autopilot._events.TURN_HANG_SUSPECTED_EVENT,
            "payload": {
                "hang_suspected": _hang_block(),
                "hang_cpu_progress": _cpu_progress_block(
                    False, delta=0.0, confirm_samples=2
                ),
            },
        }
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {"idle_seconds": 0.0},
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower(
                [[_turn_heartbeat_record(30)], [suspected], [cleared]]
            ),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_cpu_progress", snapshot)
        snapshot, _ = tailer.snapshot()
        self.assertFalse(snapshot["hang_cpu_progress"]["progressing"])
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertNotIn("hang_cpu_progress", snapshot)

    def test_clear_republishes_a_fresh_progressing_sample(self) -> None:
        # The runner clears on positive CPU, so its retraction event is the
        # one place a `progressing: true` sample and a clear arrive together.
        clock = _clock_from([0.0, 1.0, 2.0])
        hung = _turn_heartbeat_record(950)
        hung["payload"]["hang_suspected"] = _hang_block(
            autopilot.HANG_CLASS_ZERO_CPU, idle=950.0
        )
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {"hang_cpu_progress": _cpu_progress_block(True)},
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[hung, cleared]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertTrue(snapshot["hang_cpu_progress"]["progressing"])

    def test_clear_without_idle_drops_the_stale_reading(self) -> None:
        # A cleared payload without the runner's fresh idle (older runners)
        # still invalidates the pre-clear value instead of retaining it.
        clock = _clock_from([0.0, 1.0, 2.0])
        hung = _turn_heartbeat_record(900)
        hung["payload"]["hang_suspected"] = _hang_block(
            autopilot.HANG_CLASS_ZERO_CPU, idle=900.0
        )
        cleared = {
            "name": autopilot._events.TURN_HANG_CLEARED_EVENT,
            "payload": {},
        }
        tailer = autopilot._EventsTailer(
            "events.jsonl",
            clock=clock,
            follow=_batch_follower([[hung, cleared]]),
        )
        snapshot, _ = tailer.snapshot()
        self.assertNotIn("hang_suspected", snapshot)
        self.assertNotIn("idle_seconds", snapshot)
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(snapshot))


class InvokeRunEventsTailerTest(unittest.TestCase):
    """`_invoke_run` prefers the events tailer and falls back to the probe."""

    FINAL_JSON = '{"ok": true, "paused": false, "reason": "all-complete"}'

    def _parsed(self) -> dict:
        return {"argv": ["asl", "run", "/tmp/plan", "--json"], "plan_dir": "/tmp/plan"}

    def _run(self, *, popen, tailer=None, status_probe=None, stall_threshold=90.0):
        beats: list = []
        payload = autopilot._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=beats.append,
            heartbeat_interval=0.01,
            popen=popen,
            status_probe=status_probe,
            events_tailer=tailer,
            stall_threshold=stall_threshold,
        )
        return payload, beats

    def test_tailer_snapshot_drives_the_tick_line(self) -> None:
        tailer = _FakeTailer(
            [
                (
                    {
                        "state": "running",
                        "session_id": "s7",
                        "session_number": 2,
                        "total_sessions": 5,
                        "idle_seconds": 96,
                    },
                    False,
                )
            ]
        )
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, tailer=tailer)
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("session s7", tick)
        self.assertIn("(2/5)", tick)
        self.assertIn("idle 96s", tick)
        # Fresh stream (events_stale False) suppresses the WARN despite high idle.
        self.assertNotIn("WARN", tick)

    def test_tailer_stall_flag_renders_warning(self) -> None:
        tailer = _FakeTailer(
            [({"state": "running", "session_id": "s7", "idle_seconds": 96}, True)]
        )
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(popen=popen, tailer=tailer)
        self.assertIn("WARN possibly stalled", beats[1])
        self.assertIn("idle 96s (no output)", beats[1])

    def test_tailer_failure_falls_back_to_status_probe(self) -> None:
        class _BoomTailer:
            def snapshot(self):
                raise RuntimeError("tailer blew up")

        popen = FakeTimeoutPopen(timeout_count=2, stdout=self.FINAL_JSON)
        payload, beats = self._run(
            popen=popen,
            tailer=_BoomTailer(),
            status_probe=lambda: _running_status(30),
        )
        self.assertEqual(payload["reason"], "all-complete")
        # Both ticks fell back to the status probe (idle 30, enriched line).
        self.assertIn("session tidy-db-writes", beats[1])
        self.assertIn("idle 30s", beats[1])
        self.assertIn("idle 30s", beats[2])

    def test_unknown_tailer_snapshot_falls_back_to_status_probe(self) -> None:
        # A missing/unreadable events file makes `follow_events` yield empty
        # batches (never raising), so the tailer snapshot stays "unknown". The
        # tick must then fall back to the authoritative status probe instead of
        # emitting a plain line, so enriched progress and stall detection are
        # not permanently suppressed. The tailer is not disabled, so it resumes
        # on later ticks once the events file appears.
        tailer = _FakeTailer([({"state": "unknown"}, False)])
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(
            popen=popen,
            tailer=tailer,
            status_probe=lambda: _running_status(41),
        )
        self.assertEqual(payload["reason"], "all-complete")
        tick = beats[1]
        self.assertIn("session tidy-db-writes", tick)
        self.assertIn("idle 41s", tick)
        self.assertNotIn("running, elapsed", tick)

    def test_unknown_tailer_snapshot_with_stalled_probe_warns(self) -> None:
        # The recovered fallback still surfaces a stall: an unknown tailer
        # snapshot with a status probe reporting idle past the threshold must
        # render the WARN line rather than a suppressed plain line.
        tailer = _FakeTailer([({"state": "unknown"}, False)])
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        _payload, beats = self._run(
            popen=popen,
            tailer=tailer,
            status_probe=lambda: _running_status(96),
        )
        self.assertIn("WARN possibly stalled", beats[1])
        self.assertIn("idle 96s (no output)", beats[1])

    def test_builds_tailer_from_status_events_path(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-tailer-wire-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        events_path = os.path.join(tmp, "events.jsonl")
        with open(events_path, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(_turn_heartbeat_record(42, session_id="wired")) + "\n"
            )
        status = {"ok": True, "events_path": events_path}
        popen = FakeTimeoutPopen(timeout_count=1, stdout=self.FINAL_JSON)
        payload, beats = self._run(popen=popen, status_probe=lambda: status)
        self.assertEqual(payload["reason"], "all-complete")
        # The tick was derived from the events file, not a per-tick status probe.
        self.assertIn("session wired", beats[1])
        self.assertIn("idle 42s", beats[1])


if __name__ == "__main__":
    unittest.main()
