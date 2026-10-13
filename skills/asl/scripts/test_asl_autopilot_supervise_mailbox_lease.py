#!/usr/bin/env python3
"""Tests for supervise_run mailbox lease handling and lock classification."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from asl_autopilot_test_bases import SuperviseRunInjectedBase
from asl_autopilot_test_payloads import _active_lock, _status_payload
from asl_autopilot_test_support import (  # noqa: F401
    _counter_clock,
    _ManualClock,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SuperviseRunMailboxLeaseTest(SuperviseRunInjectedBase):
    def test_default_run_invoker_forwards_status_probe_and_stall_threshold(
        self,
    ) -> None:
        # With no injected run_invoker, the default closure must thread the
        # status probe (reusing the status_invoker seam) and stall_threshold
        # into `_invoke_run`.
        calls = {"status": 0}

        def status_invoker(parsed):
            calls["status"] += 1
            # First poll -> pending (start a chunk); afterwards -> complete.
            return _status_payload(0) if calls["status"] > 1 else _status_payload(1)

        captured: dict = {}

        def fake_invoke_run(target, **kwargs):
            captured["target"] = target
            captured["kwargs"] = kwargs
            return {"ok": True, "paused": False, "reason": "all-complete"}

        original = autopilot._supervision_session._invoke_run
        autopilot._supervision_session._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": "/tmp/plan",
                    "argv": ["asl", "run", "/tmp/plan", "--json"],
                },
                status_invoker=status_invoker,
                clock=_counter_clock(),
                sleep=lambda *_: None,
                heartbeat=lambda *_: None,
                stall_threshold=42.0,
            )
        finally:
            autopilot._supervision_session._invoke_run = original

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        kwargs = captured["kwargs"]
        self.assertEqual(kwargs["stall_threshold"], 42.0)
        self.assertTrue(callable(kwargs["status_probe"]))
        # The probe reuses the status_invoker seam: calling it re-polls status.
        before = calls["status"]
        probe_payload = kwargs["status_probe"]()
        self.assertEqual(calls["status"], before + 1)
        self.assertTrue(probe_payload["ok"])

    def test_default_run_establishes_mailbox_lease_before_child_launch(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-prelaunch-lease-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan_dir = os.path.join(tmp, "plan")
        state_root = os.path.join(tmp, "state")
        os.makedirs(plan_dir)
        mailbox_path = autopilot._mailbox.mailbox_path_for_plan(plan_dir, state_root)
        lease_path = autopilot._mailbox.lease_path_for_plan(plan_dir, state_root)

        initial = _status_payload(1)
        initial.update(
            {
                "plan_dir": plan_dir,
                "state_dir": str(lease_path.parent),
                "mailbox": {
                    "path": str(mailbox_path),
                    "lease_path": str(lease_path),
                    "last_seq": 0,
                },
            }
        )
        terminal = dict(initial)
        terminal["counts"] = {"pending": 0, "complete": 1}
        terminal["next_pending_id"] = None
        status = _StatusScript([initial, terminal])
        observed = {}

        def fake_invoke_run(target, **kwargs):
            observed["lease_present_at_launch"] = lease_path.exists()
            observed["watcher"] = kwargs["mailbox_watcher"]
            observed["watcher_enabled_at_launch"] = kwargs["mailbox_watcher"].enabled
            return {"ok": True, "paused": False, "reason": "all-complete"}

        original = autopilot._supervision_session._invoke_run
        autopilot._supervision_session._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": plan_dir,
                    "state_dir": state_root,
                    "argv": [
                        "asl",
                        "run",
                        plan_dir,
                        "--state-dir",
                        state_root,
                        "--json",
                    ],
                },
                status_invoker=status,
                clock=_counter_clock(),
                heartbeat=lambda *_: None,
            )
        finally:
            autopilot._supervision_session._invoke_run = original

        self.assertTrue(result["ok"], result)
        self.assertTrue(observed["lease_present_at_launch"])
        self.assertTrue(observed["watcher_enabled_at_launch"])
        self.assertFalse(observed["watcher"].enabled)
        self.assertEqual(result["outbox"]["questions"], [])

    def test_relaunch_after_expired_lease_refreshes_before_next_chunk(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-relaunch-lease-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan_dir = os.path.join(tmp, "plan")
        state_root = os.path.join(tmp, "state")
        os.makedirs(plan_dir)
        mailbox_path = autopilot._mailbox.mailbox_path_for_plan(plan_dir, state_root)
        lease_path = autopilot._mailbox.lease_path_for_plan(plan_dir, state_root)
        clock = _ManualClock()
        wall_start = datetime(2026, 7, 24, tzinfo=UTC)
        lease_writes = []
        launch_times = []
        activation_at_launch = []

        initial = _status_payload(1)
        initial.update(
            {
                "plan_dir": plan_dir,
                "state_dir": str(lease_path.parent),
                "mailbox": {
                    "path": str(mailbox_path),
                    "lease_path": str(lease_path),
                    "last_seq": 0,
                },
            }
        )
        terminal = dict(initial)
        terminal["counts"] = {"pending": 0, "complete": 1}
        terminal["next_pending_id"] = None
        status = _StatusScript([initial, initial, terminal])

        def write_lease(plan, supervisor_id, *, state_dir=None):
            lease_writes.append(clock())
            updated_at = wall_start + timedelta(seconds=clock())
            return autopilot._mailbox.write_lease(
                plan,
                supervisor_id,
                state_dir=state_dir,
                updated_at=updated_at.isoformat(),
            )

        def watcher_factory(status_payload, **kwargs):
            return autopilot._MailboxWatcher.from_status(
                status_payload,
                **kwargs,
                write_lease=write_lease,
                supervisor_id="00000000-0000-0000-0000-000000000099",
            )

        def run_invoker(_parsed, chunk_index):
            launch_times.append(clock())
            now = wall_start + timedelta(seconds=clock())
            activation_at_launch.append(
                autopilot._mailbox.read_lease(
                    plan_dir,
                    state_dir=state_root,
                    now=now,
                ).fresh
            )
            if chunk_index == 1:
                clock.advance(autopilot._mailbox.MAILBOX_LEASE_FRESH_SECONDS + 1.0)
                return {
                    "ok": False,
                    "paused": True,
                    "reason": "transport-failed",
                    "pause_reason": "transport-failed",
                }
            return {"ok": True, "paused": False, "reason": "all-complete"}

        result = autopilot.supervise_run(
            {
                "plan_dir": plan_dir,
                "state_dir": state_root,
                "argv": [
                    "asl",
                    "run",
                    plan_dir,
                    "--state-dir",
                    state_root,
                    "--json",
                ],
            },
            status_invoker=status,
            run_invoker=run_invoker,
            clock=clock,
            sleep=clock.advance,
            heartbeat=lambda *_: None,
            mailbox_watcher_factory=watcher_factory,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertFalse(mailbox_path.exists())
        self.assertEqual(activation_at_launch, [True, True])
        self.assertEqual(lease_writes, launch_times)
        self.assertGreater(
            launch_times[1] - launch_times[0],
            autopilot._mailbox.MAILBOX_LEASE_FRESH_SECONDS,
        )

    def test_no_mailbox_skips_watcher_and_lease_write(self) -> None:
        tmp = tempfile.mkdtemp(prefix="asl-autopilot-no-mailbox-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        plan_dir = os.path.join(tmp, "plan")
        state_root = os.path.join(tmp, "state")
        os.makedirs(plan_dir)
        mailbox_path = autopilot._mailbox.mailbox_path_for_plan(plan_dir, state_root)
        lease_path = autopilot._mailbox.lease_path_for_plan(plan_dir, state_root)
        initial = _status_payload(1)
        initial.update(
            {
                "plan_dir": plan_dir,
                "state_dir": str(lease_path.parent),
                "mailbox": {
                    "path": str(mailbox_path),
                    "lease_path": str(lease_path),
                    "last_seq": 0,
                },
            }
        )
        terminal = dict(initial)
        terminal["counts"] = {"pending": 0, "complete": 1}
        terminal["next_pending_id"] = None
        status = _StatusScript([initial, terminal])
        observed = {}

        def fake_invoke_run(target, **kwargs):
            observed["watcher"] = kwargs["mailbox_watcher"]
            observed["lease_present_at_launch"] = lease_path.exists()
            return {"ok": True, "paused": False, "reason": "all-complete"}

        original = autopilot._supervision_session._invoke_run
        autopilot._supervision_session._invoke_run = fake_invoke_run
        try:
            result = autopilot.supervise_run(
                {
                    "plan_dir": plan_dir,
                    "state_dir": state_root,
                    "argv": [
                        "asl",
                        "run",
                        plan_dir,
                        "--state-dir",
                        state_root,
                        "--no-mailbox",
                        "--json",
                    ],
                    "no_mailbox": True,
                },
                status_invoker=status,
                clock=_counter_clock(),
                heartbeat=lambda *_: None,
            )
        finally:
            autopilot._supervision_session._invoke_run = original

        self.assertTrue(result["ok"], result)
        self.assertIsNone(observed["watcher"])
        self.assertFalse(observed["lease_present_at_launch"])
        self.assertNotIn("outbox", result)


class ClassifyLockTest(unittest.TestCase):
    def _classify(self, parsed_extra, status):
        parsed = {"plan_dir": "/tmp/plan"}
        parsed.update(parsed_extra)
        return autopilot._classify_lock(parsed, status)

    def test_no_lock_is_clear(self) -> None:
        self.assertEqual(self._classify({}, _status_payload(1)), "clear")

    def test_present_but_inactive_is_clear(self) -> None:
        status = _status_payload(
            1, lock={"present": True, "active": False, "stale": False}
        )
        self.assertEqual(self._classify({}, status), "clear")

    def test_active_stale_lock_is_stale(self) -> None:
        status = _status_payload(1, lock=_active_lock(stale=True))
        self.assertEqual(self._classify({}, status), "stale")

    def test_active_matching_lock_is_attach(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        self.assertEqual(self._classify({}, status), "attach")

    def test_active_different_plan_is_mismatch(self) -> None:
        status = _status_payload(1, lock=_active_lock(plan_dir="/other/plan"))
        self.assertEqual(self._classify({}, status), "mismatch")

    def test_harness_mismatch_is_mismatch(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        status["runner_state"] = {"active_turn": {"harness": "claude"}}
        self.assertEqual(self._classify({"harness": "codex"}, status), "mismatch")

    def test_harness_match_is_attach(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        status["runner_state"] = {"active_turn": {"harness": "claude"}}
        self.assertEqual(self._classify({"harness": "claude"}, status), "attach")

    def test_unobservable_harness_is_attach(self) -> None:
        status = _status_payload(1, lock=_active_lock())
        self.assertEqual(self._classify({"harness": "claude"}, status), "attach")

    def _turn_status(self, **active_turn):
        status = _status_payload(1, lock=_active_lock())
        status["runner_state"] = {"active_turn": active_turn}
        return status

    def test_review_turn_matching_review_harness_is_attach(self) -> None:
        # A dual-harness run alternates harnesses; a review turn must be
        # measured against `--review-harness`, not `--harness`.
        status = self._turn_status(harness="codex", session_kind="review")
        self.assertEqual(
            self._classify({"harness": "claude", "review_harness": "codex"}, status),
            "attach",
        )

    def test_review_turn_on_a_third_harness_is_mismatch(self) -> None:
        status = self._turn_status(harness="copilot", session_kind="review")
        self.assertEqual(
            self._classify({"harness": "claude", "review_harness": "codex"}, status),
            "mismatch",
        )

    def test_implementation_turn_is_measured_against_harness(self) -> None:
        status = self._turn_status(harness="claude", session_kind="implementation")
        parsed = {"harness": "claude", "review_harness": "codex"}
        self.assertEqual(self._classify(parsed, status), "attach")

        status = self._turn_status(harness="codex", session_kind="implementation")
        self.assertEqual(self._classify(parsed, status), "mismatch")

    def test_review_turn_without_a_review_harness_is_attach(self) -> None:
        # Plan and state identity already matched, so the lock is ours;
        # `--harness` is the wrong yardstick for a review turn.
        status = self._turn_status(harness="codex", session_kind="review")
        self.assertEqual(self._classify({"harness": "claude"}, status), "attach")

    def test_absent_session_kind_still_detects_a_foreign_harness(self) -> None:
        status = self._turn_status(harness="codex")
        self.assertEqual(self._classify({"harness": "claude"}, status), "mismatch")

    def test_unrecognized_session_kind_is_attach(self) -> None:
        status = self._turn_status(harness="codex", session_kind="verification")
        self.assertEqual(self._classify({"harness": "claude"}, status), "attach")


if __name__ == "__main__":
    unittest.main()
