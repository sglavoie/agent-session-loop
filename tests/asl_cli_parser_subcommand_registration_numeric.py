"""NumericFlagValidationTests: numeric."""

from __future__ import annotations

from .asl_cli_parser_subcommand_registration_support import (
    NumericFlagValidationTestsBase,
    cli,
    io,
    redirect_stderr,
    redirect_stdout,
    tempfile,
)


class NumericFlagValidationTests(NumericFlagValidationTestsBase):
    def test_bad_numeric_values_are_refused_with_the_flag_named(self) -> None:
        cases = (
            (
                ["overseer", "watch", "--watcher-id", "w", "--for-seconds", "nan"],
                "--for-seconds",
                "non-negative",
            ),
            (
                ["overseer", "watch", "--watcher-id", "w", "--lease-seconds", "0"],
                "--lease-seconds",
                "positive",
            ),
            (
                ["overseer", "scan", "--pause-window-hours", "nan"],
                "--pause-window-hours",
                "non-negative",
            ),
            (
                ["overseer", "scan", "--pause-window-hours", "-1"],
                "--pause-window-hours",
                "non-negative",
            ),
            (
                ["overseer", "inspect", "/tmp/plan", "--incident-id", "0"],
                "--incident-id",
                "positive",
            ),
        )
        for argv, flag, requirement in cases:
            with self.subTest(argv=argv):
                text = self._refused(argv)
                self.assertIn(f"argument {flag}: must be", text)
                self.assertIn(requirement, text)
                self.assertNotIn("Traceback", text)

    def test_handler_validated_flags_read_like_argparse_refusals(self) -> None:
        """Flags a handler checks keep their JSON reason and argparse's wording.

        Each echoes what the user typed, so ``--stale-days -1`` no longer reads
        back as ``-1.0``, and the requirement is the shared phrase.
        """
        cases = (
            (
                ["metrics", "--since-days", "-1"],
                "--since-days must be a positive integer; got '-1'",
            ),
            (
                ["gc", "--max-age-days", "-5"],
                "--max-age-days must be a finite, non-negative number; got '-5'",
            ),
            (
                ["plans", "--stale-days", "-1"],
                "--stale-days must be a finite, non-negative number; got '-1'",
            ),
            (
                ["plans", "--stale-days", "-1.5"],
                "--stale-days must be a finite, non-negative number; got '-1.5'",
            ),
            (
                ["sessions", "--limit", "-1"],
                "--limit must be a positive integer; got '-1'",
            ),
            (
                ["maintenance", "install", "--hour", "25"],
                "--hour must be an integer from 0 to 23; got '25'",
            ),
            (
                ["maintenance", "install", "--minute", "-1"],
                "--minute must be an integer from 0 to 59; got '-1'",
            ),
            (
                ["maintenance", "install", "--minute", "60"],
                "--minute must be an integer from 0 to 59; got '60'",
            ),
            (
                ["maintenance", "run", "--max-runtime-seconds", "nan"],
                "--max-runtime-seconds must be a finite, positive number; got 'nan'",
            ),
            (
                ["maintenance", "install", "--max-runtime-seconds", "0"],
                "--max-runtime-seconds must be a finite, positive number; got '0'",
            ),
            (
                ["maintenance", "run", "--max-age-days", "inf"],
                "--max-age-days must be a finite, non-negative number; got 'inf'",
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for argv, message in cases:
                with self.subTest(argv=argv):
                    stderr = io.StringIO()
                    with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
                        code = cli.main([*argv, "--state-dir", tmp])
                    self.assertEqual(1, code)
                    self.assertTrue(stderr.getvalue().rstrip().endswith(message))

    def test_in_range_values_still_parse(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            ["maintenance", "install", "--hour", "23", "--minute", "0"]
        )
        self.assertEqual((23, 0), (args.hour, args.minute))
        self.assertEqual(
            0.0, parser.parse_args(["gc", "--max-age-days", "0"]).max_age_days
        )
        self.assertEqual(
            2.5,
            parser.parse_args(
                ["baseline", "/tmp/plan", "--per-check-timeout", "2.5"]
            ).per_check_timeout,
        )

    def test_group_without_a_leaf_names_the_command_placeholder(self) -> None:
        groups = (
            "control",
            "inbox",
            "outbox",
            "overseer",
            "assurance",
            "maintenance",
            "bridge",
            "agent-mux",
            "diagnostics",
        )
        for group in groups:
            with self.subTest(group=group):
                text = self._refused([group])
                self.assertIn("the following arguments are required: COMMAND", text)
                self.assertNotIn("_command", text)
        text = self._refused(["control", "guard"])
        self.assertIn("the following arguments are required: COMMAND", text)

    def test_overseer_raises_help_lists_the_valid_topics(self) -> None:
        for leaf in ("decide", "act"):
            stdout = io.StringIO()
            with (
                self.subTest(leaf=leaf),
                redirect_stdout(stdout),
                self.assertRaises(SystemExit),
            ):
                cli.build_parser(prog="asl").parse_args(["overseer", leaf, "--help"])
            help_text = " ".join(stdout.getvalue().split())
            self.assertIn(
                "TOPIC is one of: acceptance, scope, design, safety", help_text
            )

    def test_overseer_help_documents_its_exit_status_split(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            cli.build_parser(prog="asl").parse_args(["overseer", "--help"])
        help_text = " ".join(stdout.getvalue().split())
        self.assertIn("`plan_unobserved`", help_text)
        self.assertIn("`plan-not-found` exit 1", help_text)

    def test_ask_accepts_its_body_on_stdin(self) -> None:
        args = cli.build_parser().parse_args(["ask", "/tmp/plan", "--text-stdin"])
        self.assertTrue(args.text_stdin)
        self.assertIsNone(args.text)
