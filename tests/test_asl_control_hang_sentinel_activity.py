"""Hang sentinel activity notes, input waits, and stale assessments.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    run_commit,
    turn_execution_constants,
    turn_execution_hang_sentinel,
    turn_execution_protocols,
)
from tests.asl_control_hang_sentinel_support import (
    HangSentinelTestsBase,
    _AfterEvaluateSentinel,
)
from tests.asl_control_support import live_active_turn
from tests.support import (
    read_jsonl,
)


class HangSentinelTestsPart2(HangSentinelTestsBase):
    def test_cpu_progress_publishes_the_accounting_evidence(self) -> None:
        sentinel, _calls = self.make_sentinel(
            [
                [
                    self.proc(123, 10.0, birth="root", ppid=1),
                    self.proc(200, 2.0, birth="w1"),
                ],
                [self.proc(123, 10.0, birth="root", ppid=1)],
            ]
        )
        sentinel.note_harness_pid(123)

        first = sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        assert first.cpu_progress is not None
        self.assertAlmostEqual(12.0, first.cpu_progress["cpu_total_seconds"])
        self.assertEqual(2, first.cpu_progress["tracked_processes"])
        self.assertEqual(2, first.cpu_progress["sampled_processes"])

        second = sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        progress = second.cpu_progress
        assert progress is not None
        self.assertAlmostEqual(12.0, progress["cpu_total_seconds"])
        self.assertEqual(0.0, progress["cpu_delta_seconds"])
        self.assertEqual(2, progress["tracked_processes"])
        self.assertEqual(1, progress["sampled_processes"])

    def test_note_activity_retracts_and_resets_counters(self) -> None:
        sentinel, _calls = self.make_sentinel(
            [
                self.tree(5.0),
                self.tree(5.0),
                self.tree(5.0),
                self.tree(5.0),
                self.tree(5.0),
                self.tree(5.0),
            ]
        )
        sentinel.note_harness_pid(123)
        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        suspected = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        self.assertEqual("suspected", suspected.transition)
        # The heartbeat fold announces the episode before persisting it;
        # only announced episodes are handed back for a cleared event.
        self.assertTrue(sentinel.confirm_assessment(suspected))

        retracted = sentinel.note_activity()
        assert retracted is not None
        self.assertEqual(
            turn_execution_constants.HANG_REASON_ZERO_CPU, retracted["reason"]
        )
        self.assertIsNone(sentinel.note_activity())

        # Counters restart: a fresh baseline plus two confirming samples are
        # required before suspicion returns.
        self.assertIsNone(sentinel.evaluate(idle_seconds=210.0, now=self.NOW).suspected)
        self.assertIsNone(sentinel.evaluate(idle_seconds=240.0, now=self.NOW).suspected)
        self.assertEqual(
            "suspected",
            sentinel.evaluate(idle_seconds=270.0, now=self.NOW).transition,
        )

    def test_new_request_invalidates_a_prior_terminal_result(self) -> None:
        monotonic = {"now": 0.0}
        sentinel = turn_execution_hang_sentinel.HangSentinel(
            sample_tree=lambda pid: self.tree(5.0),
            monotonic=lambda: monotonic["now"],
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=45.0,
        )
        sentinel.note_harness_pid(123)
        sentinel.note_final_result()
        sentinel.note_activity(request_started=True)
        monotonic["now"] = 100.0

        assessment = sentinel.evaluate(idle_seconds=30.0, now=self.NOW)

        self.assertIsNone(assessment.suspected)
        self.assertIsNone(assessment.cpu_progress)

    def test_worker_input_wait_suppresses_sampling_and_clears_a_suspicion(
        self,
    ) -> None:
        sentinel, calls = self.make_sentinel(
            [self.tree(5.0), self.tree(5.0), self.tree(5.0)]
        )
        sentinel.note_harness_pid(123)
        sentinel.evaluate(idle_seconds=120.0, now=self.NOW)
        sentinel.evaluate(idle_seconds=150.0, now=self.NOW)
        suspected = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        self.assertTrue(sentinel.confirm_assessment(suspected))

        waiting = sentinel.evaluate(
            idle_seconds=210.0,
            now=self.NOW,
            input_wait_active=True,
        )

        self.assertTrue(waiting.input_wait_active)
        self.assertEqual("cleared", waiting.transition)
        self.assertIsNone(waiting.suspected)
        self.assertEqual([123, 123, 123], calls)

    def test_live_turn_projects_an_input_wait_without_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            calls: list[int] = []

            def sample_tree(pid: int) -> list[dict[str, Any]]:
                calls.append(pid)
                return self.tree(5.0)

            sentinel = turn_execution_hang_sentinel.HangSentinel(
                sample_tree=sample_tree,
                monotonic=lambda: 1000.0,
                trigger_idle_seconds=0.0,
                confirm_samples=2,
                cpu_epsilon_seconds=0.5,
                result_grace_seconds=0.0,
            )
            live_turn = run_commit._LiveTurn(
                payload={"state_path": str(tmp_dir / "state.json")},
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=live_active_turn(),
                hang_sentinel=sentinel,
                input_wait_probe=lambda: True,
            )
            live_turn.note_harness_pid(123)

            with mock.patch.object(turn_execution_protocols, "notify_turn_heartbeat"):
                heartbeat = live_turn._record_heartbeat()

            self.assertTrue(heartbeat["input_wait_active"])
            self.assertEqual([], calls)
            state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(state["active_turn"]["input_wait_active"])

    def test_note_activity_during_sample_discards_the_assessment(self) -> None:
        sample_calls = {"count": 0}
        holder: list[turn_execution_hang_sentinel.HangSentinel] = []

        def sample_tree(pid: int):
            sample_calls["count"] += 1
            if sample_calls["count"] == 3:
                # Output resumes while the confirming tick's ps sample is
                # still in flight.
                holder[0].note_activity()
            return self.tree(5.0)

        sentinel = turn_execution_hang_sentinel.HangSentinel(
            sample_tree=sample_tree,
            monotonic=lambda: 0.0,
            trigger_idle_seconds=120.0,
            confirm_samples=2,
            cpu_epsilon_seconds=0.5,
            result_grace_seconds=45.0,
        )
        holder.append(sentinel)
        sentinel.note_harness_pid(123)

        self.assertIsNone(sentinel.evaluate(idle_seconds=120.0, now=self.NOW).suspected)
        self.assertIsNone(sentinel.evaluate(idle_seconds=150.0, now=self.NOW).suspected)
        racing = sentinel.evaluate(idle_seconds=180.0, now=self.NOW)
        self.assertIsNone(racing.suspected)
        self.assertIsNone(racing.transition)

        # The retraction reset the counters: a fresh baseline plus two
        # confirming samples are required before suspicion returns.
        self.assertIsNone(sentinel.evaluate(idle_seconds=210.0, now=self.NOW).suspected)
        self.assertIsNone(sentinel.evaluate(idle_seconds=240.0, now=self.NOW).suspected)
        self.assertEqual(
            "suspected",
            sentinel.evaluate(idle_seconds=270.0, now=self.NOW).transition,
        )

    def test_output_between_evaluate_and_heartbeat_fold_discards_stale_assessment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            clock = {"now": 0.0}
            sentinel = _AfterEvaluateSentinel(
                sample_tree=lambda pid: HangSentinelTestsBase.tree(5.0),
                monotonic=lambda: clock["now"],
                trigger_idle_seconds=120.0,
                confirm_samples=2,
                cpu_epsilon_seconds=0.5,
                result_grace_seconds=45.0,
            )
            live_turn = run_commit._LiveTurn(
                payload={"state_path": str(tmp_dir / "state.json")},
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=live_active_turn(),
                hang_sentinel=sentinel,
            )
            live_turn.note_harness_pid(123)
            live_turn.note_final_result()
            clock["now"] = 100.0  # past the 45s result grace

            with (
                mock.patch.object(turn_execution_protocols, "notify_turn_heartbeat"),
                mock.patch.object(
                    turn_execution_protocols, "notify_turn_hang_suspected"
                ) as suspected_notify,
                mock.patch.object(
                    turn_execution_protocols, "notify_turn_hang_cleared"
                ) as cleared_notify,
            ):
                # Two warm-up ticks bring the zero-CPU streak up to the
                # confirmation a suspicion now requires; the third would
                # suspect if output had not resumed mid-flight.
                live_turn._record_heartbeat()
                live_turn._record_heartbeat()
                sentinel.after_evaluate = lambda: live_turn.record_activity(
                    "stdout", "resumed output"
                )
                heartbeat = live_turn._record_heartbeat()

            # The verdict sampled a pre-activity generation, so the fold
            # discards it instead of resurrecting it after the output.
            self.assertNotIn("hang_suspected", heartbeat)
            self.assertEqual(0.0, heartbeat["idle_seconds"])
            state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("hang_suspected", state["active_turn"])
            names = [record["name"] for record in read_jsonl(tmp_dir / "events.jsonl")]
            # No suspected event — and no orphan cleared for an episode that
            # was never announced.
            self.assertEqual([events.TURN_HEARTBEAT_EVENT] * 3, names)
            suspected_notify.assert_not_called()
            cleared_notify.assert_not_called()

    def test_activity_during_heartbeat_refresh_keeps_cleared_after_suspected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            clock = {"now": 0.0}
            sentinel = _AfterEvaluateSentinel(
                sample_tree=lambda pid: HangSentinelTestsBase.tree(5.0),
                monotonic=lambda: clock["now"],
                trigger_idle_seconds=120.0,
                confirm_samples=2,
                cpu_epsilon_seconds=0.5,
                result_grace_seconds=45.0,
            )
            live_turn = run_commit._LiveTurn(
                payload={"state_path": str(tmp_dir / "state.json")},
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=live_active_turn(),
                hang_sentinel=sentinel,
            )
            live_turn.note_harness_pid(123)
            live_turn.note_final_result()
            clock["now"] = 100.0

            with (
                mock.patch.object(turn_execution_protocols, "notify_turn_heartbeat"),
                mock.patch.object(
                    turn_execution_protocols, "notify_turn_hang_suspected"
                ) as suspected_notify,
                mock.patch.object(
                    turn_execution_protocols, "notify_turn_hang_cleared"
                ) as cleared_notify,
            ):
                # Ticks 1-2 confirm the flat CPU streak; tick 3 announces.
                live_turn._record_heartbeat()
                live_turn._record_heartbeat()
                live_turn._record_heartbeat()
                # The next refresh verdict goes stale when output resumes
                # between evaluate() and the fold.
                sentinel.after_evaluate = lambda: live_turn.record_activity(
                    "stdout", "resumed output"
                )
                live_turn._record_heartbeat()

            records = read_jsonl(tmp_dir / "events.jsonl")
            names = [record["name"] for record in records]
            self.assertEqual(1, names.count(events.TURN_HANG_SUSPECTED_EVENT))
            self.assertEqual(1, names.count(events.TURN_HANG_CLEARED_EVENT))
            # The clear is final: nothing suspected is emitted after it.
            self.assertLess(
                names.index(events.TURN_HANG_SUSPECTED_EVENT),
                names.index(events.TURN_HANG_CLEARED_EVENT),
            )
            cleared_payload = records[names.index(events.TURN_HANG_CLEARED_EVENT)][
                "payload"
            ]
            # The cleared event carries the turn's fresh idle reading; the
            # stale pre-clear value stays confined to the retracted block.
            self.assertEqual(0.0, cleared_payload["idle_seconds"])
            self.assertGreater(cleared_payload["hang_suspected"]["idle_seconds"], 0.0)
            state = json.loads((tmp_dir / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("hang_suspected", state["active_turn"])
            last_heartbeat = [
                record
                for record in records
                if record["name"] == events.TURN_HEARTBEAT_EVENT
            ][-1]
            self.assertNotIn("hang_suspected", last_heartbeat["payload"])
            self.assertEqual(1, suspected_notify.call_count)
            self.assertEqual(1, cleared_notify.call_count)

    def test_stop_set_skips_sentinel_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            calls: list[int] = []

            def sample_tree(pid: int):
                calls.append(pid)
                return self.tree(1.0)

            sentinel = turn_execution_hang_sentinel.HangSentinel(
                sample_tree=sample_tree,
                monotonic=time.monotonic,
                trigger_idle_seconds=0.0,
                confirm_samples=2,
                cpu_epsilon_seconds=0.5,
                result_grace_seconds=45.0,
            )
            live_turn = run_commit._LiveTurn(
                payload={"state_path": str(tmp_dir / "state.json")},
                event_path=tmp_dir / "events.jsonl",
                progress=mock.Mock(),
                active_turn=live_active_turn(),
                hang_sentinel=sentinel,
            )
            live_turn.note_harness_pid(123)

            live_turn._stop.set()
            heartbeat = live_turn._record_heartbeat()
            self.assertEqual([], calls)
            self.assertNotIn("hang_suspected", heartbeat)

            live_turn._stop.clear()
            live_turn._record_heartbeat()
            self.assertEqual([123], calls)


if __name__ == "__main__":
    unittest.main()
