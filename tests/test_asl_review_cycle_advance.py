"""Review cycle advance and withdrawn repair tests."""

import argparse
import json
import unittest
from typing import Any

from agent_session_loop import (
    events,
    plans_session_ids,
    plans_snapshot,
    review_continuation,
    review_cycle,
    review_session_body,
    run_persistence,
    session_verification,
    workbench_recovery_build,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _discovered,
    _structured_review_finding,
    read_jsonl,
    write_completed_review_plan,
    write_plan_with_ids,
)


class ReviewCycleAdvanceTests(RepairCycleFixtures, unittest.TestCase):
    """Advance review cycles and record withdrawn repairs."""

    def test_advance_review_cycle_pauses_on_malformed_repair(self) -> None:
        # Pre-existing children trip the materializer's idempotency guard,
        # which is one of the fail-closed malformed-review-repair paths.
        finding = _structured_review_finding()
        write_plan_with_ids(
            self.plan_dir,
            (("1", "complete"), ("2.1", "pending")),
        )
        review_body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("2"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / "final-review.md").write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / "final-review.json").write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.state_dir / "events.jsonl"
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }
        turn_payload = {
            "review_verdict": {
                "passed": True,
                "outcome": "findings",
                "total_findings": 1,
                "findings": [finding],
            }
        }
        snapshot = plans_snapshot.inspect_plan(self.plan_dir)

        action, after = review_cycle._advance_review_cycle(
            event_path=event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.state_dir), allow_not_loop_ready=False
            ),
            cwd=self.repo_root,
            session_id="2",
            session_label="session 2",
            session_details={"session": "2"},
            snapshot=snapshot,
            turn_payload=turn_payload,
            max_review_fix_cycles=2,
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            reinspect=session_verification._reinspect_after_plan_mutation,
        )

        self.assertEqual("paused", action)
        # The pause reports a re-read of the plan rather than the snapshot the
        # cycle started from, because materialization already wrote to it.
        self.assertIsNot(snapshot, after)
        self.assertEqual(
            plans_snapshot.inspect_plan(self.plan_dir).fingerprint, after.fingerprint
        )
        self.assertEqual("malformed-review-repair", payload["pause_reason"])
        cycles = payload["review_cycles"]
        self.assertEqual(1, len(cycles))
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            cycles[0]["disposition"],
        )
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_MALFORMED,
            payload["review_fix_disposition"],
        )
        event_names = [record["name"] for record in read_jsonl(event_path)]
        self.assertIn(events.REVIEW_CYCLE_EVENT, event_names)
        self.assertIn(events.RUN_PAUSED_EVENT, event_names)

    def test_a_blocked_gate_withdraws_the_children_and_projects_the_old_plan(
        self,
    ) -> None:
        """ASI-136: the pause must not leave a repair child no run can select."""
        action, after, payload, event_path = self._advance_blocked_review_cycle(
            allow_not_loop_ready=False
        )

        self.assertEqual("paused", action)
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        self.assertFalse((self.plan_dir / "session-2.1.md").exists())

        # The children were withdrawn, so every projection has to name the plan
        # that is actually on disk: the one the cycle started from.
        self.assertEqual(
            plans_snapshot.inspect_plan(self.plan_dir).fingerprint, after.fingerprint
        )
        self.assertNotIn("2.1", [item.id for item in after.sessions])
        projected = payload["final_snapshot"]
        self.assertEqual(
            [item.id for item in after.sessions],
            [item["id"] for item in projected["sessions"]],
        )
        self.assertNotEqual("2.1", projected["next_pending_id"])
        paused = [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ][0]
        self.assertEqual([], paused["details"]["repair_session_ids"])
        self.assertTrue(paused["details"]["repair_children_rolled_back"])
        self.assertIn("session-2.1.md", paused["details"]["repair_rollback"]["removed"])
        self.assertIn("withdrawn after authoring", paused["message"])

    def test_a_withdrawn_cycle_records_the_repairs_it_planned(self) -> None:
        """ASI-147: an empty child list must not erase the planned groups."""
        _action, _after, payload, event_path = self._advance_blocked_review_cycle(
            allow_not_loop_ready=False
        )

        cycle = payload["review_cycles"][-1]
        # What the cycle materialized is still nothing, because the children
        # were taken back off disk.
        self.assertEqual([], cycle["repair_groups"])
        self.assertEqual([], cycle["repair_session_ids"])
        # What it planned survives beside that, so the durable lineage says four
        # repairs were specified rather than that none ever existed.
        self.assertEqual(
            ["2.1"],
            cycle["withdrawn_repair_session_ids"],
        )
        self.assertEqual(
            ["2.1"],
            [group["repair_session_id"] for group in cycle["withdrawn_repair_groups"]],
        )
        self.assertTrue(cycle["repair_children_rolled_back"])
        recorded = [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == events.REVIEW_CYCLE_EVENT
        ][-1]
        self.assertEqual(
            cycle["withdrawn_repair_groups"], recorded["withdrawn_repair_groups"]
        )

    def test_a_materialized_cycle_records_no_withdrawn_repairs(self) -> None:
        """The withdrawal keys are absent when the children actually landed."""
        _action, _after, payload, _event_path = self._advance_blocked_review_cycle(
            allow_not_loop_ready=True
        )

        cycle = payload["review_cycles"][-1]
        self.assertEqual(["2.1"], cycle["repair_session_ids"])
        self.assertNotIn("withdrawn_repair_groups", cycle)
        self.assertNotIn("withdrawn_repair_session_ids", cycle)
        self.assertNotIn("repair_children_rolled_back", cycle)

    def test_allow_not_loop_ready_keeps_projecting_the_materialized_children(
        self,
    ) -> None:
        action, after, payload, _event_path = self._advance_blocked_review_cycle(
            allow_not_loop_ready=True
        )

        self.assertEqual("continue", action)
        self.assertTrue((self.plan_dir / "session-2.1.md").exists())
        self.assertEqual(
            plans_snapshot.inspect_plan(self.plan_dir).fingerprint, after.fingerprint
        )
        self.assertIn("2.1", [item.id for item in after.sessions])
        projected = payload["final_snapshot"]
        self.assertEqual(
            [item.id for item in after.sessions],
            [item["id"] for item in projected["sessions"]],
        )
        self.assertEqual("2.1", projected["next_pending_id"])

    def test_legacy_cycle_does_not_claim_newer_eventless_review(self) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2", "3"),
            repair_ids=("2.1",),
        )
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_CYCLES_DISABLED
                    ),
                    "fingerprints": ["finding:legacy"],
                    "repair_session_ids": [],
                    "max_review_fix_cycles": 0,
                },
            ),
        )
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": 1,
                    "source_review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "materialized": True,
                    "before_max_review_fix_cycles": 0,
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=2,
        )

        self.assertEqual("3", projected.review_session_id)
        self.assertIsNone(projected.source_cycle)
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("all-clear", projected.source_disposition)
        self.assertEqual("all-clear", projected.blocker)

        reopen_events_before = self._review_reopen_events()
        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=[
                "--allow-not-loop-ready",
                "--max-review-fix-cycles",
                "2",
            ],
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("3", payload["review_resume"]["review_session_id"])
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        self.assertEqual(reopen_events_before, self._review_reopen_events())
        self.assertTrue(
            all(
                record.get("materialized") is True
                for record in self._review_reopen_events()
            )
        )

    def test_legacy_cycle_source_resolution_preserves_controls(self) -> None:
        cases = (
            (
                "one-review-legacy",
                ("2",),
                ({"cycle": 1, "disposition": "all-clear"},),
                "2",
                1,
            ),
            (
                "explicit-lineage",
                ("2", "3"),
                (
                    {
                        "cycle": 1,
                        "review_session_id": "3",
                        "disposition": "all-clear",
                    },
                ),
                "3",
                1,
            ),
            (
                "multiple-legacy-positions",
                ("2", "3"),
                (
                    {"cycle": 1, "disposition": "all-clear"},
                    {"cycle": 2, "disposition": "all-clear"},
                ),
                "3",
                2,
            ),
        )
        for name, review_ids, cycle_payloads, source_id, source_cycle in cases:
            with self.subTest(name=name):
                plan_dir = self.tmp_path / name
                self._write_completed_review_history(plan_dir, review_ids=review_ids)
                event_path = self.tmp_path / f"{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(
                            {
                                "name": events.REVIEW_CYCLE_EVENT,
                                "payload": payload,
                            }
                        )
                        + "\n"
                        for payload in cycle_payloads
                    ),
                    encoding="utf-8",
                )

                projected = review_continuation.evaluate_review_continuation(
                    plans_snapshot.inspect_plan(plan_dir), event_path
                )

                self.assertEqual(source_id, projected.review_session_id)
                self.assertEqual(source_cycle, projected.source_cycle)
                self.assertEqual("all-clear", projected.source_disposition)
                self.assertEqual("terminal", projected.action)
                self.assertEqual("all-clear", projected.blocker)

    def test_eventless_all_clear_review_projects_terminal(self) -> None:
        write_completed_review_plan(self.plan_dir, [])
        event_path = self._events_path()
        self.assertFalse(event_path.exists())

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )

        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("all-clear", projected.source_disposition)
        self.assertEqual("all-clear", projected.blocker)
        recovery = workbench_recovery_build.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("none", recovery.kind)

        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=["--allow-not-loop-ready"],
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("all-clear", payload["review_resume"]["blocker"])
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        self.assertEqual([], self._review_reopen_events())


if __name__ == "__main__":
    unittest.main()
