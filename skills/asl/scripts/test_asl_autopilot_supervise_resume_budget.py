#!/usr/bin/env python3
"""Tests for supervise_run resume configuration, chunk caps, and budgets."""

from __future__ import annotations

import unittest

from asl_autopilot_test_bases import SuperviseRunInjectedBase
from asl_autopilot_test_payloads import _active_lock, _review_resume, _status_payload
from asl_autopilot_test_support import (  # noqa: F401
    _AttachClock,
    _max_sessions_of,
    _never_run,
    _RunScript,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SuperviseRunResumeBudgetTest(SuperviseRunInjectedBase):
    def test_resume_command_honors_persisted_review_harness_for_initial_review(
        self,
    ) -> None:
        # A resume-only command has no --review-harness of its own; the runner
        # would rehydrate it from run_config. The supervisor must resolve the
        # same persisted harness so it launches the initial review instead of
        # stopping complete.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 2}
        )
        initial = _status_payload(0)
        initial["state_path"] = state_path
        initial["review_resume"] = _review_resume(
            "initial-review", eligible=False, blocker="review-harness-required"
        )
        terminal = _status_payload(0)
        terminal["state_path"] = state_path
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            source_disposition="all-clear",
        )
        status = _StatusScript([initial, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=self._resume_parsed(state_path))
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_resume_command_honors_persisted_higher_cap_for_materialization(
        self,
    ) -> None:
        # The persisted cap (3) is high enough for a materialize-repairs
        # continuation whose minimum required cap is 3; the resume-only
        # command's default cap of 2 would otherwise wrongly stop it.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 3}
        )
        resumable = _status_payload(0)
        resumable["state_path"] = state_path
        resumable["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        terminal = _status_payload(0)
        terminal["state_path"] = state_path
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            minimum_required_cap=4,
            source_disposition="findings-repeated",
        )
        status = _StatusScript([resumable, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=self._resume_parsed(state_path))
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_resume_command_stops_when_persisted_cap_too_low(self) -> None:
        # Persisted cap (2) is below the minimum required cap (3): the resume
        # command must not launch, matching what the resumed runner (which keeps
        # the persisted cap) would decide.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 2}
        )
        blocked = _status_payload(0)
        blocked["state_path"] = state_path
        blocked["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        result = self._supervise(
            _StatusScript([blocked]), _never_run, parsed=self._resume_parsed(state_path)
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)

    def test_resume_explicit_cap_overrides_persisted_lower_cap(self) -> None:
        # An explicit --max-review-fix-cycles on the resume command wins over the
        # lower persisted cap, mirroring the runner's resume overlay.
        state_path = self._seed_run_config(
            {"review_harness": "codex", "max_review_fix_cycles": 1}
        )
        resumable = _status_payload(0)
        resumable["state_path"] = state_path
        resumable["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        terminal = _status_payload(0)
        terminal["state_path"] = state_path
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            source_disposition="findings-repeated",
        )
        parsed = self._resume_parsed(state_path)
        parsed["max_review_fix_cycles"] = 3
        parsed["max_review_fix_cycles_explicit"] = True
        status = _StatusScript([resumable, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_paused_run_stops_with_paused(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript(
            [
                {
                    "ok": False,
                    "paused": True,
                    "reason": "input-error",
                    "pause_message": "needs an answer",
                }
            ]
        )
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "paused")
        self.assertEqual(result["error"], "needs an answer")

    def test_non_paused_failure_is_blocked(self) -> None:
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([{"ok": False, "paused": False, "reason": "verify-failed"}])
        result = self._supervise(status, run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "blocked")

    def test_status_probe_failure_stops(self) -> None:
        status = _StatusScript(
            [
                {
                    "ok": False,
                    "stage": autopilot.RUN_STAGE,
                    "reason": "status-error",
                    "error": "boom",
                }
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "status-error")

    def test_runaway_relaunch_caps_at_max_chunks(self) -> None:
        # Safety net: with no session budget, a run that keeps returning a clean
        # stop while pending never decreases would relaunch forever; max_chunks
        # bounds it and surfaces `capped`.
        status = _StatusScript([_status_payload(1)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, max_chunks=3)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capped")
        self.assertEqual(result["chunks"], 3)

    def test_attach_capped_by_elapsed_time(self) -> None:
        # An attached run that never releases its lock would poll forever;
        # attach_max_seconds bounds it in wall clock and surfaces `capped`.
        driver = _AttachClock([_status_payload(1, lock=_active_lock())], step=30.0)
        result = self._supervise(
            driver.status,
            _never_run,
            clock=driver,
            events_stat=lambda _path: None,
            attach_max_seconds=90.0,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capped")
        self.assertIn("limit 90s", result["error"])
        # The self-launch counter is untouched by attach polls.
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(driver.calls, 4)

    def test_attach_is_not_bounded_by_max_chunks(self) -> None:
        # Regression guard: the two budgets are independent, so a tiny relaunch
        # cap must not evict the supervisor from a healthy attach loop.
        attached = _status_payload(1, lock=_active_lock())
        driver = _AttachClock([attached] * 6 + [_status_payload(0)], step=1.0)
        result = self._supervise(
            driver.status,
            _never_run,
            clock=driver,
            events_stat=lambda _path: None,
            max_chunks=1,
            attach_max_seconds=3600.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(driver.calls, 7)

    def test_attach_deadline_is_per_episode_not_cumulative(self) -> None:
        # Cumulative elapsed at the final attach poll is 160s; a cumulative
        # bound would cap. Per-episode elapsed never exceeds 40s.
        attached = _status_payload(1, lock=_active_lock())
        driver = _AttachClock(
            [
                attached,
                attached,
                _status_payload(1),
                attached,
                attached,
                _status_payload(0),
            ],
            step=40.0,
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(
            driver.status,
            run,
            clock=driver,
            events_stat=lambda _path: None,
            attach_max_seconds=100.0,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_max_sessions_budget_stops_cleanly(self) -> None:
        # Global budget of 1 on a 3-session plan: after one completed session
        # (pending 3 -> 2) the supervisor stops cleanly without chunking through
        # the remaining sessions.
        status = _StatusScript([_status_payload(3), _status_payload(2)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "max-sessions"}])
        result = self._supervise(status, run, parsed=self._budget_parsed(1))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "max-sessions")
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(run.chunks, [1])

    def test_budget_equal_to_pending_completes(self) -> None:
        # Budget covering every pending session must finish as `complete`, not
        # pre-empt with `max-sessions`, when pending reaches 0.
        status = _StatusScript([_status_payload(2), _status_payload(0)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=self._budget_parsed(2))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_no_budget_completes_in_one_chunk(self) -> None:
        # Without `--max-sessions`, the runner drives all pending sessions in a
        # single chunk and the supervisor reports `complete`.
        status = _StatusScript([_status_payload(4), _status_payload(0)])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_budget_injection_caps_relaunch_after_pause(self) -> None:
        # Budget 3: chunk 1 completes 2 sessions (pending 3 -> 1) then pauses on
        # a recoverable verify-failed; the retry must be capped at the 1 session
        # still in budget so total completions never exceed 3.
        status = _StatusScript(
            [
                _status_payload(3),
                _status_payload(1),
                _status_payload(0),
            ]
        )
        payloads = [
            {"ok": False, "paused": True, "reason": "verify-failed"},
            {"ok": True, "paused": False, "reason": "all-complete"},
        ]
        seen = []

        def run(parsed, chunk_index):
            seen.append(list(parsed["argv"]))
            return payloads[min(chunk_index - 1, len(payloads) - 1)]

        result = self._supervise(status, run, parsed=self._budget_parsed(3))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        # Chunk 1 launched with the full budget; the retry capped at the remainder.
        self.assertEqual(_max_sessions_of(seen[0]), "3")
        self.assertEqual(_max_sessions_of(seen[1]), "1")

    def test_budget_injection_passes_full_budget_without_pause(self) -> None:
        # No pause: a single chunk carries the full budget and completes.
        status = _StatusScript([_status_payload(2), _status_payload(0)])
        seen = []

        def run(parsed, chunk_index):
            seen.append(list(parsed["argv"]))
            return {"ok": True, "paused": False, "reason": "all-complete"}

        result = self._supervise(status, run, parsed=self._budget_parsed(5))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(len(seen), 1)
        self.assertEqual(_max_sessions_of(seen[0]), "5")


if __name__ == "__main__":
    unittest.main()
