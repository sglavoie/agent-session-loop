"""Shared fixtures for test_asl_pause_ux."""

from __future__ import annotations

import argparse
import json
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    state,
)
from tests.support import (
    init_git_repo,
    write_plan,
)


class ArtifactSafetyAndPauseUxCase(unittest.TestCase):
    def _seed_paused_plan(
        self,
        tmp_dir: Path,
        *,
        last_message_text: str = "Should we use 1a or 1b?\n",
        statuses: tuple[str, ...] = ("pending", "pending"),
        paused_session_id: str = "1",
        pause_reason: str = "runner-pause",
        pause_message: str = "agent asked a question",
    ) -> tuple[Path, Path, Path]:
        init_git_repo(tmp_dir)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, statuses)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        last_message_path = tmp_dir / "last-message.txt"
        last_message_path.write_text(last_message_text, encoding="utf-8")
        turn = {
            "run_id": "run-prev",
            "turn_index": 2,
            "session_id": paused_session_id,
            "session": int(paused_session_id),
            "session_number": int(paused_session_id),
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "effort": "high",
            "effort_source": "flag",
            "artifact_dir": str(plan_state / "runs" / "run-prev" / "turn-0002"),
            "stdout_path": str(plan_state / "stdout.log"),
            "stderr_path": str(plan_state / "stderr.log"),
            "last_message_path": str(last_message_path),
            "result_path": str(plan_state / "result.json"),
        }
        payload = {
            "ok": False,
            "paused": True,
            "reason": pause_reason,
            "pause_reason": pause_reason,
            "pause_message": pause_message,
            "pause_details": {"session": int(paused_session_id)},
            "details": {"session": int(paused_session_id)},
            "run_id": "run-prev",
            "run_dir": str(plan_state / "runs" / "run-prev"),
            "harness": "codex",
            "model": "gpt-5",
            "effort": "high",
            "turns": [turn],
        }
        state_path.write_text(json.dumps(payload), encoding="utf-8")
        return plan_dir, state_dir, last_message_path

    def _paused_args(
        self,
        plan_dir: Path,
        state_dir: Path | None,
    ) -> argparse.Namespace:
        return argparse.Namespace(
            plan_dir=str(plan_dir),
            state_dir=str(state_dir) if state_dir is not None else None,
        )

    def _seed_lost_conversation(self, tmp_dir: Path) -> tuple[Path, Path]:
        plan_dir, state_dir, _ = self._seed_paused_plan(
            tmp_dir,
            pause_reason=events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED,
            pause_message="the recorded conversation can no longer be selected",
        )
        return plan_dir, state_dir
