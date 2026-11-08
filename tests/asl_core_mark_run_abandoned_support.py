"""MarkRunAbandonedTests.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    plans_files,
    state,
    state_assurance_pending,
    state_locks,
)
from tests.support import (
    write_plan,
)

_HOLD_ABANDON_CLAIM_SOURCE = """
import fcntl, os, sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
sys.stdout.write("held\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


class MarkRunAbandonedTestsBase(unittest.TestCase):
    """Proof-of-death guards around the synthetic terminal run record."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True)
        self.state_path = self.plan_state / events.STATE_FILE_NAME
        self.events_path = self.plan_state / events.EVENTS_FILE_NAME
        self.lock_path = self.plan_state / state.LOCK_FILE_NAME

    def dead_pid(self) -> int:
        """A pid that has exited and been reaped, so it is provably gone."""
        process = subprocess.Popen([sys.executable, "-c", ""])
        process.wait()
        return process.pid

    def write_unfinished_state(
        self,
        *,
        run_id: str = "20260728T123131255926Z-77262",
        run_started_at: str = "2026-06-19T12:00:00+00:00",
        assurance: dict[str, object] | None = None,
        run_config: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "ok": True,
            "paused": False,
            "run_id": run_id,
            "harness": "claude",
            "reason": "",
            "run_started_at": run_started_at,
            "run_finished_at": None,
            "duration_seconds": None,
            "active_turn": {"session": 1, "turn_index": 1},
            "active_verification": {"session": 1, "turn_index": 1},
        }
        if assurance is not None:
            payload["assurance"] = assurance
        if run_config is not None:
            # Shaped like `run_payloads._initial_payload`: the plan directory
            # is a top-level field and the workspace binding lives in
            # `run_config`.
            payload["plan_dir"] = str(plans_files.canonicalize_path(self.plan_dir))
            payload["run_config"] = {
                "workspace_root": str(plans_files.canonicalize_path(self.plan_dir)),
                **run_config,
            }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def write_lock(self, *, pid: int, hostname: str | None) -> None:
        payload: dict[str, object] = {
            "version": 1,
            "owner": "agent-session-loop",
            "pid": pid,
            "plan_dir": str(plans_files.canonicalize_path(self.plan_dir)),
            "created_at": "2026-06-19T12:00:00+00:00",
        }
        if hostname is not None:
            payload["hostname"] = hostname
        self.lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def read_lock_info(self) -> state.LockInfo:
        lock_info = state_locks.read_lock_info(self.plan_dir, self.state_dir)
        assert lock_info is not None
        return lock_info

    def persisted_state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def assertUntouched(self) -> None:
        persisted = self.persisted_state()
        self.assertIsNone(persisted["run_finished_at"])
        self.assertNotIn("abandoned", persisted)
        self.assertFalse(self.events_path.exists())

    def pending_record(self, run_id: str) -> dict[str, object]:
        path = (
            self.plan_state
            / state.RUNS_DIR_NAME
            / run_id
            / state_assurance_pending.ASSURANCE_PENDING_FILE_NAME
        )
        return json.loads(path.read_text(encoding="utf-8"))
