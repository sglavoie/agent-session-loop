"""Tail follower tests.

Split from tests/test_agent_session_loop.py."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import (
    durability,
    events,
    run_reporting,
    state,
    tail,
    tail_logs,
    tail_models,
)
from tests.support import (
    wait_until,
    write_plan,
)


class TailFollowerTestCase(unittest.TestCase):
    def setup_tail_files(self, tmp_dir: Path):
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        return (
            plan_dir,
            state_dir,
            plan_state / events.STATE_FILE_NAME,
            plan_state / events.EVENTS_FILE_NAME,
        )

    def write_state_payload(
        self,
        state_path: Path,
        payload: dict[str, object],
    ) -> None:
        durability.write_json_file(state_path, payload)

    def append_log_line(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)

    def cleanup_follower(self, follower: tail.TailFollower) -> None:
        follower.stop()
        follower.join(1.0)
