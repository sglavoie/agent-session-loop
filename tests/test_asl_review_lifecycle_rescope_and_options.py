"""Review rescoping, v1 refresh, harness availability, and CLI options."""

from __future__ import annotations

import json

from agent_session_loop import (
    contracts,
    events,
    plans,
    review,
    review_continuation,
    review_delta,
    review_session_body,
    review_session_contract,
)
from tests.review_lifecycle_base import RunReviewHarnessLifecycleBase
from tests.support import (
    _contract_block,
    _structured_review_finding,
    git_stdout,
    read_jsonl,
    write_completed_review_plan,
)


class RunReviewRescopeAndOptionTests(RunReviewHarnessLifecycleBase):
    """Review rescoping, v1 refresh, harness availability, and CLI options."""

    def test_review_verdict_retry_preserves_note_across_rescope(self) -> None:
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
        review_session, created = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude", delta=delta
        )
        self.assertTrue(created)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            # Cycle 1's review already completed, so it consumes the first
            # scripted outcome; this delta review starts at the second.
            extra_env={
                "FAKE_CODEX_REVIEW_OUTCOMES": ("all-clear,invalid-artifact,all-clear")
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["review_completed"])
        self.assertEqual(1, len(self._review_verdict_retries()))
        resumed_body = review_session.path.read_text(encoding="utf-8")
        self.assertIn("## Delta Review Scope", resumed_body)
        self.assertIn("review verdict validation failed", resumed_body)

    def test_v1_pending_review_is_refreshed_before_execution(self) -> None:
        # Existing pending v1 review sessions are upgraded to the current
        # generated contract before the harness sees them.
        self._write_plan(("complete",), readiness=None)
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        review_id = plans.next_top_level_session_id(self.plan_dir)
        v1_contract = {
            "session": review_id.value,
            "checks": [
                {
                    "type": "command",
                    "name": "v1-final-review-output",
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
                    json.dumps(v1_contract, indent=2),
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
        # The first attempt writes a status line whose severity counts do not
        # add up. The refreshed contract rejects it immediately.
        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={"FAKE_CODEX_REVIEW_OUTCOMES": "invalid-markdown,all-clear"},
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual("verify-failed", payload["pause_reason"])
        refreshed = (self.plan_dir / review_id.filename).read_text(encoding="utf-8")
        self.assertIn(review.review_session_version_marker(), refreshed)
        contract = contracts.parse_completion_contract(refreshed, session_id=review_id)
        self.assertEqual(
            [
                "final-review-output-evidence-based",
                "final-review-artifact-consistent",
            ],
            [check.name for check in contract.verify.checks],
        )
        self.assertIn("- **Status**: pending", refreshed)

    def test_review_harness_ignores_quoted_runner_pause_in_stderr(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            extra_env={
                "FAKE_CODEX_STDOUT_TEXT": "Runner status: complete\n",
                "FAKE_CODEX_STDERR_TEXT": (
                    "```text\n"
                    "RUNNER_PAUSE: unsafe - plan is not loop-ready; "
                    "run `asl preflight <plan-dir> --mark-loop-ready` before "
                    "continuing\n"
                    "```\n"
                ),
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        self.assertIsNone(payload.get("action_required"))
        self.assertEqual(
            [plans.SESSION_KIND_IMPLEMENTATION, plans.SESSION_KIND_REVIEW],
            [turn["session_kind"] for turn in payload["turns"]],
        )
        for turn in payload["turns"]:
            self.assertEqual("complete", turn["status_result"])
            self.assertNotIn("runner_pause", turn)

    def test_review_harness_unavailable_pauses_run(self) -> None:
        # Configure `claude` with a non-existent path so `shutil.which` returns
        # None regardless of what the host PATH contains. The runner must
        # pause with `harness-unavailable` before launching the review turn.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")
        missing_claude = self.tmp_path / "definitely-not-a-real-claude"
        self._rewrite_harness_config(
            {
                "codex": str(self.fake_codex),
                "claude": str(missing_claude),
            }
        )

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("harness-unavailable", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        # The implementation turn ran with `codex`; the failure must occur
        # before the review turn starts.
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(
            plans.SESSION_KIND_IMPLEMENTATION, payload["turns"][0]["session_kind"]
        )
        # A review session was appended (so it's recorded for resume) even
        # though the review turn could not start.
        self.assertEqual("2", payload["review_session_id"])

    def test_run_rejects_same_review_harness_via_cli(self) -> None:
        # The CLI must refuse when --review-harness matches --harness.
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run(
            mode="success-with-review",
            harness="codex",
            review_harness="codex",
        )

        self.assertEqual(1, completed.returncode)
        self.assertEqual("input-error", payload["reason"])
        self.assertIn(
            "--review-harness must differ from --harness",
            payload.get("error", ""),
        )
        self.assertTrue(payload["review_requested"])
        self.assertEqual("codex", payload["review_harness"])
        self.assertIsNone(payload["review_session_id"])
        self.assertEqual(
            str(
                review_session_contract.final_review_output_path(
                    plans.canonicalize_path(self.plan_dir)
                )
            ),
            payload["review_output_path"],
        )
        self.assertFalse(payload["review_completed"])
        self.assertEqual("input-error", payload["review_skipped_reason"])

    def test_review_appends_at_top_level_id_in_dotted_plan(self) -> None:
        # Dotted-session plans must still get a single top-level review id
        # one greater than the max top-level component.
        self._write_dotted_plan(
            (("1", "pending"), ("1.1", "pending")), readiness="loop-ready"
        )
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(
            mode="success-with-review", review_harness="claude"
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # The generated review session must be `session-2.md`, not `1.2`.
        self.assertEqual("2", payload["review_session_id"])
        self.assertTrue((self.plan_dir / "session-2.md").is_file())
        # The two impl sessions ran in dotted order, then the review turn.
        turns = payload["turns"]
        self.assertEqual(3, len(turns))
        self.assertEqual("1", turns[0]["session_id"])
        self.assertEqual("1.1", turns[1]["session_id"])
        self.assertEqual("2", turns[2]["session_id"])
        self.assertEqual(plans.SESSION_KIND_REVIEW, turns[2]["session_kind"])

    def test_commit_per_session_skips_review_turn(self) -> None:
        # --commit-per-session must commit after implementation sessions but
        # never after the generated review session.
        self._write_plan(("pending",), readiness="loop-ready")
        commit_log, commit_env = self._commit_log_env()

        completed, payload = self._run(
            mode="success-with-review",
            review_harness="claude",
            commit_per_session=True,
            extra_env={
                # The implementation turn has to leave a real change behind:
                # `asl commit` refuses a clean worktree. Rewriting the same
                # bytes on the review turn is a no-op, so the review guard
                # still sees an untouched worktree afterwards.
                **commit_env,
                "FAKE_CODEX_MUTATION_PATH": "implemented.txt",
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertTrue(payload["review_completed"])
        # Exactly one auto-commit fired: for the implementation session.
        commit_records = read_jsonl(commit_log)
        self.assertEqual(1, len(commit_records))
        self.assertEqual(str(self.repo_root.resolve()), commit_records[0]["cwd"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertEqual("session", payload["commits"][0]["scope"])
        # The committed scope must reference the implementation session id (1),
        # not the review session id.
        self.assertEqual(1, payload["commits"][0]["session"])

    def test_run_without_review_harness_leaves_review_fields_unset(self) -> None:
        # When --review-harness is absent the run must behave exactly like
        # the pre-review-harness implementation: no review session file,
        # no review event, no review-related payload fields populated.
        self._write_plan(("pending",), readiness="loop-ready")
        (self.repo_root / "README.md").write_text("readme\n", encoding="utf-8")

        completed, payload = self._run(mode="success")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertFalse(payload["review_requested"])
        self.assertFalse(payload["review_completed"])
        self.assertIsNone(payload.get("review_harness"))
        self.assertIsNone(payload.get("review_session_id"))
        self.assertIsNone(payload.get("review_skipped_reason"))
        # All turns are implementation, never review.
        self.assertTrue(payload["turns"])
        for turn in payload["turns"]:
            self.assertEqual(plans.SESSION_KIND_IMPLEMENTATION, turn["session_kind"])
        # No generated review session file should exist.
        review_candidates = [
            path
            for path in self.plan_dir.glob("session-*.md")
            if review_session_contract.is_generated_review_session_file(path)
        ]
        self.assertEqual([], review_candidates)
        # No final-review.md should be written either.
        self.assertFalse(
            review_session_contract.final_review_output_path(self.plan_dir).is_file()
        )
        # No review-session-created event should have been emitted.
        review_events = [
            entry
            for entry in self._read_events()
            if entry.get("name") == events.REVIEW_SESSION_CREATED_EVENT
        ]
        self.assertEqual([], review_events)
