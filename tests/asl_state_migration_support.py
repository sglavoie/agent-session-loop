"""State orphan-guard and ``asl migrate-state`` tests.

Covers the guard that surfaces state directories stranded under a plan's old
path hash after a move, and the ``migrate-state`` command that relocates them.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    agent_mux_projection,
    cli,
    conversations,
    events,
    plans_errors,
    plans_files,
    plans_snapshot,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_vocab,
    state,
    state_abandonment,
    state_migration,
    state_schema_upgrade,
    state_status,
    state_status_text,
    workspaces,
)
from tests.support import read_jsonl, write_plan


class StateMigrationTestBase(unittest.TestCase):
    """Shared fixture: a plan moved from ``old`` to ``new`` with orphan state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_dir = self.tmp_path / "state"

        # The plan lives at its *new* location; the *old* path never exists on
        # disk (it was renamed away), which is exactly the orphan condition.
        self.new_plan = self.tmp_path / "plans" / "beta"
        write_plan(self.new_plan, ("pending",))
        self.new_plan = plans_files.canonicalize_path(self.new_plan)
        self.old_plan = plans_files.canonicalize_path(self.tmp_path / "plans" / "alpha")

        self.snapshot = plans_snapshot.inspect_plan(self.new_plan)
        self.content_sha256 = self.snapshot.content_sha256

        self.old_state_dir = state.plan_state_dir(self.old_plan, self.state_dir)
        self.new_state_dir = state.plan_state_dir(self.new_plan, self.state_dir)

    def _write_orphan_state(
        self,
        *,
        content_sha256: str | None = None,
        final_content_sha256: str | None = None,
        recorded_plan_dir: str | None = None,
        run_finished_at: str | None = "2026-07-23T00:00:00+00:00",
    ) -> Path:
        """Seed an orphaned ``state.json`` under the old plan's state dir.

        By default only an ``initial_snapshot`` is written (legacy shape). Pass
        ``final_content_sha256`` to also persist a ``final_snapshot`` — the
        shape a run leaves behind after mutating handoffs. ``recorded_plan_dir``
        overrides the recorded ``plan_dir`` to exercise identity mismatches.
        """
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.old_state_dir / events.STATE_FILE_NAME
        events_path = self.old_state_dir / events.EVENTS_FILE_NAME
        payload = {
            "ok": True,
            "command": "run",
            "plan_dir": (
                str(self.old_plan) if recorded_plan_dir is None else recorded_plan_dir
            ),
            "state_dir": str(self.old_state_dir),
            "state_path": str(state_path),
            "events_path": str(events_path),
            "event_path": str(events_path),
            "lock_path": str(self.old_state_dir / state.LOCK_FILE_NAME),
            "run_finished_at": run_finished_at,
            "initial_snapshot": {
                "content_sha256": (
                    self.content_sha256 if content_sha256 is None else content_sha256
                ),
            },
        }
        if final_content_sha256 is not None:
            payload["final_snapshot"] = {"content_sha256": final_content_sha256}
        state_path.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return state_path

    def _write_active_lock(self) -> Path:
        self.old_state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.old_state_dir / state.LOCK_FILE_NAME
        lock_path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "plan_dir": str(self.old_plan),
                    "created_at": "2026-07-23T00:00:00+00:00",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return lock_path

    def _add_result_artifact(self) -> Path:
        result_path = self.old_state_dir / "runs" / "run-1" / "turn-1" / "result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"after": {"plan_dir": str(self.old_plan)}}),
            encoding="utf-8",
        )
        return result_path


__all__ = [
    "io",
    "json",
    "os",
    "tempfile",
    "unittest",
    "redirect_stderr",
    "redirect_stdout",
    "Path",
    "agent_mux_projection",
    "cli",
    "conversations",
    "events",
    "plans_errors",
    "plans_files",
    "plans_snapshot",
    "redirect_store_journal",
    "redirect_store_models",
    "redirect_store_requests",
    "redirect_store_vocab",
    "state",
    "state_abandonment",
    "state_migration",
    "state_schema_upgrade",
    "state_status",
    "state_status_text",
    "workspaces",
    "read_jsonl",
    "write_plan",
    "StateMigrationTestBase",
]
