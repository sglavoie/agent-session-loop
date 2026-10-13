"""Preflight core, runnability, repair hint, environment, and CLI tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans,
    preflight,
)
from tests.support import (
    _write_loop_ready_plan,
    write_plan,
)
from tests.support_asl_preflight import PreflightCoreFixtures


class PreflightPlanStructureTests(PreflightCoreFixtures):
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


if __name__ == "__main__":
    unittest.main()
