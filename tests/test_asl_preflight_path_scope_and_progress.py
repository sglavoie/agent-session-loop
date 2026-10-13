"""Preflight path reference, verification scope, and progress journal tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_session_loop import (
    plans,
    preflight,
)
from tests.support import (
    _contract_block,
    _write_loop_ready_plan,
    write_plan,
)
from tests.support_asl_preflight import PreflightCoreFixtures


class PreflightPathScopeAndProgressTests(PreflightCoreFixtures):
    def test_path_reference_accepts_absolute_file_and_regex_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            target = root / "target.txt"
            contract = _contract_block(
                ", ".join(
                    (
                        json.dumps(
                            {
                                "type": "file_exists",
                                "path": str(target),
                                "name": "abs-file",
                            }
                        ),
                        json.dumps(
                            {
                                "type": "regex_in_file",
                                "path": str(target),
                                "pattern": "target",
                                "name": "abs-regex",
                            }
                        ),
                    )
                ),
                session="1",
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            path_findings = [
                finding
                for finding in report.findings
                if finding.category == "path-reference"
            ]
            self.assertEqual([], path_findings)

    def test_path_reference_flags_parent_traversal_as_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract = _contract_block(
                '{"type": "regex_in_file", "path": "../escape.txt", '
                '"pattern": "x", "name": "escape"}',
                session="1",
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            path_findings = [
                finding
                for finding in report.findings
                if finding.category == "path-reference"
            ]
            self.assertEqual(1, len(path_findings))
            self.assertEqual("yellow", path_findings[0].severity)
            self.assertIn("escapes", path_findings[0].message)

    def test_verification_scope_flags_unscoped_operator_health_as_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract = _contract_block(
                '{"type": "command", "argv": ["./operator", "health", "check-ts"], '
                '"timeout_seconds": 30, "name": "check-ts"}',
                session="1",
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            scope_findings = [
                finding
                for finding in report.findings
                if finding.category == "verification-scope"
            ]
            self.assertEqual(1, len(scope_findings))
            self.assertEqual("red", scope_findings[0].severity)
            self.assertIn("overly broad", scope_findings[0].message)
            self.assertFalse(report.ok)

    def test_verification_scope_allows_scoped_operator_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract = _contract_block(
                '{"type": "command", "argv": ["./operator", "health", "lint", '
                '"--core-oidc"], "timeout_seconds": 30, "name": "lint"}',
                session="1",
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": contract})
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            scope_findings = [
                finding
                for finding in report.findings
                if finding.category == "verification-scope"
            ]
            self.assertEqual([], scope_findings)

    def test_report_ok_only_when_no_red_or_yellow_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))

            ok_report = preflight.run_core_preflight(plan_dir)
            self.assertTrue(ok_report.ok)

            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "Instructions.", "Instructions. FIXME me later."
                ),
                encoding="utf-8",
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            yellow_report = preflight.run_core_preflight(plan_dir)
            self.assertFalse(yellow_report.ok)
            self.assertEqual((), yellow_report.red_findings)
            self.assertEqual(1, len(yellow_report.yellow_findings))

    def test_progress_journal_missing_yellow_when_complete_session_lacks_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("complete",))

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = [
                finding
                for finding in report.findings
                if finding.category == "progress-journal-missing"
            ]
            self.assertEqual(1, len(journal_findings))
            finding = journal_findings[0]
            self.assertEqual("yellow", finding.severity)
            self.assertIn("backfill-progress", finding.message)
            self.assertEqual(str(plans.progress_path(plan_dir)), finding.path)
            self.assertFalse(report.ok)

    def test_progress_journal_missing_yellow_when_header_only_with_completed_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("complete", "complete"))
            plans.write_progress_journal(plan_dir, ())

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = self._progress_findings(report)
            self.assertEqual(1, len(journal_findings))
            finding = journal_findings[0]
            self.assertEqual("yellow", finding.severity)
            self.assertIn("--force", finding.message)
            self.assertIn("expected complete sessions: 1, 2", finding.detail)
            self.assertIn("observed journal entries: none", finding.detail)
            self.assertFalse(report.ok)

    def test_progress_journal_missing_silent_when_no_completed_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = [
                finding
                for finding in report.findings
                if finding.category == "progress-journal-missing"
            ]
            self.assertEqual([], journal_findings)

    def test_progress_journal_missing_silent_when_header_only_and_no_completed_sessions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            plans.write_progress_journal(plan_dir, ())

            report = preflight.run_core_preflight(plan_dir)

            self.assertEqual([], self._progress_findings(report))

    def test_progress_journal_missing_silent_when_journal_present_and_readable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("complete",))
            entry = plans.ProgressEntry(
                session_id=plans.parse_session_id("1"),
                status="complete",
                completed_at="2026-06-19T15:00:00Z",
                files_modified=(),
                key_decisions=None,
                discovered_facts=(),
                open_questions=(),
                blockers=None,
            )
            plans.append_progress_entry(plan_dir, entry)

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = [
                finding
                for finding in report.findings
                if finding.category == "progress-journal-missing"
            ]
            self.assertEqual([], journal_findings)

    def test_progress_journal_missing_yellow_when_latest_completed_session_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("complete", "complete"))
            plans.write_progress_journal(plan_dir, (self._progress_entry("1"),))

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = self._progress_findings(report)
            self.assertEqual(1, len(journal_findings))
            finding = journal_findings[0]
            self.assertIn("--force", finding.message)
            self.assertIn("expected complete sessions: 1, 2", finding.detail)
            self.assertIn("observed journal entries: 1", finding.detail)

    def test_progress_journal_missing_yellow_when_entries_duplicated_or_out_of_order(
        self,
    ) -> None:
        cases = (
            ("duplicated", ("1", "1", "2")),
            ("out-of-order", ("2", "1")),
        )
        for name, observed_ids in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                plan_dir = Path(tmp) / "plan"
                _write_loop_ready_plan(plan_dir, ("complete", "complete"))
                entries = tuple(
                    self._progress_entry(session_id) for session_id in observed_ids
                )
                plans.write_progress_journal(plan_dir, entries)

                report = preflight.run_core_preflight(plan_dir)

                journal_findings = self._progress_findings(report)
                self.assertEqual(1, len(journal_findings))
                self.assertIn("--force", journal_findings[0].message)
                self.assertIn(
                    f"observed journal entries: {', '.join(observed_ids)}",
                    journal_findings[0].detail,
                )

    def test_progress_journal_missing_yellow_when_entry_status_is_not_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("complete",))
            plans.write_progress_journal(
                plan_dir,
                (self._progress_entry("1", status="pending"),),
            )

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = self._progress_findings(report)
            self.assertEqual(1, len(journal_findings))
            self.assertIn(
                "non-complete journal entry statuses: 1=pending",
                journal_findings[0].detail,
            )

    def test_progress_journal_missing_yellow_when_journal_unreadable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("complete",))
            plans.progress_path(plan_dir).write_text(
                "# Progress Journal\n"
                "<!-- agent-session-loop progress-journal v999 -->\n",
                encoding="utf-8",
            )

            report = preflight.run_core_preflight(plan_dir)

            journal_findings = [
                finding
                for finding in report.findings
                if finding.category == "progress-journal-missing"
            ]
            self.assertEqual(1, len(journal_findings))
            finding = journal_findings[0]
            self.assertEqual("yellow", finding.severity)
            self.assertIn("unreadable", finding.message)
            self.assertIn("v999", finding.message)
