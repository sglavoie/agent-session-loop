# ruff: noqa: F403, F405, I001
"""Assurance reconciliation schema and wiring tests."""

from __future__ import annotations

# ruff: noqa: F403, F405
from tests.asl_assurance_reconcile_test_support import *


ASL_TEST_PORTABLE = True


class ManifestSchemaTests(ReconcileTestCase):
    def _manifest(self, decisions: list[dict]) -> dict:
        return {
            "kind": assurance_models_reconcile.RECONCILIATION_MANIFEST_KIND,
            "version": assurance_models_reconcile.RECONCILIATION_MANIFEST_VERSION,
            "catalog_digest": "d" * 64,
            "generated_at": TIMESTAMP,
            "decisions": decisions,
        }

    def test_every_decision_kind_round_trips_through_its_own_shape(self) -> None:
        payload = self._manifest(
            [
                {
                    "decision": "bind",
                    "issue_id": "ASI-1",
                    "identity_key": "issue.asi-1",
                    "note": None,
                },
                {
                    "decision": "merge",
                    "source_issue_id": "ASI-2",
                    "target_issue_id": "ASI-1",
                    "note": "same defect",
                },
                {
                    "decision": "keep-distinct",
                    "issue_ids": ["ASI-1", "ASI-3"],
                    "note": None,
                },
                {
                    "decision": "review",
                    "issue_ids": ["ASI-1", "ASI-3"],
                    "reasons": ["invariant"],
                    "similarity": 0.75,
                    "note": None,
                },
            ]
        )
        manifest = assurance_models_reconcile.parse_reconciliation_manifest(payload)
        self.assertEqual(payload, manifest.to_dict())
        self.assertEqual(
            1, len(manifest.of_kind(assurance_models_reconcile.DECISION_BIND))
        )
        self.assertEqual(1, len(manifest.unresolved))

    def test_a_document_of_another_kind_is_refused_as_the_wrong_document(self) -> None:
        payload = self._manifest([])
        payload["kind"] = "assurance-summary"
        with self.assertRaises(assurance_models_primitives.AssuranceError) as raised:
            assurance_models_reconcile.parse_reconciliation_manifest(payload)
        self.assertIn("kind", str(raised.exception))

    def test_an_unsupported_version_names_what_this_build_reads(self) -> None:
        payload = self._manifest([])
        payload["version"] = 2
        with self.assertRaises(assurance_models_primitives.AssuranceError) as raised:
            assurance_models_reconcile.parse_reconciliation_manifest(payload)
        self.assertIn("version(s) 1", str(raised.exception))

    def test_a_decision_carrying_a_foreign_field_is_refused(self) -> None:
        payload = self._manifest(
            [
                {
                    "decision": "bind",
                    "issue_id": "ASI-1",
                    "identity_key": "issue.asi-1",
                    "target_issue_id": "ASI-2",
                }
            ]
        )
        with self.assertRaises(assurance_models_primitives.AssuranceError) as raised:
            assurance_models_reconcile.parse_reconciliation_manifest(payload)
        self.assertIn("target_issue_id", str(raised.exception))

    def test_binding_one_key_twice_is_refused_before_anything_is_applied(self) -> None:
        payload = self._manifest(
            [
                {
                    "decision": "bind",
                    "issue_id": "ASI-1",
                    "identity_key": "issue.asi-1",
                },
                {
                    "decision": "bind",
                    "issue_id": "ASI-2",
                    "identity_key": "issue.asi-1",
                },
            ]
        )
        with self.assertRaises(assurance_models_primitives.AssuranceError) as raised:
            assurance_models_reconcile.parse_reconciliation_manifest(payload)
        self.assertIn("issue.asi-1", str(raised.exception))

    def test_merging_one_issue_twice_is_refused_because_only_one_would_land(
        self,
    ) -> None:
        payload = self._manifest(
            [
                {
                    "decision": "merge",
                    "source_issue_id": "ASI-2",
                    "target_issue_id": "ASI-1",
                },
                {
                    "decision": "merge",
                    "source_issue_id": "ASI-2",
                    "target_issue_id": "ASI-3",
                },
            ]
        )
        with self.assertRaises(assurance_models_primitives.AssuranceError) as raised:
            assurance_models_reconcile.parse_reconciliation_manifest(payload)
        self.assertIn("ASI-2", str(raised.exception))

    def test_a_merge_into_itself_is_refused_by_the_schema(self) -> None:
        payload = self._manifest(
            [
                {
                    "decision": "merge",
                    "source_issue_id": "ASI-1",
                    "target_issue_id": "ASI-1",
                }
            ]
        )
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            assurance_models_reconcile.parse_reconciliation_manifest(payload)

    def test_a_pair_decision_naming_one_issue_is_refused(self) -> None:
        for kind in ("keep-distinct", "review"):
            with self.subTest(kind=kind):
                payload = self._manifest([{"decision": kind, "issue_ids": ["ASI-1"]}])
                with self.assertRaises(assurance_models_primitives.AssuranceError):
                    assurance_models_reconcile.parse_reconciliation_manifest(payload)

    def test_a_catalog_digest_that_is_not_a_digest_is_refused(self) -> None:
        payload = self._manifest([])
        payload["catalog_digest"] = "not-a-digest"
        with self.assertRaises(assurance_models_primitives.AssuranceError):
            assurance_models_reconcile.parse_reconciliation_manifest(payload)


class ReconcileWiringTests(ReconcileTestCase):
    def test_reconcile_subcommand_is_wired_and_previews_by_default(self) -> None:
        args = self.parser.parse_args(["assurance", "reconcile"])
        self.assertEqual(
            "assurance_cli:assurance_reconcile_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args), assurance_cli.assurance_reconcile_from_args
        )
        self.assertIsNone(args.apply)
        self.assertFalse(args.yes)
        self.assertFalse(args.json)

    def test_apply_takes_a_manifest_path(self) -> None:
        args = self.parser.parse_args(
            ["assurance", "reconcile", "--apply", "manifest.json", "--yes"]
        )
        self.assertEqual("manifest.json", args.apply)
        self.assertTrue(args.yes)
