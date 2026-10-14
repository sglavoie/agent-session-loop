"""Identity resolution, identity binding and kept-distinct tests."""

from __future__ import annotations

from unittest import mock

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models_inbox, assurance_models_primitives
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import ProjectionError
from tests.support_asl_assurance_projection import (
    LAST_TIMESTAMP,
    LATER_TIMESTAMP,
    OTHER_INPUT_DIGEST,
    OTHER_RUN_ID,
    RUN_ID,
    THIRD_INPUT_DIGEST,
    THIRD_RUN_ID,
    ProjectionTestCase,
    _artifacts,
    _finding,
    _publication,
)


class IdentityResolutionTests(ProjectionTestCase):
    RENAMED = "scope.no-escape"

    def select(self, invariant_key: str, issue_id: str, *, promote=None, **overrides):
        """Publish one finding whose identity the audit says is ``issue_id``."""
        finding = _finding(invariant_key=invariant_key)
        payload = {
            "run_id": OTHER_RUN_ID,
            "findings": (finding,),
            "artifacts": _artifacts(OTHER_INPUT_DIGEST),
            "completed_at": LATER_TIMESTAMP,
            "identity_selections": {finding.fingerprint: issue_id},
        }
        payload.update(overrides)
        return self.publish(
            _publication(**payload),
            recorded_at=LATER_TIMESTAMP,
            **({} if promote is None else {"promote": promote}),
        )

    def test_allocation_binds_the_key_in_the_same_transaction(self) -> None:
        result = self.publish(_publication())

        binding = result.bindings[0].payload

        self.assertEqual(1, len(result.bindings))
        self.assertEqual("issue.asi-1", binding.identity_key)
        self.assertEqual("ASI-1", binding.issue_id)
        self.assertEqual("allocation", binding.source)
        self.assertEqual("issue.asi-1", self.read().issue("ASI-1").primary_identity_key)

    def test_an_exact_fingerprint_overrules_a_selection(self) -> None:
        self.open_two_issues()
        finding = _finding()

        result = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(finding,),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LAST_TIMESTAMP,
                identity_selections={finding.fingerprint: "ASI-2"},
            ),
            recorded_at=LAST_TIMESTAMP,
        )

        # ASL derived ASI-1 from this fingerprint itself, so no answer about the
        # same defect can move it somewhere else.
        self.assertEqual(("ASI-1",), result.issue_ids)
        self.assertEqual((), result.bindings)

    def test_a_selection_attaches_its_fingerprint_to_the_selected_issue(self) -> None:
        self.publish(_publication())

        selected = self.select(self.RENAMED, "ASI-1")
        recurred = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key=self.RENAMED),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )

        self.assertEqual(("ASI-1",), selected.issue_ids)
        self.assertEqual((), selected.bindings)
        self.assertEqual(0, selected.summary.new_issue_count)
        # The third audit selected nothing and still lands on ASI-1: the
        # attachment made the rename an exact sighting from here on.
        self.assertEqual(("ASI-1",), recurred.issue_ids)
        self.assertEqual(1, len(self.read().issues))

    def test_a_selection_naming_a_merged_away_issue_follows_the_merge(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        result = self.select("commit.guarded", "ASI-1")

        self.assertEqual(("ASI-2",), result.issue_ids)
        self.assertEqual((), result.bindings)

    def test_a_selection_naming_no_issue_refuses_the_whole_publication(self) -> None:
        self.publish(_publication())

        with self.assertRaises(ProjectionError) as caught:
            self.select(self.RENAMED, "ASI-99")

        self.assertEqual(projection.UNKNOWN_ISSUE, caught.exception.reason)
        self.assertEqual(3, len(ledger.read_ledger(state_dir=self.state_dir).records))
        self.assertEqual((RUN_ID,), tuple(run.run_id for run in self.read().runs))

    def test_a_selection_for_a_finding_the_publication_lacks_is_refused(self) -> None:
        with self.assertRaises(assurance_models_primitives.AssuranceError) as caught:
            _publication(identity_selections={"f" * 64: "ASI-1"})
        self.assertIn(
            "names a finding this publication does not carry", str(caught.exception)
        )

    def test_a_clear_new_finding_takes_the_next_id(self) -> None:
        self.publish(_publication())

        result = self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key=self.RENAMED),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )

        self.assertEqual(("ASI-2",), result.issue_ids)
        self.assertEqual("issue.asi-2", result.bindings[0].payload.identity_key)

    def test_a_decision_reviewed_against_a_moved_catalog_is_reported_refreshed(
        self,
    ) -> None:
        self.publish(_publication())
        stale = self.read().catalog.digest
        # Another publisher opens an issue between the review and the append,
        # which is exactly the state the decision could not have seen.
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key="commit.guarded"),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )

        result = self.select(self.RENAMED, "ASI-1", catalog_digest=stale)

        self.assertTrue(result.identity_refreshed)
        self.assertEqual(("ASI-1",), result.issue_ids)

    def test_a_decision_reviewed_against_the_current_catalog_is_not_refreshed(
        self,
    ) -> None:
        self.publish(_publication())

        result = self.select(
            self.RENAMED, "ASI-1", catalog_digest=self.read().catalog.digest
        )

        self.assertFalse(result.identity_refreshed)

    def test_promotion_runs_after_identity_is_settled_and_before_the_append(
        self,
    ) -> None:
        self.publish(_publication())
        before = self.ledger_path.read_bytes()
        seen: list[bytes] = []

        def record() -> None:
            seen.append(self.ledger_path.read_bytes())

        result = self.select(self.RENAMED, "ASI-1", promote=record)

        # Promotion saw the ledger as it was, so the reports become canonical on
        # the strength of a decision that is settled but not yet durable.
        self.assertEqual([before], seen)
        self.assertEqual(("ASI-1",), result.issue_ids)
        self.assertNotEqual(before, self.ledger_path.read_bytes())

    def test_a_refused_promotion_leaves_the_ledger_untouched(self) -> None:
        self.publish(_publication())
        before = self.ledger_path.read_bytes()

        def refuse() -> None:
            raise assurance_models_primitives.AssuranceError(
                "the report could not be promoted"
            )

        with self.assertRaises(assurance_models_primitives.AssuranceError):
            self.select(self.RENAMED, "ASI-1", promote=refuse)

        self.assertEqual(before, self.ledger_path.read_bytes())
        projected = self.read()
        self.assertEqual(["ASI-1"], [issue.issue_id for issue in projected.issues])
        self.assertEqual((RUN_ID,), tuple(run.run_id for run in projected.runs))

    def test_a_failed_append_mints_nothing(self) -> None:
        self.publish(_publication())
        before = self.ledger_path.read_bytes()

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("the ledger could not be extended")

        with (
            mock.patch.object(ledger, "_append_locked", refuse),
            self.assertRaises(OSError),
        ):
            self.publish(
                _publication(
                    run_id=OTHER_RUN_ID,
                    findings=(_finding(invariant_key=self.RENAMED),),
                    artifacts=_artifacts(OTHER_INPUT_DIGEST),
                    completed_at=LATER_TIMESTAMP,
                ),
                recorded_at=LATER_TIMESTAMP,
            )

        # The ID the failed publication would have taken is still free, so the
        # next publication takes it rather than skipping past an orphan.
        self.assertEqual(before, self.ledger_path.read_bytes())
        result = self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(invariant_key=self.RENAMED),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.assertEqual(("ASI-2",), result.issue_ids)
        self.assertEqual("issue.asi-2", result.bindings[0].payload.identity_key)

    def test_republishing_promotes_nothing(self) -> None:
        self.publish(_publication())
        promoted: list[bool] = []

        result = self.publish(_publication(), promote=lambda: promoted.append(True))

        # The audit is already in the ledger, so there is no record about to be
        # written for a canonical report to accompany.
        self.assertFalse(result.created)
        self.assertEqual([], promoted)


class IdentityTests(ProjectionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.open_two_issues()

    def test_an_issue_opened_outside_a_publication_has_no_identity(self) -> None:
        # A guard that observes a defect directly mints an ID without a name for
        # it, which is what an issue carried over from a v1 ledger looks like
        # too. Both must stay readable rather than being treated as damaged.
        ledger.append_issue_observation(
            assurance_models_inbox.IssueObservation(
                run_id=THIRD_RUN_ID,
                finding=_finding(invariant_key="commit.guarded"),
                observed_at=LAST_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )
        issue = self.read().issue("ASI-3")
        self.assertEqual((), issue.identity_keys)
        self.assertIsNone(issue.primary_identity_key)

    def test_a_bound_key_names_its_own_issue(self) -> None:
        projected = self.read()
        named = projected.issue_for_identity_key("issue.asi-1")
        self.assertEqual(
            {"issue.asi-1": "ASI-1", "issue.asi-2": "ASI-2"},
            dict(projected.identity_bindings),
        )
        self.assertEqual("ASI-1", named.issue_id)
        self.assertIsNone(projected.issue_for_identity_key("issue.asi-9"))

    def test_a_binding_resolves_through_a_later_merge(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        projected = self.read()

        self.assertEqual(
            {"issue.asi-1": "ASI-2", "issue.asi-2": "ASI-2"},
            dict(projected.identity_bindings),
        )
        self.assertEqual(
            "ASI-2", projected.issue_for_identity_key("issue.asi-1").issue_id
        )
        # The record still says what it said: the merge is followed at replay,
        # not written back over the binding.
        view = ledger.read_ledger(state_dir=self.state_dir)
        self.assertEqual("ASI-1", view.identity_bindings["issue.asi-1"])

    def test_an_issue_lists_every_key_that_resolves_to_it(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        issue = self.read().issue("ASI-2")
        self.assertEqual(("issue.asi-1", "issue.asi-2"), issue.identity_keys)

    def test_an_issue_is_named_by_its_own_key_before_one_it_absorbed(self) -> None:
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        self.assertEqual("issue.asi-2", self.read().issue("ASI-2").primary_identity_key)

    def test_an_issue_named_only_by_absorbed_keys_takes_the_lowest(self) -> None:
        # ASI-3 is opened by a guard rather than by a publication, so it holds no
        # key of its own and can only be named by the keys it absorbs.
        ledger.append_issue_observation(
            assurance_models_inbox.IssueObservation(
                run_id=THIRD_RUN_ID,
                finding=_finding(invariant_key="commit.guarded"),
                observed_at=LAST_TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=LAST_TIMESTAMP,
        )
        projection.record_merge(
            "ASI-1", "ASI-3", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        projection.record_merge(
            "ASI-2", "ASI-3", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        issue = self.read().issue("ASI-3")

        self.assertEqual(("issue.asi-1", "issue.asi-2"), issue.identity_keys)
        self.assertEqual("issue.asi-1", issue.primary_identity_key)

    def test_a_stored_key_claiming_a_second_issue_is_reported_not_applied(self) -> None:
        self.force_binding("ASI-2", "ASI-1")

        projected = self.read()

        self.assertEqual(
            [projection.IDENTITY_KEY_CONFLICT],
            [item.reason for item in projected.anomalies],
        )
        self.assertEqual("ASI-1", projected.identity_bindings["issue.asi-1"])
        self.assertEqual(("issue.asi-2",), projected.issue("ASI-2").identity_keys)

    def test_a_restated_binding_is_reported_as_a_duplicate(self) -> None:
        self.force_binding("ASI-1", "ASI-1")

        projected = self.read()

        self.assertEqual(
            [projection.DUPLICATE_IDENTITY_BINDING],
            [item.reason for item in projected.anomalies],
        )
        self.assertEqual("ASI-1", projected.identity_bindings["issue.asi-1"])


class KeptDistinctTests(ProjectionTestCase):
    def test_a_ruling_holds_while_both_issues_still_read_the_way_they_did(self) -> None:
        self.open_two_issues()
        self.keep_distinct("ASI-1", "ASI-2")
        projected = self.read()
        self.assertEqual((), projected.anomalies)
        self.assertTrue(projected.is_kept_distinct("ASI-1", "ASI-2"))
        self.assertTrue(projected.is_kept_distinct("ASI-2", "ASI-1"))

    def test_a_ruling_lapses_once_one_issue_is_recognized_differently(self) -> None:
        self.open_two_issues()
        self.keep_distinct("ASI-1", "ASI-2")
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(_finding(summary="A retry ran after the budget ran out."),),
                artifacts=_artifacts(THIRD_INPUT_DIGEST),
            ),
            recorded_at=LAST_TIMESTAMP,
        )
        projected = self.read()
        self.assertEqual((), projected.anomalies)
        self.assertFalse(projected.is_kept_distinct("ASI-1", "ASI-2"))

    def test_a_ruling_about_an_unobserved_issue_is_reported_not_applied(self) -> None:
        self.publish(_publication())
        self.force_record(
            ledger.KIND_ISSUE_KEPT_DISTINCT,
            assurance_models_inbox.IssueKeptDistinct(
                issue_ids=("ASI-1", "ASI-7"),
                identity_digests=("a" * 64, "b" * 64),
                recorded_at=LATER_TIMESTAMP,
            ),
        )
        projected = self.read()
        self.assertEqual({}, dict(projected.kept_distinct))
        self.assertEqual(
            [projection.UNKNOWN_ISSUE], [item.reason for item in projected.anomalies]
        )

    def test_a_ruling_whose_pair_became_one_issue_leaves_nothing_behind(self) -> None:
        self.open_two_issues()
        self.keep_distinct("ASI-1", "ASI-2")
        projection.record_merge(
            "ASI-2", "ASI-1", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        projected = self.read()
        self.assertEqual((), projected.anomalies)
        self.assertEqual({}, dict(projected.kept_distinct))
        self.assertFalse(projected.is_kept_distinct("ASI-1", "ASI-2"))

    def test_the_later_ruling_about_a_pair_is_the_one_that_stands(self) -> None:
        self.open_two_issues()
        self.force_record(
            ledger.KIND_ISSUE_KEPT_DISTINCT,
            assurance_models_inbox.IssueKeptDistinct(
                issue_ids=("ASI-1", "ASI-2"),
                identity_digests=("a" * 64, "b" * 64),
                recorded_at=LATER_TIMESTAMP,
            ),
        )
        self.keep_distinct("ASI-1", "ASI-2")
        self.assertTrue(self.read().is_kept_distinct("ASI-1", "ASI-2"))
