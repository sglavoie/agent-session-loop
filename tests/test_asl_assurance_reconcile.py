"""Reconciliation tests: manifest schema, read-only preview, atomic idempotent apply."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_cli, cli
from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop import assurance_reconcile as reconcile
from agent_session_loop.assurance_projection import AuditPublication

RUN_ID = "20260810T101112123456Z-4242"
OTHER_RUN_ID = "20260810T101112999999Z-4243"
THIRD_RUN_ID = "20260810T101113111111Z-4244"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
LATER_TIMESTAMP = "2026-08-10T11:11:12+00:00"
LAST_TIMESTAMP = "2026-08-10T12:11:12+00:00"
INPUT_DIGEST = "a" * 64
OTHER_INPUT_DIGEST = "b" * 64
THIRD_INPUT_DIGEST = "c" * 64


def _finding(**overrides) -> models.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the retry budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
        "localization": ("agent_session_loop/runner.py",),
    }
    payload.update(overrides)
    return models.Finding(**payload)


def _artifacts(digest: str = INPUT_DIGEST) -> tuple[models.EvidenceRef, ...]:
    return (
        models.EvidenceRef(
            kind="artifact", locator="supervisor-audit-input.json", digest=digest
        ),
    )


def _publication(**overrides) -> AuditPublication:
    payload = {
        "run_id": RUN_ID,
        "state": "findings",
        "eligible": True,
        "findings": (_finding(),),
        "artifacts": _artifacts(),
        "completed_at": TIMESTAMP,
    }
    payload.update(overrides)
    return AuditPublication(**payload)


class ReconcileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)
        self.parser = cli.build_parser(prog="asl")

    # -- fixtures ---------------------------------------------------------

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", TIMESTAMP)
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

    def observe(
        self, finding: models.Finding, *, run_id: str, state_dir: Path | None = None
    ) -> str:
        """Open an issue the way pre-identity history did: minted, never bound."""
        record = ledger.append_issue_observation(
            models.IssueObservation(
                run_id=run_id, finding=finding, observed_at=TIMESTAMP
            ),
            state_dir=state_dir or self.state_dir,
            recorded_at=TIMESTAMP,
        )
        assert record.issue_id is not None
        return record.issue_id

    def open_unbound_twins(self, *, state_dir: Path | None = None) -> tuple[str, str]:
        """Two unbound issues one reworded invariant key apart, plus a third."""
        first = self.observe(_finding(), run_id=RUN_ID, state_dir=state_dir)
        second = self.observe(
            _finding(
                invariant_key="retry.budget-respect",
                summary="A retry ran after the retry budget was fully exhausted.",
            ),
            run_id=OTHER_RUN_ID,
            state_dir=state_dir,
        )
        self.observe(
            _finding(
                component="agent_session_loop/supervisor_audit.py",
                invariant_key="audit.report-present",
                summary="The audit report never appeared in the run directory.",
                localization=("agent_session_loop/supervisor_audit.py",),
            ),
            run_id=THIRD_RUN_ID,
            state_dir=state_dir,
        )
        return first, second

    def inbox(self) -> projection.AssuranceProjection:
        return projection.read_projection(state_dir=self.state_dir)

    def ledger_view(self) -> ledger.LedgerView:
        return ledger.read_ledger(state_dir=self.state_dir)

    def ledger_lines(self) -> int:
        if not self.ledger_path.exists():
            return 0
        return len(self.ledger_path.read_text(encoding="utf-8").splitlines())

    def write_manifest(self, payload: dict, name: str = "manifest.json") -> Path:
        path = Path(self._tmp.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    # -- invocation -------------------------------------------------------

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        args = self.parser.parse_args([*argv, "--state-dir", str(self.state_dir)])
        handler = cli.resolve_handler(args)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handler(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, argv: list[str]) -> tuple[int, dict, str]:
        code, out, err = self.run_cli([*argv, "--json"])
        return code, json.loads(out), err

    def preview_manifest(self) -> dict:
        code, payload, err = self.run_json(["assurance", "reconcile"])
        self.assertEqual(0, code, err)
        return payload["manifest"]

    def resolve_reviews(self, manifest: dict, *, decision: str) -> dict:
        """Rewrite every open suggestion the way a reviewer would."""
        decisions = []
        for item in manifest["decisions"]:
            if item["decision"] != models.DECISION_REVIEW:
                decisions.append(item)
            elif decision == models.DECISION_MERGE:
                decisions.append(
                    {
                        "decision": models.DECISION_MERGE,
                        "source_issue_id": item["issue_ids"][1],
                        "target_issue_id": item["issue_ids"][0],
                        "note": None,
                    }
                )
            else:
                decisions.append(
                    {
                        "decision": models.DECISION_KEEP_DISTINCT,
                        "issue_ids": item["issue_ids"],
                        "note": None,
                    }
                )
        return {**manifest, "decisions": decisions}

    def keep_distinct_only_manifest(self) -> dict:
        """Rule every open pair distinct, and leave the bindings out of it."""
        manifest = self.resolve_reviews(
            self.preview_manifest(), decision=models.DECISION_KEEP_DISTINCT
        )
        return {
            **manifest,
            "decisions": [
                item
                for item in manifest["decisions"]
                if item["decision"] == models.DECISION_KEEP_DISTINCT
            ],
        }

    def apply_path(self, path: Path) -> dict:
        """Apply one manifest file, asserting it was accepted."""
        code, payload, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)
        return payload


class ManifestSchemaTests(ReconcileTestCase):
    def _manifest(self, decisions: list[dict]) -> dict:
        return {
            "kind": models.RECONCILIATION_MANIFEST_KIND,
            "version": models.RECONCILIATION_MANIFEST_VERSION,
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
        manifest = models.parse_reconciliation_manifest(payload)
        self.assertEqual(payload, manifest.to_dict())
        self.assertEqual(1, len(manifest.of_kind(models.DECISION_BIND)))
        self.assertEqual(1, len(manifest.unresolved))

    def test_a_document_of_another_kind_is_refused_as_the_wrong_document(self) -> None:
        payload = self._manifest([])
        payload["kind"] = "assurance-summary"
        with self.assertRaises(models.AssuranceError) as raised:
            models.parse_reconciliation_manifest(payload)
        self.assertIn("kind", str(raised.exception))

    def test_an_unsupported_version_names_what_this_build_reads(self) -> None:
        payload = self._manifest([])
        payload["version"] = 2
        with self.assertRaises(models.AssuranceError) as raised:
            models.parse_reconciliation_manifest(payload)
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
        with self.assertRaises(models.AssuranceError) as raised:
            models.parse_reconciliation_manifest(payload)
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
        with self.assertRaises(models.AssuranceError) as raised:
            models.parse_reconciliation_manifest(payload)
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
        with self.assertRaises(models.AssuranceError) as raised:
            models.parse_reconciliation_manifest(payload)
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
        with self.assertRaises(models.AssuranceError):
            models.parse_reconciliation_manifest(payload)

    def test_a_pair_decision_naming_one_issue_is_refused(self) -> None:
        for kind in ("keep-distinct", "review"):
            with self.subTest(kind=kind):
                payload = self._manifest([{"decision": kind, "issue_ids": ["ASI-1"]}])
                with self.assertRaises(models.AssuranceError):
                    models.parse_reconciliation_manifest(payload)

    def test_a_catalog_digest_that_is_not_a_digest_is_refused(self) -> None:
        payload = self._manifest([])
        payload["catalog_digest"] = "not-a-digest"
        with self.assertRaises(models.AssuranceError):
            models.parse_reconciliation_manifest(payload)


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
            models.DECISION_MERGE,
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
        manifest = models.parse_reconciliation_manifest(self.preview_manifest())
        self.assertEqual(3, len(manifest.of_kind(models.DECISION_BIND)))
        self.assertEqual(1, len(manifest.unresolved))

    def test_a_key_another_issue_already_owns_is_reported_blocked_not_offered(
        self,
    ) -> None:
        first, second = self.open_unbound_twins()
        ledger.append_identity_binding(
            models.IssueIdentityBinding(
                identity_key=models.identity_key_for_issue(second),
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
            self.preview_manifest(), decision=models.DECISION_MERGE
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
                self.preview_manifest(), decision=models.DECISION_MERGE
            )
        )
        code, _, err = self.run_json(
            ["assurance", "reconcile", "--apply", str(path), "--yes"]
        )
        self.assertEqual(0, code, err)

        inbox = self.inbox()
        surviving = inbox.issue_for_identity_key(models.identity_key_for_issue(second))
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
                self.preview_manifest(), decision=models.DECISION_MERGE
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
            self.preview_manifest(), decision=models.DECISION_MERGE
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
            if item["decision"] == models.DECISION_BIND
        ] + [
            {
                "decision": models.DECISION_MERGE,
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
                "decision": models.DECISION_BIND,
                "issue_id": first,
                "identity_key": models.identity_key_for_issue(first),
                "note": None,
            },
            {
                "decision": models.DECISION_BIND,
                "issue_id": second,
                "identity_key": models.identity_key_for_issue(first),
                "note": None,
            },
        ]
        with self.assertRaises(models.AssuranceError):
            models.parse_reconciliation_manifest(manifest)

    def test_binding_a_key_to_an_issue_that_merged_away_is_refused(self) -> None:
        first, second = self.open_unbound_twins()
        projection.record_merge(second, first, state_dir=self.state_dir)
        inbox = self.inbox()
        path = self.write_manifest(
            {
                "kind": models.RECONCILIATION_MANIFEST_KIND,
                "version": models.RECONCILIATION_MANIFEST_VERSION,
                "catalog_digest": inbox.catalog.digest,
                "generated_at": TIMESTAMP,
                "decisions": [
                    {
                        "decision": models.DECISION_BIND,
                        "issue_id": second,
                        "identity_key": models.identity_key_for_issue(second),
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
                self.preview_manifest(), decision=models.DECISION_MERGE
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
            self.resolve_reviews(first, decision=models.DECISION_KEEP_DISTINCT)
        )
        self.apply_path(path)

        second = self.preview_manifest()
        self.assertEqual(
            [],
            [
                item
                for item in second["decisions"]
                if item["decision"] == models.DECISION_REVIEW
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
                if item["decision"] == models.DECISION_REVIEW
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
                "decision": models.DECISION_KEEP_DISTINCT,
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
            models.IssueKeptDistinct(
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
                self.preview_manifest(), decision=models.DECISION_KEEP_DISTINCT
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
        manifest = models.parse_reconciliation_manifest(
            self.resolve_reviews(
                self.preview_manifest(), decision=models.DECISION_MERGE
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
        manifest = models.parse_reconciliation_manifest(
            self.resolve_reviews(
                self.preview_manifest(), decision=models.DECISION_MERGE
            )
        )
        first = reconcile.apply_manifest(manifest, state_dir=self.state_dir)
        self.assertTrue(first.applied)
        settled = self.ledger_path.read_bytes()

        second = reconcile.apply_manifest(manifest, state_dir=self.state_dir)
        self.assertFalse(second.applied)
        self.assertEqual(settled, self.ledger_path.read_bytes())


if __name__ == "__main__":  # pragma: no cover - manual entry point
    unittest.main()
