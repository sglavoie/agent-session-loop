"""State compaction tests.

Split from tests/test_asl_compaction.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    state,
)
from tests.support import (
    write_plan,
)


class StateCompactionTestsBase(unittest.TestCase):
    """Cover ``asl compact-state`` v1 -> v2 record compaction."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.tmp_path / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True, exist_ok=True)
        self.big = "X" * 40000

    def _v1_turn(self) -> dict:
        return {
            "session_id": "1",
            "turn_index": 1,
            "before": {
                "counts": {"pending": 1},
                "sessions": [{"id": "1", "status": "pending"}],
            },
            "after": {
                "counts": {"complete": 1},
                "sessions": [{"id": "1", "status": "complete"}],
            },
            "verification": {
                "passed": False,
                "checks": [
                    {
                        "name": "c1",
                        "status": "fail",
                        "stdout": self.big,
                        "stderr": "",
                    }
                ],
            },
        }

    def _write_state(self, payload: dict) -> Path:
        path = self.plan_state / events.STATE_FILE_NAME
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def _write_events(self, records: list[dict]) -> Path:
        path = self.plan_state / events.EVENTS_FILE_NAME
        path.write_text(
            "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_result(self, checks: list[dict]) -> Path:
        turn_dir = self.plan_state / "runs" / "r" / "turn-0001-session-1"
        turn_dir.mkdir(parents=True, exist_ok=True)
        path = turn_dir / "result.json"
        path.write_text(
            json.dumps(
                {"session_id": "1", "verification": {"checks": checks}},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def _state_dir_disk_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self.plan_state.rglob("*") if path.is_file()
        )
