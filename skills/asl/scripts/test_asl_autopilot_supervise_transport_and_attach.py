#!/usr/bin/env python3
"""Tests for supervise_run transport backoff, harness errors, and attach polling."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from asl_autopilot_test_payloads import (
    _active_lock,
    _attach_status,
    _attach_waiting_status,
    _status_payload,
)
from asl_autopilot_test_support import (  # noqa: F401
    _counter_clock,
    _ManualClock,
    _never_run,
    _RunScript,
    _sequence_clock,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SuperviseRunTransportBackoffTest(unittest.TestCase):
    """Transient transport failures back off on the bounded 5s/30s schedule."""

    def _supervise(self, status_invoker, run_invoker, sleeps, **kwargs):
        return autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status_invoker,
            run_invoker=run_invoker,
            clock=_counter_clock(),
            sleep=lambda seconds: sleeps.append(seconds),
            heartbeat=lambda *_: None,
            **kwargs,
        )

    def _transport_pause(self, *, retry_at=None):
        return {
            "ok": False,
            "paused": True,
            "reason": "transport-failed",
            "pause_reason": "transport-failed",
            "pause_message": "connection reset by peer",
            "pause_details": {
                "failure_classification": {
                    "reason": "transport-failed",
                    "retryable": True,
                    "fingerprint": "transport-failed:connection-reset",
                    "retry_at": retry_at,
                }
            },
        }

    def test_transport_retry_clears_after_first_backoff(self) -> None:
        # First chunk hits a transport pause; after a single 5s backoff the
        # relaunch completes the plan.
        status = _StatusScript(
            [
                _status_payload(1),
                _status_payload(1),
                _status_payload(0),
            ]
        )
        run = _RunScript(
            [
                self._transport_pause(),
                {"ok": True, "paused": False, "reason": "all-complete"},
            ]
        )
        sleeps = []
        result = self._supervise(status, run, sleeps)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1, 2])
        # Exactly one 5s backoff slept before the successful relaunch.
        self.assertEqual(sleeps, [5.0])

    def test_transport_retries_follow_five_then_thirty(self) -> None:
        # The pause never clears: two retries at 5s and 30s, launching three
        # times total, then the exhausted pause is surfaced with recorded delay
        # and classification.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([self._transport_pause(retry_at="2026-07-10T06:00:00Z")])
        sleeps = []
        result = self._supervise(status, run, sleeps, max_retries=2)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(sleeps, [5.0, 30.0])
        retries = result["retries"]
        self.assertEqual(retries["reason"], "transport-failed")
        self.assertEqual(retries["count"], 2)
        self.assertTrue(retries["exhausted"])
        self.assertEqual(retries["delayed_seconds"], 35.0)
        self.assertEqual(retries["classification"]["retry_at"], "2026-07-10T06:00:00Z")

    def test_verify_failed_retry_does_not_back_off(self) -> None:
        # Verification repair retries relaunch immediately (Session 10 revisits
        # them); no backoff sleep is scheduled.
        status = _StatusScript(
            [
                _status_payload(1),
                _status_payload(1),
                _status_payload(0),
            ]
        )
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "verify-failed",
                    "pause_reason": "verify-failed",
                },
                {"ok": True, "paused": False, "reason": "all-complete"},
            ]
        )
        sleeps = []
        result = self._supervise(status, run, sleeps)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(sleeps, [])

    def test_retry_accounting_survives_interleaved_attach(self) -> None:
        # A transport retry, an attach poll (someone else briefly holds the
        # lock), then the same pause again: the retry budget keeps counting from
        # where it left off rather than resetting after the attach.
        status = _StatusScript(
            [
                _status_payload(1),  # chunk 1 -> transport pause
                _status_payload(1, lock=_active_lock()),  # attach wait
                _status_payload(1),  # chunk 2 -> transport pause
                _status_payload(1),  # chunk 3 -> transport pause
            ]
        )
        run = _RunScript([self._transport_pause()])
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status,
            run_invoker=run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            max_retries=2,
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["reason"], "paused")
        # Three launches total; the attach poll did not reset the budget.
        self.assertEqual(run.chunks, [1, 2, 3])
        self.assertEqual(result["retries"]["count"], 2)
        self.assertTrue(result["retries"]["exhausted"])
        self.assertEqual(result["retries"]["delayed_seconds"], 35.0)


class SuperviseRunUserRequiredHarnessTest(unittest.TestCase):
    """Quota/auth/invalid-model pauses surface as user_required, launch once."""

    def _supervise(self, run_payload, *, sleeps=None, **kwargs):
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([run_payload])
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status,
            run_invoker=run,
            clock=_counter_clock(),
            sleep=(sleeps.append if sleeps is not None else (lambda *_: None)),
            heartbeat=lambda *_: None,
            **kwargs,
        )
        return result, run

    def _harness_pause(self, reason, *, retry_at=None):
        return {
            "ok": False,
            "paused": True,
            "reason": reason,
            "pause_reason": reason,
            "pause_message": f"session paused: {reason}",
            "pause_details": {
                "failure_classification": {
                    "reason": reason,
                    "retryable": False,
                    "retry_at": retry_at,
                }
            },
        }

    def test_quota_exhaustion_is_user_required_and_launches_once(self) -> None:
        sleeps = []
        result, run = self._supervise(
            self._harness_pause("quota-exhausted", retry_at="2026-07-10T06:00:00Z"),
            sleeps=sleeps,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "user_required")
        # Launched exactly once, never retried, never backed off.
        self.assertEqual(run.chunks, [1])
        self.assertEqual(sleeps, [])
        retries = result["retries"]
        self.assertEqual(retries["reason"], "quota-exhausted")
        self.assertFalse(retries["exhausted"])
        self.assertEqual(retries["delayed_seconds"], 0.0)
        # Reset information travels with the result for the operator.
        self.assertEqual(retries["classification"]["retry_at"], "2026-07-10T06:00:00Z")

    def test_auth_and_invalid_model_are_user_required(self) -> None:
        for reason in ("auth-failed", "invalid-model"):
            result, run = self._supervise(self._harness_pause(reason))
            self.assertEqual(result["reason"], "user_required", reason)
            self.assertEqual(run.chunks, [1], reason)

    def test_quota_pause_without_flag_carries_no_quota_wait_block(self) -> None:
        # Default-off: the surfaced payload is exactly what it was before the
        # opt-in existed.
        result, run = self._supervise(
            self._harness_pause("quota-exhausted", retry_at="3600")
        )
        self.assertEqual(result["reason"], "user_required")
        self.assertEqual(run.chunks, [1])
        self.assertNotIn("quota_wait", result)


class SuperviseRunAttachHeartbeatTest(unittest.TestCase):
    """The attach-wait heartbeat surfaces live session/idle detail + stall WARN."""

    def _supervise(self, status_invoker, **kwargs):
        beats: list = []
        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status_invoker,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=beats.append,
            **kwargs,
        )
        return result, beats

    def test_attach_heartbeat_carries_session_and_idle(self) -> None:
        status = _StatusScript([_attach_status(34), _status_payload(0)])
        result, beats = self._supervise(status)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        joined = "\n".join(beats)
        self.assertIn("attached to active run", joined)
        self.assertIn("session tidy-db-writes", joined)
        self.assertIn("(3/8)", joined)
        self.assertIn("idle 34s", joined)
        self.assertNotIn("WARN", joined)

    def test_attach_heartbeat_warns_on_high_idle(self) -> None:
        status = _StatusScript([_attach_status(96), _status_payload(0)])
        result, beats = self._supervise(status, stall_threshold=90.0)
        self.assertTrue(result["ok"], result)
        joined = "\n".join(beats)
        self.assertIn("WARN possibly stalled", joined)
        self.assertIn("idle 96s (no output)", joined)
        # The pending/elapsed context is preserved alongside the warning.
        self.assertIn("pending 1", joined)

    def test_waiting_for_reply_is_healthy_and_reports_pending_outbox(self) -> None:
        # Older ``asl status`` payloads do not carry
        # ``waiting_for_reply_status``; retain their deadline-only behavior.
        waiting = _attach_waiting_status(pending_outbox=1)
        snapshot = autopilot._status_snapshot(waiting)
        self.assertEqual(snapshot["state"], "waiting_for_reply")
        self.assertEqual(snapshot["pending_outbox"], 1)
        self.assertNotIn("waiting_for_reply_status", snapshot)

        status = _StatusScript([waiting, _status_payload(0)])
        result, beats = self._supervise(status, stall_threshold=0.0)
        self.assertTrue(result["ok"], result)
        joined = "\n".join(beats)
        self.assertIn("waiting for reply for session 12", joined)
        self.assertIn("pending outbox 1", joined)
        self.assertNotIn("WARN possibly stalled", joined)

    def test_expired_wait_status_overrides_future_deadline_and_warns(self) -> None:
        waiting = _attach_waiting_status()
        waiting["runner_state"]["waiting_for_reply_status"] = "expired"
        waiting["waiting_for_reply_status"] = "expired"

        snapshot = autopilot._status_snapshot(waiting)
        self.assertEqual(snapshot["waiting_for_reply_status"], "expired")
        is_stall, detail = autopilot._heartbeat_detail(
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 0, tzinfo=UTC),
        )

        self.assertTrue(is_stall)
        self.assertIn("wait status expired", detail)
        heartbeat = autopilot._format_heartbeat(
            1,
            10.0,
            snapshot,
            90.0,
            events_stale=False,
            now=datetime(2026, 7, 24, 19, 0, tzinfo=UTC),
        )
        self.assertIn("WARN possibly stalled", heartbeat)

    def test_a_finalizing_run_is_named_and_never_escalated_as_idle(self) -> None:
        # Between the last turn and `run_finished` the run holds the lock with
        # no turn and no verification in flight. The phase is what keeps that
        # from reading as an unexplained silence.
        finalizing = _status_payload(1, lock=_active_lock())
        finalizing["runner_state"] = {
            "present": True,
            "paused": False,
            "active_turn": None,
            "active_verification": None,
            "active_finalization": {
                "phase": "assurance-audit",
                "elapsed_seconds": 240.0,
                "cancellable": True,
            },
        }

        snapshot = autopilot._status_snapshot(finalizing)

        self.assertEqual(snapshot["state"], "finalizing")
        self.assertEqual(snapshot["phase"], "assurance-audit")
        self.assertTrue(snapshot["cancellable"])
        # No idle reading, so the hang bars have nothing to count against.
        self.assertIsNone(autopilot._snapshot_idle_seconds(snapshot, None))
        is_stall, detail = autopilot._heartbeat_detail(
            snapshot, 90.0, events_stale=False
        )
        self.assertFalse(is_stall)
        self.assertIn("finalizing (assurance-audit)", detail)
        self.assertIn("240s elapsed", detail)

    def test_terminal_pause_takes_precedence_over_stale_wait_record(self) -> None:
        paused = _attach_waiting_status()
        paused["runner_state"]["paused"] = True
        paused["runner_state"]["reason"] = "needs-agent-reply"
        snapshot = autopilot._status_snapshot(paused)
        self.assertEqual(snapshot["state"], "paused")
        self.assertEqual(snapshot["reason"], "needs-agent-reply")

    def test_wait_fallback_keeps_the_existing_terminal_pause_reason(self) -> None:
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "blocked",
                    "pause_reason": "blocked",
                    "pause_message": "reply wait expired",
                }
            ]
        )
        result = autopilot.supervise_run(
            {
                "plan_dir": "/tmp/plan",
                "argv": ["asl", "run", "/tmp/plan", "--json"],
            },
            status_invoker=_StatusScript(
                [_attach_waiting_status(), _status_payload(1)]
            ),
            run_invoker=run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["run"]["pause_reason"], "blocked")
        self.assertNotEqual(result["reason"], "needs_supervisor_answer")


class AttachEventsWaitTest(unittest.TestCase):
    """`_attach_events_wait` watches events.jsonl by os.stat between probes."""

    def test_no_events_path_degrades_to_single_poll(self) -> None:
        sleeps: list = []
        autopilot._attach_events_wait(
            None,
            poll_interval=5.0,
            heartbeat_interval=30.0,
            clock=lambda: 0.0,
            sleep=sleeps.append,
            events_stat=lambda _p: None,
        )
        # No file to watch -> exactly one legacy poll_interval sleep, no clock use.
        self.assertEqual(sleeps, [5.0])

    def test_returns_early_when_events_file_grows(self) -> None:
        sizes = iter([100, 140])
        sleeps: list = []
        autopilot._attach_events_wait(
            "events.jsonl",
            poll_interval=1.0,
            heartbeat_interval=30.0,
            clock=lambda: 0.0,
            sleep=sleeps.append,
            events_stat=lambda _p: next(sizes),
        )
        # Growth (140 != baseline 100) breaks the wait after a single slice.
        self.assertEqual(sleeps, [1.0])

    def test_returns_when_heartbeat_interval_elapses(self) -> None:
        clock = _sequence_clock([0.0, 31.0])
        sleeps: list = []
        autopilot._attach_events_wait(
            "events.jsonl",
            poll_interval=1.0,
            heartbeat_interval=30.0,
            clock=clock,
            sleep=sleeps.append,
            events_stat=lambda _p: 100,
        )
        # Size never changes; the wait ends once the heartbeat interval elapses.
        self.assertEqual(sleeps, [1.0])

    def test_quiet_events_file_still_services_mailbox_deadlines(self) -> None:
        clock = _ManualClock()
        polls = []

        class Watcher:
            def __init__(self):
                self.next_at = 5.0

            def next_deadline(self):
                return self.next_at

            def poll(self, *, now=None):
                polls.append(now)
                self.next_at = now + 5.0
                return ()

        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            clock.advance(seconds)

        autopilot._attach_events_wait(
            "events.jsonl",
            poll_interval=10.0,
            heartbeat_interval=30.0,
            clock=clock,
            sleep=advance,
            events_stat=lambda _p: 100,
            mailbox_watcher=Watcher(),
            heartbeat=lambda *_: None,
        )

        self.assertEqual(sleeps, [5.0] * 6)
        self.assertEqual(polls, [5.0, 10.0, 15.0, 20.0, 25.0, 30.0])


class SuperviseRunAttachEventsStatTest(unittest.TestCase):
    """Attach mode consults the events_stat seam on the payload's events_path."""

    def test_attach_wait_stats_the_events_path(self) -> None:
        attach = _attach_status(20)
        attach["events_path"] = "/tmp/plan/events.jsonl"
        status = _StatusScript([attach, attach, _status_payload(0)])
        stat_paths: list = []

        def events_stat(path):
            stat_paths.append(path)
            return 100

        result = autopilot.supervise_run(
            {"plan_dir": "/tmp/plan", "argv": ["asl", "run", "/tmp/plan", "--json"]},
            status_invoker=status,
            run_invoker=_never_run,
            clock=_counter_clock(),
            sleep=lambda *_: None,
            heartbeat=lambda *_: None,
            events_stat=events_stat,
            heartbeat_interval=3.0,
            poll_interval=1.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # The attach waits watched the advertised events path by os.stat.
        self.assertTrue(stat_paths)
        self.assertTrue(all(p == "/tmp/plan/events.jsonl" for p in stat_paths))


if __name__ == "__main__":
    unittest.main()
