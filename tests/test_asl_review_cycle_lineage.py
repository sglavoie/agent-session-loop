"""Review cycle repair lineage and resplit tests."""

import unittest

from agent_session_loop import (
    events,
    plans_payloads,
    plans_snapshot,
    review_continuation,
    review_repair,
    review_transitions,
    workbench_recovery_build,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _discovered,
)


class ReviewCycleLineageTests(RepairCycleFixtures, unittest.TestCase):
    """Resolve repair children, resplits and eventless reviews."""

    def test_eventless_all_clear_defers_to_durable_repair_children(
        self,
    ) -> None:
        cases = (
            (
                "pending",
                (("2.1", "pending"),),
                "run-repairs",
                True,
                None,
            ),
            (
                "complete",
                (("2.1", "complete"),),
                "rereview",
                True,
                None,
            ),
            (
                "mixed",
                (("2.1", "complete"), ("2.2", "blocked")),
                "terminal",
                False,
                "repair-sessions-not-runnable",
            ),
        )
        for name, child_statuses, action, eligible, blocker in cases:
            with self.subTest(name=name):
                plan_dir = self.tmp_path / f"eventless-{name}"
                child_ids = tuple(session_id for session_id, _status in child_statuses)
                self._write_completed_review_history(
                    plan_dir,
                    review_ids=("2",),
                    repair_ids=child_ids,
                )
                for child_id, status in child_statuses:
                    child_path = plan_dir / f"session-{child_id}.md"
                    child_path.write_text(
                        child_path.read_text(encoding="utf-8").replace(
                            "- **Status**: complete",
                            f"- **Status**: {status}",
                        ),
                        encoding="utf-8",
                    )

                projected = review_continuation.evaluate_review_continuation(
                    plans_snapshot.inspect_plan(plan_dir),
                    self.tmp_path / f"eventless-{name}.jsonl",
                )

                self.assertEqual("2", projected.review_session_id)
                self.assertIsNone(projected.source_cycle)
                self.assertEqual("all-clear", projected.source_disposition)
                self.assertEqual(action, projected.action)
                self.assertEqual(eligible, projected.eligible)
                self.assertEqual(blocker, projected.blocker)
                self.assertNotEqual("all-clear", projected.blocker)

    def test_eventless_all_clear_with_missing_reopen_children_pauses(
        self,
    ) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1",),
        )
        child_path = self.plan_dir / "session-2.1.md"
        self.assertTrue(child_path.is_file())
        child_path.unlink()
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": None,
                    "source_review_session_id": "2",
                    "source_disposition": None,
                    "repair_session_ids": ["2.1"],
                    "materialized": True,
                    "result": "ok",
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            event_path,
            max_review_fix_cycles=1,
        )

        self.assertEqual("2", projected.review_session_id)
        self.assertIsNone(projected.source_cycle)
        self.assertEqual("all-clear", projected.source_disposition)
        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("repair-sessions-missing", projected.blocker)
        self.assertEqual(("2.1",), projected.repair_session_ids)

        completed, payload = self._run_cycles(
            review_outcomes="all-clear",
            extra_args=[
                "--allow-not-loop-ready",
                "--max-review-fix-cycles",
                "1",
            ],
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_MALFORMED_REVIEW_REPAIR,
            payload["pause_reason"],
        )
        self.assertEqual("terminal", payload["review_resume"]["action"])
        self.assertEqual("repair-sessions-missing", payload["review_resume"]["blocker"])
        self.assertIsNone(payload["review_fix_disposition"])

        recovery = workbench_recovery_build.build_workbench_recovery(
            _discovered(self.plan_dir), state_dir=self.state_dir
        )
        self.assertEqual("review-continuation", recovery.kind)
        self.assertEqual("Review continuation is inconsistent", recovery.title)
        self.assertEqual("error", recovery.severity)
        self.assertEqual(payload["pause_message"], recovery.summary)
        self.assertEqual(payload["review_resume"], recovery.review_resume)

    def test_a_resplit_repair_child_is_satisfied_by_its_descendants(self) -> None:
        """A repair too big for one turn is resplit; the lineage still names it.

        Read literally that lineage describes a child set the plan lost, and the
        projection reported `repair-sessions-missing` on a plan whose every
        repair had in fact completed -- withholding the rereview the findings
        still required (ASI-146).
        """
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1",),
        )
        self._resplit_repair_child(
            self.plan_dir,
            "2.1",
            (("2.1.1", "complete"), ("2.1.2", "complete")),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            self._append_repair_lineage(("2.1",)),
            max_review_fix_cycles=1,
        )

        self.assertEqual("rereview", projected.action)
        self.assertTrue(projected.eligible)
        self.assertIsNone(projected.blocker)
        # The lineage is reported as recorded; the descendants are what the
        # completion accounting actually read.
        self.assertEqual(("2.1",), projected.repair_session_ids)
        self.assertEqual(("2.1.1", "2.1.2"), projected.completed_repair_session_ids)

    def test_a_pending_resplit_descendant_still_has_to_run(self) -> None:
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1",),
        )
        self._resplit_repair_child(
            self.plan_dir,
            "2.1",
            (("2.1.1", "complete"), ("2.1.2", "pending")),
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            self._append_repair_lineage(("2.1",)),
            max_review_fix_cycles=1,
        )

        self.assertEqual("run-repairs", projected.action)
        self.assertTrue(projected.eligible)
        self.assertEqual(("2.1.2",), projected.pending_repair_session_ids)

    def test_a_repair_child_lost_without_a_resplit_still_fails_closed(
        self,
    ) -> None:
        """Deleting the parent alone is the loss the blocker exists for."""
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1",),
        )
        (self.plan_dir / "session-2.1.md").unlink()

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            self._append_repair_lineage(("2.1",)),
            max_review_fix_cycles=1,
        )

        self.assertEqual("terminal", projected.action)
        self.assertFalse(projected.eligible)
        self.assertEqual("repair-sessions-missing", projected.blocker)

    def test_a_repair_child_that_lost_its_marker_still_fails_closed(self) -> None:
        """A parent still in the plan is never handed to its descendants.

        Only a plan that no longer holds the parent at all can have resplit it.
        A parent still present but stripped of its marker is a different
        failure, and it keeps the blocker it already had.
        """
        self._write_completed_review_history(
            self.plan_dir,
            review_ids=("2",),
            repair_ids=("2.1",),
        )
        repair_path = self.plan_dir / "session-2.1.md"
        repair_path.write_text(
            "\n".join(
                line
                for line in repair_path.read_text(encoding="utf-8").splitlines()
                if "asl:generated-review-repair-session" not in line
            ),
            encoding="utf-8",
        )
        body = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        (self.plan_dir / "session-2.1.1.md").write_text(
            body.replace("# Session 1:", "# Session 2.1.1:"),
            encoding="utf-8",
        )

        projected = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir),
            self._append_repair_lineage(("2.1",)),
            max_review_fix_cycles=1,
        )

        self.assertEqual("terminal", projected.action)
        self.assertEqual("repair-sessions-missing", projected.blocker)

    def test_findings_repairs_all_clear_completes_in_one_invocation(
        self,
    ) -> None:
        self._write_plan()

        completed, payload = self._run_cycles(review_outcomes="findings,all-clear")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        # Turn order: impl 1, review 2 (findings), repair 2.1, review 3.
        self.assertEqual(
            ["1", "2", "2.1", "3"],
            [turn["session_id"] for turn in payload["turns"]],
        )
        self.assertEqual(
            [
                plans_payloads.SESSION_KIND_IMPLEMENTATION,
                plans_payloads.SESSION_KIND_REVIEW,
                plans_payloads.SESSION_KIND_IMPLEMENTATION,
                plans_payloads.SESSION_KIND_REVIEW,
            ],
            [turn["session_kind"] for turn in payload["turns"]],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(2, len(cycles))
        self.assertEqual(1, cycles[0]["cycle"])
        self.assertEqual("2", cycles[0]["review_session_id"])
        self.assertEqual("findings", cycles[0]["outcome"])
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycles[0]["disposition"],
        )
        self.assertEqual(["2.1"], cycles[0]["repair_session_ids"])
        self.assertEqual(cycles[0]["fingerprints"], cycles[0]["introduced_findings"])
        self.assertEqual(2, cycles[1]["cycle"])
        self.assertEqual("3", cycles[1]["review_session_id"])
        self.assertEqual("all-clear", cycles[1]["outcome"])
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
            cycles[1]["disposition"],
        )
        self.assertEqual(cycles[0]["fingerprints"], cycles[1]["closed_findings"])
        child_path = self.plan_dir / "session-2.1.md"
        self.assertTrue(child_path.is_file())
        self.assertTrue(
            review_repair.is_generated_review_repair_session_file(child_path)
        )
        self.assertEqual(2, len(self._review_cycle_events()))
        self.assertEqual(["1", "2", "2.1", "3"], payload["completed_session_ids"])

    def test_repeated_findings_stop_without_new_cycle(self) -> None:
        self._write_plan()

        # The second review re-detects the same finding with drifted line
        # numbers; the normalized fingerprint must match and stop the loop.
        completed, payload = self._run_cycles(review_outcomes="findings,findings-drift")

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            payload["review_fix_disposition"],
        )
        cycles = payload["review_cycles"]
        self.assertEqual(2, len(cycles))
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            cycles[0]["disposition"],
        )
        self.assertEqual(
            review_continuation.REVIEW_CYCLE_DISPOSITION_REPEATED,
            cycles[1]["disposition"],
        )
        self.assertEqual(cycles[0]["fingerprints"], cycles[1]["repeated_findings"])
        self.assertEqual([], cycles[1]["repair_session_ids"])
        # One repair generation only: 2.1 exists, review 3 has no children.
        self.assertTrue((self.plan_dir / "session-2.1.md").is_file())
        self.assertFalse((self.plan_dir / "session-3.1.md").exists())

        # A larger unused cap cannot reopen the repeated-finding circuit
        # breaker or append another review on a later direct invocation.
        resumed, resumed_payload = self._run_cycles(
            review_outcomes="findings,findings-drift,all-clear",
            extra_args=["--max-review-fix-cycles", "8"],
        )
        self.assertEqual(1, resumed.returncode, resumed.stderr)
        self.assertEqual([], resumed_payload["turns"])
        self.assertEqual("terminal", resumed_payload["review_resume"]["action"])
        self.assertEqual(
            "findings-repeated", resumed_payload["review_resume"]["blocker"]
        )
        self.assertFalse((self.plan_dir / "session-4.md").exists())


if __name__ == "__main__":
    unittest.main()
