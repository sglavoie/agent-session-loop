"""Issue catalog and candidate match tests."""

from __future__ import annotations

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    assurance_models_inbox,
    assurance_models_primitives,
)
from agent_session_loop import assurance_projection as projection
from tests.support_asl_assurance_projection import (
    LAST_TIMESTAMP,
    LATER_TIMESTAMP,
    OTHER_RUN_ID,
    RUN_ID,
    TIMESTAMP,
    ProjectionTestCase,
    _finding,
    _publication,
)


class CatalogTests(ProjectionTestCase):
    def open_every_state(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(invariant_key="scope.no-escape"),
                    _finding(invariant_key="commit.guarded"),
                    _finding(invariant_key="wait.lease-held"),
                )
            )
        )
        for issue_id, kind in (
            ("ASI-2", "resolve"),
            ("ASI-3", "dismiss"),
            ("ASI-4", "accept-risk"),
        ):
            projection.record_disposition(
                issue_id, kind, state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
            )

    def test_the_catalog_covers_every_lifecycle_state(self) -> None:
        self.open_every_state()

        catalog = self.read().catalog

        self.assertEqual(
            ["ASI-1", "ASI-2", "ASI-3", "ASI-4"],
            [entry.issue_id for entry in catalog.entries],
        )
        self.assertEqual(
            ["open", "resolved", "dismissed", "accepted-risk"],
            [entry.state for entry in catalog.entries],
        )

    def test_an_entry_carries_the_finding_a_recurrence_is_matched_against(self) -> None:
        self.publish(_publication())

        entry = self.read().catalog.entry("ASI-1")

        self.assertEqual(_finding().fingerprint, entry.fingerprint)
        self.assertEqual("retry.budget-respected", entry.invariant_key)
        self.assertEqual("agent_session_loop/runner.py", entry.component)
        self.assertEqual("high", entry.severity)
        self.assertEqual(1, entry.occurrence_count)
        self.assertEqual(TIMESTAMP, entry.first_seen_at)

    def test_the_catalog_of_an_empty_inbox_is_empty(self) -> None:
        catalog = self.read().catalog
        self.assertEqual((), catalog.entries)
        self.assertIsNone(catalog.entry("ASI-1"))

    def test_every_fingerprint_a_merge_gathered_indexes_the_owner(self) -> None:
        self.open_two_issues()
        absorbed = _finding().fingerprint
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        catalog = self.read().catalog

        self.assertEqual("ASI-2", catalog.issue_for_fingerprint(absorbed))
        self.assertEqual(
            "ASI-2",
            catalog.issue_for_fingerprint(
                _finding(invariant_key="scope.no-escape").fingerprint
            ),
        )

    def test_a_merged_away_id_names_no_entry(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )
        catalog = self.read().catalog
        self.assertIsNone(catalog.entry("ASI-1"))
        self.assertIsNotNone(catalog.entry("ASI-2"))

    def test_an_identity_key_indexes_the_issue_that_owns_it(self) -> None:
        self.open_two_issues()
        projection.record_merge(
            "ASI-1", "ASI-2", state_dir=self.state_dir, recorded_at=LAST_TIMESTAMP
        )

        catalog = self.read().catalog

        self.assertEqual("ASI-2", catalog.issue_for_identity_key("issue.asi-1"))
        self.assertEqual("ASI-2", catalog.issue_for_identity_key("issue.asi-2"))
        self.assertIsNone(catalog.issue_for_identity_key("issue.asi-9"))

    def test_an_invariant_gathers_every_issue_that_broke_it(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(component="agent_session_loop/state.py"),
                    _finding(invariant_key="scope.no-escape"),
                )
            )
        )

        catalog = self.read().catalog

        self.assertEqual(
            ("ASI-1", "ASI-2"), catalog.issues_with_invariant("retry.budget-respected")
        )
        self.assertEqual(("ASI-3",), catalog.issues_with_invariant("scope.no-escape"))
        self.assertEqual((), catalog.issues_with_invariant("never.seen"))

    def test_localization_is_indexed_by_file_rather_than_by_line(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(
                        localization=(
                            "agent_session_loop/runner.py:8129",
                            "agent_session_loop/runner.py:8140",
                        )
                    ),
                    _finding(
                        invariant_key="scope.no-escape",
                        localization=("agent_session_loop/runner.py:12",),
                    ),
                    _finding(
                        invariant_key="commit.guarded",
                        localization=("agent_session_loop/state.py:4",),
                    ),
                )
            )
        )

        catalog = self.read().catalog

        # One file named twice in one finding is one place to look.
        self.assertEqual(
            ("agent_session_loop/runner.py",), catalog.entry("ASI-1").localization
        )
        self.assertEqual(
            ("ASI-1", "ASI-2"),
            catalog.issues_localized_at("agent_session_loop/runner.py:9999"),
        )
        self.assertEqual(
            ("ASI-3",), catalog.issues_localized_at("agent_session_loop/state.py")
        )

    def test_a_finding_without_a_localization_is_indexed_by_nothing(self) -> None:
        self.publish(_publication())
        catalog = self.read().catalog
        self.assertEqual((), catalog.entry("ASI-1").localization)
        self.assertEqual({}, dict(catalog.by_localization))

    def test_summary_tokens_gather_two_wordings_of_one_defect(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(
                        invariant_key="scope.no-escape",
                        summary="The budget was exhausted before that retry.",
                    ),
                    _finding(
                        invariant_key="commit.guarded",
                        summary="A commit landed outside the write scope.",
                    ),
                )
            )
        )

        catalog = self.read().catalog

        self.assertEqual(
            ("after", "budget", "exhausted", "ran", "retry"),
            catalog.entry("ASI-1").summary_tokens,
        )
        self.assertEqual(
            ("ASI-1", "ASI-2"),
            catalog.issues_sharing_summary_tokens(("budget", "exhausted")),
        )
        self.assertEqual(
            ("ASI-3",), catalog.issues_sharing_summary_tokens(("scope", "commit"))
        )
        self.assertEqual((), catalog.issues_sharing_summary_tokens(()))

    def test_the_digest_survives_a_defect_merely_recurring(self) -> None:
        self.publish(_publication())
        before = self.read().catalog

        self.publish(
            _publication(run_id=OTHER_RUN_ID, completed_at=LATER_TIMESTAMP),
            recorded_at=LATER_TIMESTAMP,
        )
        after = self.read().catalog

        self.assertEqual(2, after.entry("ASI-1").occurrence_count)
        self.assertEqual(LATER_TIMESTAMP, after.entry("ASI-1").last_seen_at)
        self.assertEqual(before.digest, after.digest)

    def test_a_lifecycle_move_changes_the_digest(self) -> None:
        self.publish(_publication())
        before = self.read().catalog.digest

        projection.record_disposition(
            "ASI-1", "resolve", state_dir=self.state_dir, recorded_at=LATER_TIMESTAMP
        )

        self.assertNotEqual(before, self.read().catalog.digest)

    def test_a_new_issue_changes_the_digest(self) -> None:
        self.publish(_publication())
        before = self.read().catalog.digest

        self.open_two_issues()

        self.assertNotEqual(before, self.read().catalog.digest)

    def test_a_binding_changes_the_digest(self) -> None:
        # A guard-opened issue starts unnamed, so binding its key is the one
        # remaining way identity moves without an issue being opened, and a
        # decision reviewed before the key existed was reviewed against an issue
        # nothing durable could name.
        ledger.append_issue_observation(
            assurance_models_inbox.IssueObservation(
                run_id=RUN_ID, finding=_finding(), observed_at=TIMESTAMP
            ),
            state_dir=self.state_dir,
            recorded_at=TIMESTAMP,
        )
        before = self.read().catalog.digest

        self.bind("ASI-1", "ASI-1")

        self.assertNotEqual(before, self.read().catalog.digest)

    def test_an_empty_catalog_still_has_a_digest(self) -> None:
        self.assertTrue(self.read().catalog.digest)

    def test_one_projection_builds_one_catalog(self) -> None:
        self.publish(_publication())
        projected = self.read()
        self.assertIs(projected.catalog, projected.catalog)

    def test_the_catalog_is_derived_and_never_part_of_projection_identity(
        self,
    ) -> None:
        self.build_every_kind()
        first = self.read()
        self.assertEqual(1, len(first.catalog.entries))
        self.assertEqual(first, self.read())


class CandidateMatchTests(ProjectionTestCase):
    """Matching proposes; it never decides. Near neighbours stay separate."""

    def test_an_exact_fingerprint_is_a_match_rather_than_a_candidate(self) -> None:
        self.publish(_publication())

        catalog = self.read().catalog

        self.assertEqual("ASI-1", catalog.issue_for_fingerprint(_finding().fingerprint))
        self.assertEqual((), catalog.candidates(_finding()))

    def test_a_renamed_component_leaves_the_invariant_as_a_candidate(self) -> None:
        self.publish(_publication())
        renamed = _finding(
            component="agent_session_loop/state.py", summary="The clamp never fired."
        )

        self.assertNotEqual(_finding().fingerprint, renamed.fingerprint)
        self.assertEqual(
            (
                projection.IdentityCandidate(
                    issue_id="ASI-1", reasons=(projection.MATCH_INVARIANT,)
                ),
            ),
            self.read().catalog.candidates(renamed),
        )

    def test_a_renamed_invariant_in_one_file_is_a_localization_candidate(self) -> None:
        self.publish(
            _publication(
                findings=(_finding(localization=("agent_session_loop/runner.py:12",)),)
            )
        )
        renamed = _finding(
            invariant_key="retry.budget-clamped",
            localization=("agent_session_loop/runner.py:409",),
            summary="Nothing alike here.",
        )

        self.assertEqual(
            (
                projection.IdentityCandidate(
                    issue_id="ASI-1", reasons=(projection.MATCH_LOCALIZATION,)
                ),
            ),
            self.read().catalog.candidates(renamed),
        )

    def test_a_shared_file_alone_is_not_enough_to_be_a_candidate(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(
                        category="hygiene",
                        component="scripts/format.sh",
                        invariant_key="format.clean",
                        localization=("agent_session_loop/runner.py:99",),
                        summary="Formatting drifted.",
                    ),
                )
            )
        )
        elsewhere = _finding(
            localization=("agent_session_loop/runner.py:409",),
            summary="Nothing alike here.",
        )

        self.assertEqual(
            ("ASI-1",),
            self.read().catalog.issues_localized_at("agent_session_loop/runner.py"),
        )
        self.assertEqual((), self.read().catalog.candidates(elsewhere))

    def test_a_reworded_summary_at_the_threshold_is_a_candidate(self) -> None:
        self.publish(_publication())
        reworded = _finding(
            invariant_key="scope.no-escape",
            summary="The budget was exhausted after a retry.",
        )

        (candidate,) = self.read().catalog.candidates(reworded)

        self.assertEqual("ASI-1", candidate.issue_id)
        self.assertEqual((projection.MATCH_SUMMARY,), candidate.reasons)
        self.assertAlmostEqual(0.80, candidate.similarity)

    def test_a_summary_below_the_threshold_is_not_a_candidate(self) -> None:
        self.publish(_publication())
        distant = _finding(
            invariant_key="scope.no-escape", summary="The retry budget looked wrong."
        )

        catalog = self.read().catalog

        # Shared vocabulary, different subject: two of seven tokens overlap.
        self.assertEqual(
            ("ASI-1",),
            catalog.issues_sharing_summary_tokens(
                assurance_models_findings.normalize_summary_tokens(distant.summary)
            ),
        )
        self.assertEqual((), catalog.candidates(distant))

    def test_near_neighbours_in_one_file_are_never_matched_to_each_other(self) -> None:
        retry = _finding(localization=("agent_session_loop/runner.py:8129",))
        scope = _finding(
            category="scope",
            component="agent_session_loop/write_scope.py",
            invariant_key="scope.no-escape",
            localization=("agent_session_loop/runner.py:8140",),
            summary="A write landed outside the declared scope.",
        )
        result = self.publish(_publication(findings=(retry, scope)))
        self.assertEqual(("ASI-1", "ASI-2"), result.issue_ids)

        catalog = self.read().catalog

        self.assertEqual((), catalog.candidates(retry))
        self.assertEqual((), catalog.candidates(scope))

    def test_a_candidate_is_never_the_issue_the_fingerprint_already_names(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(),
                    _finding(component="agent_session_loop/state.py"),
                )
            )
        )

        candidates = self.read().catalog.candidates(_finding())

        self.assertEqual(["ASI-2"], [item.issue_id for item in candidates])
        self.assertEqual(
            (projection.MATCH_INVARIANT, projection.MATCH_SUMMARY),
            candidates[0].reasons,
        )

    def test_corroborated_candidates_rank_ahead_of_single_reason_ones(self) -> None:
        self.publish(
            _publication(
                findings=(
                    _finding(
                        invariant_key="retry.budget-clamped",
                        summary="The clamp never fired.",
                    ),
                    _finding(
                        component="agent_session_loop/state.py",
                        localization=("agent_session_loop/state.py:20",),
                    ),
                    _finding(
                        invariant_key="scope.no-escape",
                        summary="A retry ran after the budget was exhausted again.",
                    ),
                )
            )
        )
        query = _finding(
            component="agent_session_loop/mailbox.py",
            localization=("agent_session_loop/state.py:44",),
        )

        candidates = self.read().catalog.candidates(query)

        self.assertEqual(["ASI-2", "ASI-3"], [item.issue_id for item in candidates])
        self.assertEqual(
            (
                projection.MATCH_INVARIANT,
                projection.MATCH_LOCALIZATION,
                projection.MATCH_SUMMARY,
            ),
            candidates[0].reasons,
        )
        self.assertEqual((projection.MATCH_SUMMARY,), candidates[1].reasons)

    def test_the_candidate_list_is_bounded(self) -> None:
        self.publish(
            _publication(
                findings=tuple(
                    _finding(component=f"agent_session_loop/module_{index}.py")
                    for index in range(projection.MAX_IDENTITY_CANDIDATES + 3)
                )
            )
        )
        query = _finding(component="agent_session_loop/runner.py")

        catalog = self.read().catalog

        self.assertEqual(
            projection.MAX_IDENTITY_CANDIDATES + 3,
            len(catalog.issues_with_invariant("retry.budget-respected")),
        )
        self.assertEqual(
            projection.MAX_IDENTITY_CANDIDATES, len(catalog.candidates(query))
        )
        self.assertEqual(2, len(catalog.candidates(query, limit=2)))

    def test_a_candidate_renders_the_fields_a_prompt_shows(self) -> None:
        candidate = projection.IdentityCandidate(
            issue_id="ASI-7",
            reasons=(projection.MATCH_INVARIANT, projection.MATCH_SUMMARY),
            similarity=0.8333333,
        )

        self.assertEqual(
            {
                "issue_id": "ASI-7",
                "reasons": ["invariant", "summary"],
                "similarity": 0.83,
            },
            candidate.to_dict(),
        )

    def test_two_findings_matching_one_issue_merge_into_one_candidate(self) -> None:
        left = projection.IdentityCandidate(
            issue_id="ASI-7", reasons=(projection.MATCH_INVARIANT,)
        )
        right = projection.IdentityCandidate(
            issue_id="ASI-7", reasons=(projection.MATCH_SUMMARY,), similarity=0.7
        )

        merged = left.merged(right)

        self.assertEqual(
            (projection.MATCH_INVARIANT, projection.MATCH_SUMMARY), merged.reasons
        )
        self.assertEqual(0.7, merged.similarity)
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            left.merged(
                projection.IdentityCandidate(issue_id="ASI-8", reasons=("invariant",))
            )

    def test_an_empty_catalog_names_no_candidate(self) -> None:
        self.assertEqual((), self.read().catalog.candidates(_finding()))
