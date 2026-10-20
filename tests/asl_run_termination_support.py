"""Shared fixtures for the ``test_asl_run_termination_*`` modules."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans_models,
    plans_snapshot,
    run_control,
    run_persistence,
    runner,
)
from tests.support import (
    write_plan,
)

#: The symbols this module took from the runner. The program's no-alias rule


def runner_source() -> str:
    return Path(runner.__file__).read_text(encoding="utf-8")


class _TerminationCase(unittest.TestCase):
    """Shared scaffolding: a real persistence handle over a temp directory."""

    def setUp(self) -> None:
        self.tmp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.plan_dir = self.tmp_dir / "plan"
        write_plan(self.plan_dir, ("complete", "pending"))
        self.event_path = self.tmp_dir / events.EVENTS_FILE_NAME
        self.payload: dict[str, Any] = {
            "state_path": str(self.tmp_dir / events.STATE_FILE_NAME),
            "run_id": "run-1",
            "plan_dir": str(self.plan_dir),
        }
        self.persistence = run_persistence._RunPersistence()

    def _listener(self) -> run_control.RunControlListener:
        return run_control.RunControlListener(enabled=False)

    def _snapshot(self) -> plans_models.PlanSnapshot:
        return plans_snapshot.inspect_plan(self.plan_dir)
