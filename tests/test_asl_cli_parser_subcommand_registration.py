"""Subcommand handler registration, import hygiene and control flag parsing."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_session_loop import (
    bridge_protocol,
    cli,
    cli_defaults,
    commit_message,
    control_cli,
    control_queue,
    metrics,
    prepare_expand_ingest,
    prepare_models,
    redirect_ack,
    redirect_admission,
    redirect_store_vocab,
    repository_registration,
    retention_projection,
    review,
    turn_execution_constants,
    verification_contract_check,
    workbench_cli,
    workbench_server,
)
from tests.support import (
    REPO_ROOT,
)


class ParserSubcommandRegistrationTests(unittest.TestCase):
    """Subcommand handler registration, import hygiene and control flag parsing."""

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

    def test_review_verdict_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "review-verdict",
                "/tmp/plan",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )

        self.assertEqual("review-verdict", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertTrue(args.json)
        self.assertEqual("review:review_verdict_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), review.review_verdict_from_args)

    def test_contract_check_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            ["contract-check", "/tmp/plan", "--session", "3", "--cwd", "/repo"]
        )

        self.assertEqual("contract-check", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("3", args.session)
        self.assertEqual("/repo", args.cwd)
        self.assertEqual(
            "verification_contract_check:contract_check_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args),
            verification_contract_check.contract_check_from_args,
        )

    def test_register_repositories_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "register-repositories",
                "/tmp/plan",
                "--cwd",
                "/repo",
                "--state-dir",
                "/tmp/state",
                "--json",
            ]
        )

        self.assertEqual("register-repositories", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/repo", args.cwd)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertTrue(args.json)
        self.assertEqual(
            "repository_registration:register_repositories_from_args",
            args.handler_ref,
        )
        self.assertIs(
            cli.resolve_handler(args),
            repository_registration.register_repositories_from_args,
        )

    def test_plans_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            [
                "plans",
                "--state-dir",
                "/tmp/state",
                "--stale-days",
                "14",
                "--never-run",
                "--readiness",
                "reviewed",
                "--json",
            ]
        )

        self.assertEqual("plans", args.command)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(14.0, args.stale_days)
        self.assertTrue(args.never_run)
        self.assertEqual("reviewed", args.readiness)
        self.assertTrue(args.json)
        self.assertEqual("workbench_cli:plans_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), workbench_cli.plans_from_args)

    def test_expand_ingest_subcommand_registers_handler(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(
            ["expand-ingest", "/tmp/plan", "--batch-file", "/tmp/batch.json"]
        )

        self.assertEqual("expand-ingest", args.command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/tmp/batch.json", args.batch_file)
        self.assertEqual(
            "prepare_expand_ingest:expand_ingest_from_args", args.handler_ref
        )
        self.assertIs(
            cli.resolve_handler(args), prepare_expand_ingest.expand_ingest_from_args
        )

    def test_importing_cli_does_not_import_heavy_handler_modules(self) -> None:
        # Guard: `import agent_session_loop.cli` must not eagerly pull in the
        # heavy handler subsystems. This keeps light probes like `asl status`
        # fast — they only import their own handler module, resolved lazily
        # from `handler_ref`. Run in a clean interpreter because sys.modules is
        # process-global and other tests import these modules.
        probe = (
            "import sys\n"
            "import agent_session_loop.cli\n"
            "heavy = [\n"
            "    name\n"
            "    for name in (\n"
            "        'workbench_server',\n"
            "        'workbench',\n"
            "        'runner',\n"
            "        'review',\n"
            "        'metrics',\n"
            "        'prepare',\n"
            "        'preflight',\n"
            "    )\n"
            "    if 'agent_session_loop.' + name in sys.modules\n"
            "]\n"
            "print(','.join(heavy))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual("", completed.stdout.strip(), completed.stdout)

    def test_cli_build_time_default_constants_match_source_modules(self) -> None:
        # cli.py mirrors these defaults locally so building the parser does not
        # import the heavy `workbench_server`/`metrics` modules. Keep the copies
        # honest against their authoritative definitions.
        self.assertEqual(
            workbench_server.DEFAULT_HOST, cli_defaults._WORKBENCH_DEFAULT_HOST
        )
        self.assertEqual(
            workbench_server.DEFAULT_PORT, cli_defaults._WORKBENCH_DEFAULT_PORT
        )
        self.assertEqual(
            metrics.DEFAULT_SINCE_DAYS, cli_defaults._METRICS_DEFAULT_SINCE_DAYS
        )
        self.assertEqual(
            retention_projection.DEFAULT_MAX_AGE_DAYS,
            cli_defaults._RETENTION_DEFAULT_MAX_AGE_DAYS,
        )
        self.assertEqual(
            prepare_models.DEFAULT_VALIDATOR_JOBS,
            cli_defaults._PREPARE_DEFAULT_VALIDATOR_JOBS,
        )
        self.assertEqual(
            prepare_models.DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
            cli_defaults._PREPARE_DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            prepare_models.DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
            cli_defaults._PREPARE_DEFAULT_VALIDATOR_HEARTBEAT_SECONDS,
        )
        self.assertEqual(
            control_queue.DEFAULT_ABORT_GRACE_SECONDS,
            cli_defaults._CONTROL_DEFAULT_ABORT_GRACE_SECONDS,
        )
        self.assertEqual(
            control_cli.DEFAULT_CONTROL_WAIT_TIMEOUT_SECONDS,
            cli_defaults._CONTROL_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            commit_message.DEFAULT_GENERATION_TIMEOUT_SECONDS,
            cli_defaults._COMMIT_DEFAULT_GENERATION_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            redirect_store_vocab.REDIRECT_DEFAULT_TTL_SECONDS,
            cli_defaults._REDIRECT_DEFAULT_TTL_SECONDS,
        )
        self.assertEqual(
            redirect_store_vocab.REDIRECT_MAX_TTL_SECONDS,
            cli_defaults._REDIRECT_MAX_TTL_SECONDS,
        )
        self.assertEqual(
            redirect_admission.ACCEPTANCE_WAIT_SECONDS,
            cli_defaults._REDIRECT_ACCEPTANCE_WAIT_SECONDS,
        )
        self.assertEqual(
            redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS,
            cli_defaults._REDIRECT_MAX_ACCEPTANCE_WAIT_SECONDS,
        )
        self.assertEqual(
            redirect_ack.DEFAULT_ACK_WAIT_SECONDS,
            cli_defaults._REDIRECT_DEFAULT_ACK_WAIT_SECONDS,
        )
        self.assertEqual(
            redirect_ack.MAX_ACK_WAIT_SECONDS,
            cli_defaults._REDIRECT_MAX_ACK_WAIT_SECONDS,
        )
        self.assertEqual(
            turn_execution_constants.INTERRUPTED_EXIT, cli_defaults._INTERRUPTED_EXIT
        )
        self.assertEqual(
            bridge_protocol.PROTOCOL_VERSION, cli_defaults._BRIDGE_PROTOCOL_VERSION
        )

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
            (["workbench", "serve", "--bogus"], "asl workbench serve"),
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


class ParserFlagSpellingTests(unittest.TestCase):
    """Long options never match by prefix, and wait timeouts share spellings."""

    def _parse_error(self, argv: list[str]) -> str:
        parser = cli.build_parser(prog="asl")
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(argv)
        self.assertEqual(2, raised.exception.code)
        return stderr.getvalue()

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

    def _help(self, argv: list[str]) -> str:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            cli.build_parser(prog="asl").parse_args([*argv, "--help"])
        return " ".join(stdout.getvalue().split())

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


class NumericFlagValidationTests(unittest.TestCase):
    """Numeric flags refuse NaN, infinity and out-of-range values at parse time.

    Each of these used to reach its handler unchecked and either crash with a
    traceback (an OverflowError from ``socket.bind`` or ``timedelta``), print a
    bare ``NaN`` into ``--json`` output, or install a schedule launchd cannot
    honor. argparse now refuses them with exit 2 and a message naming the flag.
    """

    def _refused(self, argv: list[str]) -> str:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            cli.build_parser(prog="asl").parse_args(argv)
        self.assertEqual(2, raised.exception.code)
        return stderr.getvalue()

    def test_bad_numeric_values_are_refused_with_the_flag_named(self) -> None:
        cases = (
            (["workbench", "serve", "--port", "99999"], "--port", "0 to 65535"),
            (["workbench", "serve", "--port", "-1"], "--port", "0 to 65535"),
            (["workbench", "--port", "70000"], "--port", "0 to 65535"),
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
            (["maintenance", "install", "--hour", "25"], "--hour", "0 to 23"),
            (["maintenance", "install", "--minute", "-1"], "--minute", "0 to 59"),
            (["maintenance", "install", "--minute", "60"], "--minute", "0 to 59"),
            (
                ["maintenance", "run", "--max-runtime-seconds", "nan"],
                "--max-runtime-seconds",
                "positive",
            ),
            (
                ["maintenance", "run", "--max-runtime-seconds", "-1"],
                "--max-runtime-seconds",
                "positive",
            ),
            (
                ["maintenance", "run", "--max-age-days", "inf"],
                "--max-age-days",
                "non-negative",
            ),
            (
                [
                    "workbench",
                    "connect",
                    "--target-id",
                    "t",
                    "--origin",
                    "https://example.invalid",
                    "--token-file",
                    "/tmp/token",
                    "--heartbeat-interval",
                    "nan",
                ],
                "--heartbeat-interval",
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
        self.assertEqual(
            0, parser.parse_args(["workbench", "serve", "--port", "0"]).port
        )
        self.assertEqual(
            65535, parser.parse_args(["workbench", "serve", "--port", "65535"]).port
        )
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


class WorkbenchGroupFlagTests(unittest.TestCase):
    """Serve flags on the `asl workbench` group reach serve and nothing else.

    A non-serve leaf's own defaults used to overwrite the group's values, so
    `asl workbench --json registry` printed text and `--port 1 --open
    registry` was silently accepted.
    """

    def test_group_json_reaches_every_non_serve_leaf(self) -> None:
        parser = cli.build_parser()
        for argv in (
            ["workbench", "--json", "registry"],
            ["workbench", "--json", "unpin", "/tmp/plan"],
            ["workbench", "--json", "pin", "/tmp/plan"],
        ):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(args.json)
                self.assertFalse(
                    any(name.startswith("_workbench_") for name in vars(args))
                )

    def test_serve_only_group_flags_are_refused_before_another_leaf(self) -> None:
        for argv, flags in (
            (
                [
                    "workbench",
                    "--port",
                    "1",
                    "--host",
                    "10.0.0.1",
                    "--open",
                    "registry",
                ],
                "--host, --port, --open",
            ),
            (["workbench", "--state-dir", "/tmp/s", "pin", "/tmp/p"], "--state-dir"),
            (["workbench", "--plan-root", "/tmp/r", "unpin", "/tmp/p"], "--plan-root"),
        ):
            stderr = io.StringIO()
            with (
                self.subTest(argv=argv),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.build_parser(prog="asl").parse_args(argv)
            self.assertEqual(2, raised.exception.code)
            self.assertIn(
                f"{flags} only apply to `asl workbench serve`", stderr.getvalue()
            )

    def test_bare_workbench_keeps_serve_defaults(self) -> None:
        args = cli.build_parser().parse_args(["workbench"])
        self.assertIsNone(args.workbench_command)
        self.assertEqual(cli_defaults._WORKBENCH_DEFAULT_HOST, args.host)
        self.assertEqual(cli_defaults._WORKBENCH_DEFAULT_PORT, args.port)
        self.assertIsNone(args.state_dir)
        self.assertIsNone(args.plan_root)
        self.assertFalse(args.open)
        self.assertFalse(args.json)

    def test_serve_leaf_flag_wins_over_group_flag(self) -> None:
        args = cli.build_parser().parse_args(
            ["workbench", "--port", "1", "--json", "serve", "--port", "2"]
        )
        self.assertEqual(2, args.port)
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()
