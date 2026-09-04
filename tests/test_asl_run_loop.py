"""The run loop as a module: what it owns, what it left, and what it may reach.

The behaviour of a run is covered where it always has been — whole runs against
a fake harness in ``tests/test_asl_harness_integration.py``, the gate sequence
in ``tests/test_asl_run_gates.py``, and one file per delegate boundary for the
work Sessions 31-36 moved out. Those suites drive the loop; none of them can
state what this file states, because each is scoped to one behaviour rather
than to the module's shape.

What is asserted here is the boundary Session 37 created:

* the module never imports the runner, directly or lazily, and reaches nothing
  through it either — the loop is the bottom of the package, so a runner import
  here would be a cycle rather than a layering slip;
* every symbol the session moved lives here and is gone from the runner, so
  there is no alias to keep the old spelling alive;
* the loop is singular: one ``run_locked``, one ``while`` statement, one
  ``RunContext``, and the private helpers that serve them are reached from
  nowhere else in the package;
* what the runner has left is two delegating entry points, and the one that
  starts a run supplies this module's ``run_locked`` as the seam.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import unittest
from pathlib import Path

from agent_session_loop import run_entry, run_loop, runner

from .support import assert_no_runner_import

#: Everything Session 37 carried out of ``runner.py``. Each has to exist here
#: and be absent there: an alias left behind would let a caller keep the old
#: spelling and would make the move reversible by accident.
MOVED_SYMBOLS = (
    "run_locked",
    "RunContext",
    "MULTI_REPO_IMPLEMENTATION_TURNS_ENABLED",
    "_finish_run_locked",
    "_terminalize",
    "_soft_fail_commit",
    "_settle_owed_commit_group",
    "_handle_run_control_action",
    "_handle_run_control_status",
    "_expire_run_guards",
    "_abandon_turn",
)

#: The names the loop resolves at their owning module instead of through a
#: runner alias. Session 36 and earlier left these spelled ``runner._x``; the
#: loop no longer has a runner to spell them through, and a caller patching the
#: owner is only heard while the loop reads the owner.
OWNED_ELSEWHERE = (
    "run_coordination.MailboxPoll",
    "run_coordination.ReplyStatePreserver",
    "run_coordination.TerminalAuditContext",
    "run_coordination.drain_external_control_queue",
    "turn_execution.LIVE_CONTROL_POLL_INTERVAL_SECONDS",
    "turn_execution.turn_paths",
    "clock.utc_now",
    "plans.snapshot_payload",
)


def _loop_tree() -> ast.Module:
    return ast.parse(Path(run_loop.__file__ or "").read_text(encoding="utf-8"))


class BoundaryTests(unittest.TestCase):
    """The dependency runs one way, and there is nothing to run it back."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, run_loop)

    def test_the_module_reaches_no_name_through_the_runner(self) -> None:
        # The import check above catches the import; this catches the other
        # half, an attribute read on a `runner` the module never imported,
        # which would fail at runtime rather than at import time.
        reached = {
            ast.unparse(node)
            for node in ast.walk(_loop_tree())
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "runner"
        }
        self.assertEqual(set(), reached)

    def test_every_moved_symbol_lives_here_and_nowhere_in_the_runner(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(run_loop, name))
                self.assertFalse(hasattr(runner, name))

    def test_the_names_it_borrows_are_read_at_their_owners(self) -> None:
        source = Path(run_loop.__file__ or "").read_text(encoding="utf-8")
        for name in OWNED_ELSEWHERE:
            with self.subTest(name=name):
                self.assertIn(name, source)


class SingularityTests(unittest.TestCase):
    """One loop, one context, and helpers that serve only this module."""

    def test_the_module_owns_exactly_one_session_loop(self) -> None:
        loops = [node for node in ast.walk(_loop_tree()) if isinstance(node, ast.While)]
        self.assertEqual(
            1,
            len(loops),
            "the session loop is the module's whole shape; a second `while` "
            "here is a second place a run can decide to stop",
        )

    def test_the_loop_is_the_module_s_only_public_function(self) -> None:
        public = [
            node.name
            for node in _loop_tree().body
            if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        ]
        self.assertEqual(["run_locked"], public)

    def test_the_context_is_a_dataclass_the_delegates_can_satisfy(self) -> None:
        self.assertTrue(dataclasses.is_dataclass(run_loop.RunContext))
        fields = {field.name for field in dataclasses.fields(run_loop.RunContext)}
        # The three lifetimes the docstring names, one witness each.
        for name in ("run_id", "completed_count", "last_session_details"):
            with self.subTest(field=name):
                self.assertIn(name, fields)

    def test_the_private_helpers_are_reached_from_nowhere_else(self) -> None:
        private = {name for name in MOVED_SYMBOLS if name.startswith("_")}
        package = Path(run_loop.__file__ or "").parent
        for path in sorted(package.glob("*.py")):
            if path.name == "run_loop.py":
                continue
            source = path.read_text(encoding="utf-8")
            for name in sorted(private):
                with self.subTest(module=path.name, helper=name):
                    self.assertNotIn(f"run_loop.{name}", source)


class RunnerRemainderTests(unittest.TestCase):
    """What the runner kept: two delegations and the seam this module fills."""

    def test_the_runner_starts_a_run_through_this_module(self) -> None:
        source = inspect.getsource(runner.run_from_args)
        self.assertIn("run_locked=run_loop.run_locked", "".join(source.split()))

    def test_the_locked_run_still_satisfies_the_entry_point_s_seam(self) -> None:
        declared = inspect.signature(run_entry.LockedRun.__call__).parameters
        actual = inspect.signature(run_loop.run_locked).parameters
        for name in (name for name in declared if name != "self"):
            with self.subTest(parameter=name):
                self.assertIn(name, actual)


if __name__ == "__main__":
    unittest.main()
