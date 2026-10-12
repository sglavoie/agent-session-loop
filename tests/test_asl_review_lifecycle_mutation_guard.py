"""Review mutation guard exclusions and ignored-file handling in full runs."""

from __future__ import annotations

from agent_session_loop import (
    events,
    plans,
    review_continuation,
    review_delta,
    review_guard,
    review_session_body,
)
from tests.review_lifecycle_mutating_claude import MutatingFakeClaudeMixin
from tests.support import (
    _contract_block,
    _structured_review_finding,
    git_stdout,
    write_completed_review_plan,
)


class RunReviewMutationGuardTests(MutatingFakeClaudeMixin):
    """Review mutation guard exclusions and ignored-file handling in full runs."""

    def test_resumed_delta_review_is_rescoped_for_a_late_untracked_file(
        self,
    ) -> None:
        """A crash-resumed delta review must inspect files added since."""
        finding = _structured_review_finding()
        write_completed_review_plan(
            self.plan_dir,
            [finding],
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}'
                )
            },
        )
        base = git_stdout(self.repo_root, "rev-parse", "HEAD")
        events.append_event(
            self._events_path(),
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "cycle": 1,
                    "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "review_session_id": "2",
                    "repair_session_ids": ["2.1"],
                    "fingerprints": [f"finding:{finding['id']}"],
                    "repo_head": base,
                    "worktree_dirty": False,
                    "dirty_paths": [],
                },
            ),
        )
        delta, fallback_reason = review_delta._compute_review_delta_context(
            event_path=self._events_path(),
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )
        self.assertIsNone(fallback_reason)
        # The prior run wrote its delta review against a clean tree, then died
        # before executing it.
        review_session, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude", delta=delta
        )
        self.assertTrue(created)
        self.assertIn(
            f"No files changed since `{base}`",
            review_session.path.read_text(encoding="utf-8"),
        )
        late = self.repo_root / "src" / "late.py"
        late.parent.mkdir(parents=True, exist_ok=True)
        late.write_text("late\n", encoding="utf-8")
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(review_session.session_id.value, payload["review_session_id"])
        self.assertEqual(1, len(payload["turns"]))
        resumed_body = review_session.path.read_text(encoding="utf-8")
        self.assertIn("  - `src/late.py`", resumed_body)
        flags = " ".join(review_session_body._DELTA_DIFF_FLAGS)
        self.assertIn(f"git diff {flags} {base}", resumed_body)
        scope_events = [
            entry["payload"]
            for entry in self._read_events()
            if entry["name"] == events.REVIEW_SCOPE_EVENT
        ]
        self.assertEqual(1, len(scope_events))
        self.assertEqual(
            review_session_body.REVIEW_SCOPE_DELTA, scope_events[0]["scope"]
        )
        self.assertEqual(base, scope_events[0]["base_sha"])
        self.assertIsNone(scope_events[0]["fallback_reason"])
        self.assertIs(True, scope_events[0]["refreshed"])

    def test_review_mutated_worktree_pauses_run(self) -> None:
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_session = self._write_pending_review_session()
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # Mutation should be visible on disk after the pause.
        self.assertEqual(
            "mutated by review\n",
            (self.repo_root / "README.md").read_text(encoding="utf-8"),
        )
        self.assertEqual("pending", plans.read_session_status(review_session).status)
        self.assertEqual(1, payload["final_snapshot"]["counts"]["pending"])
        guard = payload["turns"][0].get("review_mutation_guard")
        self.assertIsNotNone(guard)
        self.assertFalse(guard["passed"])
        self.assertEqual("review-mutated-worktree", guard["reason"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual([], guard["excluded"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])
        self.assertFalse(guard["changed_paths"]["head_changed"])
        self.assertEqual(
            guard["changed_paths"], payload["pause_details"]["changed_paths"]
        )
        self.assertEqual([], payload["pause_details"]["excluded"])
        self.assertIn("README.md", payload["pause_message"])

    def test_review_guard_exclude_flag_allows_excluded_mutation(self) -> None:
        self._write_plan(("complete",), readiness=None)
        review_session = self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(
            review_harness="claude", review_guard_exclude=("README.md",)
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual("complete", plans.read_session_status(review_session).status)
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(["README.md"], guard["excluded"])

    def test_review_guard_exclude_env_var_allows_excluded_mutation(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(
            review_harness="claude",
            extra_env={review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR: "README.md"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            ["README.md"], payload["turns"][0]["review_mutation_guard"]["excluded"]
        )

    def test_review_guard_exclude_merges_env_var_and_flag(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        # The tracked README is excluded by the flag; the untracked file under
        # notes/ only by the env var, so the run completes only if both merge.
        self._install_mutating_fake_claude(("README.md", "notes/scratch.md"))

        completed, payload = self._run(
            review_harness="claude",
            review_guard_exclude=("README.md",),
            extra_env={review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR: "notes"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(
            ["README.md", "notes"],
            payload["turns"][0]["review_mutation_guard"]["excluded"],
        )

    def test_review_guard_exclude_still_trips_on_other_paths(self) -> None:
        self._write_plan(("complete",), readiness=None)
        self._write_pending_review_session()
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        self._install_mutating_fake_claude()

        completed, payload = self._run(
            review_harness="claude", review_guard_exclude=("notes",)
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertEqual(["notes"], guard["excluded"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])

    def test_review_turn_writing_a_gitignored_cache_still_passes(self) -> None:
        self._write_pycache_review_plan()

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertTrue(
            (self.repo_root / "agent_session_loop/__pycache__/x.pyc").exists()
        )
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["passed"])
        self.assertFalse(guard["include_ignored"])

    def test_include_ignored_flag_trips_the_guard_on_a_cache_write(self) -> None:
        self._write_pycache_review_plan()

        completed, payload = self._run(
            review_harness="claude", review_guard_include_ignored=True
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["include_ignored"])
        self.assertEqual(
            ["agent_session_loop/__pycache__/x.pyc"],
            guard["changed_paths"]["untracked_added"],
        )
