"""ParserSubcommandRegistrationTests: handlers."""

from __future__ import annotations

from .asl_cli_parser_subcommand_registration_support import (
    REPO_ROOT,
    ParserSubcommandRegistrationTestsBase,
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
    subprocess,
    sys,
    turn_execution_constants,
    verification_contract_check,
    workbench_cli,
    workbench_server,
)


class ParserSubcommandRegistrationTests(ParserSubcommandRegistrationTestsBase):
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
