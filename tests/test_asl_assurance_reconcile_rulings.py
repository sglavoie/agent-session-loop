# ruff: noqa: F401, F403, F405, I001
"""Assurance reconciliation standing-ruling and race tests."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from tests.asl_assurance_reconcile_test_support import *


class KeepDistinctTests(ReconcileTestCase):
    """A reviewed 'these are two defects' has to survive the reviewer's session."""

    def rule_the_twins_distinct(self) -> Path:
        path = self.write_manifest(
            self.keep_distinct_only_manifest(), name="keep-distinct.json"
        )
        self.assertEqual(1, self.apply_path(path)["kept_distinct"])
        return path

    def test_a_reviewed_pair_is_not_suggested_again(self) -> None:
        self.open_unbound_twins()
        self.rule_the_twins_distinct()

        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual([], payload["unresolved"])
        self.assertEqual(1, payload["suppressed_pairs"])

    def test_applying_a_ruling_twice_writes_nothing_the_second_time(self) -> None:
        self.open_unbound_twins()
        path = self.rule_the_twins_distinct()
        settled = self.ledger_path.read_bytes()

        payload = self.apply_path(path)
        self.assertFalse(payload["applied"])
        self.assertEqual(0, payload["kept_distinct"])
        self.assertEqual(settled, self.ledger_path.read_bytes())

    def test_the_apply_preview_loop_repeats_no_decision_and_still_finds_new_ones(
        self,
    ) -> None:
        """The whole point of FR-3: reviewing a pair has to reduce the next preview."""
        self.open_unbound_twins()
        first = self.preview_manifest()
        path = self.write_manifest(
            self.resolve_reviews(
                first, decision=assurance_models_reconcile.DECISION_KEEP_DISTINCT
            )
        )
        self.apply_path(path)

        second = self.preview_manifest()
        self.assertEqual(
            [],
            [
                item
                for item in second["decisions"]
                if item["decision"] == assurance_models_reconcile.DECISION_REVIEW
            ],
        )

        self.observe(
            _finding(
                component="agent_session_loop/supervisor_audit.py",
                invariant_key="audit.report-presence",
                summary="The audit report never appeared in the run directory at all.",
                localization=("agent_session_loop/supervisor_audit.py",),
            ),
            run_id="20260810T101116444444Z-4247",
        )
        third = self.preview_manifest()
        self.assertEqual(
            [["ASI-3", "ASI-4"]],
            [
                item["issue_ids"]
                for item in third["decisions"]
                if item["decision"] == assurance_models_reconcile.DECISION_REVIEW
            ],
        )

    def test_a_ruling_lapses_when_one_of_its_issues_materially_changes(self) -> None:
        first, second = self.open_unbound_twins()
        self.rule_the_twins_distinct()
        # The same defect recurs, described differently enough that what the pair
        # was ruled on is no longer what the pair says.
        self.observe(
            _finding(summary="A retry ran once more after the budget was exhausted."),
            run_id="20260810T101117555555Z-4248",
        )

        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual(0, payload["suppressed_pairs"])
        self.assertEqual(
            [[first, second]], [item["issue_ids"] for item in payload["unresolved"]]
        )

    def test_a_ruling_about_an_issue_that_merged_away_is_refused(self) -> None:
        first, second = self.open_unbound_twins()
        manifest = self.keep_distinct_only_manifest()
        projection.record_merge(second, first, state_dir=self.state_dir)
        manifest["catalog_digest"] = self.inbox().catalog.digest
        path = self.write_manifest(manifest)
        before = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual(reconcile.ISSUE_NOT_CANONICAL, payload["reason"])
        self.assertEqual(before, self.ledger_path.read_bytes())

    def test_a_ruling_naming_an_issue_nobody_observed_is_refused(self) -> None:
        first, _ = self.open_unbound_twins()
        manifest = self.keep_distinct_only_manifest()
        manifest["decisions"] = [
            {
                "decision": assurance_models_reconcile.DECISION_KEEP_DISTINCT,
                "issue_ids": [first, "ASI-99"],
                "note": None,
            }
        ]
        path = self.write_manifest(manifest)
        before = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual(projection.UNKNOWN_ISSUE, payload["reason"])
        self.assertEqual(before, self.ledger_path.read_bytes())

    def twin_identity_digests(self) -> tuple[str, str]:
        """The digests the twins mint with, replayed in a throwaway inbox.

        A ruling that names issues nothing has observed yet can only be written
        before they exist, so the digests it needs to carry to look current have
        to come from somewhere other than the inbox it is written into.
        """
        scratch = Path(self._tmp.name) / "scratch-inbox"
        first, second = self.open_unbound_twins(state_dir=scratch)
        catalog = projection.read_projection(state_dir=scratch).catalog
        first_digest = catalog.identity_digest(first)
        second_digest = catalog.identity_digest(second)
        assert first_digest is not None and second_digest is not None
        return first_digest, second_digest

    def test_an_anomalous_prior_ruling_cannot_suppress_a_fresh_apply(self) -> None:
        """A record the projection threw out must not answer for a live review."""
        ledger.append_record(
            ledger.KIND_ISSUE_KEPT_DISTINCT,
            assurance_models_inbox.IssueKeptDistinct(
                issue_ids=("ASI-1", "ASI-2"),
                identity_digests=self.twin_identity_digests(),
                recorded_at=TIMESTAMP,
            ),
            state_dir=self.state_dir,
            recorded_at=TIMESTAMP,
        )
        first, second = self.open_unbound_twins()
        anomalous = self.inbox()
        self.assertEqual(
            [projection.UNKNOWN_ISSUE], [item.reason for item in anomalous.anomalies]
        )
        self.assertFalse(anomalous.is_kept_distinct(first, second))

        path = self.write_manifest(
            self.resolve_reviews(
                self.preview_manifest(),
                decision=assurance_models_reconcile.DECISION_KEEP_DISTINCT,
            )
        )
        self.assertEqual(1, self.apply_path(path)["kept_distinct"])

        settled = self.inbox()
        self.assertTrue(settled.is_kept_distinct(first, second))
        # The rejected record stays exactly where it landed: the ledger is never
        # rewritten, so the anomaly it reports is still readable as evidence.
        self.assertEqual(
            [projection.UNKNOWN_ISSUE], [item.reason for item in settled.anomalies]
        )

        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual([], payload["unresolved"])
        self.assertEqual(1, payload["suppressed_pairs"])

    def test_text_says_which_pairs_a_standing_ruling_already_answered(self) -> None:
        self.open_unbound_twins()
        self.rule_the_twins_distinct()

        code, out, err = self.run_cli(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertIn("No pairs need review.", out)
        self.assertIn("1 pair(s) were already reviewed as distinct defects", out)


class ConcurrentApplyTests(ReconcileTestCase):
    def test_a_publisher_that_wins_the_race_makes_the_manifest_stale(self) -> None:
        """A decision reviewed before another writer moved identity never lands."""
        self.open_unbound_twins()
        manifest = assurance_models_reconcile.parse_reconciliation_manifest(
            self.resolve_reviews(
                self.preview_manifest(),
                decision=assurance_models_reconcile.DECISION_MERGE,
            )
        )
        original = ledger.append_transaction
        raced: list[bool] = []

        def race(build, **kwargs):
            if not raced:
                raced.append(True)
                self.observe(
                    _finding(
                        component="agent_session_loop/mailbox.py",
                        invariant_key="mailbox.no-stale-read",
                        summary="A stale mailbox record was read as guidance.",
                        localization=("agent_session_loop/mailbox.py",),
                    ),
                    run_id="20260810T101115333333Z-4246",
                )
            return original(build, **kwargs)

        with (
            mock.patch.object(ledger, "append_transaction", race),
            self.assertRaises(projection.ProjectionError) as raised,
        ):
            reconcile.apply_manifest(manifest, state_dir=self.state_dir)
        self.assertEqual(reconcile.STALE_CATALOG_DIGEST, raised.exception.reason)
        self.assertEqual({}, dict(self.inbox().identity_bindings))

    def test_an_apply_that_lost_the_race_to_an_identical_one_is_a_no_op(self) -> None:
        self.open_unbound_twins()
        manifest = assurance_models_reconcile.parse_reconciliation_manifest(
            self.resolve_reviews(
                self.preview_manifest(),
                decision=assurance_models_reconcile.DECISION_MERGE,
            )
        )
        first = reconcile.apply_manifest(manifest, state_dir=self.state_dir)
        self.assertTrue(first.applied)
        settled = self.ledger_path.read_bytes()

        second = reconcile.apply_manifest(manifest, state_dir=self.state_dir)
        self.assertFalse(second.applied)
        self.assertEqual(settled, self.ledger_path.read_bytes())
