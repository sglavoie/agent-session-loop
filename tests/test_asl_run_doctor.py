"""Boundary tests for `agent_session_loop.run_doctor`.

The command's end-to-end behaviour is already covered where it has always been:
`tests/test_asl_cli.py` drives `asl doctor` as a subprocess and asserts the JSON
report, the text report, the registered-repository rows and the error stream it
writes to. None of that says what this module *owns*, which is a rehearsal --
every path, prompt and invocation a turn would use, resolved and then not run.
That is what is stated here.

Four contracts:

* The extraction contract -- runner-free, no alias left behind, exactly the
  three symbols the session moved, nothing durable written, and both stderr
  writers accounted for at their new owner now that the runner has none.
* The delegation contract -- `runner.doctor_from_args` stays a one-line shim
  with the same name and signature, because `cli.py` reaches it through the
  string `handler_ref="runner:doctor_from_args"` and no import, grep or type
  check would catch a rename of it.
* The report contract -- the preview is built from the same builders a turn
  would call, a plan with nothing pending still reports, and the paths a run
  would write are resolved without being written.
* The refusal contract -- one printer, one shape, the selected model and effort
  on every failure, and each refusal carrying exactly what the stages before it
  had resolved.

Every report here is driven for real against a plan on disk. Nothing reaches
Git or a harness, so nothing spawns a process, which is what keeps this module
in `PORTABLE_MODULES`; `test_a_whole_report_spawns_no_process` asserts that
rather than assuming it.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import cli, run_doctor, runner
from tests.support import (
    assert_no_runner_import,
    called_names,
    install_fake_harness,
    write_multi_repo_plan,
    write_plan,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale reference fails here rather than in review. `doctor_from_args`
#: is deliberately absent: its *body* moved, its name did not.
MOVED_SYMBOLS = (
    "NOT_IMPLEMENTED_EXIT",
    "_not_implemented",
    "_print_doctor_error",
)

#: Every call that would outlive the process. A rehearsal writes nothing at
#: all, so this is a flat prohibition rather than a per-function map: the paths
#: this module resolves belong to the run that would later write them.
DURABLE_WRITE_NAMES = frozenset(
    {
        "acquire_lock",
        "append_event",
        "append_progress_entry",
        "mkdir",
        "rename",
        "rmdir",
        "touch",
        "unlink",
        "utime",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
    }
)


def _stderr_owners(source: str) -> set[str]:
    """The top-level definitions in ``source`` that name ``sys.stderr``."""
    owners: set[str] = set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "stderr"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "sys"
            ):
                owners.add(node.name)
                break
    return owners


class _DoctorFixture:
    """A plan on disk plus the isolated environment the report resolves in."""

    def __init__(
        self,
        case: unittest.TestCase,
        statuses: tuple[str, ...] = ("pending",),
        *,
        harness: str = "claude",
        registered: bool = False,
        harness_on_path: bool = True,
    ) -> None:
        self.harness = harness
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # Resolve the temp root: `plans.canonicalize_path` resolves the macOS
        # `/var/folders/...` symlink and a later path comparison would differ.
        self.root = Path(tmp.name).resolve()
        self.plan_dir = self.root / "plan"
        if registered:
            # A registry whose repositories were never checked out: enough to
            # exercise the workspace stage without a Git call.
            write_multi_repo_plan(self.plan_dir, statuses)
        else:
            write_plan(self.plan_dir, statuses)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.state_root = self.root / "state"
        self.executable: Path | None = None
        if harness_on_path:
            self.executable = install_fake_harness(self.bin_dir, harness)

    @property
    def env(self) -> dict[str, str]:
        # `PATH` is replaced rather than prepended so a harness the developer
        # really has installed cannot decide whether a test passes, and `HOME`
        # is redirected so a native model lookup cannot read their own config.
        return {
            "HOME": str(self.root),
            "PATH": str(self.bin_dir),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.state_root),
        }

    def args(self, *extra: str) -> argparse.Namespace:
        return cli.build_parser().parse_args(
            [
                "doctor",
                "--harness",
                self.harness,
                "--cwd",
                str(self.root),
                "--plan-dir",
                str(self.plan_dir),
                *extra,
            ]
        )

    def report(self, *extra: str) -> tuple[int, str, str]:
        """Drive the report for real and return its code, stdout and stderr."""
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, self.env),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = run_doctor.doctor_from_args(self.args(*extra))
        return code, out.getvalue(), err.getvalue()

    def payload(self, *extra: str) -> tuple[int, dict[str, Any]]:
        code, out, _err = self.report("--json", *extra)
        return code, json.loads(out)


class ExtractionContractTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes nothing."""

    def setUp(self) -> None:
        self.source = Path(run_doctor.__file__ or "").read_text(encoding="utf-8")

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, run_doctor)

    def test_every_moved_symbol_lives_here_and_nowhere_in_the_runner(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(run_doctor, name))
                self.assertFalse(hasattr(runner, name))

    def test_the_boundary_writes_nothing_durable_of_its_own(self) -> None:
        # A rehearsal that persisted anything would make reading the report
        # cost something, which is the one thing the command promises it does
        # not.
        tree = ast.parse(self.source)
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.ClassDef):
                continue
            with self.subTest(symbol=node.name):
                self.assertEqual(frozenset(), called_names(node) & DURABLE_WRITE_NAMES)

    def test_only_the_two_report_writers_resolve_sys_stderr(self) -> None:
        # `run_reporting` owns the single handle on stderr while a run is live.
        # These two are allowed their own because neither has a run behind it:
        # one answers an unimplemented subcommand, the other a command that
        # never starts a loop.
        self.assertEqual(
            {"_not_implemented", "_print_doctor_error"}, _stderr_owners(self.source)
        )


class DelegationContractTests(unittest.TestCase):
    """`runner.doctor_from_args` stays the command's public name, unchanged."""

    def test_the_cli_still_reaches_the_command_through_the_runner(self) -> None:
        args = cli.build_parser().parse_args(
            ["doctor", "--harness", "claude", "--plan-dir", "plan-dir"]
        )
        self.assertEqual("runner:doctor_from_args", args.handler_ref)
        # `resolve_handler` imports by string, so nothing else in the tree --
        # no import, no grep over call sites, no type checker -- would catch a
        # rename of the shim.
        self.assertIs(runner.doctor_from_args, cli.resolve_handler(args))

    def test_the_public_signature_is_unchanged_by_the_move(self) -> None:
        signature = inspect.signature(runner.doctor_from_args)
        self.assertEqual(["args"], list(signature.parameters))

    def test_the_shim_is_one_delegation_and_nothing_else(self) -> None:
        tree = ast.parse(inspect.getsource(runner.doctor_from_args))
        body = [
            node
            for node in tree.body[0].body  # type: ignore[attr-defined]
            if not isinstance(node, ast.Expr)  # the docstring
        ]
        self.assertEqual(1, len(body))
        returned = body[0]
        self.assertIsInstance(returned, ast.Return)
        call = returned.value  # type: ignore[union-attr]
        self.assertIsInstance(call, ast.Call)
        self.assertEqual("doctor_from_args", call.func.attr)  # type: ignore[union-attr]
        self.assertEqual("run_doctor", call.func.value.id)  # type: ignore[union-attr]


class ReportTests(unittest.TestCase):
    """The rehearsal: resolved through the builders a turn would call."""

    def test_the_report_previews_the_invocation_the_next_turn_would_use(self) -> None:
        fixture = _DoctorFixture(self)
        code, payload = fixture.payload("--harness-model", "opus", "--effort", "high")

        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual("doctor", payload["command"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual(1, payload["next_pending"])
        self.assertEqual(str(fixture.executable), payload["resolved_executable"])
        self.assertEqual("opus", payload["model"])
        self.assertEqual("high", payload["effort"])
        # The preview is the real invocation, not a description of one: the
        # argv reported is the argv the turn would be launched with, carrying
        # the selection this report just named.
        self.assertEqual(payload["invocation"]["argv"], payload["invocation_argv"])
        self.assertEqual("claude", payload["invocation_argv"][0])
        self.assertIn("--model", payload["invocation_argv"])
        self.assertIn("opus", payload["invocation_argv"])
        # And the prompt is the one that session would receive, built by the
        # same builder a turn calls rather than described here.
        self.assertIn(str(fixture.plan_dir), payload["prompt"])

    def test_a_plan_with_nothing_pending_still_reports(self) -> None:
        fixture = _DoctorFixture(self, ("complete",))
        code, payload = fixture.payload()

        # No pending session means no session scope, not a refusal: the harness,
        # the executable and the plan are still worth reporting.
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["next_pending"])
        self.assertIsNone(payload["next_pending_id"])

        code, out, _err = fixture.report()
        self.assertEqual(0, code)
        self.assertIn("Next pending: none", out)

    def test_the_text_report_names_the_harness_the_executable_and_the_argv(
        self,
    ) -> None:
        fixture = _DoctorFixture(self)
        code, out, err = fixture.report()

        self.assertEqual(0, code)
        self.assertEqual("", err)
        self.assertIn("Harness: claude", out)
        self.assertIn(f"Executable: {fixture.executable}", out)
        self.assertIn(f"Plan: {fixture.plan_dir}", out)
        self.assertIn("Next pending: session 1", out)
        self.assertIn("Argv:", out)

    def test_a_transport_that_carries_a_turn_plan_has_it_printed(self) -> None:
        # Under `app-server`, argv is two words and no longer states the access
        # profile; a report that printed only argv would not show what the turn
        # is allowed to do. Model and effort are passed explicitly so nothing
        # here consults a native default.
        fixture = _DoctorFixture(self, harness="codex")
        code, out, _err = fixture.report("--harness-model", "gpt-5", "--effort", "high")

        self.assertEqual(0, code)
        self.assertIn("Transport: app-server", out)
        self.assertIn("Turn plan:", out)
        self.assertIn('"sandbox"', out)
        self.assertIn('"model": "gpt-5"', out)

    def test_the_paths_a_run_would_write_are_resolved_and_never_written(self) -> None:
        fixture = _DoctorFixture(self)
        code, payload = fixture.payload()

        self.assertEqual(0, code)
        last_message = Path(payload["invocation"]["last_message_path"])
        self.assertEqual("last-message-doctor.txt", last_message.name)
        self.assertTrue(str(last_message).startswith(str(fixture.state_root)))
        self.assertFalse(last_message.exists())
        # Not even the directory that would hold it: the report resolves the
        # state root without creating any part of it.
        self.assertFalse(fixture.state_root.exists())


class RefusalTests(unittest.TestCase):
    """One printer, one shape, and each refusal carries what was resolved."""

    def test_a_bad_cwd_refuses_before_the_plan_is_read(self) -> None:
        fixture = _DoctorFixture(self)
        code, payload = fixture.payload("--cwd", str(fixture.root / "nope"))

        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertEqual("input-error", payload["reason"])
        self.assertIn("cwd does not exist", payload["error"])
        # Nothing had been resolved yet, so the refusal echoes the raw flags.
        self.assertEqual(str(fixture.root / "nope"), payload["cwd"])
        self.assertEqual(str(fixture.plan_dir), payload["plan_dir"])

    def test_a_malformed_plan_refuses_with_the_resolved_cwd(self) -> None:
        fixture = _DoctorFixture(self)
        (fixture.plan_dir / "session-1.md").unlink()
        code, payload = fixture.payload()

        self.assertEqual(1, code)
        self.assertEqual("malformed-plan", payload["reason"])
        self.assertEqual(str(fixture.root), payload["cwd"])

    def test_a_workspace_that_cannot_be_bound_refuses_with_the_digest(self) -> None:
        fixture = _DoctorFixture(self, registered=True)
        code, payload = fixture.payload()

        self.assertEqual(1, code)
        self.assertEqual("workspace-error", payload["reason"])
        # The digest travels on the refusal so the operator can see which
        # binding they have, and the plan directory the snapshot resolved is
        # reported rather than the raw flag.
        self.assertTrue(payload["details"]["registry_digest"])
        self.assertEqual(str(fixture.plan_dir), payload["plan_dir"])

    def test_the_prepared_workspace_binding_is_checked_only_once_it_resolves(
        self,
    ) -> None:
        source = inspect.getsource(run_doctor.doctor_from_args)
        resolved = source.index("workspaces.resolve_workspace(")
        compared = source.index("plans.load_plan_metadata(")
        # Comparing a binding that has not resolved yet would report a
        # mismatch against nothing; the order is the invariant.
        self.assertLess(resolved, compared)

    def test_a_harness_that_is_not_on_path_refuses_with_the_plan_dir(self) -> None:
        fixture = _DoctorFixture(self, harness_on_path=False)
        code, payload = fixture.payload()

        self.assertEqual(1, code)
        self.assertEqual("harness-error", payload["reason"])
        self.assertIn("executable not found on PATH", payload["error"])
        # Later stage, more resolved context: this refusal carries both.
        self.assertEqual(str(fixture.root), payload["cwd"])
        self.assertEqual(str(fixture.plan_dir), payload["plan_dir"])

    def test_every_refusal_carries_the_selected_model_and_effort(self) -> None:
        fixture = _DoctorFixture(self, harness_on_path=False)
        for extra in (
            ("--cwd", str(fixture.root / "nope")),
            (),
        ):
            with self.subTest(extra=extra):
                _code, payload = fixture.payload("--harness-model", "opus", *extra)
                self.assertEqual("opus", payload["model"])
                self.assertIn("model_source", payload)
                self.assertIn("effort_source", payload)

    def test_a_non_json_refusal_goes_to_stderr_and_leaves_stdout_empty(self) -> None:
        fixture = _DoctorFixture(self, harness_on_path=False)
        code, out, err = fixture.report()

        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertIn("agent-session-loop doctor:", err)


class NotImplementedTests(unittest.TestCase):
    """The other report to a caller who has no run."""

    def test_an_unimplemented_subcommand_says_so_on_stderr(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = run_doctor._not_implemented("frobnicate")

        self.assertEqual(run_doctor.NOT_IMPLEMENTED_EXIT, code)
        self.assertNotEqual(0, run_doctor.NOT_IMPLEMENTED_EXIT)
        self.assertIn("frobnicate: not implemented yet", err.getvalue())


class PortabilityTests(unittest.TestCase):
    """Nothing on this path spawns a process, which is what earns PORTABLE."""

    def test_a_whole_report_spawns_no_process(self) -> None:
        fixture = _DoctorFixture(self)

        def _refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("run_doctor spawned an external process")

        with (
            mock.patch.object(subprocess, "Popen", _refuse),
            mock.patch.object(subprocess, "run", _refuse),
            mock.patch.object(subprocess, "call", _refuse),
            mock.patch.object(subprocess, "check_call", _refuse),
            mock.patch.object(subprocess, "check_output", _refuse),
        ):
            code, payload = fixture.payload()
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
