"""Assurance pinning: open findings keep an otherwise reclaimable run."""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    retention,
    retention_projection,
)
from agent_session_loop import assurance_projection as projection
from tests.asl_retention_support import (
    AUDIT_STAMP,
    NOW,
    RUN_ABSENT,
    RUN_NEW,
    RUN_OLD,
    RetentionTestBase,
    _stamp,
)

ASL_TEST_PORTABLE = True


class AssurancePinningTests(RetentionTestBase):
    """Open issues pin their evidence, and an unreadable ledger pins everything.

    The ledger is never swept in either case; what changes is who is allowed to
    be swept while it cannot be read, which is nobody.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ledger_path = ledger.ledger_path(self.state_root)

    def _publish(self, run_id: str, *, invariant_key: str = "retry.budget") -> None:
        finding = assurance_models_findings.Finding(
            category="lifecycle",
            component="agent_session_loop/runner.py",
            invariant_key=invariant_key,
            lane="lifecycle-retry-resume",
            severity="high",
            summary="A retry ran after the budget was exhausted.",
            recommendation="Clamp the retry counter before relaunching.",
            evidence=("events.jsonl",),
        )
        projection.publish_audit(
            projection.AuditPublication(
                run_id=run_id,
                state="findings",
                eligible=True,
                findings=(finding,),
                completed_at=AUDIT_STAMP,
            ),
            state_dir=self.state_root,
            recorded_at=AUDIT_STAMP,
        )

    def test_open_issue_pins_an_otherwise_reclaimable_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)

        runs = self._runs_by_id(self._report())

        self.assertFalse(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, retention_projection.RETAIN_OPEN_ISSUE)
        self.assertIn("open assurance issue", runs[RUN_OLD].detail)

    def test_closing_the_issue_releases_the_run(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_root, recorded_at=AUDIT_STAMP
        )

        runs = self._runs_by_id(self._report())

        self.assertTrue(runs[RUN_OLD].reclaimable)
        self.assertEqual(runs[RUN_OLD].reason, "")

    def test_apply_keeps_the_ledger_and_the_pinned_run(self) -> None:
        pinned_dir = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        sweepable = self._make_plan("sweepable")
        removable = self._write_run(
            RUN_OLD, finished_at=_stamp(45), plan_dir=sweepable, log_bytes=100
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=sweepable)
        self._publish(RUN_OLD)
        before = self.ledger_path.read_bytes()

        report = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertTrue(pinned_dir.is_dir())
        # Two plans share the pinned run ID, so both directories are held.
        self.assertTrue(removable.is_dir())
        self.assertEqual(report.removed_run_count, 0)
        self.assertTrue(report.assurance["ledger_excluded"])
        self.assertEqual(report.assurance["ledger_path"], str(self.ledger_path))
        self.assertEqual(report.assurance["open_issue_count"], 1)
        self.assertEqual(report.assurance["pinned_run_count"], 1)
        self.assertEqual(report.assurance["missing_run_references"], [])
        json.dumps(report.to_dict())

    def test_history_survives_the_run_it_pointed_at(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_root, recorded_at=AUDIT_STAMP
        )

        report = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(report.removed_run_count, 1)
        self.assertTrue(self.ledger_path.exists())
        inbox = projection.read_projection(state_dir=self.state_root)
        self.assertEqual(len(inbox.issues), 1)
        self.assertEqual(inbox.issues[0].run_ids, (RUN_OLD,))

    def test_a_reference_with_no_run_directory_is_reported(self) -> None:
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_ABSENT)

        report = retention.run_gc(state_dir=self.state_root, now=NOW)

        self.assertEqual(report.assurance["pinned_run_count"], 1)
        self.assertEqual(report.assurance["missing_run_references"], [RUN_ABSENT])

    def test_plan_scope_resolves_references_against_the_whole_root(self) -> None:
        other_plan = self._make_plan("other-plan")
        self._write_run(RUN_OLD, finished_at=_stamp(45), plan_dir=other_plan)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._publish(RUN_OLD)

        report = retention.run_gc(self.plan_dir, state_dir=self.state_root, now=NOW)

        self.assertEqual(report.scope, "plan")
        self.assertEqual(report.assurance["missing_run_references"], [])

    def _break_the_ledger(self) -> None:
        """Make the ledger unreadable without making it look absent.

        A missing ledger is a readable empty inbox; only a ledger that exists
        and cannot be parsed leaves the pin question unanswered.
        """
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.ledger_path.mkdir()

    def _reasons(self, report: retention.GCReport) -> set[str]:
        return {run.reason for entry in report.plans for run in entry.retention.runs}

    def test_unreadable_ledger_blocks_collection_and_says_so(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._break_the_ledger()

        report = retention.run_gc(state_dir=self.state_root, now=NOW)

        self.assertFalse(report.assurance["available"])
        self.assertTrue(report.assurance["error"])
        self.assertTrue(report.assurance["collection_blocked"])
        self.assertEqual(report.assurance["pinned_run_count"], 0)
        self.assertEqual(report.reclaimable_run_count, 0)
        self.assertEqual(
            self._reasons(report), {retention_projection.RETAIN_PIN_UNKNOWN}
        )
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertTrue(payload["assurance"]["collection_blocked"])
        self.assertEqual(
            payload["retained_by_reason"][retention_projection.RETAIN_PIN_UNKNOWN][
                "run_count"
            ],
            2,
        )

    def test_dry_run_output_says_collection_is_blocked(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._break_the_ledger()
        args = argparse.Namespace(
            plan_dir=None,
            state_dir=self.state_root,
            max_age_days=retention_projection.DEFAULT_MAX_AGE_DAYS,
            apply=False,
            json=False,
        )

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = retention.gc_from_args(args)
        text = buffer.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertIn("collection is blocked", text)
        self.assertIn(retention_projection.RETAIN_PIN_UNKNOWN, text)
        self.assertNotIn("Re-run with --apply", text)

    def test_plan_scope_apply_removes_nothing_when_the_ledger_is_unreadable(
        self,
    ) -> None:
        old = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        new = self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._break_the_ledger()

        report = retention.run_gc(
            self.plan_dir, state_dir=self.state_root, apply=True, now=NOW
        )

        self.assertEqual(report.scope, "plan")
        self.assertEqual(report.removed_run_count, 0)
        self.assertEqual(report.reclaimed_bytes, 0)
        self.assertEqual(report.failure_count, 0)
        self.assertTrue(old.is_dir())
        self.assertTrue(new.is_dir())
        self.assertEqual(
            self._reasons(report), {retention_projection.RETAIN_PIN_UNKNOWN}
        )

    def test_corpus_scope_apply_removes_nothing_when_the_ledger_is_unreadable(
        self,
    ) -> None:
        first = self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        other_plan = self._make_plan("other-plan")
        second = self._write_run(
            RUN_OLD, finished_at=_stamp(45), plan_dir=other_plan, log_bytes=100
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=other_plan)
        self._break_the_ledger()

        blocked = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(blocked.scope, "corpus")
        self.assertEqual(blocked.removed_run_count, 0)
        self.assertEqual(blocked.failure_count, 0)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertEqual(
            self._reasons(blocked), {retention_projection.RETAIN_PIN_UNKNOWN}
        )

        # Both runs really were sweepable, so the block above held back a
        # deletion that would otherwise have happened against an empty pin set.
        self.ledger_path.rmdir()
        repaired = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(repaired.removed_run_count, 2)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())


if __name__ == "__main__":
    unittest.main()
