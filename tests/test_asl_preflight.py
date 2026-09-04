"""Preflight core, runnability, repair hint, environment, and CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    baseline,
    baseline_cache,
    contracts,
    harness_compatibility,
    plans,
    preflight,
    repository_registration,
    state,
    workspaces,
)
from tests.support import (
    _FIX_CATEGORIES,
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _command_contract,
    _contract_block,
    _write_loop_ready_plan,
    init_git_repo,
    init_multi_repo_workspace,
    patched_env,
    write_multi_repo_plan,
    write_plan,
)


class PreflightCoreTests(unittest.TestCase):
    def _progress_findings(
        self,
        report: preflight.PreflightReport,
    ) -> list[preflight.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "progress-journal-missing"
        ]

    def _progress_entry(
        self,
        session_id: str,
        *,
        status: str = "complete",
    ) -> plans.ProgressEntry:
        return plans.ProgressEntry(
            session_id=plans.parse_session_id(session_id),
            status=status,
            completed_at="2026-06-19T15:00:00Z",
            files_modified=(),
            key_decisions=None,
            discovered_facts=(),
            open_questions=(),
            blockers=None,
        )

    def test_missing_plan_directory_emits_red_plan_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "does-not-exist"

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            self.assertIsNone(report.snapshot)
            self.assertEqual(1, len(report.findings))
            finding = report.findings[0]
            self.assertEqual("red", finding.severity)
            self.assertEqual("plan-structure", finding.category)
            self.assertIn("does not exist", finding.message)

    def test_missing_context_or_manifest_emits_red_plan_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "context.md").unlink()

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            categories = {finding.category for finding in report.findings}
            self.assertIn("plan-structure", categories)
            structure = [
                finding
                for finding in report.findings
                if finding.category == "plan-structure"
            ]
            self.assertTrue(structure)
            self.assertEqual("red", structure[0].severity)
            self.assertIn("context.md", structure[0].message)

    def test_non_canonical_session_filenames_emit_red_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (plan_dir / "session-01.md").write_text(
                "# Session 01\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            canonical_findings = [
                finding
                for finding in report.findings
                if finding.category == "canonical-session-id"
            ]
            self.assertEqual(1, len(canonical_findings))
            finding = canonical_findings[0]
            self.assertEqual("red", finding.severity)
            self.assertIn("session-01.md", finding.message)
            self.assertIn("session-1.md", finding.detail)

    def test_missing_readiness_metadata_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            readiness_findings = [
                finding
                for finding in report.findings
                if finding.category == "manifest-readiness"
            ]
            self.assertEqual(1, len(readiness_findings))
            finding = readiness_findings[0]
            self.assertEqual("red", finding.severity)
            self.assertIn("missing readiness", finding.message)
            self.assertIsNone(report.readiness)
            self.assertIsNotNone(report.expected_readiness_sha256)

    def test_readiness_sha_mismatch_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            stored = plans.parse_readiness_metadata(
                (plan_dir / "manifest.md").read_text(encoding="utf-8")
            )
            assert stored is not None
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state=stored.state,
                    readiness_sha256="00" * 32,
                    checked_at=stored.checked_at,
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            mismatches = [
                finding
                for finding in report.findings
                if finding.category == "manifest-readiness"
            ]
            self.assertTrue(mismatches)
            self.assertEqual("red", mismatches[0].severity)
            self.assertIn("SHA-256", mismatches[0].message)
            self.assertIn(report.expected_readiness_sha256, mismatches[0].detail)

    def test_draft_readiness_state_is_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",), state="draft")

            report = preflight.run_core_preflight(plan_dir)

            yellow = [
                finding
                for finding in report.findings
                if finding.category == "manifest-readiness"
            ]
            self.assertEqual(1, len(yellow))
            self.assertEqual("yellow", yellow[0].severity)
            self.assertIn("draft", yellow[0].message)
            self.assertFalse(report.ok)

    def test_missing_completion_contract_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            missing = [
                finding
                for finding in report.findings
                if finding.category == "missing-contract"
            ]
            self.assertEqual(1, len(missing))
            self.assertEqual("red", missing[0].severity)
            self.assertEqual("1", missing[0].session_id)
            self.assertIn("Completion Contract", missing[0].message)
            self.assertEqual({}, report.contracts_by_session)

    def test_malformed_completion_contract_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            broken_contract = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- something",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{not valid json",
                    "```",
                ]
            )
            write_plan(plan_dir, ("pending",), contracts_by_id={"1": broken_contract})
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            malformed = [
                finding
                for finding in report.findings
                if finding.category == "malformed-contract"
            ]
            self.assertEqual(1, len(malformed))
            self.assertEqual("red", malformed[0].severity)
            self.assertEqual("1", malformed[0].session_id)
            self.assertIn("malformed", malformed[0].message)

    def test_valid_loop_ready_plan_is_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending", "pending"))

            report = preflight.run_core_preflight(plan_dir)

            self.assertTrue(
                report.ok,
                msg=[
                    (finding.category, finding.severity, finding.message)
                    for finding in report.findings
                ],
            )
            self.assertEqual((), report.findings)
            self.assertIsNotNone(report.snapshot)
            self.assertIsNotNone(report.readiness)
            self.assertEqual({"1", "2"}, set(report.contracts_by_session))

    def test_stale_parallel_validation_readiness_snapshot_does_not_block(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            validation = plan_dir / "parallel-validation.md"
            validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Summary",
                        "",
                        "- **Safe concurrent sessions**: none",
                        "",
                        "## Plan Readiness",
                        "",
                        "- **Recorded state**: draft",
                        "- **Recorded SHA-256**: old",
                        "- **Recomputed SHA-256**: new",
                        "- **Divergence**: recorded SHA differs from recomputed SHA",
                        "",
                        "## Preflight Snapshot",
                        "",
                        "- **Findings**: 1R / 1Y / 0G",
                        "- **Reason**: preflight-red",
                        "",
                        "## Session-Run Precedence",
                        "",
                        "Dependency and ownership claims remain authoritative.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            current_sha = plans.stable_readiness_sha256(plan_dir)
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=current_sha,
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            self.assertTrue(
                report.ok,
                msg=[
                    (finding.category, finding.severity, finding.message)
                    for finding in report.findings
                ],
            )
            self.assertEqual(current_sha, report.expected_readiness_sha256)

            validation.write_text(
                validation.read_text(encoding="utf-8").replace(
                    "- **Findings**: 1R / 1Y / 0G",
                    "- **Findings**: 0R / 0Y / 2G",
                ),
                encoding="utf-8",
            )

            refreshed_report = preflight.run_core_preflight(plan_dir)
            self.assertTrue(refreshed_report.ok)
            self.assertEqual(current_sha, refreshed_report.expected_readiness_sha256)

    def test_ambiguity_sentinel_emits_yellow_above_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            text = text.replace("Instructions.", "Instructions. TODO finalize.")
            session_path.write_text(text, encoding="utf-8")
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            sentinels = [
                finding
                for finding in report.findings
                if finding.category == "ambiguity-sentinel"
            ]
            self.assertEqual(1, len(sentinels))
            self.assertEqual("yellow", sentinels[0].severity)
            self.assertEqual("1", sentinels[0].session_id)
            self.assertIn("TODO", sentinels[0].message)
            self.assertIsNotNone(sentinels[0].line)
            self.assertFalse(report.ok)

    def test_ambiguity_sentinel_ignores_word_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            session_path.write_text(
                text.replace(
                    "Instructions.",
                    "Instructions. AIDEV-TODO AIDEV_TODO prefixTODO TODO-item.",
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

            report = preflight.run_core_preflight(plan_dir)

            sentinels = [
                finding
                for finding in report.findings
                if finding.category == "ambiguity-sentinel"
            ]
            self.assertEqual([], sentinels)

    def test_ambiguity_sentinel_ignores_handoff_template_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            session_path.write_text(
                text + "\n- **Notes**: TODO follow up here\n",
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

            report = preflight.run_core_preflight(plan_dir)

            sentinels = [
                finding
                for finding in report.findings
                if finding.category == "ambiguity-sentinel"
            ]
            self.assertEqual([], sentinels)

    def test_ambiguity_sentinel_ignores_anchor_references(self) -> None:
        # Cleanup sessions legitimately reference code anchors mid-sentence or
        # inside inline code; these are not unresolved placeholders.
        reference_lines = (
            "Instructions. Anchor/FIXME cleanups plus migration.",
            "Instructions. Replace the bare `FIXME` at shared.ts:286.",
            "Instructions. Refresh the TODO in the internal reset route.",
            'Instructions. The companion "same password" TODO stays out of scope.',
            "Instructions. Stale AIDEV-TODOs and the `ssoEmail` FIXME are converted.",
        )
        for reference in reference_lines:
            with (
                self.subTest(reference=reference),
                tempfile.TemporaryDirectory() as tmp,
            ):
                plan_dir = Path(tmp) / "plan"
                _write_loop_ready_plan(plan_dir, ("pending",))
                session_path = plan_dir / "session-1.md"
                text = session_path.read_text(encoding="utf-8")
                session_path.write_text(
                    text.replace("Instructions.", reference),
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

                report = preflight.run_core_preflight(plan_dir)

                sentinels = [
                    finding
                    for finding in report.findings
                    if finding.category == "ambiguity-sentinel"
                ]
                self.assertEqual([], sentinels)

    def test_ambiguity_sentinel_still_flags_leading_markers(self) -> None:
        # A sentinel that leads a clause (after a terminator or a list/heading
        # prefix) is a genuine placeholder and must still block loop-ready.
        marker_lines = (
            "Instructions. TODO: decide the column name.",
            "Instructions.\n\n- FIXME resolve this before running.",
            "Instructions.\n\n## TBD finalize the schema.",
        )
        for marker in marker_lines:
            with (
                self.subTest(marker=marker),
                tempfile.TemporaryDirectory() as tmp,
            ):
                plan_dir = Path(tmp) / "plan"
                _write_loop_ready_plan(plan_dir, ("pending",))
                session_path = plan_dir / "session-1.md"
                text = session_path.read_text(encoding="utf-8")
                session_path.write_text(
                    text.replace("Instructions.", marker),
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

                report = preflight.run_core_preflight(plan_dir)

                sentinels = [
                    finding
                    for finding in report.findings
                    if finding.category == "ambiguity-sentinel"
                ]
                self.assertTrue(sentinels)

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

    # --- Baseline readiness gate --------------------------------------------

    def _baseline_findings(
        self, report: preflight.PreflightReport
    ) -> list[preflight.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category.startswith("baseline-")
        ]

    def _record_baseline(
        self,
        plan_dir: Path,
        repo_root: Path,
        result: dict,
        *,
        session: str = "1",
    ) -> None:
        contract = contracts.read_completion_contract(plan_dir, session)
        assert contract is not None
        check = contract.verify.checks[0]
        key = baseline.baseline_identity_key(check, repo_root)
        path = plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
        cache = baseline_cache.BaselineCache.load(path)
        cache.put(
            key,
            baseline_cache.BaselineRecord(
                key=key,
                recipe=baseline.normalize_recipe(baseline._single_check_recipe(check)),
                result=result,
            ),
        )
        cache.save()

    def _fresh_result(self, repo_root: Path, **overrides: object) -> dict:
        result = {
            "check_name": "cmd",
            "kind": "command",
            "expectation": "pass",
            "status": "pass",
            "satisfied": True,
            "duration_seconds": 0.1,
            "budget_seconds": 120.0,
            "near_timeout": False,
            "detail": "command exited 0",
            "repository_fingerprint": baseline.repository_fingerprint(repo_root),
            "recorded_at": "2026-06-19T00:00:00Z",
        }
        result.update(overrides)
        return result

    def test_baseline_missing_evidence_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-missing", findings[0].category)
            self.assertEqual("red", findings[0].severity)
            self.assertIn("asl baseline", findings[0].detail)
            self.assertFalse(report.ok)

    def test_baseline_recorded_pass_emits_no_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(plan_dir, repo_root, self._fresh_result(repo_root))

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            self.assertEqual([], self._baseline_findings(report))

    def test_baseline_unexpected_failure_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(repo_root, status="fail", satisfied=False),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-unexpected", findings[0].category)
            self.assertEqual("red", findings[0].severity)

    def test_baseline_stale_evidence_emits_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(
                    repo_root, repository_fingerprint="stale-fingerprint"
                ),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-stale", findings[0].category)
            self.assertEqual("yellow", findings[0].severity)

    def test_baseline_timeout_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(repo_root, status="timeout", satisfied=False),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-timeout", findings[0].category)
            self.assertEqual("red", findings[0].severity)

    def test_baseline_total_budget_overrun_not_accepted_as_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            # Satisfied on its own terms, but produced by a run that crossed
            # the total baseline budget: must not count as loop-ready evidence.
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(repo_root, total_budget_exceeded=True),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-timeout", findings[0].category)
            self.assertEqual("red", findings[0].severity)
            self.assertIn("total baseline budget", findings[0].message)
            self.assertFalse(report.ok)

    def test_baseline_near_timeout_emits_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )
            self._record_baseline(
                plan_dir,
                repo_root,
                self._fresh_result(
                    repo_root, near_timeout=True, duration_seconds=110.0
                ),
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            findings = self._baseline_findings(report)
            self.assertEqual(1, len(findings))
            self.assertEqual("baseline-near-timeout", findings[0].category)
            self.assertEqual("yellow", findings[0].severity)

    def test_baseline_findings_suppressed_after_a_session_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("complete", "pending"),
                contracts_by_id={
                    "1": _command_contract('["true"]', session="1"),
                    "2": _command_contract('["false"]', session="2"),
                },
            )

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            # Implementation has started; no baseline gating even with no evidence.
            self.assertEqual([], self._baseline_findings(report))

    def test_baseline_skip_expectation_is_never_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "README.md").write_text("x\n", encoding="utf-8")
            _write_loop_ready_plan(plan_dir, ("pending",))

            report = preflight.run_preflight(plan_dir, cwd=repo_root)

            self.assertEqual([], self._baseline_findings(report))

    def test_baseline_not_evaluated_without_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": _command_contract('["true"]')},
            )

            report = preflight.run_preflight(plan_dir)

            self.assertEqual([], self._baseline_findings(report))


class PreflightCommandRunnabilityTests(unittest.TestCase):
    def _contract(
        self,
        session_id: str,
        *checks: contracts.VerificationCheck,
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=plans.parse_session_id(session_id),
            done_when=("recipe runs",),
            verify=contracts.VerifyRecipe(checks=tuple(checks)),
        )

    def _command(
        self,
        name: str,
        argv: tuple[str, ...],
    ) -> contracts.CommandCheck:
        return contracts.CommandCheck(
            name=name,
            argv=argv,
            timeout_seconds=300,
        )

    def test_duplicate_command_and_cwd_emits_yellow(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                self._command("first", ("python3", "-m", "unittest")),
                self._command("second", ("python3", "-m", "unittest")),
            )
        }

        findings = preflight._check_duplicate_verification_commands(
            contracts_by_session
        )

        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("yellow", finding.severity)
        self.assertEqual("verification-duplicate", finding.category)
        self.assertIn("'first' and 'second'", finding.message)

    def test_same_argv_in_different_cwds_is_not_duplicate(self) -> None:
        command = ("python3", "-m", "unittest")
        first = contracts.CommandCheck(
            name="first", argv=command, timeout_seconds=300, cwd="one"
        )
        second = contracts.CommandCheck(
            name="second", argv=command, timeout_seconds=300, cwd="two"
        )
        contracts_by_session = {"1": self._contract("1", first, second)}

        self.assertEqual(
            (),
            preflight._check_duplicate_verification_commands(contracts_by_session),
        )

    def test_same_argv_in_different_repositories_is_not_duplicate(self) -> None:
        command = ("python3", "-m", "unittest")
        first = contracts.CommandCheck(
            name="first", argv=command, timeout_seconds=300, repo="SVC"
        )
        second = contracts.CommandCheck(
            name="second", argv=command, timeout_seconds=300, repo="OAK"
        )

        self.assertEqual(
            (),
            preflight._check_duplicate_verification_commands(
                {"1": self._contract("1", first, second)}
            ),
        )

    def test_default_and_dot_cwd_are_duplicate(self) -> None:
        command = ("python3", "-m", "unittest")
        first = contracts.CommandCheck(name="first", argv=command, timeout_seconds=300)
        second = contracts.CommandCheck(
            name="second", argv=command, timeout_seconds=300, cwd="."
        )
        contracts_by_session = {"1": self._contract("1", first, second)}

        findings = preflight._check_duplicate_verification_commands(
            contracts_by_session
        )

        self.assertEqual(1, len(findings))
        self.assertEqual("verification-duplicate", findings[0].category)

    def test_missing_bare_executable_emits_single_red(self) -> None:
        contracts_by_session = {
            "1": self._contract("1", self._command("run-1", ("pytest", "-q")))
        }
        findings = preflight._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: None,
        )
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("red", finding.severity)
        self.assertEqual("command-not-runnable", finding.category)
        self.assertEqual("1", finding.session_id)
        self.assertIn("executable is not on PATH", finding.message)
        self.assertIn("pytest", finding.detail)

    def test_missing_module_emits_single_yellow(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                self._command("run-1", ("python3", "-m", "pytest", "tests")),
            )
        }
        findings = preflight._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: f"/usr/bin/{name}",
            module_probe=lambda interpreter, module, *, cwd: False,
        )
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("yellow", finding.severity)
        self.assertEqual("command-not-runnable", finding.category)
        self.assertIn("module is not importable", finding.message)
        self.assertIn("pytest", finding.detail)

    def test_runnable_command_emits_no_finding(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                self._command("run-1", ("python3", "-m", "unittest", "tests")),
            )
        }
        findings = preflight._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: f"/usr/bin/{name}",
            module_probe=lambda interpreter, module, *, cwd: True,
        )
        self.assertEqual((), findings)

    def test_cwd_none_skips_check(self) -> None:
        contracts_by_session = {
            "1": self._contract("1", self._command("run-1", ("pytest", "-q")))
        }
        findings = preflight._check_command_runnability(
            contracts_by_session,
            None,
            which=lambda name: None,
        )
        self.assertEqual((), findings)

    def test_non_command_checks_are_ignored(self) -> None:
        contracts_by_session = {
            "1": self._contract(
                "1",
                contracts.FileExistsCheck(name="f-1", path="README.md"),
                contracts.RegexInFileCheck(name="r-1", path="README.md", pattern="x"),
            )
        }
        findings = preflight._check_command_runnability(
            contracts_by_session,
            "/repo",
            which=lambda name: None,
        )
        self.assertEqual((), findings)

    def test_run_preflight_folds_command_finding_with_real_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            repo_root = Path(tmp) / "repo"
            repo_root.mkdir()
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _contract_block(
                        '{"type": "command", "argv": '
                        '["definitely-not-a-real-binary-xyz", "--version"], '
                        '"timeout_seconds": 30, "name": "run-1"}',
                        session="1",
                    ),
                },
            )
            report = preflight.run_preflight(plan_dir, cwd=repo_root)
            command_findings = [
                finding
                for finding in report.findings
                if finding.category == "command-not-runnable"
            ]
            self.assertEqual(1, len(command_findings))
            self.assertEqual("red", command_findings[0].severity)
            self.assertFalse(report.ok)

    def test_run_preflight_cwd_none_emits_no_command_finding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={
                    "1": _contract_block(
                        '{"type": "command", "argv": '
                        '["definitely-not-a-real-binary-xyz", "--version"], '
                        '"timeout_seconds": 30, "name": "run-1"}',
                        session="1",
                    ),
                },
            )
            report = preflight.run_preflight(plan_dir, cwd=None)
            command_findings = [
                finding
                for finding in report.findings
                if finding.category == "command-not-runnable"
            ]
            self.assertEqual([], command_findings)

    def test_executable_finding_resolves_to_override_hint(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="command-not-runnable",
            message="session 1 check 'run-1' executable is not on PATH",
            session_id="1",
            detail="executable not found on PATH: pytest",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("command-not-runnable.executable", hint.check_id)
        self.assertEqual("red", hint.severity_hint)

    def test_module_finding_resolves_to_override_hint(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="command-not-runnable",
            message="session 1 check 'run-1' module is not importable",
            session_id="1",
            detail="module not importable by python3: pytest",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("command-not-runnable.module", hint.check_id)
        self.assertEqual("yellow", hint.severity_hint)


class RepositoryFootprintPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "README.md").write_text("x\n", encoding="utf-8")
        self.plan_dir = self.root / "plan"

    def _write_plan(self, *paths: Path) -> None:
        contract = _contract_block(
            ", ".join(
                json.dumps(
                    {"type": "file_exists", "path": str(path), "name": f"check-{index}"}
                )
                for index, path in enumerate(paths)
            ),
            session="1",
        )
        _write_loop_ready_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )

    def _footprint_findings(
        self, report: preflight.PreflightReport
    ) -> list[preflight.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "repository-footprint"
        ]

    def test_out_of_root_check_is_red_using_the_stored_plan_metadata(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._footprint_findings(report)
        self.assertEqual(["red", "red"], [finding.severity for finding in findings])
        self.assertIn("escapes the bound root", findings[0].message)
        self.assertIn("2 git repositories", findings[1].message)
        self.assertEqual(
            ["repository-footprint.out-of-root", "repository-footprint.multi-repo"],
            [preflight.repair_hint_for_finding(f).check_id for f in findings],
        )

    def test_absolute_check_inside_the_bound_root_stays_green(self) -> None:
        self._write_plan(self.first / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._footprint_findings(report))
        self.assertFalse(report.repository_footprint.registry_required)

    def test_non_git_target_outside_the_bound_root_stays_green(self) -> None:
        loose = self.root / "loose.txt"
        loose.write_text("x\n", encoding="utf-8")
        self._write_plan(self.first / "README.md", loose)
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._footprint_findings(report))
        self.assertFalse(report.repository_footprint.registry_required)

    def test_without_plan_metadata_the_explicit_cwd_supplies_the_root(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")

        core_report = preflight.run_core_preflight(self.plan_dir)
        self.assertEqual([], self._footprint_findings(core_report))

        report = preflight.run_preflight(self.plan_dir, cwd=self.first)

        self.assertTrue(self._footprint_findings(report))

    def test_footprint_blocks_marking_the_plan_loop_ready(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        blockers = preflight._mark_loop_ready_blockers(report)
        self.assertIn(
            "repository-footprint", {blocker.category for blocker in blockers}
        )

    def test_payload_carries_a_registry_table_derived_from_disk(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)

        payload = preflight._preflight_payload_fields(
            report,
            plan_dir_arg=str(self.plan_dir),
            cwd=None,
            harness=None,
            commit_mode=None,
            state_dir=None,
            max_pending_sessions=None,
            mark_loop_ready=False,
        )["repository_footprint"]

        self.assertEqual(str(self.workspace.resolve()), payload["workspace_root"])
        self.assertFalse(payload["registry_present"])
        self.assertTrue(payload["registry_required"])
        self.assertEqual(
            ["| Alias | Path | Origin |", "| --- | --- | --- |"],
            payload["suggested_registry_table"][:2],
        )
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(entry["alias"] for entry in payload["detected"]),
        )
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(payload["suggested_scopes"]["1"]["writes"]),
        )

    def test_registered_plans_are_governed_by_the_registry_instead(self) -> None:
        write_multi_repo_plan(
            self.plan_dir, ("pending",), specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._footprint_findings(report))
        self.assertIsNone(report.repository_footprint)

    def test_footprint_blockers_are_cleared_by_the_registry_steps(self) -> None:
        self._write_plan(self.first / "README.md", self.second / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)
        self.assertEqual(2, len(self._footprint_findings(report)))

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(
            ["register-repositories", "prepare"],
            [step["argv"][1] for step in steps],
        )
        self.assertIsNone(
            preflight.build_suggested_pause(
                report, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )
        )


class RepositoryIntentPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "README.md").write_text("x\n", encoding="utf-8")
        self.plan_dir = self.root / "plan"

    def _write_plan(
        self, *paths: Path, statuses: tuple[str, ...] = ("pending",)
    ) -> None:
        contract = _contract_block(
            ", ".join(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": str(path),
                        "name": f"check-{index}",
                    }
                )
                for index, path in enumerate(paths)
            ),
            session="1",
        )
        _write_loop_ready_plan(self.plan_dir, statuses, contracts_by_id={"1": contract})

    def _set_handoff(
        self, session_id: str, *, status: str, files_modified: str
    ) -> None:
        path = self.plan_dir / f"session-{session_id}.md"
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("- **Status**: "):
                line = f"- **Status**: {status}"
            elif line.startswith("- **Files modified**: "):
                line = f"- **Files modified**: {files_modified}"
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _intent_findings(
        self, report: preflight.PreflightReport
    ) -> list[preflight.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "repository-registry-required"
        ]

    def test_target_a_session_has_yet_to_create_still_names_its_repository(
        self,
    ) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertIn("2 git repositories", findings[0].message)
        self.assertIn("`## Repositories`", findings[0].message)
        self.assertEqual(
            "repository-registry-required.declare",
            preflight.repair_hint_for_finding(findings[0]).check_id,
        )
        # The footprint check cannot see a path that does not exist yet, which
        # is exactly the gap the intent check closes.
        self.assertFalse(report.repository_footprint.registry_required)

    def test_repair_guidance_names_the_migration_command(self) -> None:
        hint = preflight.REPAIR_HINTS["repository-registry-required"]
        self.assertIn(
            "asl register-repositories <plan-dir> --cwd <repo-root>",
            hint.message,
        )
        self.assertIn("`## Repositories` table", hint.message)
        self.assertIn("Reads repositories**:", hint.message)
        self.assertIn("Writes repositories**:", hint.message)
        self.assertIn("`repo` alias", hint.message)
        self.assertIn("asl register-repositories", hint.question_template)

    def test_every_repository_hint_names_the_migration_command(self) -> None:
        hints = (
            preflight.REPAIR_HINTS["repository-footprint"],
            preflight.REPAIR_HINTS["repository-registry-required"],
            preflight._CATEGORY_HINT_OVERRIDES["repository-footprint.multi-repo"],
            preflight._CATEGORY_HINT_OVERRIDES["repository-footprint.out-of-root"],
        )
        for hint in hints:
            with self.subTest(check_id=hint.check_id):
                self.assertIn("asl register-repositories", hint.message)
                self.assertIn("asl register-repositories", hint.question_template)
                # The hand-edit recipe is gone: no numbered step list survives.
                self.assertNotIn("(1) add", hint.message)

    def test_single_repository_legacy_plan_is_unchanged(self) -> None:
        self._write_plan(self.first / "README.md", self.first / "src" / "new.py")
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertFalse(report.repository_intent.registry_required)

    def test_git_target_outside_non_git_bound_root_requires_registry(self) -> None:
        non_git_root = self.root / "operator-workspace"
        non_git_root.mkdir()
        self._write_plan(self.first / "README.md")
        plans.save_plan_metadata(self.plan_dir, str(non_git_root))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertTrue(report.repository_intent.registry_required)
        self.assertIsNone(report.repository_intent.bound_git_root)
        self.assertEqual(
            (workspaces.canonicalize_filesystem_path(self.first),),
            report.repository_intent.git_roots,
        )

    def test_source_plan_in_another_repository_is_not_a_second_target(
        self,
    ) -> None:
        self._write_plan(self.first / "README.md", self.first / "src" / "new.py")
        manifest = self.plan_dir / "manifest.md"
        source = self.second / "plans" / "source.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"\n**Split from**: `{source}`\n",
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertFalse(report.repository_intent.registry_required)

    def _declare_source(
        self, value: str, *, cited_in_session: str | None = None
    ) -> None:
        """Record a `**Split from**` pointer, optionally citing it in prose."""
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"\n**Split from**: `{value}`\n",
            encoding="utf-8",
        )
        if cited_in_session is not None:
            session = self.plan_dir / "session-1.md"
            session.write_text(
                session.read_text(encoding="utf-8")
                + f"\nRestated from {cited_in_session} for context.\n",
                encoding="utf-8",
            )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))

    def _footprint_findings(
        self, report: preflight.PreflightReport
    ) -> list[preflight.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "repository-footprint"
        ]

    def test_source_plan_cited_in_session_prose_is_not_a_second_target(
        self,
    ) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(self.first / "README.md", self.first / "src" / "new.py")
        self._declare_source(str(source), cited_in_session=f"{source}:209")

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertEqual([], self._footprint_findings(report))
        self.assertFalse(report.repository_intent.registry_required)

    def test_sibling_of_the_source_plan_is_not_a_second_target(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        sibling = source.parent / "companion.md"
        sibling.write_text("# Companion\n", encoding="utf-8")
        self._write_plan(self.first / "README.md")
        self._declare_source(str(source), cited_in_session=str(sibling))

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertEqual([], self._footprint_findings(report))

    def test_a_write_elsewhere_in_the_source_repository_still_needs_a_registry(
        self,
    ) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        self._declare_source(str(source), cited_in_session=str(source))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertIn("2 git repositories", findings[0].message)

    def test_a_relative_split_from_leaves_the_gate_intact(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(self.first / "README.md")
        self._declare_source("plans/source.md", cited_in_session=str(source))

        report = preflight.run_core_preflight(self.plan_dir)

        findings = self._intent_findings(report)
        self.assertEqual(1, len(findings))
        self.assertIn("2 git repositories", findings[0].message)

    def test_footprint_gate_does_not_count_the_cited_source_plan(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        self._write_plan(self.first / "README.md")
        self._declare_source(str(source), cited_in_session=str(source))

        footprint, payload = preflight.plan_repository_footprint(
            self.plan_dir, self.first
        )

        self.assertEqual((), footprint.out_of_root)
        self.assertEqual(
            (workspaces.canonicalize_filesystem_path(self.first),),
            footprint.repo_roots,
        )
        # ``None`` is the payload's way of saying there is nothing to report.
        self.assertIsNone(payload)

    def test_registered_multi_repository_plan_passes(self) -> None:
        write_multi_repo_plan(
            self.plan_dir, ("pending",), specs=DEFAULT_MULTI_REPO_SPECS[:2]
        )

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertEqual([], self._intent_findings(report))
        self.assertIsNone(report.repository_intent)

    def test_explicit_cwd_supplies_the_root_without_plan_metadata(self) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )

        core_report = preflight.run_core_preflight(self.plan_dir)
        self.assertEqual([], self._intent_findings(core_report))

        report = preflight.run_preflight(self.plan_dir, cwd=self.first)

        self.assertTrue(self._intent_findings(report))

    def test_payload_reports_the_repositories_the_plan_intends_to_reach(
        self,
    ) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)

        payload = preflight._preflight_payload_fields(
            report,
            plan_dir_arg=str(self.plan_dir),
            cwd=None,
            harness=None,
            commit_mode=None,
            state_dir=None,
            max_pending_sessions=None,
            mark_loop_ready=False,
        )["repository_intent"]

        self.assertTrue(payload["registry_required"])
        self.assertEqual("repository-registry-required", payload["reason"])
        self.assertEqual(2, payload["git_root_count"])
        self.assertEqual(
            sorted(
                str(workspaces.canonicalize_filesystem_path(path))
                for path in (self.first, self.second)
            ),
            sorted(payload["git_roots"]),
        )

    def test_mark_loop_ready_cannot_silence_the_invariant(self) -> None:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)

        blockers = preflight._mark_loop_ready_blockers(report)
        self.assertIn(
            "repository-registry-required",
            {blocker.category for blocker in blockers},
        )

        marked, error = preflight._mark_loop_ready(report)

        self.assertFalse(marked)
        self.assertIn("non-bypassable", error)
        self.assertIn("repository-registry-required", error)

    def _registry_required_report(self) -> preflight.PreflightReport:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        return preflight.run_core_preflight(self.plan_dir)

    def test_auto_repair_migrates_the_registry_and_clears_the_pause(self) -> None:
        report = self._registry_required_report()

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        workspace = workspaces.canonicalize_filesystem_path(self.workspace)
        self.assertEqual(
            [
                [
                    "asl",
                    "register-repositories",
                    str(self.plan_dir),
                    "--cwd",
                    str(self.first),
                    "--json",
                ],
                [
                    "asl",
                    "prepare",
                    str(self.plan_dir),
                    "--resume",
                    "--rebind-workspace",
                    "--cwd",
                    str(workspace),
                ],
            ],
            [step["argv"] for step in steps],
        )
        for step in steps:
            self.assertEqual(1, step["max_attempts"])
            self.assertEqual("repository-registry-required", step["category"])
        self.assertIsNone(
            preflight.build_suggested_pause(
                report, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )
        )

    def test_registry_steps_forward_a_state_directory_override(self) -> None:
        report = self._registry_required_report()

        steps = preflight.build_auto_repair_plan(
            report,
            plan_dir=str(self.plan_dir),
            cwd=str(self.first),
            state_dir="/state/dir",
        )

        for step in steps:
            self.assertIn("--state-dir", step["argv"])
            self.assertEqual(
                "/state/dir",
                step["argv"][step["argv"].index("--state-dir") + 1],
            )

    def test_completed_handoff_blocks_automatic_registry_steps(self) -> None:
        """Preflight never advertises a migration the command would refuse."""
        self._write_plan(
            self.first / "README.md",
            self.second / "src" / "new_module.py",
            statuses=("complete", "pending"),
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        self._set_handoff("1", status="complete", files_modified="src/new.py")
        report = preflight.run_core_preflight(self.plan_dir)

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(
            [],
            [step for step in steps if "register-repositories" in step["argv"]],
        )
        pause = preflight.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])

        # The rule is the entry, not the status: a completed handoff that
        # modified nothing survives the registry, so the steps come back.
        self._set_handoff("1", status="complete", files_modified="(none)")
        report = preflight.run_core_preflight(self.plan_dir)

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(
            [
                "asl",
                "register-repositories",
                str(self.plan_dir),
                "--cwd",
                str(self.first),
                "--json",
            ],
            steps[0]["argv"],
        )
        self.assertIsNone(
            preflight.build_suggested_pause(
                report, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )
        )

    def test_missing_origin_keeps_registry_blocker_and_suggested_pause(
        self,
    ) -> None:
        """Topology alone cannot see this refusal; the derivation can."""
        subprocess.run(
            ["git", "remote", "remove", "origin"],
            cwd=self.second,
            check=True,
            capture_output=True,
        )
        report = self._registry_required_report()
        self.assertTrue(self._intent_findings(report))
        self.assertEqual(
            "repository-origin",
            repository_registration.derive_registration(
                self.plan_dir, cwd=self.first
            ).missing_fact,
        )

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual([], list(steps))
        pause = preflight.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])
        self.assertIn("repository-origin", pause["detail"])

    def test_unattributable_contract_keeps_registry_blocker_and_suggested_pause(
        self,
    ) -> None:
        """A check under no repository refuses, so neither step is offered."""
        self._write_plan(
            self.first / "README.md",
            self.second / "src" / "new_module.py",
            self.root / "outside" / "artifact.txt",
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        report = preflight.run_core_preflight(self.plan_dir)
        self.assertTrue(self._intent_findings(report))
        self.assertEqual(
            "contract-path-outside-registry",
            repository_registration.derive_registration(
                self.plan_dir, cwd=self.first
            ).missing_fact,
        )

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual([], list(steps))
        pause = preflight.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])
        self.assertIn("contract-path-outside-registry", pause["detail"])

    def test_without_a_cwd_no_registry_step_is_emitted(self) -> None:
        report = self._registry_required_report()

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=str(self.plan_dir), cwd=None
        )

        self.assertEqual([], list(steps))
        pause = preflight.build_suggested_pause(
            report, plan_dir=str(self.plan_dir), cwd=None
        )
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("repository-registry-required", pause["detail"])

    def test_an_active_lock_still_wins_over_the_registry_steps(self) -> None:
        report = self._registry_required_report()
        locked = dataclasses.replace(
            report,
            findings=report.findings
            + (_finding("red", "env-lock", "active lock held by pid 9 at /p/lock"),),
        )

        plan = preflight.build_auto_repair_plan(
            locked, plan_dir=str(self.plan_dir), cwd=str(self.first)
        )

        self.assertEqual(1, len(plan))
        self.assertTrue(plan[0]["blocked"])
        self.assertEqual("lock-contention", plan[0]["pause_reason"])
        self.assertEqual(
            "lock-contention",
            preflight.build_suggested_pause(
                locked, plan_dir=str(self.plan_dir), cwd=str(self.first)
            )["reason"],
        )

    def test_the_rebind_step_matches_the_command_the_migration_prints(
        self,
    ) -> None:
        # The two surfaces emit the same follow-up command, so neither can
        # drift into telling an operator to run something the other does not.
        report = self._registry_required_report()
        plan_dir = str(workspaces.canonicalize_filesystem_path(self.plan_dir))

        steps = preflight.build_auto_repair_plan(
            report, plan_dir=plan_dir, cwd=str(self.first)
        )
        applied = repository_registration.register_repositories(
            self.plan_dir, cwd=self.first
        )

        self.assertEqual("applied", applied.status)
        self.assertEqual(applied.next_commands[0], steps[1]["command"])

    def test_category_is_not_bypassable(self) -> None:
        self.assertIn(
            "repository-registry-required",
            preflight.NON_BYPASSABLE_PREFLIGHT_CATEGORIES,
        )


class PreflightRepairHintTests(unittest.TestCase):
    def test_every_preflight_category_has_a_default_hint(self) -> None:
        for category in preflight.PREFLIGHT_CATEGORIES:
            with self.subTest(category=category):
                hint = preflight.REPAIR_HINTS[category]
                self.assertEqual(category, hint.category)
                self.assertTrue(hint.check_id)
                self.assertTrue(hint.message)
                self.assertTrue(hint.question_template)
                self.assertIn(hint.fix_category, _FIX_CATEGORIES)

    def test_all_repair_hint_check_ids_are_unique(self) -> None:
        check_ids: list[str] = []
        for hint in preflight.REPAIR_HINTS.values():
            check_ids.append(hint.check_id)
        for hint in preflight._CATEGORY_HINT_OVERRIDES.values():
            check_ids.append(hint.check_id)
        for hint in preflight.RUNTIME_REPAIR_HINTS.values():
            check_ids.append(hint.check_id)
        self.assertEqual(
            len(check_ids),
            len(set(check_ids)),
            msg=f"duplicate check_ids in repair-hint tables: {check_ids}",
        )

    def test_missing_contract_finding_resolves_to_add_hint(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="missing-contract",
            message="session 1 is missing a ## Completion Contract section",
            session_id="1",
            path="session-1.md",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("missing-contract.add", hint.check_id)
        self.assertEqual("missing-contract", hint.category)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_manifest_readiness_missing_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="manifest-readiness",
            message="manifest is missing readiness metadata",
            path="manifest.md",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.missing", hint.check_id)
        self.assertEqual("manifest-update", hint.fix_category)

    def test_manifest_readiness_sha_mismatch_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="manifest-readiness",
            message="manifest readiness SHA-256 does not match current plan content",
            path="manifest.md",
            detail="recorded 00; expected aa",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.sha-mismatch", hint.check_id)

    def test_manifest_readiness_state_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="manifest-readiness",
            message="plan readiness state is 'draft'",
            path="manifest.md",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.state-not-loop-ready", hint.check_id)
        self.assertEqual("yellow", hint.severity_hint)

    def test_env_git_outside_worktree_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="env-git",
            message="cwd is not inside a git repository: /tmp/x",
            path="/tmp/x",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-git.outside-worktree", hint.check_id)
        self.assertEqual("invocation-fix", hint.fix_category)

    def test_env_git_dirty_worktree_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="env-git",
            message="git worktree is dirty; commit-mode runs require a clean tree",
            path="/tmp/repo",
            detail="commit-mode: per-session",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-git.dirty-worktree", hint.check_id)
        self.assertEqual("env-state", hint.fix_category)

    def test_env_lock_stale_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="env-lock",
            message="stale lock present at /tmp/lock",
            path="/tmp/lock",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-lock.stale", hint.check_id)

    def test_env_lock_active_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="env-lock",
            message="active lock held by pid 1234 at /tmp/lock",
            path="/tmp/lock",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-lock.active", hint.check_id)

    def test_env_amx_green_finding_resolves_to_optional(self) -> None:
        finding = preflight.PreflightFinding(
            severity="green",
            category="env-amx",
            message="amx hook indicator not on PATH",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-amx.optional", hint.check_id)
        self.assertEqual("informational", hint.fix_category)

    def test_env_turn_budget_yellow_resolves_to_split(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="env-turn-budget",
            message="30 pending sessions exceeds yellow threshold 25",
        )
        hint = preflight.repair_hint_for_finding(finding)
        # The id still says `split` because it feeds an issue fingerprint and
        # renaming it would split the issue's history in the inbox.
        self.assertEqual("env-turn-budget.split", hint.check_id)
        self.assertEqual("env-state", hint.fix_category)

    def test_the_turn_budget_hint_names_no_remedy_that_raises_the_count(self) -> None:
        # A resplit replaces one pending parent with several pending children,
        # so the remedy the hint used to lead with raised the very count the
        # finding is about.
        hint = preflight._CATEGORY_HINT_OVERRIDES["env-turn-budget.split"]

        for text in (hint.message, hint.question_template):
            self.assertNotIn("resplit", text.lower())
        self.assertIn("--max-sessions", hint.message)
        self.assertIn("--max-pending-sessions", hint.message)

    def test_env_turn_budget_green_resolves_to_healthy(self) -> None:
        finding = preflight.PreflightFinding(
            severity="green",
            category="env-turn-budget",
            message="5 session(s) pending; rough turn budget ~15 turns",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-turn-budget.healthy", hint.check_id)
        self.assertEqual("informational", hint.fix_category)

    def test_baseline_categories_resolve_to_hints(self) -> None:
        expected = {
            "baseline-missing": ("baseline-missing.refresh", "manifest-update"),
            "baseline-stale": ("baseline-stale.refresh", "manifest-update"),
            "baseline-unexpected": (
                "baseline-unexpected.fix",
                "session-recipe-edit",
            ),
            "baseline-timeout": ("baseline-timeout.fix", "session-recipe-edit"),
            "baseline-near-timeout": (
                "baseline-near-timeout.split",
                "session-recipe-edit",
            ),
        }
        for category, (check_id, fix_category) in expected.items():
            with self.subTest(category=category):
                finding = preflight.PreflightFinding(
                    severity="red",
                    category=category,
                    message="baseline finding",
                    session_id="1",
                    detail="run `asl baseline`",
                )
                hint = preflight.repair_hint_for_finding(finding)
                self.assertEqual(check_id, hint.check_id)
                self.assertEqual(category, hint.category)
                self.assertEqual(fix_category, hint.fix_category)

    def test_report_repair_hints_align_with_findings_one_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",), state="draft")

            report = preflight.run_core_preflight(plan_dir)

            self.assertEqual(len(report.findings), len(report.repair_hints))
            for finding, hint in zip(
                report.findings, report.repair_hints, strict=False
            ):
                self.assertEqual(finding.category, hint.category)

    def test_runtime_no_contract_resolves(self) -> None:
        hint = preflight.runtime_repair_hint("no-contract")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.no-contract", hint.check_id)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_runtime_malformed_contract_resolves(self) -> None:
        hint = preflight.runtime_repair_hint("malformed-contract")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.malformed-contract", hint.check_id)

    def test_runtime_verify_failed_without_failure_reason_returns_none(
        self,
    ) -> None:
        self.assertIsNone(preflight.runtime_repair_hint("verify-failed"))

    def test_runtime_verify_failed_command_not_found_resolves(self) -> None:
        hint = preflight.runtime_repair_hint(
            "verify-failed", failure_reason="command-not-found"
        )
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.verify-failed.command-not-found", hint.check_id)
        self.assertEqual("env-install", hint.fix_category)

    def test_runtime_verify_failed_every_failure_reason_resolves(self) -> None:
        # Every failure_reason emitted by verification.py must resolve so the
        # guided skill can surface a fix for any per-check failure.
        for failure_reason in (
            "command-failed",
            "command-timeout",
            "command-not-found",
            "command-error",
            "command-overbroad",
            "file-missing",
            "file-error",
            "regex-not-matched",
        ):
            with self.subTest(failure_reason=failure_reason):
                hint = preflight.runtime_repair_hint(
                    "verify-failed", failure_reason=failure_reason
                )
                self.assertIsNotNone(hint)
                assert hint is not None
                self.assertEqual(
                    f"runtime.verify-failed.{failure_reason}", hint.check_id
                )
                self.assertEqual("verify-failed", hint.category)

    def test_unknown_runtime_reason_returns_none(self) -> None:
        self.assertIsNone(preflight.runtime_repair_hint("nonexistent"))


def _finding(
    severity: str,
    category: str,
    message: str,
    *,
    detail: str = "",
    path: str | None = None,
) -> preflight.PreflightFinding:
    return preflight.PreflightFinding(
        severity=severity,
        category=category,
        message=message,
        detail=detail,
        path=path,
    )


def _report(*findings: preflight.PreflightFinding) -> preflight.PreflightReport:
    return preflight.PreflightReport(
        plan_dir=Path("/plan"),
        findings=tuple(findings),
    )


def _backtick_commands(text: str) -> list[str]:
    """Return every substring wrapped in single backticks in ``text``."""
    return re.findall(r"`([^`]+)`", text)


def _extract_backtick_command(text: str, prefix: str) -> str:
    """Return the first backtick command in ``text`` starting with ``prefix``."""
    for command in _backtick_commands(text):
        if command.startswith(prefix):
            return command
    raise AssertionError(f"no `{prefix}...` command found in: {text!r}")


class RepairHintSubstitutionTests(unittest.TestCase):
    def test_plan_dir_and_repo_root_substituted_into_command(self) -> None:
        hint = preflight.REPAIR_HINTS["env-lock"]
        payload = preflight.repair_hint_payload(
            hint, plan_dir="/plans/demo", cwd="/repo"
        )
        self.assertIn("asl unlock /plans/demo --json", payload["message"])
        self.assertNotIn("<plan-dir>", payload["message"])
        self.assertNotIn("<plan-dir>", payload["question_template"])

    def test_repo_root_left_as_placeholder_when_cwd_none(self) -> None:
        hint = preflight.REPAIR_HINTS["baseline-missing"]
        payload = preflight.repair_hint_payload(hint, plan_dir="/plans/demo", cwd=None)
        self.assertIn("asl baseline /plans/demo --cwd <repo-root>", payload["message"])

    def test_every_hint_command_carries_plan_dir_placeholder(self) -> None:
        # Audit: any hint whose message embeds an `asl <plandir-cmd>` command
        # must use the `<plan-dir>` placeholder (never a bare `<plan>` or an
        # unsubstituted literal) so the payload can inject the real directory.
        plan_dir_commands = ("asl unlock", "asl backfill-progress", "asl baseline")
        tables = (
            preflight.REPAIR_HINTS.values(),
            preflight._CATEGORY_HINT_OVERRIDES.values(),
        )
        for table in tables:
            for hint in table:
                with self.subTest(check_id=hint.check_id):
                    self.assertNotIn("<plan>", hint.message)
                    for command in plan_dir_commands:
                        if command in hint.message:
                            self.assertIn("<plan-dir>", hint.message)
                    if (
                        "asl preflight" in hint.message
                        and "--mark-loop-ready" in hint.message
                    ):
                        self.assertIn(
                            "asl preflight <plan-dir> --json --mark-loop-ready",
                            hint.message,
                        )

    def test_baseline_command_survives_whitespace_and_metacharacters(self) -> None:
        # The baseline remediation command embeds both the plan directory and
        # the repo root; a path with whitespace or shell metacharacters must
        # round-trip as one argument each.
        weird_plan = "/plans/de mo; rm -rf $(evil)"
        weird_repo = "/re po & 'quote'"
        payload = preflight.repair_hint_payload(
            preflight.REPAIR_HINTS["baseline-missing"],
            plan_dir=weird_plan,
            cwd=weird_repo,
        )
        command = _extract_backtick_command(payload["message"], "asl baseline")
        self.assertEqual(
            ["asl", "baseline", weird_plan, "--cwd", weird_repo],
            shlex.split(command),
        )

    def test_every_substituted_hint_embeds_only_quoted_paths(self) -> None:
        # Audit: after substitution, each weird path appears in the payload
        # only in its shell-quoted form (never raw), so every embedded command
        # round-trips through shlex.split without splitting a path.
        weird_plan = "/plans/de mo; rm -rf $(evil)"
        weird_repo = "/re po & 'quote'"
        tables = (
            preflight.REPAIR_HINTS.values(),
            preflight._CATEGORY_HINT_OVERRIDES.values(),
        )
        for table in tables:
            for hint in table:
                payload = preflight.repair_hint_payload(
                    hint, plan_dir=weird_plan, cwd=weird_repo
                )
                for field in ("message", "question_template"):
                    text = payload[field]
                    with self.subTest(check_id=hint.check_id, field=field):
                        # Strip the quoted forms; the raw path must not survive.
                        residue = text.replace(shlex.quote(weird_plan), "").replace(
                            shlex.quote(weird_repo), ""
                        )
                        self.assertNotIn(weird_plan, residue)
                        self.assertNotIn(weird_repo, residue)
                    for command in _backtick_commands(text):
                        if command.startswith("asl "):
                            # Well-formed shell: parses without raising.
                            shlex.split(command)


class AutoRepairPlanTests(unittest.TestCase):
    def test_ok_report_has_empty_plan_and_no_pause(self) -> None:
        report = _report(_finding("green", "env-amx", "optional"))
        self.assertEqual(
            (),
            preflight.build_auto_repair_plan(report, plan_dir="/plan", cwd=None),
        )
        self.assertIsNone(preflight.build_suggested_pause(report, plan_dir="/plan"))

    def test_stale_lock_then_progress_then_readiness_order(self) -> None:
        report = _report(
            _finding(
                "red", "manifest-readiness", "manifest is missing readiness metadata"
            ),
            _finding(
                "yellow", "progress-journal-missing", "progress journal not initialized"
            ),
            _finding("yellow", "env-lock", "stale lock present at /p/lock"),
        )
        plan = preflight.build_auto_repair_plan(report, plan_dir="/plans/x", cwd=None)
        commands = [step["command"] for step in plan]
        self.assertEqual(
            [
                "asl unlock /plans/x --json",
                "asl backfill-progress --force /plans/x",
                "asl preflight /plans/x --json --mark-loop-ready",
            ],
            commands,
        )
        for step in plan:
            self.assertEqual(1, step["max_attempts"])
        # readiness-only residue is fully repairable -> no fallback pause.
        self.assertIsNone(preflight.build_suggested_pause(report, plan_dir="/plans/x"))

    def test_active_lock_blocks_and_never_unlocks(self) -> None:
        report = _report(
            _finding("red", "env-lock", "active lock held by pid 9 at /p/lock"),
            _finding("yellow", "progress-journal-missing", "journal missing"),
        )
        plan = preflight.build_auto_repair_plan(report, plan_dir="/plans/x", cwd=None)
        self.assertEqual(1, len(plan))
        step = plan[0]
        self.assertTrue(step["blocked"])
        self.assertEqual("lock-contention", step["pause_reason"])
        # The active-lock plan never emits an unlock command or `--force`.
        self.assertNotIn("command", step)
        for entry in plan:
            self.assertNotIn("--force", json.dumps(entry))
        pause = preflight.build_suggested_pause(report, plan_dir="/plans/x")
        self.assertEqual("lock-contention", pause["reason"])

    def test_hard_blocker_suppresses_readiness_step_and_suggests_unsafe(
        self,
    ) -> None:
        report = _report(
            _finding(
                "red", "manifest-readiness", "manifest is missing readiness metadata"
            ),
            _finding(
                "red",
                "missing-contract",
                "session 1 is missing a ## Completion Contract section",
            ),
        )
        plan = preflight.build_auto_repair_plan(report, plan_dir="/plans/x", cwd=None)
        # A non-readiness blocker (missing-contract) means mark-loop-ready would
        # be refused, so no readiness repair step is offered.
        self.assertEqual([], [s for s in plan if "command" in s])
        pause = preflight.build_suggested_pause(report, plan_dir="/plans/x")
        self.assertEqual("unsafe", pause["reason"])
        self.assertIn("missing-contract", pause["detail"])
        self.assertIn("automatic plan repair failed", pause["detail"])

    def test_progress_only_repairable_without_pause(self) -> None:
        report = _report(
            _finding("yellow", "progress-journal-missing", "journal missing"),
        )
        plan = preflight.build_auto_repair_plan(report, plan_dir="/plans/x", cwd=None)
        self.assertEqual(
            ["asl backfill-progress --force /plans/x"],
            [s["command"] for s in plan],
        )
        self.assertIsNone(preflight.build_suggested_pause(report, plan_dir="/plans/x"))

    def test_repair_commands_survive_whitespace_and_metacharacters(self) -> None:
        # A plan path carrying whitespace and shell metacharacters must stay a
        # single argument in every auto-repair step: stale-lock, progress-
        # backfill, and readiness. Each step exposes an authoritative `argv`
        # array, and its `command` is that argv rendered shell-safe so it
        # round-trips through shlex.split.
        weird = "/plans/de mo; rm -rf $(evil) 'q'"
        report = _report(
            _finding(
                "red",
                "manifest-readiness",
                "manifest is missing readiness metadata",
            ),
            _finding(
                "yellow",
                "progress-journal-missing",
                "progress journal not initialized",
            ),
            _finding("yellow", "env-lock", "stale lock present at /p/lock"),
        )
        plan = preflight.build_auto_repair_plan(report, plan_dir=weird, cwd=None)
        self.assertEqual(
            [
                ["asl", "unlock", weird, "--json"],
                ["asl", "backfill-progress", "--force", weird],
                ["asl", "preflight", weird, "--json", "--mark-loop-ready"],
            ],
            [step["argv"] for step in plan],
        )
        for step in plan:
            self.assertEqual(step["argv"], shlex.split(step["command"]))
            self.assertIn(weird, shlex.split(step["command"]))


class PreflightEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.xdg_config = self.tmp_path / "xdg-config"
        self.xdg_state = self.tmp_path / "xdg-state"
        self.path_dir = self.tmp_path / "path"
        self.xdg_config.mkdir()
        self.xdg_state.mkdir()
        self.path_dir.mkdir()
        self._env_ctx = patched_env(
            XDG_CONFIG_HOME=str(self.xdg_config),
            XDG_STATE_HOME=str(self.xdg_state),
        )
        self._env_ctx.__enter__()
        self.plan_dir = self.tmp_path / "plan"
        _write_loop_ready_plan(self.plan_dir, ("pending",))

    def tearDown(self) -> None:
        self._env_ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def _drop_executable(self, directory: Path, name: str) -> Path:
        target = directory / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def _path_with(self, *names: str, base: Path | None = None) -> str:
        base = base or self.path_dir
        for name in names:
            self._drop_executable(base, name)
        return str(base)

    def test_env_repo_root_missing_emits_red(self) -> None:
        missing = self.tmp_path / "absent"
        report = preflight.run_environment_preflight(self.plan_dir, cwd=missing)
        repo_findings = [f for f in report.findings if f.category == "env-repo-root"]
        self.assertEqual(1, len(repo_findings))
        self.assertEqual("red", repo_findings[0].severity)
        self.assertIn("does not exist", repo_findings[0].message)
        self.assertFalse(report.ok)

    def test_env_repo_root_not_directory_emits_red(self) -> None:
        file_cwd = self.tmp_path / "file.txt"
        file_cwd.write_text("nope", encoding="utf-8")
        report = preflight.run_environment_preflight(self.plan_dir, cwd=file_cwd)
        repo_findings = [f for f in report.findings if f.category == "env-repo-root"]
        self.assertEqual(1, len(repo_findings))
        self.assertEqual("red", repo_findings[0].severity)
        self.assertIn("not a directory", repo_findings[0].message)

    def test_env_git_outside_repo_emits_red(self) -> None:
        cwd = self.tmp_path / "non-git"
        cwd.mkdir()
        report = preflight.run_environment_preflight(self.plan_dir, cwd=cwd)
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("red", git_findings[0].severity)
        self.assertIn("not inside a git repository", git_findings[0].message)

    def test_env_git_inside_repo_is_silent_without_commit_mode(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        report = preflight.run_environment_preflight(self.plan_dir, cwd=repo_dir)
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual([], git_findings)

    def test_env_git_dirty_with_commit_mode_emits_yellow(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        (repo_dir / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        report = preflight.run_environment_preflight(
            self.plan_dir, cwd=repo_dir, commit_mode="per-session"
        )
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0].severity)
        self.assertIn("dirty", git_findings[0].message)
        self.assertIn("per-session", git_findings[0].detail)

    def test_env_git_clean_with_commit_mode_is_silent(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        report = preflight.run_environment_preflight(
            self.plan_dir, cwd=repo_dir, commit_mode="end"
        )
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual([], git_findings)

    def test_env_harness_unsupported_name_emits_red(self) -> None:
        report = preflight.run_environment_preflight(self.plan_dir, harness="bogus")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("unsupported harness", harness_findings[0].message)

    def test_env_harness_missing_executable_emits_red(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("not found on PATH", harness_findings[0].message)

    def _drop_versioned_harness(self, directory: Path, name: str, output: str) -> Path:
        target = directory / name
        target.write_text(f"#!/bin/sh\nprintf '%s\\n' {output!r}\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def test_env_harness_supported_version_is_silent(self) -> None:
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)
        latest = harness_compatibility.POLICY["codex"].latest
        self._drop_versioned_harness(self.path_dir, "codex", f"codex-cli {latest}")
        with patched_env(PATH=str(self.path_dir)):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual([], harness_findings)

    def test_env_harness_unverified_version_warns_without_blocking(self) -> None:
        # An executable that reports nothing parseable is `unknown`. Preflight
        # must say so and still let the run start: yellow is a blocker here, so
        # the warning lane is green with the word "warning" in its message.
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)
        path_str = self._path_with("codex")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("green", harness_findings[0].severity)
        self.assertIn("unverified codex version", harness_findings[0].message)
        self.assertEqual((), report.red_findings)
        self.assertEqual((), report.yellow_findings)

    def test_env_harness_known_incompatible_version_emits_red(self) -> None:
        harness_compatibility.clear_probe_cache()
        self.addCleanup(harness_compatibility.clear_probe_cache)
        self._drop_versioned_harness(self.path_dir, "codex", "codex-cli 0.1.0")
        with patched_env(PATH=str(self.path_dir)):
            report = preflight.run_environment_preflight(self.plan_dir, harness="codex")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("known-incompatible", harness_findings[0].message)
        self.assertFalse(report.ok)

    def test_commit_mode_needs_no_external_commit_executable(self) -> None:
        # Auto-commit re-enters this checkout as `asl commit`, so an empty
        # PATH can no longer withhold anything a commit mode depends on and
        # preflight has nothing left to check for it.
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, commit_mode="per-session"
            )
        self.assertEqual([], [f for f in report.findings if f.category == "env-spec"])
        self.assertNotIn("env-spec", preflight.PREFLIGHT_CATEGORIES)
        self.assertNotIn(
            "spec", " ".join(finding.message for finding in report.findings)
        )

    def test_module_overview_omits_retired_spec_requirement(self) -> None:
        # The module overview is the last place the retired external commit
        # dependency could still be documented as a checked requirement.
        overview = preflight.__doc__ or ""
        self.assertNotEqual("", overview.strip())
        self.assertNotIn("env-spec", overview)
        self.assertNotIn("``spec``", overview)
        self.assertNotIn("spec availability", overview)
        self.assertIn("asl commit", overview)

    def test_env_lock_absent_emits_no_finding(self) -> None:
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual([], lock_findings)

    def test_env_lock_stale_emits_yellow(self) -> None:
        plan_state = state.plan_state_dir(self.plan_dir, self.xdg_state)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.xdg_state)
        canonical_plan = plans.canonicalize_path(self.plan_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(canonical_plan),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0].severity)
        self.assertIn("stale lock", lock_findings[0].message)

    def test_env_lock_active_emits_red(self) -> None:
        lock_info = state.acquire_lock(self.plan_dir, self.xdg_state)
        try:
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        finally:
            state.release_lock(self.plan_dir, self.xdg_state)
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("red", lock_findings[0].severity)
        self.assertIn(str(lock_info.pid), lock_findings[0].message)

    def test_env_amx_present_emits_green(self) -> None:
        path_str = self._path_with("amx")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        amx_findings = [f for f in report.findings if f.category == "env-amx"]
        self.assertEqual(1, len(amx_findings))
        self.assertEqual("green", amx_findings[0].severity)
        self.assertIn("available", amx_findings[0].message)

    def test_env_amx_absent_emits_green(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        amx_findings = [f for f in report.findings if f.category == "env-amx"]
        self.assertEqual(1, len(amx_findings))
        self.assertEqual("green", amx_findings[0].severity)
        self.assertIn("not on PATH", amx_findings[0].message)

    def test_env_turn_budget_green_with_pending_count(self) -> None:
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn("1 session(s) pending", budget_findings[0].message)

    def test_env_turn_budget_yellow_above_threshold(self) -> None:
        many_plan = self.tmp_path / "many-plan"
        _write_loop_ready_plan(
            many_plan,
            tuple(["pending"] * preflight.TURN_BUDGET_YELLOW_THRESHOLD),
        )
        report = preflight.run_environment_preflight(
            many_plan, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("yellow", budget_findings[0].severity)
        self.assertIn("exceeds", budget_findings[0].message)

    def test_env_turn_budget_override_allows_large_pending_count(self) -> None:
        many_plan = self.tmp_path / "many-plan-override"
        pending_count = preflight.TURN_BUDGET_YELLOW_THRESHOLD + 5
        _write_loop_ready_plan(
            many_plan,
            tuple(["pending"] * pending_count),
        )

        report = preflight.run_environment_preflight(
            many_plan,
            state_dir=self.xdg_state,
            max_pending_sessions=pending_count,
        )

        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn(
            f"{pending_count} session(s) pending",
            budget_findings[0].message,
        )
        self.assertIn(
            f"max pending sessions {pending_count}",
            budget_findings[0].detail,
        )

    def test_env_turn_budget_complete_plan_is_green(self) -> None:
        complete_plan = self.tmp_path / "complete-plan"
        _write_loop_ready_plan(complete_plan, ("complete", "complete"))
        report = preflight.run_environment_preflight(
            complete_plan, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn("all sessions complete", budget_findings[0].message)

    def test_run_environment_preflight_emits_no_core_findings(self) -> None:
        # Break readiness so core would emit a red. Environment-only must skip it.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        categories = {f.category for f in report.findings}
        self.assertNotIn("manifest-readiness", categories)
        self.assertNotIn("plan-structure", categories)

    def test_run_preflight_composes_core_and_env(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        # Prepend the controlled dir to system PATH so git remains discoverable
        # while the fake codex/spec/amx executables take precedence.
        for name in ("codex", "spec", "amx"):
            self._drop_executable(self.path_dir, name)
        path_str = os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])
        with patched_env(PATH=path_str):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=repo_dir,
                harness="codex",
                commit_mode="per-session",
                state_dir=self.xdg_state,
            )
        # Core fields surface.
        self.assertIsNotNone(report.snapshot)
        self.assertIsNotNone(report.readiness)
        self.assertEqual({"1"}, set(report.contracts_by_session))
        # Env findings are present (env-amx + env-turn-budget at minimum, both green).
        categories = {f.category for f in report.findings}
        self.assertIn("env-amx", categories)
        self.assertIn("env-turn-budget", categories)
        # No red/yellow → ok.
        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )


class MultiRepositoryPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace_root = self.root / "workspace"
        self.specs = DEFAULT_MULTI_REPO_SPECS[:2]
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            self.specs,
        )
        self.plan_dir = self.root / "plan"
        self.contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", '
            '"name": "readme", "repo": "SVC"}'
        )
        self._write_plan()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_plan(self) -> None:
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={"1": self.contract},
            specs=self.specs,
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_non_git_workspace_with_registered_children_passes(self) -> None:
        self.assertFalse((self.workspace_root / ".git").exists())

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertIsNotNone(report.verification_context)
        self.assertEqual(
            ["SVC", "OAK"],
            [repository.alias for repository in report.repositories],
        )
        self.assertTrue(
            all(repository.resolved_root for repository in report.repositories)
        )

    def test_workspace_is_resolved_once_per_full_preflight(self) -> None:
        original = workspaces.resolve_workspace
        with mock.patch.object(
            workspaces,
            "resolve_workspace",
            wraps=original,
        ) as resolve:
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        self.assertTrue(report.ok)
        resolve.assert_called_once()

    def test_wrong_origin_is_stable_red_finding(self) -> None:
        subprocess.run(
            ["git", "remote", "set-url", "origin", "git@example.com:wrong/repo.git"],
            cwd=self.repositories["OAK"],
            text=True,
            capture_output=True,
            check=True,
        )

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-origin"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertEqual(
            "repository-origin.correct-checkout",
            preflight.repair_hint_for_finding(findings[0]).check_id,
        )

    def test_missing_registered_child_is_repository_root_failure(self) -> None:
        shutil.rmtree(self.repositories["OAK"])

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-root"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertIn("OAK", findings[0].message)

    def test_overlapping_registry_paths_have_stable_category(self) -> None:
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                f"| OAK | {self.specs[1][1]} |",
                f"| OAK | {self.specs[0][1]} |",
            ),
            encoding="utf-8",
        )

        report = preflight.run_core_preflight(self.plan_dir)

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-overlap"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual(
            "repository-overlap.separate",
            preflight.repair_hint_for_finding(findings[0]).check_id,
        )

    def test_unavailable_workspace_evidence_fails_closed(self) -> None:
        unavailable = {
            "git": True,
            "unavailable": {"reason": "git-command-failed"},
        }
        with mock.patch.object(
            preflight.repository_evidence,
            "capture_repository_fingerprint",
            side_effect=[unavailable, {"git": True}],
        ):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        findings = [
            finding
            for finding in report.findings
            if "evidence is unavailable" in finding.message
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("repository-root", findings[0].category)
        self.assertIn("SVC", findings[0].message)

    def test_uninspectable_undeclared_repository_produces_no_finding(
        self,
    ) -> None:
        sibling = self.workspace_root / "undeclared-broken"
        (sibling / ".git").mkdir(parents=True)

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertEqual(
            [],
            [
                (finding.category, finding.severity, finding.message)
                for finding in report.findings
                if "undeclared-broken"
                in f"{finding.message}\n{finding.path}\n{finding.detail}"
            ],
        )

    def test_undeclared_breakage_is_ignored_while_registered_still_fails(
        self,
    ) -> None:
        sibling = self.workspace_root / "undeclared-broken"
        (sibling / ".git").mkdir(parents=True)
        unavailable = {
            "git": True,
            "unavailable": {"reason": "git-command-failed"},
        }
        with mock.patch.object(
            preflight.repository_evidence,
            "capture_repository_fingerprint",
            side_effect=[unavailable, {"git": True}],
        ):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        findings = [
            finding
            for finding in report.findings
            if "evidence is unavailable" in finding.message
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("red", findings[0].severity)
        self.assertEqual("repository-root", findings[0].category)
        self.assertIn("SVC", findings[0].message)
        self.assertEqual(
            [],
            [
                (finding.category, finding.severity, finding.message)
                for finding in report.findings
                if "undeclared-broken"
                in f"{finding.message}\n{finding.path}\n{finding.detail}"
            ],
        )

    def test_commit_mode_requires_every_registered_tree_clean(self) -> None:
        (self.repositories["OAK"] / "dirty.txt").write_text(
            "dirty\n",
            encoding="utf-8",
        )

        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
            commit_mode="per-session",
        )

        dirty = [
            finding
            for finding in report.findings
            if finding.category == "env-git" and "OAK" in finding.message
        ]
        self.assertEqual(1, len(dirty))
        self.assertEqual("yellow", dirty[0].severity)

    def test_baseline_staleness_uses_all_registered_repositories(self) -> None:
        self.contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", '
            '"name": "baseline", "repo": "SVC", '
            '"baseline_expectation": "pass"}'
        )
        self._write_plan()
        baseline.run_baseline(self.plan_dir, self.workspace_root)

        fresh = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )
        self.assertNotIn(
            "baseline-stale",
            {finding.category for finding in fresh.findings},
        )

        (self.repositories["OAK"] / "changed.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        stale = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        self.assertIn(
            "baseline-stale",
            {finding.category for finding in stale.findings},
        )

    def test_registered_baseline_fingerprint_is_captured_once_per_preflight(
        self,
    ) -> None:
        self.contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", '
            '"name": "svc-baseline", "repo": "SVC", '
            '"baseline_expectation": "pass"}, '
            '{"type": "file_exists", "path": "README.md", '
            '"name": "oak-baseline", "repo": "OAK", '
            '"baseline_expectation": "pass"}'
        )
        self._write_plan()
        baseline.run_baseline(self.plan_dir, self.workspace_root)
        original = baseline.capture_baseline_repository_evidence

        with mock.patch.object(
            baseline,
            "capture_baseline_repository_evidence",
            wraps=original,
        ) as capture:
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=self.workspace_root,
            )

        self.assertNotIn(
            "baseline-stale",
            {finding.category for finding in report.findings},
        )
        capture.assert_called_once()

    def test_invalid_scope_has_repository_scope_category(self) -> None:
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                "**Writes repositories**: SVC, OAK",
                "**Writes repositories**: Unknown",
            ),
            encoding="utf-8",
        )

        report = preflight.run_core_preflight(self.plan_dir)

        findings = [
            finding
            for finding in report.findings
            if finding.category == "repository-scope"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("1", findings[0].session_id)
        self.assertEqual(str(session.resolve()), findings[0].path)

    def test_unknown_contract_alias_has_repository_contract_category(self) -> None:
        session = self.plan_dir / "session-1.md"
        session.write_text(
            session.read_text(encoding="utf-8").replace(
                '"repo": "SVC"',
                '"repo": "Unknown"',
            ),
            encoding="utf-8",
        )

        report = preflight.run_core_preflight(self.plan_dir)

        self.assertIn(
            "repository-contract",
            {finding.category for finding in report.findings},
        )

    def test_registered_payload_has_ordered_repository_summaries(self) -> None:
        report = preflight.run_preflight(
            self.plan_dir,
            cwd=self.workspace_root,
        )

        payload = preflight._preflight_payload_fields(
            report,
            plan_dir_arg=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness=None,
            commit_mode=None,
            state_dir=None,
            max_pending_sessions=None,
            mark_loop_ready=False,
        )

        self.assertEqual(str(self.workspace_root.resolve()), payload["workspace_root"])
        self.assertEqual(
            ["SVC", "OAK"],
            [repository["alias"] for repository in payload["repositories"]],
        )
        self.assertEqual(
            self.specs[0][1],
            payload["repositories"][0]["path"],
        )


class PreflightResumeTests(unittest.TestCase):
    _SKIPPED_CHECKS = (
        "_check_session_contracts",
        "_check_ambiguity_sentinels",
        "_check_path_references",
        "_check_verification_scope",
        "_check_command_runnability",
        "_check_progress_journal",
        "_check_baselines",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.xdg_state = self.tmp_path / "xdg-state"
        self.xdg_state.mkdir()
        self.plan_dir = self.tmp_path / "plan"
        self.repo_dir = self.tmp_path / "repo"
        init_git_repo(self.repo_dir)
        _write_loop_ready_plan(self.plan_dir, ("pending",))

    def _rewrite_readiness(
        self,
        *,
        state: plans.ReadinessState = "loop-ready",
        sha: str | None = None,
    ) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=state,
                readiness_sha256=sha or plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _resume(self, **kwargs: Any) -> preflight.PreflightReport | None:
        kwargs.setdefault("snapshot", plans.inspect_plan(self.plan_dir))
        kwargs.setdefault("cwd", self.repo_dir)
        kwargs.setdefault("state_dir", self.xdg_state)
        return preflight.run_resume_preflight(self.plan_dir, **kwargs)

    def _unhashed(self, **kwargs: Any) -> tuple[preflight.PreflightFinding, ...]:
        kwargs.setdefault("snapshot", plans.inspect_plan(self.plan_dir))
        kwargs.setdefault("cwd", self.repo_dir)
        return preflight.run_unhashed_state_checks(self.plan_dir, **kwargs)

    def test_returns_none_without_snapshot(self) -> None:
        self.assertIsNone(self._resume(snapshot=None))

    def test_returns_none_for_non_resumable_readiness_states(self) -> None:
        for readiness_state in ("draft", "reviewed", "complete"):
            with self.subTest(state=readiness_state):
                self._rewrite_readiness(state=readiness_state)
                self.assertIsNone(self._resume())

    def test_returns_none_when_recorded_sha_does_not_match(self) -> None:
        self._rewrite_readiness(sha="00" * 32)

        self.assertIsNone(self._resume())

    def test_returns_none_when_readiness_metadata_is_missing(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        self.assertIsNone(self._resume())

    def test_returns_none_when_readiness_metadata_is_malformed(self) -> None:
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "**Readiness**: loop-ready", "**Readiness**: bogus"
            ),
            encoding="utf-8",
        )

        self.assertIsNone(self._resume())

    def test_eligible_report_skips_plan_content_checks(self) -> None:
        with mock.patch.multiple(
            preflight, **{name: mock.DEFAULT for name in self._SKIPPED_CHECKS}
        ) as mocks:
            report = self._resume()

        self.assertIsNotNone(report)
        for name, patched in mocks.items():
            self.assertFalse(patched.called, msg=name)
        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertEqual({}, report.contracts_by_session)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            report.expected_readiness_sha256,
        )
        self.assertEqual("loop-ready", report.readiness.state)
        self.assertIsNotNone(report.snapshot)
        categories = {f.category for f in report.findings}
        self.assertEqual({"env-amx", "env-turn-budget"}, categories)

    def test_clean_plan_has_no_unhashed_state_findings(self) -> None:
        self.assertEqual((), self._unhashed())

    def test_stale_progress_journal_surfaces_from_unhashed_state_checks(
        self,
    ) -> None:
        _write_loop_ready_plan(self.plan_dir, ("complete", "pending"))

        report = self._resume()
        findings = self._unhashed()

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        journal = [f for f in findings if f.category == "progress-journal-missing"]
        self.assertEqual(1, len(journal))
        self.assertEqual("yellow", journal[0].severity)

    def test_missing_baseline_evidence_surfaces_from_unhashed_state_checks(
        self,
    ) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _command_contract('["true"]', session="1", expectation="pass")
            },
        )

        report = self._resume()
        findings = self._unhashed()

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        baselines = [f for f in findings if f.category == "baseline-missing"]
        self.assertEqual(1, len(baselines))
        self.assertEqual("red", baselines[0].severity)

    def test_malformed_contract_surfaces_from_unhashed_state_checks(self) -> None:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "## Completion Contract", "## Notes"
            ),
            encoding="utf-8",
        )
        self._rewrite_readiness()

        report = self._resume()
        findings = self._unhashed()

        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )
        self.assertEqual(
            ["missing-contract"],
            [f.category for f in findings if f.severity == "red"],
        )

    def test_running_readiness_state_is_eligible(self) -> None:
        self._rewrite_readiness(state="running")

        report = self._resume()

        self.assertIsNotNone(report)
        self.assertEqual("running", report.readiness.state)

    def test_red_environment_finding_surfaces(self) -> None:
        report = self._resume(harness="bogus")

        self.assertIsNotNone(report)
        self.assertFalse(report.ok)
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)


class PreflightCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.xdg_config = self.tmp_path / "xdg-config"
        self.xdg_state = self.tmp_path / "xdg-state"
        self.path_dir = self.tmp_path / "path"
        self.xdg_config.mkdir()
        self.xdg_state.mkdir()
        self.path_dir.mkdir()
        self.plan_dir = self.tmp_path / "plan"
        self.repo_root = self.tmp_path / "repo"
        init_git_repo(self.repo_root)
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                ),
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _path_value(self, *names: str) -> str:
        for name in names:
            target = self.path_dir / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        return os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])

    def _run_preflight(
        self,
        *,
        json_output: bool = True,
        cwd: Path | None = None,
        harness: str | None = None,
        commit_mode: str | None = None,
        mark_loop_ready: bool = False,
        max_pending_sessions: int | None = None,
        path_value: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "preflight",
            str(self.plan_dir),
            "--state-dir",
            str(self.xdg_state),
        ]
        if cwd is not None:
            argv.extend(["--cwd", str(cwd)])
        if harness is not None:
            argv.extend(["--harness", harness])
        if commit_mode == "per-session":
            argv.append("--commit-per-session")
        elif commit_mode == "end":
            argv.append("--commit-at-end")
        if max_pending_sessions is not None:
            argv.extend(["--max-pending-sessions", str(max_pending_sessions)])
        if json_output:
            argv.append("--json")
        if mark_loop_ready:
            argv.append("--mark-loop-ready")
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(self.xdg_config)
        env["XDG_STATE_HOME"] = str(self.xdg_state)
        if path_value is not None:
            env["PATH"] = path_value
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_preflight_json_ok_when_ready_plan(self) -> None:
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("preflight", payload["command"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual(str(self.plan_dir), payload["plan_dir"])
        self.assertEqual(str(self.repo_root), payload["cwd"])
        self.assertEqual("codex", payload["harness"])
        self.assertEqual("per-session", payload["commit_mode"])
        self.assertFalse(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        # An ok plan needs no repairs and suggests no pause.
        self.assertEqual([], payload["auto_repair_plan"])
        self.assertIsNone(payload["suggested_pause"])
        # No red/yellow findings present.
        counts = payload["findings_by_severity"]
        self.assertEqual(0, counts["red"])
        self.assertEqual(0, counts["yellow"])
        # All green findings still surface (env-amx and env-turn-budget at minimum).
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("env-amx", categories)
        self.assertIn("env-turn-budget", categories)
        # Readiness and expected sha-256 surface as serializable values.
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        self.assertEqual(
            payload["readiness"]["readiness_sha256"],
            payload["expected_readiness_sha256"],
        )

    def test_mark_loop_ready_accepts_max_pending_sessions_override(self) -> None:
        pending_count = preflight.TURN_BUDGET_YELLOW_THRESHOLD + 5
        _write_loop_ready_plan(
            self.plan_dir,
            tuple(["pending"] * pending_count),
            state="draft",
        )

        blocked = self._run_preflight(mark_loop_ready=True)
        blocked_payload = json.loads(blocked.stdout)

        self.assertEqual(1, blocked.returncode)
        self.assertFalse(blocked_payload["marked_loop_ready"])
        self.assertIn("env-turn-budget", blocked_payload["mark_error"])

        completed = self._run_preflight(
            mark_loop_ready=True,
            max_pending_sessions=pending_count,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual(pending_count, payload["max_pending_sessions"])
        budget_findings = [
            finding
            for finding in payload["findings"]
            if finding["category"] == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0]["severity"])

    def test_preflight_json_red_when_readiness_breaks(self) -> None:
        # Break readiness sha to produce a red manifest-readiness finding.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-red", payload["reason"])
        self.assertFalse(payload["paused"])
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("manifest-readiness", categories)
        # A readiness-only break is auto-repairable via --mark-loop-ready and
        # the emitted repair_hint embeds the plan-dir-substituted command.
        commands = [
            step["command"] for step in payload["auto_repair_plan"] if "command" in step
        ]
        self.assertIn(
            f"asl preflight {self.plan_dir} --json --mark-loop-ready",
            commands,
        )
        self.assertIsNone(payload["suggested_pause"])
        readiness_hints = [
            hint
            for hint in payload["repair_hints"]
            if hint["category"] == "manifest-readiness"
        ]
        self.assertTrue(readiness_hints)
        self.assertIn(
            f"asl preflight {self.plan_dir} --json --mark-loop-ready",
            readiness_hints[0]["message"],
        )
        # findings keep the dataclass shape exactly.
        sample = payload["findings"][0]
        for key in (
            "severity",
            "category",
            "message",
            "session_id",
            "path",
            "line",
            "detail",
        ):
            self.assertIn(key, sample)

    def test_preflight_json_yellow_when_ambiguity_only(self) -> None:
        # Inject a yellow ambiguity sentinel into session-1.md instructions.
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        text = text.replace("Instructions.", "Instructions. TODO: revisit later")
        session_path.write_text(text, encoding="utf-8")
        # Recompute readiness so manifest-readiness stays green.
        expected = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=expected,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        categories_by_severity = {
            (finding["severity"], finding["category"])
            for finding in payload["findings"]
        }
        self.assertIn(("yellow", "ambiguity-sentinel"), categories_by_severity)

    def test_preflight_mark_loop_ready_writes_manifest(self) -> None:
        # The fixture is already loop-ready; --mark-loop-ready refreshes the
        # checked_at timestamp once preflight succeeds.
        original_sha = plans.stable_readiness_sha256(self.plan_dir)
        original = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(original)
        self.assertEqual("loop-ready", original.state)
        self.assertEqual(original_sha, original.readiness_sha256)
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertTrue(payload["marked_loop_ready"])
        refreshed = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(refreshed)
        self.assertEqual("loop-ready", refreshed.state)
        self.assertEqual(original_sha, refreshed.readiness_sha256)
        self.assertNotEqual(original.checked_at, refreshed.checked_at)

    def test_preflight_mark_loop_ready_promotes_draft_or_reviewed(self) -> None:
        for readiness_state in ("draft", "reviewed"):
            with self.subTest(readiness_state=readiness_state):
                _write_loop_ready_plan(
                    self.plan_dir,
                    ("pending",),
                    contracts_by_id={
                        "1": _contract_block(
                            '{"type": "file_exists", "path": "README.md", '
                            '"name": "readme-1"}',
                            session="1",
                        ),
                    },
                    state=readiness_state,
                )

                completed = self._run_preflight(mark_loop_ready=True)

                self.assertEqual(0, completed.returncode, completed.stderr)
                payload = json.loads(completed.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual("loop-ready-marked", payload["reason"])
                self.assertTrue(payload["marked_loop_ready"])
                self.assertEqual("loop-ready", payload["readiness"]["state"])
                self.assertEqual(0, payload["findings_by_severity"]["red"])
                self.assertEqual(0, payload["findings_by_severity"]["yellow"])
                metadata = plans.read_readiness_metadata(self.plan_dir)
                self.assertIsNotNone(metadata)
                self.assertEqual("loop-ready", metadata.state)

    def test_preflight_mark_loop_ready_repairs_missing_readiness(self) -> None:
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_preflight_mark_loop_ready_repairs_stale_readiness_sha(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        self.assertEqual(
            payload["expected_readiness_sha256"],
            payload["readiness"]["readiness_sha256"],
        )

    def test_preflight_mark_loop_ready_refused_when_non_readiness_not_ok(
        self,
    ) -> None:
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            text.replace("Instructions.", "Instructions. TODO: revisit later"),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="draft",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        readiness = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_preflight_command_not_runnable_blocks_mark_loop_ready(self) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "command", "argv": '
                    '["definitely-not-a-real-binary-xyz", "--version"], '
                    '"timeout_seconds": 30, "name": "run-1"}',
                    session="1",
                ),
            },
            state="draft",
        )

        completed = self._run_preflight(cwd=self.repo_root, mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-red", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        command_findings = [
            finding
            for finding in payload["findings"]
            if finding["category"] == "command-not-runnable"
        ]
        self.assertEqual(1, len(command_findings))
        self.assertEqual("red", command_findings[0]["severity"])
        readiness = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_preflight_mark_loop_ready_auto_backfills_missing_progress_journal(
        self,
    ) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertIn("auto_repairs", payload)
        self.assertEqual(1, len(payload["auto_repairs"]))
        repair = payload["auto_repairs"][0]
        self.assertEqual("rewrite", repair["action"])
        self.assertEqual("progress-journal-missing", repair["category"])
        self.assertEqual(str(progress_file), repair["path"])
        self.assertEqual(1, repair["entries_written"])
        self.assertEqual(["1"], repair["completed_session_ids"])
        self.assertFalse(repair["journal_replaced"])
        # The progress.md was actually rebuilt and the rerun no longer surfaces
        # progress-journal-missing.
        self.assertTrue(progress_file.exists())
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertNotIn("progress-journal-missing", categories)
        entries = plans.read_progress_entries(self.plan_dir)
        self.assertEqual(("1",), tuple(entry.session_id.value for entry in entries))

    def test_preflight_mark_loop_ready_refuses_when_progress_plus_other_blocker(
        self,
    ) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        # Inject a yellow ambiguity sentinel so progress-journal-missing is not
        # the only non-readiness blocker. Readiness SHA excludes handoffs and
        # sentinel-only edits do not affect the stable hash because they live
        # inside the instructions body, so refresh readiness explicitly.
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            text.replace("Instructions.", "Instructions. TODO: revisit later"),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        progress_file = plans.progress_path(self.plan_dir)
        self.assertFalse(progress_file.exists())

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        self.assertEqual([], payload["auto_repairs"])
        # No repair attempted because the blockers were not progress-only.
        self.assertFalse(progress_file.exists())
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("progress-journal-missing", categories)
        self.assertIn("ambiguity-sentinel", categories)

    def test_preflight_json_auto_repairs_empty_when_no_repair_runs(self) -> None:
        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("auto_repairs", payload)
        self.assertEqual([], payload["auto_repairs"])

    def test_preflight_text_output_mentions_auto_repair(self) -> None:
        _write_loop_ready_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        completed = self._run_preflight(
            json_output=False,
            mark_loop_ready=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Auto-repair: rebuilt progress journal", completed.stdout)
        self.assertIn("1 entry written", completed.stdout)
        self.assertIn("Preflight: ok", completed.stdout)
        self.assertIn("Loop-ready: marked", completed.stdout)
        # Stable reason strings remain unaffected.
        self.assertNotIn("loop-ready-refused", completed.stdout)
        self.assertNotIn("loop-ready-write-failed", completed.stdout)

    def test_preflight_text_output_lists_findings(self) -> None:
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            json_output=False,
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f"Plan: {self.plan_dir}", completed.stdout)
        self.assertIn("Sessions: 1", completed.stdout)
        self.assertIn("Harness: codex", completed.stdout)
        self.assertIn("Commit mode: per-session", completed.stdout)
        self.assertIn("Preflight: ok", completed.stdout)
        self.assertIn("[GREEN] env-amx", completed.stdout)
        self.assertIn("[GREEN] env-turn-budget", completed.stdout)

    def test_preflight_text_output_failure_routes_summary_to_stderr(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight(json_output=False)
        self.assertEqual(1, completed.returncode)
        self.assertIn("[RED] manifest-readiness", completed.stdout)
        self.assertIn("Preflight: failed", completed.stderr)

    def test_preflight_json_includes_repair_hints_aligned_with_findings(
        self,
    ) -> None:
        # Break readiness to force a red manifest-readiness finding so we have
        # at least one finding to align a repair hint with.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertIn("repair_hints", payload)
        self.assertIsInstance(payload["repair_hints"], list)
        self.assertEqual(len(payload["findings"]), len(payload["repair_hints"]))
        # Each hint exposes the documented stable keys.
        for hint in payload["repair_hints"]:
            for key in (
                "category",
                "check_id",
                "severity_hint",
                "message",
                "question_template",
                "fix_category",
            ):
                self.assertIn(key, hint)
        # The manifest-readiness finding resolves to the sha-mismatch override.
        manifest_hints = [
            hint
            for hint, finding in zip(
                payload["repair_hints"], payload["findings"], strict=False
            )
            if finding["category"] == "manifest-readiness"
        ]
        self.assertTrue(manifest_hints)
        self.assertEqual(
            "manifest-readiness.sha-mismatch", manifest_hints[0]["check_id"]
        )


if __name__ == "__main__":
    unittest.main()
