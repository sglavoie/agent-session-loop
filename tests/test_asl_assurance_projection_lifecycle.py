"""Issue lifecycle, merge and closeout tests."""

from __future__ import annotations

import unittest

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models_primitives, assurance_models_vocab
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import ProjectionError
from tests.support_asl_assurance_projection import (
    LAST_TIMESTAMP,
    LATER_TIMESTAMP,
    OTHER_INPUT_DIGEST,
    OTHER_RUN_ID,
    RUN_ID,
    THIRD_RUN_ID,
    ProjectionTestCase,
    _artifacts,
    _finding,
    _publication,
)


class LifecycleTests(ProjectionTestCase):
    def _observe_again(
        self, timestamp: str = LATER_TIMESTAMP, run_id: str = OTHER_RUN_ID
    ) -> None:
        self.publish(
            _publication(run_id=run_id, completed_at=timestamp),
            recorded_at=timestamp,
        )

    def test_an_observed_issue_starts_open(self) -> None:
        self.publish(_publication())
        self.assertEqual("open", self.read().issue("ASI-1").state)

    def test_each_disposition_moves_the_issue_through_an_append(self) -> None:
        self.publish(_publication())
        for kind, expected in assurance_models_vocab.DISPOSITION_RESULT_STATE.items():
            projection.record_disposition(
                "ASI-1", kind, state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
            )
            self.assertEqual(expected, self.read().issue("ASI-1").state)
        issue = self.read().issue("ASI-1")
        self.assertEqual(3, len(issue.dispositions))
        self.assertEqual(1, issue.occurrence_count)

    def test_a_recurrence_after_resolution_reopens_the_issue(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )
        self.assertEqual("resolved", self.read().issue("ASI-1").state)
        self._observe_again(LAST_TIMESTAMP)
        issue = self.read().issue("ASI-1")
        self.assertEqual("open", issue.state)
        self.assertEqual(1, issue.reopened_count)
        self.assertEqual(2, issue.occurrence_count)

    def test_a_recurrence_after_dismissal_stays_closed(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1",
            "dismiss",
            note="Working as intended.",
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )
        self._observe_again(LAST_TIMESTAMP)
        issue = self.read().issue("ASI-1")
        self.assertEqual("dismissed", issue.state)
        self.assertEqual(0, issue.reopened_count)
        self.assertEqual(2, issue.occurrence_count)

    def test_a_recurrence_after_accepting_risk_stays_closed(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1",
            "accept-risk",
            state_dir=self.state_dir,
            recorded_at=LATER_TIMESTAMP,
        )
        self._observe_again(LAST_TIMESTAMP)
        issue = self.read().issue("ASI-1")
        self.assertEqual("accepted-risk", issue.state)
        self.assertEqual(2, issue.occurrence_count)

    def test_disposing_an_unknown_issue_is_refused(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_disposition("ASI-9", "resolve", state_dir=self.state_dir)
        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        self.assertFalse(self.ledger_path.exists())

    def test_a_disposition_never_rewrites_an_earlier_record(self) -> None:
        self.publish(_publication())
        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )
        projection.record_disposition(
            "ASI-1", "dismiss", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        view = ledger.read_ledger(state_dir=self.state_dir)
        kinds = [record.kind for record in view.records]
        self.assertEqual(
            [
                "audit_completed",
                "issue_observed",
                "issue_identity_bound",
                "issue_disposition",
                "issue_disposition",
            ],
            kinds,
        )
        self.assertEqual("dismissed", self.read().issue("ASI-1").state)


class MergeTests(ProjectionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_two_issues()

    def test_merge_moves_the_whole_history_to_the_target(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        projected = self.read()
        self.assertEqual(["ASI-2"], [issue.issue_id for issue in projected.issues])
        target = projected.issue("ASI-2")
        self.assertEqual(("ASI-1",), target.merged_from)
        self.assertEqual(2, target.occurrence_count)
        self.assertEqual(2, len(target.fingerprints))

    def test_a_merged_reference_resolves_to_its_target(self) -> None:
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        projected = self.read()
        self.assertEqual("ASI-2", projected.resolve("ASI-1"))
        self.assertEqual("ASI-2", projected.issue("ASI-1").issue_id)
        self.assertEqual({"ASI-1": "ASI-2"}, dict(projected.aliases))

    def test_later_observations_route_through_the_final_canonical_target(self) -> None:
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        projection.record_merge("ASI-2", "ASI-3", state_dir=self.state_dir)
        # The original ASI-1 identity, seen again, lands on the end of the chain.
        result = self.publish(
            _publication(artifacts=_artifacts(OTHER_INPUT_DIGEST)),
            recorded_at=LAST_TIMESTAMP,
        )
        self.assertEqual(("ASI-3",), result.issue_ids)
        projected = self.read()
        self.assertEqual(["ASI-3"], [issue.issue_id for issue in projected.issues])
        self.assertEqual("ASI-3", projected.resolve("ASI-1"))
        self.assertEqual(("ASI-1", "ASI-2"), projected.issue("ASI-3").merged_from)
        self.assertEqual(4, projected.issue("ASI-3").occurrence_count)

    def test_a_disposition_on_a_merged_id_reaches_the_canonical_issue(self) -> None:
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        record = projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        self.assertEqual("ASI-2", record.payload.issue_id)
        self.assertEqual("resolved", self.read().issue("ASI-1").state)

    def test_merge_rejects_an_issue_merging_into_itself(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-1", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_SELF, caught.exception.reason)

    def test_merge_rejects_an_unknown_source(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-9", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_UNKNOWN_SOURCE, caught.exception.reason)

    def test_merge_rejects_an_unknown_target(self) -> None:
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-1", "ASI-9", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_UNKNOWN_TARGET, caught.exception.reason)

    def test_merge_is_irreversible(self) -> None:
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-1", "ASI-3", state_dir=self.state_dir)
        self.assertEqual(
            projection.MERGE_SOURCE_ALREADY_MERGED, caught.exception.reason
        )

    def test_merge_rejects_an_already_merged_target(self) -> None:
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-3", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(
            projection.MERGE_TARGET_ALREADY_MERGED, caught.exception.reason
        )

    def test_merge_rejects_a_cycle(self) -> None:
        projection.record_merge("ASI-1", "ASI-2", state_dir=self.state_dir)
        with self.assertRaises(ProjectionError) as caught:
            projection.record_merge("ASI-2", "ASI-1", state_dir=self.state_dir)
        self.assertEqual(projection.MERGE_CYCLE, caught.exception.reason)

    def test_a_refused_merge_writes_nothing(self) -> None:
        before = self.ledger_path.read_bytes()
        with self.assertRaises(ProjectionError):
            projection.record_merge("ASI-1", "ASI-9", state_dir=self.state_dir)
        self.assertEqual(before, self.ledger_path.read_bytes())


class CloseoutTests(ProjectionTestCase):
    def closeout(self, **kwargs):
        kwargs.setdefault("recorded_at", LAST_TIMESTAMP)
        return projection.record_closeout(
            kwargs.pop("run_id", RUN_ID),
            kwargs.pop("outcome", "accepted"),
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_a_closeout_records_the_verdict_on_an_audited_run(self) -> None:
        self.publish(_publication())

        record = self.closeout(
            outcome="reworked",
            rework_minutes=25,
            issue_ids=("ASI-1",),
            note="clamped the counter",
            plan_id="b766d9cd537c-c70d18f6ec8e",
        )

        self.assertEqual(ledger.KIND_HUMAN_CLOSEOUT, record.kind)
        stored = self.read().run(RUN_ID).closeout
        self.assertEqual("reworked", stored.outcome)
        self.assertEqual(25.0, stored.rework_minutes)
        self.assertEqual(("ASI-1",), stored.issue_ids)
        self.assertEqual("b766d9cd537c-c70d18f6ec8e", stored.plan_id)
        self.assertIsNone(stored.replaces)

    def test_a_run_the_ledger_never_audited_cannot_be_closed_out(self) -> None:
        self.publish(_publication())

        with self.assertRaises(projection.ProjectionError) as caught:
            self.closeout(run_id=OTHER_RUN_ID)

        self.assertEqual(projection.UNKNOWN_RUN, caught.exception.reason)
        self.assertEqual(1, len(self.read().runs))

    def test_a_closeout_cannot_link_an_issue_the_inbox_never_opened(self) -> None:
        self.publish(_publication())

        with self.assertRaises(projection.ProjectionError) as caught:
            self.closeout(issue_ids=("ASI-99",))

        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        self.assertIsNone(self.read().run(RUN_ID).closeout)

    def test_a_linked_issue_that_was_merged_away_names_its_owner(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-2", "ASI-1", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        self.closeout(issue_ids=("ASI-2", "ASI-1"))

        self.assertEqual(("ASI-1",), self.read().run(RUN_ID).closeout.issue_ids)

    def test_a_second_closeout_is_refused_unless_it_replaces_the_first(self) -> None:
        self.publish(_publication())
        self.closeout(outcome="accepted")

        with self.assertRaises(projection.ProjectionError) as caught:
            self.closeout(outcome="escaped")

        self.assertEqual(projection.DUPLICATE_CLOSEOUT, caught.exception.reason)
        self.assertEqual(1, len(self.read().run(RUN_ID).closeouts))

    def test_a_replacement_names_the_closeout_it_supersedes(self) -> None:
        self.publish(_publication())
        first = self.closeout(outcome="accepted")

        second = self.closeout(outcome="escaped", replace=True)

        self.assertEqual(
            projection.closeout_reference(first.seq), second.payload.replaces
        )
        recorded = self.read().run(RUN_ID)
        # Superseding appends: both verdicts, and the order they were reached
        # in, stay readable, and the later one is the one in force.
        self.assertEqual(
            ["accepted", "escaped"], [item.outcome for item in recorded.closeouts]
        )
        self.assertEqual("escaped", recorded.closeout.outcome)

    def test_replacing_when_nothing_was_closed_out_names_nothing(self) -> None:
        self.publish(_publication())

        record = self.closeout(replace=True)

        self.assertIsNone(record.payload.replaces)

    def test_unusable_closeout_values_are_refused_before_the_lock(self) -> None:
        self.publish(_publication())
        for kwargs in (
            {"outcome": "partly"},
            {"rework_minutes": -1},
            {"rework_minutes": float("nan")},
            {"rework_minutes": float("inf")},
        ):
            with (
                self.subTest(**kwargs),
                self.assertRaises(assurance_models_primitives.AssuranceError),
            ):
                self.closeout(**kwargs)
        self.assertIsNone(self.read().run(RUN_ID).closeout)


if __name__ == "__main__":
    unittest.main()
