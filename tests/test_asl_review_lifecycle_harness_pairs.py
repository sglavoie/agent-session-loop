"""Cross-harness review runs and persisted review verdicts."""

from __future__ import annotations

import json
import subprocess

from agent_session_loop import (
    plans_files,
    plans_payloads,
    review_session_contract,
    review_transitions,
)
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase
from tests.support import (
    _structured_review_finding,
)

ASL_TEST_LANE = "integration"


class RunReviewHarnessPairTests(RunReviewHarnessLifecycleBase):
    """Cross-harness review runs and persisted review verdicts."""

    def test_fresh_run_review_writes_final_review_via_fake_harness(self) -> None:
        # Exercise the new fake-harness review mode end-to-end: the implementation
        # turn just marks its session complete, but the review turn must actually
        # write `final-review.md` so the generated contract passes. We do not
        # pre-stamp the file -- the harness fixture has to produce it.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("claude", payload["review_harness"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        review_path = review_session_contract.final_review_output_path(
            plans_files.canonicalize_path(self.plan_dir)
        )
        self.assertTrue(review_path.is_file())
        self.assertEqual(
            self._valid_final_review_body(),
            review_path.read_text(encoding="utf-8"),
        )

    def test_copilot_implementation_run_completes_claude_review(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            harness="copilot",
            review_harness="claude",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("claude", payload["review_harness"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("2", payload["review_session_id"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        turns = payload["turns"]
        self.assertEqual(["copilot", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual(
            [
                plans_payloads.SESSION_KIND_IMPLEMENTATION,
                plans_payloads.SESSION_KIND_REVIEW,
            ],
            [turn["session_kind"] for turn in turns],
        )

    def test_codex_implementation_run_completes_copilot_review(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            harness="codex",
            review_harness="copilot",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual("copilot", payload["review_harness"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("2", payload["review_session_id"])
        self.assertEqual(
            {
                "status_line": "Status: All clear, no findings",
                "outcome": "all-clear",
                "total_findings": 0,
                "findings_by_severity": {"high": 0, "med": 0, "low": 0},
            },
            payload["review_verdict"],
        )
        turns = payload["turns"]
        self.assertEqual(["codex", "copilot"], [turn["harness"] for turn in turns])
        self.assertEqual(
            [
                plans_payloads.SESSION_KIND_IMPLEMENTATION,
                plans_payloads.SESSION_KIND_REVIEW,
            ],
            [turn["session_kind"] for turn in turns],
        )

    def test_review_verdict_persisted_for_counted_findings(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        for argv in (["git", "add", "README.md"], ["git", "commit", "-m", "seed"]):
            subprocess.run(
                argv,
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
        _commit_log, commit_env = self._commit_log_env()
        review_body = (
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Medium issue in tests/test_agent_session_loop.py.\n"
            "- Low issue in docs/agent-session-loop.md.\n"
            "Status: 5 Findings (2 high, 2 med, 1 low)\n"
        )
        severities = ("high", "high", "med", "med", "low")
        review_json = json.dumps(
            {
                "version": 2,
                "outcome": "findings",
                "findings": [
                    _structured_review_finding(
                        finding_id=f"F{index}", severity=severity
                    )
                    for index, severity in enumerate(severities, start=1)
                ],
            }
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_BODY": review_body,
                "FAKE_CODEX_REVIEW_JSON": review_json,
            },
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            {
                "status_line": "Status: 5 Findings (2 high, 2 med, 1 low)",
                "outcome": "findings",
                "total_findings": 5,
                "findings_by_severity": {"high": 2, "med": 2, "low": 1},
            },
            payload["review_verdict"],
        )
        # The findings the run could not close keep it from claiming success.
        self.assertFalse(payload["ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, payload["reason"]
        )
        self.assertEqual(5, payload["action_required"]["unresolved_findings"])
