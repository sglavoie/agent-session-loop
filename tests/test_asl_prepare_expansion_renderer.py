"""Expansion renderer tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    authoring,
    plans,
    prepare_corrections,
    prepare_expansion,
    prepare_models,
    prepare_parsing,
)
from tests.support import (
    _validator_payload,
    _write_prepare_plan,
)


class ExpansionRendererTests(unittest.TestCase):
    """Expansion rendering and exact factual corrections."""

    def _validation(
        self, **session_overrides: object
    ) -> prepare_models.PrepareValidation:
        return prepare_parsing.parse_validator_output(
            _validator_payload(**session_overrides)
        )

    # -- rendering -------------------------------------------------------------

    def test_render_matches_expansion_schema(self) -> None:
        validation = self._validation(
            issues=[
                {
                    "issue_type": "signature-mismatch",
                    "summary": "load_config takes two args",
                    "session_ref": "`load_config(path)`",
                    "actual": "load_config(path, strict)",
                    "fix": "pass strict=True",
                }
            ],
            details=[{"path": "foo/bar.py", "notes": ["Lines 1-3: retry helper"]}],
            patterns=[
                {"name": "retry", "location": "`foo/bar.py:1-3`", "reason": "mirror it"}
            ],
            dependencies=[
                {"direction": "requires", "session_id": "2", "description": "helper"},
                {"direction": "provides", "session_id": "3", "description": "API"},
            ],
            contract_problems=[
                {"message": "stale path", "suggested_fix": "point at foo/bar.py"}
            ],
        )

        rendered = prepare_expansion.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertIn("# Session 1 Expansion: Test", rendered)
        self.assertIn("**Expanded**: 2026-07-10", rendered)
        self.assertIn("**Codebase root**: `/repo`", rendered)
        self.assertIn("**Claims checked**: 3 | **Issues found**: 1", rendered)
        self.assertIn("### ISSUE-1: load_config takes two args", rendered)
        self.assertIn("- **Type**: signature-mismatch", rendered)
        self.assertIn("| Claim | Type | Location | Line |", rendered)
        self.assertIn("| `load_config` | S | `foo/config.py` | 12 |", rendered)
        self.assertIn("### File: `foo/bar.py`", rendered)
        self.assertIn("- **retry**: `foo/bar.py:1-3` — mirror it", rendered)
        self.assertIn("- **Requires from session 2**: helper", rendered)
        self.assertIn("- **Provides for session 3**: API", rendered)
        self.assertIn("## Contract Issues", rendered)
        self.assertIn("- `1: stale path: point at foo/bar.py`", rendered)
        self.assertNotIn(prepare_models.NO_CONTRACT_ISSUES_LINE, rendered)

    def test_render_omits_empty_sections_and_marks_clean_contract(self) -> None:
        validation = self._validation(claims=[], issues=[])
        rendered = prepare_expansion.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertNotIn("## Issues", rendered)
        self.assertNotIn("## Verified Claims", rendered)
        self.assertNotIn("## Concrete Details", rendered)
        self.assertNotIn("## Patterns to Reuse", rendered)
        self.assertNotIn("## Session Dependencies", rendered)
        self.assertIn("## Contract Issues", rendered)
        self.assertIn(prepare_models.NO_CONTRACT_ISSUES_LINE, rendered)

    def test_render_to_be_created_claim_row(self) -> None:
        validation = self._validation(
            claims=[
                {
                    "claim": "NewTests",
                    "claim_type": "S",
                    "to_be_created": True,
                }
            ]
        )
        rendered = prepare_expansion.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )
        self.assertIn(
            f"| `NewTests` | S | {prepare_models.TO_BE_CREATED_LOCATION} | "
            f"{prepare_models.TO_BE_CREATED_LINE} |",
            rendered,
        )

    def test_render_places_removal_findings_in_existing_sections(self) -> None:
        validation = self._validation(
            claims=[
                {
                    "claim": "retry backoff branch",
                    "claim_type": "R",
                    "location": "foo/bar.py — added in a1b2c3d",
                    "line": 12,
                    "to_be_created": False,
                }
            ],
            issues=[
                {
                    "issue_type": "removal-unjustified",
                    "summary": "no rationale for dropping the backoff",
                    "session_ref": "delete the backoff branch",
                    "actual": "no commit or covering test explains the branch",
                    "fix": "record the origin rationale before removing it",
                }
            ],
        )

        rendered = prepare_expansion.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertIn(
            "| `retry backoff branch` | R | `foo/bar.py — added in a1b2c3d` | 12 |",
            rendered,
        )
        self.assertIn("- **Type**: removal-unjustified", rendered)
        self.assertEqual(1, rendered.count("## Issues"))
        self.assertEqual(1, rendered.count("## Verified Claims"))

    def test_render_without_removal_content_is_byte_identical(self) -> None:
        validation = self._validation(
            issues=[
                {
                    "issue_type": "signature-mismatch",
                    "summary": "load_config takes two args",
                    "session_ref": "`load_config(path)`",
                    "actual": "load_config(path, strict)",
                    "fix": "pass strict=True",
                }
            ],
            details=[{"path": "foo/bar.py", "notes": ["Lines 1-3: retry helper"]}],
            patterns=[
                {"name": "retry", "location": "`foo/bar.py:1-3`", "reason": "mirror it"}
            ],
            dependencies=[
                {"direction": "requires", "session_id": "2", "description": "helper"},
                {"direction": "provides", "session_id": "3", "description": "API"},
            ],
            contract_problems=[
                {"message": "stale path", "suggested_fix": "point at foo/bar.py"}
            ],
        )

        rendered = prepare_expansion.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )

        self.assertEqual(
            "\n".join(
                [
                    "# Session 1 Expansion: Test",
                    "",
                    "**Expanded**: 2026-07-10",
                    "**Codebase root**: `/repo`",
                    "**Claims checked**: 3 | **Issues found**: 1",
                    "",
                    "## Issues",
                    "",
                    "### ISSUE-1: load_config takes two args",
                    "",
                    "- **Type**: signature-mismatch",
                    '- **Session ref**: "`load_config(path)`"',
                    "- **Actual**: load_config(path, strict)",
                    "- **Fix**: pass strict=True",
                    "",
                    "## Verified Claims",
                    "",
                    "| Claim | Type | Location | Line |",
                    "| --- | --- | --- | --- |",
                    "| `load_config` | S | `foo/config.py` | 12 |",
                    "",
                    "## Concrete Details",
                    "",
                    "### File: `foo/bar.py`",
                    "",
                    "- Lines 1-3: retry helper",
                    "",
                    "## Patterns to Reuse",
                    "",
                    "- **retry**: `foo/bar.py:1-3` — mirror it",
                    "",
                    "## Session Dependencies",
                    "",
                    "- **Requires from session 2**: helper",
                    "- **Provides for session 3**: API",
                    "",
                    "## Contract Issues",
                    "",
                    "- `1: stale path: point at foo/bar.py`",
                    "",
                ]
            ),
            rendered,
        )

    def test_render_omits_contract_issues_when_not_checked(self) -> None:
        validation = self._validation(contract_checked=False)
        rendered = prepare_expansion.render_expansion(
            validation.sessions[0], codebase_root="/repo", expanded="2026-07-10"
        )
        self.assertNotIn("## Contract Issues", rendered)

    # -- expansion writing -----------------------------------------------------

    def test_write_expansions_writes_file_and_reference_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._validation()

            prepare_expansion.write_expansions(
                plan_dir, validation, expanded="2026-07-10"
            )

            expansion_path = plan_dir / "session-1.expand.md"
            self.assertTrue(expansion_path.is_file())
            session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            reference = prepare_expansion.expansion_reference_line("1")
            self.assertEqual(1, session_text.count(reference))
            # The reference sits directly under the Before-starting line.
            lines = session_text.splitlines()
            anchor = lines.index(authoring.CONTEXT_REFERENCE_LINE)
            self.assertEqual(reference, lines[anchor + 1])

            prepare_expansion.write_expansions(
                plan_dir, validation, expanded="2026-07-10"
            )
            session_text_again = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertEqual(session_text, session_text_again)
            self.assertEqual(1, session_text_again.count(reference))

    def test_write_expansions_fails_closed_on_missing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            payload = _validator_payload(session_id="7")
            validation = prepare_parsing.parse_validator_output(payload)

            with self.assertRaisesRegex(plans.PlanError, "missing session file"):
                prepare_expansion.write_expansions(plan_dir, validation)
            self.assertFalse((plan_dir / "session-7.expand.md").exists())

    # -- exact factual corrections ----------------------------------------------

    def _corrections_validation(
        self, corrections: list[dict]
    ) -> prepare_models.PrepareValidation:
        return self._validation(corrections=corrections)

    def test_correction_applies_on_exactly_one_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "Update the retry helper in `foo/bar.py`",
                        "replacement_text": "Update the retry helper in `foo/baz.py`",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(1, len(outcomes))
            self.assertEqual(prepare_models.CORRECTION_APPLIED, outcomes[0].status)
            text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn("`foo/baz.py` to use `load_config`", text)
            # Contract and handoff occurrences of the old path are untouched.
            self.assertIn("The helper in `foo/bar.py` passes tests.", text)
            self.assertIn("remember `foo/bar.py` cleanup", text)

    def test_correction_is_idempotent_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "Update the retry helper in `foo/bar.py`",
                        "replacement_text": "Update the retry helper in `foo/baz.py`",
                        "reason": "file moved",
                    }
                ]
            )

            prepare_corrections.apply_corrections(plan_dir, validation)
            text_after_first = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(
                prepare_models.CORRECTION_ALREADY_APPLIED, outcomes[0].status
            )
            self.assertEqual(
                text_after_first,
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_ambiguous_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "`foo/bar.py`",
                        "replacement_text": "`foo/baz.py`",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare_models.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare_models.BLOCKER_AMBIGUOUS, outcomes[0].blocker)
            self.assertEqual(
                before, (plan_dir / "session-1.md").read_text(encoding="utf-8")
            )

    def test_unknown_text_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "text that never existed",
                        "replacement_text": "anything",
                        "reason": "r",
                    }
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare_models.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare_models.BLOCKER_NOT_FOUND, outcomes[0].blocker)

    def test_contract_body_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            before = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "The helper in `foo/bar.py` passes tests.",
                        "replacement_text": "The helper in `foo/baz.py` passes tests.",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare_models.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare_models.BLOCKER_CONTRACT_BODY, outcomes[0].blocker)
            self.assertEqual(
                before, (plan_dir / "session-1.md").read_text(encoding="utf-8")
            )

    def test_handoff_body_correction_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "remember `foo/bar.py` cleanup",
                        "replacement_text": "remember `foo/baz.py` cleanup",
                        "reason": "file moved",
                    }
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare_models.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare_models.BLOCKER_HANDOFF_BODY, outcomes[0].blocker)

    def test_heading_correction_is_blocked_as_structural(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "## Completion Contract",
                        "replacement_text": "## Different Section",
                        "reason": "restructure",
                    }
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(prepare_models.CORRECTION_BLOCKED, outcomes[0].status)
            self.assertEqual(prepare_models.BLOCKER_STRUCTURAL, outcomes[0].blocker)

    def test_multiple_corrections_to_one_file_compose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_prepare_plan(plan_dir)
            validation = self._corrections_validation(
                [
                    {
                        "expected_text": "Update the retry helper in `foo/bar.py`",
                        "replacement_text": "Update the retry helper in `foo/baz.py`",
                        "reason": "file moved",
                    },
                    {
                        "expected_text": "to use `load_config`.",
                        "replacement_text": "to use `load_settings`.",
                        "reason": "renamed",
                    },
                ]
            )

            outcomes = prepare_corrections.apply_corrections(plan_dir, validation)

            self.assertEqual(
                [prepare_models.CORRECTION_APPLIED, prepare_models.CORRECTION_APPLIED],
                [outcome.status for outcome in outcomes],
            )
            text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            self.assertIn(
                "Update the retry helper in `foo/baz.py` to use `load_settings`.",
                text,
            )
