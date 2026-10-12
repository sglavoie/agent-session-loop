"""Revert snapshot cache integration for the review guard."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    harnesses,
    plans,
    run_loop,
    session_verification,
    state,
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


class RevertSnapshotCacheIntegrationTests(unittest.TestCase):
    """A status revert must leave the run-owned snapshot cache usable.

    The revert rewrites the session file, so a re-read that bypasses the cache
    strands it on stale stamps and forces the review-continuation projection at
    loop exit to inspect the whole plan a second time.
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

    def _run_locked_counting_inspections(
        self,
    ) -> tuple[dict[str, Any], list[Path], list[int]]:
        """Drive one `_run_locked` turn, counting every `inspect_plan` call.

        Returns the run payload, the plan directories inspected in order, and
        the running count observed right after each post-revert re-inspection.
        """

        def complete_first_pending(plan_dir: Path) -> None:
            path = plan_dir / "session-1.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            complete_first_pending(Path(str(kwargs["plan_dir"])))
            return harnesses.HarnessResult(
                name=name,
                argv=[name],
                returncode=0,
                stdout="",
                stderr="",
            )

        inspected: list[Path] = []
        real_inspect_plan = plans.inspect_plan

        def counting_inspect_plan(plan_dir):
            inspected.append(plans.canonicalize_path(plan_dir))
            return real_inspect_plan(plan_dir)

        counts_after_revert: list[int] = []
        real_reinspect = session_verification._reinspect_after_revert

        def recording_reinspect(*args, **kwargs):
            snapshot = real_reinspect(*args, **kwargs)
            counts_after_revert.append(len(inspected))
            return snapshot

        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.repo_root),
            harness="codex",
            review_harness="claude",
            review_harness_model=None,
            state_dir=str(self.state_dir),
            json=False,
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
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
            mock.patch.object(plans, "inspect_plan", counting_inspect_plan),
            mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness),
            mock.patch.object(
                session_verification, "_reinspect_after_revert", recording_reinspect
            ),
        ):
            lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
            try:
                _exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-revert-cache",
                )
            finally:
                state.release_lock(self.plan_dir, self.state_dir)
        return payload, inspected, counts_after_revert

    def test_verification_revert_leaves_final_review_projection_cached(
        self,
    ) -> None:
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "MISSING.md", '
                    '"name": "missing-file"}',
                    session="1",
                )
            },
        )

        payload, inspected, counts_after_revert = (
            self._run_locked_counting_inspections()
        )

        self.assertEqual("verify-failed", payload["pause_reason"])
        session_text = (self.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        # The final continuation projection ran at loop exit.
        self.assertIsNotNone(payload["review_resume"])
        # Exactly one revert re-inspection, and nothing inspected the plan
        # again afterwards: the projection's `plan_cache.get()` was a hit.
        self.assertEqual(1, len(counts_after_revert))
        self.assertEqual(counts_after_revert[0], len(inspected))
