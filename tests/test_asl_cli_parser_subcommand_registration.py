"""Subcommand handler registration, import hygiene and control flag parsing."""

from __future__ import annotations

import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_session_loop import (
    bridge,
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
    redirect_store,
    repository_registration,
    retention,
    review,
    turn_execution,
    verification,
    workbench,
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
        self.assertEqual("verification:contract_check_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), verification.contract_check_from_args)

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
        self.assertEqual("workbench:plans_from_args", args.handler_ref)
        self.assertIs(cli.resolve_handler(args), workbench.plans_from_args)

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
            retention.DEFAULT_MAX_AGE_DAYS, cli_defaults._RETENTION_DEFAULT_MAX_AGE_DAYS
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
            redirect_store.REDIRECT_DEFAULT_TTL_SECONDS,
            cli_defaults._REDIRECT_DEFAULT_TTL_SECONDS,
        )
        self.assertEqual(
            redirect_store.REDIRECT_MAX_TTL_SECONDS,
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
            turn_execution.INTERRUPTED_EXIT, cli_defaults._INTERRUPTED_EXIT
        )
        self.assertEqual(bridge.PROTOCOL_VERSION, cli_defaults._BRIDGE_PROTOCOL_VERSION)

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

    def test_preflight_mark_loop_ready_short_flag(self) -> None:
        parser = cli.build_parser()

        args = parser.parse_args(["preflight", "/tmp/plan", "-m"])

        self.assertTrue(args.mark_loop_ready)


if __name__ == "__main__":
    unittest.main()
