"""Shared fixtures for the ControlCliTests modules split from test_asl_control."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import (
    control_queue,
)
from tests.support import (
    _write_live_lock_file,
    _write_state_payload,
    live_wakeup_endpoint,
)


class ControlCliTestsBase(unittest.TestCase):
    """`asl control`: the CLI producer surface for the durable control queue.

    The queue itself is covered by ``ControlQueueTests``; these tests pin the
    handler contract (enqueue plus a report of whether the command will take
    effect) and the parser wiring.
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"

    def queue_path(self, plan_dir: Path | None = None) -> Path:
        return control_queue.queue_path_for_plan(
            plan_dir if plan_dir is not None else self.plan_dir,
            self.state_dir,
        )

    def args(self, plan_dir: Path | str | None = None, **overrides):
        namespace = {
            "plan_dir": str(self.plan_dir if plan_dir is None else plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
        }
        namespace.update(overrides)
        return argparse.Namespace(**namespace)

    def mark_run_active(
        self,
        plan_dir: Path | None = None,
        *,
        review_harness: str | None = None,
    ) -> None:
        """Simulate a live runner: an active lock plus a mid-flight turn."""
        target = plan_dir if plan_dir is not None else self.plan_dir
        endpoint, _ = self.enterContext(live_wakeup_endpoint(target, self.state_dir))
        _write_live_lock_file(target, self.state_dir, wakeup_endpoint=endpoint)
        _write_state_payload(
            target,
            self.state_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260724T100000Z-1",
                "harness": "codex",
                "run_config": {"review_harness": review_harness},
                "reason": "",
                "run_started_at": "2026-07-24T10:00:00+00:00",
                "run_finished_at": None,
                "duration_seconds": None,
                "active_turn": {
                    "run_id": "20260724T100000Z-1",
                    "turn_index": 1,
                    "session": 1,
                    "session_id": "1",
                    "started_at": "2026-07-24T10:00:00+00:00",
                    "elapsed_seconds": 3.0,
                    "idle_seconds": 1.0,
                },
                "active_verification": None,
            },
        )

    def payload_from(self, handler, args) -> tuple[int, Any]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = handler(args)
        return exit_code, json.loads(buffer.getvalue())

    def mark_run_active_between_turns(self, plan_dir: Path | None = None) -> None:
        """Simulate a live runner holding the lock with no turn in flight."""
        target = plan_dir if plan_dir is not None else self.plan_dir
        _write_live_lock_file(target, self.state_dir)
        _write_state_payload(
            target,
            self.state_dir,
            {
                "ok": True,
                "paused": False,
                "run_id": "20260724T100000Z-1",
                "harness": "codex",
                "run_config": {"review_harness": None},
                "reason": "",
                "run_started_at": "2026-07-24T10:00:00+00:00",
                "run_finished_at": None,
                "duration_seconds": None,
                "active_turn": None,
                "active_verification": None,
            },
        )

    def unusable_state_dir(self) -> Path:
        """Return a state dir whose parent is a regular file.

        Queue setup calls ``mkdir(parents=True)``, which raises
        ``NotADirectoryError`` here — the hermetic equivalent of the
        ``--state-dir /dev/null`` failure an operator hits when the state root
        is not a directory.
        """
        blocker = self.tmp / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        return blocker / "state"
