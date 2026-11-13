"""ParserSubcommandRegistrationTests: flags."""

from __future__ import annotations

from .asl_cli_parser_subcommand_registration_support import (
    ParserSubcommandRegistrationTestsBase,
    argparse,
    cli,
    control_queue,
    io,
    mock,
    redirect_stderr,
)


class ParserSubcommandRegistrationTests(ParserSubcommandRegistrationTestsBase):
    def test_main_returns_interrupted_exit_on_keyboard_interrupt(self) -> None:
        def interrupted(_args: argparse.Namespace) -> int:
            raise KeyboardInterrupt

        with mock.patch.object(cli, "resolve_handler", return_value=interrupted):
            self.assertEqual(130, cli.main(["tail", "/tmp/plan"]))

    def test_control_wait_flags_are_available_on_every_subcommand(self) -> None:
        parser = cli.build_parser()
        subcommands = (
            ["control", "stop-after-current", "/tmp/plan"],
            ["control", "abort", "/tmp/plan"],
            ["control", "select-harness", "/tmp/plan", "--harness", "codex"],
            ["control", "set-model", "/tmp/plan", "--clear-effort"],
        )
        for argv in subcommands:
            with self.subTest(control_command=argv[1]):
                defaults = parser.parse_args(argv)

                self.assertFalse(defaults.wait)
                self.assertIsNone(defaults.wait_timeout)

                waiting = parser.parse_args([*argv, "--wait", "--timeout", "5"])

                self.assertTrue(waiting.wait)
                self.assertEqual(5.0, waiting.wait_timeout)

    def test_control_wait_timeout_must_be_a_number(self) -> None:
        parser = cli.build_parser()

        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                ["control", "abort", "/tmp/plan", "--wait", "--timeout", "soon"]
            )

        self.assertEqual(2, raised.exception.code)

    def test_control_abort_parser_defaults_and_overrides(self) -> None:
        parser = cli.build_parser()

        defaults = parser.parse_args(["control", "abort", "/tmp/plan"])

        self.assertEqual("control", defaults.command)
        self.assertEqual("abort", defaults.control_command)
        self.assertEqual("/tmp/plan", defaults.plan_dir)
        self.assertIsNone(defaults.state_dir)
        self.assertFalse(defaults.json)
        self.assertEqual(control_queue.DEFAULT_ABORT_GRACE_SECONDS, defaults.grace)
        self.assertIsNone(defaults.reason)
        self.assertEqual("control_cli:control_abort_from_args", defaults.handler_ref)

        overridden = parser.parse_args(
            [
                "control",
                "abort",
                "/tmp/plan",
                "--state-dir",
                "/tmp/state",
                "--json",
                "--grace",
                "15",
                "--reason",
                "supervisor stop",
            ]
        )

        self.assertEqual("/tmp/state", overridden.state_dir)
        self.assertTrue(overridden.json)
        self.assertEqual(15, overridden.grace)
        self.assertEqual("supervisor stop", overridden.reason)

    def test_control_abort_requires_a_plan_and_an_integer_grace(self) -> None:
        parser = cli.build_parser()

        for argv in (
            ["control", "abort"],
            ["control", "abort", "/tmp/plan", "--grace", "soon"],
        ):
            with self.subTest(argv=argv):
                with (
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as raised,
                ):
                    parser.parse_args(argv)
                self.assertEqual(2, raised.exception.code)

    def test_expand_ingest_requires_batch_file(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["expand-ingest", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)

    def test_review_verdict_requires_plan_dir(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["review-verdict"])

        self.assertEqual(2, raised.exception.code)

    def test_tail_interactive_flag_is_rejected(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["tail", "--interactive", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --interactive", stderr.getvalue())

    def test_unknown_flag_reports_the_matched_subcommand_usage(self) -> None:
        cases = (
            (["gc", "--bogus"], "asl gc"),
            (["status", "/tmp/plan", "extra"], "asl status"),
            (["control", "abort", "/tmp/plan", "--bogus"], "asl control abort"),
        )
        for argv, leaf in cases:
            stderr = io.StringIO()
            with (
                self.subTest(argv=argv),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.build_parser(prog="asl").parse_args(argv)

            self.assertEqual(2, raised.exception.code)
            text = stderr.getvalue()
            self.assertTrue(text.startswith(f"usage: {leaf} "), text)
            self.assertIn(f"{leaf}: error: unrecognized arguments:", text)
            self.assertNotIn("supervisor-guide", text)

    def test_doctor_accepts_plan_dir_positionally_or_by_alias(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["doctor", "/tmp/plan", "--harness", "codex"],
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"],
            ["doctor", "/tmp/plan", "--harness", "codex", "--plan-dir", "/tmp/plan/"],
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertIn(args.plan_dir, ("/tmp/plan", "/tmp/plan/"))
                self.assertFalse(hasattr(args, "_plan_dir_positional"))

    def test_doctor_refuses_a_missing_or_conflicting_plan_dir(self) -> None:
        cases = (
            (["doctor", "--harness", "codex"], "required: PLAN_DIR"),
            (
                ["doctor", "/tmp/a", "--harness", "codex", "--plan-dir", "/tmp/b"],
                "disagree",
            ),
        )
        for argv, expected in cases:
            stderr = io.StringIO()
            with (
                self.subTest(argv=argv),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.build_parser(prog="asl").parse_args(argv)

            self.assertEqual(2, raised.exception.code)
            self.assertIn("asl doctor: error:", stderr.getvalue())
            self.assertIn(expected, stderr.getvalue())

    def test_preflight_mark_loop_ready_short_flag(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(["preflight", "/tmp/plan", "-m"])

        self.assertTrue(args.mark_loop_ready)
