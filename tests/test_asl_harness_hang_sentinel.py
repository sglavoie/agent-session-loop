"""Live-turn hang sentinel integration."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    process_tree,
    run_commit,
    turn_execution,
)
from tests.support import (
    read_jsonl,
)


class LiveTurnHangSentinelIntegrationTests(unittest.TestCase):
    """_LiveTurn + fake sampler end-to-end persistence of hang suspicion."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_dir = Path(tmp.name)
        self.state_path = self.tmp_dir / "state.json"
        self.event_path = self.tmp_dir / "events.jsonl"

    @staticmethod
    def tree(cpu_seconds: float) -> list[dict[str, Any]]:
        return [
            {
                "pid": 4242,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": cpu_seconds,
                "command": "claude --print",
            }
        ]

    def test_suspicion_persists_and_clears_on_activity(self) -> None:
        samples = [self.tree(9.0) for _ in range(4)]

        def sample_tree(pid: int):
            return samples.pop(0)

        sentinel = turn_execution.HangSentinel(
            sample_tree=sample_tree,
            monotonic=time.monotonic,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=45.0,
        )
        started_at = clock.isoformat(clock.utc_now() - timedelta(seconds=600))
        active_turn: dict[str, Any] = {
            "run_id": "run-hang",
            "turn_index": 1,
            "session": 3,
            "session_id": "3",
            "total_sessions": 3,
            "harness": "claude",
            "artifact_dir": str(self.tmp_dir),
            "stdout_path": str(self.tmp_dir / "stdout.log"),
            "stderr_path": str(self.tmp_dir / "stderr.log"),
            "last_message_path": str(self.tmp_dir / "last-message.txt"),
            "result_path": str(self.tmp_dir / "result.json"),
            "started_at": started_at,
            "last_activity_at": started_at,
            "heartbeat_count": 0,
        }
        live_turn = run_commit._LiveTurn(
            payload={"state_path": str(self.state_path)},
            event_path=self.event_path,
            progress=mock.Mock(),
            active_turn=active_turn,
            hang_sentinel=sentinel,
        )
        live_turn.note_harness_pid(4242)

        # Baseline, first confirm, second confirm: suspicion lands on the
        # third heartbeat tick.
        for _ in range(3):
            live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        block = state["active_turn"]["hang_suspected"]
        self.assertEqual(turn_execution.HANG_REASON_ZERO_CPU, block["reason"])
        self.assertEqual(4242, block["harness_pid"])
        self.assertEqual(2, block["confirm_samples"])
        self.assertGreaterEqual(block["idle_seconds"], 120.0)
        self.assertEqual(4242, block["process_tree"][0]["pid"])

        records = read_jsonl(self.event_path)
        suspected = [
            record
            for record in records
            if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
        ]
        self.assertEqual(1, len(suspected))
        self.assertEqual(
            turn_execution.HANG_REASON_ZERO_CPU,
            suspected[0]["payload"]["hang_suspected"]["reason"],
        )

        # A later heartbeat refreshes in place: no duplicate suspected event,
        # and the heartbeat payload carries the block for the status tailer.
        live_turn._record_heartbeat()
        records = read_jsonl(self.event_path)
        suspected = [
            record
            for record in records
            if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
        ]
        self.assertEqual(1, len(suspected))
        heartbeats = [
            record
            for record in records
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertEqual(
            turn_execution.HANG_REASON_ZERO_CPU,
            heartbeats[-1]["payload"]["hang_suspected"]["reason"],
        )

        live_turn.record_activity("stdout", "resumed output")

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("hang_suspected", state["active_turn"])
        records = read_jsonl(self.event_path)
        cleared = [
            record
            for record in records
            if record["name"] == events.TURN_HANG_CLEARED_EVENT
        ]
        self.assertEqual(1, len(cleared))
        self.assertEqual(
            turn_execution.HANG_REASON_ZERO_CPU,
            cleared[0]["payload"]["hang_suspected"]["reason"],
        )

    def _live_turn(
        self, sample_tree, *, result_grace_seconds: float = 45.0
    ) -> run_commit._LiveTurn:
        sentinel = turn_execution.HangSentinel(
            sample_tree=sample_tree,
            monotonic=time.monotonic,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=result_grace_seconds,
        )
        started_at = clock.isoformat(clock.utc_now() - timedelta(seconds=600))
        active_turn: dict[str, Any] = {
            "run_id": "run-hang",
            "turn_index": 1,
            "session": 3,
            "session_id": "3",
            "total_sessions": 3,
            "harness": "claude",
            "artifact_dir": str(self.tmp_dir),
            "stdout_path": str(self.tmp_dir / "stdout.log"),
            "stderr_path": str(self.tmp_dir / "stderr.log"),
            "last_message_path": str(self.tmp_dir / "last-message.txt"),
            "result_path": str(self.tmp_dir / "result.json"),
            "started_at": started_at,
            "last_activity_at": started_at,
            "heartbeat_count": 0,
        }
        return run_commit._LiveTurn(
            payload={"state_path": str(self.state_path)},
            event_path=self.event_path,
            progress=mock.Mock(),
            active_turn=active_turn,
            hang_sentinel=sentinel,
        )

    def test_recovery_report_is_live_in_status_heartbeat_and_hang_event(self) -> None:
        tree = [
            {
                "pid": 4242,
                "ppid": 1,
                "state": "S",
                "cpu_seconds": 9.0,
                "birth": "root",
                "command": "claude --print",
            },
            {
                "pid": 5001,
                "ppid": 4242,
                "state": "S",
                "cpu_seconds": 1.0,
                "birth": "leaf",
                "command": "eza -la",
            },
        ]
        live_turn = self._live_turn(
            lambda pid: [dict(entry) for entry in tree],
            result_grace_seconds=0.0,
        )
        live_turn.note_harness_pid(4242)
        live_turn.note_final_result()
        report = {
            "policy": process_tree.POST_RESULT_RECOVERY_POLICY,
            "attempted_at": "now",
            "signal": "SIGTERM",
            "candidate_pids": [5001],
            "signaled_pids": [],
            "already_gone_pids": [],
            "identity_mismatches": [],
            "signal_failures": [],
        }
        with (
            mock.patch.object(
                process_tree,
                "prepare_verified_leaf_recovery",
                return_value=(report, (5001,)),
            ),
            mock.patch.object(
                process_tree,
                "signal_verified_leaf_recovery",
                return_value={**report, "signaled_pids": [5001]},
            ),
        ):
            live_turn._record_heartbeat()
            live_turn._record_heartbeat()
            heartbeat = live_turn._record_heartbeat()

        recovery = heartbeat["hang_suspected"]["recovery"]
        self.assertEqual([5001], recovery["signaled_pids"])
        state_payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(
            recovery,
            state_payload["active_turn"]["hang_suspected"]["recovery"],
        )
        suspected = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
        ]
        self.assertEqual(
            recovery,
            suspected[0]["payload"]["hang_suspected"]["recovery"],
        )

        # A harness that exits without another output chunk still clears the
        # live episode at turn finalization, preserving the recovery evidence.
        live_turn.finish(clock.utc_now())
        cleared = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HANG_CLEARED_EVENT
        ]
        self.assertEqual(1, len(cleared))
        self.assertEqual(
            recovery,
            cleared[0]["payload"]["hang_suspected"]["recovery"],
        )

    def test_cpu_progress_publishes_without_suspicion_and_clears(self) -> None:
        # A worker burning CPU behind a buffering pipe: silent past the
        # trigger, but its tree keeps advancing.
        samples = [self.tree(cpu) for cpu in (9.0, 12.0, 15.0, 18.0)]
        live_turn = self._live_turn(lambda pid: samples.pop(0))
        live_turn.note_harness_pid(4242)

        for _ in range(3):
            live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        active_turn = state["active_turn"]
        self.assertNotIn("hang_suspected", active_turn)
        progress = active_turn["hang_cpu_progress"]
        self.assertTrue(progress["progressing"])
        self.assertEqual(4242, progress["harness_pid"])
        self.assertEqual(3.0, progress["cpu_delta_seconds"])
        self.assertEqual(0, progress["confirm_samples"])
        self.assertIn("last_checked_at", progress)

        records = read_jsonl(self.event_path)
        self.assertEqual(
            [],
            [
                record
                for record in records
                if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
            ],
        )
        heartbeats = [
            record
            for record in records
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertTrue(
            heartbeats[-1]["payload"]["hang_cpu_progress"]["progressing"],
        )
        self.assertNotIn("hang_suspected", heartbeats[-1]["payload"])

        # Resumed output resets the CPU baseline, so the published sample is
        # no longer fresh and must not linger.
        live_turn.record_activity("stdout", "resumed output")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("hang_cpu_progress", state["active_turn"])

    def test_churning_tree_persists_a_non_decreasing_cpu_total(self) -> None:
        """ASI-58 end to end: worker churn must not persist a hang suspicion."""

        def proc(pid: int, cpu_seconds: float, birth: str) -> dict[str, Any]:
            return {
                "pid": pid,
                "ppid": 1 if pid == 4242 else 4242,
                "state": "S",
                "cpu_seconds": cpu_seconds,
                "birth": birth,
                "command": "python3 -m worker",
            }

        samples = [
            [proc(4242, 9.0, "root"), proc(5001, 6.0, "w1")],
            [proc(4242, 9.2, "root"), proc(5002, 4.0, "w2")],
            [proc(4242, 9.4, "root")],
            [proc(4242, 9.6, "root"), proc(5003, 5.0, "w3")],
        ]
        live_turn = self._live_turn(lambda pid: samples.pop(0))
        live_turn.note_harness_pid(4242)

        for _ in range(4):
            live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        active_turn = state["active_turn"]
        self.assertNotIn("hang_suspected", active_turn)
        progress = active_turn["hang_cpu_progress"]
        self.assertTrue(progress["progressing"])
        self.assertEqual(4, progress["tracked_processes"])
        self.assertEqual(2, progress["sampled_processes"])

        records = read_jsonl(self.event_path)
        self.assertEqual(
            [],
            [
                record
                for record in records
                if record["name"] == events.TURN_HANG_SUSPECTED_EVENT
            ],
        )
        totals = [
            record["payload"]["hang_cpu_progress"]["cpu_total_seconds"]
            for record in records
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertEqual(4, len(totals))
        self.assertEqual(sorted(totals), totals)

    def test_cpu_progress_absent_while_the_turn_is_not_sampled(self) -> None:
        def never_sampled(pid: int):  # pragma: no cover - guard
            raise AssertionError("a busy turn must not be sampled")

        live_turn = self._live_turn(never_sampled)
        # No harness pid: the sentinel cannot sample, so it must publish
        # nothing rather than a stale or invented reading.
        live_turn._record_heartbeat()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("hang_cpu_progress", state["active_turn"])
        heartbeats = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HEARTBEAT_EVENT
        ]
        self.assertNotIn("hang_cpu_progress", heartbeats[-1]["payload"])
