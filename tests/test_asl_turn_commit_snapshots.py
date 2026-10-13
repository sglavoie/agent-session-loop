"""Committed-turn immutability, publication snapshots and anomaly classification."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from typing import Any

from agent_session_loop import (
    plans_content_hash,
    plans_models,
    plans_payloads,
    plans_readiness,
    plans_snapshot,
    run_persistence,
    run_reporting,
    session_verification,
    supervisor_audit_evidence_failures,
    turn_execution_commit,
)
from tests.asl_turn_commit_support import SPECS
from tests.support import (
    write_multi_repo_plan,
)


class CommittedTurnImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        write_multi_repo_plan(self.plan_dir, ("complete",), specs=SPECS)
        self.snapshot = plans_snapshot.inspect_plan(self.plan_dir)

    def _commit(self, turn_payload: dict[str, Any]) -> Any:
        payload: dict[str, Any] = {"run_id": "run-1", "turns": [turn_payload]}
        commit = turn_execution_commit.commit_turn(
            event_path=self.tmp_path / "events.jsonl",
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            progress=run_reporting._ProgressReporter(enabled=False),
            turn_payload=turn_payload,
            live_turn=dict(turn_payload),
            accepted=self.snapshot,
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            paths={"result": self.tmp_path / "result.json"},
        )
        return commit.record, payload

    def test_commit_returns_the_published_snapshot_it_recorded(self) -> None:
        turn_payload = {"turn_index": 1, "session": "1"}
        payload: dict[str, Any] = {"run_id": "run-1", "turns": [turn_payload]}
        commit = turn_execution_commit.commit_turn(
            event_path=self.tmp_path / "events.jsonl",
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            progress=run_reporting._ProgressReporter(enabled=False),
            turn_payload=turn_payload,
            live_turn=dict(turn_payload),
            accepted=self.snapshot,
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            paths={"result": self.tmp_path / "result.json"},
        )
        self.assertIsInstance(commit, turn_execution_commit.TurnCommit)
        self.assertEqual(
            plans_payloads.snapshot_payload(commit.published), commit.record["after"]
        )

    def test_committed_turn_cannot_be_rewritten(self) -> None:
        committed, _payload = self._commit({"turn_index": 1, "session": "1"})
        self.assertIsInstance(committed, MappingProxyType)
        with self.assertRaises(TypeError):
            committed["session"] = "2"  # type: ignore[index]

    def test_committed_turn_replaces_the_in_flight_state_record(self) -> None:
        turn_payload = {"turn_index": 1, "session": "1"}
        committed, payload = self._commit(turn_payload)
        self.assertEqual(1, len(payload["turns"]))
        self.assertTrue(payload["turns"][0]["committed"])
        self.assertNotIn("committed", turn_payload)
        self.assertEqual(dict(committed), payload["turns"][0])

    def test_committed_turn_carries_the_accepted_snapshot(self) -> None:
        committed, _payload = self._commit(
            {"turn_index": 1, "session": "1", "after": {"stale": True}}
        )
        self.assertEqual(
            plans_payloads.snapshot_payload(self.snapshot), committed["after"]
        )


class PublicationSnapshotTests(unittest.TestCase):
    """The snapshot a turn is published against is re-read, and must still agree."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans_snapshot.inspect_plan(self.plan_dir)

    def publish(self) -> plans_models.PlanSnapshot:
        return turn_execution_commit.publication_snapshot(
            plans_snapshot.PlanSnapshotCache(), self.snapshot
        )

    def test_readiness_bookkeeping_is_republished_without_moving_identity(self) -> None:
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="running",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        published = self.publish()
        self.assertEqual(
            plans_content_hash.plan_content_sha256(self.plan_dir),
            published.content_sha256,
        )
        self.assertEqual(self.snapshot.content_sha256, published.content_sha256)

    def test_status_drift_refuses_the_turn(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(turn_execution_commit.TurnCommitRejected) as caught:
            self.publish()
        self.assertEqual("unexpected-status-change", caught.exception.reason)
        self.assertEqual(
            [{"number": 1, "before": "pending", "after": "complete"}],
            caught.exception.detail["status_changes"],
        )

    def test_unreadable_plan_refuses_the_turn_rather_than_raising_plan_error(
        self,
    ) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        with self.assertRaises(turn_execution_commit.TurnCommitRejected) as caught:
            self.publish()
        self.assertEqual("malformed-plan", caught.exception.reason)
        self.assertTrue(caught.exception.detail["error"])


class PlanMutationReinspectionTests(unittest.TestCase):
    """A runner-owned plan rewrite is re-read before it is reported."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending",), specs=SPECS)
        self.snapshot = plans_snapshot.inspect_plan(self.plan_dir)

    def test_reinspection_reports_the_rewritten_plan(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            ),
            encoding="utf-8",
        )
        refreshed = session_verification._reinspect_after_plan_mutation(
            plans_snapshot.PlanSnapshotCache(), self.snapshot
        )
        self.assertEqual("pending", self.snapshot.sessions[0].status)
        self.assertEqual("complete", refreshed.sessions[0].status)

    def test_unparseable_rewrite_keeps_the_pre_mutation_snapshot(self) -> None:
        (self.plan_dir / "session-1.md").write_text("no handoff\n", encoding="utf-8")
        refreshed = session_verification._reinspect_after_plan_mutation(
            plans_snapshot.PlanSnapshotCache(), self.snapshot
        )
        self.assertIs(self.snapshot, refreshed)


class TurnAnomalyClassificationTests(unittest.TestCase):
    """A clean exit code is not on its own evidence that a turn succeeded."""

    def test_unreadable_handoff_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit_evidence_failures._turn_shows_failure(
                {"returncode": 0, "plan_error": "session-1.md: no handoff"}
            )
        )

    def test_failure_status_result_is_anomalous(self) -> None:
        self.assertTrue(
            supervisor_audit_evidence_failures._turn_shows_failure(
                {"returncode": 0, "status_result": "no-status-change"}
            )
        )

    def test_accepted_turn_is_not_anomalous(self) -> None:
        self.assertFalse(
            supervisor_audit_evidence_failures._turn_shows_failure(
                {"returncode": 0, "status_result": "complete", "plan_error": None}
            )
        )

    def test_turn_without_a_status_result_is_judged_on_the_generic_signals(
        self,
    ) -> None:
        self.assertFalse(
            supervisor_audit_evidence_failures._turn_shows_failure({"returncode": 0})
        )
        self.assertTrue(
            supervisor_audit_evidence_failures._turn_shows_failure({"returncode": 1})
        )


if __name__ == "__main__":
    unittest.main()
