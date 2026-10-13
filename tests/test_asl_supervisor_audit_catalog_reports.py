"""Agent catalog, report staging, pause exits, and path-keyed redaction."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    artifacts,
    events,
    supervisor_audit_agent,
    supervisor_audit_checks,
    supervisor_audit_evidence_collector,
    supervisor_audit_evidence_constants,
    supervisor_audit_evidence_models,
)
from agent_session_loop.assurance_models_artifacts import audit_artifact_name
from agent_session_loop.assurance_models_audit import SupervisorAudit
from agent_session_loop.assurance_models_findings import EvidenceRef, LaneEvidence
from agent_session_loop.assurance_models_primitives import (
    AssuranceError,
    canonical_json_bytes,
    issue_id_number,
)
from agent_session_loop.assurance_models_vocab import (
    AUDIT_MARKDOWN_FILENAME,
    AUDIT_REPORT_FILENAME,
    RUBRIC_LANES,
)
from agent_session_loop.assurance_projection import CatalogEntry, IdentityCatalog
from tests.support_supervisor_audit import RUN_ID


def _catalog_entry(number: int, *, last_seen_at: str) -> CatalogEntry:
    return CatalogEntry(
        issue_id=f"ASI-{number}",
        state="open",
        severity="medium",
        category="identity",
        component="workspace-registry",
        invariant_key=f"identity.invariant-{number:04d}",
        lane="identity-authority",
        summary="The registry named a repository the manifest never declared. " * 8,
        fingerprints=(f"{number:064d}",),
        identity_keys=(),
        primary_identity_key=None,
        localization=(),
        summary_tokens=(),
        invariant_tokens=(),
        occurrence_count=1,
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at=last_seen_at,
    )


class AgentCatalogTests(unittest.TestCase):
    """The supplied catalog is bounded, ordered, and honest about what it drops."""

    def _catalog(self, count: int) -> IdentityCatalog:
        return IdentityCatalog(
            entries=tuple(
                _catalog_entry(
                    number, last_seen_at=f"2026-02-{number % 28 + 1:02d}T00:00:00+00:00"
                )
                for number in range(1, count + 1)
            )
        )

    def test_a_small_catalog_is_supplied_whole(self) -> None:
        supplied = supervisor_audit_agent.build_agent_catalog(self._catalog(5))

        self.assertEqual(5, len(supplied.entries))
        self.assertEqual((), supplied.omitted)
        self.assertEqual(0, supplied.to_dict()["omitted"])

    def test_a_large_catalog_is_bounded_and_reports_what_it_dropped(self) -> None:
        catalog = self._catalog(1000)

        supplied = supervisor_audit_agent.build_agent_catalog(catalog)
        rendered = json.dumps(supplied.to_dict(), separators=(",", ":"))

        self.assertLessEqual(
            len(rendered.encode("utf-8")),
            supervisor_audit_agent.MAX_AGENT_CATALOG_BYTES,
        )
        self.assertTrue(supplied.omitted)
        self.assertEqual(
            len(catalog.entries), len(supplied.entries) + len(supplied.omitted)
        )
        self.assertEqual(len(supplied.omitted), supplied.to_dict()["omitted"])
        self.assertEqual(set(), supplied.issue_ids.intersection(supplied.omitted))

    def test_the_bound_is_spent_on_the_most_recently_seen_issues(self) -> None:
        catalog = IdentityCatalog(
            entries=(
                _catalog_entry(1, last_seen_at="2026-01-01T00:00:00+00:00"),
                _catalog_entry(2, last_seen_at="2026-06-01T00:00:00+00:00"),
                _catalog_entry(3, last_seen_at="2026-03-01T00:00:00+00:00"),
            )
        )
        whole = supervisor_audit_agent.build_agent_catalog(catalog)
        # Room for the whole listing less one entry and the comma before it.
        bound = len(canonical_json_bytes(whole.to_dict())) - len(
            canonical_json_bytes(whole.entries[0].to_dict())
        )

        with mock.patch.object(
            supervisor_audit_agent, "MAX_AGENT_CATALOG_BYTES", bound
        ):
            supplied = supervisor_audit_agent.build_agent_catalog(catalog)

        self.assertEqual(["ASI-2", "ASI-3"], [e.issue_id for e in supplied.entries])
        self.assertEqual(("ASI-1",), supplied.omitted)

    def test_supplied_entries_are_listed_in_issue_order(self) -> None:
        supplied = supervisor_audit_agent.build_agent_catalog(self._catalog(30))

        self.assertEqual(
            [f"ASI-{number}" for number in range(1, 31)],
            [entry.issue_id for entry in supplied.entries],
        )

    def test_the_digest_names_the_whole_catalog_not_the_sample(self) -> None:
        catalog = self._catalog(1000)

        supplied = supervisor_audit_agent.build_agent_catalog(catalog)

        self.assertTrue(supplied.omitted)
        self.assertEqual(catalog.digest, supplied.digest)

    def test_an_omitted_issue_is_refused_without_being_denied(self) -> None:
        catalog = self._catalog(1000)
        supplied = supervisor_audit_agent.build_agent_catalog(catalog)
        omitted = supplied.omitted[0]
        reply = json.dumps(
            {
                "schema_version": supervisor_audit_agent.AGENT_OUTPUT_SCHEMA_VERSION,
                "lanes": [
                    {
                        "lane": lane,
                        "status": "pass",
                        "summary": f"{lane} judged from the recorded evidence",
                        "evidence": ["run:supervisor-audit-input.json"],
                    }
                    for lane in RUBRIC_LANES
                ],
                "findings": [
                    {
                        "lane": "identity-authority",
                        "severity": "medium",
                        "category": "identity",
                        "component": "workspace-registry",
                        "invariant_key": "identity.agent-observed-drift",
                        "summary": "The registry named an undeclared repository.",
                        "recommendation": "Re-derive the registry.",
                        "evidence": ["run:supervisor-audit-input.json"],
                        "identity": {"decision": "existing", "issue_id": omitted},
                    }
                ],
            }
        )

        with self.assertRaises(supervisor_audit_agent.AgentOutputError) as raised:
            supervisor_audit_agent.parse_agent_audit(
                f"```json\n{reply}\n```",
                declared=["run:supervisor-audit-input.json"],
                catalog=supplied,
            )

        self.assertEqual("invalid-reference", raised.exception.outcome)
        self.assertIn("size bound kept", raised.exception.detail)
        self.assertNotIn("does not list", raised.exception.detail)

    def test_a_candidate_the_bound_dropped_is_promoted_for_the_retry(self) -> None:
        catalog = self._catalog(1000)
        supplied = supervisor_audit_agent.build_agent_catalog(catalog)
        omitted = supplied.omitted[0]

        widened = supervisor_audit_agent.widen_agent_catalog(
            supplied, catalog, [omitted]
        )

        self.assertIn(omitted, widened.issue_ids)
        self.assertNotIn(omitted, widened.omitted)
        self.assertEqual(len(supplied.entries) + 1, len(widened.entries))
        self.assertEqual(len(supplied.omitted) - 1, len(widened.omitted))
        self.assertEqual(
            sorted(widened.entries, key=lambda entry: issue_id_number(entry.issue_id)),
            list(widened.entries),
        )

    def test_widening_restates_the_same_inbox_and_keeps_the_digest(self) -> None:
        catalog = self._catalog(1000)
        supplied = supervisor_audit_agent.build_agent_catalog(catalog)

        widened = supervisor_audit_agent.widen_agent_catalog(
            supplied, catalog, list(supplied.omitted[:3])
        )

        self.assertEqual(supplied.digest, widened.digest)
        self.assertEqual(
            len(catalog.entries), len(widened.entries) + len(widened.omitted)
        )

    def test_widening_toward_a_listed_or_unknown_issue_changes_nothing(self) -> None:
        catalog = self._catalog(5)
        supplied = supervisor_audit_agent.build_agent_catalog(catalog)

        self.assertIs(
            supplied,
            supervisor_audit_agent.widen_agent_catalog(supplied, catalog, ["ASI-1"]),
        )
        self.assertIs(
            supplied,
            supervisor_audit_agent.widen_agent_catalog(supplied, catalog, ["ASI-4242"]),
        )


class ReportStagingTests(unittest.TestCase):
    """Canonical reports appear together, at their canonical names, or not at all."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.audit = SupervisorAudit(
            run_id=RUN_ID,
            state="all-clear",
            input_digest="b" * 64,
            generated_at="2026-08-10T10:11:12+00:00",
            evidence=(EvidenceRef(kind="event", locator="events.jsonl"),),
            lanes=tuple(
                LaneEvidence(
                    lane=lane,
                    status="pass",
                    summary=f"{lane} judged from the event ledger",
                    evidence=("events.jsonl",),
                )
                for lane in RUBRIC_LANES
            ),
        )

    @property
    def canonical(self) -> tuple[Path, Path]:
        return (
            self.run_dir / audit_artifact_name(AUDIT_REPORT_FILENAME),
            self.run_dir / audit_artifact_name(AUDIT_MARKDOWN_FILENAME),
        )

    def stage(self) -> supervisor_audit_agent.StagedAuditReports:
        return supervisor_audit_agent.stage_audit_reports(
            self.run_dir, self.audit, durable=False
        )

    def test_staging_writes_both_reports_outside_their_canonical_names(self) -> None:
        staged = self.stage()

        self.assertEqual(2, len(staged.entries))
        for path, canonical, digest in staged.entries:
            self.assertTrue(path.is_file())
            self.assertFalse(canonical.exists())
            self.assertEqual(digest, artifacts.sha256_hex(path.read_bytes()))
        self.assertEqual(
            ["run/supervisor-audit.json", "run/supervisor-audit.md"],
            [reference.locator for reference in staged.references],
        )

    def test_promotion_gives_both_reports_their_canonical_names(self) -> None:
        staged = self.stage()

        staged.promote()

        report, markdown = self.canonical
        self.assertTrue(report.is_file())
        self.assertTrue(markdown.is_file())
        self.assertEqual(
            json.loads(report.read_text(encoding="utf-8"))["run_id"], RUN_ID
        )

    def test_a_taken_canonical_name_refuses_the_whole_promotion(self) -> None:
        staged = self.stage()
        report, markdown = self.canonical
        report.write_text("an earlier publication already claimed this name\n")

        with self.assertRaises(AssuranceError) as caught:
            staged.promote()

        self.assertIn("could not promote audit report", str(caught.exception))
        # The markdown is ordered after the report, so nothing was linked before the
        # refusal; the assertion that matters is that it is not left behind either.
        self.assertFalse(markdown.exists())

    def test_a_refusal_after_a_partial_promotion_leaves_no_report(self) -> None:
        staged = self.stage()
        report, markdown = self.canonical
        markdown.write_text("an earlier publication already claimed this name\n")
        original = markdown.read_bytes()

        with self.assertRaises(AssuranceError):
            staged.promote()

        self.assertFalse(report.exists())
        self.assertEqual(original, markdown.read_bytes())

    def test_unpromoting_frees_the_canonical_names_for_another_attempt(self) -> None:
        staged = self.stage()
        staged.promote()

        staged.unpromote()

        for path in self.canonical:
            self.assertFalse(path.exists())
        staged.promote()
        for path in self.canonical:
            self.assertTrue(path.is_file())

    def test_discarding_removes_the_staging_directory_but_not_the_reports(self) -> None:
        staged = self.stage()
        staged.promote()

        staged.discard()

        self.assertFalse(staged.directory.exists())
        for path in self.canonical:
            self.assertTrue(path.is_file())

    def test_staging_clears_scratch_an_earlier_attempt_left_behind(self) -> None:
        abandoned = self.run_dir / supervisor_audit_agent.AUDIT_STAGING_DIR_NAME
        abandoned.mkdir()
        (abandoned / "half-written.json").write_text("{")

        staged = self.stage()

        self.assertFalse((abandoned / "half-written.json").exists())
        for path, _, _ in staged.entries:
            self.assertTrue(path.is_file())

    def test_an_unwritable_run_directory_refuses_staging(self) -> None:
        blocked = self.run_dir / "blocked"
        blocked.write_text("a file where the run directory should be\n")

        with self.assertRaises(AssuranceError) as caught:
            supervisor_audit_agent.stage_audit_reports(
                blocked, self.audit, durable=False
            )

        self.assertIn("could not create audit staging directory", str(caught.exception))

    def test_writing_reports_twice_refuses_the_second_attempt(self) -> None:
        supervisor_audit_agent.write_audit_reports(
            self.run_dir, self.audit, durable=False
        )

        with self.assertRaises(AssuranceError):
            supervisor_audit_agent.write_audit_reports(
                self.run_dir, self.audit, durable=False
            )

        self.assertFalse(
            (self.run_dir / supervisor_audit_agent.AUDIT_STAGING_DIR_NAME).exists()
        )


class WorkerPauseExitAuditTests(unittest.TestCase):
    def bundle(self):
        result = {
            "session_id": "1",
            "session": 1,
            "turn_index": 2,
            "returncode": 143,
            "runner_pause": "blocked - operator input needed",
            "runner_pause_source": "last_message",
            "status_result": "blocked",
            "pending_completed_ids": [],
        }
        return {
            "status": {
                "paused": True,
                "ok": False,
                "pause_reason": "blocked",
                "pause_message": result["runner_pause"],
                "pause_details": {"session": 1, "source": "last_message"},
            },
            "turns": [{"name": "turn-0002-session-1", "result": result}],
            "events": {
                "records": [
                    {
                        "name": events.TURN_EXECUTION_FINISHED_EVENT,
                        "payload": {
                            "session_id": "1",
                            "session": 1,
                            "turn_index": 2,
                            "returncode": 143,
                        },
                    }
                ]
            },
        }

    def checks(self, bundle):
        return {
            r.check: r for r in supervisor_audit_checks.run_deterministic_checks(bundle)
        }

    def test_a_held_worker_pause_accounts_only_for_its_exact_exit(self):
        bundle = self.bundle()
        results = self.checks(bundle)
        self.assertEqual("pass", results["worker-pause-exits"].status)
        self.assertEqual("pass", results["unclassified-anomalies"].status)
        bundle["events"]["records"].append(
            {
                "name": events.TURN_EXECUTION_FINISHED_EVENT,
                "payload": {
                    "session_id": "1",
                    "session": 1,
                    "turn_index": 3,
                    "returncode": 143,
                },
            }
        )
        self.assertEqual("fail", self.checks(bundle)["unclassified-anomalies"].status)

    def test_untrusted_or_contradictory_pause_evidence_is_not_exempt(self):
        changes_to_try: list[dict[str, Any]] = [
            {"runner_pause_source": "stderr"},
            {"returncode": 137},
            {"runner_pause": ""},
            {"status_result": "complete"},
            {"pending_completed_ids": ["1"]},
        ]
        for changes in changes_to_try:
            bundle = self.bundle()
            bundle["turns"][0]["result"].update(changes)
            results = self.checks(bundle)
            self.assertEqual("fail", results["worker-pause-exits"].status)
            self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_an_unrelated_pause_accounts_for_no_exit(self):
        for changes in [
            {"paused": False},
            {"ok": True},
            {"pause_message": "other pause"},
            {"pause_details": {"session": 9}},
        ]:
            bundle = self.bundle()
            bundle["status"].update(changes)
            results = self.checks(bundle)
            self.assertEqual("fail", results["worker-pause-exits"].status)
            self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_missing_session_identity_explains_no_exit(self):
        bundle = self.bundle()
        bundle["events"]["records"][0]["payload"]["session_id"] = None
        bundle["turns"][0]["result"]["session_id"] = None
        bundle["status"]["pause_details"]["session"] = None
        results = self.checks(bundle)
        self.assertEqual("fail", results["worker-pause-exits"].status)
        self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_a_guard_refusal_must_reconcile_the_workers_completion(self):
        bundle = self.bundle()
        guard = {
            "passed": False,
            "reverted_to_pending": True,
            "reason": events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
        }
        bundle["status"].update(
            pause_reason=guard["reason"],
            pause_message="guard refused",
            pause_details={"session": 1},
        )
        result = bundle["turns"][0]["result"]
        result.update(workspace_write_guard=guard, pending_completed_ids=["1"])
        results = self.checks(bundle)
        self.assertEqual("pass", results["worker-pause-exits"].status)
        self.assertEqual("pass", results["unclassified-anomalies"].status)
        guard["reverted_to_pending"] = False
        results = self.checks(bundle)
        self.assertEqual("fail", results["worker-pause-exits"].status)
        self.assertEqual("fail", results["unclassified-anomalies"].status)

    def test_an_aborted_exit_missing_its_acknowledgment_has_a_named_owner(self):
        bundle = self.bundle()
        bundle["events"]["records"][0]["payload"].update(aborted=True, returncode=137)
        results = self.checks(bundle)
        self.assertEqual("fail", results["operator-abort"].status)
        self.assertIn(
            "without an abort acknowledgment", results["operator-abort"].detail
        )
        self.assertEqual("pass", results["unclassified-anomalies"].status)
        bundle["omissions"] = [{"section": "events", "reason": "size-cap", "count": 1}]
        self.assertEqual("unavailable", self.checks(bundle)["operator-abort"].status)


class PathKeyedRedactionTests(unittest.TestCase):
    """A path used as a map key is data, not a field name (ASI-35)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        roots = supervisor_audit_evidence_models.EvidenceRoots.build(
            run_dir=self.root / "run", state_dir=self.root, plan_dir=self.root
        )
        self.collector = supervisor_audit_evidence_collector._Collector(roots)

    def _paths(self, paths: dict[str, Any]) -> dict[str, Any]:
        projected = self.collector.redact(
            {"workspace_write_guard": {"external_writes": {"paths": paths}}}
        )
        return projected["workspace_write_guard"]["external_writes"]["paths"]

    def test_credential_shaped_file_names_keep_their_classification(self) -> None:
        names = ("ntfy.token", "action_token", "api_key")
        paths = {str(self.root / name): "unclassified" for name in names}
        paths[str(self.root / "state.json")] = "control-plane"

        self.assertEqual(paths, self._paths(paths))

    def test_a_real_secret_field_is_still_redacted(self) -> None:
        projected = self.collector.redact({"config": {"token": "abc"}})
        self.assertEqual(
            supervisor_audit_evidence_constants.REDACTED, projected["config"]["token"]
        )

    def test_a_secret_value_under_a_path_key_is_still_redacted(self) -> None:
        secret = "ghp_" + "A" * 36
        plain = str(self.root / "notes.txt")
        named = str(self.root / "api_key")

        projected = self._paths({plain: secret, named: "hunter2"})

        self.assertNotIn(secret, json.dumps(projected))
        self.assertEqual(supervisor_audit_evidence_constants.REDACTED, projected[named])

    def test_a_path_key_outside_the_approved_roots_is_withheld(self) -> None:
        outside = "/private/var/lib/pi-monitoring/autopilot/ntfy.token"

        projected = self._paths({outside: "unclassified"})

        self.assertNotIn(outside, json.dumps(projected))
        ((name, value),) = projected.items()
        self.assertTrue(
            name.startswith(
                f"[{supervisor_audit_evidence_constants.REASON_OUTSIDE_APPROVED_ROOTS}:"
            )
        )
        self.assertEqual("unclassified", value)


if __name__ == "__main__":
    unittest.main()
