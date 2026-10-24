"""End-to-end review reopen and re-review tests through the CLI.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json

from agent_session_loop import (
    events,
    review,
    state,
)
from tests.asl_e2e_support import (
    EndToEndQABase,
)
from tests.support import (
    _contract_block,
    _write_loop_ready_plan,
)

ASL_TEST_LANE = "integration"


class ReviewReopenEndToEndTests(EndToEndQABase):
    """Completed reviews reopen and re-review through the real CLI."""

    def test_completed_review_reopens_and_rereviews_through_cli(self) -> None:
        """A higher absolute cap resumes the persisted review in a new run."""

        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        outcomes = "findings,all-clear"

        first, first_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=0,
            mode="success-with-review",
            review_outcomes=outcomes,
        )
        # The zero cap leaves the finding open, so the first run reports it.
        self.assertEqual(1, first.returncode, first.stderr)
        self.assertEqual(
            ["1", "2"],
            [turn["session_id"] for turn in first_payload["turns"]],
        )
        self.assertEqual(
            "higher-cap-required", first_payload["review_resume"]["blocker"]
        )

        second, second_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=1,
            mode="success-with-review",
            review_outcomes=outcomes,
        )

        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(
            ["2.1", "3"],
            [turn["session_id"] for turn in second_payload["turns"]],
        )
        self.assertEqual("all-clear", second_payload["review_fix_disposition"])
        self.assertEqual("terminal", second_payload["review_resume"]["action"])
        reopen = second_payload["review_reopen"]
        self.assertTrue(reopen["materialized"])
        self.assertEqual("2", reopen["source_review_session_id"])
        self.assertEqual(["2.1"], reopen["repair_session_ids"])
        self.assertTrue((self.plan_dir / "session-3.md").is_file())

    def test_autopilot_reopens_exact_legacy_review_history_after_normalization(
        self,
    ) -> None:
        """Legacy not-loop-ready + digestless malformed history resumes."""

        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        seeded, seeded_payload = self._run_run(
            review_harness="claude",
            max_review_fix_cycles=1,
            mode="success-with-review",
            review_outcomes="findings,all-clear",
        )
        self.assertEqual(0, seeded.returncode, seeded.stderr)
        self.assertEqual(
            ["1", "2", "2.1", "3"],
            [turn["session_id"] for turn in seeded_payload["turns"]],
        )

        # Reproduce the motivating persisted history: the first source did
        # materialize a child but stopped not-loop-ready, and the later source
        # recorded malformed repair output before artifact digests existed.
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        records = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        cycles = [
            record["payload"]
            for record in records
            if record["name"] == events.REVIEW_CYCLE_EVENT
        ]
        self.assertEqual(2, len(cycles))
        cycles[0]["disposition"] = "not-loop-ready"
        cycles[0].pop("review_output_sha256", None)
        cycles[0].pop("review_artifact_sha256", None)
        cycles[1].update(
            {
                "outcome": "findings",
                "disposition": "malformed-review-repair",
                "fingerprints": ["finding:legacy-nine-groups"],
                "introduced_findings": ["finding:legacy-nine-groups"],
                "closed_findings": [],
                "repeated_findings": [],
                "repair_session_ids": [],
                "max_review_fix_cycles": 1,
            }
        )
        cycles[1].pop("review_output_sha256", None)
        cycles[1].pop("review_artifact_sha256", None)
        event_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

        findings = []
        for index in range(1, 10):
            findings.append(
                {
                    "id": f"legacy-{index}",
                    "severity": "high",
                    "title": f"Legacy finding {index}",
                    "evidence": [
                        {
                            "path": f"agent_session_loop/legacy_{index}.py:{index}",
                            "symbol": f"target_{index}",
                        }
                    ],
                    "explanation": "Compatibility fixture finding.",
                    "repair_group": f"group-{index}",
                    "done_criteria": ["The compatibility defect is fixed."],
                    "verification_checks": [
                        {
                            "type": "command",
                            "name": f"legacy-check-{index}",
                            "argv": ["python3", "-c", "pass"],
                        }
                    ],
                }
            )
        artifact_path = self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
        artifact_path.write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": findings},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- Legacy review repair grouping requires normalization.\n"
            "Status: 9 Findings (9 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )

        invalid, invalid_payload = self._run_autopilot_review(
            max_review_fix_cycles=2,
            review_outcomes="findings,all-clear,all-clear",
        )
        self.assertEqual(1, invalid.returncode, invalid.stderr)
        self.assertEqual("review_findings", invalid_payload["reason"])
        self.assertEqual(0, invalid_payload["chunks"])
        self.assertEqual(
            "artifact-invalid", invalid_payload["review_resume"]["blocker"]
        )
        self.assertIn(
            "exceed the maximum of 8",
            invalid_payload["review_resume"]["artifact_error"],
        )
        self.assertEqual(
            1, invalid_payload["review_resume"]["historical_materializations"]
        )

        # Artifact correction is external. Preserve all nine findings while
        # normalizing them from nine repair groups to the supported eight.
        findings[-1]["repair_group"] = "group-8"
        artifact_path.write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": findings},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        resumed, resumed_payload = self._run_autopilot_review(
            max_review_fix_cycles=2,
            review_outcomes="findings,all-clear,all-clear",
        )
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertTrue(resumed_payload["ok"])
        self.assertEqual("complete", resumed_payload["reason"])
        self.assertEqual(1, resumed_payload["chunks"])
        self.assertEqual("all-clear", resumed_payload["review"]["outcome"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        for index in range(1, 9):
            child = self.plan_dir / f"session-3.{index}.md"
            self.assertTrue(child.is_file(), child)
            self.assertIn("- **Status**: complete", child.read_text(encoding="utf-8"))
        self.assertIn(
            "- **Status**: complete",
            (self.plan_dir / "session-4.md").read_text(encoding="utf-8"),
        )

        final_records = [
            json.loads(line)
            for line in event_path.read_text(encoding="utf-8").splitlines()
        ]
        reopen = [
            record["payload"]
            for record in final_records
            if record["name"] == events.REVIEW_REPAIR_REOPENED_EVENT
            and record["payload"].get("materialized") is True
            and record["payload"].get("source_review_session_id") == "3"
        ]
        self.assertEqual(1, len(reopen))
        self.assertEqual(1, reopen[0]["historical_materializations_before"])
        self.assertEqual(2, reopen[0]["historical_materializations_after"])
        self.assertIsNone(reopen[0]["source_review_output_sha256"])
        self.assertIsNone(reopen[0]["source_review_artifact_sha256"])
        self.assertEqual(
            [f"3.{index}" for index in range(1, 9)],
            reopen[0]["repair_session_ids"],
        )
