"""Review cycle crash recovery tests."""

import json
import unittest

from agent_session_loop import (
    plans_content_hash,
    plans_models,
    plans_readiness,
    plans_snapshot,
    review,
    review_continuation,
    review_repair,
    review_session_body,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _structured_review_finding,
)


class ReviewCycleCrashRecoveryTests(RepairCycleFixtures, unittest.TestCase):
    """Backfill re-review baselines after materialization crashes."""

    def test_eventless_materialization_crash_backfills_rereview_baseline(
        self,
    ) -> None:
        eventless_finding = _structured_review_finding(
            finding_id="cycle-finding-2",
            title="Missing error handling in verification executor",
            evidence=[
                {
                    "path": "agent_session_loop/verification.py:88",
                    "symbol": "run_check",
                }
            ],
            repair_group="verification-errors",
        )
        eventless_fingerprint = review.finding_fingerprint(eventless_finding)
        cases = (
            (
                "repeats-eventless",
                "findings-new",
                review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
                [eventless_fingerprint],
                [],
            ),
            (
                "matches-only-older",
                "findings",
                review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
                [],
                None,
            ),
        )
        for (
            name,
            rereview_outcome,
            expected_disposition,
            expected_repeated,
            expected_introduced,
        ) in cases:
            with self.subTest(name=name):
                self.plan_dir = self.tmp_path / f"plan-{name}"
                self.state_dir = self.tmp_path / f"state-{name}"
                self._write_plan()
                first, first_payload = self._run_cycles(
                    review_outcomes="findings",
                    extra_args=["--max-review-fix-cycles", "0"],
                )
                self.assertEqual(1, first.returncode, first.stderr)
                older_fingerprint = first_payload["review_cycles"][0]["fingerprints"][0]
                self.assertNotEqual(older_fingerprint, eventless_fingerprint)

                eventless_review, created = (
                    review_session_body.ensure_generated_review_session(
                        self.plan_dir, review_harness="claude"
                    )
                )
                self.assertTrue(created)
                self.assertEqual("3", eventless_review.id)
                eventless_review.path.write_text(
                    eventless_review.path.read_text(encoding="utf-8").replace(
                        "- **Status**: pending", "- **Status**: complete"
                    ),
                    encoding="utf-8",
                )
                (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "outcome": "findings",
                            "findings": [eventless_finding],
                        }
                    ),
                    encoding="utf-8",
                )
                materialized = review_repair.materialize_review_repair_sessions(
                    self.plan_dir,
                    review_session_id="3",
                    cwd=self.repo_root,
                    state_dir=self.state_dir,
                )
                self.assertTrue(materialized.ok, materialized.error)
                self.assertEqual([], self._review_reopen_events())

                projected = review_continuation.evaluate_review_continuation(
                    plans_snapshot.inspect_plan(self.plan_dir),
                    self._events_path(),
                    max_review_fix_cycles=1,
                )
                self.assertIsNone(projected.source_disposition)
                self.assertEqual("run-repairs", projected.action)
                self.assertEqual(
                    (eventless_fingerprint,), projected.current_fingerprints
                )

                resumed, payload = self._run_cycles(
                    review_outcomes=rereview_outcome,
                    extra_args=["--max-review-fix-cycles", "1"],
                )

                self.assertEqual(1, resumed.returncode, resumed.stderr)
                self.assertEqual(
                    ["3.1", "4"],
                    [turn["session_id"] for turn in payload["turns"]],
                )
                reopen_events = self._review_reopen_events()
                self.assertEqual(1, len(reopen_events))
                self.assertTrue(reopen_events[0]["recovered"])
                self.assertEqual(
                    [eventless_fingerprint],
                    reopen_events[0]["current_fingerprints"],
                )
                rereview_cycle = payload["review_cycles"][-1]
                self.assertEqual(expected_disposition, rereview_cycle["disposition"])
                self.assertEqual(expected_repeated, rereview_cycle["repeated_findings"])
                if expected_introduced is None:
                    expected_introduced = [older_fingerprint]
                self.assertEqual(
                    expected_introduced,
                    rereview_cycle["introduced_findings"],
                )

    def test_first_review_crash_backfill_repeats_against_reopen_baseline(
        self,
    ) -> None:
        self._write_plan()

        # Model a crash after the first implementation completed, followed by
        # a first review that materialized its repair child before the initial
        # review_cycle append. The event log therefore has no cycle record.
        implementation_path = self.plan_dir / "session-1.md"
        implementation_path.write_text(
            implementation_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        first_review, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        self.assertTrue(created)
        self.assertEqual("2", first_review.id)
        first_review.path.write_text(
            first_review.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        finding = _structured_review_finding(
            finding_id="cycle-finding-2",
            title="Missing error handling in verification executor",
            evidence=[
                {
                    "path": "agent_session_loop/verification.py:88",
                    "symbol": "run_check",
                }
            ],
            repair_group="verification-errors",
        )
        fingerprint = review.finding_fingerprint(finding)
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- agent_session_loop/verification.py:88 — Missing error handling "
            "in verification executor.\n"
            "- Checked tests/test_agent_session_loop.py for regression coverage.\n"
            "- Compared docs/agent-session-loop.md for documented behavior.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        materialized = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            review_session_id="2",
            cwd=self.repo_root,
            state_dir=self.state_dir,
        )
        self.assertTrue(materialized.ok, materialized.error)
        self.assertFalse(self._events_path().exists())

        resumed, payload = self._run_cycles(
            review_outcomes="findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual(
            ["2.1", "3"], [turn["session_id"] for turn in payload["turns"]]
        )
        reopen_events = self._review_reopen_events()
        self.assertEqual(1, len(reopen_events))
        self.assertTrue(reopen_events[0]["recovered"])
        self.assertEqual([fingerprint], reopen_events[0]["current_fingerprints"])
        self.assertEqual(1, len(payload["review_cycles"]))
        rereview_cycle = payload["review_cycles"][0]
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            rereview_cycle["disposition"],
        )
        self.assertEqual([fingerprint], rereview_cycle["repeated_findings"])
        self.assertEqual([], rereview_cycle["introduced_findings"])
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

    def test_existing_reopened_children_backfill_missing_success_event(
        self,
    ) -> None:
        self._write_plan()
        first, _ = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)
        materialized = review_repair.materialize_review_repair_sessions(
            self.plan_dir,
            review_session_id="2",
            cwd=self.repo_root,
            state_dir=self.state_dir,
        )
        self.assertTrue(materialized.ok, materialized.error)
        self.assertEqual([], self._review_reopen_events())

        resumed, payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(
            ["2.1", "3"], [turn["session_id"] for turn in payload["turns"]]
        )
        reopen_events = self._review_reopen_events()
        self.assertEqual(1, len(reopen_events))
        self.assertTrue(reopen_events[0]["materialized"])
        self.assertTrue(reopen_events[0]["recovered"])
        self.assertEqual("recovered-existing-children", reopen_events[0]["result"])
        self.assertEqual(["2.1"], reopen_events[0]["repair_session_ids"])
        self.assertFalse((self.plan_dir / "session-2.2.md").exists())


if __name__ == "__main__":
    unittest.main()
