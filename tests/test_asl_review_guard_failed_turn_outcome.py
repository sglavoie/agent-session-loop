"""Review guard outcomes for failed review turns."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import uuid
from contextlib import (
    ExitStack,
)
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    harnesses,
    mailbox_leases,
    pause_policy,
    plans,
    review_session_body,
    run_coordination,
    run_loop,
    state,
    turn_execution,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    install_fake_harness,
    patched_env,
    write_plan,
)


class ReviewGuardFailedTurnOutcomeTests(unittest.TestCase):
    """A failed review turn still has to answer for what it did to the worktree.

    Every unsuccessful outcome used to pause before the post-turn fingerprint
    was captured, so a review that mutated the repository and then timed out
    was relaunched by the supervisor against its own mutation.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        fake_codex = install_fake_codex(self.tmp_path)
        fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_claude)},
                }
            ),
            encoding="utf-8",
        )
        write_plan(
            self.plan_dir,
            ("complete",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                )
            },
        )
        self.review_session = review_session_body.ensure_generated_review_session(
            self.plan_dir, review_harness="claude"
        )[0]

    def _run_locked(
        self,
        *,
        returncode: int = 0,
        timed_out: bool = False,
        interrupted: bool = False,
        last_message: str = "Runner status: complete\n",
        mutate: bool = True,
        unreadable_plan: bool = False,
        forbid_mailbox_wait: bool = False,
        git_argvs: tuple[tuple[str, ...], ...] = (),
    ) -> dict[str, Any]:
        """Drive one review turn whose harness fails the way `kwargs` describe.

        ``git_argvs`` run in the repository during the turn, standing in for a
        ref move made by the session or by a concurrent process.
        """
        pending_git = list(git_argvs)

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            if mutate:
                (self.repo_root / "README.md").write_text(
                    "mutated by review\n", encoding="utf-8"
                )
            # Only the review turn moves refs, not a later audit harness call.
            while pending_git:
                subprocess.run(
                    ["git", *pending_git.pop(0)],
                    cwd=self.repo_root,
                    capture_output=True,
                    check=True,
                )
            plan_dir = Path(str(kwargs["plan_dir"]))
            path = plan_dir / self.review_session.path.name
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            if unreadable_plan:
                (plan_dir / "context.md").unlink()
            return harnesses.HarnessResult(
                name=name,
                argv=[name],
                returncode=returncode,
                stdout="",
                stderr="",
                last_message=last_message,
                timed_out=timed_out,
                interrupted=interrupted,
            )

        args = cli.build_parser().parse_args(
            [
                "run",
                str(self.plan_dir),
                "--cwd",
                str(self.repo_root),
                "--harness",
                "codex",
                "--review-harness",
                "claude",
                "--state-dir",
                str(self.state_dir),
                "--allow-not-loop-ready",
            ]
        )
        with ExitStack() as stack:
            stack.enter_context(
                patched_env(
                    XDG_CONFIG_HOME=str(self.config_home),
                    PYTHONPATH=str(REPO_ROOT)
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                )
            )
            stack.enter_context(
                mock.patch.object(
                    harnesses, "run_harness", side_effect=fake_run_harness
                )
            )
            if forbid_mailbox_wait:
                mailbox_leases.write_lease(
                    self.plan_dir,
                    str(uuid.uuid4()),
                    state_dir=str(self.state_dir),
                )
                stack.enter_context(
                    mock.patch.object(
                        run_coordination,
                        "prepare_mailbox_reply_wait",
                        side_effect=AssertionError(
                            "the mailbox reply wait must not be reachable "
                            "after the review mutation guard trips"
                        ),
                    )
                )
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                _exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-guard-turn-outcome",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return payload

    def _assert_guard_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Assert the run stopped on the guard and returned the durable record."""
        self.assertTrue(payload["paused"])
        self.assertEqual("review-mutated-worktree", payload["pause_reason"])
        self.assertFalse(payload["review_completed"])
        self.assertEqual(
            "pending", plans.read_session_status(self.review_session).status
        )
        turn = payload["turns"][0]
        guard = turn["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("review-mutated-worktree", guard["reason"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(["README.md"], guard["changed_paths"]["tracked"])
        # The evidence has to survive the process, not just the payload.
        persisted = json.loads(
            Path(str(turn["result_path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(guard, persisted["review_mutation_guard"])
        return turn

    def _assert_supervisor_will_not_relaunch(
        self, payload: dict[str, Any], *, suppressed_reason: str
    ) -> None:
        """Assert the pause the supervisor sees is not the retryable one."""
        policy = payload["pause_policy"]
        self.assertFalse(policy["recoverable"])
        self.assertEqual(0, policy["max_retries"])
        self.assertNotIn(
            "review-mutated-worktree", pause_policy.RECOVERABLE_PAUSE_REASONS
        )
        # Without the guard the run would have surfaced this reason instead,
        # and the supervisor would have relaunched onto the mutated tree.
        self.assertIn(suppressed_reason, pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_timed_out_review_turn_trips_the_guard_instead_of_retrying(
        self,
    ) -> None:
        payload = self._run_locked(returncode=124, timed_out=True)

        turn = self._assert_guard_pause(payload)
        self.assertTrue(turn["timed_out"])
        self._assert_supervisor_will_not_relaunch(payload, suppressed_reason="timeout")

    def test_nonzero_exit_review_turn_trips_the_guard(self) -> None:
        payload = self._run_locked(returncode=3)

        turn = self._assert_guard_pause(payload)
        self.assertEqual(3, turn["returncode"])

    def test_runner_pause_review_turn_trips_the_guard_before_any_reply_wait(
        self,
    ) -> None:
        payload = self._run_locked(
            last_message="Runner status: paused\nRUNNER_PAUSE: blocked - needs a "
            "decision\n",
            forbid_mailbox_wait=True,
        )

        self._assert_guard_pause(payload)
        self.assertIsNone(payload.get("waiting_for_reply"))

    def test_interrupted_review_turn_trips_the_guard(self) -> None:
        payload = self._run_locked(
            returncode=turn_execution.INTERRUPTED_EXIT, interrupted=True
        )

        self._assert_guard_pause(payload)

    def test_unreadable_plan_after_a_mutating_review_turn_trips_the_guard(
        self,
    ) -> None:
        payload = self._run_locked(unreadable_plan=True)

        turn = self._assert_guard_pause(payload)
        self.assertFalse(turn["timed_out"])

    def test_a_clean_failed_review_turn_keeps_its_own_pause_reason(self) -> None:
        payload = self._run_locked(returncode=124, timed_out=True, mutate=False)

        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertTrue(payload["turns"][0]["review_mutation_guard"]["passed"])

    # ASI-161: a background push (or fetch) moving refs/remotes/* is drift with
    # an unknown writer, never a content mutation by the review.

    def _head(self) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_a_remote_tracking_ref_move_alone_passes_with_drift_recorded(
        self,
    ) -> None:
        head = self._head()
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "older"],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", head],
            cwd=self.repo_root,
            capture_output=True,
            check=True,
        )
        tip = self._head()

        payload = self._run_locked(
            mutate=False,
            git_argvs=(
                ("update-ref", "refs/remotes/origin/main", tip),
                ("update-ref", "refs/remotes/origin/feature", tip),
            ),
        )

        self.assertNotEqual("review-mutated-worktree", payload.get("pause_reason"))
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(
            [
                {
                    "ref": "refs/remotes/origin/feature",
                    "change": "added",
                    "before": None,
                    "after": tip,
                    "writer": "unknown",
                    "new_tip_is_local": True,
                },
                {
                    "ref": "refs/remotes/origin/main",
                    "change": "changed",
                    "before": head,
                    "after": tip,
                    "writer": "unknown",
                    "new_tip_is_local": True,
                },
            ],
            guard["remote_ref_drift"],
        )

    def test_a_remote_ref_moved_to_a_non_local_commit_is_still_only_drift(
        self,
    ) -> None:
        # A fetch lands commits no local ref names; it changes no content.
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        fetched = subprocess.run(
            ["git", "commit-tree", tree, "-p", "HEAD", "-m", "upstream"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        payload = self._run_locked(
            mutate=False,
            git_argvs=(("update-ref", "refs/remotes/origin/main", fetched),),
        )

        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertTrue(guard["passed"])
        self.assertFalse(guard["remote_ref_drift"][0]["new_tip_is_local"])

    def test_a_local_branch_or_tag_move_still_trips_the_guard(self) -> None:
        for argv in (("branch", "review-side-branch"), ("tag", "v-review")):
            with self.subTest(argv=argv):
                self.setUp()
                payload = self._run_locked(mutate=False, git_argvs=(argv,))

                self.assertEqual("review-mutated-worktree", payload["pause_reason"])
                guard = payload["turns"][0]["review_mutation_guard"]
                self.assertFalse(guard["passed"])
                self.assertEqual(
                    [f"refs/{'heads' if argv[0] == 'branch' else 'tags'}/{argv[1]}"],
                    [entry["ref"] for entry in guard["changed_paths"]["refs_added"]],
                )
                self.assertNotIn("remote_ref_drift", guard)

    def test_remote_drift_beside_a_tracked_change_still_trips_the_guard(
        self,
    ) -> None:
        payload = self._run_locked(
            git_argvs=(("update-ref", "refs/remotes/origin/main", "HEAD"),),
        )

        turn = self._assert_guard_pause(payload)
        guard = turn["review_mutation_guard"]
        self.assertEqual(
            ["refs/remotes/origin/main"],
            [entry["ref"] for entry in guard["remote_ref_drift"]],
        )
        # The remote-ref move is drift evidence, never named as a mutation.
        for key in ("refs_added", "refs_removed", "refs_changed"):
            self.assertEqual([], guard["changed_paths"][key])
        self.assertIn("README.md", payload["pause_message"])
        self.assertNotIn("refs/remotes", payload["pause_message"])
