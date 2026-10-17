"""Tests for test asl guards queue."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_queue,
    events,
    guards,
    run_control,
    run_coordination_control,
    run_coordination_control_drain,
    state,
)
from tests import support_asl_guards as fixtures


class GuardControlQueueTests(unittest.TestCase):
    """Guard mutations are durable, validated commands like every other."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = self.root / "state"

    def test_add_guard_round_trips_through_the_queue(self) -> None:
        queued = control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_ADD_GUARD,
            state_dir=self.state_dir,
            deny_commands=["git push origin main"],
            expected_run_id="run-1",
            reason="protect main",
        )
        self.assertEqual((("git", "push", "origin", "main"),), queued.deny_commands)
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.rejected)
        command = drained.accepted[0]
        self.assertEqual(control_queue.COMMAND_ADD_GUARD, command.command)
        self.assertEqual((("git", "push", "origin", "main"),), command.deny_commands)
        self.assertEqual("run-1", command.expected_run_id)
        self.assertEqual("protect main", command.reason)

    def test_remove_guard_round_trips_through_the_queue(self) -> None:
        control_queue.append_command(
            self.plan_dir,
            control_queue.COMMAND_REMOVE_GUARD,
            state_dir=self.state_dir,
            guard_id="g-abc",
            expected_run_id="run-1",
        )
        command = control_queue.drain_queue(
            self.plan_dir, state_dir=self.state_dir
        ).accepted[0]
        self.assertEqual("g-abc", command.guard_id)
        self.assertEqual("run-1", command.expected_run_id)

    def test_append_rejects_malformed_guard_commands(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            (
                {"deny_commands": ["git push"]},
                control_queue.REJECT_MISSING_EXPECTED_RUN_ID,
            ),
            ({"expected_run_id": "run-1"}, control_queue.REJECT_MISSING_GUARD_RULE),
            (
                {"deny_commands": [], "expected_run_id": "run-1"},
                control_queue.REJECT_MISSING_GUARD_RULE,
            ),
            (
                {"deny_commands": ["/bin/git push"], "expected_run_id": "run-1"},
                guards.REJECT_EXECUTABLE_NOT_BARE_NAME,
            ),
            (
                {"deny_commands": ["git push && rm"], "expected_run_id": "run-1"},
                guards.REJECT_SHELL_OPERATOR,
            ),
        ]
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(control_queue.ControlQueueError) as caught:
                    control_queue.append_command(
                        self.plan_dir,
                        control_queue.COMMAND_ADD_GUARD,
                        state_dir=self.state_dir,
                        **kwargs,
                    )
                self.assertEqual(reason, str(caught.exception))

    def test_remove_without_a_guard_id_is_rejected(self) -> None:
        with self.assertRaises(control_queue.ControlQueueError) as caught:
            control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_REMOVE_GUARD,
                state_dir=self.state_dir,
                expected_run_id="run-1",
            )
        self.assertEqual(control_queue.REJECT_MISSING_GUARD_ID, str(caught.exception))

    def test_drain_rejects_a_hand_written_line_with_the_same_reason(self) -> None:
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(
            json.dumps(
                {
                    "id": "c-1",
                    "command": control_queue.COMMAND_ADD_GUARD,
                    "deny_commands": ["git push"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual((), drained.accepted)
        self.assertEqual(
            control_queue.REJECT_MISSING_EXPECTED_RUN_ID, drained.rejected[0].reason
        )


class GuardDrainTests(unittest.TestCase):
    """The runner applies guard commands only to the run they name."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        fixtures.install_fake_git(self.bin_dir)
        self.plan_dir = self.root / "plan"
        self.plan_dir.mkdir()
        self.state_dir = self.root / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.plan_state.mkdir(parents=True, exist_ok=True)
        self.event_path = self.plan_state / events.EVENTS_FILE_NAME
        self.payload: dict[str, Any] = {"run_id": "run-1"}
        self.control_payload = run_coordination_control.initial_control_payload()
        self.persistence = fixtures._RecordingPersistence(self.append_event)
        self.controller = guards.GuardController(
            run_id="run-1",
            run_dir=self.plan_state / "runs" / "run-1",
            path_value=str(self.bin_dir),
        )

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"name": name, "payload": payload}) + "\n")

    def guard_events(self) -> list[dict[str, Any]]:
        if not self.event_path.exists():
            return []
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return [
            record["payload"]
            for record in records
            if record["payload"].get("action") in events.GUARD_TERMINAL_ACTIONS
        ]

    def drain(self) -> None:
        run_coordination_control_drain.drain_external_control_queue(
            plan_dir=self.plan_dir,
            state_dir_arg=str(self.state_dir),
            control_state=run_control.RunControlState(),
            seen_ids=set(),
            payload=self.payload,
            event_path=self.event_path,
            control_payload=self.control_payload,
            context=None,
            persistence=self.persistence,
            run_control_lock=mock.MagicMock(),
            guard_controller=self.controller,
        )

    def queue(self, **kwargs: Any) -> None:
        control_queue.append_command(
            self.plan_dir,
            kwargs.pop("command"),
            state_dir=self.state_dir,
            **kwargs,
        )

    def test_guard_add_is_applied_and_recorded(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push origin main"],
            expected_run_id="run-1",
        )
        self.drain()
        recorded = self.guard_events()
        self.assertEqual(1, len(recorded))
        self.assertEqual(events.RUN_CONTROL_ACTION_GUARD_APPLIED, recorded[0]["action"])
        self.assertEqual(1, len(self.controller.active()))
        self.assertEqual(1, len(self.control_payload["guards"]))
        self.assertEqual(
            self.control_payload["guards"], self.payload["control"]["guards"]
        )

    def test_guard_for_a_different_run_is_rejected_at_drain(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push"],
            expected_run_id="run-0",
        )
        self.drain()
        recorded = self.guard_events()
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_REJECTED, recorded[0]["action"]
        )
        self.assertEqual(guards.REJECT_RUN_CHANGED, recorded[0]["reason"])
        self.assertEqual((), self.controller.active())

    def test_guard_command_without_a_controller_is_rejected(self) -> None:
        self.queue(
            command=control_queue.COMMAND_ADD_GUARD,
            deny_commands=["git push"],
            expected_run_id="run-1",
        )
        self.controller = None  # type: ignore[assignment]
        self.drain()
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, self.guard_events()[0]["reason"])

    def test_guard_remove_is_applied_and_recorded(self) -> None:
        applied = self.controller.add([("git", "push")])
        self.queue(
            command=control_queue.COMMAND_REMOVE_GUARD,
            guard_id=applied.guard_ids[0],
            expected_run_id="run-1",
        )
        self.drain()
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_REMOVED, self.guard_events()[0]["action"]
        )
        self.assertEqual((), self.controller.active())
        self.assertEqual([], self.control_payload["guards"])

    def test_removing_an_unknown_guard_is_rejected(self) -> None:
        self.queue(
            command=control_queue.COMMAND_REMOVE_GUARD,
            guard_id="g-nope",
            expected_run_id="run-1",
        )
        self.drain()
        self.assertEqual(guards.REJECT_UNKNOWN_GUARD, self.guard_events()[0]["reason"])

    def test_expiry_records_the_guards_it_ended(self) -> None:
        self.controller.add([("git", "push")])
        run_coordination_control.expire_run_guards(
            controller=self.controller,
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            persistence=self.persistence,
        )
        records = [
            json.loads(line)
            for line in self.event_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        expired = [
            record["payload"]
            for record in records
            if record["payload"].get("action")
            == events.RUN_CONTROL_ACTION_GUARDS_EXPIRED
        ]
        self.assertEqual(1, len(expired))
        self.assertEqual(1, len(expired[0]["guard_ids"]))
        self.assertEqual([], self.control_payload["guards"])

    def test_expiry_of_an_empty_gate_records_nothing(self) -> None:
        run_coordination_control.expire_run_guards(
            controller=self.controller,
            event_path=self.event_path,
            payload=self.payload,
            control_payload=self.control_payload,
            persistence=self.persistence,
        )
        self.assertFalse(self.event_path.exists())


if __name__ == "__main__":
    unittest.main()
