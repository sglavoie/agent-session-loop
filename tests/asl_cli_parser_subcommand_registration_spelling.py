"""ParserFlagSpellingTests: spelling."""

from __future__ import annotations

from .asl_cli_parser_subcommand_registration_support import (
    ParserFlagSpellingTestsBase,
    cli,
    io,
    redirect_stdout,
)


class ParserFlagSpellingTests(ParserFlagSpellingTestsBase):
    def test_unique_prefix_of_a_long_option_is_refused(self) -> None:
        for argv, prefix in (
            (["gc", "--app"], "--app"),
            (["unlock", "/tmp/plan", "--for"], "--for"),
            (["status", "/tmp/plan", "--js"], "--js"),
        ):
            with self.subTest(argv=argv):
                self.assertIn(
                    f"unrecognized arguments: {prefix}", self._parse_error(argv)
                )

    def test_wait_timeout_spellings_are_aliases_everywhere(self) -> None:
        parser = cli.build_parser(prog="asl")
        cases = (
            (["inbox", "guidance", "/p", "--text", "t"], "wait_timeout"),
            (["inbox", "reply", "/p", "--to", "q", "--text", "t"], "wait_timeout"),
            (
                ["inbox", "resolve", "/p", "--to", "q", "--action", "continue"],
                "wait_timeout",
            ),
            (["ask", "/p", "text"], "wait_timeout"),
            (["control", "stop-after-current", "/p"], "wait_timeout"),
            (["outbox", "wait", "/p"], "timeout"),
            (
                ["outbox", "supervise", "/p", "--supervision-id", "s"],
                "timeout",
            ),
            (["overseer", "watch", "--watcher-id", "w"], "timeout"),
        )
        for argv, dest in cases:
            for flag in ("--timeout", "--wait-timeout"):
                with self.subTest(argv=argv, flag=flag):
                    args = parser.parse_args([*argv, flag, "7"])
                    self.assertEqual(7.0, getattr(args, dest))

    def test_wait_timeout_help_shows_both_spellings_with_seconds(self) -> None:
        for argv in (
            ["inbox", "reply"],
            ["outbox", "wait"],
            ["overseer", "act"],
        ):
            parser = cli.build_parser(prog="asl")
            stdout = io.StringIO()
            with (
                redirect_stdout(stdout),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args([*argv, "--help"])
            help_text = " ".join(stdout.getvalue().split())
            with self.subTest(argv=argv):
                self.assertIn("--timeout SECONDS", help_text)
                self.assertIn("--wait-timeout SECONDS", help_text)
                self.assertNotIn("TIMEOUT,", help_text)

    def test_help_names_values_by_kind_not_by_dest(self) -> None:
        cases = (
            (["notify"], ("TEXT",), ()),
            (["ask"], ("TEXT",), ()),
            (["prepare"], ("PLAN", "--cwd DIR", "--validator-jobs N"), ("CWD",)),
            (
                ["split"],
                ("--plan-file PATH", "--base-dir DIR", "--cwd DIR"),
                ("PLAN_FILE", "BASE_DIR"),
            ),
            (["register-repositories"], ("--output PATH",), ("OUTPUT",)),
            (["inbox", "resolve"], ("--note TEXT",), ("NOTE",)),
            (["tail"], ("--lines N", "(default: 40)"), ("LINES",)),
            (
                ["run"],
                (
                    "--timeout SECONDS",
                    "--audit-timeout SECONDS",
                    "--audit-harness-model MODEL",
                    "--note TEXT",
                ),
                ("AUDIT_TIMEOUT", "AUDIT_HARNESS_MODEL", "NOTE"),
            ),
        )
        for argv, present, absent in cases:
            help_text = self._help(argv)
            with self.subTest(argv=argv):
                for text in present:
                    self.assertIn(text, help_text)
                for text in absent:
                    self.assertNotIn(text, help_text)

    def test_wait_timeout_help_names_the_flag_it_needs(self) -> None:
        for argv, needed in (
            (["inbox", "reply"], "--wait-consumed"),
            (["inbox", "resolve"], "--wait-consumed"),
            (["inbox", "guidance"], "--wait-delivered"),
        ):
            with self.subTest(argv=argv):
                # argparse may wrap at the hyphen, so compare without spaces.
                self.assertIn(
                    f"requires {needed}".replace(" ", ""),
                    self._help(argv).replace(" ", ""),
                )

    def test_progress_accepts_a_state_dir_for_label_resolution(self) -> None:
        args = cli.build_parser().parse_args(
            ["progress", "label", "--state-dir", "/tmp/state"]
        )
        self.assertEqual("/tmp/state", args.state_dir)
