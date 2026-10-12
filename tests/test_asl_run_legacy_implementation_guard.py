"""Legacy implementation guard run tests."""

from __future__ import annotations

import argparse
import dataclasses
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    run_loop,
    state,
    verification,
    workspaces,
)
from tests.support import (
    _contract_block,
    init_git_repo,
    write_plan,
)


class LegacyImplementationGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        init_git_repo(self.repo_root)
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme"}',
                    session="1",
                )
            },
        )
        self.context = verification.VerificationContext.legacy(self.repo_root)

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.repo_root,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)

    def _mark_complete(self) -> harnesses.HarnessResult:
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "- **Status**: pending", "- **Status**: complete", 1
            ),
            encoding="utf-8",
        )
        return harnesses.HarnessResult(
            name="codex",
            argv=["fake"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    def test_commit_mode_none_records_worker_created_commit(self) -> None:
        def commit_then_complete(*_args: object, **_kwargs: object) -> Any:
            (self.repo_root / "README.md").write_text(
                "worker commit\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "commit", "-am", "worker commit"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            return self._mark_complete()

        with mock.patch.object(
            harnesses, "run_harness", side_effect=commit_then_complete
        ):
            exit_code, payload = self._invoke("legacy-worker-commit")

        self.assertEqual(0, exit_code)
        self.assertIsNone(payload["commit_mode"])
        self.assertEqual([], payload["commit_groups"])
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual("legacy-single-repository", guard["scope_mode"])
        transition = guard["changed"]["."]["head_transition"]
        self.assertEqual("fast-forward", transition["relation"])
        self.assertEqual(1, transition["commits_added"])
        self.assertFalse(transition["history_rewrite"])

    def test_commit_mode_none_records_worker_history_rewrite(self) -> None:
        readme = self.repo_root / "README.md"
        readme.write_text("old branch tip\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "old branch tip"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        old_tip = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

        def rewrite_then_complete(*_args: object, **_kwargs: object) -> Any:
            subprocess.run(
                ["git", "branch", "backup/pre-rewrite", old_tip],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "reset", "--hard", "HEAD~1"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            readme.write_text("rewritten branch tip\n", encoding="utf-8")
            subprocess.run(
                ["git", "commit", "-am", "rewritten branch tip"],
                cwd=self.repo_root,
                text=True,
                capture_output=True,
                check=True,
            )
            return self._mark_complete()

        with mock.patch.object(
            harnesses, "run_harness", side_effect=rewrite_then_complete
        ):
            exit_code, payload = self._invoke("legacy-history-rewrite")

        self.assertEqual(0, exit_code)
        changes = payload["turns"][0]["workspace_write_guard"]["changed"]["."]
        transition = changes["head_transition"]
        self.assertEqual("rewrite", transition["relation"])
        self.assertEqual(1, transition["commits_added"])
        self.assertEqual(1, transition["commits_removed"])
        self.assertTrue(transition["history_rewrite"])
        self.assertIn(
            "refs/heads/backup/pre-rewrite",
            [entry["ref"] for entry in changes["refs_added"]],
        )

    def test_authorized_external_data_survives_the_full_run_guard(self) -> None:
        trial = self.tmp_path.resolve() / "trial"
        trial.mkdir()
        evidence = trial / "evidence.json"
        evidence.write_text("before\n", encoding="utf-8")
        args = self._args()
        args.external_write_root = [str(trial)]

        def edit_trial(*_args: object, **_kwargs: object) -> Any:
            evidence.write_text("after\n", encoding="utf-8")
            return dataclasses.replace(
                self._mark_complete(), last_message=f"Updated `{evidence}`.\n"
            )

        with (
            mock.patch.object(self, "_args", return_value=args),
            mock.patch.object(harnesses, "run_harness", side_effect=edit_trial),
        ):
            exit_code, payload = self._invoke("authorized-external-data")
        self.assertEqual(0, exit_code)
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertTrue(guard["passed"])
        record = guard["external_writes"]["authorized"][str(evidence)]
        self.assertNotEqual(record["before"]["sha256"], record["after"]["sha256"])

    def test_a_write_into_an_unobserved_repository_fails_the_guard_closed(
        self,
    ) -> None:
        # ASI-19. The legacy snapshot fingerprints `cwd` and nothing else, so
        # this edit produces no change, no added or removed repository and
        # nothing unavailable -- the three inputs `passed` was computed from.
        # The turn used to be recorded as clean work.
        other_root = self.tmp_path / "other"
        init_git_repo(other_root)

        def edit_elsewhere(*_args: object, **_kwargs: object) -> Any:
            (other_root / "README.md").write_text("edited away\n", encoding="utf-8")
            result = self._mark_complete()
            return dataclasses.replace(
                result,
                last_message=f"Done. I edited {other_root / 'README.md'}.\n",
            )

        with mock.patch.object(harnesses, "run_harness", side_effect=edit_elsewhere):
            exit_code, payload = self._invoke("legacy-unobserved-repository")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("legacy-single-repository", guard["scope_mode"])
        entry = guard["unavailable"][
            str(workspaces.canonicalize_filesystem_path(other_root))
        ]
        self.assertEqual("repository-not-observed", entry["reason"])
        # Nothing inside `cwd` moved, which is exactly why the old guard passed.
        self.assertEqual({}, guard["changed"])
        self.assertEqual([], guard["violations"])
        # The session the turn claimed goes back to pending: a completion the
        # guard cannot vouch for is not a completion.
        self.assertTrue(guard["reverted_to_pending"])

    def test_non_git_legacy_scope_refuses_before_harness(self) -> None:
        shutil.rmtree(self.repo_root / ".git")

        with mock.patch.object(
            harnesses,
            "run_harness",
            side_effect=AssertionError("harness must not start without Git scope"),
        ):
            exit_code, payload = self._invoke("legacy-scope-unavailable")

        self.assertEqual(1, exit_code)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        guard = payload["pause_details"]["workspace_write_guard"]
        self.assertEqual("legacy-single-repository", guard["scope_mode"])
        self.assertEqual("repository-unavailable", guard["unavailable"]["."]["reason"])
        # A refusal before the harness leaves nothing turn-shaped behind. An
        # empty turn directory reads downstream as a turn that ran and wrote no
        # result.json, which is a turn this run never spent.
        self.assertEqual([], sorted(self.state_dir.rglob("turn-*")))


if __name__ == "__main__":
    unittest.main()
