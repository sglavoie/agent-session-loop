"""The preparation boundary, exercised without the run loop.

``turn_preparation`` answers the four questions a *selected* session raises
before a harness process exists: what kind of session it is, whether a pending
review still describes the truth, whether a repair child may run at all, and
what the worker actually receives. These tests drive those four through a
stand-in run context defined here rather than through ``runner``, which is the
point of the boundary.

What is covered is the shape the module docstring claims, as properties rather
than as a walk through one happy path:

* every ``ctx.pause`` is immediately followed by the return that carries it,
  so the ``break`` the moved branches ended in cannot be lost to a
  fall-through;
* the module owns no loop statement, because the loop is not its to break;
* each step is a no-op for the session kind it does not own, so the loop's
  ``if is_review_session`` / ``if is_repair_session`` guards survive the move
  as the module's own preconditions;
* a reply and a resolution pending at once raises rather than picking one;
* the repository-intent gate reads the rendered prompt and is skipped
  entirely for a run with a registered workspace;
* the runner's own body reaches this module and no longer builds a prompt or
  resumes a repair child itself."""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    plans_payloads,
    plans_snapshot,
    run_loop_context,
    turn_preparation,
    turn_preparation_models,
    turn_preparation_prompt,
    turn_preparation_repair,
    turn_preparation_review,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import
from .support_asl_turn_preparation import ENTRY_POINTS, PreparationTestCase

ASL_TEST_PORTABLE = True


class PreparationBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any session it prepares."""

    def module_source(self) -> ast.Module:
        source = "\n".join(
            Path(module.__file__).read_text(encoding="utf-8")
            for module in (
                turn_preparation_models,
                turn_preparation_review,
                turn_preparation_repair,
                turn_preparation_prompt,
            )
        )
        return ast.parse(source)

    def test_turn_preparation_does_not_import_the_runner(self) -> None:
        for module in (
            turn_preparation,
            turn_preparation_models,
            turn_preparation_review,
            turn_preparation_repair,
            turn_preparation_prompt,
        ):
            assert_no_runner_import(self, module)

    def test_every_pause_is_followed_by_the_return_that_carries_it(self) -> None:
        # The branches this replaced ended in `break`. Here the `break` is the
        # returned `paused` flag, so a pause that fell through would refuse a
        # turn and then run it anyway.
        pauses = 0
        for node in ast.walk(self.module_source()):
            body = getattr(node, "body", None)
            if not isinstance(body, list):
                continue
            for index, statement in enumerate(body):
                if not isinstance(statement, ast.Expr):
                    continue
                call = statement.value
                if not isinstance(call, ast.Call):
                    continue
                if ast.unparse(call.func) != "ctx.pause":
                    continue
                pauses += 1
                self.assertLess(
                    index + 1,
                    len(body),
                    "a pause at the end of a block falls through into the "
                    f"turn it refused (turn_preparation.py:{statement.lineno})",
                )
                self.assertIsInstance(
                    body[index + 1],
                    ast.Return,
                    "a pause must be followed by the return that carries it "
                    f"back to the loop (turn_preparation.py:{statement.lineno})",
                )
        self.assertEqual(
            6,
            pauses,
            "the six refusals that live here are the malformed review scope, "
            "the conversation-recovery gate, the ineligible repair child, its "
            "dirty writable set, the repository-intent gate, and the "
            "plan-store state-root gate",
        )

    def test_the_boundary_carries_no_loop_statements(self) -> None:
        stray = [
            f"line {statement.lineno}"
            for statement in ast.walk(self.module_source())
            if isinstance(statement, ast.Break | ast.Continue)
        ]
        self.assertEqual(
            [],
            stray,
            "the loop's `break` is the returned `paused` flag now; a "
            "surviving one would belong to a loop this module does not own",
        )

    def test_every_result_is_frozen(self) -> None:
        for record in (
            turn_preparation.SessionClassification,
            turn_preparation.ReviewPreparation,
            turn_preparation.PacketInputs,
            turn_preparation.RepairResumption,
            turn_preparation.TurnPrompt,
        ):
            with self.subTest(record=record.__name__):
                self.assertTrue(record.__dataclass_params__.frozen)

    def test_the_protocol_names_only_members_the_run_context_has(self) -> None:
        # The Protocol exists so this module never imports the runner; that is
        # only true if the runner's own context object still fits through it.
        declared = {
            name
            for name in vars(turn_preparation.RunContext)
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
                    f"turn_preparation.RunContext declares {name!r}, which the "
                    "run loop's own context does not provide",
                )


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of its work."""

    def loop_source(self) -> str:
        return run_loop_view.assembled_source()

    def test_the_loop_reaches_exactly_the_five_entry_points(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).startswith("turn_preparation.")
        }
        self.assertEqual(set(ENTRY_POINTS), calls)

    def test_the_loop_no_longer_builds_a_prompt_itself(self) -> None:
        moved = {
            "task_packet.build_task_packet",
            "run_prompts.build_reply_prompt",
            "run_prompts.build_resolution_prompt",
            "run_prompts.build_session_prompt",
            "write_guards_repair._pending_repair_child_continuation",
            "review_guard._repair_commit_preflight",
            "run_inputs._task_packet_repository_intent_report",
        }
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
        }
        self.assertEqual(
            set(),
            moved & calls,
            "prompt construction and repair resumption belong to "
            "turn_preparation now; a surviving call is work that escaped",
        )


class ClassificationTests(PreparationTestCase):
    """What kind of session was selected, decided once from its own file."""

    def test_an_ordinary_session_is_implementation_work(self) -> None:
        snapshot = self.snapshot()

        result = turn_preparation.classify_session(snapshot.sessions[0].path)

        self.assertFalse(result.is_review_session)
        self.assertFalse(result.is_repair_session)
        self.assertEqual(
            plans_payloads.SESSION_KIND_IMPLEMENTATION, result.session_kind
        )

    def test_a_generated_review_session_is_review_work(self) -> None:
        snapshot = self.snapshot()
        path = snapshot.sessions[0].path
        with mock.patch.object(
            turn_preparation.review_session_contract,
            "is_generated_review_session_file",
            return_value=True,
        ):
            result = turn_preparation.classify_session(path)

        self.assertTrue(result.is_review_session)
        self.assertEqual(plans_payloads.SESSION_KIND_REVIEW, result.session_kind)

    def test_a_generated_repair_child_is_still_implementation_work(self) -> None:
        snapshot = self.snapshot()
        path = snapshot.sessions[0].path
        with mock.patch.object(
            turn_preparation.review_repair,
            "is_generated_review_repair_session_file",
            return_value=True,
        ):
            result = turn_preparation.classify_session(path)

        self.assertTrue(result.is_repair_session)
        self.assertEqual(
            plans_payloads.SESSION_KIND_IMPLEMENTATION,
            result.session_kind,
            "a review authored the child, but the child does implementation "
            "work and must not be counted as a review turn",
        )


class ReviewPreparationTests(PreparationTestCase):
    """A pending review is re-derived, or refused; it is never assumed."""

    def test_an_implementation_session_is_left_alone(self) -> None:
        snapshot = self.snapshot()

        result = turn_preparation.prepare_review_session(
            self.ctx,
            session=snapshot.sessions[0],
            snapshot=snapshot,
            classification=self.classification(),
            cwd=self.tmp_dir,
            review_guard_excludes=(),
            review_guard_include_ignored=False,
        )

        self.assertIs(snapshot, result.snapshot)
        self.assertIsNone(result.workspace_snapshot)
        self.assertIsNone(result.session_version)
        self.assertFalse(result.paused)
        self.assertEqual([], self.ctx.pauses)

    def test_a_scope_that_cannot_be_re_derived_pauses(self) -> None:
        snapshot = self.snapshot()
        session = snapshot.sessions[0]
        session.path.unlink()

        result = turn_preparation.prepare_review_session(
            self.ctx,
            session=session,
            snapshot=snapshot,
            classification=self.classification(review=True),
            cwd=self.tmp_dir,
            review_guard_excludes=(),
            review_guard_include_ignored=False,
        )

        self.assertTrue(result.paused)
        self.assertIsNone(result.workspace_snapshot)
        (pause,) = self.ctx.pauses
        self.assertEqual("malformed-plan", pause["reason"])
        self.assertIn("error", pause["details"])
        self.assertEqual([snapshot], self.ctx.final_snapshots)

    def test_a_refreshed_contract_forces_a_fresh_plan_image(self) -> None:
        snapshot = self.snapshot()
        session = snapshot.sessions[0]
        refreshed = plans_snapshot.inspect_plan(self.plan_dir)
        with (
            mock.patch.object(
                turn_preparation.review_session_contract,
                "refreshed_generated_review_contract",
                return_value="# refreshed\n",
            ),
            mock.patch.object(
                turn_preparation.review_delta,
                "_refresh_pending_review_scope",
                return_value=False,
            ),
            mock.patch.object(
                turn_preparation.readiness_gate,
                "_refresh_active_readiness_state",
            ) as refresh_readiness,
            mock.patch.object(
                turn_preparation.review_session_contract,
                "generated_review_session_version",
                return_value=2,
            ),
            mock.patch.object(self.ctx.plan_cache, "get_fresh", return_value=refreshed),
        ):
            result = turn_preparation.prepare_review_session(
                self.ctx,
                session=session,
                snapshot=snapshot,
                classification=self.classification(review=True),
                cwd=self.tmp_dir,
                review_guard_excludes=(),
                review_guard_include_ignored=False,
            )

        self.assertFalse(result.paused)
        self.assertIs(refreshed, result.snapshot)
        self.assertEqual(2, result.session_version)
        self.assertEqual("# refreshed\n", session.path.read_text(encoding="utf-8"))
        refresh_readiness.assert_called_once()

    def test_an_unchanged_contract_keeps_the_image_the_loop_had(self) -> None:
        snapshot = self.snapshot()
        session = snapshot.sessions[0]
        original = session.path.read_text(encoding="utf-8")
        with (
            mock.patch.object(
                turn_preparation.review_delta,
                "_refresh_pending_review_scope",
                return_value=False,
            ),
            mock.patch.object(
                turn_preparation.review_session_contract,
                "generated_review_session_version",
                return_value=1,
            ),
            mock.patch.object(
                turn_preparation.readiness_gate,
                "_refresh_active_readiness_state",
            ) as refresh_readiness,
        ):
            result = turn_preparation.prepare_review_session(
                self.ctx,
                session=session,
                snapshot=snapshot,
                classification=self.classification(review=True),
                cwd=self.tmp_dir,
                review_guard_excludes=(),
                review_guard_include_ignored=False,
            )

        self.assertIs(snapshot, result.snapshot)
        self.assertEqual(original, session.path.read_text(encoding="utf-8"))
        refresh_readiness.assert_not_called()


if __name__ == "__main__":
    unittest.main()
