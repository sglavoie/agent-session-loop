"""Shared fixtures for the review delta scope tests."""

from __future__ import annotations

import argparse
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_models,
    plans_progress,
    plans_session_ids,
    plans_snapshot,
    review_continuation,
    review_cycle,
    review_delta,
    review_guard,
    review_session_body,
    run_persistence,
    session_verification,
)
from tests.support import (
    _structured_review_finding,
    init_git_repo,
    read_jsonl,
    write_completed_review_plan,
)


class ReviewDeltaScopeBase(unittest.TestCase):
    """Shared fixtures for the review delta scope tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo_root = self.tmp_path / "repo"
        self.plan_dir = self.tmp_path / "plan"
        init_git_repo(self.repo_root)
        self.event_path = self.tmp_path / "events.jsonl"
        self.finding = _structured_review_finding()
        write_completed_review_plan(self.plan_dir, [self.finding])

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _commit(self, rel: str, text: str) -> str:
        path = self.repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._git("add", rel)
        self._git("commit", "-m", f"write {rel}")
        return self._git("rev-parse", "HEAD")

    def _record_repair_cycle(self, **overrides: object) -> None:
        record: dict[str, Any] = {
            "cycle": 1,
            "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            "review_session_id": "2",
            "repair_session_ids": ["2.1"],
            "fingerprints": ["finding:F1"],
            "worktree_dirty": False,
            "dirty_paths": [],
        }
        record.update(overrides)
        events.append_event(
            self.event_path, events.Event(events.REVIEW_CYCLE_EVENT, record)
        )

    def _record_repair_progress(self, *files: str) -> None:
        plans_progress.append_progress_entry(
            self.plan_dir,
            plans_models.ProgressEntry(
                session_id=plans_session_ids.parse_session_id("2.1"),
                status="complete",
                completed_at="2026-01-01T00:00:00Z",
                files_modified=files,
            ),
        )

    def _compute(
        self, cwd: Path | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        return review_delta._compute_review_delta_context(
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root if cwd is None else cwd,
        )

    def _scope_diff_commands(self, scope: str) -> list[str]:
        """Return the `git` commands the rendered scope tells a reviewer to run."""
        return [span for span in scope.split("`")[1::2] if span.startswith("git ")]

    def _scope_section(self, delta: dict[str, Any]) -> str:
        """Return the `### Scope` text the reviewer is actually told to read."""
        body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
            delta=delta,
        )
        self.assertIn("## Delta Review Scope", body)
        return body.split("### Scope", 1)[1]

    def _repo_state(self) -> dict[str, Any] | None:
        return review_guard._review_repo_state(
            review_guard._capture_review_repo_fingerprint(
                self.repo_root, self.plan_dir, exclude=()
            )
        )

    def _advance_cycle(
        self, repo_state: object
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Run one all-clear cycle and return its state and event records."""
        write_completed_review_plan(self.plan_dir, [])
        snapshot = plans_snapshot.inspect_plan(self.plan_dir)
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }

        action, _ = review_cycle._advance_review_cycle(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(),
            args=argparse.Namespace(
                state_dir=str(self.tmp_path / "state"),
                allow_not_loop_ready=False,
            ),
            cwd=self.repo_root,
            session_id="2",
            session_label="session 2",
            session_details={"session": "2"},
            snapshot=snapshot,
            turn_payload={
                "review_verdict": {
                    "passed": True,
                    "outcome": "all-clear",
                    "total_findings": 0,
                    "findings": [],
                },
                "review_repo_state": repo_state,
            },
            max_review_fix_cycles=1,
            plan_cache=plans_snapshot.PlanSnapshotCache(),
            reinspect=session_verification._reinspect_after_plan_mutation,
        )

        self.assertEqual("complete", action)
        recorded = [
            entry["payload"]
            for entry in read_jsonl(self.event_path)
            if entry["name"] == events.REVIEW_CYCLE_EVENT
        ]
        return payload["review_cycles"][0], recorded[-1]

    def _pending_delta_body(self) -> str:
        """Render the session file a crashed run would have left pending."""
        delta, fallback_reason = self._compute()
        self.assertIsNone(fallback_reason)
        return review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("3"),
            review_harness="codex",
            plan_dir=self.plan_dir,
            delta=delta,
        )
