"""The completion boundary, exercised without the run loop.

``run_completion`` owns the one pass a run takes when selection finds nothing
left to do, and the decision it makes there is ordered: is the plan
inconsistent, does it still owe a commit, does it still owe a review and the
repair children a review can reopen, or is it simply finished. These tests
drive that decision through a stand-in run context defined here rather than
through ``runner``, which is the point of the boundary.

What is covered is the ordering and the once-ness the module docstring
claims, as properties rather than as a walk through one happy path:

* an end commit marks itself attempted before it runs, so a failure spends
  the attempt instead of retrying here;
* a failed end commit soft-fails and still hands the loop "go round again";
* the external control queue is drained *before* ``stop_after_current`` is
  read, so a stop queued while the last session ran is seen by the review;
* every exit from :func:`run_completion.complete_plan` states an outcome, so
  the ``break``/``continue`` it replaced cannot come back as a fall-through;
* the runner's branch does nothing but delegate and act on the outcome.
"""

from __future__ import annotations

import argparse
import ast
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    plans,
    run_completion,
    run_control,
    run_coordination,
    run_termination,
)
from agent_session_loop.run_errors import RunError
from tests import support_asl_run_loop_view as run_loop_view

from .support import assert_no_runner_import, write_plan

RUN_ID = "run-completion-1"


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real — the boundary takes it around every payload mutation and
    a fake that did not would hide a missing hold rather than expose it — while
    events and state writes are kept in order so a test can assert on what was
    recorded without reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))


class FakeRunContext:
    """A :class:`run_completion.RunContext` built by hand rather than by a run.

    Only the members the completion pass reaches are populated, which is what
    the Protocol declares; the collaborators a given test does not exercise are
    left as ``None`` so reaching one is an ``AttributeError`` here instead of a
    silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.progress = None
        self.guard_controller = None
        self.plan_cache = plans.PlanSnapshotCache()
        self.verification_context = None
        self.commit_generation = None
        self.control_payload: dict[str, Any] = {}
        self.seen_external_command_ids: set[str] = set()
        self.commit_at_end_attempted = False
        self.review_appended = False
        self.final_snapshots: list[plans.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class FakeRunControls:
    """The listener seam, reduced to the state the completion pass reads."""

    def __init__(self) -> None:
        self.state = run_control.RunControlState()


class RecordingSoftFail:
    """A :class:`run_completion.SoftFailCommit` that records what it absorbed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class CompletionTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a pass needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)
        self.run_controls = FakeRunControls()
        self.run_control_lock = threading.RLock()
        self.soft_fail = RecordingSoftFail()

    def snapshot(self, statuses: tuple[str, ...]) -> plans.PlanSnapshot:
        write_plan(self.plan_dir, statuses)
        return plans.inspect_plan(self.plan_dir)

    def args(self, **overrides: Any) -> argparse.Namespace:
        values: dict[str, Any] = {
            "state_dir": None,
            "review_harness": "codex",
            "max_repair_group_attempts": 2,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def complete(
        self,
        snapshot: plans.PlanSnapshot,
        **overrides: Any,
    ) -> run_completion.CompletionOutcome:
        kwargs: dict[str, Any] = {
            "args": self.args(),
            "cwd": self.tmp_dir,
            "commit_mode": None,
            "review_requested": False,
            "max_review_fix_cycles": 1,
            "allow_not_loop_ready": False,
            "review_guard_excludes": (),
            "review_guard_include_ignored": False,
            "run_controls": self.run_controls,
            "run_control_lock": self.run_control_lock,
            "wakeup": None,
            "soft_fail_commit": self.soft_fail,
        }
        kwargs.update(overrides)
        return run_completion.complete_plan(self.ctx, snapshot=snapshot, **kwargs)


class CompletionBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any plan it completes."""

    def complete_plan_node(self) -> ast.FunctionDef:
        source = Path(run_completion.__file__).read_text(encoding="utf-8")
        (node,) = [
            item
            for item in ast.parse(source).body
            if isinstance(item, ast.FunctionDef) and item.name == "complete_plan"
        ]
        return node

    def test_run_completion_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, run_completion)

    def test_every_exit_states_an_outcome(self) -> None:
        node = self.complete_plan_node()
        returns = [
            statement
            for statement in ast.walk(node)
            if isinstance(statement, ast.Return)
        ]
        self.assertTrue(returns)
        for statement in returns:
            self.assertIsInstance(
                statement.value,
                ast.Call,
                "a bare `return` leaves the caller with no plan image and no "
                "next move, which is the fall-through the moved `break` and "
                f"`continue` could not have (run_completion.py:{statement.lineno})",
            )
            call = statement.value
            assert isinstance(call, ast.Call)
            self.assertEqual(
                "CompletionOutcome",
                getattr(getattr(call.func, "value", None), "id", None),
                "every exit must be built through CompletionOutcome, so the "
                "action and the snapshot are stated together "
                f"(run_completion.py:{statement.lineno})",
            )
            self.assertIn(
                getattr(call.func, "attr", None),
                {"again", "stop"},
                f"unknown outcome constructor (run_completion.py:{statement.lineno})",
            )

    def test_the_pass_never_falls_off_its_end(self) -> None:
        node = self.complete_plan_node()
        self.assertIsInstance(
            node.body[-1],
            ast.Return,
            "the branch this replaced ended in `break`, so a pass that fell "
            "off the end would return None and crash the loop's `.snapshot`",
        )

    def test_the_pass_carries_no_loop_statements(self) -> None:
        node = self.complete_plan_node()
        stray = [
            statement
            for statement in ast.walk(node)
            if isinstance(statement, ast.Break | ast.Continue)
        ]
        self.assertEqual(
            [],
            [f"line {statement.lineno}" for statement in stray],
            "the loop's `break` and `continue` are the outcome now; a "
            "surviving one would belong to a loop this module does not own",
        )

    def test_the_two_actions_are_the_two_the_loop_had(self) -> None:
        self.assertEqual("continue", run_completion.ACTION_CONTINUE)
        self.assertEqual("break", run_completion.ACTION_BREAK)


class RunLoopDelegationTests(unittest.TestCase):
    """The loop's no-pending branch is a delegation and nothing else."""

    def branch(self) -> ast.If:
        source = run_loop_view.assembled_source()
        branches = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "pending_session"
            and any(isinstance(op, ast.Is) for op in node.test.ops)
        ]
        self.assertEqual(1, len(branches), "expected exactly one no-pending branch")
        return branches[0]

    def test_the_branch_calls_only_the_completion_boundary(self) -> None:
        calls = [
            ast.unparse(node.func)
            for node in ast.walk(self.branch())
            if isinstance(node, ast.Call)
        ]
        self.assertEqual(
            ["run_completion.complete_plan", "functools.partial"],
            calls,
            "the end-of-run commit, the review append, and the repair reopen "
            "belong to run_completion now; a second call here is work that "
            "escaped the boundary",
        )

    def test_the_branch_acts_on_the_outcome_it_is_handed(self) -> None:
        body = self.branch().body
        self.assertIsInstance(body[-1], ast.Break)
        self.assertTrue(
            any(
                isinstance(node, ast.Continue)
                for statement in body
                for node in ast.walk(statement)
            ),
            "a completion pass that grew the plan must send the loop round "
            "again; without the `continue` a review would never be run",
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Attribute) and node.attr == "snapshot"
                for statement in body
                for node in ast.walk(statement)
            ),
            "the loop must adopt the outcome's snapshot, otherwise the next "
            "selection runs against the image the pass started from",
        )


class PlanConsistencyTests(CompletionTestCase):
    """Selection and status disagreeing is a pause, not an ending."""

    def test_a_plan_with_an_unfinished_session_pauses(self) -> None:
        snapshot = self.snapshot(("complete", "blocked"))

        outcome = self.complete(snapshot)

        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        self.assertIs(snapshot, outcome.snapshot)
        (pause,) = self.ctx.pauses
        self.assertEqual("no-pending-but-incomplete", pause["reason"])
        self.assertIn("snapshot", pause["details"])
        self.assertEqual([snapshot], self.ctx.final_snapshots)

    def test_the_inconsistency_check_runs_before_any_other_obligation(self) -> None:
        snapshot = self.snapshot(("complete", "blocked"))

        outcome = self.complete(
            snapshot,
            commit_mode="end",
            review_requested=True,
        )

        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        self.assertEqual("no-pending-but-incomplete", self.ctx.pauses[0]["reason"])
        self.assertFalse(
            self.ctx.commit_at_end_attempted,
            "an inconsistent plan must not spend the end-of-run commit: "
            "nothing below the consistency check is meaningful on it",
        )
        self.assertFalse(self.ctx.review_appended)


class AllCompleteTests(CompletionTestCase):
    """A plan that owes nothing ends, and says so in the run's own record."""

    def test_a_finished_plan_stops_with_all_complete(self) -> None:
        snapshot = self.snapshot(("complete", "complete"))

        outcome = self.complete(snapshot)

        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        self.assertIs(snapshot, outcome.snapshot)
        self.assertEqual("all-complete", self.ctx.payload["reason"])
        self.assertEqual(
            plans.snapshot_payload(snapshot),
            self.ctx.payload["final_snapshot"],
        )
        self.assertEqual([], self.ctx.pauses)

    def test_a_spent_commit_and_a_spent_review_fall_through_to_the_end(self) -> None:
        snapshot = self.snapshot(("complete",))
        self.ctx.commit_at_end_attempted = True
        self.ctx.review_appended = True

        outcome = self.complete(
            snapshot,
            commit_mode="end",
            review_requested=True,
        )

        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        self.assertEqual("all-complete", self.ctx.payload["reason"])
        self.assertEqual([], self.soft_fail.calls)


class EndCommitTests(CompletionTestCase):
    """The end-of-run commit is owed once and never ends the run."""

    def setUp(self) -> None:
        super().setUp()
        self.targets = mock.patch.object(
            run_completion.commit_settlement,
            "_registered_end_commit_targets",
            return_value=(),
        )
        self.targets.start()
        self.addCleanup(self.targets.stop)

    def run_end_commit(self, **group: Any) -> run_completion.CompletionOutcome:
        snapshot = self.snapshot(("complete",))
        with mock.patch.object(
            run_completion.commit_settlement, "_run_commit_group", **group
        ) as commit_group:
            self.commit_group = commit_group
            return self.complete(snapshot, commit_mode="end")

    def test_a_successful_commit_sends_the_loop_round_again(self) -> None:
        outcome = self.run_end_commit(return_value=({"ok": True}, None))

        self.assertEqual(run_completion.ACTION_CONTINUE, outcome.action)
        self.assertEqual([], self.soft_fail.calls)
        self.assertTrue(self.ctx.commit_at_end_attempted)
        self.assertEqual(None, self.commit_group.call_args.kwargs["session_id"])

    def test_the_attempt_is_spent_before_the_commit_runs(self) -> None:
        seen: list[bool] = []

        def record(**kwargs: Any) -> tuple[dict[str, Any] | None, None]:
            seen.append(self.ctx.commit_at_end_attempted)
            return (None, None)

        self.run_end_commit(side_effect=record)

        self.assertEqual(
            [True],
            seen,
            "marking the attempt after the commit would let a failure retry "
            "here forever; the retry belongs to the owed-group settlement",
        )

    def test_a_failed_commit_soft_fails_and_keeps_the_run_going(self) -> None:
        outcome = self.run_end_commit(
            return_value=({"status": "failed"}, {"error": "dirty tree"})
        )

        self.assertEqual(run_completion.ACTION_CONTINUE, outcome.action)
        (call,) = self.soft_fail.calls
        self.assertEqual("end", call["scope"])
        self.assertIsNone(call["session_id"])
        self.assertEqual({"error": "dirty tree"}, call["commit"])
        self.assertIsNone(call["error"])
        self.assertEqual([], self.ctx.pauses)

    def test_a_raised_commit_error_is_absorbed_rather_than_propagated(self) -> None:
        outcome = self.run_end_commit(side_effect=RunError("no repository"))

        self.assertEqual(run_completion.ACTION_CONTINUE, outcome.action)
        (call,) = self.soft_fail.calls
        self.assertEqual("no repository", call["error"])
        self.assertIsNone(call["group"])
        self.assertIsNone(call["commit"])
        self.assertTrue(self.ctx.commit_at_end_attempted)

    def test_the_next_pass_moves_on_to_the_review(self) -> None:
        self.run_end_commit(side_effect=RunError("no repository"))
        self.assertTrue(self.ctx.commit_at_end_attempted)

        snapshot = plans.inspect_plan(self.plan_dir)
        with mock.patch.object(
            run_completion.commit_settlement, "_run_commit_group"
        ) as never:
            outcome = self.complete(snapshot, commit_mode="end")

        never.assert_not_called()
        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        self.assertEqual("all-complete", self.ctx.payload["reason"])


class StopBeforeReviewTests(CompletionTestCase):
    """A stop queued while the last session ran is seen by the review."""

    def setUp(self) -> None:
        super().setUp()
        self.stopped = mock.patch.object(
            run_completion.run_termination, "_stop_before_review_work"
        )
        self.stop_before_review_work = self.stopped.start()
        self.addCleanup(self.stopped.stop)
        self.resume = mock.patch.object(
            run_completion.review_cycle,
            "_set_review_resume",
            return_value=mock.Mock(action="review"),
        )
        self.set_review_resume = self.resume.start()
        self.addCleanup(self.resume.stop)

    def drain_that_queues_a_stop(self, **kwargs: Any) -> None:
        """Stand in for the queue drain, delivering a stop the way it would."""
        self.run_controls.state.request_stop_after_current()
        self.drained_before_resume = not self.set_review_resume.called

    def test_the_queue_is_drained_before_the_stop_flag_is_read(self) -> None:
        snapshot = self.snapshot(("complete",))

        with mock.patch.object(
            run_completion.run_coordination,
            "drain_external_control_queue",
            side_effect=self.drain_that_queues_a_stop,
        ):
            outcome = self.complete(snapshot, review_requested=True)

        self.assertTrue(
            self.drained_before_resume,
            "the drain must run before the review resume is computed",
        )
        self.stop_before_review_work.assert_called_once()
        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        self.assertFalse(
            self.ctx.review_appended,
            "a stop observed before the review means no review session is "
            "appended, so a later resume still owes one",
        )

    def test_no_stop_leaves_the_review_to_proceed(self) -> None:
        snapshot = self.snapshot(("complete",))

        # A single-repository run: no workspace, and the delta context is the
        # git-touching collaborator this boundary is not the test subject of.
        self.ctx.verification_context = mock.Mock(workspace=None)

        with (
            mock.patch.object(
                run_completion.run_coordination, "drain_external_control_queue"
            ),
            mock.patch.object(
                run_completion.review_delta,
                "_compute_review_delta_context",
                return_value=(None, None),
            ),
            mock.patch.object(
                run_completion.review_session_body,
                "ensure_generated_review_session",
                side_effect=plans.PlanError("cannot write"),
            ),
        ):
            outcome = self.complete(snapshot, review_requested=True)

        self.stop_before_review_work.assert_not_called()
        self.assertEqual(run_completion.ACTION_BREAK, outcome.action)
        (pause,) = self.ctx.pauses
        self.assertEqual("malformed-plan", pause["reason"])


class SeamOwnershipTests(unittest.TestCase):
    """The collaborators the boundary names are the ones it actually needs."""

    def test_the_context_protocol_is_structural(self) -> None:
        self.assertTrue(
            issubclass(run_completion.RunContext, object)
            and getattr(run_completion.RunContext, "_is_protocol", False),
            "RunContext must stay a Protocol: importing the runner's "
            "dataclass is the dependency this extraction removed",
        )

    def test_the_modules_the_boundary_reaches_are_all_extracted(self) -> None:
        source = Path(run_completion.__file__).read_text(encoding="utf-8")
        imported = {
            alias.name
            for node in ast.parse(source).body
            if isinstance(node, ast.ImportFrom) and node.module is None
            for alias in node.names
        }
        self.assertIn("run_coordination", imported)
        self.assertIn("run_termination", imported)
        self.assertNotIn("runner", imported)
        # The three names the tests above patch through the module must be the
        # module's own bindings, not copies, or a patch would be a no-op.
        self.assertIs(run_coordination, run_completion.run_coordination)
        self.assertIs(run_termination, run_completion.run_termination)


if __name__ == "__main__":
    unittest.main()
