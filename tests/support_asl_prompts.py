"""Shared task packet fixtures for the ASL prompt tests."""

from __future__ import annotations

from pathlib import Path

from agent_session_loop import (
    plans_files,
    qna,
)


class TaskPacketFixtureMixin:
    PLAN_DIR = "/tmp/plan"

    def _canon(self, name: str) -> str:
        return str(plans_files.canonicalize_path(self.PLAN_DIR) / name)

    def _make_reply_context(
        self,
        *,
        session_id: str = "9",
        pause_details: dict[str, object] | None = None,
    ) -> qna.QnAContext:
        return qna.QnAContext(
            plan_dir=Path(self.PLAN_DIR),
            state_dir=Path("/tmp/state"),
            state_path=Path("/tmp/state/state.json"),
            run_id="run-abc",
            run_dir=None,
            pause_reason="agent-question",
            pause_message="Need clarification on option A vs B.",
            pause_details=dict(pause_details) if pause_details else {},
            harness="codex",
            model=None,
            model_source=None,
            effort=None,
            effort_source=None,
            session_id=session_id,
            next_pending_id=session_id,
            turn_index=2,
            artifact_dir=None,
            stdout_path=None,
            stderr_path=None,
            last_message_path=Path("/tmp/last-message.txt"),
            result_path=None,
            last_message="Should I implement 1a, 1b, or skip?\n",
        )
