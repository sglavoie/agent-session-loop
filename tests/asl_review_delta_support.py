"""Shared fixtures for the ``test_asl_review_delta_*`` modules."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    review_continuation,
    review_delta,
    state,
)
from tests.support import (
    _structured_review_finding,
    init_git_repo,
    write_completed_review_plan,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to


class DeltaHarness(unittest.TestCase):
    """A real repository, a completed review on disk, and an event log."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo_root = root / "repo"
        init_git_repo(self.repo_root)
        self.plan_dir = root / "plan"
        self.finding = _structured_review_finding()
        write_completed_review_plan(self.plan_dir, [self.finding])
        self.state_root = root / "state"
        self.event_path = (
            state.plan_state_dir(self.plan_dir, self.state_root)
            / events.EVENTS_FILE_NAME
        )

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def record_repair_cycle(self, **overrides: Any) -> None:
        record: dict[str, Any] = {
            "cycle": 1,
            "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            "review_session_id": "2",
            "repair_session_ids": ["2.1"],
            "fingerprints": ["finding:F1"],
            "worktree_dirty": False,
            "dirty_paths": [],
            "repo_head": self.head(),
        }
        record.update(overrides)
        events.append_event(
            self.event_path, events.Event(events.REVIEW_CYCLE_EVENT, record)
        )

    def compute(self) -> tuple[dict[str, Any] | None, str | None]:
        return review_delta._compute_review_delta_context(
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )
