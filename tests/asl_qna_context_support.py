# ruff: noqa: F403, F405
from .test_asl_qna_shared import *


class _QnAContextFixture(unittest.TestCase):
    def _seed_plan(self, tmp_dir: Path) -> tuple[Path, Path, Path]:
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, ("pending", "pending"))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        state_path = plan_state / events.STATE_FILE_NAME
        return plan_dir, state_dir, state_path

    def _write_state(
        self,
        state_path: Path,
        *,
        paused: bool = True,
        session_id: str = "1",
        last_message_path: str | None = None,
        turn_overrides: dict[str, object] | None = None,
        payload_overrides: dict[str, object] | None = None,
        turns: list[dict[str, object]] | None = None,
    ) -> None:
        if last_message_path is None:
            last_message_path = str(state_path.parent / "last-message.txt")
        turn: dict[str, object] = {
            "run_id": "run-1",
            "turn_index": 1,
            "session_id": session_id,
            "session": 1,
            "session_number": 1,
            "harness": "fake",
            "model": "test-model",
            "model_source": "default",
            "effort": "medium",
            "effort_source": "default",
            "artifact_dir": str(state_path.parent / "turn-0001"),
            "stdout_path": str(state_path.parent / "stdout.log"),
            "stderr_path": str(state_path.parent / "stderr.log"),
            "last_message_path": last_message_path,
            "result_path": str(state_path.parent / "result.json"),
        }
        if turn_overrides:
            turn.update(turn_overrides)
        payload: dict[str, object] = {
            "ok": not paused,
            "paused": paused,
            "reason": "runner-pause",
            "pause_reason": "runner-pause",
            "pause_message": "agent asked a question",
            "pause_details": {"session": 1},
            "details": {"session": 1},
            "run_id": "run-1",
            "run_dir": str(state_path.parent / "runs" / "run-1"),
            "harness": "fake",
            "model": "test-model",
            "effort": "medium",
            "turns": [turn] if turns is None else turns,
        }
        if payload_overrides:
            payload.update(payload_overrides)
        state_path.write_text(json.dumps(payload), encoding="utf-8")

    def _write_last_message(self, path: str, text: str = "Question?\n") -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
