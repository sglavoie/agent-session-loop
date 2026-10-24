"""Commit-per-session, commit-at-end and legacy commit retries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_session_loop import (
    commit_message,
    events,
    plans_content_hash,
    plans_files,
    plans_metadata,
    plans_models,
    plans_readiness,
    progress_repair,
    run_selection,
)
from tests.harness_integration_base import FakeHarnessIntegrationBase
from tests.support import (
    REPO_ROOT,
    git_stdout,
    init_git_repo,
    read_jsonl,
)

ASL_TEST_LANE = "integration"


class CommitModeTests(FakeHarnessIntegrationBase):
    def test_commit_per_session_commits_after_each_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(
                run_selection.COMMIT_MODE_PER_SESSION, payload["commit_mode"]
            )
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(
                initial_commit_count + 2,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            expected_cwd = str(plans_files.canonicalize_path(repo_dir))
            commit_records = read_jsonl(commit_log)
            self.assertEqual(2, len(commit_records))
            for record in commit_records:
                self.assertEqual(expected_cwd, record["cwd"])

            self.assertEqual(2, len(payload["commits"]))
            for expected_session, commit in enumerate(payload["commits"], start=1):
                self.assertTrue(commit["success"])
                self.assertEqual("success", commit["status_result"])
                self.assertEqual("session", commit["scope"])
                self.assertEqual(expected_session, commit["session"])
                self.assertEqual(expected_cwd, commit["cwd"])
                # The run commits with the `asl commit` its own checkout
                # ships, under the run's declared implementation harness --
                # never with a `spec` resolved off PATH. A legacy target has
                # no recorded write scope, so it stages repository-wide and
                # carries no `--pathspec`. The bound is on every run-owned
                # commit argv, including this one: the run never leaves its
                # nested message turn unbounded.
                self.assertEqual(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "commit",
                        "--yes",
                        "--no-verify",
                        "--timeout",
                        str(commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS),
                        "--harness",
                        "codex",
                    ],
                    commit["argv"],
                )
                self.assertTrue(Path(commit["result_path"]).exists())

            # The session commits between the harness exiting and the turn
            # being accepted: a failed commit still refuses the turn, so
            # `turn_finished` cannot precede it.
            event_records = read_jsonl(Path(payload["events_path"]))
            turn_1_executed = self.event_index(
                event_records, events.TURN_EXECUTION_FINISHED_EVENT, session=1
            )
            turn_1_finished = self.event_index(
                event_records, events.TURN_FINISHED_EVENT, session=1
            )
            commit_1_started = self.event_index(
                event_records,
                events.COMMIT_STARTED_EVENT,
                session=1,
                scope="session",
            )
            commit_1_finished = self.event_index(
                event_records,
                events.COMMIT_FINISHED_EVENT,
                session=1,
                scope="session",
            )
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(turn_1_executed, commit_1_started)
            self.assertLess(commit_1_started, commit_1_finished)
            self.assertLess(commit_1_finished, turn_1_finished)
            self.assertLess(turn_1_finished, turn_2_started)

    def test_commit_at_end_commits_once_after_all_sessions_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=run_selection.COMMIT_MODE_END,
                extra_env=commit_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(run_selection.COMMIT_MODE_END, payload["commit_mode"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(commit_log)))
            self.assertEqual(
                initial_commit_count + 1,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            commit = payload["commits"][0]
            self.assertTrue(commit["success"])
            self.assertEqual("end", commit["scope"])
            self.assertIsNone(commit["session"])

            event_records = read_jsonl(Path(payload["events_path"]))
            turn_2_finished = self.event_index(
                event_records, "turn_finished", session=2
            )
            commit_started = self.event_index(
                event_records,
                events.COMMIT_STARTED_EVENT,
                session=None,
                scope="end",
            )
            commit_finished = self.event_index(
                event_records,
                events.COMMIT_FINISHED_EVENT,
                session=None,
                scope="end",
            )
            run_finished = self.event_index(event_records, "run_finished")
            self.assertLess(turn_2_finished, commit_started)
            self.assertLess(commit_started, commit_finished)
            self.assertLess(commit_finished, run_finished)

    def test_commit_failure_soft_fails_and_the_run_carries_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(
                tmp_dir,
                mode="failure",
            )
            initial_head = git_stdout(repo_dir, "rev-parse", "HEAD")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            # A repository that refuses every commit costs the plan its
            # commits, not its remaining sessions: the run finishes the work
            # and reports the commits it never made.
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(2, len(payload["turns"]))
            self.assertTrue(read_jsonl(commit_log))
            self.assertEqual(initial_head, git_stdout(repo_dir, "rev-parse", "HEAD"))

            self.assertFalse(any(commit["success"] for commit in payload["commits"]))
            commit = payload["commits"][0]
            self.assertEqual("nonzero-exit", commit["status_result"])
            # `asl commit` reports every refusal as exit 1; the runner reads
            # the reason out of the captured stderr, not out of the code.
            self.assertEqual(1, commit["returncode"])

            failures = payload["commit_failures"]
            self.assertTrue(failures)
            self.assertEqual({"session"}, {record["scope"] for record in failures})
            # Both sessions tried and both retries tried, so each session is
            # named at least once.
            self.assertEqual({"1", "2"}, {record["session_id"] for record in failures})
            self.assertEqual(
                sorted({"1", "2"}),
                sorted(
                    {
                        str(summary["session_id"])
                        for summary in payload["unresolved_commit_groups"]
                    }
                ),
            )

            event_names = [
                record["name"] for record in read_jsonl(Path(payload["events_path"]))
            ]
            self.assertNotIn("run_paused", event_names)
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )

    def test_transient_commit_failure_heals_before_the_next_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            commit_log, commit_env = self.setup_commit_harness(
                tmp_dir,
                mode="failure",
                fail_limit=1,
            )
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                extra_env=commit_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            # The one failure was retried inside the same run, so nothing is
            # left owed and no human had to resume anything.
            self.assertEqual(1, len(payload["commit_failures"]))
            self.assertEqual([], payload["unresolved_commit_groups"])
            self.assertTrue(payload["commit_group_recovery"]["success"])
            self.assertEqual(
                initial_commit_count + 2,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )
            self.assertEqual("", git_stdout(repo_dir, "status", "--porcelain"))

            event_records = read_jsonl(Path(payload["events_path"]))
            names = [record["name"] for record in event_records]
            self.assertNotIn("run_paused", names)
            recovery_commit = [
                index
                for index, record in enumerate(event_records)
                if record["name"] == events.COMMIT_FINISHED_EVENT
                and record["payload"].get("session") == 1
                and record["payload"].get("success")
            ][-1]
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(recovery_commit, turn_2_started)

    def test_legacy_failed_commit_is_retried_before_next_session_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
                statuses=("pending", "pending")
            )
            subprocess.run(
                ["git", "init"], cwd=plan_dir, capture_output=True, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=plan_dir,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test User"],
                cwd=plan_dir,
                capture_output=True,
                check=True,
            )
            progress_repair.repair_progress_journal(plan_dir)
            plans_readiness.write_readiness_metadata(
                plan_dir,
                plans_models.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans_content_hash.stable_readiness_sha256(
                        plan_dir
                    ),
                    checked_at="2026-08-08T00:00:00Z",
                ),
            )
            plans_metadata.save_plan_metadata(plan_dir, plan_dir)
            subprocess.run(
                ["git", "add", "-A"], cwd=plan_dir, capture_output=True, check=True
            )
            subprocess.run(
                ["git", "commit", "-m", "initial plan"],
                cwd=plan_dir,
                capture_output=True,
                check=True,
            )
            commit_log, failing_env = self.setup_commit_harness(
                tmp_dir, mode="failure", mutation_path=None
            )
            # `--max-sessions 1` ends the run while the group is still owed,
            # which is the only way a failed commit now outlives its run: the
            # retry that would otherwise heal it happens at the next commit
            # boundary, and this run has none left.
            first, first_payload = self.run_loop_command(
                mode="success",
                plan_dir=plan_dir,
                state_dir=state_dir,
                config_home=config_home,
                cwd=plan_dir,
                max_sessions=1,
                commit_mode=run_selection.COMMIT_MODE_PER_SESSION,
                extra_env=failing_env,
            )
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertFalse(first_payload["paused"])
            self.assertEqual([1], first_payload["completed_sessions"])
            self.assertEqual(
                ["1"],
                [
                    str(summary["session_id"])
                    for summary in first_payload["unresolved_commit_groups"]
                ],
            )

            env = os.environ.copy()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "FAKE_CODEX_MODE": "success",
                    "FAKE_CODEX_COMMIT_LOG": str(commit_log),
                    "PYTHONPATH": str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                }
            )
            env.pop("FAKE_CODEX_COMMIT_MODE", None)
            dry_run = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--resume",
                    "--dry-run",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stderr)
            dry_payload = json.loads(dry_run.stdout)
            self.assertTrue(dry_payload["commit_group_recovery"]["pending"])
            # The first run spent its in-run retry on the same group, so two
            # refused turns are on the log and a dry run adds none.
            self.assertEqual(2, len(read_jsonl(commit_log)))

            second = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--resume",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, second.returncode, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertTrue(second_payload["commit_group_recovery"]["success"])
            self.assertEqual([2], second_payload["completed_sessions"])
            self.assertEqual([], second_payload["unresolved_commit_groups"])
            self.assertEqual(4, len(read_jsonl(commit_log)))
            event_records = read_jsonl(Path(second_payload["events_path"]))
            recovery_commit = [
                index
                for index, record in enumerate(event_records)
                if record["name"] == events.COMMIT_FINISHED_EVENT
                and record["payload"].get("session") == 1
            ][-1]
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(recovery_commit, turn_2_started)

    def test_default_run_does_not_require_or_create_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            init_git_repo(repo_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertIsNone(payload["commit_mode"])
            self.assertEqual([], payload["commits"])
            self.assertEqual([1], payload["completed_sessions"])
            self.assertEqual(
                initial_commit_count,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            event_names = [
                record["name"] for record in read_jsonl(Path(payload["events_path"]))
            ]
            self.assertNotIn(events.COMMIT_STARTED_EVENT, event_names)
            self.assertNotIn(events.COMMIT_FINISHED_EVENT, event_names)
