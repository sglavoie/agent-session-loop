"""Run-level handling of an unavailable review guard."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    events,
    harnesses,
    plans_handoff,
    review,
    review_guard,
    review_session_body,
    run_loop,
    state_locks,
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


class ReviewGuardUnavailableRunTests(unittest.TestCase):
    """A guard that cannot see the worktree must stop the run, not wave it on."""

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

    _UNAVAILABLE = {
        "git": True,
        "unavailable": {
            "reason": "git-command-failed",
            "argv": ["ls-files", "--others", "--exclude-standard"],
            "returncode": 128,
            "stderr": "fatal: broken index",
        },
    }
    _CLEAN = {
        "git": True,
        "head": "0" * 40,
        "diff_sha256": hashlib.sha256(b"").hexdigest(),
        "tracked_changed": [],
        "tracked_patches": [],
        "untracked": [],
    }

    def _run_locked(
        self, fingerprints: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], list[str]]:
        """Drive one run with `fingerprints` returned by successive captures."""
        harness_calls: list[str] = []

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            harness_calls.append(name)
            path = Path(str(kwargs["plan_dir"])) / self.review_session.path.name
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "- **Status**: pending", "- **Status**: complete", 1
                ),
                encoding="utf-8",
            )
            return harnesses.HarnessResult(
                name=name, argv=[name], returncode=0, stdout="", stderr=""
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
                # A completing run would otherwise inherit `claude` as its audit
                # role and add a harness call these assertions do not describe.
                "--no-supervisor-audit",
            ]
        )
        with (
            patched_env(
                XDG_CONFIG_HOME=str(self.config_home),
                PYTHONPATH=str(REPO_ROOT)
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            ),
            mock.patch.object(harnesses, "run_harness", side_effect=fake_run_harness),
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                side_effect=fingerprints,
            ),
        ):
            lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
            try:
                _exit_code, payload = run_loop.run_locked(
                    args=args,
                    cwd=self.repo_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id="test-run-guard-unavailable",
                )
            finally:
                state_locks.release_lock(self.plan_dir, self.state_dir)
        return payload, harness_calls

    def test_unavailable_before_capture_pauses_without_spending_the_turn(
        self,
    ) -> None:
        payload, harness_calls = self._run_locked([self._UNAVAILABLE])

        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, payload["pause_reason"]
        )
        self.assertEqual([], harness_calls)
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            "pending",
            plans_handoff.read_session_status(self.review_session).status,
        )
        guard = payload["pause_details"]["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("before", guard["phase"])
        self.assertEqual("git-command-failed", guard["unavailable"]["reason"])

    def test_unavailable_after_capture_reverts_the_review_to_pending(
        self,
    ) -> None:
        payload, harness_calls = self._run_locked([self._CLEAN, self._UNAVAILABLE])

        self.assertTrue(payload["paused"])
        self.assertEqual(
            events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, payload["pause_reason"]
        )
        self.assertEqual(["claude"], harness_calls)
        self.assertFalse(payload["review_completed"])
        self.assertEqual(
            "pending",
            plans_handoff.read_session_status(self.review_session).status,
        )
        guard = payload["turns"][0]["review_mutation_guard"]
        self.assertFalse(guard["passed"])
        self.assertEqual("after", guard["phase"])
        self.assertTrue(guard["reverted_to_pending"])
        self.assertEqual(guard, payload["pause_details"]["review_mutation_guard"])
        self.assertIn("git-command-failed", payload["pause_message"])

    def test_clean_captures_still_complete_the_review(self) -> None:
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- Reviewed agent_session_loop/runner.py for guard behavior.\n"
            "- Checked tests/test_asl_review_lifecycle_*.py for coverage.\n"
            "- Compared docs/agent-session-loop.md for operator impact.\n"
            "Status: All clear, no findings\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )

        payload, harness_calls = self._run_locked([self._CLEAN, self._CLEAN])

        self.assertFalse(payload["paused"])
        self.assertTrue(payload["review_completed"])
        self.assertEqual(["claude"], harness_calls)
        self.assertTrue(payload["turns"][0]["review_mutation_guard"]["passed"])
