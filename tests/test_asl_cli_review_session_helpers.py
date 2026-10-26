"""Generated review-session body, verdict parsing and session-id helper tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    contracts,
    events,
    plans_discovery,
    plans_files,
    plans_handoff,
    plans_payloads,
    plans_session_ids,
    review,
    review_session_body,
    review_session_contract,
    verification_run,
)
from tests.support import (
    write_plan,
    write_plan_with_ids,
)


class ReviewSessionHelperTests(unittest.TestCase):
    """Generated review-session body, verdict parsing and session-id helper tests."""

    def test_review_session_kind_constants(self) -> None:
        self.assertEqual("implementation", plans_payloads.SESSION_KIND_IMPLEMENTATION)
        self.assertEqual("review", plans_payloads.SESSION_KIND_REVIEW)
        self.assertEqual(
            (
                plans_payloads.SESSION_KIND_IMPLEMENTATION,
                plans_payloads.SESSION_KIND_REVIEW,
            ),
            plans_payloads.SESSION_KINDS,
        )
        self.assertEqual("final-review.md", review.FINAL_REVIEW_FILENAME)
        self.assertIn("asl:generated-review-session", review.REVIEW_SESSION_MARKER)

    def test_review_pause_reasons_are_registered(self) -> None:
        self.assertIn("review-harness-required", events.PAUSE_REASONS)
        self.assertIn("review-mutated-worktree", events.PAUSE_REASONS)
        self.assertEqual(
            "review-guard-unavailable",
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE,
        )
        self.assertIn(
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, events.PAUSE_REASONS
        )

    def test_contains_review_session_marker_detects_marker(self) -> None:
        body_with_marker = "\n".join(
            [
                "# Session 7: Final Review",
                "",
                review.REVIEW_SESSION_MARKER,
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
            ]
        )
        body_without_marker = "\n".join(
            [
                "# Session 1",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
            ]
        )

        self.assertTrue(
            review_session_contract.contains_review_session_marker(body_with_marker)
        )
        self.assertFalse(
            review_session_contract.contains_review_session_marker(body_without_marker)
        )

    def test_is_generated_review_session_file_reads_marker_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            review_path = plan_dir / "session-7.md"
            other_path = plan_dir / "session-1.md"
            review_path.write_text(
                f"# Review\n\n{review.REVIEW_SESSION_MARKER}\n",
                encoding="utf-8",
            )
            other_path.write_text("# Session 1\n", encoding="utf-8")

            self.assertTrue(
                review_session_contract.is_generated_review_session_file(review_path)
            )
            self.assertFalse(
                review_session_contract.is_generated_review_session_file(other_path)
            )
            self.assertFalse(
                review_session_contract.is_generated_review_session_file(
                    plan_dir / "missing.md"
                )
            )

    def test_final_review_output_path_joins_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)

            self.assertEqual(
                plan_dir / review.FINAL_REVIEW_FILENAME,
                review_session_contract.final_review_output_path(plan_dir),
            )

    def test_parse_review_verdict_accepts_all_clear_status(self) -> None:
        verdict = review_session_contract.parse_review_verdict(
            "## Findings\n"
            "- Reviewed agent_session_loop/runner.py.\n"
            "Status: All clear, no findings\n"
        )

        self.assertEqual("all-clear", verdict["outcome"])
        self.assertEqual(0, verdict["total_findings"])
        self.assertEqual(
            {"high": 0, "med": 0, "low": 0},
            verdict["findings_by_severity"],
        )

    def test_parse_review_verdict_accepts_counted_findings_status(self) -> None:
        verdict = review_session_contract.parse_review_verdict(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "Status: 5 Findings (2 high, 2 med, 1 low)\n"
        )

        self.assertEqual("findings", verdict["outcome"])
        self.assertEqual(5, verdict["total_findings"])
        self.assertEqual(
            {"high": 2, "med": 2, "low": 1},
            verdict["findings_by_severity"],
        )

    def test_parse_review_verdict_rejects_missing_status(self) -> None:
        with self.assertRaisesRegex(
            review_session_contract.ReviewVerdictError,
            "missing required final Status line",
        ):
            review_session_contract.parse_review_verdict(
                "## Findings\n- Nothing to report.\n"
            )

    def test_parse_review_verdict_rejects_multiple_status_lines(self) -> None:
        with self.assertRaisesRegex(
            review_session_contract.ReviewVerdictError,
            "expected exactly one Status line",
        ):
            review_session_contract.parse_review_verdict(
                "Status: All clear, no findings\n"
                "## Findings\n"
                "Status: All clear, no findings\n"
            )

    def test_parse_review_verdict_rejects_non_final_status(self) -> None:
        with self.assertRaisesRegex(
            review_session_contract.ReviewVerdictError,
            "Status line must be the final non-empty line",
        ):
            review_session_contract.parse_review_verdict(
                "## Findings\nStatus: All clear, no findings\n- trailing note\n"
            )

    def test_parse_review_verdict_rejects_malformed_status(self) -> None:
        with self.assertRaisesRegex(
            review_session_contract.ReviewVerdictError, "malformed Status line"
        ):
            review_session_contract.parse_review_verdict(
                "## Findings\nStatus: 1 Finding (1 high, 0 med, 0 low)\n"
            )

    def test_parse_review_verdict_rejects_count_mismatch(self) -> None:
        with self.assertRaisesRegex(
            review_session_contract.ReviewVerdictError, "severity counts do not add up"
        ):
            review_session_contract.parse_review_verdict(
                "## Findings\nStatus: 5 Findings (2 high, 2 med, 0 low)\n"
            )

    def test_parse_review_verdict_rejects_a_count_too_long_to_convert(self) -> None:
        # Past Python's integer-string limit ``int()`` raises ValueError, which
        # escaped the parser as a traceback instead of a malformed verdict.
        digits = "1" * 5000
        with self.assertRaisesRegex(
            review_session_contract.ReviewVerdictError, "too large"
        ):
            review_session_contract.parse_review_verdict(
                f"Status: {digits} Findings ({digits} high, 0 med, 0 low)\n"
            )

    def test_next_top_level_session_id_returns_one_for_empty_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")

            self.assertEqual(
                "1", plans_discovery.next_top_level_session_id(plan_dir).value
            )

    def test_next_top_level_session_id_uses_max_top_level_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan_with_ids(
                plan_dir,
                (
                    ("1", "complete"),
                    ("2.1", "complete"),
                    ("3", "pending"),
                ),
            )

            self.assertEqual(
                "4", plans_discovery.next_top_level_session_id(plan_dir).value
            )

    def test_next_top_level_session_id_ignores_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))
            (plan_dir / "session-9.expand.md").write_text(
                "# Expansion\n", encoding="utf-8"
            )

            self.assertEqual(
                "2", plans_discovery.next_top_level_session_id(plan_dir).value
            )

    def test_build_generated_review_session_body_is_valid_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans_session_ids.SessionId((7,))

            body = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
            )

            self.assertIn(review.REVIEW_SESSION_MARKER, body)
            self.assertIn("claude", body)
            self.assertIn(review.FINAL_REVIEW_FILENAME, body)
            self.assertIn("three distinct source, documentation, or test", body)
            self.assertIn("review-oriented section heading", body)
            self.assertIn("valid `Status:` verdict line", body)
            self.assertIn("Do not modify", body)
            self.assertIn("- **Status**: pending", body)

            contract = contracts.parse_completion_contract(body, session_id=session_id)
            self.assertIsNotNone(contract)
            self.assertEqual(session_id, contract.session_id)
            self.assertEqual(2, len(contract.verify.checks))
            check = contract.verify.checks[0]
            self.assertIsInstance(check, contracts.CommandCheck)
            self.assertEqual("final-review-output-evidence-based", check.name)
            self.assertEqual("python3", check.argv[0])
            # The check runs at the bound root; the plan artifacts it reads are
            # named by absolute path inside the command source.
            self.assertEqual(".", check.cwd)
            self.assertIn(
                str(
                    plans_files.canonicalize_path(plan_dir)
                    / review.FINAL_REVIEW_FILENAME
                ),
                check.argv[2],
            )

    def test_find_pending_generated_review_session_returns_none_when_no_marker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("pending", "complete"))

            self.assertIsNone(
                review_session_body.find_pending_generated_review_session(plan_dir)
            )

    def test_find_pending_generated_review_session_returns_pending_marked_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete", "complete"))
            review_id = plans_discovery.next_top_level_session_id(plan_dir)
            review_path = plan_dir / review_id.filename
            review_path.write_text(
                review_session_body.build_generated_review_session_body(
                    review_id,
                    review_harness="claude",
                    plan_dir=plan_dir,
                ),
                encoding="utf-8",
            )

            found = review_session_body.find_pending_generated_review_session(plan_dir)

            self.assertIsNotNone(found)
            self.assertEqual(review_id, found.session_id)
            self.assertEqual(plans_files.canonicalize_path(review_path), found.path)

    def test_find_pending_generated_review_session_skips_completed_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))
            review_id = plans_discovery.next_top_level_session_id(plan_dir)
            review_path = plan_dir / review_id.filename
            body = review_session_body.build_generated_review_session_body(
                review_id,
                review_harness="claude",
                plan_dir=plan_dir,
            ).replace("- **Status**: pending", "- **Status**: complete")
            review_path.write_text(body, encoding="utf-8")

            self.assertIsNone(
                review_session_body.find_pending_generated_review_session(plan_dir)
            )

    def test_ensure_generated_review_session_creates_new_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete", "pending"))

            session_file, created = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )

            self.assertTrue(created)
            self.assertEqual("3", session_file.session_id.value)
            self.assertEqual(
                plans_files.canonicalize_path(plan_dir / "session-3.md"),
                session_file.path,
            )
            self.assertTrue(
                review_session_contract.is_generated_review_session_file(
                    session_file.path
                )
            )
            status = plans_handoff.read_session_status(session_file)
            self.assertEqual("pending", status.status)

    def test_ensure_generated_review_session_reuses_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            first_file, first_created = (
                review_session_body.ensure_generated_review_session(
                    plan_dir, review_harness="claude"
                )
            )
            second_file, second_created = (
                review_session_body.ensure_generated_review_session(
                    plan_dir, review_harness="claude"
                )
            )

            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_file.session_id, second_file.session_id)
            self.assertEqual(first_file.path, second_file.path)

    def test_ensure_generated_review_session_appends_after_completed_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))
            first_file, _ = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            completed_body = first_file.path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete"
            )
            first_file.path.write_text(completed_body, encoding="utf-8")

            second_file, created = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )

            self.assertTrue(created)
            self.assertNotEqual(first_file.session_id, second_file.session_id)
            self.assertEqual(
                first_file.session_id.components[0] + 1,
                second_file.session_id.components[0],
            )

    def test_ensure_generated_review_session_contract_passes_with_review_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                (
                    "## Findings\n"
                    "- Reviewed agent_session_loop/runner.py for generated review "
                    "contracts.\n"
                    "- Checked tests/test_agent_session_loop.py for lifecycle "
                    "coverage.\n"
                    "- Compared docs/agent-session-loop.md for operator-facing "
                    "documentation impact.\n"
                    "Status: All clear, no findings\n"
                ),
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
                encoding="utf-8",
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification_run.run_verification(contract, repo_root=plan_dir)
            self.assertTrue(result.passed, result.failures)

    def test_ensure_generated_review_session_contract_fails_without_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "   \n\n  \t\n", encoding="utf-8"
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification_run.run_verification(contract, repo_root=plan_dir)
            self.assertFalse(result.passed)

    def test_ensure_generated_review_session_contract_fails_without_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                (
                    "## Findings\n"
                    "- Reviewed agent_session_loop/runner.py for generated review "
                    "contracts.\n"
                    "- Checked tests/test_agent_session_loop.py for lifecycle "
                    "coverage.\n"
                    "- Compared docs/agent-session-loop.md for operator-facing "
                    "documentation impact.\n"
                ),
                encoding="utf-8",
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification_run.run_verification(contract, repo_root=plan_dir)
            self.assertFalse(result.passed)

    def test_ensure_generated_review_session_contract_fails_low_effort_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            write_plan(plan_dir, ("complete",))

            session_file, _ = review_session_body.ensure_generated_review_session(
                plan_dir, review_harness="claude"
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "Looks good overall.\nNo issues found.\n", encoding="utf-8"
            )

            contract = contracts.read_completion_contract(
                plan_dir, session_file.session_id
            )
            self.assertIsNotNone(contract)
            result = verification_run.run_verification(contract, repo_root=plan_dir)
            self.assertFalse(result.passed)


if __name__ == "__main__":
    unittest.main()
