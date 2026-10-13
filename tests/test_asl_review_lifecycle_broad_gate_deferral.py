"""Deferred broad-gate failures and generated repair sessions."""

from __future__ import annotations

import json

from agent_session_loop import (
    events,
    pause_policy,
    plans_handoff,
    plans_models,
    plans_payloads,
    plans_session_ids,
    review,
    review_transitions,
    run_selection,
    verification_transitions,
)
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase
from tests.support import (
    _structured_review_finding,
    git_stdout,
    read_jsonl,
)


class RunReviewBroadGateDeferralTests(RunReviewHarnessLifecycleBase):
    """Deferred broad-gate failures and generated repair sessions."""

    def test_findings_defer_broad_failure_until_repair_and_all_clear(self) -> None:
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        review_turns = [
            turn
            for turn in payload["turns"]
            if turn["session_kind"] == plans_payloads.SESSION_KIND_REVIEW
        ]
        self.assertEqual(2, len(review_turns))
        first_verification = review_turns[0]["verification"]
        self.assertFalse(first_verification["passed"])
        self.assertEqual(
            verification_transitions.REVIEW_BROAD_FAILURES_DEFERRED_REASON,
            first_verification["acceptance"]["reason"],
        )
        self.assertEqual(
            ["broad:1:repair-child-exists"],
            [failure["name"] for failure in first_verification["deferred_failures"]],
        )
        finished = [
            record["payload"]
            for record in self._read_events()
            if record["name"] == events.VERIFICATION_FINISHED_EVENT
            and record["payload"].get("acceptance")
        ]
        self.assertEqual(1, len(finished))
        self.assertFalse(finished[0]["passed"])
        self.assertEqual([], payload["commits"])
        self.assertFalse(commit_log.exists())

    def test_a_stopped_run_cannot_leave_a_deferred_broad_failure_green(self) -> None:
        # ASI-133 reproducer. The review defers its failing broad gate against
        # a repair loop, and the stop is exactly what keeps that loop from ever
        # running. The ending is legitimate; the success is not. Before terminal
        # adjudication this exited 0 reporting ``stopped`` with the plan's own
        # required gate still failing and nothing naming it.
        self._setup_review_with_broad_gate_waiting_for_repair()
        self._queue_stop_after_current()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        adjudication = payload["terminal_adjudication"]
        self.assertTrue(adjudication["claimed_ok"])
        self.assertEqual("stopped", adjudication["claimed_reason"])
        unresolved = payload["pause_details"]["deferred_unresolved"]
        self.assertEqual(
            ["broad:1:repair-child-exists"],
            sorted(check for entry in unresolved for check in entry["checks"]),
        )
        self.assertEqual(["2"], [entry["session_id"] for entry in unresolved])
        self.assertEqual(
            pause_policy.USER_ACTION_INSPECT,
            payload["action_required"]["user_action"],
        )

    def test_a_resumed_run_that_redeems_nothing_keeps_the_same_blocker(self) -> None:
        # The blocker lives in the append-only event log, so a resumed run
        # re-derives it instead of inheriting a forgotten one — and stopping
        # again redeems nothing, so the same deferral blocks the same way.
        self._setup_review_with_broad_gate_waiting_for_repair()
        self._queue_stop_after_current()
        first_completed, first = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )
        self.assertEqual(1, first_completed.returncode, first_completed.stderr)

        self._queue_stop_after_current()
        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        self.assertEqual(
            first["pause_details"]["deferred_unresolved"][0]["checks"],
            payload["pause_details"]["deferred_unresolved"][0]["checks"],
        )

    def test_a_resumed_run_clears_the_blocker_once_a_review_comes_back_clear(
        self,
    ) -> None:
        # The redeeming half: the resumed run executes the repair the deferral
        # was promised against, the next review cycle comes back all-clear, and
        # only then may the plan report itself complete.
        self._setup_review_with_broad_gate_waiting_for_repair()
        self._queue_stop_after_current()
        blocked_completed, blocked = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings"},
        )
        self.assertEqual(1, blocked_completed.returncode, blocked_completed.stderr)
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            blocked["pause_reason"],
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "all-clear"},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertFalse(payload["terminal_adjudication"]["blocked"])
        self.assertEqual("all-clear", payload["review_cycles"][-1]["disposition"])

    def test_an_exhausted_repair_budget_cannot_strand_the_deferral_either(
        self,
    ) -> None:
        # ASI-133's second shape, observed on 20260825T152058432989Z-6755: the
        # run refuses success on its own (``review-findings-unresolved``), so
        # nothing here is dishonest — but the deferral it opened is still owed
        # and a plain failure leaves nobody to redeem it. Adjudicating the
        # failure path turns that into a blocker a resume re-evaluates.
        self._setup_review_with_broad_gate_waiting_for_repair()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            max_review_fix_cycles=1,
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "findings,findings"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual(
            verification_transitions.REQUIRED_CHECKS_INCOMPLETE_REASON,
            payload["pause_reason"],
        )
        adjudication = payload["terminal_adjudication"]
        self.assertFalse(adjudication["claimed_ok"])
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            adjudication["claimed_reason"],
        )
        # The narrower reason the review path recorded survives the pause.
        self.assertEqual(
            review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON,
            payload["action_required"]["reason"],
        )
        self.assertEqual(
            ["broad:1:repair-child-exists"],
            sorted(
                check
                for entry in payload["pause_details"]["deferred_unresolved"]
                for check in entry["checks"]
            ),
        )

    def test_generated_repair_commits_under_null_ordinary_mode(self) -> None:
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_REPAIR_MUTATION_PATH": "repair.txt",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD, payload["repair_commit_mode"]
        )
        self.assertEqual(1, len(read_jsonl(commit_log)))
        self.assertEqual(
            ["2.1"], [commit["session_id"] for commit in payload["commits"]]
        )
        self.assertEqual(
            "",
            git_stdout(self.repo_root, "status", "--porcelain"),
        )
        self.assertEqual(
            "repair.txt",
            git_stdout(self.repo_root, "show", "--format=", "--name-only", "HEAD"),
        )

    def test_generated_repair_skips_runner_commit_after_worker_commit(self) -> None:
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_REPAIR_MUTATION_PATH": "repair.txt",
                "FAKE_CODEX_REPAIR_COMMIT": "1",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([], payload["commits"])
        self.assertFalse(commit_log.exists())
        repair_turn = next(
            turn for turn in payload["turns"] if turn["session_id"] == "2.1"
        )
        self.assertEqual("no-changes", repair_turn["repair_commit"]["result"])
        changed = repair_turn["workspace_write_guard"]["changed"]
        self.assertTrue(next(iter(changed.values()))["head_changed"])

    def test_generated_repair_dirty_start_pauses_with_path_evidence(self) -> None:
        # The review turn leaves a guard-excluded file behind, so the repair
        # child inherits a dirty worktree it did not author. The preflight has
        # to name that path and stop before the child is ever dispatched.
        self._setup_review_with_broad_gate_waiting_for_repair()
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            review_guard_exclude=("dirty.txt",),
            extra_env={
                **commit_env,
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_REVIEW_MUTATION_PATH": "dirty.txt",
            },
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("commit-preflight", payload["pause_reason"])
        self.assertEqual("2.1", payload["pause_details"]["session_id"])
        self.assertEqual(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            payload["pause_details"]["repair_commit_mode"],
        )
        dirty = payload["pause_details"]["dirty_repositories"]
        self.assertEqual(["dirty.txt"], dirty[0]["dirty_paths"])
        self.assertEqual(["2"], [turn["session_id"] for turn in payload["turns"]])
        self.assertFalse(commit_log.exists())
        self.assertEqual(
            "pending",
            plans_handoff.read_session_status(
                plans_models.SessionFile(
                    plans_session_ids.parse_session_id("2.1"),
                    self.plan_dir / "session-2.1.md",
                )
            ).status,
        )
        # The one payload has to agree with itself: no answer clears a dirty
        # worktree, so the pause asks for an inspection and names the fix, and
        # the continuation refuses the child it just declined to dispatch
        # instead of reporting it eligible (ASI-114 / ASI-6).
        action = payload["action_required"]
        self.assertEqual("commit-preflight", action["reason"])
        self.assertEqual(pause_policy.USER_ACTION_INSPECT, action["user_action"])
        self.assertTrue(action["remediation"])
        resume = payload["review_resume"]
        self.assertEqual("run-repairs", resume["action"])
        self.assertFalse(resume["eligible"])
        self.assertEqual("commit-preflight", resume["blocker"])
        self.assertEqual(["2.1"], resume["pending_repair_session_ids"])

    def test_all_clear_cannot_defer_a_broad_failure(self) -> None:
        review_session = self._setup_review_with_broad_gate_waiting_for_repair()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "all-clear"},
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("verify-failed", payload["pause_reason"])
        verification_payload = payload["turns"][0]["verification"]
        self.assertFalse(verification_payload["passed"])
        self.assertNotIn("acceptance", verification_payload)
        self.assertEqual(
            "pending", plans_handoff.read_session_status(review_session).status
        )

    def test_v2_review_missing_structured_artifact_pauses_run(self) -> None:
        review_session = self._setup_complete_plan_with_pending_review()
        # Valid markdown review, but no final-review.json: the generated v2
        # contract must fail closed.
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            self._valid_final_review_body(), encoding="utf-8"
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # The turn ran, so the run attests it — but attestation reads the
        # verdict under the same artifact requirement the run enforced, so a
        # verdict already refused as invalid is never projected back.
        self.assertEqual(review_session.id, payload["review_session_executed"])
        self.assertIsNone(payload["review_verdict"])
        self.assertIsNone(payload["review_artifact_sha256"])
        self.assertNotIn("acceptance", payload["turns"][0]["verification"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_v2_review_contradictory_structured_artifact_pauses_run(
        self,
    ) -> None:
        review_session = self._setup_complete_plan_with_pending_review()
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        # All-clear artifact contradicts the findings status line.
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)

    def test_v2_review_incomplete_artifact_fails_runner_deep_validation(
        self,
    ) -> None:
        # A finding whose verification check is structurally present but not a
        # valid contract check passes the standalone contract gate and must be
        # caught by the runner's shared-module validation.
        review_session = self._setup_complete_plan_with_pending_review()
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_agent_session_loop.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        finding = _structured_review_finding(
            verification_checks=[{"type": "bogus", "name": "not-a-check"}]
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )

        completed, payload = self._run(review_harness="claude")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertEqual(
            "malformed-review-artifact",
            payload["pause_details"]["failure_reason"],
        )
        self.assertFalse(payload["review_completed"])
        review_text = review_session.path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", review_text)
        self.assertIn("review verdict validation failed", review_text)
