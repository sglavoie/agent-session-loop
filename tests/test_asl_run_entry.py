"""Boundary tests for `agent_session_loop.run_entry`.

The behavioural coverage of this boundary already exists and stays where it is:
`tests/test_asl_supervisor_audit_run_config.py` owns the reconstructed `asl run` command
and the audit flags it recovers, `tests/test_asl_cli.py` and
`tests/test_asl_spark.py` own the resume overlay's effect on individual flags,
`tests/test_asl_run_workspace_guard_exceptions.py` owns the restored workspace binding,
and `tests/test_asl_pause_ux.py`, `tests/test_asl_reply_flows_*.py` and
`tests/test_asl_harness_*.py` drive whole runs through it. None of
them can cheaply state what this module actually owns, which is *the outside of
a run*: the flags it rehydrates and the ordered gate a run passes before a loop
exists. That is what is stated here.

Four contracts:

* The extraction contract -- runner-free, no alias left behind, exactly the ten
  symbols the session moved, and the plan's lock as the only thing this
  boundary publishes.
* The delegation contract -- `runner.run_from_args` stays a two-line shim with
  the same name and signature, because `cli.py` reaches it through the string
  `handler_ref="runner:run_from_args"` and `workbench.py`, `maintenance.py` and
  `skills/asl/scripts/asl_autopilot.py` call it by name. The shim's whole job is
  to supply the one collaborator this module may not import.
* The seam contract -- `run_locked` is required and keyword-only, and
  `run_loop.run_locked` satisfies the `LockedRun` shape. It is required rather
  than defaulted because it is called only once the plan's lock is held, and a
  default would let a caller start a run this module cannot finish.
* The admission contract -- the ordered gate: a refusal before the lock never
  reaches the locked run, contention is checked only under our own published
  claim, and the lock is released whatever the locked run does.

Every run here is driven for real against a plan on disk with a fake locked
run. Nothing reaches Git or a harness, so nothing spawns a process, which is
what keeps this module in `PORTABLE_MODULES`;
`test_driving_the_entry_point_spawns_no_process` asserts that rather than
assuming it.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    cli,
    events,
    run_entry,
    run_flags,
    run_loop,
    runner,
    state,
)
from agent_session_loop.run_errors import RunError
from tests.support import assert_no_runner_import, called_names, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review. `run_from_args`
#: is deliberately absent: its *body* moved, its name did not.
MOVED_SYMBOLS = (
    "_RESUME_OVERLAY_EXCLUDED_DESTS",
    "_resume_overlay_dests",
    "_RESUME_OVERLAY_DESTS",
    "_ResumeUnavailable",
    "_resume_run_tokens",
    "_explicit_resume_dests",
    "_reconstruct_resume_command",
    "_reconstructed_audit_flags",
    "_load_resume_state",
    "_apply_resume_config",
)

#: Every call that would outlive the process. This boundary publishes exactly
#: one thing -- the plan's run lock -- and `state.acquire_lock` is pinned
#: separately below, so everything else is a flat prohibition: the run state,
#: the events and the turn records all belong to the locked run.
DURABLE_WRITE_NAMES = frozenset(
    {
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


def _parse(tokens: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(tokens)


class _EntryFixture:
    """A plan on disk plus the fake locked run the entry point calls."""

    def __init__(self, case: unittest.TestCase, statuses: tuple[str, ...]) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # Resolve the temp root: `plans.inspect_plan` canonicalizes the macOS
        # `/var/folders/...` symlink and a later path comparison would differ.
        self.root = Path(tmp.name).resolve()
        self.plan_dir = self.root / "plan"
        write_plan(self.plan_dir, statuses)
        self.state_dir = self.root / "state"
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        self.calls: list[dict[str, Any]] = []

    def tokens(self, *extra: str) -> list[str]:
        # `--harness claude` keeps the selection deterministic: the native model
        # payload for `codex`/`copilot` reads the developer's own config files.
        return [
            "run",
            str(self.plan_dir),
            "--state-dir",
            str(self.state_dir),
            "--harness",
            "claude",
            "--json",
            *extra,
        ]

    def locked(
        self,
        *,
        result: tuple[int, dict[str, Any]] = (0, {"ok": True}),
        raises: BaseException | None = None,
    ):
        def _run_locked(**kwargs: Any) -> tuple[int, dict[str, Any]]:
            self.calls.append(kwargs)
            if raises is not None:
                raise raises
            code, payload = result
            return code, {"plan_dir": str(self.plan_dir), **payload}

        return _run_locked

    def run(self, *extra: str, **locked_kwargs: Any) -> tuple[int, dict[str, Any]]:
        """Drive the entry point end to end and return its code and payload."""
        tokens = self.tokens(*extra)
        args = _parse(tokens)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_entry.run_from_args(
                args,
                run_argv=tokens[1:],
                run_locked=self.locked(**locked_kwargs),
            )
        return code, json.loads(out.getvalue())

    def write_state_payload(self, payload: dict[str, Any]) -> Path:
        self.plan_state.mkdir(parents=True, exist_ok=True)
        path = self.plan_state / events.STATE_FILE_NAME
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class ExtractionContractTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and publishes only the lock."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, run_entry)

    def test_every_moved_symbol_lives_here_and_nowhere_in_the_runner(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(run_entry, name))
                self.assertFalse(hasattr(runner, name))

    def test_the_boundary_writes_nothing_durable_of_its_own(self) -> None:
        tree = ast.parse(Path(run_entry.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef | ast.ClassDef):
                continue
            with self.subTest(symbol=node.name):
                self.assertEqual(frozenset(), called_names(node) & DURABLE_WRITE_NAMES)

    def test_the_lock_is_taken_once_and_always_released(self) -> None:
        source = inspect.getsource(run_entry.run_from_args)
        tree = ast.parse(inspect.cleandoc(source))
        called = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"acquire_lock", "release_lock"}
        ]
        self.assertEqual(["acquire_lock", "release_lock"], called)
        # The release must sit in a `finally`, not on the happy path: the
        # locked run raising anything at all still has to give the lock back.
        finallys = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Try) and node.finalbody
        ]
        released_in_finally = any(
            "release_lock" in called_names(statement)
            for node in finallys
            for statement in node.finalbody
        )
        self.assertTrue(released_in_finally)


class DelegationContractTests(unittest.TestCase):
    """`runner.run_from_args` stays the package's public entry, unchanged."""

    def test_the_cli_still_reaches_the_entry_point_through_the_runner(self) -> None:
        args = _parse(["run", "plan-dir"])
        self.assertEqual("runner:run_from_args", args.handler_ref)
        self.assertIs(runner.run_from_args, cli.resolve_handler(args))

    def test_the_public_signature_is_unchanged_by_the_move(self) -> None:
        signature = inspect.signature(runner.run_from_args)
        self.assertEqual(["args", "run_argv"], list(signature.parameters))
        self.assertIs(
            inspect.Parameter.KEYWORD_ONLY, signature.parameters["run_argv"].kind
        )
        self.assertIsNone(signature.parameters["run_argv"].default)
        # The seam is the shim's business, not the caller's.
        self.assertNotIn("run_locked", signature.parameters)

    def test_the_shim_is_one_delegation_that_supplies_the_seam(self) -> None:
        tree = ast.parse(inspect.cleandoc(inspect.getsource(runner.run_from_args)))
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
        self.assertEqual("run_from_args", call.func.attr)  # type: ignore[union-attr]
        self.assertEqual("run_entry", call.func.value.id)  # type: ignore[union-attr]
        seam = {keyword.arg: keyword.value for keyword in call.keywords}  # type: ignore[union-attr]
        self.assertEqual({"run_argv", "run_locked"}, set(seam))
        self.assertEqual("run_loop.run_locked", ast.unparse(seam["run_locked"]))


class LockedRunSeamTests(unittest.TestCase):
    """The one collaborator this module may not import enters as a Protocol."""

    def test_the_seam_is_required_and_keyword_only(self) -> None:
        parameter = inspect.signature(run_entry.run_from_args).parameters["run_locked"]
        self.assertIs(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertIs(inspect.Parameter.empty, parameter.default)

    def test_the_runners_locked_run_satisfies_the_declared_shape(self) -> None:
        declared = inspect.signature(run_entry.LockedRun.__call__).parameters
        actual = inspect.signature(run_loop.run_locked).parameters
        expected = {name for name in declared if name != "self"}
        self.assertTrue(expected)
        for name in expected:
            with self.subTest(parameter=name):
                self.assertIn(name, actual)
                self.assertIs(inspect.Parameter.KEYWORD_ONLY, actual[name].kind)
        # Every argument the runner's locked run requires must be one the seam
        # promises to pass, or the entry point cannot call it.
        required = {
            name
            for name, parameter in actual.items()
            if parameter.default is inspect.Parameter.empty
        }
        self.assertTrue(required <= expected)

    def test_the_entry_point_hands_the_seam_the_resolved_run(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        code, _payload = fixture.run()
        self.assertEqual(0, code)
        self.assertEqual(1, len(fixture.calls))
        call = fixture.calls[0]
        self.assertIsInstance(call["lock_info"], state.LockInfo)
        self.assertTrue(call["run_id"])
        # The loop is handed the *resolved* root, not the raw `--cwd`: root and
        # workspace resolution happen on this side of the lock.
        self.assertIsNotNone(call["verification_context"])
        self.assertEqual(call["verification_context"].root, call["cwd"])


class ResumeOverlayTests(unittest.TestCase):
    """Explicit flags win; a dest the persisted block omits is skipped."""

    def test_external_data_roots_resume_and_explicit_roots_replace_them(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        fixture.write_state_payload(
            {"run_config": {"external_write_root": ["/old/trial"]}}
        )
        tokens = fixture.tokens("--resume")
        args = run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        self.assertEqual(["/old/trial"], args.external_write_root)
        self.assertEqual(["/old/trial"], args._restored_external_write_roots)
        tokens = fixture.tokens("--resume", "--external-write-root", "/new/trial")
        args = run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        self.assertEqual(["/new/trial"], args.external_write_root)
        self.assertEqual([], args._restored_external_write_roots)

    def test_the_overlay_dests_are_derived_from_the_flag_spec(self) -> None:
        expected: list[str] = []
        for flag in run_flags.RUN_FLAG_SPEC:
            if flag.positional or flag.dest in run_entry._RESUME_OVERLAY_EXCLUDED_DESTS:
                continue
            if flag.dest not in expected:
                expected.append(flag.dest)
        self.assertEqual(tuple(expected), run_entry._RESUME_OVERLAY_DESTS)
        self.assertEqual(
            run_entry._resume_overlay_dests(), run_entry._RESUME_OVERLAY_DESTS
        )

    def test_a_persisted_flag_is_restored_and_an_explicit_one_wins(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        fixture.write_state_payload(
            {"run_config": {"note": "from-state", "timeout": 4242}}
        )
        tokens = fixture.tokens("--resume", "--note", "from-argv")
        args = run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        self.assertEqual("from-argv", args.note)
        self.assertEqual(4242, args.timeout)

    def test_a_dest_the_persisted_block_omits_is_left_at_its_default(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        fixture.write_state_payload({"run_config": {}})
        tokens = fixture.tokens("--resume")
        default = _parse(fixture.tokens()).note
        args = run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        # Skipped silently rather than refused, which is exactly why
        # `run_payloads._run_config_payload` has to seed every overlay dest.
        self.assertEqual(default, args.note)

    def test_resume_never_rehydrates_the_fresh_conversation_authorization(
        self,
    ) -> None:
        # `--fresh-conversation` authorizes discarding one agent's accumulated
        # context, once. A resume that restored it from persisted state would
        # make every later resume of that run start over silently, so the dest
        # is excluded from the overlay and the payload writes no key for it --
        # this pins the behaviour even against a state file that carries one.
        fixture = _EntryFixture(self, ("pending",))
        fixture.write_state_payload({"run_config": {"fresh_conversation": True}})
        tokens = fixture.tokens("--resume")
        args = run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        self.assertFalse(args.fresh_conversation)
        self.assertNotIn("fresh_conversation", run_entry._RESUME_OVERLAY_DESTS)

    def test_the_deprecated_cap_alias_marks_the_canonical_dest_explicit(self) -> None:
        explicit = run_entry._explicit_resume_dests(
            ["run", "plan-dir", "--max-review-fix-plans", "9"]
        )
        self.assertIn("max_review_fix_plans", explicit)
        self.assertIn("max_review_fix_cycles", explicit)

    def test_the_run_subcommand_name_is_stripped_from_the_tokens(self) -> None:
        self.assertEqual(
            ["plan-dir", "--json"],
            run_entry._resume_run_tokens(["run", "plan-dir", "--json"]),
        )
        self.assertEqual(["plan-dir"], run_entry._resume_run_tokens(["plan-dir"]))

    def test_state_that_cannot_be_read_or_parsed_reads_as_absent(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        self.assertIsNone(
            run_entry._load_resume_state(str(fixture.plan_dir), str(fixture.state_dir))
        )
        path = fixture.write_state_payload({})
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(
            run_entry._load_resume_state(str(fixture.plan_dir), str(fixture.state_dir))
        )

    def test_a_resume_with_no_usable_config_hands_back_a_runnable_command(
        self,
    ) -> None:
        fixture = _EntryFixture(self, ("pending",))
        tokens = fixture.tokens("--resume")
        with self.assertRaises(run_entry._ResumeUnavailable) as caught:
            run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        self.assertIn("asl run", caught.exception.command)
        self.assertIn(str(fixture.plan_dir), caught.exception.command)

        # State that predates `run_config` refuses too, but reconstructs from
        # the top-level fields the state document has always written.
        fixture.write_state_payload({"harness": "codex", "effort": "high"})
        with self.assertRaises(run_entry._ResumeUnavailable) as legacy:
            run_entry._apply_resume_config(_parse(tokens), tokens[1:])
        self.assertIn("--harness codex", legacy.exception.command)
        self.assertIn("--effort high", legacy.exception.command)

    def test_the_refusal_is_reported_rather_than_raised_to_the_caller(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        code, payload = fixture.run("--resume")
        self.assertEqual(1, code)
        self.assertEqual("resume-unavailable", payload["reason"])
        self.assertIn("asl run", payload["resume_command"])
        self.assertEqual([], fixture.calls)


class AdmissionOrderTests(unittest.TestCase):
    """The ordered gate a run passes before a loop exists."""

    def test_a_malformed_plan_never_reaches_the_locked_run(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        (fixture.plan_dir / "session-1.md").unlink()
        code, payload = fixture.run()
        self.assertEqual(1, code)
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertEqual([], fixture.calls)

    def test_an_input_error_never_reaches_the_locked_run(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        code, payload = fixture.run("--cwd", str(fixture.root / "nope"))
        self.assertEqual(1, code)
        self.assertEqual("input-error", payload["reason"])
        self.assertEqual([], fixture.calls)

    def test_contention_is_checked_only_after_the_lock_is_acquired(self) -> None:
        source = inspect.getsource(run_entry.run_from_args)
        acquired = source.index("state_locks.acquire_lock(")
        contended = source.index("run_contention.detect_conflicts(")
        # A check before the acquire leaves two racing starters blind to each
        # other and both proceed; checking under our own published claim means
        # the worst case is both refusing.
        self.assertLess(acquired, contended)

    def test_the_lock_is_released_when_the_locked_run_raises(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        code, payload = fixture.run(raises=RunError("boom"))
        self.assertEqual(1, code)
        self.assertEqual("run-error", payload["reason"])
        self.assertEqual(1, len(fixture.calls))
        # The lock is gone, so a second run can take it.
        second = _EntryFixture(self, ("pending",))
        second.plan_dir = fixture.plan_dir
        second.state_dir = fixture.state_dir
        second.plan_state = fixture.plan_state
        code, _payload = second.run()
        self.assertEqual(0, code)
        self.assertEqual(1, len(second.calls))

    def test_an_interrupt_pauses_with_the_interrupted_exit_code(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        tokens = fixture.tokens()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = run_entry.run_from_args(
                _parse(tokens),
                run_argv=tokens[1:],
                run_locked=fixture.locked(raises=KeyboardInterrupt()),
            )
        payload = json.loads(out.getvalue())
        self.assertEqual(run_entry.turn_execution.INTERRUPTED_EXIT, code)
        self.assertEqual("interrupted", payload["pause_reason"])

    def test_the_locked_runs_payload_is_the_one_that_is_emitted(self) -> None:
        fixture = _EntryFixture(self, ("pending",))
        code, payload = fixture.run(result=(3, {"ok": False, "marker": "from-loop"}))
        self.assertEqual(3, code)
        self.assertEqual("from-loop", payload["marker"])


class PortabilityTests(unittest.TestCase):
    """Nothing on this path spawns a process, which is what earns PORTABLE."""

    def test_driving_the_entry_point_spawns_no_process(self) -> None:
        fixture = _EntryFixture(self, ("pending",))

        def _refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("run_entry spawned an external process")

        with (
            mock.patch.object(subprocess, "Popen", _refuse),
            mock.patch.object(subprocess, "run", _refuse),
            mock.patch.object(subprocess, "call", _refuse),
            mock.patch.object(subprocess, "check_call", _refuse),
            mock.patch.object(subprocess, "check_output", _refuse),
        ):
            code, _payload = fixture.run()
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
