"""The eight terminating branches, exercised without the run loop.

``turn_dispositions`` owns what a turn that did not advance the session does
next: two pauses for a plan that could not be re-inspected, an operator abort
that is recorded rather than paused, a malformed plan that may repair itself, a
restore the provider refused, a worker pause that may be answered out of, a
failed harness exit that may retry off Spark, and a turn an operator corrected
mid-flight, which earns the same session another attempt. These tests drive each
branch through a stand-in run
context defined here rather than through ``runner``, which is the point of the
boundary, and every collaborator is replaced by a recorder so what is asserted
is the decision and the record rather than any collaborator's own behaviour.

What is covered is the shape the module docstring claims, as properties rather
than as a walk through one happy path:

* the two uninspected pauses publish the newest plan image the run holds, which
  is the after-image when there is one and the before-image otherwise;
* an abort is recorded, never paused, and is the only branch that consumes the
  control listener;
* the malformed-plan branch prefers a correction to a reopen, and only the
  correction lets the run continue;
* a runner pause short-circuits its wait when the answer already arrived,
  enters it only when the pause could still be answered, and pins the idle
  baseline exactly where the wait is entered;
* a reply and a continue-resolution each produce a continuation, and a
  pause-resolution does not;
* the Spark retry is refused unless the failed turn left no trace, and its
  run-scoped suppression is set only when the shared ledger could not be
  written;
* a nonzero exit that pauses takes back a completion claim the turn never
  settled, publishes the reconciled plan image rather than the claimed one,
  and says so in the pause when the reversion itself could not be written;
* the refused-restore branch retires the conversation the provider would not
  restore and pauses for recovery, and is argued in
  ``tests/test_asl_native_restore_recovery.py`` alongside the adapter refusals
  that are the only thing that can select it;
* the module owns no loop statement, because the loop is not its to break;
* the redirected branch is dispatched from the same table, and is argued in
  ``tests/test_asl_redirect_continuation.py`` alongside the gate whose
  acceptance is the only thing that can select it;
* the runner's own body reaches these entry points and no longer performs any
  of the branch work itself."""

from __future__ import annotations

import ast
import dataclasses
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    handoff_correction,
    plans_payloads,
    run_loop_context,
    run_termination,
    turn_disposition_failures,
    turn_disposition_mailbox,
    turn_disposition_models,
    turn_disposition_redirect,
    turn_dispositions,
    turn_execution_commit,
)
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import
from .support_asl_turn_dispositions import (
    ENTRY_POINTS,
    INJECTED_SEAMS,
    MOVED_CALLS,
    SESSION_ID,
    SESSION_LABEL,
    DispositionTestCase,
)

ASL_TEST_PORTABLE = True


class BoundaryTests(unittest.TestCase):
    """The dependency runs one way, and the loop stays the loop's."""

    modules = (
        turn_dispositions,
        turn_disposition_models,
        turn_disposition_redirect,
        turn_disposition_mailbox,
        turn_disposition_failures,
    )

    def test_the_module_never_imports_the_runner(self) -> None:
        for module in self.modules:
            with self.subTest(module=module.__name__):
                assert_no_runner_import(self, module)

    def test_the_module_owns_no_loop_statement(self) -> None:
        source = "\n".join(
            Path(module.__file__ or "").read_text(encoding="utf-8")
            for module in self.modules
        )
        loops = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.While | ast.For)
        ]
        self.assertEqual(
            [],
            loops,
            "a boundary that owns a loop statement is deciding when to stop; "
            "these branches hand a continuation back and let the loop decide",
        )

    def test_the_context_protocol_matches_the_runner_s_own_context(self) -> None:
        fields = {
            field.name for field in dataclasses.fields(run_loop_context.RunContext)
        }
        for name in turn_dispositions.RunContext.__annotations__:
            with self.subTest(member=name):
                self.assertIn(name, fields)
        for name in ("run_id", "event_path", "payload", "persistence"):
            with self.subTest(member=name):
                self.assertIn(name, fields)


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of the branch work."""

    def loop_calls(self) -> set[str]:
        source = run_loop_view.assembled_source()
        return {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }

    def test_the_loop_reaches_exactly_the_declared_entry_points(self) -> None:
        reached = {
            name for name in self.loop_calls() if name.startswith("turn_dispositions.")
        }
        self.assertEqual(set(ENTRY_POINTS), reached)

    def test_the_loop_no_longer_performs_the_branch_work_itself(self) -> None:
        self.assertEqual(
            set(),
            MOVED_CALLS & self.loop_calls(),
            "the terminating branches belong to turn_dispositions now; a "
            "surviving call is work that escaped",
        )

    def test_the_loop_still_hands_in_the_three_mailbox_wait_seams(self) -> None:
        # The counterpart to the assertion above: these three are deliberately
        # *not* imported by the boundary. The loop resolves each one as an
        # attribute of its owning module and passes it in, which is what makes
        # a caller's patch on ``run_coordination_mailbox_wait`` take effect; binding
        # them to
        # a module-level name here would freeze the pre-patch function instead.
        source = run_loop_view.assembled_source()
        tree = ast.parse(source)
        loaded = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
        }
        for name in INJECTED_SEAMS:
            with self.subTest(seam=name):
                self.assertIn(name, loaded)


class UninspectedTurnTests(DispositionTestCase):
    """Turns the plan could not be re-inspected after."""

    def test_an_interrupted_turn_publishes_the_after_image_when_it_has_one(
        self,
    ) -> None:
        turn_dispositions.pause_interrupted_run(
            self.ctx,
            before=self.before,
            after=self.after,
            session_details=self.session_details,
        )
        self.assertEqual(
            plans_payloads.snapshot_payload(self.after),
            self.ctx.payload["final_snapshot"],
        )
        self.assertEqual(
            [
                {
                    "reason": "interrupted",
                    "message": "run interrupted by user",
                    "details": self.session_details,
                }
            ],
            self.ctx.pauses,
        )

    def test_an_interrupted_turn_falls_back_to_the_before_image(self) -> None:
        turn_dispositions.pause_interrupted_run(
            self.ctx,
            before=self.before,
            after=None,
            session_details=self.session_details,
        )
        self.assertEqual(
            plans_payloads.snapshot_payload(self.before),
            self.ctx.payload["final_snapshot"],
        )

    def test_a_timed_out_turn_names_the_session_in_its_message(self) -> None:
        turn_dispositions.pause_timed_out_turn(
            self.ctx,
            before=self.before,
            after=None,
            session_label=SESSION_LABEL,
            session_details=self.session_details,
        )
        self.assertEqual(
            plans_payloads.snapshot_payload(self.before),
            self.ctx.payload["final_snapshot"],
        )
        self.assertEqual("timeout", self.ctx.pauses[0]["reason"])
        self.assertEqual(
            f"harness turn timed out for {SESSION_LABEL}",
            self.ctx.pauses[0]["message"],
        )

    def test_an_abort_is_recorded_rather_than_paused(self) -> None:
        listener = mock.Mock()
        with mock.patch.object(run_termination, "_honor_abort_turn") as honor:
            turn_dispositions.record_aborted_turn(
                self.ctx,
                listener,
                before=self.before,
                after=self.after,
                session_id=SESSION_ID,
                turn_payload={"turn_index": 1},
            )
        self.assertEqual([], self.ctx.pauses)
        honor.assert_called_once()
        self.assertIs(listener, honor.call_args.args[0])
        self.assertIs(self.after, honor.call_args.kwargs["snapshot"])

    def test_an_abort_falls_back_to_the_before_image(self) -> None:
        with mock.patch.object(run_termination, "_honor_abort_turn") as honor:
            turn_dispositions.record_aborted_turn(
                self.ctx,
                mock.Mock(),
                before=self.before,
                after=None,
                session_id=SESSION_ID,
                turn_payload={},
            )
        self.assertIs(self.before, honor.call_args.kwargs["snapshot"])


class MalformedPlanTests(DispositionTestCase):
    """The one terminating branch that may put the run back on its feet."""

    def settle(
        self,
        *,
        corrected: tuple[Any, Any],
        reopened: tuple[Any, Any] = (None, None),
    ) -> Any:
        with (
            mock.patch.object(
                handoff_correction,
                "_correct_malformed_handoff",
                return_value=corrected,
            ),
            mock.patch.object(
                handoff_correction,
                "_reopen_uninspectable_handoff",
                return_value=reopened,
            ),
            mock.patch.object(
                turn_execution_commit, "record_turn_rejected"
            ) as rejected,
        ):
            self.rejected = rejected
            return turn_dispositions.settle_malformed_plan(
                self.ctx,
                before=self.before,
                workspace=None,
                session_id=SESSION_ID,
                turn_index=1,
                plan_error="unparsable handoff",
                turn_payload={},
                paths=self.paths(),
                session_details=self.session_details,
            )

    def test_a_corrected_handoff_retries_without_pausing(self) -> None:
        outcome = self.settle(corrected=(self.after, {"attempts": 1}))
        self.assertIs(self.after, outcome)
        self.assertEqual([], self.ctx.pauses)
        self.rejected.assert_not_called()

    def test_an_uncorrectable_handoff_pauses_and_records_the_rejection(self) -> None:
        outcome = self.settle(corrected=(None, {"exhausted": True}))
        self.assertIsNone(outcome)
        self.assertEqual("malformed-plan", self.ctx.pauses[0]["reason"])
        details = self.ctx.pauses[0]["details"]
        self.assertEqual("unparsable handoff", details["error"])
        self.assertEqual({"exhausted": True}, details["handoff_correction"])
        self.assertEqual(SESSION_ID, details["session_id"])
        self.rejected.assert_called_once()
        self.assertEqual("plan-inspection", self.rejected.call_args.kwargs["guard"])

    def test_a_reopened_session_becomes_the_final_snapshot(self) -> None:
        evidence = {"reopened": True}
        self.settle(
            corrected=(None, {"exhausted": True}),
            reopened=(self.after, evidence),
        )
        self.assertEqual([self.after], self.ctx.final_snapshots)
        self.assertEqual(evidence, self.ctx.pauses[0]["details"]["handoff_reopen"])
        self.assertIs(True, self.rejected.call_args.kwargs["reopened"])

    def test_a_reopen_that_found_nothing_keeps_the_before_image(self) -> None:
        self.settle(corrected=(None, None), reopened=(None, {"reopened": False}))
        self.assertEqual([self.before], self.ctx.final_snapshots)
        self.assertIs(False, self.rejected.call_args.kwargs["reopened"])


if __name__ == "__main__":
    unittest.main()
