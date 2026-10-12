"""Review reopen and re-review tests."""

import json
import unittest

from agent_session_loop import (
    events,
    plans,
    review,
    review_continuation,
    review_delta,
    review_repair,
    review_session_body,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _structured_review_finding,
    read_jsonl,
)


class ReviewCycleReopenTests(RepairCycleFixtures, unittest.TestCase):
    """Re-review reopened children against corrected fingerprints."""

    def test_reopen_rechecks_corrected_artifact_and_preserves_legacy_lineage(
        self,
    ) -> None:
        self._write_plan()
        first, _ = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)

        # Model a legacy source cycle whose persisted event predates digests.
        event_path = self._events_path()
        records = read_jsonl(event_path)
        for record in records:
            if record["name"] == events.REVIEW_CYCLE_EVENT:
                record["payload"].pop("review_output_sha256", None)
                record["payload"].pop("review_artifact_sha256", None)
        event_path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        findings = [
            _structured_review_finding(
                finding_id=f"revised-{index}",
                title=f"Revised finding {index}",
                evidence=[
                    {
                        "path": f"agent_session_loop/revised_{index}.py:{index}",
                        "symbol": f"target_{index}",
                    }
                ],
                repair_group=f"group-{index}",
                verification_checks=[
                    {
                        "type": "command",
                        "name": f"verify-revised-{index}",
                        "argv": ["python3", "-c", "pass"],
                    }
                ],
            )
            for index in range(1, 10)
        ]
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
            encoding="utf-8",
        )
        review_path = self.plan_dir / review.FINAL_REVIEW_FILENAME
        review_path.write_text(
            review_path.read_text(encoding="utf-8").replace(
                "Status: 1 Findings (1 high, 0 med, 0 low)",
                "Status: 9 Findings (9 high, 0 med, 0 low)",
            ),
            encoding="utf-8",
        )
        invalid_bytes = artifact_path.read_bytes()

        invalid, invalid_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, invalid.returncode)
        self.assertEqual("malformed-review-repair", invalid_payload["pause_reason"])
        self.assertEqual(
            "artifact-invalid", invalid_payload["review_resume"]["blocker"]
        )
        self.assertIn("exceed the maximum of 8", invalid_payload["pause_message"])
        self.assertEqual(invalid_bytes, artifact_path.read_bytes())
        self.assertEqual([], sorted(self.plan_dir.glob("session-2.*.md")))
        self.assertFalse(self._review_reopen_events()[-1]["materialized"])

        # A revised, internally consistent v2 artifact may regroup findings.
        findings[-1]["repair_group"] = "group-8"
        artifact_path.write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": findings}),
            encoding="utf-8",
        )
        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertEqual(
            [*(f"2.{index}" for index in range(1, 9)), "3"],
            [turn["session_id"] for turn in resumed_payload["turns"]],
        )
        success = [
            record for record in self._review_reopen_events() if record["materialized"]
        ][0]
        self.assertIsNone(success["source_review_output_sha256"])
        self.assertIsNone(success["source_review_artifact_sha256"])
        self.assertNotEqual(
            success["previous_fingerprints"], success["current_fingerprints"]
        )
        self.assertEqual(
            [f"2.{index}" for index in range(1, 9)],
            success["repair_session_ids"],
        )

    def test_post_reopen_review_repeats_against_corrected_fingerprints(
        self,
    ) -> None:
        self._write_plan()
        first, first_payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED,
            first_payload["review_fix_disposition"],
        )

        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text("{not valid json", encoding="utf-8")
        invalid, invalid_payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )
        self.assertEqual(1, invalid.returncode)
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            invalid_payload["pause_reason"],
        )
        self.assertFalse(self._review_reopen_events()[-1]["materialized"])

        # Model a completed findings rereview whose review_cycle event was
        # lost. Its corrected artifact is then materialized successfully,
        # leaving the latest durable cycle attached to the older review 2.
        eventless_review, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        self.assertTrue(created)
        self.assertEqual("3", eventless_review.id)
        eventless_review.path.write_text(
            eventless_review.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )

        corrected = _structured_review_finding(
            finding_id="corrected-cycle-finding",
            title="Missing error handling in verification executor",
            evidence=[
                {
                    "path": "agent_session_loop/verification.py:88",
                    "symbol": "run_check",
                }
            ],
            repair_group="verification-errors",
        )
        corrected_fingerprint = review.finding_fingerprint(corrected)
        self.assertNotEqual(
            first_payload["review_cycles"][0]["fingerprints"],
            [corrected_fingerprint],
        )
        artifact_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "outcome": "findings",
                    "findings": [corrected],
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
        self.assertEqual(
            ["3.1"], [child.session_id.value for child in materialized.children]
        )
        events.append_event(
            self._events_path(),
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "run_id": "eventless-review-reopen",
                    "source_cycle": 2,
                    "source_review_session_id": "3",
                    "source_disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED
                    ),
                    "previous_fingerprints": None,
                    "current_fingerprints": [corrected_fingerprint],
                    "repair_session_ids": ["3.1"],
                    "materialized": True,
                    "recovered": False,
                    "result": "materialized",
                    "historical_materializations_before": 0,
                    "historical_materializations_after": 1,
                    "minimum_required_cap": 1,
                    "before_max_review_fix_cycles": 0,
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )
        before_rereview = review_delta._review_cycle_state(
            self._events_path(), plans.inspect_plan(self.plan_dir)
        )
        self.assertEqual(
            (corrected_fingerprint,), before_rereview["previous_fingerprints"]
        )

        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,findings-new",
            extra_args=["--max-review-fix-cycles", "1"],
        )

        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual(
            ["3.1", "4"],
            [turn["session_id"] for turn in resumed_payload["turns"]],
        )
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            resumed_payload["review_fix_disposition"],
        )
        post_reopen = resumed_payload["review_cycles"][-1]
        self.assertEqual("4", post_reopen["review_session_id"])
        self.assertEqual([corrected_fingerprint], post_reopen["fingerprints"])
        self.assertEqual([corrected_fingerprint], post_reopen["repeated_findings"])
        self.assertEqual([], post_reopen["introduced_findings"])
        successful_reopen = [
            record for record in self._review_reopen_events() if record["materialized"]
        ][0]
        self.assertEqual("3", successful_reopen["source_review_session_id"])
        self.assertEqual(
            [corrected_fingerprint],
            successful_reopen["current_fingerprints"],
        )
        self.assertFalse((self.plan_dir / "session-4.1.md").exists())

    def test_reopened_children_rereview_across_max_sessions_boundary(
        self,
    ) -> None:
        self._write_plan()
        first, _ = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=["--max-review-fix-cycles", "0"],
        )
        self.assertEqual(1, first.returncode, first.stderr)

        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        original = json.loads(artifact_path.read_text(encoding="utf-8"))
        extra_finding = dict(original["findings"][0])
        extra_finding.update(
            {
                "id": "cycle-finding-extra",
                "title": "A second repair group",
                "evidence": [
                    {
                        "path": "agent_session_loop/review.py:200",
                        "symbol": "load_review_verdict",
                    }
                ],
                "repair_group": "review-loading",
                "verification_checks": [
                    {
                        "type": "command",
                        "name": "review-loading-check",
                        "argv": ["python3", "-c", "pass"],
                    }
                ],
            }
        )
        original["findings"].append(extra_finding)
        artifact_path.write_text(json.dumps(original), encoding="utf-8")
        review_path = self.plan_dir / review.FINAL_REVIEW_FILENAME
        review_path.write_text(
            review_path.read_text(encoding="utf-8").replace(
                "Status: 1 Findings (1 high, 0 med, 0 low)",
                "Status: 2 Findings (2 high, 0 med, 0 low)",
            ),
            encoding="utf-8",
        )

        partial, partial_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=[
                "--max-review-fix-cycles",
                "1",
                "--max-sessions",
                "1",
            ],
        )

        self.assertEqual(0, partial.returncode, partial.stderr)
        self.assertEqual("max-sessions", partial_payload["reason"])
        self.assertEqual(
            ["2.1"], [turn["session_id"] for turn in partial_payload["turns"]]
        )
        self.assertTrue((self.plan_dir / "session-2.2.md").is_file())
        self.assertFalse((self.plan_dir / "session-3.md").exists())
        partial_resume = partial_payload["review_resume"]
        self.assertEqual(["2.2"], partial_resume["pending_repair_session_ids"])
        self.assertEqual(["2.1"], partial_resume["completed_repair_session_ids"])
        partial_finished = [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == "run_finished"
            and record["payload"]["run_id"] == partial_payload["run_id"]
        ]
        self.assertEqual(1, len(partial_finished))
        self.assertEqual(partial_resume, partial_finished[0]["review_resume"])

        final, final_payload = self._run_cycles(
            review_outcomes="findings,all-clear",
            extra_args=[
                "--max-review-fix-cycles",
                "1",
                "--max-sessions",
                "1",
            ],
        )

        self.assertEqual(0, final.returncode, final.stderr)
        self.assertEqual(
            ["2.2", "3"],
            [turn["session_id"] for turn in final_payload["turns"]],
        )
        self.assertEqual("all-clear", final_payload["review_fix_disposition"])
        self.assertEqual(1, len(self._review_reopen_events()))
        self.assertFalse((self.plan_dir / "session-2.3.md").exists())
        progress = (self.plan_dir / "progress.md").read_text(encoding="utf-8")
        self.assertEqual(1, progress.count("## Session 2.1 "))
        self.assertEqual(1, progress.count("## Session 2.2 "))
        self.assertEqual(1, progress.count("## Session 3 "))


if __name__ == "__main__":
    unittest.main()
