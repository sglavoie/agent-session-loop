"""Workbench serve parser and pin/unpin/registry handler tests.

Split from tests/test_asl_workbench_static.py."""

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from agent_session_loop import (
    cli,
    workbench_archive,
    workbench_cli,
    workbench_registry,
    workbench_registry_models,
    workbench_server,
)
from tests.support import (
    _write_plan_dir,
    patched_env,
)

ASL_TEST_LANE = "integration"


class WorkbenchServeCliTests(unittest.TestCase):
    """Cover the ``asl workbench serve`` argparse wiring."""

    def test_parser_registers_workbench_serve_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/plans-a",
                "--plan-root",
                "/tmp/plans-b",
                "--open",
                "--json",
            ]
        )
        self.assertEqual("workbench", args.command)
        self.assertEqual("serve", args.workbench_command)
        self.assertEqual(0, args.port)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/plans-a", "/tmp/plans-b"], args.plan_root)
        self.assertTrue(args.open)
        self.assertTrue(args.json)
        self.assertIs(
            workbench_server.workbench_serve_from_args, cli.resolve_handler(args)
        )

    def test_parser_defaults_bare_workbench_to_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--port",
                "0",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/plans",
                "--json",
            ]
        )
        self.assertEqual("workbench", args.command)
        self.assertIsNone(args.workbench_command)
        self.assertEqual(0, args.port)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/plans"], args.plan_root)
        self.assertTrue(args.json)
        self.assertIs(
            workbench_server.workbench_serve_from_args, cli.resolve_handler(args)
        )

    def test_parser_preserves_parent_position_flags_before_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--host",
                "0.0.0.0",
                "--port",
                "0",
                "--state-dir",
                "/tmp/state",
                "--plan-root",
                "/tmp/plans-a",
                "--plan-root",
                "/tmp/plans-b",
                "--open",
                "--json",
                "serve",
            ]
        )
        self.assertEqual("workbench", args.command)
        self.assertEqual("serve", args.workbench_command)
        self.assertEqual("0.0.0.0", args.host)
        self.assertEqual(0, args.port)
        self.assertEqual("/tmp/state", args.state_dir)
        self.assertEqual(["/tmp/plans-a", "/tmp/plans-b"], args.plan_root)
        self.assertTrue(args.open)
        self.assertTrue(args.json)
        self.assertFalse(
            any(name.startswith("_workbench_serve_") for name in vars(args))
        )
        self.assertIs(
            workbench_server.workbench_serve_from_args, cli.resolve_handler(args)
        )

    def test_parser_merges_plan_roots_split_around_serve(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--plan-root",
                "/tmp/plans-a",
                "serve",
                "--plan-root",
                "/tmp/plans-b",
                "--plan-root",
                "/tmp/plans-c",
            ]
        )

        self.assertEqual(
            ["/tmp/plans-a", "/tmp/plans-b", "/tmp/plans-c"],
            args.plan_root,
        )

    def test_parser_child_position_flags_override_parent_position_flags(
        self,
    ) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "--host",
                "127.0.0.1",
                "--port",
                "1234",
                "serve",
                "--host",
                "0.0.0.0",
                "--port",
                "5678",
            ]
        )

        self.assertEqual("0.0.0.0", args.host)
        self.assertEqual(5678, args.port)

    def test_parser_registers_workbench_pin_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "workbench",
                "pin",
                "/tmp/plan",
                "--cwd",
                "/repo",
                "--label",
                "Alpha",
            ]
        )
        self.assertEqual("pin", args.workbench_command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertEqual("/repo", args.cwd)
        self.assertEqual("Alpha", args.label)
        self.assertIs(
            workbench_archive.workbench_pin_from_args, cli.resolve_handler(args)
        )

    def test_parser_pin_supports_minimal_invocation(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["workbench", "pin", "/tmp/plan"])
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertIsNone(args.cwd)
        self.assertIsNone(args.label)

    def test_parser_registers_workbench_unpin_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["workbench", "unpin", "/tmp/plan"])
        self.assertEqual("unpin", args.workbench_command)
        self.assertEqual("/tmp/plan", args.plan_dir)
        self.assertIs(
            workbench_archive.workbench_unpin_from_args, cli.resolve_handler(args)
        )

    def test_parser_registers_workbench_registry_subcommand(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(["workbench", "registry", "--json"])
        self.assertEqual("registry", args.workbench_command)
        self.assertTrue(args.json)
        self.assertIs(
            workbench_cli.workbench_registry_from_args, cli.resolve_handler(args)
        )


class WorkbenchCliHandlerTests(unittest.TestCase):
    """Cover the ``asl workbench`` pin/unpin/registry CLI handlers."""

    def _xdg(self, tmp: str) -> Path:
        return Path(tmp) / "config"

    def test_pin_handler_creates_registry_with_profile_and_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            args = argparse.Namespace(
                plan_dir=str(plan_dir),
                cwd=str(tmp_path / "repo"),
                label="Alpha plan",
            )
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_pin_from_args(args)
                self.assertEqual(0, rc)
                registry_path = workbench_registry.default_registry_path()
                self.assertTrue(registry_path.is_file())
                self.assertIn(str(registry_path), stdout.getvalue())
                reloaded = workbench_registry.load_registry(registry_path)
            self.assertEqual(1, len(reloaded.pins))
            pin = reloaded.pins[0]
            self.assertEqual(str(plan_dir), pin.plan_dir)
            self.assertEqual("Alpha plan", pin.label)
            self.assertEqual(str(tmp_path / "repo"), pin.profile.cwd)

    def test_pin_handler_updates_existing_pin_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                first = argparse.Namespace(
                    plan_dir=str(plan_dir), cwd=None, label="First"
                )
                second = argparse.Namespace(
                    plan_dir=str(plan_dir),
                    cwd=str(tmp_path / "newrepo"),
                    label="Second",
                )
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    pin_handler = workbench_archive.workbench_pin_from_args
                    self.assertEqual(0, pin_handler(first))
                    self.assertEqual(0, pin_handler(second))
                reloaded = workbench_registry.load_registry()
            self.assertEqual(1, len(reloaded.pins))
            pin = reloaded.pins[0]
            self.assertEqual("Second", pin.label)
            self.assertEqual(str(tmp_path / "newrepo"), pin.profile.cwd)

    def test_pin_handler_reports_error_on_invalid_plan_dir(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))),
        ):
            args = argparse.Namespace(plan_dir="   ", cwd=None, label=None)
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                rc = workbench_archive.workbench_pin_from_args(args)
            self.assertEqual(1, rc)
            self.assertIn("workbench pin", stderr.getvalue())

    def test_pin_handler_refuses_a_plan_dir_that_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp).resolve() / "plans" / "missing"
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_pin_from_args(
                        argparse.Namespace(
                            plan_dir=str(missing), cwd=None, label=None, json=True
                        )
                    )
                self.assertEqual(1, rc)
                refused = json.loads(stdout.getvalue())
                self.assertFalse(refused["ok"])
                self.assertIn("plan directory does not exist", refused["error"])
                self.assertEqual((), workbench_registry.load_registry().pins)

    def test_unpin_of_a_path_that_was_never_pinned_says_so(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))),
        ):
            plan_dir = str(Path(tmp) / "nothing")
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                rc = workbench_archive.workbench_unpin_from_args(
                    argparse.Namespace(plan_dir=plan_dir)
                )
            self.assertEqual(0, rc)
            self.assertIn("was not pinned", stdout.getvalue())

            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                rc = workbench_archive.workbench_unpin_from_args(
                    argparse.Namespace(plan_dir=plan_dir, json=True)
                )
            self.assertEqual(0, rc)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual("not-pinned", payload["reason"])

    def test_pin_and_unpin_report_json_when_asked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_pin_from_args(
                        argparse.Namespace(
                            plan_dir=str(plan_dir), cwd=None, label="A", json=True
                        )
                    )
                self.assertEqual(0, rc)
                pinned = json.loads(stdout.getvalue())
                self.assertTrue(pinned["ok"])
                self.assertEqual("workbench-pin", pinned["command"])
                self.assertEqual(str(plan_dir), pinned["plan_dir"])
                self.assertEqual("A", pinned["label"])
                self.assertEqual(
                    str(workbench_registry.default_registry_path()),
                    pinned["registry_path"],
                )

                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_unpin_from_args(
                        argparse.Namespace(plan_dir=str(plan_dir), json=True)
                    )
                self.assertEqual(0, rc)
                self.assertEqual("unpinned", json.loads(stdout.getvalue())["reason"])

                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    rc = workbench_archive.workbench_pin_from_args(
                        argparse.Namespace(
                            plan_dir="   ", cwd=None, label=None, json=True
                        )
                    )
                self.assertEqual(1, rc)
                refused = json.loads(stdout.getvalue())
                self.assertFalse(refused["ok"])
                self.assertTrue(refused["reason"])
                self.assertEqual("", stderr.getvalue())

    def test_unpin_handler_removes_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                seeded = workbench_registry.upsert_pin(
                    workbench_registry.load_registry(),
                    plan_dir,
                    label="seed",
                )
                workbench_registry.save_registry(seeded)
                args = argparse.Namespace(plan_dir=str(plan_dir))
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    rc = workbench_archive.workbench_unpin_from_args(args)
                self.assertEqual(0, rc)
                reloaded = workbench_registry.load_registry()
            self.assertEqual((), reloaded.pins)

    def test_unpin_handler_is_idempotent_for_missing_pin(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))),
        ):
            args = argparse.Namespace(plan_dir=str(Path(tmp) / "nothing"))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = workbench_archive.workbench_unpin_from_args(args)
            self.assertEqual(0, rc)

    def test_registry_handler_text_output_lists_pins_and_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                seeded = workbench_registry.upsert_pin(
                    workbench_registry.load_registry(), plan_dir, label="Alpha"
                )
                workbench_registry.save_registry(seeded)
                args = argparse.Namespace(json=False)
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_cli.workbench_registry_from_args(args)
                self.assertEqual(0, rc)
            text = stdout.getvalue()
            self.assertIn("Pins (1)", text)
            self.assertIn(str(plan_dir), text)
            self.assertIn("[Alpha]", text)
            self.assertIn("Plan roots: none", text)

    def test_registry_handler_json_output_includes_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp).resolve()
            plan_dir = _write_plan_dir(tmp_path / "plans" / "alpha")
            with patched_env(XDG_CONFIG_HOME=str(self._xdg(tmp))):
                seeded = workbench_registry.upsert_pin(
                    workbench_registry.load_registry(),
                    plan_dir,
                    label="Alpha",
                    profile=workbench_registry_models.LaunchProfile(cwd=str(tmp_path)),
                )
                workbench_registry.save_registry(seeded)
                args = argparse.Namespace(json=True)
                stdout = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                    rc = workbench_cli.workbench_registry_from_args(args)
                self.assertEqual(0, rc)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual("workbench", payload["command"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual(1, len(payload["registry"]["pins"]))
            self.assertEqual(str(plan_dir), payload["registry"]["pins"][0]["plan_dir"])
            self.assertEqual(
                str(tmp_path),
                payload["registry"]["pins"][0]["profile"]["cwd"],
            )


if __name__ == "__main__":
    unittest.main()
