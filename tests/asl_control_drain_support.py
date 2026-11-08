"""ControlQueueDrainInRunnerTests.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from agent_session_loop import (
    control_queue,
    events,
    harnesses,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    run_persistence,
)
from tests.support import (
    read_jsonl,
)


class ControlQueueDrainInRunnerTestsBase(unittest.TestCase):
    """Validate the runner-side drain helper translates commands correctly."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.plan_dir = self.tmp / "plan"
        self.state_dir = self.tmp / "state"
        self.plan_dir.mkdir()
        (self.plan_dir / "manifest.md").write_text("# m\n", encoding="utf-8")
        (self.plan_dir / "context.md").write_text("# c\n", encoding="utf-8")

        self.payload: dict[str, object] = {
            "run_id": "test-run",
            "control": run_coordination_control.initial_control_payload(),
            "harness": "codex",
        }
        self.control_payload = dict(self.payload["control"])  # type: ignore[arg-type]
        self.event_path = self.state_dir / "events.jsonl"
        self.event_path.parent.mkdir(parents=True, exist_ok=True)

    def drain(
        self,
        control_state: run_control.RunControlState,
        seen_ids: set[str] | None = None,
        wakeup: threading.Event | None = None,
        abort: harnesses.AbortSignal | None = None,
        mailbox_enabled: bool = False,
        finalization: bool = False,
    ) -> tuple[control_queue.DrainResult, set[str]]:
        seen = set() if seen_ids is None else seen_ids
        result = run_coordination_control_drain.drain_external_control_queue(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=control_state,
            seen_ids=seen,
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            context=None,
            persistence=run_persistence._RunPersistence(),
            run_control_lock=threading.Lock(),
            wakeup=wakeup,
            abort=abort,
            mailbox_enabled=mailbox_enabled,
            finalization=finalization,
        )
        return result, seen

    def run_control_actions(self) -> list[str]:
        if not self.event_path.exists():
            return []
        return [
            record["payload"]["action"]
            for record in read_jsonl(self.event_path)
            if record["name"] == events.RUN_CONTROL_EVENT
        ]

    def queue_abort(
        self,
        *,
        command_id: str = "abort-1",
        grace_seconds: int | None = None,
        reason: str | None = None,
    ) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REQUEST_ABORT_TURN,
            state_dir=self.state_dir,
            grace_seconds=grace_seconds,
            reason=reason,
            command_id=command_id,
        )
