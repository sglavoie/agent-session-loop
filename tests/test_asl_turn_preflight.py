"""The preflight boundary, exercised without the run loop.

``turn_preflight`` owns the last window before a harness process exists: what
the resumed repair child retained, what this turn *is*, and what it will later
be judged against. These tests drive its three steps through a stand-in run
context defined in ``asl_turn_preflight_support`` rather than through
``runner``, which is the point of the boundary.

What is covered is the shape the module docstring claims, as properties rather
than as a walk through one happy path:

* every ``ctx.pause`` is immediately followed by the return that carries it,
  and a refusal hands back no armed guard, so the ``break`` the moved branches
  ended in cannot be lost to a fall-through;
* the module owns no loop statement, because the loop is not its to break;
* the retained-repair lookup re-checks the guard record's own identity, which
  is narrower than the pause envelope's;
* the turn record reads its clocks once and carries the plan size the loop
  reports again later;
* exactly one guard mode is armed per turn, a legacy review turn included --
  the branch whose absence would let a crash skip adjudication entirely;
* the control plane is digested for both modes, because the repository
  snapshot is silent about it;
* the runner's own body reaches this module and no longer captures a baseline
  or builds a turn record itself.
"""

from __future__ import annotations

import ast
import dataclasses
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    run_loop_context,
    run_prompts,
    turn_baselines,
    turn_preflight,
    workspace_evidence,
)
from tests import support_asl_run_loop_view as run_loop_view

from .asl_turn_preflight_support import (
    ENTRY_POINTS,
    MOVED_CALLS,
    SESSION_ID,
    FakeVerificationContext,
    PreflightTestCase,
    fake_workspace,
)
from .support import assert_no_runner_import


class PreflightBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any turn it prepares."""

    def module_source(self) -> ast.Module:
        modules = [
            ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            for module in (turn_preflight, turn_baselines)
        ]
        return ast.Module(
            body=[statement for module in modules for statement in module.body],
            type_ignores=[],
        )

    def test_turn_preflight_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, turn_preflight)
        assert_no_runner_import(self, turn_baselines)

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
                    f"turn it refused (turn_preflight.py:{statement.lineno})",
                )
                self.assertIsInstance(
                    body[index + 1],
                    ast.Return,
                    "a pause must be followed by the return that carries it "
                    f"back to the loop (turn_preflight.py:{statement.lineno})",
                )
        self.assertEqual(
            4,
            pauses,
            "the four refusals moved here are the unusable workspace "
            "snapshot, the retained repair that never cleared its scope, the "
            "unusable legacy repository, and the unusable review fingerprint",
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
        for record in (turn_preflight.ActiveTurn, turn_baselines.TurnBaselines):
            with self.subTest(record=record.__name__):
                self.assertTrue(record.__dataclass_params__.frozen)

    def test_the_protocol_names_only_members_the_run_context_has(self) -> None:
        # The Protocol exists so this module never imports the runner; that is
        # only true if the runner's own context object still fits through it.
        declared = {
            name for name in vars(turn_preflight.RunContext) if not name.startswith("_")
        }
        self.assertTrue(declared)
        fields = {
            field.name for field in dataclasses.fields(run_loop_context.RunContext)
        }
        for name in sorted(declared):
            with self.subTest(member=name):
                self.assertTrue(
                    name in fields or hasattr(run_loop_context.RunContext, name),
                    f"turn_preflight.RunContext declares {name!r}, which the "
                    "run loop's own context does not provide",
                )


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of its work."""

    def loop_source(self) -> str:
        return run_loop_view.assembled_source()

    def test_the_loop_reaches_exactly_the_declared_entry_points(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).startswith(
                ("turn_preflight.", "turn_baselines.")
            )
        }
        self.assertEqual(set(ENTRY_POINTS), calls)

    def test_the_loop_no_longer_captures_a_baseline_itself(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
        }
        self.assertEqual(
            set(),
            MOVED_CALLS & calls,
            "turn-start capture and comparison belong to turn_preflight now; "
            "a surviving call is work that escaped",
        )

    def test_the_loop_no_longer_builds_the_turn_record_itself(self) -> None:
        # The record is the one object every downstream reader is handed, so a
        # second literal in the loop would be a second description of one turn.
        literals = [
            assign.lineno
            for assign in ast.walk(ast.parse(self.loop_source()))
            if isinstance(assign, ast.Assign | ast.AnnAssign)
            and isinstance(assign.value, ast.Dict)
            and {"prompt", "turn_index"}
            <= {key.value for key in assign.value.keys if isinstance(key, ast.Constant)}
        ]
        self.assertEqual([], literals)


class RetainedScopeRepairTests(PreflightTestCase):
    """What a resumed repair child retained, and whose it has to be."""

    def test_a_legacy_run_never_consults_the_repair_ledger(self) -> None:
        # Only a registered workspace has per-alias write scope to repair, so
        # the legacy mode must not even reach the lookup.
        with mock.patch.object(
            workspace_evidence, "_latest_workspace_scope_repair_requirement"
        ) as lookup:
            retained = turn_preflight.resolve_retained_scope_repair(
                event_path=self.event_path,
                session_id=SESSION_ID,
                workspace=None,
            )
        self.assertIsNone(retained)
        lookup.assert_not_called()

    def test_a_requirement_recorded_for_this_session_is_returned(self) -> None:
        record = {"session_id": SESSION_ID, "snapshot_before": {}}
        with mock.patch.object(
            workspace_evidence,
            "_latest_workspace_scope_repair_requirement",
            return_value=record,
        ):
            retained = turn_preflight.resolve_retained_scope_repair(
                event_path=self.event_path,
                session_id=SESSION_ID,
                workspace=fake_workspace("app"),
            )
        self.assertIs(record, retained)

    def test_a_requirement_recorded_for_another_session_is_refused(self) -> None:
        # The lookup also matches on the pause envelope's session, which is the
        # broader identity; the guard record's own is what decides.
        with mock.patch.object(
            workspace_evidence,
            "_latest_workspace_scope_repair_requirement",
            return_value={"session_id": "7", "snapshot_before": {}},
        ):
            retained = turn_preflight.resolve_retained_scope_repair(
                event_path=self.event_path,
                session_id=SESSION_ID,
                workspace=fake_workspace("app"),
            )
        self.assertIsNone(retained)


class ActiveTurnTests(PreflightTestCase):
    """The payload that is this turn, for every reader that needs one."""

    def build(self, **overrides: Any) -> turn_preflight.ActiveTurn:
        values: dict[str, Any] = {
            "snapshot": self.snapshot(("complete", "pending", "pending")),
            "classification": self.classification(),
            "prompt": self.turn_prompt(),
            "session_identity": {"session_id": SESSION_ID, "session_label": "S1"},
            "turn_index": 4,
            "paths": self.paths(),
            "harness": "codex",
            "model_effort": {
                "model": "gpt-5",
                "model_source": "config",
                "effort": "high",
                "effort_source": "flag",
            },
            "spark_route": {"route": None},
            "cwd": self.cwd,
        }
        values.update(overrides)
        return turn_preflight.build_active_turn(self.ctx, **values)

    def test_the_record_carries_the_persisted_prompt_not_the_rendered_one(self) -> None:
        # A continuation's rendered prompt contains the operator's words; the
        # state file keeps the placeholder instead.
        record = self.build()
        self.assertEqual("the words the state file keeps", record.payload["prompt"])

    def test_the_plan_size_travels_with_the_record(self) -> None:
        # The loop reports it again at verification, where a fresh count would
        # read a plan image that has since moved.
        record = self.build()
        self.assertEqual(3, record.total_sessions)
        self.assertEqual(3, record.payload["total_sessions"])

    def test_the_turn_clock_is_read_once(self) -> None:
        record = self.build()
        self.assertEqual(
            record.payload["started_at"], record.payload["last_activity_at"]
        )

    def test_idle_is_zero_before_the_runner_has_finished_any_work(self) -> None:
        self.ctx.runner_work_finished_at = None
        record = self.build()
        self.assertEqual(0.0, record.payload["idle_seconds_since_previous_turn"])

    def test_idle_is_measured_from_the_runner_s_own_last_work(self) -> None:
        # Verification and finalization are runner-owned work, so the gap is
        # measured from when they ended rather than from the harness exit.
        self.ctx.runner_work_finished_at = clock.utc_now() - timedelta(seconds=30)
        record = self.build()
        self.assertGreaterEqual(
            record.payload["idle_seconds_since_previous_turn"], 29.0
        )

    def test_only_a_repair_child_declares_its_commit_mode(self) -> None:
        plain = self.build(classification=self.classification())
        self.assertNotIn("repair_commit_mode", plain.payload)
        repair = self.build(classification=self.classification(repair=True))
        self.assertEqual("per-child", repair.payload["repair_commit_mode"])

    def test_a_resolution_source_is_recorded_only_when_one_exists(self) -> None:
        plain = self.build()
        self.assertNotIn("resolution_source", plain.payload)
        resolved = self.build(
            prompt=self.turn_prompt(resolution_source={"note_length": 3})
        )
        self.assertEqual({"note_length": 3}, resolved.payload["resolution_source"])

    def test_a_legacy_run_records_no_workspace_fields(self) -> None:
        record = self.build()
        self.assertNotIn("cwd", record.payload)
        self.assertNotIn("workspace_root", record.payload)

    def test_a_registered_workspace_adds_its_own_fields(self) -> None:
        self.ctx.verification_context = FakeVerificationContext(
            workspace=fake_workspace("app")
        )
        with mock.patch.object(
            run_prompts,
            "_workspace_payload_fields",
            return_value={"workspace_root": "/workspace"},
        ):
            record = self.build()
        self.assertEqual(str(self.cwd), record.payload["cwd"])
        self.assertEqual("/workspace", record.payload["workspace_root"])


if __name__ == "__main__":
    unittest.main()
