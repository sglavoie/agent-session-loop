"""Review cycle reopen baseline and legacy lineage tests."""

import argparse
import json
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans,
    review,
    review_continuation,
    review_cycle,
    review_delta,
    review_repair,
    review_session_body,
    run_persistence,
    session_verification,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _structured_review_finding,
    write_plan_with_ids,
)


class ReviewCycleBaselineTests(RepairCycleFixtures, unittest.TestCase):
    """Recover post-reopen baselines and count legacy cycle events."""

    def test_post_reopen_baseline_resolves_lineage_less_cycle_source(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2", "3", "4"),
        )
        snapshot = plans.inspect_plan(self.plan_dir)

        def write_history(
            name: str,
            *,
            reopen_source: str | None,
            reopen_cycle: int | None,
        ) -> Path:
            event_path = self.tmp_path / f"lineage-less-{name}.jsonl"
            reopen_payload: dict[str, Any] = {
                "materialized": True,
                "current_fingerprints": [f"finding:{name}"],
            }
            if reopen_source is not None:
                reopen_payload["source_review_session_id"] = reopen_source
            if reopen_cycle is not None:
                reopen_payload["source_cycle"] = reopen_cycle
            event_path.write_text(
                "".join(
                    json.dumps(record) + "\n"
                    for record in (
                        {
                            "name": events.REVIEW_CYCLE_EVENT,
                            "payload": {
                                "cycle": 2,
                                "fingerprints": ["finding:legacy"],
                            },
                        },
                        {
                            "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                            "payload": reopen_payload,
                        },
                    )
                ),
                encoding="utf-8",
            )
            return event_path

        newer_path = write_history("newer", reopen_source="4", reopen_cycle=None)
        self.assertEqual(
            ("finding:newer",),
            review_delta._review_cycle_state(newer_path, snapshot)[
                "previous_fingerprints"
            ],
        )
        self.assertEqual(
            ("finding:newer",),
            review_delta._review_cycle_state(newer_path)["previous_fingerprints"],
        )

        same_path = write_history("same", reopen_source="3", reopen_cycle=None)
        self.assertEqual(
            ("finding:same",),
            review_delta._review_cycle_state(same_path, snapshot)[
                "previous_fingerprints"
            ],
        )

        older_path = write_history("older", reopen_source="2", reopen_cycle=None)
        self.assertEqual(
            ("finding:legacy",),
            review_delta._review_cycle_state(older_path, snapshot)[
                "previous_fingerprints"
            ],
        )

        mismatch_path = write_history(
            "numeric-mismatch", reopen_source=None, reopen_cycle=99
        )
        self.assertEqual(
            ("finding:legacy",),
            review_delta._review_cycle_state(mismatch_path, snapshot)[
                "previous_fingerprints"
            ],
        )

    def test_post_reopen_baseline_without_readable_reviews_orders_sources(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2", "4"),
        )
        snapshot = plans.inspect_plan(self.plan_dir)
        for review_id in ("2", "4"):
            (self.plan_dir / f"session-{review_id}.md").unlink()
        self.assertEqual({}, review_continuation._generated_review_statuses(snapshot))

        for name, sources in (
            ("older-then-newer", ("2", "4")),
            ("newer-then-older", ("4", "2")),
        ):
            with self.subTest(name=name):
                event_path = self.tmp_path / f"unreadable-reviews-{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(record) + "\n"
                        for record in (
                            {
                                "name": events.REVIEW_CYCLE_EVENT,
                                "payload": {
                                    "cycle": 2,
                                    "fingerprints": ["finding:legacy"],
                                },
                            },
                            *(
                                {
                                    "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                                    "payload": {
                                        "source_review_session_id": source,
                                        "materialized": True,
                                        "current_fingerprints": [
                                            f"finding:source-{source}"
                                        ],
                                    },
                                }
                                for source in sources
                            ),
                        )
                    ),
                    encoding="utf-8",
                )

                self.assertEqual(
                    ("finding:source-4",),
                    review_delta._review_cycle_state(event_path, snapshot)[
                        "previous_fingerprints"
                    ],
                )

    def test_zero_cycle_reopen_baseline_orders_sources(self) -> None:
        for name, sources in (
            ("older-then-newer", ("2", "4")),
            ("newer-then-older", ("4", "2")),
        ):
            with self.subTest(name=name):
                event_path = self.tmp_path / f"zero-cycle-{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(
                            {
                                "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                                "payload": {
                                    "source_review_session_id": source,
                                    "materialized": True,
                                    "current_fingerprints": [
                                        f"finding:source-{source}"
                                    ],
                                },
                            }
                        )
                        + "\n"
                        for source in sources
                    ),
                    encoding="utf-8",
                )

                state_payload = review_delta._review_cycle_state(event_path)

                self.assertEqual(1, state_payload["cycle"])
                self.assertEqual(
                    ("finding:source-4",),
                    state_payload["previous_fingerprints"],
                )

    def test_legacy_event_without_lineage_counts_once_with_children(
        self,
    ) -> None:
        write_plan_with_ids(
            self.plan_dir,
            (("1", "complete"), ("2", "complete"), ("2.1", "pending")),
        )
        review_body = review_session_body.build_generated_review_session_body(
            plans.parse_session_id("2"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
        child_path = self.plan_dir / "session-2.1.md"
        child_path.write_text(
            review_repair.REVIEW_REPAIR_SESSION_MARKER
            + "\n"
            + review_repair.REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(
                version=review_repair.GENERATED_REVIEW_REPAIR_SESSION_VERSION
            )
            + "\n"
            + child_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        event_path = self.tmp_path / "events.jsonl"
        event_path.write_text(
            json.dumps(
                {
                    "name": events.REVIEW_CYCLE_EVENT,
                    "payload": {
                        "cycle": 1,
                        "disposition": (
                            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                        ),
                        "fingerprints": ["finding:legacy"],
                        "max_review_fix_cycles": 1,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        snapshot = plans.inspect_plan(self.plan_dir)

        event_only = review_continuation.review_cycle_state(event_path)
        reconciled = review_continuation.review_cycle_state(event_path, snapshot)
        projected = review_continuation.evaluate_review_continuation(
            snapshot, event_path
        )

        self.assertEqual(1, event_only["repairs_materialized"])
        self.assertEqual(
            event_only["repairs_materialized"],
            reconciled["repairs_materialized"],
        )
        self.assertEqual(1, projected.historical_materializations)
        self.assertEqual(2, projected.minimum_required_cap)

    def test_cap_enforcement_counts_children_after_cycle_event_crash(
        self,
    ) -> None:
        finding = _structured_review_finding()
        write_plan_with_ids(
            self.plan_dir,
            (
                ("1", "complete"),
                ("2", "complete"),
                ("2.1", "complete"),
                ("3", "complete"),
            ),
        )
        for review_id in ("2", "3"):
            review_body = review_session_body.build_generated_review_session_body(
                plans.parse_session_id(review_id),
                review_harness="claude",
                plan_dir=self.plan_dir,
            ).replace("- **Status**: pending", "- **Status**: complete")
            (self.plan_dir / f"session-{review_id}.md").write_text(
                review_body, encoding="utf-8"
            )
        child_path = self.plan_dir / "session-2.1.md"
        child_path.write_text(
            review_repair.REVIEW_REPAIR_SESSION_MARKER
            + "\n"
            + review_repair.REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(
                version=review_repair.GENERATED_REVIEW_REPAIR_SESSION_VERSION
            )
            + "\n"
            + child_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_asl_review.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.state_dir / "events.jsonl"
        snapshot = plans.inspect_plan(self.plan_dir)
        projected_before = review_continuation.evaluate_review_continuation(
            snapshot, event_path, max_review_fix_cycles=1
        )
        self.assertEqual(1, projected_before.historical_materializations)
        self.assertEqual(2, projected_before.minimum_required_cap)

        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }
        action, after = review_cycle._advance_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.state_dir), allow_not_loop_ready=False
            ),
            cwd=self.repo_root,
            session_id="3",
            session_label="session 3",
            session_details={"session": "3"},
            snapshot=snapshot,
            turn_payload={
                "review_verdict": {
                    "passed": True,
                    "outcome": "findings",
                    "total_findings": 1,
                    "findings": [finding],
                }
            },
            max_review_fix_cycles=1,
            plan_cache=plans.PlanSnapshotCache(),
            reinspect=session_verification._reinspect_after_plan_mutation,
        )

        self.assertEqual("complete", action)
        self.assertIs(snapshot, after)
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED,
            payload["review_cycles"][0]["disposition"],
        )
        enforced = review_delta._review_cycle_state(event_path, snapshot)
        projected_after = review_continuation.evaluate_review_continuation(
            snapshot, event_path, max_review_fix_cycles=1
        )
        self.assertEqual(
            projected_after.historical_materializations,
            enforced["repairs_materialized"],
        )
        self.assertEqual(1, enforced["repairs_materialized"])


if __name__ == "__main__":
    unittest.main()
