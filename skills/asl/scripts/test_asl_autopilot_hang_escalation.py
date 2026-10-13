#!/usr/bin/env python3
"""Tests for hang escalation policy and supervise_run worker-hung handling."""

from __future__ import annotations

import unittest

from asl_autopilot_test_fakes import FakeTimeoutPopen, _FakeTailer, _HangingPopen
from asl_autopilot_test_payloads import (
    _attach_status,
    _cpu_progress_block,
    _hang_block,
    _hung_attach_status,
    _hung_run_status,
    _hung_running_snapshot,
    _status_payload,
)
from asl_autopilot_test_support import (  # noqa: F401
    _counter_clock,
    _never_run,
    _RunScript,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class HangEscalationPolicyTest(unittest.TestCase):
    """`_HangEscalation` hard bars above the soft stall WARN."""

    def test_result_received_needs_consecutive_probes(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        verdict = policy.observe(_hung_running_snapshot())
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(verdict["probes"], 2)
        self.assertEqual(verdict["idle_seconds"], 162.0)
        self.assertEqual(
            verdict["hang_suspected"]["reason"],
            autopilot.HANG_CLASS_RESULT_RECEIVED,
        )

    def test_retraction_resets_the_probe_count(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        # A tick without the block (turn_hang_cleared, or a heartbeat that
        # dropped it) resets the consecutive count.
        healthy = {"state": "running", "idle_seconds": 3}
        self.assertIsNone(policy.observe(healthy))
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        verdict = policy.observe(_hung_running_snapshot())
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["probes"], 2)

    def test_rearmed_new_episode_resets_the_probe_count(self) -> None:
        # F-001: two result-received blocks with different detected_at values
        # are different suspicion episodes; the second must count as probe 1,
        # not inherit the prior episode's streak.
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(policy.observe(_hung_running_snapshot()))
        rearmed = _hung_running_snapshot()
        rearmed["hang_suspected"]["detected_at"] = "2026-07-24T19:05:30+00:00"
        self.assertIsNone(policy.observe(rearmed))
        verdict = policy.observe(rearmed)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(verdict["probes"], 2)

    def test_idle_ceiling_without_block_is_idle_ceiling_exceeded(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        verdict = policy.observe({"state": "running", "idle_seconds": 950})
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertEqual(verdict["probes"], 0)
        self.assertIsNone(verdict["hang_suspected"])
        self.assertEqual(verdict["idle_seconds"], 950.0)

    def test_idle_ceiling_with_zero_cpu_block_keeps_the_runner_class(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot = _hung_running_snapshot(950, reason=autopilot.HANG_CLASS_ZERO_CPU)
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_ZERO_CPU)

    def test_idle_falls_back_to_the_hang_block_reading(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot = {
            "state": "running",
            "hang_suspected": _hang_block(autopilot.HANG_CLASS_ZERO_CPU, idle=950.0),
        }
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_ZERO_CPU)
        self.assertEqual(verdict["idle_seconds"], 950.0)

    def test_below_the_bars_never_escalates(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        # Zero-cpu suspicion below the ceiling is deliberately conservative:
        # the runner already surfaced it; the helper keeps waiting.
        snapshot = _hung_running_snapshot(300, reason=autopilot.HANG_CLASS_ZERO_CPU)
        for _ in range(5):
            self.assertIsNone(policy.observe(snapshot))

    def test_idle_ceiling_declines_while_the_tree_burns_cpu(self) -> None:
        # The defect this gate fixes: a worker piping output through a
        # buffering `tail` is silent for far longer than the ceiling while
        # its process tree keeps consuming CPU.
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot = {
            "state": "running",
            "idle_seconds": 1200,
            "hang_cpu_progress": _cpu_progress_block(True),
        }
        for _ in range(5):
            self.assertIsNone(policy.observe(snapshot))

    def test_idle_ceiling_escalates_once_progress_stops(self) -> None:
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        self.assertIsNone(
            policy.observe(
                {
                    "state": "running",
                    "idle_seconds": 1200,
                    "hang_cpu_progress": _cpu_progress_block(True),
                }
            )
        )
        verdict = policy.observe(
            {
                "state": "running",
                "idle_seconds": 1200,
                "hang_cpu_progress": _cpu_progress_block(
                    False, delta=0.0, confirm_samples=1
                ),
            }
        )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertEqual(verdict["idle_seconds"], 1200.0)

    def test_idle_ceiling_escalates_when_cpu_evidence_is_unusable(self) -> None:
        # Unknown must stay armed: the runner resets its zero-cpu streak on a
        # failed `ps`, so it can never confirm, and the ceiling is the only
        # remaining backstop.
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        for label, block in (
            ("absent", None),
            ("not-a-dict", "progressing"),
            ("missing-key", {"harness_pid": 4242}),
            ("non-bool", {"progressing": "yes"}),
            ("null", {"progressing": None}),
        ):
            with self.subTest(evidence=label):
                snapshot = {"state": "running", "idle_seconds": 950}
                if block is not None:
                    snapshot["hang_cpu_progress"] = block
                verdict = autopilot._HangEscalation(
                    probes=2, idle_ceiling=900.0
                ).observe(snapshot)
                self.assertIsNotNone(verdict)
                self.assertEqual(verdict["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertIsNone(policy.observe({"state": "running", "idle_seconds": 3}))

    def test_consecutive_probe_bar_stays_ungated_by_cpu_evidence(self) -> None:
        # The runner no longer publishes a result-received block while the
        # tree progresses, so the helper trusts that verdict as-is.
        policy = autopilot._HangEscalation(probes=2, idle_ceiling=900.0)
        snapshot = _hung_running_snapshot()
        snapshot["hang_cpu_progress"] = _cpu_progress_block(True)
        self.assertIsNone(policy.observe(snapshot))
        verdict = policy.observe(snapshot)
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)


class InvokeRunHangEscalationTest(unittest.TestCase):
    """`_invoke_run` returns the `_worker_hung` marker without touching the child."""

    def _parsed(self) -> dict:
        return {"argv": ["asl", "run", "/tmp/plan", "--json"], "plan_dir": "/tmp/plan"}

    def _run(self, *, tailer, status_probe=None, **kwargs):
        beats: list = []
        popen = _HangingPopen()
        payload = autopilot._supervision_session._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=beats.append,
            heartbeat_interval=0.01,
            popen=popen,
            status_probe=status_probe,
            events_tailer=tailer,
            **kwargs,
        )
        return payload, beats, popen

    def test_result_received_probes_escalate_without_killing(self) -> None:
        tailer = _FakeTailer([(_hung_running_snapshot(), False)])
        payload, beats, popen = self._run(
            tailer=tailer,
            status_probe=lambda: _hung_run_status(),
        )
        # _HangingPopen raises from kill/terminate/wait, so reaching here at
        # all proves the child was left alone.
        self.assertTrue(payload["_worker_hung"])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual(payload["reason"], "worker_hung")
        hang = payload["hang"]
        self.assertEqual(hang["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(hang["probes"], 2)
        self.assertEqual(hang["supervised_child_pid"], 4242)
        self.assertTrue(hang["detached"])
        self.assertEqual(hang["plan_dir"], "/tmp/plan")
        self.assertEqual(hang["state_path"], "/tmp/plan/.asl/state.json")
        self.assertEqual(hang["events_path"], "/tmp/plan/.asl/events.jsonl")
        self.assertEqual(
            hang["artifact_paths"],
            {
                "stdout": "/tmp/plan/.asl/turn-stdout.log",
                "stderr": "/tmp/plan/.asl/turn-stderr.log",
                "last_message": "/tmp/plan/.asl/last-message.txt",
                "result": "/tmp/plan/.asl/result.json",
            },
        )
        self.assertEqual(len(hang["next_steps"]), 4)
        # The freshly probed status rides on the marker for supervise_run.
        self.assertEqual(payload["status"]["plan_dir"], "/tmp/plan")
        joined = "\n".join(beats)
        self.assertIn("escalating worker_hung", joined)
        self.assertIn("leaving the `asl run` child alive", joined)

    def test_first_result_received_probe_does_not_escalate(self) -> None:
        # One result-received tick then a healthy tick, then the ordinary
        # timeout loop would continue — cap it with a final clean payload.
        tailer = _FakeTailer(
            [
                (_hung_running_snapshot(), False),
                ({"state": "running", "idle_seconds": 3}, False),
            ]
        )
        popen = FakeTimeoutPopen(
            timeout_count=2,
            stdout='{"ok": true, "paused": false, "reason": "all-complete"}',
        )
        payload = autopilot._supervision_session._invoke_run(
            self._parsed(),
            chunk_index=1,
            clock=_counter_clock(),
            heartbeat=lambda *_: None,
            heartbeat_interval=0.01,
            popen=popen,
            events_tailer=tailer,
        )
        self.assertEqual(payload["reason"], "all-complete")
        self.assertNotIn("_worker_hung", payload)

    def test_idle_ceiling_escalates_without_status_probe(self) -> None:
        tailer = _FakeTailer(
            [({"state": "running", "session_id": "s7", "idle_seconds": 950}, False)]
        )
        payload, _beats, _popen = self._run(tailer=tailer, status_probe=None)
        self.assertTrue(payload["_worker_hung"])
        hang = payload["hang"]
        self.assertEqual(hang["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertEqual(hang["probes"], 0)
        self.assertIsNone(hang["hang_suspected"])
        # No status payload: plan_dir falls back to the parsed command; the
        # path fields degrade to None rather than being omitted.
        self.assertEqual(hang["plan_dir"], "/tmp/plan")
        self.assertIsNone(hang["state_path"])
        self.assertEqual(
            hang["artifact_paths"],
            {"stdout": None, "stderr": None, "last_message": None, "result": None},
        )
        self.assertNotIn("status", payload)

    def test_idle_ceiling_waits_out_a_busy_tree_then_escalates(self) -> None:
        busy = {
            "state": "running",
            "session_id": "s7",
            "idle_seconds": 1200,
            "hang_cpu_progress": _cpu_progress_block(True),
        }
        stopped = dict(busy)
        stopped["hang_cpu_progress"] = _cpu_progress_block(
            False, delta=0.0, confirm_samples=1
        )
        tailer = _FakeTailer([(busy, False), (busy, False), (stopped, False)])
        payload, beats, _popen = self._run(tailer=tailer, status_probe=None)
        self.assertTrue(payload["_worker_hung"])
        self.assertEqual(payload["hang"]["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        joined = "\n".join(beats)
        # Two ceiling-crossing ticks passed as ordinary progress heartbeats
        # before the sample stopped reporting progress.
        self.assertEqual(joined.count("session s7 | idle 1200s"), 3)
        self.assertEqual(joined.count("escalating worker_hung"), 1)


class SuperviseRunWorkerHungTest(unittest.TestCase):
    """`supervise_run` maps hang escalations to `worker_hung` results."""

    PARSED = {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]}

    def test_worker_hung_result_preserves_leaf_recovery_report(self) -> None:
        block = _hang_block()
        block["recovery"] = {
            "policy": "verified_leaf_sigterm",
            "attempted_at": "2026-08-20T12:00:00+00:00",
            "signal": "SIGTERM",
            "candidate_pids": [5001],
            "signaled_pids": [],
            "already_gone_pids": [],
            "identity_mismatches": [{"pid": 5001, "reason": "birth"}],
            "signal_failures": [],
        }
        status = _hung_run_status()
        status["active_turn"]["hang_suspected"] = block
        escalation = {
            "class": autopilot.HANG_CLASS_RESULT_RECEIVED,
            "hang_suspected": block,
            "idle_seconds": 162.0,
            "probes": 2,
        }

        hang = autopilot._hang_payload(escalation, self.PARSED, status)

        self.assertEqual(block["recovery"], hang["hang_suspected"]["recovery"])

    def test_marker_from_chunk_maps_to_worker_hung_result(self) -> None:
        hang = {"class": autopilot.HANG_CLASS_RESULT_RECEIVED, "probes": 2}
        marker_status = {"ok": True, "plan_dir": "/tmp/plan", "marker": True}
        marker = {
            "ok": False,
            "paused": False,
            "reason": "worker_hung",
            "_worker_hung": True,
            "hang": hang,
            "status": marker_status,
        }
        run = _RunScript([marker])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=_StatusScript([_status_payload(1)]),
            run_invoker=run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"], hang)
        # The marker's escalation-time status is fresher than the launch probe.
        self.assertTrue(result["status"]["marker"])
        self.assertEqual(result["chunks"], 1)

    def test_marker_without_status_falls_back_to_the_loop_probe(self) -> None:
        marker = {
            "ok": False,
            "paused": False,
            "reason": "worker_hung",
            "_worker_hung": True,
            "hang": {"class": autopilot.HANG_CLASS_IDLE_CEILING},
        }
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=_StatusScript([_status_payload(1)]),
            run_invoker=_RunScript([marker]),
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["status"]["counts"]["pending"], 1)

    def test_attach_path_escalates_after_consecutive_hung_probes(self) -> None:
        beats: list = []
        status = _StatusScript([_hung_attach_status(), _hung_attach_status()])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=beats.append,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "worker_hung")
        hang = result["hang"]
        self.assertEqual(hang["class"], autopilot.HANG_CLASS_RESULT_RECEIVED)
        self.assertEqual(hang["probes"], 2)
        # Attach mode launched nothing, so there is no child pid to report.
        self.assertNotIn("supervised_child_pid", hang)
        self.assertNotIn("detached", hang)
        self.assertIn("escalating worker_hung", "\n".join(beats))

    def test_attach_probe_from_a_different_episode_resets_the_count(self) -> None:
        # The attach-path policy instance keys on episode identity too: a
        # second probe whose block carries a new detected_at is probe 1 of
        # the re-armed episode, so escalation waits for the third probe.
        first = _hung_attach_status()
        rearmed = _hung_attach_status()
        rearmed["runner_state"]["active_turn"]["hang_suspected"]["detected_at"] = (
            "2026-07-24T19:05:30+00:00"
        )
        status = _StatusScript([first, rearmed, rearmed])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"]["probes"], 2)
        self.assertEqual(status.calls, 3)

    def test_attach_idle_ceiling_escalates_without_a_hang_block(self) -> None:
        status = _StatusScript([_attach_status(950)])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )
        self.assertEqual(result["reason"], "worker_hung")
        self.assertEqual(result["hang"]["class"], autopilot.HANG_CLASS_IDLE_CEILING)
        self.assertIsNone(result["hang"]["hang_suspected"])

    def test_attach_soft_stall_below_the_bars_keeps_waiting(self) -> None:
        # idle 96 crosses the 90s soft WARN but no hard bar: supervision must
        # keep polling and complete normally (soft-stall behavior unchanged).
        beats: list = []
        status = _StatusScript([_attach_status(96), _status_payload(0)])
        result = autopilot.supervise_run(
            dict(self.PARSED),
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=beats.append,
            stall_threshold=90.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        joined = "\n".join(beats)
        self.assertIn("WARN possibly stalled", joined)
        self.assertNotIn("escalating worker_hung", joined)


if __name__ == "__main__":
    unittest.main()
