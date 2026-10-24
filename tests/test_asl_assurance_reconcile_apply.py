# ruff: noqa: F403, F405, I001
"""Assurance reconciliation preview and apply tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_assurance_reconcile_test_support import *


ASL_TEST_PORTABLE = True


class PreviewTests(ReconcileTestCase):
    def test_an_empty_inbox_previews_nothing_rather_than_failing(self) -> None:
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertTrue(payload["ok"])
        self.assertEqual([], payload["bindings"])
        self.assertEqual([], payload["unresolved"])
        self.assertEqual(0, payload["issue_count"])

    def test_the_preview_writes_nothing_at_all(self) -> None:
        self.open_unbound_twins()
        before = self.ledger_path.read_bytes()
        listing = sorted(item.name for item in self.state_dir.iterdir())
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual(before, self.ledger_path.read_bytes())
        self.assertEqual(
            listing, sorted(item.name for item in self.state_dir.iterdir())
        )

    def test_every_unbound_issue_is_offered_its_own_minted_key(self) -> None:
        self.open_unbound_twins()
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual(3, payload["issue_count"])
        self.assertEqual(0, payload["bound_count"])
        self.assertEqual(
            [
                ("ASI-1", "issue.asi-1"),
                ("ASI-2", "issue.asi-2"),
                ("ASI-3", "issue.asi-3"),
            ],
            [(item["issue_id"], item["identity_key"]) for item in payload["bindings"]],
        )

    def test_an_issue_published_with_its_binding_is_not_offered_one_again(self) -> None:
        self.publish(_publication())
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual(1, payload["bound_count"])
        self.assertEqual([], payload["bindings"])
        self.assertEqual(0, payload["unbound_count"])

    def test_a_reworded_twin_is_offered_as_a_pair_to_review_never_as_a_merge(
        self,
    ) -> None:
        first, second = self.open_unbound_twins()
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual(1, len(payload["unresolved"]))
        pair = payload["unresolved"][0]
        self.assertEqual([first, second], pair["issue_ids"])
        self.assertIn(projection.MATCH_SUMMARY, pair["reasons"])
        self.assertNotIn(
            assurance_models_reconcile.DECISION_MERGE,
            [item["decision"] for item in payload["manifest"]["decisions"]],
        )

    def test_one_pair_is_asked_about_once_rather_than_from_both_sides(self) -> None:
        self.open_unbound_twins()
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        pairs = [tuple(item["issue_ids"]) for item in payload["unresolved"]]
        self.assertEqual(len(pairs), len(set(pairs)))
        self.assertEqual([("ASI-1", "ASI-2")], pairs)

    def test_the_preview_carries_the_digest_it_read_the_inbox_at(self) -> None:
        self.open_unbound_twins()
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertEqual(self.inbox().catalog.digest, payload["catalog_digest"])
        self.assertEqual(
            payload["catalog_digest"], payload["manifest"]["catalog_digest"]
        )

    def test_the_manifest_is_the_preview_and_validates_as_one(self) -> None:
        self.open_unbound_twins()
        manifest = assurance_models_reconcile.parse_reconciliation_manifest(
            self.preview_manifest()
        )
        self.assertEqual(
            3, len(manifest.of_kind(assurance_models_reconcile.DECISION_BIND))
        )
        self.assertEqual(1, len(manifest.unresolved))

    def test_a_key_another_issue_already_owns_is_reported_blocked_not_offered(
        self,
    ) -> None:
        first, second = self.open_unbound_twins()
        ledger.append_identity_binding(
            assurance_models_inbox.IssueIdentityBinding(
                identity_key=assurance_models_primitives.identity_key_for_issue(second),
                issue_id=first,
                source="reconcile",
                bound_at=TIMESTAMP,
            ),
            state_dir=self.state_dir,
        )
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        self.assertNotIn(second, [item["issue_id"] for item in payload["bindings"]])
        self.assertEqual([second], [item["issue_id"] for item in payload["blocked"]])
        self.assertEqual(
            reconcile.IDENTITY_KEY_CONFLICT, payload["blocked"][0]["reason"]
        )

    def test_weaker_pairs_beyond_the_cap_are_counted_rather_than_dropped_silently(
        self,
    ) -> None:
        self.open_unbound_twins()
        preview = reconcile.build_preview(self.inbox(), limit=0)
        self.assertEqual((), preview.unresolved)
        self.assertEqual(1, preview.truncated_pairs)

    def test_text_and_json_agree_about_what_needs_deciding(self) -> None:
        self.open_unbound_twins()
        code, out, err = self.run_cli(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        _, payload, _ = self.run_json(["assurance", "reconcile"])
        self.assertIn(payload["catalog_digest"], out)
        self.assertIn("Safe bindings to add (3)", out)
        self.assertIn("Pairs awaiting a verdict (1)", out)
        self.assertIn("review ASI-1 + ASI-2", out)
        self.assertIn("Nothing was written", out)


class ApplyTests(ReconcileTestCase):
    def test_a_reviewed_manifest_binds_and_merges_in_one_append(self) -> None:
        self.open_unbound_twins()
        manifest = self.resolve_reviews(
            self.preview_manifest(), decision=assurance_models_reconcile.DECISION_MERGE
        )
        path = self.write_manifest(manifest)
        before = self.ledger_lines()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        self.assertTrue(payload["applied"])
        self.assertEqual(3, payload["bindings"])
        self.assertEqual(1, payload["merges"])
        self.assertEqual(4, payload["records"])
        self.assertEqual(before + 4, self.ledger_lines())

    def test_a_merged_away_key_still_resolves_to_the_surviving_issue(self) -> None:
        first, second = self.open_unbound_twins()
        path = self.write_manifest(
            self.resolve_reviews(
                self.preview_manifest(),
                decision=assurance_models_reconcile.DECISION_MERGE,
            )
        )
        code, _, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)

        inbox = self.inbox()
        surviving = inbox.issue_for_identity_key(
            assurance_models_primitives.identity_key_for_issue(second)
        )
        self.assertIsNotNone(surviving)
        assert surviving is not None
        self.assertEqual(first, surviving.issue_id)
        self.assertEqual((), inbox.anomalies)

    def test_applying_the_same_manifest_twice_writes_nothing_the_second_time(
        self,
    ) -> None:
        self.open_unbound_twins()
        path = self.write_manifest(
            self.resolve_reviews(
                self.preview_manifest(),
                decision=assurance_models_reconcile.DECISION_MERGE,
            )
        )
        code, _, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        settled = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        self.assertFalse(payload["applied"])
        self.assertEqual(0, payload["records"])
        self.assertEqual(settled, self.ledger_path.read_bytes())

    def test_a_manifest_of_keep_distinct_alone_records_the_ruling(self) -> None:
        self.open_unbound_twins()
        path = self.write_manifest(self.keep_distinct_only_manifest())
        before = self.ledger_lines()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        self.assertTrue(payload["applied"])
        self.assertEqual(1, payload["kept_distinct"])
        self.assertEqual(1, payload["records"])
        self.assertEqual(before + 1, self.ledger_lines())
        self.assertEqual(
            (ledger.KIND_ISSUE_KEPT_DISTINCT,),
            tuple(record.kind for record in self.ledger_view().records[before:]),
        )

    def test_an_unread_suggestion_refuses_the_whole_manifest(self) -> None:
        self.open_unbound_twins()
        path = self.write_manifest(self.preview_manifest())
        before = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual(reconcile.UNRESOLVED_DECISION, payload["reason"])
        self.assertIn("ASI-1+ASI-2", payload["error"])
        self.assertEqual(before, self.ledger_path.read_bytes())

    def test_a_manifest_reviewed_against_a_moved_catalog_is_refused(self) -> None:
        self.open_unbound_twins()
        manifest = self.resolve_reviews(
            self.preview_manifest(), decision=assurance_models_reconcile.DECISION_MERGE
        )
        self.observe(
            _finding(
                component="agent_session_loop/mailbox.py",
                invariant_key="mailbox.no-stale-read",
                summary="A stale mailbox record was read as live guidance.",
                localization=("agent_session_loop/mailbox.py",),
            ),
            run_id="20260810T101114222222Z-4245",
        )
        path = self.write_manifest(manifest)
        before = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual(reconcile.STALE_CATALOG_DIGEST, payload["reason"])
        self.assertIn(manifest["catalog_digest"], payload["error"])
        self.assertEqual(before, self.ledger_path.read_bytes())

    def test_one_refused_decision_takes_the_whole_manifest_with_it(self) -> None:
        first, second = self.open_unbound_twins()
        manifest = self.preview_manifest()
        manifest["decisions"] = [
            item
            for item in manifest["decisions"]
            if item["decision"] == assurance_models_reconcile.DECISION_BIND
        ] + [
            {
                "decision": assurance_models_reconcile.DECISION_MERGE,
                "source_issue_id": "ASI-99",
                "target_issue_id": first,
                "note": None,
            }
        ]
        path = self.write_manifest(manifest)
        before = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual(projection.MERGE_UNKNOWN_SOURCE, payload["reason"])
        self.assertEqual(before, self.ledger_path.read_bytes())
        self.assertEqual({}, dict(self.inbox().identity_bindings))

    def test_two_bindings_of_one_key_inside_a_manifest_check_against_each_other(
        self,
    ) -> None:
        first, second = self.open_unbound_twins()
        manifest = self.preview_manifest()
        manifest["decisions"] = [
            {
                "decision": assurance_models_reconcile.DECISION_BIND,
                "issue_id": first,
                "identity_key": assurance_models_primitives.identity_key_for_issue(
                    first
                ),
                "note": None,
            },
            {
                "decision": assurance_models_reconcile.DECISION_BIND,
                "issue_id": second,
                "identity_key": assurance_models_primitives.identity_key_for_issue(
                    first
                ),
                "note": None,
            },
        ]
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            assurance_models_reconcile.parse_reconciliation_manifest(manifest)

    def test_binding_a_key_to_an_issue_that_merged_away_is_refused(self) -> None:
        first, second = self.open_unbound_twins()
        projection.record_merge(second, first, state_dir=self.state_dir)
        inbox = self.inbox()
        path = self.write_manifest(
            {
                "kind": assurance_models_reconcile.RECONCILIATION_MANIFEST_KIND,
                "version": assurance_models_reconcile.RECONCILIATION_MANIFEST_VERSION,
                "catalog_digest": inbox.catalog.digest,
                "generated_at": TIMESTAMP,
                "decisions": [
                    {
                        "decision": assurance_models_reconcile.DECISION_BIND,
                        "issue_id": second,
                        "identity_key": identity_key_for_issue(second),
                        "note": None,
                    }
                ],
            }
        )
        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual(reconcile.ISSUE_NOT_CANONICAL, payload["reason"])

    def test_a_missing_manifest_names_how_to_produce_one(self) -> None:
        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", "/nonexistent/manifest.json", "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual("manifest-read-failed", payload["reason"])

    def test_a_manifest_that_is_not_json_is_refused_before_the_ledger_is_touched(
        self,
    ) -> None:
        self.open_unbound_twins()
        path = Path(self._tmp.name) / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        before = self.ledger_path.read_bytes()

        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(1, code)
        self.assertEqual("manifest-read-failed", payload["reason"])
        self.assertEqual(before, self.ledger_path.read_bytes())

    def test_apply_reports_the_same_counts_in_text_and_json(self) -> None:
        self.open_unbound_twins()
        path = self.write_manifest(
            self.resolve_reviews(
                self.preview_manifest(),
                decision=assurance_models_reconcile.DECISION_MERGE,
            )
        )
        code, out, err = self.run_cli(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        self.assertIn(
            "Applied 4 record(s): 3 binding(s), 0 ruling(s) and 1 merge(s).", out
        )
        self.assertIn("Merging is irreversible.", out)
