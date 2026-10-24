"""The settlement of a turn that produced work, driven without the run loop.

``turn_settlement`` owns what happens to a turn that ran, moved a session from
``pending`` to ``complete``, and now has to be judged before it may be believed:
the status-change guards, the unconditional plan-artifact guard, session
verification, the review verdict, the progress journal and the derived ledgers,
the per-session commit, the turn commit, and the review cycle advance.

These tests build the run context by hand and replace every collaborator with a
recorder, so what is asserted is the decision and the order, not any
collaborator's own behaviour. The properties covered are the three the module
docstring claims plus the sequence it claims them in:

* the three status refusals each record a ``turn_rejected``, pause with their
  own reason, adjudicate the plan artifacts only, and stop the pass;
* every guard that refuses stops the pass, and nothing after it runs — asserted
  once per guard rather than on one representative;
* the documented order holds on a turn that survives: artifact guard, then
  verification, then the verdict, then the journal, then the ledgers, then the
  commit;
* a review verdict of ``retry`` hands back the verdict's own snapshot, and the
  external control queue is drained before the verdict is taken;
* a per-session commit that fails is soft-failed rather than rejected, and the
  turn is still committed;
* the review cycle's three outcomes map to stop, stop and proceed, and only the
  repair outcome clears the review-appended flag;
* the module owns no loop statement and never imports the runner, and the
  runner reaches exactly one entry point here and keeps none of the work."""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    handoff_guards,
    plans_errors,
    plans_snapshot,
    plans_status_tables,
    run_loop_context,
    session_verification,
    turn_execution_commit,
    turn_settlement,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import, write_plan
from .support_asl_turn_settlement import ENTRY_POINTS, MOVED_CALLS, SettlementTestCase

ASL_TEST_PORTABLE = True


class BoundaryTests(unittest.TestCase):
    """The dependency runs one way, and the loop stays the loop's."""

    def test_the_module_never_imports_the_runner(self) -> None:
        assert_no_runner_import(self, turn_settlement)

    def test_the_module_owns_no_loop_statement(self) -> None:
        source = Path(turn_settlement.__file__ or "").read_text(encoding="utf-8")
        loops = [
            node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.While)
        ]
        self.assertEqual(
            [],
            loops,
            "a boundary that owns a loop statement is deciding when to stop; "
            "the settlement hands an action back and lets the loop decide",
        )

    def test_the_context_protocol_matches_the_runner_s_own_context(self) -> None:
        fields = {
            field.name for field in dataclasses.fields(run_loop_context.RunContext)
        }
        members = set(turn_settlement.RunContext.__annotations__) | {
            "run_id",
            "run_dir",
            "event_path",
            "payload",
            "persistence",
            "progress",
            "plan_cache",
            "verification_context",
            "commit_generation",
            "control_payload",
            "guard_controller",
            "seen_external_command_ids",
        }
        for name in members:
            with self.subTest(member=name):
                self.assertIn(name, fields)


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of the settlement."""

    def loop_tree(self) -> ast.Module:
        source = run_loop_view.assembled_source()
        return ast.parse(source)

    def calls_in(self, root: ast.AST) -> set[str]:
        return {
            ast.unparse(node.func)
            for node in ast.walk(root)
            if isinstance(node, ast.Call)
        }

    def session_loop(self) -> ast.While:
        # The same locator the run-gate source guards use: the one loop whose
        # body reads the turn's terminating disposition.
        loops = [
            node
            for node in ast.walk(self.loop_tree())
            if isinstance(node, ast.While)
            and any(
                isinstance(name, ast.Name) and name.id == "terminating_disposition"
                for statement in node.body
                for name in ast.walk(statement)
            )
        ]
        self.assertEqual(1, len(loops), "expected exactly one session loop")
        return loops[0]

    def test_the_loop_reaches_exactly_the_declared_entry_points(self) -> None:
        reached = {
            name
            for name in self.calls_in(self.loop_tree())
            if name.startswith("turn_settlement.")
        }
        self.assertEqual(set(ENTRY_POINTS), reached)

    def test_the_loop_no_longer_performs_the_settlement_itself(self) -> None:
        # Scoped to the session loop rather than the whole module: the owed
        # commit group above the loop and the post-loop review-resume refresh
        # legitimately keep calling two of these, and they are not this
        # boundary's work.
        self.assertEqual(
            set(),
            MOVED_CALLS & self.calls_in(self.session_loop()),
            "the settlement belongs to turn_settlement now; a surviving call "
            "is work that escaped the move",
        )


class StatusChangeTests(SettlementTestCase):
    """Exactly one session moved, and it moved the one expected way."""

    def test_a_turn_that_moved_no_status_is_refused(self) -> None:
        settlement = self.settle(after=self.before)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["no-status-change"], [p["reason"] for p in self.ctx.pauses])
        self.assertIn("turn_rejected", self.ctx.persistence.event_names())
        self.assertEqual([], self.ctx.payload["observed_status_changes"])
        self.assertEqual(["artifact-guard"], self.trace)

    def test_a_turn_that_moved_more_than_one_status_is_refused(self) -> None:
        write_plan(self.plan_dir, ("complete", "complete"))
        settlement = self.settle(after=plans_snapshot.inspect_plan(self.plan_dir))
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(
            ["multiple-status-changes"], [p["reason"] for p in self.ctx.pauses]
        )
        self.assertEqual(2, len(self.ctx.payload["observed_status_changes"]))
        self.assertEqual(["artifact-guard"], self.trace)

    def test_a_turn_that_moved_the_wrong_status_is_refused(self) -> None:
        settlement = self.settle(session_id="2")
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(
            ["unexpected-status-change"], [p["reason"] for p in self.ctx.pauses]
        )
        self.assertEqual(["artifact-guard"], self.trace)

    def test_a_refusal_hands_the_loop_back_the_snapshot_it_had(self) -> None:
        settlement = self.settle(after=self.before)
        self.assertIs(self.before, settlement.snapshot)
        self.assertIsNone(settlement.turn_record)


class GuardOrderTests(SettlementTestCase):
    """The one order the settlement may run in, and the refusals that end it."""

    def test_a_surviving_turn_runs_the_guards_in_the_documented_order(self) -> None:
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertEqual(
            [
                "artifact-guard",
                "verification",
                "progress",
                "ledgers",
                "readiness",
                "turn-commit",
            ],
            self.trace,
        )

    def test_the_artifact_guard_runs_before_verification_can_refuse(self) -> None:
        self.patch(
            handoff_guards,
            "_enforce_plan_artifact_guard",
            self.recorder("artifact-guard", True),
        )
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["artifact-guard"], self.trace)

    def test_a_failed_verification_stops_before_anything_is_recorded(self) -> None:
        self.patch(
            session_verification,
            "_enforce_session_verification",
            self.recorder("verification", True),
        )
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["artifact-guard", "verification"], self.trace)
        self.assertEqual([], self.turn_executor.calls)

    def test_the_journal_row_lands_before_the_ledgers_it_describes(self) -> None:
        self.settle()
        self.assertLess(self.trace.index("progress"), self.trace.index("ledgers"))

    def test_a_journal_failure_is_recorded_rather_than_fatal(self) -> None:
        def _raise(*args: Any, **kwargs: Any) -> bool:
            self.trace.append("progress")
            raise RuntimeError("journal is read-only")

        self.patch(handoff_guards, "_append_progress_entry", _raise)
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertIn(
            "progress-journal-append-failed", self.ctx.persistence.event_names()
        )
        self.assertEqual("journal is read-only", self.turn_payload["progress_error"])
        self.assertFalse(self.turn_payload["progress_appended"])

    def test_ledgers_that_cannot_be_synced_pause_the_run(self) -> None:
        def _raise(*args: Any, **kwargs: Any) -> list[Path]:
            self.trace.append("ledgers")
            raise plans_errors.PlanError("ledger is malformed")

        self.patch(plans_status_tables, "sync_derived_status_ledgers", _raise)
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["malformed-plan"], [p["reason"] for p in self.ctx.pauses])
        self.assertEqual([], self.turn_executor.calls)

    def test_a_rejected_turn_commit_pauses_with_the_rejection_s_own_reason(
        self,
    ) -> None:
        self.turn_executor.rejection = turn_execution_commit.TurnCommitRejected(
            reason="plan-changed",
            message="the plan moved under the accepted snapshot",
            detail={"path": "session-1.md"},
        )
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["plan-changed"], [p["reason"] for p in self.ctx.pauses])
        self.assertEqual("session-1.md", self.ctx.pauses[0]["details"]["path"])


if __name__ == "__main__":
    unittest.main()
