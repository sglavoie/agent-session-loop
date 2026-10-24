"""Review session created and scope events, regating, and reuse."""

from __future__ import annotations

from agent_session_loop import (
    events,
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    review_session_body,
    review_session_contract,
)
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase

ASL_TEST_LANE = "integration"


class RunReviewSessionEventTests(RunReviewHarnessLifecycleBase):
    """Review session created and scope events, regating, and reuse."""

    def test_review_session_created_event_is_emitted(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        review_session_id = payload["review_session_id"]
        events_log = self._read_events()
        created_events = [
            entry
            for entry in events_log
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual(1, len(created_events))
        created_payload = created_events[0]["payload"]
        self.assertEqual(review_session_id, created_payload["session_id"])
        self.assertEqual("claude", created_payload["review_harness"])
        canonical_plan_dir = plans_files.canonicalize_path(self.plan_dir)
        self.assertEqual(
            str(canonical_plan_dir / f"session-{review_session_id}.md"),
            created_payload["review_session_path"],
        )
        self.assertEqual(
            str(review_session_contract.final_review_output_path(canonical_plan_dir)),
            created_payload["review_output_path"],
        )

    def test_review_scope_event_records_the_full_scope_fallback(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        events_log = self._read_events()
        scope_events = [
            entry
            for entry in events_log
            if entry.get("name") == events.REVIEW_SCOPE_EVENT
        ]
        self.assertEqual(1, len(scope_events))
        scope_payload = scope_events[0]["payload"]
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, scope_payload["scope"])
        self.assertIsNone(scope_payload["base_sha"])
        self.assertEqual("no-prior-repair-cycle", scope_payload["fallback_reason"])
        created_payload = next(
            entry["payload"]
            for entry in events_log
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        )
        self.assertEqual(
            review_session_body.REVIEW_SCOPE_FULL, created_payload["review_scope"]
        )
        self.assertIsNone(created_payload["review_base_sha"])
        review_body = (
            self.plan_dir / f"session-{payload['review_session_id']}.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("## Delta Review Scope", review_body)

    def test_review_session_created_event_not_emitted_when_reused(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        events_log = self._read_events()
        created_events = [
            entry
            for entry in events_log
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual([], created_events)

    def test_appending_the_review_session_regates_the_grown_plan(self) -> None:
        # The audit's complaint: a reused readiness report saying every session
        # was complete and no turns were required authorized a turn on a session
        # that report never saw. The re-gate is what makes the recorded
        # authority describe the plan that actually ran.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        gate = payload["readiness_gate_reopened"]
        self.assertEqual(events.REVIEW_SESSION_CREATED_EVENT, gate["trigger"])
        self.assertTrue(gate["ok"])
        # The appended review session is the one pending session the startup
        # gate could not have counted.
        self.assertEqual(1, gate["pending_sessions"])
        self.assertEqual(2, gate["total_sessions"])
        # No repair children were materialized, so naming any here would be a
        # different growth than the one that happened.
        self.assertEqual([], gate["repair_session_ids"])

    def test_a_reused_review_session_is_not_regated(self) -> None:
        # It was authorized when it was created; re-gating it would restate an
        # authority that already exists and blur which growth was licensed.
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        self._write_pending_review_session()
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._precreate_final_review()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertNotIn("readiness_gate_reopened", payload)
