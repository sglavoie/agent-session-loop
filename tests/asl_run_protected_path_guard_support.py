"""Protected path guard run tests."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    intervention,
    plans_metadata,
    plans_registry,
    plans_session_ids,
    review_session_body,
    review_session_contract,
    run_loop,
    state,
    state_locks,
    verification_models,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
    write_plan,
)


class ProtectedPathGuardRunTestsBase(unittest.TestCase):
    """End-to-end: a frozen path decides whether a finished turn is published.

    The guard is enqueued before the run rather than mid-turn because the
    external control queue is drained once before any turn starts, which makes
    "the guard is active for this turn" deterministic here. The mid-turn
    activation semantics -- a guard added late still compares against the
    turn-start snapshot -- are pinned by the controller and enforcement unit
    tests in tests/test_asl_guards.py.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        init_git_repo(self.repo_root)
        self.protected = self.repo_root / "notes.md"
        self.protected.write_text("turn-start contents\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.md"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "notes"],
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
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
        self.context = verification_models.VerificationContext.legacy(self.repo_root)
        self.cwd = self.repo_root
        self.review_harness: str | None = None

    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.cwd),
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
            # Non-None only for the legacy-review fixture: a run that names a
            # review harness also requests a generated review of its own.
            review_harness=self.review_harness,
        )

    def _use_legacy_review_fixture(self) -> None:
        """Turn the pending session into a generated review of this repository.

        A legacy review turn is the one shape that captures neither write-guard
        snapshot, so it is the only way to reach the crash path with the review
        fingerprint as the turn's sole turn-start comparison.
        """
        self.review_harness = "claude"
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            review_session_body.build_generated_review_session_body(
                plans_session_ids.parse_session_id("1"),
                review_harness="claude",
                plan_dir=self.plan_dir,
            ),
            encoding="utf-8",
        )
        self.assertTrue(
            review_session_contract.is_generated_review_session_file(session_path)
        )

    def _use_workspace_fixture(self) -> None:
        """Re-point this fixture at a registered two-repository workspace.

        The class default is a legacy single repository, which declares no
        write aliases and therefore has no way to reach a write-scope
        violation. Only the composition tests need one, so the workspace is
        built on demand rather than in ``setUp``.
        """
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root,
            DEFAULT_MULTI_REPO_SPECS[:2],
        )
        self.plan_dir = self.tmp_path / "workspace-plan"
        write_multi_repo_plan(
            self.plan_dir,
            ("pending",),
            specs=DEFAULT_MULTI_REPO_SPECS[:2],
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "repo": "SVC", '
                    '"path": "README.md", "name": "svc-readme"}',
                    session="1",
                )
            },
        )
        session_path = self.plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans_registry.read_repository_registry(self.plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            self.plan_dir,
            self.workspace_root,
            registry.digest,
        )
        self.context = verification_models.VerificationContext.from_plan(
            self.plan_dir,
            self.workspace_root,
        )
        self.cwd = self.workspace_root
        # The frozen file lives in the alias the session *may* write, so the
        # scope violation and the protected-path violation are independent
        # breaches rather than two names for one write.
        self.protected = self.repositories["SVC"] / "notes.md"
        self.protected.write_text("turn-start contents\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "notes.md"],
            cwd=self.repositories["SVC"],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "notes"],
            cwd=self.repositories["SVC"],
            text=True,
            capture_output=True,
            check=True,
        )

    def _turn_rejections(self, guard: str) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record["payload"]
            for record in read_jsonl(event_path)
            if record["name"] == events.TURN_REJECTED_EVENT
            and record["payload"].get("guard") == guard
        ]

    def _enqueue_guard(self, run_id: str, *protect_paths: str) -> None:
        dispatch = intervention.dispatch_control_command(
            self.plan_dir,
            control_queue.COMMAND_ADD_GUARD,
            state_dir=self.state_dir,
            protect_paths=list(protect_paths),
            reason="operator froze this file",
            expected_run_id=run_id,
        )
        self.assertEqual(
            tuple(protect_paths),
            tuple(
                f"{path.qualifier}:{path.pattern}"
                for path in dispatch.command.protect_paths
            ),
        )

    def _invoke(self, run_id: str) -> tuple[int, dict[str, Any]]:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        try:
            return run_loop.run_locked(
                args=self._args(),
                cwd=self.cwd,
                timeout=None,
                max_sessions=None,
                commit_mode=None,
                lock_info=lock_info,
                run_id=run_id,
                verification_context=self.context,
            )
        finally:
            state_locks.release_lock(self.plan_dir, self.state_dir)

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

    def _run_with(self, run_id: str, turn: Any) -> tuple[int, dict[str, Any]]:
        with mock.patch.object(harnesses, "run_harness", side_effect=turn):
            return self._invoke(run_id)
