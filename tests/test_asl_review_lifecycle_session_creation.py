"""Review session creation, role models, and existing review sessions."""

from __future__ import annotations

import json

from agent_session_loop import (
    config,
    plans,
    review_session_contract,
)
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase


class RunReviewSessionCreationTests(RunReviewHarnessLifecycleBase):
    """Review session creation, models, scope events, and reuse."""

    def test_fresh_run_appends_and_completes_review_session(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_requested"])
        self.assertEqual("claude", payload["review_harness"])
        self.assertTrue(payload["review_completed"])
        self.assertIsNone(payload.get("review_skipped_reason"))
        review_session_id = payload["review_session_id"]
        self.assertIsNotNone(review_session_id)
        # Review session id must be a fresh top-level id beyond impl sessions.
        self.assertEqual("2", review_session_id)
        review_path = self.plan_dir / f"session-{review_session_id}.md"
        self.assertTrue(review_path.is_file())
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(review_path)
        )
        # Both impl and review turns ran with the right kind tag.
        turns = payload["turns"]
        self.assertEqual(2, len(turns))
        self.assertEqual(plans.SESSION_KIND_IMPLEMENTATION, turns[0]["session_kind"])
        self.assertEqual("codex", turns[0]["harness"])
        self.assertEqual(plans.SESSION_KIND_REVIEW, turns[1]["session_kind"])
        self.assertEqual("claude", turns[1]["harness"])
        self.assertEqual(1, turns[0]["total_sessions"])
        self.assertEqual(2, turns[1]["total_sessions"])

    def test_role_specific_models_reach_only_their_matching_turns(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(
            review_harness="claude",
            harness_model="gpt-5",
            review_harness_model="claude-opus-4",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        turns = payload["turns"]
        self.assertEqual("gpt-5", turns[0]["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turns[0]["model_source"])
        self.assertEqual("claude-opus-4", turns[1]["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turns[1]["model_source"])
        self.assertEqual(
            "gpt-5", turns[0]["argv"][turns[0]["argv"].index("--model") + 1]
        )
        self.assertEqual(
            "claude-opus-4",
            turns[1]["argv"][turns[1]["argv"].index("--model") + 1],
        )

        turn_started = [
            event["payload"]
            for event in self._read_events()
            if event["name"] == "turn_started"
        ]
        self.assertEqual(
            ["gpt-5", "claude-opus-4"],
            [event["model"] for event in turn_started],
        )

    def test_omitted_role_specific_models_leave_native_defaults_unforwarded(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        turns = payload["turns"]
        self.assertNotIn("--model", turns[0]["argv"])
        self.assertNotIn("--model", turns[1]["argv"])
        self.assertNotEqual(config.MODEL_SOURCE_FLAG, turns[0]["model_source"])
        self.assertNotEqual(config.MODEL_SOURCE_FLAG, turns[1]["model_source"])

    def test_dry_run_existing_review_uses_review_harness_model(self) -> None:
        self._write_plan(("complete",), readiness="complete")
        self._write_pending_review_session()

        completed, payload = self._run(
            review_harness="claude",
            harness_model="gpt-5",
            review_harness_model="claude-opus-4",
            dry_run=True,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        planned = payload["planned"]
        invocation = planned["invocation"]
        self.assertEqual("claude", invocation["name"])
        self.assertEqual("claude-opus-4", planned["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, planned["model_source"])
        self.assertEqual(
            "claude-opus-4",
            invocation["argv"][invocation["argv"].index("--model") + 1],
        )

    def test_review_runs_when_plan_starts_already_complete(self) -> None:
        # Author the plan as complete from the start, with readiness=complete
        # and a matching SHA, so the normal loop-ready gate would refuse it.
        self._write_plan(("complete",), readiness="complete")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("2", payload["review_session_id"])
        # Only the review turn should have executed.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(plans.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"])
        self.assertEqual("claude", payload["turns"][0]["harness"])
        # Initial readiness was 'complete'; after appending a review session
        # the readiness was switched to 'running' and then finalized to
        # 'complete' once all sessions (impl + review) ended complete.
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])

    def test_completed_review_handoff_plan_artifacts_are_normalized(self) -> None:
        self._write_plan(("complete",), readiness=None)
        review_path = self._write_pending_review_session().path
        review_path.write_text(
            review_path.read_text(encoding="utf-8")
            .replace("- **Status**: pending", "- **Status**: complete")
            .replace(
                "- **Files modified**: (none yet)",
                "- **Files modified**: final-review.md, final-review.json",
            ),
            encoding="utf-8",
        )
        self._precreate_final_review()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        self.assertEqual([str(review_path)], payload["normalized_review_handoffs"])
        self.assertIn(
            "- **Files modified**: (none)",
            review_path.read_text(encoding="utf-8"),
        )

    def test_fresh_review_handoff_plan_artifacts_are_normalized(self) -> None:
        self._write_plan(("complete",), readiness="complete")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {"2": ["final-review.md", "final-review.json"]}
                )
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        assert payload is not None
        review_path = self.plan_dir / "session-2.md"
        self.assertEqual(
            [str(plans.canonicalize_path(review_path))],
            payload["normalized_review_handoffs"],
        )
        self.assertIn(
            "- **Files modified**: (none)",
            review_path.read_text(encoding="utf-8"),
        )

    def test_complete_plan_review_harness_repairs_progress_journal(
        self,
    ) -> None:
        # Complete plan with `readiness=complete`; delete the journal so the
        # auto-repair must rebuild it. The repair must rebuild progress.md
        # without entering the manifest-readiness phase (no SHA drift) and
        # without downgrading the `complete` readiness marker.
        self._write_plan(("complete",), readiness="complete")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()
        progress_file = plans.progress_path(self.plan_dir)
        self.assertTrue(progress_file.exists())
        progress_file.unlink()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])
        self.assertTrue(progress_file.exists())

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["progress-journal-missing"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        # `ok` is the strict readiness-gate state (`loop-ready`); the
        # complete plan stays at `complete`, so the strict gate stays
        # False even though the run proceeds via the review-only
        # complete-start exception.
        self.assertFalse(repair_payload["ok"])
        # Only the progress repair fired; manifest-readiness was not needed.
        categories = [repair["category"] for repair in repair_payload["auto_repairs"]]
        self.assertEqual(["progress-journal-missing"], categories)
        # Final on-disk readiness is `complete` (the lifecycle finishes
        # writing it after the review session completes).
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", metadata.state)

    def test_complete_plan_review_harness_repairs_sha_keeps_complete_readiness(
        self,
    ) -> None:
        # Complete plan with `readiness=complete` but a stale SHA. The
        # auto-repair must rewrite the manifest readiness metadata with the
        # current SHA *and* keep the state at `complete`, not downgrade to
        # `loop-ready` just to satisfy the readiness gate.
        self._write_plan(("complete",), readiness="complete", sha_override="0" * 64)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual("complete", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])

        repair_payload = payload["readiness_gate_repair"]
        self.assertEqual(["manifest-readiness"], repair_payload["attempted"])
        self.assertEqual([], repair_payload["errors"])
        # `ok` is the strict readiness-gate state (`loop-ready`); the
        # complete plan stays at `complete`, so the strict gate stays
        # False even though the run proceeds via the review-only
        # complete-start exception.
        self.assertFalse(repair_payload["ok"])
        self.assertEqual(1, len(repair_payload["auto_repairs"]))
        repair = repair_payload["auto_repairs"][0]
        self.assertEqual("manifest-readiness", repair["category"])
        # The fix: target_state is `complete`, not `loop-ready`. This is the
        # whole point of the review-only complete start branch in
        # `readiness_gate._repair_readiness_gate`.
        self.assertEqual("complete", repair["target_state"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", metadata.state)
        self.assertNotEqual("0" * 64, metadata.readiness_sha256)

    def test_resumes_existing_pending_review_session(self) -> None:
        # Author the plan as fully complete + readiness=loop-ready first,
        # then drop in a pre-existing pending review session so we end up
        # with the impl session complete and the review session pending.
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        # The readiness SHA must reflect the plan including the review session.
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # The pre-existing review session must be reused, not replaced.
        self.assertEqual(review_session.session_id.value, payload["review_session_id"])
        # Only the existing review session needed to run.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(plans.SESSION_KIND_REVIEW, payload["turns"][0]["session_kind"])

    def test_pending_review_session_without_review_harness_pauses(self) -> None:
        self._write_plan(("complete",), readiness=None)
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

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("review-harness-required", payload["pause_reason"])
        # No turns should have run -- the runner refused before launching.
        self.assertEqual([], payload["turns"])

    def test_max_sessions_skips_review_and_records_skip_reason(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            review_harness="claude",
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual("max-sessions", payload["review_skipped_reason"])
        # Only one implementation turn ran; the loop stopped before any
        # review session could be appended.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            plans.SESSION_KIND_IMPLEMENTATION,
            payload["turns"][0]["session_kind"],
        )
        # No review session file should have been created.
        review_candidates = [
            path
            for path in self.plan_dir.glob("session-*.md")
            if review_session_contract.is_generated_review_session_file(path)
        ]
        self.assertEqual([], review_candidates)

    def test_review_that_ran_is_attested_even_when_the_plan_breaks(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_BREAK_PLAN": "1"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        # The review turn ran to completion; only the commit that records it
        # was never reached.
        self.assertEqual("2", payload["review_session_executed"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload["review_skipped_reason"])
        verdict = payload["review_verdict"]
        self.assertEqual("all-clear", verdict["outcome"])
        self.assertEqual(0, verdict["total_findings"])
        self.assertIsNotNone(payload["review_output_sha256"])
        self.assertIsNotNone(payload["review_artifact_sha256"])

    def test_review_that_never_ran_is_still_reported_as_skipped(self) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(review_harness="claude", max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIsNone(payload["review_session_executed"])
        self.assertEqual("max-sessions", payload["review_skipped_reason"])
        self.assertIsNone(payload["review_verdict"])
