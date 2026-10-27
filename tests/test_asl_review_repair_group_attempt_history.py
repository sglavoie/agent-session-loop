"""Repair group attempt history across review cycles."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_models,
    plans_session_ids,
    plans_snapshot,
    review,
    review_continuation,
    review_continuation_state,
    review_cycle,
    review_repair_planning,
    review_session_body,
    run_persistence,
    session_verification,
)
from tests.support import (
    _structured_review_finding,
    write_completed_review_plan,
)

ASL_TEST_LANE = "integration"


class GroupAttemptCapStateTests(unittest.TestCase):
    def test_a_non_utf8_state_yields_no_recorded_cap(self) -> None:
        # Bytes that are not UTF-8 used to escape as UnicodeDecodeError instead
        # of reading as an unrecorded cap, like a torn state already did.
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            state_path = event_path.with_name(events.STATE_FILE_NAME)
            state_path.write_bytes(
                b'{"run_config": {"max_repair_group_attempts": 2, "x": "\xff"}}'
            )

            self.assertIsNone(
                review_continuation_state._selected_group_attempt_cap_from_history(
                    (), event_path
                )
            )

    def test_an_over_deep_state_yields_no_recorded_cap(self) -> None:
        # Deep enough that the decoder raises RecursionError, not a JSON error.
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            event_path.with_name(events.STATE_FILE_NAME).write_text(
                "[" * 1_000_000, encoding="utf-8"
            )

            self.assertIsNone(
                review_continuation_state._selected_group_attempt_cap_from_history(
                    (), event_path
                )
            )


class ReviewRepairGroupAttemptHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.event_path = self.tmp_path / events.EVENTS_FILE_NAME

    def _write_repair_children(
        self, findings: list[dict[str, Any]]
    ) -> plans_models.PlanSnapshot:
        write_completed_review_plan(self.plan_dir, findings)
        repair_plan = review_repair_planning.plan_review_repair_sessions(self.plan_dir)
        self.assertTrue(repair_plan.ok, repair_plan.error)
        for group in repair_plan.repair_groups:
            child_path = self.plan_dir / group.proposed_child_id.filename
            child_path.write_text(group.rendered_text, encoding="utf-8")
        return plans_snapshot.inspect_plan(self.plan_dir)

    def test_repair_group_attempts_reads_event_history(self) -> None:
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1", "2.2"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "2.1",
                            "finding_ids": ["F1"],
                        },
                        {
                            "repair_group": "docs",
                            "repair_session_id": "2.2",
                            "finding_ids": ["F2"],
                        },
                    ],
                },
            ),
        )
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_review_session_id": "4",
                    "materialized": True,
                    "repair_session_ids": ["4.1"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "4.1",
                            "finding_ids": ["F3"],
                        }
                    ],
                },
            ),
        )

        self.assertEqual(
            {"docs": 1, "runner": 2},
            review_continuation.repair_group_attempts(self.event_path),
        )

    def test_repair_group_attempts_recovers_registry_free_children(
        self,
    ) -> None:
        snapshot = self._write_repair_children(
            [
                _structured_review_finding(finding_id="F1", repair_group="runner"),
                _structured_review_finding(finding_id="F2", repair_group="docs"),
            ]
        )

        self.assertEqual(
            {"docs": 1, "runner": 1},
            review_continuation.repair_group_attempts(self.event_path, snapshot),
        )

    def test_repair_group_attempts_deduplicates_events_and_children(
        self,
    ) -> None:
        snapshot = self._write_repair_children(
            [_structured_review_finding(repair_group="runner")]
        )
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1", "2.2"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "2.1",
                            "finding_ids": ["F1"],
                        },
                        {
                            "repair_group": "docs",
                            "repair_session_id": "2.2",
                            "finding_ids": ["F2"],
                        },
                    ],
                },
            ),
        )

        self.assertEqual(
            {"docs": 1, "runner": 1},
            review_continuation.repair_group_attempts(self.event_path, snapshot),
        )

    def test_advance_review_cycle_stops_when_group_attempt_cap_is_exhausted(
        self,
    ) -> None:
        initial = _structured_review_finding(finding_id="F1", repair_group="runner")
        rereview = _structured_review_finding(
            finding_id="F2",
            repair_group="runner",
            title="A different runner failure still needs repair",
            evidence=[{"path": "pkg/F2.py", "symbol": "retry_loop"}],
            explanation="F2 is still broken in a different way.",
            done_criteria=["F2 is fixed and tested."],
        )
        self._write_repair_children([initial])
        child_path = self.plan_dir / "session-2.1.md"
        child_path.write_text(
            child_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        review_body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("3"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-3.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- pkg/F2.py still fails.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [rereview]}),
            encoding="utf-8",
        )
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "review_session_id": "2",
                    "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "fingerprints": [review.finding_fingerprint(initial)],
                    "repair_session_ids": ["2.1"],
                    "repair_groups": [
                        {
                            "repair_group": "runner",
                            "repair_session_id": "2.1",
                            "finding_ids": ["F1"],
                        }
                    ],
                    "max_review_fix_cycles": 4,
                },
            ),
        )
        snapshot = plans_snapshot.inspect_plan(self.plan_dir)
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }

        action, after = review_cycle._advance_review_cycle(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.tmp_path / "state"),
                allow_not_loop_ready=False,
                max_repair_group_attempts=1,
            ),
            cwd=self.tmp_path,
            session_id="3",
            session_label="session 3",
            session_details={"session": "3"},
            snapshot=snapshot,
            turn_payload={
                "review_verdict": {
                    "passed": True,
                    "outcome": "findings",
                    "total_findings": 1,
                    "findings": [rereview],
                }
            },
            max_review_fix_cycles=4,
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            reinspect=session_verification._reinspect_after_plan_mutation,
        )

        self.assertEqual("complete", action)
        self.assertIs(snapshot, after)
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())
        cycle = payload["review_cycles"][0]
        assert isinstance(cycle, dict)
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_GROUP_EXHAUSTED,
            cycle["disposition"],
        )
        self.assertEqual([], cycle["repair_session_ids"])
        self.assertEqual(1, cycle["max_repair_group_attempts"])
        self.assertEqual("runner", cycle["exhausted_repair_group"])
        self.assertEqual(1, cycle["exhausted_repair_group_attempts"])
        self.assertEqual(
            [
                {
                    "repair_group": "runner",
                    "repair_session_id": "3.1",
                    "finding_ids": ["F2"],
                }
            ],
            cycle["repair_groups"],
        )
        # The refused cycle created no child, so it is not another attempt.
        self.assertEqual(
            {"runner": 1},
            review_continuation.repair_group_attempts(self.event_path, self.plan_dir),
        )
