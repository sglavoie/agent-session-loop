"""ControlCliTestsPart2.

Split from tests/test_asl_control.py."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_session_loop import (
    cli,
    control_cli,
    control_queue,
    plans,
)
from tests.asl_control_cli_support import ControlCliTestsBase
from tests.support import (
    _write_state_payload,
    write_plan,
)


class ControlCliTestsPart2(ControlCliTestsBase):
    def test_select_harness_invalid_harness_text_goes_to_stderr(self) -> None:
        write_plan(self.plan_dir, ("pending",))

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_select_harness_from_args(
                self.args(harness="gemini", json=False)
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn(
            "agent-session-loop control select-harness: unsupported harness",
            stderr.getvalue(),
        )

    def test_stop_after_current_queue_io_failure_is_one_json_error(self) -> None:
        write_plan(self.plan_dir, ("pending", "pending"))
        state_dir = self.unusable_state_dir()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args(state_dir=str(state_dir))
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())
        # Exactly one JSON object, so a supervisor can parse stdout whole.
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual("control-stop-after-current", payload["command"])
        self.assertEqual("os-error", payload["reason"])
        self.assertFalse(payload["paused"])
        self.assertEqual(
            str(
                control_queue.queue_path_for_plan(
                    plans.canonicalize_path(self.plan_dir), state_dir
                )
            ),
            payload["details"]["queue_path"],
        )
        self.assertIn("cannot write control queue", payload["error"])
        self.assertNotIn("Traceback", payload["error"])

    def test_select_harness_queue_io_failure_is_one_json_error(self) -> None:
        write_plan(self.plan_dir, ("pending",))
        state_dir = self.unusable_state_dir()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_select_harness_from_args(
                self.args(state_dir=str(state_dir), harness="claude")
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual("control-select-harness", payload["command"])
        self.assertEqual("os-error", payload["reason"])
        self.assertIn("cannot write control queue", payload["error"])

    def test_queue_io_failure_text_output_is_one_stderr_line(self) -> None:
        write_plan(self.plan_dir, ("pending",))
        state_dir = self.unusable_state_dir()

        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args(state_dir=str(state_dir), json=False)
            )

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        lines = stderr.getvalue().splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn(
            "agent-session-loop control stop-after-current: cannot write control queue",
            lines[0],
        )
        self.assertIn("check that the state directory is writable", lines[0])

    def test_malformed_plan_reference_mirrors_status_error(self) -> None:
        broken = self.tmp / "broken-plan"
        broken.mkdir()

        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args(broken),
        )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("control-stop-after-current", payload["command"])
        self.assertEqual("malformed-plan", payload["reason"])
        self.assertEqual(str(broken), payload["plan_reference"])
        self.assertNotIn("plan_dir", payload)
        self.assertEqual({}, payload["details"])
        self.assertFalse(self.queue_path(broken).exists())

    def test_ambiguous_plan_label_mirrors_status_error(self) -> None:
        first = self.tmp / "a" / "dup-label"
        second = self.tmp / "b" / "dup-label"
        write_plan(first, ("pending",))
        write_plan(second, ("pending",))
        for candidate in (first, second):
            _write_state_payload(
                candidate,
                self.state_dir,
                {"plan_dir": str(plans.canonicalize_path(candidate))},
            )

        exit_code, payload = self.payload_from(
            control_cli.control_stop_after_current_from_args,
            self.args("dup-label"),
        )

        self.assertEqual(1, exit_code)
        self.assertEqual("ambiguous-plan", payload["reason"])
        self.assertEqual("dup-label", payload["plan_reference"])
        self.assertNotIn("plan_dir", payload)
        self.assertEqual(
            sorted(
                [
                    str(plans.canonicalize_path(first)),
                    str(plans.canonicalize_path(second)),
                ]
            ),
            payload["details"]["candidates"],
        )
        self.assertEqual(
            str(plans.canonicalize_path(self.state_dir)),
            payload["details"]["state_root"],
        )
        self.assertEqual(
            (), control_queue.drain_queue(first, state_dir=self.state_dir).accepted
        )

    def test_ambiguous_plan_label_text_instructs_full_path(self) -> None:
        first = self.tmp / "a" / "dup-label"
        second = self.tmp / "b" / "dup-label"
        write_plan(first, ("pending",))
        write_plan(second, ("pending",))
        for candidate in (first, second):
            _write_state_payload(
                candidate,
                self.state_dir,
                {"plan_dir": str(plans.canonicalize_path(candidate))},
            )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = control_cli.control_stop_after_current_from_args(
                self.args("dup-label", json=False)
            )

        self.assertEqual(1, exit_code)
        self.assertIn(
            "pass a full plan directory path to disambiguate", stderr.getvalue()
        )

    def test_cli_registers_control_subcommands_with_lazy_handlers(self) -> None:
        parser = cli.build_parser()

        stop_args = parser.parse_args(
            [
                "control",
                "stop-after-current",
                "/tmp/plan",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )
        self.assertEqual("control", stop_args.command)
        self.assertEqual("stop-after-current", stop_args.control_command)
        self.assertEqual("/tmp/plan", stop_args.plan_dir)
        self.assertEqual("/tmp/state", stop_args.state_dir)
        self.assertTrue(stop_args.json)
        self.assertEqual(
            "control_cli:control_stop_after_current_from_args",
            stop_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(stop_args),
            control_cli.control_stop_after_current_from_args,
        )

        abort_args = parser.parse_args(
            [
                "control",
                "abort",
                "/tmp/plan",
                "--grace",
                "30",
                "--reason",
                "supervisor stop",
            ]
        )
        self.assertEqual("abort", abort_args.control_command)
        self.assertEqual("/tmp/plan", abort_args.plan_dir)
        self.assertEqual(30, abort_args.grace)
        self.assertEqual("supervisor stop", abort_args.reason)
        self.assertEqual(
            "control_cli:control_abort_from_args",
            abort_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(abort_args),
            control_cli.control_abort_from_args,
        )

        harness_args = parser.parse_args(
            ["control", "select-harness", "/tmp/plan", "--harness", "claude"]
        )
        self.assertEqual("select-harness", harness_args.control_command)
        self.assertEqual("claude", harness_args.harness)
        self.assertFalse(harness_args.json)
        self.assertIsNone(harness_args.state_dir)
        self.assertEqual(
            "control_cli:control_select_harness_from_args",
            harness_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(harness_args),
            control_cli.control_select_harness_from_args,
        )

        model_args = parser.parse_args(
            [
                "control",
                "set-model",
                "/tmp/plan",
                "--harness",
                "copilot",
                "--clear-harness-model",
                "--effort",
                "high",
            ]
        )
        self.assertEqual("set-model", model_args.control_command)
        self.assertEqual("copilot", model_args.harness)
        self.assertIsNone(model_args.harness_model)
        self.assertTrue(model_args.clear_harness_model)
        self.assertEqual("high", model_args.effort)
        self.assertFalse(model_args.clear_effort)
        self.assertEqual(
            "control_cli:control_set_model_from_args",
            model_args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(model_args),
            control_cli.control_set_model_from_args,
        )

    def test_cli_control_group_requires_a_subcommand(self) -> None:
        parser = cli.build_parser()

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["control"])

        self.assertEqual(2, raised.exception.code)

    def test_cli_select_harness_requires_known_harness_choice(self) -> None:
        parser = cli.build_parser()

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                ["control", "select-harness", "/tmp/plan", "--harness", "gemini"]
            )

        self.assertEqual(2, raised.exception.code)

    def test_cli_model_set_and_clear_flags_are_mutually_exclusive(self) -> None:
        parser = cli.build_parser()
        cases = (
            (
                "--harness-model",
                "gpt-5.4",
                "--clear-harness-model",
            ),
            ("--effort", "high", "--clear-effort"),
        )
        for operations in cases:
            with (
                self.subTest(operations=operations),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                parser.parse_args(["control", "set-model", "/tmp/plan", *operations])
            self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
