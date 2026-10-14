"""Record kind, replay, malformed record and anomaly tests."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models_inbox
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import ProjectionError
from tests.support_asl_assurance_projection import (
    LAST_TIMESTAMP,
    LATER_TIMESTAMP,
    OTHER_RUN_ID,
    RUN_ID,
    THIRD_RUN_ID,
    TIMESTAMP,
    ProjectionTestCase,
    _finding,
    _publication,
)


class RecordKindTests(ProjectionTestCase):
    def test_every_ledger_kind_reaches_the_projection(self) -> None:
        self.build_every_kind()
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual(
            set(ledger.LEDGER_KINDS), {record.kind for record in view.records}
        )
        projected = projection.project(view)
        self.assertEqual((), projected.anomalies)
        self.assertEqual(["ASI-1"], [issue.issue_id for issue in projected.issues])
        issue = projected.issue("ASI-1")
        self.assertEqual("resolved", issue.state)
        self.assertEqual(("ASI-2",), issue.merged_from)
        self.assertEqual(("issue.asi-1", "issue.asi-2"), issue.identity_keys)
        self.assertEqual("issue.asi-1", issue.primary_identity_key)
        self.assertEqual(1, len(issue.plans))
        self.assertEqual(
            {RUN_ID, OTHER_RUN_ID, THIRD_RUN_ID},
            {run.run_id for run in projected.runs},
        )
        run = projected.run(RUN_ID)
        self.assertEqual("findings", run.state)
        self.assertEqual("reworked", run.closeout.outcome)
        self.assertEqual(("ASI-1",), run.issue_ids)
        self.assertEqual("skipped", projected.run(OTHER_RUN_ID).state)
        self.assertEqual("unavailable", projected.run(THIRD_RUN_ID).state)


class ReplayTests(ProjectionTestCase):
    def test_replaying_only_the_ledger_reconstructs_identical_state(self) -> None:
        self.build_every_kind()
        first = self.read()
        data = self.ledger_path.read_bytes()
        replayed = projection.project(ledger.parse_ledger_bytes(data, self.ledger_path))
        self.assertEqual(first, replayed)

    def test_a_copied_ledger_with_no_cache_reconstructs_identical_state(self) -> None:
        self.build_every_kind()
        first = self.read()
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        copied = ledger.ledger_path(elsewhere)
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.write_bytes(self.ledger_path.read_bytes())
        self.assertFalse(ledger.ledger_index_path(elsewhere).exists())
        second = projection.read_projection(state_dir=elsewhere)
        self.assertEqual(first, second)

    def test_replay_is_stable_across_repeated_reads(self) -> None:
        self.build_every_kind()
        self.assertEqual(self.read(), self.read())


class MalformedRecordTests(ProjectionTestCase):
    def test_an_observation_record_with_no_issue_id_is_refused(self) -> None:
        # The parse boundary rejects this envelope, so the only way one reaches
        # the replay is a `LedgerRecord` built in code. Projecting it would have
        # opened an issue under a null identity.
        self.publish(_publication())
        view = ledger.parse_ledger_bytes(
            self.ledger_path.read_bytes(), self.ledger_path
        )
        observation = next(
            record
            for record in view.records
            if record.kind == ledger.KIND_ISSUE_OBSERVED
        )
        forged = dataclasses.replace(
            view, records=(dataclasses.replace(observation, issue_id=None),)
        )
        with self.assertRaises(ProjectionError) as caught:
            projection.project(forged)
        self.assertEqual(
            projection.OBSERVATION_WITHOUT_ISSUE_ID, caught.exception.reason
        )


class AnomalyTests(ProjectionTestCase):
    def test_a_disposition_for_an_unobserved_issue_is_reported_not_applied(
        self,
    ) -> None:
        self.publish(_publication())
        self.force_record(
            ledger.KIND_ISSUE_DISPOSITION,
            assurance_models_inbox.Disposition(
                issue_id="ASI-7", kind="resolve", recorded_at=LATER_TIMESTAMP
            ),
        )
        projected = self.read()
        self.assertEqual("open", projected.issue("ASI-1").state)
        self.assertEqual(1, len(projected.anomalies))
        self.assertEqual(projection.UNKNOWN_ISSUE, projected.anomalies[0].reason)
        self.assertEqual(4, projected.anomalies[0].seq)

    def test_a_cyclic_merge_pair_on_disk_is_reported_and_terminates(self) -> None:
        self.open_two_issues()
        self.force_record(
            ledger.KIND_ISSUE_MERGED,
            assurance_models_inbox.IssueMerge(
                source_issue_id="ASI-1",
                target_issue_id="ASI-2",
                recorded_at=LAST_TIMESTAMP,
            ),
        )
        self.force_record(
            ledger.KIND_ISSUE_MERGED,
            assurance_models_inbox.IssueMerge(
                source_issue_id="ASI-2",
                target_issue_id="ASI-1",
                recorded_at=LAST_TIMESTAMP,
            ),
        )
        projected = self.read()
        self.assertEqual(["ASI-2"], [issue.issue_id for issue in projected.issues])
        self.assertEqual(
            [projection.MERGE_CYCLE], [item.reason for item in projected.anomalies]
        )
        self.assertEqual("ASI-2", projected.resolve("ASI-1"))

    def test_a_closeout_without_an_audit_record_is_reported(self) -> None:
        self.force_record(
            ledger.KIND_HUMAN_CLOSEOUT,
            assurance_models_inbox.Closeout(
                run_id=RUN_ID, outcome="accepted", recorded_at=TIMESTAMP
            ),
        )
        projected = self.read()
        self.assertEqual((), projected.runs)
        self.assertEqual(
            [projection.UNKNOWN_RUN], [item.reason for item in projected.anomalies]
        )

    def test_an_observation_naming_a_merged_source_lands_on_the_target(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        ledger.append_transaction(
            lambda view: (
                ledger.PendingRecord(
                    kind=ledger.KIND_ISSUE_OBSERVED,
                    payload=assurance_models_inbox.IssueObservation(
                        run_id=THIRD_RUN_ID,
                        finding=_finding(),
                        observed_at=LAST_TIMESTAMP,
                    ),
                    issue_id="ASI-1",
                ),
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )
        projected = self.read()
        self.assertEqual(["ASI-2"], [issue.issue_id for issue in projected.issues])
        self.assertEqual(3, len(projected.issue("ASI-2").occurrences))

    def test_rejected_lines_are_carried_through_to_the_projection(self) -> None:
        self.publish(_publication())
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        projected = self.read()
        self.assertEqual(1, len(projected.rejected))
        self.assertEqual(ledger.REJECT_INVALID_JSON, projected.rejected[0].reason)
        self.assertEqual(1, len(projected.issues))
