"""WorkbenchGroupFlagTests: workbench."""

from __future__ import annotations

from .asl_cli_parser_subcommand_registration_support import (
    WorkbenchGroupFlagTestsBase,
    cli,
    cli_defaults,
    io,
    redirect_stderr,
)


class WorkbenchGroupFlagTests(WorkbenchGroupFlagTestsBase):
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
