"""Generated review replay and review verdict command tests."""

import argparse
import io
import json
import shlex
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import (
    contracts,
    events,
    plans_session_ids,
    review,
    review_session_body,
    review_session_contract,
    state,
)
from tests.support import (
    _contract_block,
    _structured_review_finding,
    write_completed_review_plan,
    write_plan,
)

ASL_TEST_LANE = "integration"


class GeneratedReviewReplayTests(unittest.TestCase):
    """Replay generated review sessions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"

    def _write_replay_plan(self) -> None:
        durable = json.dumps(
            {
                "type": "file_exists",
                "name": "durable-readme",
                "path": "README.md",
                "baseline_expectation": "fail",
                "review_replay": True,
            }
        )
        historical = json.dumps(
            {
                "type": "command",
                "name": "historical-head",
                "argv": ["git", "rev-parse", "HEAD"],
            }
        )
        dirty_tree = json.dumps(
            {
                "type": "command",
                "name": "session-local-clean-tree",
                "argv": ["git", "diff", "--quiet"],
            }
        )
        write_plan(
            self.plan_dir,
            ("pending", "pending"),
            contracts_by_id={
                "1": _contract_block(
                    ",".join((durable, historical, dirty_tree)), session="1"
                ),
                "2": _contract_block(durable, session="2"),
            },
        )

    def test_broad_review_replays_only_opted_in_durable_gates(self) -> None:
        self._write_replay_plan()

        first = review_session_contract._broad_review_verification_checks(self.plan_dir)
        review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )
        second = review_session_contract._broad_review_verification_checks(
            self.plan_dir
        )

        self.assertEqual(first, second)
        self.assertEqual(["broad:1:durable-readme"], [row["name"] for row in first])
        self.assertNotIn("baseline_expectation", first[0])
        self.assertNotIn("review_replay", first[0])
        serialized = json.dumps(first)
        self.assertNotIn("historical-head", serialized)
        self.assertNotIn("session-local-clean-tree", serialized)

    def test_pending_generated_review_refresh_preserves_lifetime_sections(
        self,
    ) -> None:
        self._write_replay_plan()
        session_id = plans_session_ids.parse_session_id("3")
        current = review_session_body.build_generated_review_session_body(
            session_id,
            review_harness="claude",
            plan_dir=self.plan_dir,
            delta={"mode": review_session_body.REVIEW_SCOPE_FULL, "prior_findings": []},
        )
        old = current.replace(
            review.review_session_version_marker(),
            review.review_session_version_marker(2),
            1,
        ).replace(
            "## Completion Contract",
            "## Ordered repository review base\n\n- preserved-base\n\n"
            "## Completion Contract",
            1,
        )
        old = old.replace(
            "- **Key decisions**: (none yet)",
            "- **Key decisions**: preserve this handoff",
            1,
        )
        old_scope = old.split("## Delta Review Scope", 1)[1].split(
            "## Completion Contract", 1
        )[0]
        old_handoff = old.split("## Handoff", 1)[1]

        refreshed = review_session_contract.refreshed_generated_review_contract(
            old,
            session_id,
            plan_dir=self.plan_dir,
        )

        self.assertIn(review.review_session_version_marker(), refreshed)
        self.assertEqual(
            old_scope,
            refreshed.split("## Delta Review Scope", 1)[1].split(
                "## Completion Contract", 1
            )[0],
        )
        self.assertIn("- preserved-base", refreshed)
        self.assertEqual(old_handoff, refreshed.split("## Handoff", 1)[1])
        contract = contracts.parse_completion_contract(refreshed, session_id="3")
        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
                "broad:1:durable-readme",
            ],
            [check.name for check in contract.verify.checks],
        )


class ReviewVerdictCommandTests(unittest.TestCase):
    """Drive the review verdict command."""

    def _artifact(self, **overrides: object) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": review.REVIEW_ARTIFACT_VERSION,
            "outcome": "findings",
            "findings": [_structured_review_finding()],
        }
        payload.update(overrides)
        return payload

    def _write_review(
        self, plan_dir: Path, body: str, artifact: dict | None = None
    ) -> None:
        (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(body, encoding="utf-8")
        if artifact is not None:
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                json.dumps(artifact), encoding="utf-8"
            )

    def _run_from_args(self, plan_dir: Path, state_dir: Path) -> tuple[int, dict]:
        args = argparse.Namespace(
            plan_dir=str(plan_dir), state_dir=str(state_dir), json=True
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = review.review_verdict_from_args(args)
        return code, json.loads(out.getvalue())

    def test_all_clear_payload_has_no_findings_and_no_next_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual("review-verdict", payload["command"])
            self.assertEqual("all-clear", payload["outcome"])
            self.assertIsNone(payload["findings"])
            self.assertFalse(payload["summary_truncated"])
            self.assertEqual([], payload["next_steps"])
            self.assertIn("review_resume", payload)

    def test_findings_payload_carries_full_untruncated_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            big_body = "detail " * 1000
            self._write_review(
                plan_dir,
                f"{big_body}\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual("findings", payload["outcome"])
            self.assertEqual("F1", payload["findings"][0]["id"])
            # The summary is never truncated by this surface, even well past
            # the helper's bounded excerpt limit.
            self.assertFalse(payload["summary_truncated"])
            self.assertEqual(payload["summary_total_chars"], len(payload["summary"]))
            self.assertGreater(payload["summary_total_chars"], 2000)
            self.assertTrue(payload["next_steps"])

    def test_missing_review_reports_stable_reason_and_generate_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MISSING, payload["reason"])
            self.assertEqual("", payload["summary"])
            self.assertEqual(1, len(payload["next_steps"]))
            self.assertIn("--review-harness", payload["next_steps"][0])

    def test_malformed_review_reports_stable_reason_and_correct_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, "no status line here\n")

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED, payload["reason"])
            self.assertEqual(1, len(payload["next_steps"]))
            self.assertIn(review.FINAL_REVIEW_FILENAME, payload["next_steps"][0])

    def test_malformed_artifact_reports_stable_reason_and_correct_step(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(
                plan_dir, "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n"
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
                "{not json", encoding="utf-8"
            )

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED_ARTIFACT, payload["reason"])
            self.assertIn(
                review.FINAL_REVIEW_ARTIFACT_FILENAME,
                payload["next_steps"][0],
            )

    def test_text_verdict_prints_every_finding_in_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(
                plan_dir,
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )
            args = argparse.Namespace(
                plan_dir=str(plan_dir), state_dir=str(state_dir), json=False
            )
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(0, review.review_verdict_from_args(args))

            text = out.getvalue()
            self.assertIn("Verdict: 1 Findings (1 high, 0 med, 0 low)\n", text)
            self.assertNotIn("Verdict: Status:", text)
            self.assertIn(
                "Findings:\n"
                "  F1 [high] Finding F1 title (repair_group group-1)\n"
                "    Evidence:\n"
                "      - agent_session_loop/runner.py (run_loop)\n"
                "    Explanation:\n"
                "      Explanation for F1.\n"
                "    Done criteria:\n"
                "      - The issue is fixed and covered by a test.\n"
                "    Verification checks:\n"
                "      - verify-F1 (command)\n",
                text,
            )

    def test_non_utf8_event_log_and_review_do_not_crash_the_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")
            plan_state = state.plan_state_dir(plan_dir, state_dir)
            plan_state.mkdir(parents=True)
            # A stray byte spoils only its own line of the event history.
            (plan_state / events.EVENTS_FILE_NAME).write_bytes(b"\xff\n")

            payload = review.review_verdict_payload(plan_dir, state_dir)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual("all-clear", payload["outcome"])

            # A review that is not UTF-8 reads as malformed, not as a crash.
            (plan_dir / review.FINAL_REVIEW_FILENAME).write_bytes(b"body \xff\n")
            payload = review.review_verdict_payload(plan_dir, state_dir)
            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MALFORMED, payload["reason"])

    def test_missing_review_step_keeps_metacharacter_plan_path_one_arg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "weird plan dir; echo $HOME"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertEqual(review.REVIEW_REASON_MISSING, payload["reason"])
            command = payload["next_steps"][0]
            # The whitespace/metacharacter plan path round-trips as exactly one
            # argument under shlex.split instead of splitting apart.
            self.assertIn(payload["plan_dir"], shlex.split(command))

    def test_findings_fallback_step_keeps_metacharacter_plan_path_one_arg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "weird plan dir; echo $HOME"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            # A plain findings review (no generated-review marker, no events)
            # yields no continuation guidance, so the findings-without-guidance
            # fallback command is emitted.
            self._write_review(
                plan_dir,
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )

            payload = review.review_verdict_payload(plan_dir, state_dir)

            self.assertEqual("findings", payload["outcome"])
            command = payload["next_steps"][0]
            self.assertIn("--review-harness", command)
            self.assertIn(payload["plan_dir"], shlex.split(command))

    def test_from_args_json_round_trips_and_rejects_malformed_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("complete",))
            self._write_review(plan_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")

            args = argparse.Namespace(
                plan_dir=str(plan_dir), state_dir=str(state_dir), json=True
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = review.review_verdict_from_args(args)
            self.assertEqual(0, code)
            payload = json.loads(out.getvalue())
            self.assertEqual("review-verdict", payload["command"])
            self.assertTrue(payload["ok"])

            missing = argparse.Namespace(
                plan_dir=str(Path(tmp) / "nope"),
                state_dir=str(state_dir),
                json=True,
            )
            err_out = io.StringIO()
            with redirect_stdout(err_out):
                code = review.review_verdict_from_args(missing)
            self.assertEqual(1, code)
            err_payload = json.loads(err_out.getvalue())
            self.assertFalse(err_payload["ok"])
            self.assertEqual("plan-not-found", err_payload["reason"])

            # A plan that exists but cannot be read keeps its own reason.
            (Path(tmp) / "empty").mkdir()
            malformed = argparse.Namespace(
                plan_dir=str(Path(tmp) / "empty"),
                state_dir=str(state_dir),
                json=True,
            )
            err_out = io.StringIO()
            with redirect_stdout(err_out):
                code = review.review_verdict_from_args(malformed)
            self.assertEqual(1, code)
            self.assertEqual("malformed-plan", json.loads(err_out.getvalue())["reason"])

    def test_from_args_exit_status_tracks_verdict_validity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            # Valid all-clear → ok, exit 0.
            clear_dir = Path(tmp) / "clear"
            write_plan(clear_dir, ("complete",))
            self._write_review(clear_dir, f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n")
            code, payload = self._run_from_args(clear_dir, state_dir)
            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-clear", payload["outcome"])

            # Valid findings → ok, exit 0.
            findings_dir = Path(tmp) / "findings"
            write_plan(findings_dir, ("complete",))
            self._write_review(
                findings_dir,
                "body\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
                artifact=self._artifact(),
            )
            code, payload = self._run_from_args(findings_dir, state_dir)
            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual("findings", payload["outcome"])

            # Missing review → non-ok, exit 1.
            missing_dir = Path(tmp) / "missing"
            write_plan(missing_dir, ("complete",))
            code, payload = self._run_from_args(missing_dir, state_dir)
            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MISSING, payload["reason"])

            # Malformed review → non-ok, exit 1.
            malformed_dir = Path(tmp) / "malformed"
            write_plan(malformed_dir, ("complete",))
            self._write_review(malformed_dir, "no status line here\n")
            code, payload = self._run_from_args(malformed_dir, state_dir)
            self.assertEqual(1, code)
            self.assertEqual(review.REVIEW_REASON_MALFORMED, payload["reason"])

            # Contradictory artifact → non-ok, exit 1.
            contra_dir = Path(tmp) / "contra"
            write_plan(contra_dir, ("complete",))
            self._write_review(
                contra_dir,
                f"body\n{review.REVIEW_STATUS_ALL_CLEAR}\n",
                artifact=self._artifact(),
            )
            code, payload = self._run_from_args(contra_dir, state_dir)
            self.assertEqual(1, code)
            self.assertEqual(
                review.REVIEW_REASON_CONTRADICTORY_ARTIFACT, payload["reason"]
            )

    def test_generated_v2_review_without_artifact_reports_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            # A completed generated v2 review with an all-clear Markdown
            # verdict but the structured artifact removed.
            write_completed_review_plan(plan_dir, [])
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            code, payload = self._run_from_args(plan_dir, state_dir)

            self.assertEqual(1, code)
            self.assertFalse(payload["ok"])
            self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, payload["reason"])
            self.assertIn(
                review.FINAL_REVIEW_ARTIFACT_FILENAME,
                payload["next_steps"][0],
            )

    def test_legacy_v1_markdown_only_review_stays_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_completed_review_plan(plan_dir, [])
            # Downgrade the generated review session to legacy v1 (base marker
            # only) and drop the structured artifact: v1 reviews are
            # Markdown-only, so the all-clear verdict must remain valid.
            session_two = plan_dir / "session-2.md"
            body = session_two.read_text(encoding="utf-8")
            body = body.replace(review.review_session_version_marker() + "\n", "")
            session_two.write_text(body, encoding="utf-8")
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            code, payload = self._run_from_args(plan_dir, state_dir)

            self.assertEqual(0, code)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual("all-clear", payload["outcome"])
            self.assertEqual([], payload["next_steps"])

    def test_pending_v2_review_still_requires_artifact_after_bump(
        self,
    ) -> None:
        # Enforcement is pinned to STRUCTURED_REVIEW_MIN_VERSION, so a review
        # session written before the generated-version bump keeps requiring
        # final-review.json instead of silently degrading to Markdown-only.
        self.assertLess(
            review.STRUCTURED_REVIEW_MIN_VERSION,
            review.GENERATED_REVIEW_SESSION_VERSION,
        )
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_completed_review_plan(plan_dir, [])
            session_two = plan_dir / "session-2.md"
            body = session_two.read_text(encoding="utf-8").replace(
                review.review_session_version_marker(),
                review.review_session_version_marker(
                    review.STRUCTURED_REVIEW_MIN_VERSION
                ),
            )
            session_two.write_text(body, encoding="utf-8")
            self.assertEqual(
                review.STRUCTURED_REVIEW_MIN_VERSION,
                review.review_session_marker_version(body),
            )
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()

            code, payload = self._run_from_args(plan_dir, state_dir)

            self.assertEqual(1, code)
            self.assertEqual(review.REVIEW_REASON_MISSING_ARTIFACT, payload["reason"])


if __name__ == "__main__":
    unittest.main()
