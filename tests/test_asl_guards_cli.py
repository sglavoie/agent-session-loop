"""Tests for test asl guards cli."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    control_cli,
    control_queue,
    events,
    guards,
    intervention,
    state,
    state_status,
)
from tests.support import _write_lock_file, _write_state_payload, write_plan


class GuardCliTests(unittest.TestCase):
    """The operator surface: refuses without a run, waits for enforcement."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.root / "state"

    def args(self, **overrides: Any) -> Any:
        namespace = {
            "plan_dir": str(self.plan_dir),
            "state_dir": str(self.state_dir),
            "json": True,
            "wait": False,
            "wait_timeout": None,
            "deny_command": None,
            "protect_path": None,
            "reason": None,
            "id": None,
        }
        namespace.update(overrides)
        return type("Args", (), namespace)()

    def activate_run(self, run_id: str = "run-1") -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {"run_id": run_id, "started_at": "2026-01-01T00:00:00+00:00"},
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())

    def capture(self, handler, args) -> tuple[int, dict[str, Any]]:
        import contextlib
        import io as _io

        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = handler(args)
        return code, json.loads(buffer.getvalue())

    def test_add_without_a_rule_is_refused_before_anything_is_written(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args, self.args()
        )
        self.assertEqual(1, code)
        self.assertEqual(control_queue.REJECT_MISSING_GUARD_RULE, payload["reason"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_add_names_the_rule_that_is_malformed(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["/bin/git push"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_EXECUTABLE_NOT_BARE_NAME, payload["reason"])

    def test_add_without_an_active_run_is_refused(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["git push"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, payload["reason"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_remove_without_an_active_run_is_refused(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_remove_from_args,
            self.args(id="g-abc"),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_NO_ACTIVE_RUN, payload["reason"])

    def test_add_enqueues_with_the_active_run_id(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(deny_command=["git push origin main"], reason="protect"),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual("run-1", payload["run_id"])
        self.assertEqual([["git", "push", "origin", "main"]], payload["deny_commands"])
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        self.assertEqual("run-1", drained.accepted[0].expected_run_id)

    def test_add_enqueues_protected_paths_alongside_command_rules(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(
                deny_command=["git push"],
                protect_path=[".:docs/**/*.md", ".:README.md"],
                reason="frozen",
            ),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual([["git", "push"]], payload["deny_commands"])
        self.assertEqual([".:docs/**/*.md", ".:README.md"], payload["protect_paths"])
        drained = control_queue.drain_queue(self.plan_dir, state_dir=self.state_dir)
        queued = drained.accepted[0]
        self.assertEqual(
            [".:docs/**/*.md", ".:README.md"],
            [path.rendered for path in queued.protect_paths],
        )

    def test_add_with_only_a_protected_path_is_accepted(self) -> None:
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(protect_path=[".:README.md"]),
        )
        self.assertEqual(0, code, payload)
        self.assertEqual([], payload["deny_commands"])

    def test_add_names_the_protected_path_that_is_malformed(self) -> None:
        cases = {
            ".:/etc/passwd": guards.REJECT_PATH_ABSOLUTE,
            # No qualifier at all is the more specific complaint, even when the
            # path is also absolute: the operator has to name a repository
            # before the path can mean anything.
            "/etc/passwd": guards.REJECT_PATH_MISSING_QUALIFIER,
            "docs/api.md": guards.REJECT_PATH_MISSING_QUALIFIER,
            ".:../escape": guards.REJECT_PATH_TRAVERSAL,
            ".:docs\\api.md": guards.REJECT_PATH_BACKSLASH,
        }
        for rule, reason in cases.items():
            with self.subTest(rule=rule):
                code, payload = self.capture(
                    control_cli.control_guard_add_from_args,
                    self.args(protect_path=[rule]),
                )
                self.assertEqual(1, code)
                self.assertEqual(reason, payload["reason"])
                self.assertEqual(rule, payload["details"]["protect_path"])

    def test_a_legacy_plan_refuses_an_alias_it_does_not_have(self) -> None:
        """The alias half is checked against the plan the operator named."""
        self.activate_run()
        code, payload = self.capture(
            control_cli.control_guard_add_from_args,
            self.args(protect_path=["App:docs/api.md"]),
        )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_PATH_UNKNOWN_ALIAS, payload["reason"])
        self.assertEqual(["."], payload["details"]["known_qualifiers"])
        queue_path = control_queue.queue_path_for_plan(self.plan_dir, self.state_dir)
        self.assertFalse(queue_path.exists())

    def test_a_registered_plan_takes_its_declared_aliases(self) -> None:
        self.assertEqual(
            ("App", "Docs"),
            control_cli.plan_path_qualifiers(
                {"repositories": [{"alias": "App"}, {"alias": "Docs"}]}
            ),
        )
        self.assertEqual(
            (guards.LEGACY_PATH_QUALIFIER,),
            control_cli.plan_path_qualifiers({"repositories": []}),
        )

    def test_list_reports_the_active_set_and_the_coverage_statement(self) -> None:
        self.activate_run()
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "control": {
                    "guards": [{"id": "g-1", "command": ["git", "push"]}],
                },
            },
        )
        code, payload = self.capture(
            control_cli.control_guard_list_from_args, self.args()
        )
        self.assertEqual(0, code)
        self.assertTrue(payload["run_active"])
        self.assertEqual("g-1", payload["guards"][0]["id"])
        self.assertEqual(
            guards.COMMAND_GATE_COVERAGE["denied_exit_code"],
            payload["command_gate"]["denied_exit_code"],
        )

    def test_list_without_a_run_reports_no_enforcement(self) -> None:
        code, payload = self.capture(
            control_cli.control_guard_list_from_args, self.args()
        )
        self.assertEqual(0, code)
        self.assertFalse(payload["run_active"])
        self.assertEqual([], payload["guards"])

    def test_wait_resolves_on_the_matching_guard_outcome(self) -> None:
        self.activate_run()
        events_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

        def fake_dispatch(*_args: Any, **kwargs: Any) -> Any:
            command = control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_ADD_GUARD,
                state_dir=self.state_dir,
                deny_commands=list(kwargs["deny_commands"]),
                expected_run_id=kwargs["expected_run_id"],
            )
            with open(events_path, "a", encoding="utf-8") as handle:
                for action, command_id in (
                    (
                        events.RUN_CONTROL_ACTION_COMMAND_OBSERVED,
                        command.id,
                    ),
                    # A concurrent operator's guard must not answer this wait.
                    (events.RUN_CONTROL_ACTION_GUARD_APPLIED, "other-command"),
                    (events.RUN_CONTROL_ACTION_GUARD_APPLIED, command.id),
                ):
                    handle.write(
                        json.dumps(
                            {
                                "name": events.RUN_CONTROL_EVENT,
                                "payload": {
                                    "action": action,
                                    "command_id": command_id,
                                    "guard_ids": ["g-1"],
                                },
                            }
                        )
                        + "\n"
                    )
            return intervention.ControlDispatch(
                command=command,
                queue_path=control_queue.queue_path_for_plan(
                    self.plan_dir, self.state_dir
                ),
                signal=control_cli.control_signal.NotifyResult(
                    delivered=False, reason="no-runner"
                ),
            )

        with mock.patch.object(
            control_cli.intervention, "dispatch_control_command", fake_dispatch
        ):
            code, payload = self.capture(
                control_cli.control_guard_add_from_args,
                self.args(deny_command=["git push"], wait=True, wait_timeout=5.0),
            )
        self.assertEqual(0, code, payload)
        self.assertEqual(
            events.RUN_CONTROL_ACTION_GUARD_APPLIED, payload["wait"]["terminal_action"]
        )
        # A guard outcome is final on its own; it must not be reported as a
        # run that is still finalizing.
        self.assertNotIn("note", payload["wait"])

    def test_wait_reports_a_refusal_as_a_failure(self) -> None:
        self.activate_run()
        events_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )

        def fake_dispatch(*_args: Any, **kwargs: Any) -> Any:
            command = control_queue.append_command(
                self.plan_dir,
                control_queue.COMMAND_ADD_GUARD,
                state_dir=self.state_dir,
                deny_commands=list(kwargs["deny_commands"]),
                expected_run_id=kwargs["expected_run_id"],
            )
            with open(events_path, "a", encoding="utf-8") as handle:
                for action, extra in (
                    (events.RUN_CONTROL_ACTION_COMMAND_OBSERVED, {}),
                    (
                        events.RUN_CONTROL_ACTION_GUARD_REJECTED,
                        {"reason": guards.REJECT_RUN_CHANGED, "detail": "run-0"},
                    ),
                ):
                    handle.write(
                        json.dumps(
                            {
                                "name": events.RUN_CONTROL_EVENT,
                                "payload": {
                                    "action": action,
                                    "command_id": command.id,
                                    **extra,
                                },
                            }
                        )
                        + "\n"
                    )
            return intervention.ControlDispatch(
                command=command,
                queue_path=control_queue.queue_path_for_plan(
                    self.plan_dir, self.state_dir
                ),
                signal=control_cli.control_signal.NotifyResult(
                    delivered=False, reason="no-runner"
                ),
            )

        with mock.patch.object(
            control_cli.intervention, "dispatch_control_command", fake_dispatch
        ):
            code, payload = self.capture(
                control_cli.control_guard_add_from_args,
                self.args(deny_command=["git push"], wait=True, wait_timeout=5.0),
            )
        self.assertEqual(1, code)
        self.assertEqual(guards.REJECT_RUN_CHANGED, payload["reason"])


class GuardStatusTests(unittest.TestCase):
    """Status reports the gate a live run is enforcing, and nothing more."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, ("pending",))
        self.state_dir = self.root / "state"

    def status(self) -> dict[str, Any]:
        from agent_session_loop import plans_snapshot

        return state_status.status_payload(
            plans_snapshot.inspect_plan(self.plan_dir), self.state_dir
        )

    def test_no_run_reports_an_empty_gate_with_its_coverage(self) -> None:
        payload = self.status()
        self.assertEqual([], payload["command_guards"]["active"])
        self.assertEqual(0, payload["command_guards"]["count"])
        self.assertEqual(
            guards.COMMAND_GATE_COVERAGE["mechanism"],
            payload["command_guards"]["coverage"]["mechanism"],
        )

    def test_active_run_reports_its_guards(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "control": {
                    "guards": [
                        {
                            "id": "g-1",
                            "command": ["git", "push"],
                            "rendered_command": "git push",
                        }
                    ]
                },
            },
        )
        _write_lock_file(self.plan_dir, self.state_dir, pid=os.getpid())
        payload = self.status()
        self.assertEqual(1, payload["command_guards"]["count"])
        self.assertEqual("g-1", payload["command_guards"]["active"][0]["id"])

    def test_a_finished_run_reports_no_enforcement(self) -> None:
        _write_state_payload(
            self.plan_dir,
            self.state_dir,
            {
                "run_id": "run-1",
                "started_at": "2026-01-01T00:00:00+00:00",
                "run_finished_at": "2026-01-01T01:00:00+00:00",
                "control": {"guards": [{"id": "g-1", "command": ["git", "push"]}]},
            },
        )
        payload = self.status()
        self.assertEqual([], payload["command_guards"]["active"])


if __name__ == "__main__":
    unittest.main()
