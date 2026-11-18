"""Remote refs changed between guard windows retain explicit drift evidence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import ref_observations, run_persistence
from tests.support import read_jsonl


class RefObservationTests(unittest.TestCase):
    def test_inter_turn_remote_moves_are_recorded_without_inventing_a_writer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "events.jsonl"
            payload: dict[str, Any] = {"run_id": "run-A"}
            persistence = run_persistence._RunPersistence()

            def record(phase: str, tip: str, turn: int) -> None:
                ref_observations.record_boundary(
                    {
                        "app": {
                            "git": True,
                            "head": "new",
                            "refs": [["refs/remotes/origin/main", tip]],
                        }
                    },
                    payload=payload,
                    persistence=persistence,
                    event_path=event_path,
                    phase=phase,
                    session_id=str(turn),
                    turn_index=turn,
                )

            record("turn-start", "old", 1)
            record("turn-end", "old", 1)
            record("turn-start", "new", 2)
            record("turn-end", "new", 2)
            record("turn-start", "new", 3)
            [event] = read_jsonl(event_path)
            self.assertEqual("inter_turn_ref_drift", event["name"])
            detail = event["payload"]
            self.assertEqual(
                (1, 2), (detail["previous_turn_index"], detail["turn_index"])
            )
            self.assertEqual("unknown", detail["writer"])
            [move] = detail["repositories"]["app"]
            self.assertEqual(("old", "new"), (move["before"], move["after"]))
            self.assertTrue(move["new_tip_is_local"])
            payload["run_id"] = "run-B"
            record("turn-start", "different", 1)
            self.assertEqual(1, len(read_jsonl(event_path)))
