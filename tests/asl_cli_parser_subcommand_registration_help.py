"""ParserSubcommandRegistrationTests: help."""

from __future__ import annotations

from .asl_cli_parser_subcommand_registration_support import (
    REPO_ROOT,
    ParserSubcommandRegistrationTestsBase,
    argparse,
    cli,
    cli_defaults,
    cli_parser_support,
    io,
    mock,
    redirect_stderr,
    redirect_stdout,
    subprocess,
    sys,
    unittest,
)


class ParserSubcommandRegistrationTests(ParserSubcommandRegistrationTestsBase):
    @unittest.skipIf(
        sys.version_info < (3, 15), "formatter arguments require Python 3.15"
    )
    def test_help_and_usage_accept_supplied_formatters(self) -> None:
        parser = cli_parser_support._AgentSessionLoopArgumentParser(prog="asl")
        left = parser.add_argument("--left")
        right = parser.add_argument("--right")
        parser.show_as_alternatives(left, right)

        for name in ("format_usage", "format_help"):
            for keyword in (False, True):
                with self.subTest(method=name, keyword=keyword):
                    formatter = argparse.HelpFormatter("supplied-formatter", width=1000)
                    render = getattr(parser, name)
                    text = render(formatter=formatter) if keyword else render(formatter)
                    self.assertIn("usage: supplied-formatter ", text)
                    self.assertIn("[--left LEFT | --right RIGHT]", text)
                    self.assertEqual([], parser._mutually_exclusive_groups)

    def test_parser_uses_custom_program_name_in_help(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()

        self.assertTrue(parser.format_help().startswith("usage: asl "))
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "--help"])

        self.assertEqual(0, raised.exception.code)
        self.assertIn("usage: asl run", stdout.getvalue())

    def test_program_name_follows_the_invoked_wrapper(self) -> None:
        cases = {
            "/home/me/.local/bin/asl": "asl",
            "/home/me/.local/bin/agent-session-loop": "agent-session-loop",
            "/repo/agent_session_loop/__main__.py": "asl",
            "tests/run_suite.py": "asl",
            "python3 -m unittest": "asl",
            "-c": "asl",
            "": "asl",
        }
        for argv0, expected in cases.items():
            with self.subTest(argv0=argv0):
                self.assertEqual(expected, cli_defaults.program_name(argv0))

    def test_handler_errors_use_the_program_name_the_usage_line_uses(self) -> None:
        for argv0, expected in (
            ("/bin/asl", "asl"),
            ("/bin/agent-session-loop", "agent-session-loop"),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(argv0=argv0),
                mock.patch.object(sys, "argv", [argv0]),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                code = cli.main(["status", "/nonexistent/asl-plan"])

                self.assertEqual(1, code)
                self.assertTrue(
                    stderr.getvalue().startswith(
                        f"{expected} status: plan directory does not exist"
                    ),
                    stderr.getvalue(),
                )

    def test_module_entry_point_names_itself_asl(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "agent_session_loop", "--help"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )

        self.assertTrue(completed.stdout.startswith("usage: asl "))

    def test_every_subcommand_help_carries_a_summary(self) -> None:
        def walk(
            parser: argparse.ArgumentParser, path: tuple[str, ...]
        ) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
            found = []
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        found.append(((*path, name), child))
                        found.extend(walk(child, (*path, name)))
            return found

        leaves = walk(cli.build_parser(prog="asl"), ())
        self.assertGreater(len(leaves), 50)
        for path, child in leaves:
            with self.subTest(command=" ".join(path)):
                self.assertTrue(child.description)

    def test_leaf_help_prints_its_summary(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()

        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            parser.parse_args(["inbox", "reply", "--help"])

        self.assertIn(
            "Append the first reply to an existing worker question.",
            stdout.getvalue(),
        )

    def test_an_explicit_description_wins_over_the_help_text(self) -> None:
        parser = cli.build_parser(prog="asl")
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        maintenance = subparsers.choices["maintenance"]
        maintenance_sub = next(
            action
            for action in maintenance._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        install = maintenance_sub.choices["install"]

        self.assertTrue(install.description)
        self.assertNotEqual(
            "Install and load the daily ASL-owned maintenance job.",
            install.description,
        )
