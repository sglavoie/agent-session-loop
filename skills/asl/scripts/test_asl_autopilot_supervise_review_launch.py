#!/usr/bin/env python3
"""Tests for supervise_run review launch decisions and attach behavior."""

from __future__ import annotations

import unittest

from asl_autopilot_test_bases import SuperviseRunInjectedBase
from asl_autopilot_test_payloads import _active_lock, _review_resume, _status_payload
from asl_autopilot_test_support import (  # noqa: F401
    _never_run,
    _RunScript,
    _StatusScript,
    autopilot,
    setUpModule,
    tearDownModule,
)


class SuperviseRunReviewLaunchTest(SuperviseRunInjectedBase):
    def test_already_complete_returns_without_running(self) -> None:
        status = _StatusScript([_status_payload(0)])
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(result["chunks"], 0)

    def test_stale_lock_starts_a_run(self) -> None:
        status = _StatusScript(
            [
                _status_payload(1, lock=_active_lock(stale=True)),
                _status_payload(0),
            ]
        )
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(run.chunks, [1])

    def test_mismatched_lock_is_user_required(self) -> None:
        status = _StatusScript(
            [_status_payload(1, lock=_active_lock(plan_dir="/other"))]
        )
        result = self._supervise(status, _never_run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "user_required")

    def test_attach_waits_then_completes(self) -> None:
        status = _StatusScript(
            [
                _status_payload(1, lock=_active_lock()),
                _status_payload(1, lock=_active_lock()),
                _status_payload(0),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "complete")
        # Polled three times: two attach waits then the completed state.
        self.assertEqual(status.calls, 3)

    def test_active_lock_with_zero_pending_attaches_before_completing(self) -> None:
        status = _StatusScript(
            [
                _status_payload(0, lock=_active_lock()),
                _status_payload(0),
            ]
        )
        result = self._supervise(status, _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")
        self.assertEqual(status.calls, 2)

    def test_attach_survives_a_review_turn_on_the_review_harness(self) -> None:
        # The runner alternates harnesses; a codex review turn under
        # `--harness claude` used to read as a foreign lock and abort
        # supervision with `user_required` on a perfectly healthy run.
        review_turn = _status_payload(1, lock=_active_lock())
        review_turn["runner_state"] = {
            "active_turn": {"harness": "codex", "session_kind": "review"}
        }
        status = _StatusScript([review_turn, _status_payload(0)])
        parsed = {
            "plan_dir": "/tmp/plan",
            "harness": "claude",
            "review_harness": "codex",
            "argv": [
                "asl",
                "run",
                "/tmp/plan",
                "--harness",
                "claude",
                "--review-harness",
                "codex",
                "--json",
            ],
        }
        result = self._supervise(status, _never_run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason"], "complete")

    def test_all_complete_initial_review_launches_with_review_harness(self) -> None:
        initial = _status_payload(0)
        initial["review_resume"] = _review_resume(
            "initial-review", eligible=False, blocker="review-harness-required"
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            source_disposition="all-clear",
        )
        status = _StatusScript([initial, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["review_resume"]["blocker"], "all-clear")
        self.assertNotIn("no automatic continuation", autopilot._human_summary(result))

    def test_corrected_malformed_review_launches_materialization(self) -> None:
        corrected = _status_payload(0)
        corrected["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=True,
            minimum_required_cap=1,
            source_disposition="malformed-review-repair",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            minimum_required_cap=2,
            source_disposition="findings-repeated",
        )
        status = _StatusScript([corrected, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 1,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])
        # The terminal projection prevents another launch after the reopened
        # chunk, even though its reported next-cap minimum is now higher.
        self.assertEqual(status.calls, 2)

    def test_raised_command_cap_overrides_status_historical_cap(self) -> None:
        resumable = _status_payload(0)
        resumable["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="findings-repeated",
            minimum_required_cap=4,
            source_disposition="findings-repeated",
        )
        status = _StatusScript([resumable, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": [
                "asl",
                "run",
                "/tmp/plan",
                "--max-review-fix-cycles",
                "3",
                "--json",
            ],
            "review_harness": "codex",
            "max_review_fix_cycles": 3,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_default_cap_launches_materialization_requiring_three_or_four(
        self,
    ) -> None:
        parsed = autopilot.parse_command("asl run /tmp/plan --review-harness codex")
        self.assertTrue(parsed["ok"], parsed)
        for minimum_cap in (3, 4):
            with self.subTest(minimum_cap=minimum_cap):
                status = _status_payload(0)
                status["review_resume"] = _review_resume(
                    "materialize-repairs",
                    eligible=False,
                    blocker="higher-cap-required",
                    minimum_required_cap=minimum_cap,
                    source_disposition="cycle-cap-exhausted",
                )
                self.assertTrue(autopilot._should_launch_for_status(parsed, status, 0))

    def test_unchanged_insufficient_cap_returns_guidance_without_launch(self) -> None:
        blocked = _status_payload(0)
        blocked["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=False,
            blocker="higher-cap-required",
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": [
                "asl",
                "run",
                "/tmp/plan",
                "--max-review-fix-cycles",
                "2",
                "--json",
            ],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(_StatusScript([blocked]), _never_run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["review_resume"]["minimum_required_cap"], 3)

    def test_command_cap_lower_than_status_selected_cap_does_not_launch(self) -> None:
        status_payload = _status_payload(0)
        status_payload["review_resume"] = _review_resume(
            "materialize-repairs",
            eligible=True,
            minimum_required_cap=3,
            source_disposition="cycle-cap-exhausted",
        )
        status_payload["review_resume"]["selected_cap"] = 5
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": [
                "asl",
                "run",
                "/tmp/plan",
                "--max-review-fix-cycles",
                "2",
                "--json",
            ],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(
            _StatusScript([status_payload]), _never_run, parsed=parsed
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)
        summary = autopilot._human_summary(result)
        self.assertIn("with a review harness", summary)
        self.assertIn("--max-review-fix-cycles 3 or higher", summary)

    def test_missing_rereview_launches_when_command_cap_is_sufficient(self) -> None:
        rereview = _status_payload(0)
        rereview["review_resume"] = _review_resume(
            "rereview",
            eligible=True,
            minimum_required_cap=2,
            source_disposition="repairs-materialized",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            minimum_required_cap=2,
            source_disposition="all-clear",
        )
        status = _StatusScript([rereview, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(run.chunks, [1])

    def test_missing_rereview_launches_below_reported_cap(self) -> None:
        rereview = _status_payload(0)
        rereview["review_resume"] = _review_resume(
            "rereview",
            eligible=True,
            minimum_required_cap=3,
            source_disposition="repairs-materialized",
        )
        terminal = _status_payload(0)
        terminal["review_resume"] = _review_resume(
            "terminal",
            eligible=False,
            blocker="all-clear",
            minimum_required_cap=3,
            source_disposition="all-clear",
        )
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": [
                "asl",
                "run",
                "/tmp/plan",
                "--max-review-fix-cycles",
                "2",
                "--json",
            ],
            "review_harness": "codex",
            "max_review_fix_cycles": 2,
        }
        status = _StatusScript([rereview, terminal])
        run = _RunScript([{"ok": True, "paused": False, "reason": "all-complete"}])
        result = self._supervise(status, run, parsed=parsed)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 1)
        self.assertEqual(run.chunks, [1])
        self.assertEqual(result["review_resume"]["blocker"], "all-clear")

    def test_artifact_invalid_and_terminal_dispositions_do_not_launch(self) -> None:
        cases = (
            _review_resume(
                "materialize-repairs",
                eligible=False,
                blocker="artifact-invalid",
                source_disposition="malformed-review-repair",
                artifact_error="repair_group is missing",
            ),
            _review_resume(
                "terminal",
                eligible=False,
                blocker="findings-repeated",
                source_disposition="findings-repeated",
            ),
            _review_resume(
                "terminal",
                eligible=False,
                blocker="unstructured-findings",
                source_disposition="unstructured-findings",
            ),
            _review_resume(
                "terminal",
                eligible=False,
                blocker="all-clear",
                source_disposition="all-clear",
            ),
        )
        for review_resume in cases:
            with self.subTest(blocker=review_resume["blocker"]):
                status_payload = _status_payload(0)
                status_payload["review_resume"] = review_resume
                parsed = {
                    "plan_dir": "/tmp/plan",
                    "argv": [
                        "asl",
                        "run",
                        "/tmp/plan",
                        "--review-harness",
                        "codex",
                        "--json",
                    ],
                    "review_harness": "codex",
                    "max_review_fix_cycles": 9,
                }
                result = self._supervise(
                    _StatusScript([status_payload]), _never_run, parsed=parsed
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["chunks"], 0)

    def test_run_repairs_and_unknown_actions_do_not_launch(self) -> None:
        for action in ("run-repairs", "future-action"):
            with self.subTest(action=action):
                status_payload = _status_payload(0)
                status_payload["review_resume"] = _review_resume(
                    action,
                    eligible=True,
                    source_disposition="repairs-materialized",
                )
                parsed = {
                    "plan_dir": "/tmp/plan",
                    "argv": [
                        "asl",
                        "run",
                        "/tmp/plan",
                        "--review-harness",
                        "codex",
                        "--json",
                    ],
                    "review_harness": "codex",
                    "max_review_fix_cycles": 9,
                }
                result = self._supervise(
                    _StatusScript([status_payload]), _never_run, parsed=parsed
                )
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["chunks"], 0)

    def test_review_resume_does_not_launch_without_review_harness(self) -> None:
        status_payload = _status_payload(0)
        status_payload["review_resume"] = _review_resume(
            "materialize-repairs", eligible=True
        )
        result = self._supervise(_StatusScript([status_payload]), _never_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)
        summary = autopilot._human_summary(result)
        self.assertIn("with a review harness", summary)
        self.assertIn("eligible materialize-repairs continuation", summary)

    def test_older_status_without_review_resume_keeps_complete_behavior(self) -> None:
        parsed = {
            "plan_dir": "/tmp/plan",
            "argv": ["asl", "run", "/tmp/plan", "--review-harness", "codex", "--json"],
            "review_harness": "codex",
            "max_review_fix_cycles": 4,
        }
        result = self._supervise(
            _StatusScript([_status_payload(0)]), _never_run, parsed=parsed
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["chunks"], 0)


if __name__ == "__main__":
    unittest.main()
