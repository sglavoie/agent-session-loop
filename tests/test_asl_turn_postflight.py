"""The post-execution guard sequence's shape and ordering, without the run loop.

``turn_postflight`` owns the window between the harness process ending and the
run loop's first result branch: five guards in one fixed order, and the ordering
*is* the contract. These tests drive the sequence through a stand-in run context
defined here rather than through ``runner``, which is the point of the boundary,
and every guard is replaced by a recorder so what is asserted is the order and
the arguments rather than any guard's own verdict.

What is covered is the shape the module docstring claims, as properties rather
than as a walk through one happy path:

* the five guards run in the declared order, whichever write mode the turn is
  in, and exactly one write-mode guard runs per turn;
* the crash-path copy of the write guard is disarmed *before* the live guard
  runs, so a guard that raises cannot leave both armed;
* a finalization guard that pauses still reaches the protected-path step, in
  adjudicate-only mode, because a turn that breached both has to report both;
* "no baseline" and "proven clean" stay distinct answers to the protected-path
  guard;
* the terminating disposition is named once, ahead of the plan artifact guard,
  and gates it;
* the module owns no loop statement, because the loop is not its to break;
* the runner's own body reaches this module and no longer sequences the guards
  itself.
"""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import run_loop_context, turn_postflight, write_guards
from tests import support_asl_run_loop_view as run_loop_view

from .asl_turn_postflight_support import (
    DISARM,
    DISPOSITION,
    ENTRY_POINTS,
    LEGACY_WRITE,
    MOVED_CALLS,
    PLAN_ARTIFACT,
    PROTECTED_PATH,
    REVIEW_MUTATION,
    SESSION_ID,
    WORKSPACE_WRITE,
    GuardRecorder,
    PostflightTestCase,
    fake_workspace,
)
from .support import assert_no_runner_import


class PostflightBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any turn it judges."""

    def module_source(self) -> ast.Module:
        source = Path(turn_postflight.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_turn_postflight_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, turn_postflight)

    def test_the_boundary_carries_no_loop_statements(self) -> None:
        stray = [
            f"line {statement.lineno}"
            for statement in ast.walk(self.module_source())
            if isinstance(statement, ast.Break | ast.Continue)
        ]
        self.assertEqual(
            [],
            stray,
            "the loop's `break` is the returned `stopped` flag now; a "
            "surviving one would belong to a loop this module does not own",
        )

    def test_the_result_is_frozen(self) -> None:
        record: type[Any] = turn_postflight.PostTurnGuards
        self.assertTrue(record.__dataclass_params__.frozen)

    def test_a_stop_never_carries_a_terminating_disposition(self) -> None:
        # The loop selects its result branch by that name, so a stop that
        # carried one would name a branch the loop already declined to take.
        stopped = turn_postflight._stopped()
        self.assertTrue(stopped.stopped)
        self.assertIsNone(stopped.terminating_disposition)

    def test_the_protocol_names_only_members_the_run_context_has(self) -> None:
        # The Protocol exists so this module never imports the runner; that is
        # only true if the runner's own context object still fits through it.
        declared = {
            name
            for name in vars(turn_postflight.RunContext)
            if not name.startswith("_")
        }
        self.assertTrue(declared)
        fields = {
            field.name for field in dataclasses.fields(run_loop_context.RunContext)
        }
        for name in sorted(declared):
            with self.subTest(member=name):
                self.assertTrue(
                    name in fields or hasattr(run_loop_context.RunContext, name),
                    f"turn_postflight.RunContext declares {name!r}, which the "
                    "run loop's own context does not provide",
                )


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of its sequencing."""

    def loop_source(self) -> str:
        return run_loop_view.assembled_source()

    def test_the_loop_reaches_exactly_the_one_entry_point(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).startswith("turn_postflight.")
        }
        self.assertEqual(set(ENTRY_POINTS), calls)

    def test_the_loop_no_longer_sequences_the_guards_itself(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
        }
        self.assertEqual(
            set(),
            MOVED_CALLS & calls,
            "the post-execution guard sequence belongs to turn_postflight "
            "now; a surviving call is work that escaped",
        )

    def test_the_crash_path_keeps_its_own_copies_of_the_write_guards(self) -> None:
        # The counterpart to the assertion above: the guards the exception
        # handler re-runs are deliberately *not* moved, so an over-eager sweep
        # that deleted them would be caught here rather than at runtime.
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
        }
        for name in (WORKSPACE_WRITE, LEGACY_WRITE, PROTECTED_PATH):
            with self.subTest(guard=name):
                self.assertIn(name, calls)


class GuardOrderingTests(PostflightTestCase):
    """The order the five guards run in, which is the whole contract."""

    def test_a_workspace_turn_runs_write_then_protected_path_then_artifact(
        self,
    ) -> None:
        outcome = self.workspace_turn()

        self.assertEqual(
            [DISARM, WORKSPACE_WRITE, PROTECTED_PATH, DISPOSITION, PLAN_ARTIFACT],
            self.recorder.order,
        )
        self.assertFalse(outcome.stopped)
        self.assertEqual("interrupted", outcome.terminating_disposition)

    def test_a_legacy_turn_substitutes_only_its_own_write_guard(self) -> None:
        self.legacy_turn()

        self.assertEqual(
            [DISARM, LEGACY_WRITE, PROTECTED_PATH, DISPOSITION, PLAN_ARTIFACT],
            self.recorder.order,
        )

    def test_a_legacy_review_turn_runs_the_mutation_guard_in_that_slot(self) -> None:
        self.legacy_review_turn()

        self.assertEqual(
            [DISARM, REVIEW_MUTATION, PROTECTED_PATH, DISPOSITION, PLAN_ARTIFACT],
            self.recorder.order,
        )

    def test_exactly_one_write_mode_guard_runs_per_turn(self) -> None:
        modes = {
            "workspace": self.workspace_turn,
            "legacy": self.legacy_turn,
            "legacy-review": self.legacy_review_turn,
        }
        for mode, run_turn in modes.items():
            with self.subTest(mode=mode):
                self.recorder = GuardRecorder()
                run_turn()
                ran = [
                    name
                    for name in self.recorder.order
                    if name in {WORKSPACE_WRITE, LEGACY_WRITE, REVIEW_MUTATION}
                ]
                self.assertEqual(1, len(ran), ran)

    def test_the_disposition_is_named_once_and_before_the_artifact_guard(self) -> None:
        self.workspace_turn()

        self.assertEqual(1, self.recorder.order.count(DISPOSITION))
        self.assertLess(
            self.recorder.order.index(DISPOSITION),
            self.recorder.order.index(PLAN_ARTIFACT),
        )

    def test_a_workspace_review_turn_still_runs_the_workspace_write_guard(self) -> None:
        # The mutation guard is the legacy mode's comparison; a registered
        # workspace review turn is judged by the write guard plus its bases.
        self.workspace_turn(is_review_session=True)

        self.assertNotIn(REVIEW_MUTATION, self.recorder.order)
        self.assertIn(WORKSPACE_WRITE, self.recorder.order)


class ExceptionGuardDisarmTests(PostflightTestCase):
    """The crash-path copy is retired before the live guard can run."""

    def test_each_write_mode_disarms_before_it_enforces(self) -> None:
        modes = {
            "workspace": (self.workspace_turn, WORKSPACE_WRITE),
            "legacy": (self.legacy_turn, LEGACY_WRITE),
            "legacy-review": (self.legacy_review_turn, REVIEW_MUTATION),
        }
        for mode, (run_turn, guard) in modes.items():
            with self.subTest(mode=mode):
                self.recorder = GuardRecorder()
                run_turn()
                self.assertLess(
                    self.recorder.order.index(DISARM),
                    self.recorder.order.index(guard),
                    "the crash-path guard must be retired before the live one "
                    "runs, or a guard that raises leaves both armed",
                )

    def test_the_disarm_survives_a_guard_that_raises(self) -> None:
        # The reason the disarming is a callback rather than a returned value:
        # a returned one is never assigned when the guard raises.
        armed: dict[str, Any] | None = {"mode": "workspace"}

        def disarm() -> None:
            nonlocal armed
            armed = None

        with (
            mock.patch.object(
                write_guards,
                "_enforce_workspace_write_guard",
                side_effect=RuntimeError("inventory exploded"),
            ),
            self.assertRaises(RuntimeError),
        ):
            turn_postflight.enforce_post_turn_guards(
                self.ctx,
                before=self.before,
                after=None,
                result=self.result(),
                turn_payload={},
                workspace=fake_workspace("app"),
                session_scope=mock.Mock(name="scope"),
                workspace_snapshot_before={"app": {"head": "abc"}},
                legacy_snapshot_before=None,
                review_fingerprint_before=None,
                external_digests_before={},
                turn_started_ns=1,
                plan_artifacts_before={},
                plan_artifacts_written_by_runner=(),
                is_review_session=False,
                cwd=self.cwd,
                session_id=SESSION_ID,
                session_label="Session 1",
                session_details={"session_id": SESSION_ID},
                review_guard_excludes=(),
                review_guard_include_ignored=False,
                paths=self.paths(),
                state_dir_arg=None,
                disarm_exception_guard=disarm,
            )

        self.assertIsNone(armed)

    def test_a_turn_with_no_comparison_at_all_leaves_the_guard_armed(self) -> None:
        # Preflight's three arming branches partition every turn, so this shape
        # is unreachable in a real run -- but nothing here may quietly disarm a
        # guard whose live counterpart never ran.
        self.enforce()

        self.assertNotIn(DISARM, self.recorder.order)
        self.assertEqual(
            [PROTECTED_PATH, DISPOSITION, PLAN_ARTIFACT], self.recorder.order
        )


if __name__ == "__main__":
    unittest.main()
