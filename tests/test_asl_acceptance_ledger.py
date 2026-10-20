"""Asl acceptance ledger regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_inbox,
    assurance_report,
)
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication

from .support_asl_acceptance import (
    INPUT_DIGEST,
    OTHER_DIGEST,
    RUN_A,
    RUN_B,
    T0,
    WHOLE_HISTORY,
    _artifacts,
    _finding,
)


class AppendOnlyLedgerTests(unittest.TestCase):
    """No canonical operation shortens the ledger or rewrites earlier bytes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name) / "state"
        self.path = ledger.ledger_path(self.state_dir)

    def _bytes(self) -> bytes:
        return self.path.read_bytes() if self.path.exists() else b""

    def _publish(self, run_id: str, digest: str, invariant_key: str) -> None:
        projection.publish_audit(
            AuditPublication(
                run_id=run_id,
                state="findings",
                eligible=True,
                findings=(_finding(invariant_key),),
                artifacts=_artifacts(digest),
                completed_at=T0,
            ),
            state_dir=self.state_dir,
            recorded_at=T0,
        )

    def test_every_canonical_operation_only_appends(self) -> None:
        before = self._bytes()
        steps = (
            ("publish an audit", lambda: self._publish(RUN_A, INPUT_DIGEST, "a.key")),
            (
                "publish a second audit",
                lambda: self._publish(RUN_B, OTHER_DIGEST, "b.key"),
            ),
            (
                "republish the first",
                lambda: self._publish(RUN_A, INPUT_DIGEST, "a.key"),
            ),
            (
                "resolve an issue",
                lambda: projection.record_disposition(
                    "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=T0
                ),
            ),
            (
                "merge one issue into another",
                lambda: projection.record_merge(
                    "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=T0
                ),
            ),
            (
                "close out a run",
                lambda: projection.record_closeout(
                    RUN_A,
                    "accepted",
                    issue_ids=("ASI-2",),
                    state_dir=self.state_dir,
                    recorded_at=T0,
                ),
            ),
            (
                "replace that closeout",
                lambda: projection.record_closeout(
                    RUN_A,
                    "reworked",
                    rework_minutes=15,
                    replace=True,
                    state_dir=self.state_dir,
                    recorded_at=T0,
                ),
            ),
            (
                "record a drafted proposal",
                lambda: projection.record_improvement_plan_draft(
                    assurance_models_inbox.ImprovementPlanDraft(
                        issue_id="ASI-2",
                        harness="claude",
                        output_path="/tmp/asi-2.md",
                        output_sha256="c" * 64,
                        drafted_at=T0,
                    ),
                    state_dir=self.state_dir,
                    recorded_at=T0,
                ),
            ),
        )

        records = 0
        for label, operation in steps:
            operation()
            after = self._bytes()
            with self.subTest(operation=label):
                self.assertTrue(
                    after.startswith(before), f"{label} rewrote earlier bytes"
                )
                view = ledger.read_ledger(state_dir=self.state_dir)
                self.assertGreaterEqual(len(view.records), records)
                records = len(view.records)
            before = after

        final = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual(records, len(final.records))

    def test_reading_the_inbox_never_writes_to_it(self) -> None:
        self._publish(RUN_A, INPUT_DIGEST, "a.key")
        before = self._bytes()

        ledger.read_ledger(state_dir=self.state_dir)
        projection.read_projection(state_dir=self.state_dir)
        assurance_report.collect_assurance_metrics(
            state_dir=self.state_dir, since=WHOLE_HISTORY
        )

        self.assertEqual(before, self._bytes())


class PreFeatureCompatibilityTests(unittest.TestCase):
    """A state root written before assurance existed is an empty inbox."""

    def test_a_state_root_without_a_ledger_reads_as_an_empty_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "planhash").mkdir()

            inbox = projection.read_projection(state_dir=state_dir)
            metrics = assurance_report.collect_assurance_metrics(
                state_dir=state_dir, since=WHOLE_HISTORY
            )

            self.assertEqual((), inbox.issues)
            self.assertTrue(metrics["available"])
            self.assertFalse(ledger.ledger_path(state_dir).exists())


if __name__ == "__main__":
    unittest.main()
