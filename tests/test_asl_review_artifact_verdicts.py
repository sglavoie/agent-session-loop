"""Structured review verdict loading and generated review session body tests."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    contracts,
    handoff_guards,
    plans_session_ids,
    plans_snapshot,
    review,
    review_continuation,
    review_delta,
    review_session_body,
)
from tests.support import (
    _load_script_module,
    _structured_review_finding,
    write_completed_review_plan,
)

ASL_TEST_LANE = "integration"


class ReviewArtifactVerdictTests(unittest.TestCase):
    """Merge artifacts into verdicts and render the generated review body."""

    def _artifact(self, **overrides: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": review.REVIEW_ARTIFACT_VERSION,
            "outcome": "findings",
            "findings": [_structured_review_finding()],
        }
        payload.update(overrides)
        return payload

    def test_load_review_verdict_merges_valid_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(self._artifact()), encoding="utf-8"
            )

            verdict = review.load_review_verdict(plan_dir, require_artifact=True)

            self.assertTrue(verdict["ok"], verdict)
            self.assertEqual(
                review.REVIEW_ARTIFACT_VERSION, verdict["artifact_version"]
            )
            self.assertEqual("F1", verdict["findings"][0]["id"])
            self.assertEqual(
                str(plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME),
                verdict["review_artifact_path"],
            )

    def test_load_review_verdict_degrades_when_registry_is_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "manifest.md").write_text(
                "# Manifest\n\n## Repositories\n\n| Alias | Path |\n"
                "| --- | --- |\n| Core | core |\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(self._artifact()), encoding="utf-8"
            )

            verdict = review.load_review_verdict(plan_dir)

            self.assertFalse(verdict["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED_ARTIFACT, verdict["reason"])
            self.assertIn("repository registry", verdict["error"])
            self.assertEqual([], review_delta._prior_review_findings(plan_dir))

    def test_runner_and_autopilot_report_same_verdict_payload(self) -> None:
        autopilot = _load_script_module(
            "asl_autopilot_review_parity",
            "skills",
            "asl",
            "scripts",
            "asl_autopilot.py",
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(self._artifact()), encoding="utf-8"
            )

            runner_verdict = handoff_guards._read_review_verdict(
                plan_dir, require_artifact=True
            )
            autopilot_verdict = autopilot._read_review_verdict(
                {"plan_dir": str(plan_dir)}
            )

            summary = autopilot_verdict.pop("summary")
            self.assertIn("Status: 1 Findings", summary)
            # The helper additionally flags truncation and points at the full
            # review; drop those before comparing the shared verdict payload.
            self.assertFalse(autopilot_verdict.pop("summary_truncated"))
            self.assertIn("asl review-verdict", autopilot_verdict.pop("full_review"))
            self.assertEqual(runner_verdict, autopilot_verdict)
            self.assertIs(autopilot.parse_review_verdict, review.parse_review_verdict)

    def test_eventless_disposition_enforces_artifact_for_v2_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_completed_review_plan(plan_dir, [])
            session_two = plan_dir / "session-2.md"
            session_two.write_text(
                session_two.read_text(encoding="utf-8").replace(
                    review.review_session_version_marker(),
                    review.review_session_version_marker(
                        review.STRUCTURED_REVIEW_MIN_VERSION
                    ),
                ),
                encoding="utf-8",
            )
            snapshot = plans_snapshot.inspect_plan(plan_dir)
            source = next(session for session in snapshot.sessions if session.id == "2")

            self.assertEqual(
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR,
                review_continuation._eventless_review_disposition(
                    snapshot.plan_dir, source
                ),
            )

            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            self.assertIsNone(
                review_continuation._eventless_review_disposition(
                    snapshot.plan_dir, source
                )
            )

    def test_generated_review_session_body_renders_delta_scope(self) -> None:
        finding = {
            "id": "F1",
            "severity": "high",
            "title": "Guard is bypassed",
            "evidence": [
                {"path": "agent_session_loop/runner.py", "symbol": "guard"},
                {"path": "docs/agent-session-loop.md"},
            ],
            "done_criteria": ["The guard rejects the bypass."],
        }
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans_session_ids.SessionId((7,))

            full = review_session_body.build_generated_review_session_body(
                session_id, review_harness="claude", plan_dir=plan_dir
            )
            delta = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
                delta={
                    "mode": review_session_body.REVIEW_SCOPE_DELTA,
                    "base_sha": "f" * 40,
                    "changed_files": ["agent_session_loop/runner.py"],
                    "prior_findings": [finding],
                },
            )
            degraded = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
                delta={
                    "mode": review_session_body.REVIEW_SCOPE_FULL,
                    "prior_findings": [finding],
                },
            )

            # The first review of a run renders no scope section at all.
            self.assertNotIn("## Delta Review Scope", full)

            self.assertIn("## Delta Review Scope", delta)
            flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
            self.assertIn(f"git diff {flags} {'f' * 40}", delta)
            self.assertIn(f"git diff --cached {flags} {'f' * 40}", delta)
            self.assertIn("`agent_session_loop/runner.py`", delta)
            self.assertIn("`F1` (high): Guard is bypassed", delta)
            self.assertIn("Done when: The guard rejects the bypass.", delta)

            self.assertIn("## Delta Review Scope", degraded)
            self.assertNotIn("git diff", degraded)
            self.assertIn("this review stays full scope", degraded)
            self.assertIn("this scope is not narrowed", degraded)
            self.assertIn("`F1` (high): Guard is bypassed", degraded)

            # Scope narrows reading only: grammar, artifacts, and the
            # completion contract stay identical across all three bodies.
            for body in (full, delta, degraded):
                contract = contracts.parse_completion_contract(
                    body, session_id=session_id
                )
                self.assertEqual(
                    [
                        "final-review-output-evidence-based",
                        "final-review-artifact-consistent",
                    ],
                    [check.name for check in contract.verify.checks],
                )
                self.assertEqual(
                    review.GENERATED_REVIEW_SESSION_VERSION,
                    review.review_session_marker_version(body),
                )
                self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, body)
            self.assertEqual(
                contracts.parse_completion_contract(
                    full, session_id=session_id
                ).done_when,
                contracts.parse_completion_contract(
                    delta, session_id=session_id
                ).done_when,
            )

    def test_generated_review_session_body_is_versioned_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans_session_ids.SessionId((7,))

            body = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
            )

            self.assertIn(review.REVIEW_SESSION_MARKER, body)
            self.assertIn(review.review_session_version_marker(), body)
            self.assertIn(review.FINAL_REVIEW_ARTIFACT_FILENAME, body)
            self.assertEqual(
                review.GENERATED_REVIEW_SESSION_VERSION,
                review.review_session_marker_version(body),
            )
            contract = contracts.parse_completion_contract(body, session_id=session_id)
            self.assertEqual(
                [
                    "final-review-output-evidence-based",
                    "final-review-artifact-consistent",
                ],
                [check.name for check in contract.verify.checks],
            )

    def test_generated_review_session_body_states_presence_only_semantics(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = review_session_body.build_generated_review_session_body(
                plans_session_ids.SessionId((7,)),
                review_harness="claude",
                plan_dir=Path(tmp),
            )

            self.assertIn(
                "A verification check accepts only the keys its own type "
                "documents; any other key is rejected.",
                body,
            )
            self.assertIn(
                "`regex_in_file` asserts the pattern is PRESENT, and neither "
                "has absence semantics",
                body,
            )
            # The gate the review runs against itself says the same thing.
            contract = contracts.parse_completion_contract(
                body, session_id=plans_session_ids.SessionId((7,))
            )
            self.assertIn(
                contracts.CHECK_PRESENCE_SEMANTICS["regex_in_file"],
                contract.verify.checks[1].argv[2],
            )

    def test_generated_review_session_body_requires_falsifiable_checks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = review_session_body.build_generated_review_session_body(
                plans_session_ids.SessionId((7,)),
                review_harness="claude",
                plan_dir=Path(tmp),
            )

            # The repair session is handed these checks verbatim, so a gate a
            # record headed "NOT PERFORMED" can satisfy closes the finding it
            # was written to prove (ASI-136).
            self.assertIn(
                "Every check must be able to fail while the finding is still open.",
                body,
            )
            self.assertIn(
                "Gate on the state the `done_criteria` require, not on text "
                "about that state",
                body,
            )

    def test_generated_artifact_contract_check_gates_consistency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            session_id = plans_session_ids.SessionId((7,))
            body = review_session_body.build_generated_review_session_body(
                session_id,
                review_harness="claude",
                plan_dir=plan_dir,
            )
            contract = contracts.parse_completion_contract(body, session_id=session_id)
            artifact_check = contract.verify.checks[1]
            md_path = plan_dir / review.FINAL_REVIEW_FILENAME
            artifact_path = plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME
            md_path.write_text(
                "## Findings\n"
                "- One issue in agent_session_loop/runner.py.\n"
                "Status: 1 Findings (1 high, 0 med, 0 low)\n",
                encoding="utf-8",
            )

            def run_check() -> int:
                return subprocess.run(
                    list(artifact_check.argv),
                    cwd=plan_dir,
                    capture_output=True,
                    check=False,
                ).returncode

            # Missing artifact fails.
            self.assertEqual(1, run_check())
            # Consistent artifact passes.
            artifact_path.write_text(json.dumps(self._artifact()), encoding="utf-8")
            self.assertEqual(0, run_check())
            # Contradictory severity totals fail.
            artifact_path.write_text(
                json.dumps(
                    self._artifact(
                        findings=[_structured_review_finding(severity="low")]
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(1, run_check())
            # Incomplete findings fail.
            incomplete = _structured_review_finding()
            del incomplete["repair_group"]
            artifact_path.write_text(
                json.dumps(self._artifact(findings=[incomplete])),
                encoding="utf-8",
            )
            self.assertEqual(1, run_check())


if __name__ == "__main__":
    unittest.main()
