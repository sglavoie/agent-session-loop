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
  own reason, and stop the pass;
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
  runner reaches exactly one entry point here and keeps none of the work.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_settlement,
    events,
    handoff_guards,
    plans,
    readiness_gate,
    review_cycle,
    run_coordination,
    run_inputs,
    run_loop,
    run_selection,
    session_verification,
    turn_execution,
    turn_settlement,
)

from .support import assert_no_runner_import, write_plan

RUN_ID = "run-settlement-1"
SESSION_ID = "1"
SESSION_LABEL = "Session 1"

#: The one entry point the run loop is allowed to reach here. The settlement is
#: a single sequence whose order is the guarantee, so a second name would be a
#: seam through which that order could be reassembled differently.
ENTRY_POINTS = ("turn_settlement.settle_turn",)

#: Settlement work that belonged to the loop and now belongs here. Each is
#: checked as the *callee* of a call, so the runner's surviving module-level
#: aliases -- assignments, not calls -- are deliberately unaffected.
MOVED_CALLS = frozenset(
    {
        "handoff_guards._enforce_plan_artifact_guard",
        "handoff_guards._append_progress_entry",
        "session_verification._enforce_session_verification",
        "session_verification._enforce_review_verdict",
        "plans.sync_derived_status_ledgers",
        "readiness_gate._refresh_active_readiness_state",
        "commit_settlement._registered_session_commit_targets",
        "commit_settlement._dirty_commit_targets",
        "commit_settlement._commit_target_is_clean",
        "commit_settlement._run_commit_group",
        "turn_executor.commit",
        "review_cycle._advance_review_cycle",
        "review_cycle._set_review_resume",
        "review_cycle._terminal_review_reason",
    }
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real, because several of the writes this boundary makes are
    meant to land under one hold and a fake that did not lock would hide a
    missing hold rather than expose it. Events and state writes are kept in
    order so a test asserts on what was recorded without reading files back.
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

    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]


class FakeRunContext:
    """A :class:`turn_settlement.RunContext` built by hand, not by a run.

    Only the members the settlement reaches are populated, which is what the
    Protocol declares; reaching anything else is an ``AttributeError`` here
    rather than a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {
            "run_id": RUN_ID,
            "completed_sessions": [],
        }
        self.persistence = RecordingPersistence()
        self.progress = object()
        self.guard_controller = object()
        self.plan_cache = plans.PlanSnapshotCache()
        self.verification_context = argparse.Namespace(workspace=None)
        self.commit_generation = object()
        self.control_payload: dict[str, Any] = {}
        self.seen_external_command_ids: set[str] = set()
        self.completed_count = 0
        self.review_appended = True
        self.final_snapshots: list[plans.PlanSnapshot] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: plans.PlanSnapshot) -> None:
        self.final_snapshots.append(snapshot)
        with self.persistence.lock:
            self.payload["final_snapshot"] = plans.snapshot_payload(snapshot)

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class FakeTurnExecutor:
    """The publication seam, reduced to what the settlement asks of it."""

    def __init__(self, *, published: plans.PlanSnapshot, record: dict[str, Any]):
        self.published = published
        self.record = record
        self.calls: list[dict[str, Any]] = []
        self.rejection: turn_execution.TurnCommitRejected | None = None

    def commit(self, **kwargs: Any) -> turn_execution.TurnCommit:
        self.calls.append(kwargs)
        if self.rejection is not None:
            raise self.rejection
        return turn_execution.TurnCommit(published=self.published, record=self.record)


class SettlementTestCase(unittest.TestCase):
    """A real plan on disk, a fake context, and every collaborator recorded.

    Each collaborator is patched at its owning module rather than through the
    runner's aliases, because that is where this boundary reads it; a patch on
    the alias would pass while the code under test called the real thing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME

        write_plan(self.plan_dir, ("pending", "pending"))
        self.before = plans.inspect_plan(self.plan_dir)
        write_plan(self.plan_dir, ("complete", "pending"))
        self.after = plans.inspect_plan(self.plan_dir)

        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)
        self.turn_payload: dict[str, Any] = {"turn": 1}
        self.session_details: dict[str, Any] = {"session_id": SESSION_ID}
        self.paths = {
            "artifact_dir": self.artifact_dir,
            "result": self.artifact_dir / "result.json",
        }
        self.published = self.after
        self.turn_record: dict[str, Any] = {"turn": 1, "accepted": True}
        self.turn_executor = FakeTurnExecutor(
            published=self.published, record=self.turn_record
        )
        self.run_controls = argparse.Namespace(
            state=argparse.Namespace(stop_after_current=False)
        )
        self.soft_failures: list[dict[str, Any]] = []
        self.trace: list[str] = []
        self._install_collaborators()

    # -- collaborators ---------------------------------------------------

    def patch(self, owner: Any, name: str, value: Any) -> None:
        patcher = mock.patch.object(owner, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorder(self, label: str, result: Any) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            self.trace.append(label)
            return result

        return _call

    def _install_collaborators(self) -> None:
        self.patch(
            handoff_guards,
            "_enforce_plan_artifact_guard",
            self.recorder("artifact-guard", False),
        )
        self.patch(
            session_verification,
            "_enforce_session_verification",
            self.recorder("verification", False),
        )
        self.patch(
            run_coordination,
            "drain_external_control_queue",
            self.recorder("drain", None),
        )
        self.patch(
            session_verification,
            "_enforce_review_verdict",
            self.recorder("verdict", ("pass", None)),
        )
        self.patch(
            handoff_guards, "_append_progress_entry", self.recorder("progress", True)
        )
        self.patch(plans, "sync_derived_status_ledgers", self.recorder("ledgers", []))
        self.patch(
            readiness_gate,
            "_refresh_active_readiness_state",
            self.recorder("readiness", None),
        )
        self.patch(
            commit_settlement, "_dirty_commit_targets", self.recorder("targets", [])
        )
        self.patch(
            commit_settlement,
            "_registered_session_commit_targets",
            self.recorder("registered-targets", []),
        )
        self.patch(
            commit_settlement,
            "_commit_target_is_clean",
            self.recorder("target-clean", False),
        )
        self.patch(
            commit_settlement,
            "_run_commit_group",
            self.recorder("commit-group", (None, None)),
        )
        self.patch(
            review_cycle,
            "_advance_review_cycle",
            self.recorder("advance", ("complete", self.after)),
        )
        self.patch(
            review_cycle, "_set_review_resume", self.recorder("review-resume", None)
        )
        self.patch(
            review_cycle,
            "_terminal_review_reason",
            self.recorder("terminal-reason", "review-complete"),
        )
        self.patch(
            run_inputs, "_max_repair_group_attempts", self.recorder("max-attempts", 2)
        )

        publish = self.turn_executor.commit

        def _commit(**kwargs: Any) -> turn_execution.TurnCommit:
            self.trace.append("turn-commit")
            return publish(**kwargs)

        self.turn_executor.commit = _commit  # type: ignore[method-assign]

    def soft_fail_commit(self, **kwargs: Any) -> None:
        self.trace.append("soft-fail")
        self.soft_failures.append(kwargs)

    # -- the call under test ---------------------------------------------

    def settle(self, **overrides: Any) -> turn_settlement.TurnSettlement:
        kwargs: dict[str, Any] = {
            "args": argparse.Namespace(state_dir=None),
            "cwd": self.tmp_dir,
            "before": self.before,
            "after": self.after,
            "snapshot": self.before,
            "session_id": SESSION_ID,
            "session_label": SESSION_LABEL,
            "session_details": self.session_details,
            "turn_index": 1,
            "total_sessions": 2,
            "turn_payload": self.turn_payload,
            "paths": self.paths,
            "plan_artifacts_before": {},
            "plan_artifacts_written_by_runner": (),
            "is_review_session": False,
            "is_repair_session": False,
            "review_session_version": None,
            "allow_not_loop_ready": False,
            "commit_mode": None,
            "max_review_fix_cycles": 1,
            "review_requested": False,
            "turn_executor": self.turn_executor,
            "live_turn_payload": {},
            "run_controls": self.run_controls,
            "run_control_lock": threading.RLock(),
            "wakeup": None,
            "soft_fail_commit": self.soft_fail_commit,
        }
        kwargs.update(overrides)
        return turn_settlement.settle_turn(self.ctx, **kwargs)


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
        fields = {field.name for field in dataclasses.fields(run_loop.RunContext)}
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
        source = Path(run_loop.__file__ or "").read_text(encoding="utf-8")
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
        self.assertEqual([], self.trace)

    def test_a_turn_that_moved_more_than_one_status_is_refused(self) -> None:
        write_plan(self.plan_dir, ("complete", "complete"))
        settlement = self.settle(after=plans.inspect_plan(self.plan_dir))
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(
            ["multiple-status-changes"], [p["reason"] for p in self.ctx.pauses]
        )
        self.assertEqual(2, len(self.ctx.payload["observed_status_changes"]))
        self.assertEqual([], self.trace)

    def test_a_turn_that_moved_the_wrong_status_is_refused(self) -> None:
        settlement = self.settle(session_id="2")
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(
            ["unexpected-status-change"], [p["reason"] for p in self.ctx.pauses]
        )
        self.assertEqual([], self.trace)

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
            raise plans.PlanError("ledger is malformed")

        self.patch(plans, "sync_derived_status_ledgers", _raise)
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["malformed-plan"], [p["reason"] for p in self.ctx.pauses])
        self.assertEqual([], self.turn_executor.calls)

    def test_a_rejected_turn_commit_pauses_with_the_rejection_s_own_reason(
        self,
    ) -> None:
        self.turn_executor.rejection = turn_execution.TurnCommitRejected(
            reason="plan-changed",
            message="the plan moved under the accepted snapshot",
            detail={"path": "session-1.md"},
        )
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual(["plan-changed"], [p["reason"] for p in self.ctx.pauses])
        self.assertEqual("session-1.md", self.ctx.pauses[0]["details"]["path"])


class ReviewVerdictTests(SettlementTestCase):
    """The verdict gate, and the stop request that must be seen before it."""

    def test_the_control_queue_is_drained_before_the_verdict_is_taken(self) -> None:
        self.settle(is_review_session=True)
        self.assertLess(self.trace.index("drain"), self.trace.index("verdict"))

    def test_the_verdict_runs_after_verification(self) -> None:
        self.settle(is_review_session=True)
        self.assertLess(self.trace.index("verification"), self.trace.index("verdict"))

    def test_a_retry_verdict_hands_back_the_verdict_s_own_snapshot(self) -> None:
        self.patch(
            session_verification,
            "_enforce_review_verdict",
            self.recorder("verdict", ("retry", self.before)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_RETRY, settlement.action)
        self.assertIs(self.before, settlement.snapshot)
        self.assertIsNone(settlement.turn_record)
        self.assertNotIn("progress", self.trace)

    def test_a_pausing_verdict_stops_the_pass(self) -> None:
        self.patch(
            session_verification,
            "_enforce_review_verdict",
            self.recorder("verdict", ("pause", self.after)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual([], self.turn_executor.calls)

    def test_a_non_review_turn_never_reaches_the_verdict(self) -> None:
        self.settle(is_review_session=False)
        self.assertNotIn("verdict", self.trace)
        self.assertNotIn("drain", self.trace)


class SessionCommitTests(SettlementTestCase):
    """The per-session commit, and the failure the run survives."""

    def test_no_commit_is_attempted_outside_the_per_session_mode(self) -> None:
        self.settle()
        self.assertNotIn("commit-group", self.trace)

    def test_the_per_session_mode_commits_before_the_turn_is_published(self) -> None:
        self.settle(commit_mode=run_selection.COMMIT_MODE_PER_SESSION)
        self.assertLess(
            self.trace.index("commit-group"), self.trace.index("turn-commit")
        )

    def test_a_failed_commit_is_soft_failed_and_the_turn_still_commits(self) -> None:
        self.patch(
            commit_settlement,
            "_run_commit_group",
            self.recorder("commit-group", ({"id": "g1"}, {"target": "."})),
        )
        settlement = self.settle(commit_mode=run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual(1, len(self.soft_failures))
        self.assertEqual("session", self.soft_failures[0]["scope"])
        self.assertEqual("commit-failed", self.turn_payload["session_commit"]["reason"])
        self.assertEqual(1, len(self.turn_executor.calls))

    def test_a_repair_child_owns_its_own_commit_and_records_the_mode(self) -> None:
        self.patch(
            commit_settlement,
            "_dirty_commit_targets",
            self.recorder("targets", [{"target": "."}]),
        )
        self.settle(is_repair_session=True, commit_mode=None)
        self.assertEqual(
            run_selection.REPAIR_COMMIT_MODE_PER_CHILD,
            self.turn_payload["repair_commit"]["mode"],
        )
        self.assertEqual("committed", self.turn_payload["repair_commit"]["result"])

    def test_a_repair_child_with_nothing_dirty_records_no_changes(self) -> None:
        self.settle(is_repair_session=True, commit_mode=None)
        self.assertEqual("no-changes", self.turn_payload["repair_commit"]["result"])
        self.assertNotIn("commit-group", self.trace)


class PublicationTests(SettlementTestCase):
    """What a settled turn leaves on the run."""

    def test_a_completed_turn_hands_back_the_published_turn_record(self) -> None:
        settlement = self.settle()
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertIs(self.turn_record, settlement.turn_record)
        self.assertIs(self.after, settlement.snapshot)
        self.assertIs(self.published, self.ctx.final_snapshots[-1])

    def test_a_completed_session_advances_the_run_s_own_counter(self) -> None:
        self.settle()
        self.assertEqual(1, self.ctx.completed_count)
        self.assertEqual([SESSION_ID], self.ctx.payload["completed_session_ids"])

    def test_a_review_session_is_recorded_without_advancing_the_counter(self) -> None:
        self.turn_record["review_verdict"] = {
            "status_line": "ASL-REVIEW: pass",
            "outcome": "pass",
            "total_findings": 0,
            "findings_by_severity": {},
        }
        self.settle(is_review_session=True)
        self.assertEqual(0, self.ctx.completed_count)
        self.assertTrue(self.ctx.payload["review_completed"])
        self.assertEqual("pass", self.ctx.payload["review_verdict"]["outcome"])


class ReviewCycleTests(SettlementTestCase):
    """The last decision on the review lane, and the flag only it clears."""

    def test_a_completed_cycle_stops_the_run_with_a_terminal_reason(self) -> None:
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertEqual("review-complete", self.ctx.payload["reason"])
        self.assertTrue(self.ctx.review_appended)

    def test_a_paused_cycle_stops_without_a_terminal_reason(self) -> None:
        self.patch(
            review_cycle,
            "_advance_review_cycle",
            self.recorder("advance", ("paused", self.after)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_STOP, settlement.action)
        self.assertNotIn("reason", self.ctx.payload)

    def test_repair_children_clear_the_flag_and_let_the_loop_go_round(self) -> None:
        self.patch(
            review_cycle,
            "_advance_review_cycle",
            self.recorder("advance", ("continue", self.after)),
        )
        settlement = self.settle(is_review_session=True)
        self.assertEqual(turn_settlement.ACTION_PROCEED, settlement.action)
        self.assertFalse(self.ctx.review_appended)
        self.assertIs(self.turn_record, settlement.turn_record)

    def test_the_review_resume_state_is_written_on_every_cycle_outcome(self) -> None:
        for action in ("complete", "paused", "continue"):
            with self.subTest(cycle=action):
                self.trace.clear()
                self.patch(
                    review_cycle,
                    "_advance_review_cycle",
                    self.recorder("advance", (action, self.after)),
                )
                self.settle(is_review_session=True)
                self.assertIn("review-resume", self.trace)

    def test_a_non_review_turn_never_advances_the_cycle(self) -> None:
        self.settle()
        self.assertNotIn("advance", self.trace)


if __name__ == "__main__":
    unittest.main()
