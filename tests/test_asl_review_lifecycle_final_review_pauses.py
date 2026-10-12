"""Pauses caused by a failed review turn or an invalid final review file."""

from __future__ import annotations

import json

from agent_session_loop import (
    plans,
    review,
    review_session_contract,
)
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase


class RunReviewFinalReviewPauseTests(RunReviewHarnessLifecycleBase):
    """Pauses caused by a failed review turn or an invalid final review file."""

    def test_review_nonzero_exit_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(mode="failure", review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(plans.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"])

    def test_review_timeout_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="timeout",
            review_harness="claude",
            timeout=1,
            fake_codex_sleep="5",
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])

    def test_review_missing_final_review_file_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        # Intentionally omit _precreate_final_review() so the contract fails.

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # The runner should have reverted the review session back to pending.
        review_path = next(
            path
            for path in self.plan_dir.glob("session-*.md")
            if review_session_contract.is_generated_review_session_file(path)
        )
        review_text = review_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_review_empty_final_review_file_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        # Whitespace-only final-review.md must fail the contract.
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "   \n\t\n", encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])

    def test_review_low_effort_final_review_file_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "Looks good overall.\nNo issues found.\n", encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])

    def test_legacy_review_contract_still_requires_runner_verdict(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_id = plans.next_top_level_session_id(self.plan_dir)
        legacy_contract = {
            "session": review_id.value,
            "checks": [
                {
                    "type": "command",
                    "name": "legacy-final-review-output",
                    "argv": [
                        "python3",
                        "-c",
                        (
                            "import pathlib, sys; "
                            "text = pathlib.Path('final-review.md').read_text("
                            "encoding='utf-8'); "
                            "sys.exit(0 if text.strip() else 1)"
                        ),
                    ],
                    "cwd": str(plans.canonicalize_path(self.plan_dir)),
                    "timeout_seconds": 60,
                }
            ],
        }
        (self.plan_dir / review_id.filename).write_text(
            "\n".join(
                [
                    f"# {review_id.display_label}: Final Review",
                    "",
                    review.REVIEW_SESSION_MARKER,
                    "",
                    "## Instructions",
                    "",
                    "- Perform a critical review and write final-review.md.",
                    "- Do not modify repository files.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    "- final-review.md exists and contains review findings.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps(legacy_contract, indent=2),
                    "```",
                    "",
                    "## Handoff",
                    "",
                    "- **Status**: pending",
                    "- **Files modified**: (none yet)",
                    "- **Key decisions**: (none yet)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
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

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload["review_verdict"])
        review_text = (self.plan_dir / review_id.filename).read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_text_output_prints_review_path(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, _ = self._run(review_harness="claude", json_mode=False)

        self.assertEqual(0, completed.returncode, completed.stderr)
        expected_path = review_session_contract.final_review_output_path(
            plans.canonicalize_path(self.plan_dir)
        )
        self.assertIn(f"Review: {expected_path}", completed.stdout)
        self.assertEqual(
            "Status: All clear, no findings",
            completed.stdout.splitlines()[-1],
        )
        self.assertIn("session 1/1 started", completed.stderr)
        self.assertIn("review session started (2/2)", completed.stderr)
        self.assertNotIn("session 2/2 started", completed.stderr)
