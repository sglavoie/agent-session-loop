"""The post-execution guard sequence, exercised without the run loop.

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
import contextlib
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_guards,
    harnesses,
    plans,
    run_loop,
    turn_postflight,
    workspace_evidence,
    write_guards,
)
from agent_session_loop.run_errors import RunError

from .support import assert_no_runner_import, write_plan

RUN_ID = "run-postflight-1"
SESSION_ID = "1"

#: The one entry point the run loop is allowed to reach here. A second would be
#: a guard the loop kept sequencing itself.
ENTRY_POINTS = ("turn_postflight.enforce_post_turn_guards",)

#: The guards this boundary sequences, in the order it must run them, named as
#: ``module._name`` so a recorder can report the order it observed.
WORKSPACE_WRITE = "write_guards._enforce_workspace_write_guard"
LEGACY_WRITE = "write_guards._enforce_legacy_implementation_write_guard"
REVIEW_MUTATION = "handoff_guards._enforce_review_mutation_guard"
PROTECTED_PATH = "write_guards._enforce_protected_path_guard"
PLAN_ARTIFACT = "handoff_guards._enforce_plan_artifact_guard"
DISPOSITION = "handoff_guards._terminating_result_disposition"
DISARM = "disarm_exception_guard"

#: Sequencing calls that belonged to the loop and now belong here. The two write
#: guards, the protected-path guard and the change-path projection are
#: deliberately absent: the runner's ``except BaseException`` handler re-runs
#: them from the armed crash-path context, which is a different call site with a
#: different before-image. The plan artifact guard is absent for the same kind of
#: reason -- the status-guard path below the loop's result branches has its own
#: call to it.
MOVED_CALLS = frozenset({REVIEW_MUTATION, DISPOSITION, "_workspace_review_bases"})


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real -- this boundary rewrites the turn's result artifact under
    it, and a fake that did not hold a lock would hide a missing hold rather
    than expose it -- while events are kept in order so a test can assert on
    what was recorded without reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))


class FakeRunContext:
    """A :class:`turn_postflight.RunContext` built by hand rather than by a run.

    Only the members the five guards reach are populated, which is what the
    Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_dir = run_dir
        self.event_path = event_path
        self.payload: dict[str, Any] = {"run_id": RUN_ID}
        self.persistence = RecordingPersistence()
        self.plan_cache = plans.PlanSnapshotCache()
        self.guard_controller = mock.Mock()
        self.pauses: list[dict[str, Any]] = []

    def pause(
        self,
        *,
        reason: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        self.pauses.append({"reason": reason, "message": message, "details": details})


class GuardRecorder:
    """Every guard in the sequence, replaced by a recorder of order and calls.

    Each guard's verdict is a value the test sets, so what the sequence does
    with a refusal is asserted rather than simulated: the recorder never touches
    the filesystem and never spawns a process, which is what keeps this module
    portable.
    """

    def __init__(self) -> None:
        self.order: list[str] = []
        self.calls: dict[str, dict[str, Any]] = {}
        self.verdicts: dict[str, bool] = {}
        self.disposition: str | None = "interrupted"
        self.review_bases: list[dict[str, Any]] | RunError = [{"alias": "app"}]
        self.changed_paths: tuple[str, ...] | None = ("app:src/main.py",)

    def _guard(self, name: str) -> Any:
        def recorder(*args: Any, **kwargs: Any) -> bool:
            self.order.append(name)
            self.calls[name] = kwargs
            return self.verdicts.get(name, False)

        return recorder

    def _disposition(self, *args: Any, **kwargs: Any) -> str | None:
        self.order.append(DISPOSITION)
        return self.disposition

    def _bases(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        if isinstance(self.review_bases, RunError):
            raise self.review_bases
        return self.review_bases

    def disarm(self) -> None:
        self.order.append(DISARM)

    @contextlib.contextmanager
    def patched(self) -> Any:
        targets = (
            (write_guards, "_enforce_workspace_write_guard", WORKSPACE_WRITE),
            (write_guards, "_enforce_legacy_implementation_write_guard", LEGACY_WRITE),
            (handoff_guards, "_enforce_review_mutation_guard", REVIEW_MUTATION),
            (write_guards, "_enforce_protected_path_guard", PROTECTED_PATH),
            (handoff_guards, "_enforce_plan_artifact_guard", PLAN_ARTIFACT),
        )
        with contextlib.ExitStack() as stack:
            for module, attribute, name in targets:
                stack.enter_context(
                    mock.patch.object(module, attribute, self._guard(name))
                )
            stack.enter_context(
                mock.patch.object(
                    handoff_guards,
                    "_terminating_result_disposition",
                    self._disposition,
                )
            )
            stack.enter_context(
                mock.patch.object(
                    workspace_evidence, "_workspace_review_bases", self._bases
                )
            )
            stack.enter_context(
                mock.patch.object(
                    write_guards,
                    "_protected_path_changed_paths",
                    lambda evidence: self.changed_paths,
                )
            )
            yield self


def fake_workspace(*aliases: str) -> Any:
    """A resolved workspace reduced to what the sequence hands its guards."""
    return mock.Mock(name="workspace", repositories=tuple(aliases))


class PostflightTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments the sequence needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.artifact_dir = self.run_dir / "turn-1"
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = self.tmp_dir / "repo"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)
        write_plan(self.plan_dir, ("pending",))
        self.before = plans.inspect_plan(self.plan_dir)
        self.recorder = GuardRecorder()

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.artifact_dir,
            "stdout": self.artifact_dir / "stdout.log",
            "stderr": self.artifact_dir / "stderr.log",
            "last_message": self.artifact_dir / "last-message.txt",
            "result": self.artifact_dir / "result.json",
        }

    def result(self, **overrides: Any) -> harnesses.HarnessResult:
        values: dict[str, Any] = {
            "argv": ["claude"],
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        }
        values.update(overrides)
        return harnesses.HarnessResult(**values)

    def enforce(
        self,
        *,
        workspace: Any = None,
        session_scope: Any = None,
        workspace_snapshot_before: Any = None,
        legacy_snapshot_before: Any = None,
        review_fingerprint_before: dict[str, Any] | None = None,
        is_review_session: bool = False,
        turn_payload: dict[str, Any] | None = None,
        after: plans.PlanSnapshot | None = None,
    ) -> turn_postflight.PostTurnGuards:
        with self.recorder.patched():
            return turn_postflight.enforce_post_turn_guards(
                self.ctx,
                before=self.before,
                after=after,
                result=self.result(),
                turn_payload={} if turn_payload is None else turn_payload,
                workspace=workspace,
                session_scope=session_scope,
                workspace_snapshot_before=workspace_snapshot_before,
                legacy_snapshot_before=legacy_snapshot_before,
                review_fingerprint_before=review_fingerprint_before,
                external_digests_before={"plan": {"digest": "before"}},
                turn_started_ns=1,
                plan_artifacts_before={"session-2.md": "digest"},
                plan_artifacts_written_by_runner=(),
                is_review_session=is_review_session,
                cwd=self.cwd,
                session_id=SESSION_ID,
                session_label="Session 1",
                session_details={"session_id": SESSION_ID},
                review_guard_excludes=(".venv",),
                review_guard_include_ignored=False,
                paths=self.paths(),
                state_dir_arg=None,
                disarm_exception_guard=self.recorder.disarm,
            )

    def workspace_turn(self, **overrides: Any) -> turn_postflight.PostTurnGuards:
        """A turn in the registered-workspace mode, the common case."""
        arguments: dict[str, Any] = {
            "workspace": fake_workspace("app"),
            "session_scope": mock.Mock(name="scope"),
            "workspace_snapshot_before": {"app": {"head": "abc"}},
        }
        arguments.update(overrides)
        return self.enforce(**arguments)

    def legacy_turn(self, **overrides: Any) -> turn_postflight.PostTurnGuards:
        """A turn in the legacy single-repository implementation mode."""
        arguments: dict[str, Any] = {"legacy_snapshot_before": {".": {"head": "abc"}}}
        arguments.update(overrides)
        return self.enforce(**arguments)

    def legacy_review_turn(self, **overrides: Any) -> turn_postflight.PostTurnGuards:
        """A legacy review turn, whose only comparison is its fingerprint."""
        arguments: dict[str, Any] = {
            "review_fingerprint_before": {".": {"head": "abc"}},
            "is_review_session": True,
        }
        arguments.update(overrides)
        return self.enforce(**arguments)


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
        fields = {field.name for field in dataclasses.fields(run_loop.RunContext)}
        for name in sorted(declared):
            with self.subTest(member=name):
                self.assertTrue(
                    name in fields or hasattr(run_loop.RunContext, name),
                    f"turn_postflight.RunContext declares {name!r}, which the "
                    "run loop's own context does not provide",
                )


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of its sequencing."""

    def loop_source(self) -> str:
        return Path(run_loop.__file__ or "").read_text(encoding="utf-8")

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


class FinalizationPauseTests(PostflightTestCase):
    """A guard that pauses still has to reach the protected-path step."""

    def test_a_paused_write_guard_still_adjudicates_the_protected_paths(self) -> None:
        self.recorder.verdicts[WORKSPACE_WRITE] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIn(PROTECTED_PATH, self.recorder.order)
        self.assertTrue(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])
        self.assertNotIn(PLAN_ARTIFACT, self.recorder.order)

    def test_a_paused_mutation_guard_still_adjudicates_the_protected_paths(
        self,
    ) -> None:
        self.recorder.verdicts[REVIEW_MUTATION] = True

        outcome = self.legacy_review_turn()

        self.assertTrue(outcome.stopped)
        self.assertTrue(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])

    def test_a_paused_write_guard_skips_the_mutation_guard(self) -> None:
        # One refusal per turn: the mutation guard would otherwise record a
        # second verdict against a comparison the write guard already refused.
        self.recorder.verdicts[WORKSPACE_WRITE] = True

        self.workspace_turn(is_review_session=True, review_fingerprint_before={})

        self.assertNotIn(REVIEW_MUTATION, self.recorder.order)

    def test_a_clean_turn_adjudicates_nothing(self) -> None:
        self.workspace_turn()

        self.assertFalse(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])


class ProtectedPathEvidenceTests(PostflightTestCase):
    """ "No baseline" and "proven clean" stay distinct answers."""

    def test_a_write_guard_comparison_supplies_the_changed_paths(self) -> None:
        self.recorder.changed_paths = ("app:src/main.py",)

        self.workspace_turn(turn_payload={"workspace_write_guard": {"passed": True}})

        self.assertEqual(
            ("app:src/main.py",),
            self.recorder.calls[PROTECTED_PATH]["changed_paths"],
        )

    def test_a_passing_legacy_review_turn_clears_every_path(self) -> None:
        # No write-guard comparison exists, but an unchanged worktree is a
        # stronger clearance than an empty change list -- not a missing one.
        self.legacy_review_turn()

        self.assertEqual((), self.recorder.calls[PROTECTED_PATH]["changed_paths"])

    def test_a_paused_legacy_review_turn_clears_nothing(self) -> None:
        self.recorder.verdicts[REVIEW_MUTATION] = True

        self.legacy_review_turn()

        self.assertIsNone(self.recorder.calls[PROTECTED_PATH]["changed_paths"])

    def test_a_turn_with_neither_evidence_reports_no_baseline(self) -> None:
        self.workspace_turn()

        self.assertIsNone(self.recorder.calls[PROTECTED_PATH]["changed_paths"])

    def test_a_convicted_protected_path_stops_the_turn(self) -> None:
        self.recorder.verdicts[PROTECTED_PATH] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIsNone(outcome.terminating_disposition)
        self.assertNotIn(PLAN_ARTIFACT, self.recorder.order)


class TerminatingDispositionTests(PostflightTestCase):
    """The artifact guard is gated on the turn having a branch to take."""

    def test_a_turn_that_reaches_the_status_guards_skips_the_artifact_guard(
        self,
    ) -> None:
        self.recorder.disposition = None

        outcome = self.workspace_turn()

        self.assertFalse(outcome.stopped)
        self.assertIsNone(outcome.terminating_disposition)
        self.assertNotIn(PLAN_ARTIFACT, self.recorder.order)

    def test_a_terminating_turn_runs_the_artifact_guard_with_its_own_name(self) -> None:
        self.recorder.disposition = "timeout"

        outcome = self.workspace_turn()

        self.assertEqual("timeout", outcome.terminating_disposition)
        self.assertEqual(
            "timeout", self.recorder.calls[PLAN_ARTIFACT]["result_disposition"]
        )

    def test_a_refused_artifact_guard_stops_the_turn(self) -> None:
        self.recorder.verdicts[PLAN_ARTIFACT] = True

        outcome = self.workspace_turn()

        self.assertTrue(outcome.stopped)
        self.assertIsNone(outcome.terminating_disposition)


class ReviewBaseProjectionTests(PostflightTestCase):
    """The one durable record this boundary writes on its own."""

    def test_a_passing_workspace_review_turn_rewrites_its_result_artifact(
        self,
    ) -> None:
        turn_payload: dict[str, Any] = {"turn_index": 1}

        self.workspace_turn(is_review_session=True, turn_payload=turn_payload)

        self.assertEqual([{"alias": "app"}], turn_payload["review_repository_bases"])
        written = json.loads(
            self.paths()["result"].read_text(encoding="utf-8"),
        )
        self.assertEqual([{"alias": "app"}], written["review_repository_bases"])

    def test_a_non_review_turn_projects_no_bases(self) -> None:
        turn_payload: dict[str, Any] = {"turn_index": 1}

        self.workspace_turn(turn_payload=turn_payload)

        self.assertNotIn("review_repository_bases", turn_payload)
        self.assertFalse(self.paths()["result"].exists())

    def test_a_paused_write_guard_projects_no_bases(self) -> None:
        self.recorder.verdicts[WORKSPACE_WRITE] = True
        turn_payload: dict[str, Any] = {"turn_index": 1}

        self.workspace_turn(is_review_session=True, turn_payload=turn_payload)

        self.assertNotIn("review_repository_bases", turn_payload)

    def test_bases_that_cannot_be_projected_pause_the_run(self) -> None:
        self.recorder.review_bases = RunError("no base for app")

        outcome = self.workspace_turn(is_review_session=True, after=self.before)

        self.assertTrue(outcome.stopped)
        (pause,) = self.ctx.pauses
        self.assertEqual(events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE, pause["reason"])
        self.assertEqual("no base for app", pause["details"]["error"])
        self.assertIn("final_snapshot", self.ctx.payload)
        # The refusal still reaches the protected-path step, like every other
        # finalization pause.
        self.assertTrue(self.recorder.calls[PROTECTED_PATH]["adjudicate_only"])

    def test_the_final_snapshot_falls_back_to_the_before_image(self) -> None:
        self.recorder.review_bases = RunError("no base for app")

        self.workspace_turn(is_review_session=True, after=None)

        self.assertEqual(
            plans.snapshot_payload(self.before), self.ctx.payload["final_snapshot"]
        )


if __name__ == "__main__":
    unittest.main()
