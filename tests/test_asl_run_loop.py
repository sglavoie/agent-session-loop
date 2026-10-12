"""The run loop as a module: what it owns, what it left, and what it may reach.

The behaviour of a run is covered where it always has been — whole runs against
a fake harness in ``tests/test_asl_harness_*.py``, the gate sequence
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

The charter is widened by two behaviours, deliberately, and both are about
where something sits in this module rather than about what a run does.

The first is the paused-work settlement's *gate*.
``PausedRetainedWorkGateTests`` below decides which pauses
reach ``commit_settlement`` and which never do, with that module's own
settlement replaced by a recorder — so what is asserted is still a property of
this module's shape (one arming site, one call site, one set of refusals, one
progress line reported after the record is durable, and the two source-order
assertions that fail if either site moves)
rather than of a run. The commit those refusals let through is covered where it
is made, in ``tests/test_asl_turn_commit.py``, and the cross-process behaviour
this all exists for is covered in ``tests/test_asl_harness_*.py``.
The second is the commit-group barrier. ``CommitGroupBarrierTests`` asserts
that spending a group's retry budget arms it and that it is read at the loop's
selection boundary, which is again a property of this module's shape: the
bound on the retries lives in ``commit_settlement``, and what this module
decides is that exhausting it ends the run rather than only ending the
attempts. The commit that budget is spent on is covered in
``tests/test_asl_commit_groups.py``.

Nothing here drives a subprocess, so the module stays in both the fast and the
portable lanes.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    commit_settlement,
    pause_policy,
    run_entry,
    run_loop,
    run_selection,
    run_termination,
    runner,
)

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


#: The three non-recoverable reasons the gate has to refuse, one per way a run
#: can be blocked on its environment rather than on a retry:
#: ``guard-protected-path-changed`` is the gate whose whole purpose is to leave
#: the changes on disk for a human, and the other two are what the relaunch
#: itself trips over. All three are in
#: ``pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS``.
NON_RECOVERABLE_REASONS = (
    "guard-protected-path-changed",
    "commit-preflight",
    "not-loop-ready",
)


class RecordingPersistence:
    """The run's write seam, reduced to the lock and the two writes."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.states: list[dict[str, Any]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:  # pragma: no cover - the recorder below appends none
            pass

    def write_state(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.states.append(json.loads(json.dumps(payload, default=str)))


class RecordingProgress:
    """The one reporter method the adapter calls, and nothing else.

    Anything else the adapter reached for would raise here rather than be
    silently absorbed, which is the point: the settlement's only output to a
    terminal is the line it prints about what the relaunch will find.
    """

    def __init__(self) -> None:
        self.paused_work: list[Mapping[str, Any]] = []

    def paused_work_committed(self, record: Mapping[str, Any]) -> None:
        self.paused_work.append(record)


class FakeRunContext:
    """A :class:`run_loop.RunContext` built by hand rather than by a run.

    Only the members the settlement reaches are populated. The collaborators it
    merely forwards are left as ``None``: the recorder standing in for
    ``commit_settlement._settle_paused_retained_work`` never calls them, so a
    ``None`` here is the assertion that the gate does not read them either.
    ``progress`` is the exception, because the adapter reports the record
    itself rather than forwarding the reporter.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = "run-20260904-gate"
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.progress = RecordingProgress()
        self.verification_context = None
        self.commit_generation = None
        self.owed_commit_group: dict[str, Any] | None = None
        self.owed_commit_group_attempts: dict[str, int] = {}
        self.retained_work: commit_settlement.RetainedWorkContext | None = None


class PausedRetainedWorkGateTests(unittest.TestCase):
    """Which pauses owe the work they retained a commit, and which never do."""

    def setUp(self) -> None:
        self.loop_source = inspect.getsource(run_loop.run_locked)
        self.module_source = Path(run_loop.__file__ or "").read_text(encoding="utf-8")
        self.fresh()

    def fresh(self) -> None:
        """A run directory, an unarmed context and an empty call log.

        Called again between the parametrized cases so each reason and mode is
        judged by a context no earlier case touched.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run_dir = Path(tmp.name) / "run"
        self.artifact_dir = self.run_dir / "turn-0001-session-5"
        self.artifact_dir.mkdir(parents=True)
        self.result_path = self.artifact_dir / "result.json"
        self.ctx = FakeRunContext(
            run_dir=self.run_dir,
            event_path=self.run_dir / "events.jsonl",
        )
        self.calls: list[dict[str, Any]] = []

    def arm(
        self,
        *,
        is_review_session: bool = False,
        is_repair_session: bool = False,
    ) -> commit_settlement.RetainedWorkContext:
        retained = commit_settlement.RetainedWorkContext(
            session_id="5",
            session_label="session 5",
            turn_payload={"session_id": "5", "workspace_write_guard": {"passed": True}},
            result_path=self.result_path,
            artifact_dir=self.artifact_dir,
            is_review_session=is_review_session,
            is_repair_session=is_repair_session,
        )
        self.ctx.retained_work = retained
        return retained

    def pause(self, reason: str) -> None:
        # The recorded classification is what the gate reads, so it is written
        # by the one writer of it rather than hand-assembled here.
        run_termination.set_pause(
            self.ctx.payload,
            reason=reason,
            message=f"paused: {reason}",
            details={"session_id": "5", "verification_repair_attempt": 1},
        )

    def settle(self, commit_mode: str | None) -> None:
        def recorder(**kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            record = {"guard": "paused-work-commit", "result": "committed"}
            kwargs["retained"].turn_payload["paused_work_commit"] = record
            return record

        with mock.patch.object(
            commit_settlement, "_settle_paused_retained_work", recorder
        ):
            run_loop._settle_paused_retained_work(self.ctx, commit_mode=commit_mode)

    def test_a_recoverable_pause_under_per_session_settles_the_retained_work(
        self,
    ) -> None:
        retained = self.arm()
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual(1, len(self.calls))
        call = self.calls[0]
        self.assertIs(retained, call["retained"])
        self.assertEqual("verify-failed", call["pause_reason"])
        self.assertEqual(run_selection.COMMIT_MODE_PER_SESSION, call["commit_mode"])
        # The details the settlement reads its repair numbers out of are the
        # payload's own, not a copy the mirror below would then miss.
        self.assertIs(self.ctx.payload["pause_details"], call["pause_details"])

    def test_every_recoverable_reason_settles_under_an_active_commit_mode(
        self,
    ) -> None:
        for reason in pause_policy.RECOVERABLE_PAUSE_REASONS:
            for commit_mode in run_selection.COMMIT_MODES:
                with self.subTest(reason=reason, commit_mode=commit_mode):
                    self.fresh()
                    self.arm()
                    self.pause(reason)
                    self.settle(commit_mode)
                    self.assertEqual(1, len(self.calls))

    def test_the_record_is_mirrored_into_the_payload_and_the_pause_details(
        self,
    ) -> None:
        self.arm()
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        key = commit_settlement.PAUSED_WORK_COMMIT_KEY
        record = self.ctx.payload[key]
        self.assertEqual("committed", record["result"])
        # `set_pause` aliases the two spellings to one dict, so an in-place
        # update of the details serves every reader of either name.
        self.assertIs(record, self.ctx.payload["pause_details"][key])
        self.assertIs(record, self.ctx.payload["details"][key])
        self.assertTrue(self.ctx.persistence.states)
        self.assertEqual(record, self.ctx.persistence.states[-1][key])
        # The turn's own artifact is re-persisted here because
        # `commit_settlement` writes the record onto the live turn payload and
        # makes no file writes of its own.
        written = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertEqual(record, written[key])

    def test_the_settled_record_is_reported_once_after_it_is_durable(self) -> None:
        self.arm()
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        record = self.ctx.payload[commit_settlement.PAUSED_WORK_COMMIT_KEY]
        # The same object, not a rendering of it: the reporter decides how the
        # fact reads and this module decides nothing about how it prints.
        self.assertEqual([record], self.ctx.progress.paused_work)
        # Reported after the state write, so a terminal that never drains
        # cannot leave the record undurable.
        self.assertTrue(self.ctx.persistence.states)

    def test_a_pause_that_settles_nothing_reports_nothing(self) -> None:
        self.arm()
        self.pause("guard-protected-path-changed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)
        self.assertEqual([], self.ctx.progress.paused_work)

    def test_a_non_recoverable_pause_never_settles(self) -> None:
        for reason in NON_RECOVERABLE_REASONS:
            with self.subTest(reason=reason):
                self.fresh()
                self.arm()
                self.pause(reason)
                self.assertFalse(self.ctx.payload["pause_policy"]["recoverable"])
                self.settle(run_selection.COMMIT_MODE_PER_SESSION)
                self.assertEqual([], self.calls)
                self.assertNotIn(
                    commit_settlement.PAUSED_WORK_COMMIT_KEY, self.ctx.payload
                )
                self.assertFalse(self.result_path.exists())

    def test_a_review_session_pause_never_settles(self) -> None:
        self.arm(is_review_session=True)
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)

    def test_a_paused_repair_child_settles_with_no_commit_mode(self) -> None:
        self.arm(is_repair_session=True)
        self.pause("verify-failed")
        self.settle(None)
        self.assertEqual(1, len(self.calls))
        self.assertIsNone(self.calls[0]["commit_mode"])

    def test_an_authored_session_with_no_commit_mode_never_settles(self) -> None:
        self.arm()
        self.pause("verify-failed")
        self.settle(None)
        self.assertEqual([], self.calls)

    def test_a_pause_no_turn_armed_settles_nothing(self) -> None:
        self.pause("verify-failed")
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)

    def test_a_run_that_did_not_pause_settles_nothing(self) -> None:
        self.arm()
        self.settle(run_selection.COMMIT_MODE_PER_SESSION)
        self.assertEqual([], self.calls)

    # Both halves of the settlement are placements rather than conditions --
    # what makes a protected-path pause unreachable is where the arming sits,
    # and what makes the commit follow the revert is where the call sits -- so
    # the two below fail if either one moves.

    def test_the_context_is_armed_once_and_right_after_the_postflight_stop(
        self,
    ) -> None:
        armed = "ctx.retained_work = commit_settlement.RetainedWorkContext("
        self.assertEqual(
            1,
            self.module_source.count(armed),
            "one arming site: a second one would arm a turn some guard already refused",
        )
        stopped = self.loop_source.index("if postflight.stopped:")
        self.assertLess(stopped, self.loop_source.index(armed))
        # Exactly one loop exit between the two: the postflight check's own.
        # Anything else there would be a pause that leaves the loop with the
        # work retained and nothing armed to settle it.
        self.assertEqual(
            1,
            self.loop_source[stopped : self.loop_source.index(armed)].count("break\n"),
            "the arming site sits immediately after the postflight stop check",
        )

    def test_the_settlement_is_called_once_after_the_loop_and_before_the_end(
        self,
    ) -> None:
        call = "_settle_paused_retained_work(ctx, commit_mode=commit_mode)"
        self.assertEqual(1, self.module_source.count(call), "one call site")
        called = self.loop_source.index(call)
        # After the loop: the handler that wraps the whole `while` closes
        # above it, so every break the loop takes has already converged here.
        self.assertLess(
            self.loop_source.index("except BaseException as error:"), called
        )
        # And before both readers of what it wrote: the review-resume
        # projection, and the terminalization that snapshots the unresolved
        # commit groups a soft-failed settlement lands in.
        self.assertLess(called, self.loop_source.rindex("if review_requested:"))
        self.assertLess(called, self.loop_source.rindex("_terminalize("))


class RecordingCommitProgress:
    """The one reporter method a soft-failed commit calls, and nothing else."""

    def __init__(self) -> None:
        self.failures: list[Mapping[str, Any]] = []

    def commit_failed(self, record: Mapping[str, Any]) -> None:
        self.failures.append(record)


class BarrierRunContext:
    """A context reduced to what the commit-group barrier reads and writes.

    The barrier's collaborators are the two the failure already used -- the
    write seam and the reporter -- plus the two terminal writes it takes,
    which are recorded here rather than performed so the assertion is about
    what the barrier decided rather than about how a run ends.
    """

    def __init__(self) -> None:
        self.payload: dict[str, Any] = {}
        self.persistence = RecordingPersistence()
        self.progress = RecordingCommitProgress()
        self.owed_commit_group: dict[str, Any] | None = None
        self.owed_commit_group_attempts: dict[str, int] = {}
        self.unsettled_commit_group: dict[str, Any] | None = None
        self.final_snapshots: list[Any] = []
        self.pauses: list[dict[str, Any]] = []

    def record_final_snapshot(self, snapshot: Any) -> None:
        self.final_snapshots.append(snapshot)

    def pause(
        self, *, reason: str, message: str, details: dict[str, Any] | None
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


#: One durable group, the shape ``_commit_failure_record`` reads its identity
#: out of. Only the four keys the record names are populated, because a group
#: this run cannot settle is identified rather than replayed.
FAILING_GROUP: dict[str, Any] = {
    "group_id": "group-1",
    "scope": "session",
    "session_id": "5",
    "failed_alias": "app",
}


class CommitGroupBarrierTests(unittest.TestCase):
    """A run that spent a group's retries stops instead of selecting more work.

    The retry itself is bounded where it always was, in ``_soft_fail_commit``.
    What is asserted here is the second half the bound now has: exhausting it
    arms a barrier, and the barrier is read at the loop's selection boundary,
    so no session is launched over a commit this run could not make (ASI-138,
    ASI-139).
    """

    def setUp(self) -> None:
        self.ctx = BarrierRunContext()
        self.loop_source = inspect.getsource(run_loop.run_locked)

    def soft_fail(self, *, group: Mapping[str, Any] | None = FAILING_GROUP) -> None:
        run_loop._soft_fail_commit(
            self.ctx,  # type: ignore[arg-type]
            scope="session",
            session_id="5",
            group=group,
            commit=None,
            error="git refused the commit",
            message="the session commit could not be made",
        )

    def barrier(self) -> bool:
        return run_loop._pause_for_unsettled_commit_group(
            self.ctx,  # type: ignore[arg-type]
            snapshot="the-plan-image",  # type: ignore[arg-type]
        )

    def test_a_group_inside_its_budget_is_rearmed_and_raises_no_barrier(self) -> None:
        for spent in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES):
            with self.subTest(retries_spent=spent):
                self.soft_fail()
                # The retry is what the failure is still for, so the group
                # stays owed and nothing has been given up on yet.
                self.assertEqual(FAILING_GROUP, self.ctx.owed_commit_group)
                self.assertIsNone(self.ctx.unsettled_commit_group)
                self.assertFalse(self.barrier())
                self.assertEqual([], self.ctx.pauses)

    def test_a_group_that_spent_its_budget_arms_the_barrier(self) -> None:
        for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
            self.soft_fail()
        # Nothing is owed any more because nothing more will be attempted, and
        # the last failure is the one recorded as not retrying.
        self.assertIsNone(self.ctx.owed_commit_group)
        self.assertFalse(self.ctx.progress.failures[-1]["retrying"])
        armed = self.ctx.unsettled_commit_group
        assert armed is not None
        self.assertEqual("group-1", armed["group_id"])
        self.assertEqual("app", armed["failed_alias"])
        self.assertEqual(
            commit_settlement.MAX_COMMIT_GROUP_RETRIES, armed["retries_spent"]
        )
        self.assertEqual("git refused the commit", armed["error"])
        # The group is still owed durably, which is what lets a later run
        # recover it with the message that group asked for.
        failures = self.ctx.payload[commit_settlement.COMMIT_FAILURES_KEY]
        self.assertEqual("group-1", failures[-1]["group_id"])

    def test_the_armed_barrier_carries_the_failed_attempt_stderr_path(self) -> None:
        # ASI-160: the commit-failed remediation points at this file rather
        # than asking the operator to reproduce the error.
        for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
            run_loop._soft_fail_commit(
                self.ctx,  # type: ignore[arg-type]
                scope="session",
                session_id="5",
                group=FAILING_GROUP,
                commit={"stderr_path": "/state/commits/3/stderr.log"},
                error="git refused the commit",
                message="the session commit could not be made",
            )
        armed = self.ctx.unsettled_commit_group
        assert armed is not None
        self.assertEqual("/state/commits/3/stderr.log", armed["stderr_path"])

    def test_a_failure_with_no_group_never_arms_the_barrier(self) -> None:
        # An attempt that failed before any group existed has nothing durable
        # a later run could recover, so pausing on it would refuse the first
        # commit failure without ever retrying it.
        self.soft_fail(group=None)
        self.assertIsNone(self.ctx.owed_commit_group)
        self.assertIsNone(self.ctx.unsettled_commit_group)
        self.assertFalse(self.barrier())

    def test_an_unarmed_barrier_pauses_nothing(self) -> None:
        self.assertFalse(self.barrier())
        self.assertEqual([], self.ctx.pauses)
        self.assertEqual([], self.ctx.final_snapshots)

    def test_the_armed_barrier_pauses_commit_failed_and_names_the_group(self) -> None:
        for _ in range(commit_settlement.MAX_COMMIT_GROUP_RETRIES + 1):
            self.soft_fail()

        self.assertTrue(self.barrier())

        self.assertEqual(["the-plan-image"], self.ctx.final_snapshots)
        self.assertEqual(1, len(self.ctx.pauses))
        paused = self.ctx.pauses[0]
        self.assertEqual("commit-failed", paused["reason"])
        self.assertEqual(self.ctx.unsettled_commit_group, paused["details"])
        self.assertIn(
            str(commit_settlement.MAX_COMMIT_GROUP_RETRIES), paused["message"]
        )

    def test_the_pause_it_takes_is_one_no_answer_can_clear(self) -> None:
        # The barrier mints no vocabulary. `commit-failed` is already the
        # reason whose clearing path is the one this pause has -- the working
        # tree or the commit harness has to change on disk -- and being
        # environment-blocked rather than recoverable is also what keeps the
        # retained-work settlement from committing this pause's dirt under a
        # marking that says the session is back to `pending`.
        self.assertIn("commit-failed", pause_policy.ENVIRONMENT_BLOCKED_PAUSE_REASONS)
        self.assertNotIn("commit-failed", pause_policy.RECOVERABLE_PAUSE_REASONS)

    def test_the_barrier_is_checked_before_any_session_is_selected(self) -> None:
        # A placement rather than a condition: the barrier is about the
        # selection boundary, so it has to sit above the one read of
        # `next_pending` that both branches below depend on -- the branch that
        # launches the next session, and the completion branch that would
        # otherwise append a review or materialize repair children over the
        # same uncommitted tree.
        checked = self.loop_source.index("if _pause_for_unsettled_commit_group(")
        selection = "pending_session = snapshot.next_pending"
        self.assertEqual(1, self.loop_source.count(selection), "one selection point")
        self.assertLess(checked, self.loop_source.index(selection))
        # And below the settlement that spends the retry, so a group whose last
        # attempt fails is stopped on the same pass rather than one later.
        self.assertLess(
            self.loop_source.index("_settle_owed_commit_group(ctx)"), checked
        )


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
