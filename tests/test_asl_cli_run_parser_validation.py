"""Run/doctor/prepare parser flags and run-input validation tests."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from agent_session_loop import (
    cli,
    harnesses,
    review_guard,
    run_flags,
    run_inputs,
    runner,
)


class RunParserValidationTests(unittest.TestCase):
    """Run/doctor/prepare parser flags and run-input validation tests."""

    def test_run_subparser_actions_match_run_flag_spec(self) -> None:
        # Tripwire: the real `asl run` subparser surface must equal the set the
        # declarative RUN_FLAG_SPEC describes, so a flag can never be added to
        # the CLI (or the helper's supervisor parser) without going through the
        # shared spec.
        parser = cli.build_parser()
        subparsers_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        run_subparser = subparsers_action.choices["run"]

        real = {
            (tuple(action.option_strings), action.dest)
            for action in run_subparser._actions
            if action.dest not in ("help", "handler")
        }
        expected = {
            (tuple(flag.option_strings), flag.dest) for flag in run_flags.RUN_FLAG_SPEC
        }

        self.assertEqual(expected, real)

    def test_run_parser_review_harness_defaults_to_none(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(["run", "/tmp/plan"])

        self.assertIsNone(args.review_harness)

    def test_run_parser_uses_role_specific_model_attributes(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "run",
                "/tmp/plan",
                "--harness-model",
                "gpt-5",
                "--review-harness-model",
                "claude-opus-4",
            ]
        )

        self.assertEqual("gpt-5", args.harness_model)
        self.assertEqual("claude-opus-4", args.review_harness_model)
        self.assertFalse(hasattr(args, "model"))

    def test_run_parser_rejects_legacy_model_flag(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "/tmp/plan", "--model", "gpt-5"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --model", stderr.getvalue())

    def test_prepare_parser_reserves_role_specific_model_forwarding(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "prepare",
                "/tmp/plan.md",
                "--harness-model",
                "gpt-5",
                "--review-harness-model",
                "claude-opus-4",
            ]
        )

        self.assertEqual("gpt-5", args.harness_model)
        self.assertEqual("claude-opus-4", args.review_harness_model)

    def test_doctor_parser_rejects_legacy_model_flag(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                [
                    "doctor",
                    "--harness",
                    "codex",
                    "--plan-dir",
                    "/tmp/plan",
                    "--model",
                    "gpt-5",
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --model", stderr.getvalue())

    def test_run_parser_accepts_review_harness_choices(self) -> None:
        parser = cli.build_parser()

        for harness_name in harnesses.HARNESS_NAMES:
            with self.subTest(harness=harness_name):
                args = parser.parse_args(
                    ["run", "/tmp/plan", "--review-harness", harness_name]
                )
                self.assertEqual(harness_name, args.review_harness)

    def test_run_parser_rejects_unknown_review_harness(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "/tmp/plan", "--review-harness", "not-a-harness"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("--review-harness", stderr.getvalue())

    def test_validate_run_inputs_rejects_review_harness_equal_to_harness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                review_harness="codex",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )
            with self.assertRaises(runner.RunError) as raised:
                run_inputs._validate_run_inputs(
                    args,
                    resolve_review_guard_excludes=review_guard._review_guard_excludes,
                )

            message = str(raised.exception)
            self.assertIn("--review-harness must differ from --harness", message)
            self.assertIn("'codex'", message)

    def test_validate_run_inputs_accepts_distinct_review_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                review_harness="claude",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )

            cwd, timeout, max_sessions, commit_mode = run_inputs._validate_run_inputs(
                args, resolve_review_guard_excludes=review_guard._review_guard_excludes
            )

            self.assertEqual(Path(tmp).resolve(), cwd)
            self.assertIsNone(timeout)
            self.assertIsNone(max_sessions)
            self.assertIsNone(commit_mode)

    def test_validate_run_inputs_requires_review_harness_for_review_model(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                review_harness=None,
                review_harness_model="claude-opus-4",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )

            with self.assertRaises(runner.RunError) as raised:
                run_inputs._validate_run_inputs(
                    args,
                    resolve_review_guard_excludes=review_guard._review_guard_excludes,
                )

        self.assertEqual(
            "--review-harness-model requires --review-harness",
            str(raised.exception),
        )

    def test_validate_run_inputs_allows_missing_review_harness_attribute(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                harness="codex",
                timeout=None,
                max_sessions=None,
                commit_mode=None,
            )

            run_inputs._validate_run_inputs(
                args, resolve_review_guard_excludes=review_guard._review_guard_excludes
            )


if __name__ == "__main__":
    unittest.main()
