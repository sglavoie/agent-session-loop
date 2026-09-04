"""The preflight boundary, exercised without the run loop.

``turn_preflight`` owns the last window before a harness process exists: what
the resumed repair child retained, what this turn *is*, and what it will later
be judged against. These tests drive its three steps through a stand-in run
context defined here rather than through ``runner``, which is the point of the
boundary.

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
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    external_write_evidence,
    handoff_guards,
    plans,
    repository_evidence,
    review_guard,
    run_loop,
    run_prompts,
    turn_preflight,
    turn_preparation,
    workspace_evidence,
)

from .support import assert_no_runner_import, write_plan

RUN_ID = "run-preflight-1"
SESSION_ID = "1"

#: The three entry points the run loop is allowed to reach here. A fourth would
#: be work the loop kept that this boundary was supposed to take.
ENTRY_POINTS = (
    "turn_preflight.resolve_retained_scope_repair",
    "turn_preflight.build_active_turn",
    "turn_preflight.capture_turn_baselines",
)

#: Capture and comparison calls that belonged to the loop and now belong here.
#: ``_latest_workspace_scope_repair_requirement`` is deliberately absent: the
#: readiness gate reads it once before any turn exists, and that reader is not
#: this boundary's to take.
MOVED_CALLS = frozenset(
    {
        "handoff_guards._capture_workspace_write_guard_snapshot",
        "handoff_guards._capture_legacy_implementation_snapshot",
        "review_guard._capture_review_repo_fingerprint",
        "review_guard._review_guard_unavailable_detail",
        "workspace_evidence._workspace_snapshot_unavailable",
        "workspace_evidence._workspace_unavailable_summary",
        "workspace_evidence._workspace_write_guard_evidence",
        "workspace_evidence._workspace_scope_repair_comparison",
        "workspace_evidence._workspace_scope_repair_is_fresh",
        "workspace_evidence._session_expansion_evidence",
        "external_write_evidence._capture_external_write_digests",
        "external_write_evidence._control_plane_digest_roots",
        "repository_evidence.compare_workspace_snapshots",
    }
)


class RecordingPersistence:
    """A stand-in for the run's durable-write mutex that keeps its calls.

    The lock is real -- the boundary has one durable write and a fake that did
    not hold a lock would hide a missing hold rather than expose it -- while
    events are kept in order so a test can assert on what was recorded without
    reading files back.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[tuple[str, dict[str, Any]]] = []

    def append_event(self, path: Path, name: str, payload: dict[str, Any]) -> None:
        with self.lock:
            self.events.append((name, json.loads(json.dumps(payload, default=str))))


class FakeVerificationContext:
    """The verification context, reduced to the members preflight reads."""

    def __init__(self, *, workspace: Any = None) -> None:
        self.workspace = workspace
        self.root = Path("/workspace")
        self.registry_digest = "digest"


class FakeRunContext:
    """A :class:`turn_preflight.RunContext` built by hand rather than by a run.

    Only the members the three preflight steps reach are populated, which is
    what the Protocol declares; anything else is absent so reaching it is an
    ``AttributeError`` here instead of a silently tolerated call.
    """

    def __init__(self, *, run_dir: Path, event_path: Path) -> None:
        self.run_id = RUN_ID
        self.run_dir = run_dir
        self.event_path = event_path
        self.persistence = RecordingPersistence()
        self.verification_context = FakeVerificationContext()
        self.runner_work_finished_at: Any = None
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


def fake_workspace(*aliases: str) -> Any:
    """A resolved workspace reduced to the repositories preflight enumerates."""
    return SimpleNamespace(
        repositories=tuple(
            SimpleNamespace(alias=alias, repository=SimpleNamespace(path=alias))
            for alias in aliases
        )
    )


class PreflightTestCase(unittest.TestCase):
    """A plan on disk, a fake context, and the arguments a step needs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.plan_dir = self.tmp_dir / "plan"
        self.run_dir = self.tmp_dir / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cwd = self.tmp_dir / "repo"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.event_path = self.run_dir / events.EVENTS_FILE_NAME
        self.ctx = FakeRunContext(run_dir=self.run_dir, event_path=self.event_path)

    def snapshot(self, statuses: tuple[str, ...] = ("pending",)) -> plans.PlanSnapshot:
        write_plan(self.plan_dir, statuses)
        return plans.inspect_plan(self.plan_dir)

    def classification(
        self,
        *,
        review: bool = False,
        repair: bool = False,
    ) -> turn_preparation.SessionClassification:
        return turn_preparation.SessionClassification(
            is_review_session=review,
            is_repair_session=repair,
            session_kind=(
                plans.SESSION_KIND_REVIEW
                if review
                else plans.SESSION_KIND_IMPLEMENTATION
            ),
        )

    def turn_prompt(self, **overrides: Any) -> turn_preparation.TurnPrompt:
        values: dict[str, Any] = {
            "prompt": "the words the harness receives",
            "prompt_kind": "session",
            "persisted_prompt": "the words the state file keeps",
            "reply_source": None,
            "resolution_source": None,
            "paused": False,
        }
        values.update(overrides)
        return turn_preparation.TurnPrompt(**values)

    def paths(self) -> dict[str, Path]:
        return {
            "artifact_dir": self.run_dir / "turn-1",
            "stdout": self.run_dir / "turn-1" / "stdout.log",
            "stderr": self.run_dir / "turn-1" / "stderr.log",
            "last_message": self.run_dir / "turn-1" / "last-message.txt",
            "result": self.run_dir / "turn-1" / "result.json",
        }

    def baselines(
        self,
        snapshot: plans.PlanSnapshot,
        *,
        classification: turn_preparation.SessionClassification,
        workspace: Any = None,
        session_scope: plans.SessionScope | None = None,
        review_base: Any = None,
        retained_scope_repair: dict[str, Any] | None = None,
        regenerated_write_scope: dict[str, Any] | None = None,
    ) -> turn_preflight.TurnBaselines:
        return turn_preflight.capture_turn_baselines(
            self.ctx,
            snapshot=snapshot,
            state_dir_arg=None,
            cwd=self.cwd,
            classification=classification,
            workspace=workspace,
            session_scope=session_scope,
            review_base=review_base,
            retained_scope_repair=retained_scope_repair,
            regenerated_write_scope=regenerated_write_scope,
            session_id=SESSION_ID,
            session_label="Session 1",
            session_details={"session_id": SESSION_ID},
            review_guard_excludes=(".venv",),
            review_guard_include_ignored=False,
            active_turn={"turn_index": 1},
            live_turn=mock.Mock(),
            paths=self.paths(),
        )

    def patch_digests(self, digests: dict[str, dict[str, Any]] | None = None) -> Any:
        """Stand in for the control-plane digest sweep, which walks real trees."""
        captured = {"plan": {"digest": "before"}} if digests is None else digests
        patcher = mock.patch.object(
            external_write_evidence,
            "_capture_external_write_digests",
            return_value=captured,
        )
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked


class PreflightBoundaryTests(unittest.TestCase):
    """The boundary's own shape, independent of any turn it prepares."""

    def module_source(self) -> ast.Module:
        source = Path(turn_preflight.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    def test_turn_preflight_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, turn_preflight)

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
        for record in (turn_preflight.ActiveTurn, turn_preflight.TurnBaselines):
            with self.subTest(record=record.__name__):
                self.assertTrue(record.__dataclass_params__.frozen)

    def test_the_protocol_names_only_members_the_run_context_has(self) -> None:
        # The Protocol exists so this module never imports the runner; that is
        # only true if the runner's own context object still fits through it.
        declared = {
            name for name in vars(turn_preflight.RunContext) if not name.startswith("_")
        }
        self.assertTrue(declared)
        fields = {field.name for field in dataclasses.fields(run_loop.RunContext)}
        for name in sorted(declared):
            with self.subTest(member=name):
                self.assertTrue(
                    name in fields or hasattr(run_loop.RunContext, name),
                    f"turn_preflight.RunContext declares {name!r}, which the "
                    "run loop's own context does not provide",
                )


class RunLoopDelegationTests(unittest.TestCase):
    """The run loop reaches this boundary and keeps none of its work."""

    def loop_source(self) -> str:
        return Path(run_loop.__file__ or "").read_text(encoding="utf-8")

    def test_the_loop_reaches_exactly_the_three_entry_points(self) -> None:
        calls = {
            ast.unparse(node.func)
            for node in ast.walk(ast.parse(self.loop_source()))
            if isinstance(node, ast.Call)
            and ast.unparse(node.func).startswith("turn_preflight.")
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


class LegacyBaselineTests(PreflightTestCase):
    """The single-repository mode: one snapshot, or one fingerprint."""

    def test_an_implementation_turn_captures_the_legacy_snapshot(self) -> None:
        self.patch_digests()
        snapshot = self.snapshot()
        captured = repository_evidence.WorkspaceSnapshot()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=captured,
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.baselines(snapshot, classification=self.classification())
        self.assertFalse(baselines.paused)
        self.assertIs(captured, baselines.legacy_snapshot)
        self.assertIsNone(baselines.review_fingerprint)
        self.assertTrue(baselines.protected_path_baseline)
        assert baselines.pending_exception_guard is not None
        self.assertEqual("legacy", baselines.pending_exception_guard["mode"])

    def test_an_unusable_legacy_snapshot_refuses_the_turn(self) -> None:
        self.patch_digests()
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_snapshot_unavailable",
                return_value={".": {"reason": "not-a-repository"}},
            ),
        ):
            baselines = self.baselines(snapshot, classification=self.classification())
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.pending_exception_guard)
        self.assertFalse(baselines.protected_path_baseline)
        self.assertEqual(
            [events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE],
            [pause["reason"] for pause in self.ctx.pauses],
        )
        self.assertEqual([snapshot], self.ctx.final_snapshots)

    def test_a_legacy_review_turn_is_armed_from_its_fingerprint(self) -> None:
        # Without this branch the turn enters the harness with no exception
        # context and a crash skips protected-path adjudication entirely.
        self.patch_digests()
        snapshot = self.snapshot()
        fingerprint = {"head": "abc", "dirty": False}
        with (
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                return_value=fingerprint,
            ),
            mock.patch.object(
                review_guard, "_review_guard_unavailable_detail", return_value=None
            ),
            mock.patch.object(
                handoff_guards, "_capture_legacy_implementation_snapshot"
            ) as legacy,
        ):
            baselines = self.baselines(
                snapshot, classification=self.classification(review=True)
            )
        legacy.assert_not_called()
        self.assertFalse(baselines.paused)
        self.assertIsNone(baselines.legacy_snapshot)
        self.assertIs(fingerprint, baselines.review_fingerprint)
        assert baselines.pending_exception_guard is not None
        self.assertEqual("review", baselines.pending_exception_guard["mode"])
        self.assertIs(
            fingerprint,
            baselines.pending_exception_guard["review_fingerprint_before"],
        )

    def test_a_review_fingerprint_counts_as_a_protected_path_baseline(self) -> None:
        # It proves the whole worktree unchanged, which clears every path rule
        # at once rather than leaving them unproven.
        self.patch_digests()
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                return_value={"head": "abc"},
            ),
            mock.patch.object(
                review_guard, "_review_guard_unavailable_detail", return_value=None
            ),
        ):
            baselines = self.baselines(
                snapshot, classification=self.classification(review=True)
            )
        self.assertIsNone(baselines.workspace_snapshot)
        self.assertIsNone(baselines.legacy_snapshot)
        self.assertTrue(baselines.protected_path_baseline)

    def test_an_unusable_review_fingerprint_refuses_the_turn(self) -> None:
        self.patch_digests()
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                review_guard,
                "_capture_review_repo_fingerprint",
                return_value={"head": None},
            ),
            mock.patch.object(
                review_guard,
                "_review_guard_unavailable_detail",
                return_value={"reason": "git-unavailable"},
            ),
        ):
            baselines = self.baselines(
                snapshot, classification=self.classification(review=True)
            )
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.pending_exception_guard)
        self.assertEqual(
            [events.PAUSE_REASON_REVIEW_GUARD_UNAVAILABLE],
            [pause["reason"] for pause in self.ctx.pauses],
        )

    def test_the_control_plane_is_digested_for_the_legacy_mode_too(self) -> None:
        # The repository snapshot is silent about the plan directory, the state
        # root and the run directory, so the digest is not made conditional.
        digests = self.patch_digests({"plan": {"digest": "x"}})
        snapshot = self.snapshot()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_legacy_implementation_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.baselines(snapshot, classification=self.classification())
        digests.assert_called_once()
        self.assertEqual({"plan": {"digest": "x"}}, baselines.external_digests)
        assert baselines.pending_exception_guard is not None
        self.assertEqual(
            {"plan": {"digest": "x"}},
            baselines.pending_exception_guard["external_digests_before"],
        )
        self.assertGreater(baselines.turn_started_ns, 0)


class WorkspaceBaselineTests(PreflightTestCase):
    """The registered-workspace mode: one snapshot, and the repair it clears."""

    def setUp(self) -> None:
        super().setUp()
        self.workspace = fake_workspace("app", "docs")
        self.scope = plans.SessionScope(reads=("app", "docs"), writes=("app",))

    def capture(self, **overrides: Any) -> turn_preflight.TurnBaselines:
        snapshot = overrides.pop("snapshot", None) or self.snapshot()
        return self.baselines(
            snapshot,
            classification=overrides.pop(
                "classification", self.classification(repair=True)
            ),
            workspace=self.workspace,
            session_scope=self.scope,
            **overrides,
        )

    def test_a_prepared_review_base_is_reused_rather_than_recaptured(self) -> None:
        # The guard must measure against the base the session file declares,
        # not a second capture taken moments later.
        self.patch_digests()
        base = repository_evidence.WorkspaceSnapshot()
        with (
            mock.patch.object(
                handoff_guards, "_capture_workspace_write_guard_snapshot"
            ) as recapture,
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
        ):
            baselines = self.capture(
                classification=self.classification(review=True), review_base=base
            )
        recapture.assert_not_called()
        self.assertIs(base, baselines.workspace_snapshot)
        assert baselines.pending_exception_guard is not None
        self.assertEqual("workspace", baselines.pending_exception_guard["mode"])

    def test_an_inventory_failure_refuses_the_turn(self) -> None:
        self.patch_digests()
        with mock.patch.object(
            handoff_guards,
            "_capture_workspace_write_guard_snapshot",
            side_effect=repository_evidence.RepositoryEvidenceError("git is gone"),
        ):
            baselines = self.capture()
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.workspace_snapshot)
        self.assertIsNone(baselines.pending_exception_guard)
        (pause,) = self.ctx.pauses
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_GUARD_UNAVAILABLE, pause["reason"]
        )
        guard = pause["details"]["workspace_write_guard"]
        self.assertEqual(
            "workspace-inventory-failed", guard["unavailable"]["."]["reason"]
        )
        self.assertEqual(["app", "docs"], guard["read_aliases"])
        self.assertEqual(["app"], guard["write_aliases"])

    def test_a_retained_repair_that_cleared_its_scope_is_recorded(self) -> None:
        self.patch_digests()
        evidence = {"passed": True, "violations": []}
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_workspace_write_guard_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
            mock.patch.object(
                repository_evidence, "compare_workspace_snapshots", return_value={}
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_comparison",
                return_value=({}, ["docs"]),
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_write_guard_evidence",
                return_value=dict(evidence),
            ),
            mock.patch.object(
                workspace_evidence,
                "_session_expansion_evidence",
                return_value={"digest": "now"},
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_is_fresh",
                return_value=True,
            ),
        ):
            baselines = self.capture(
                retained_scope_repair={
                    "session_id": SESSION_ID,
                    "snapshot_before": {},
                    "expansion_before_repair": {"digest": "then"},
                },
                regenerated_write_scope={"widened": ["docs"]},
            )
        self.assertFalse(baselines.paused)
        (name, payload) = self.ctx.persistence.events[0]
        self.assertEqual(events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT, name)
        recorded = payload["details"]["workspace_write_guard"]
        self.assertEqual(["docs"], recorded["repair_registry_delta"])
        self.assertEqual({"widened": ["docs"]}, recorded["write_scope_regeneration"])
        self.assertEqual({"digest": "now"}, payload["details"]["expansion"])

    def test_a_retained_repair_without_a_fresh_expansion_refuses_the_turn(self) -> None:
        # A scope that is clean only because nobody re-ran expansion is not
        # proof, so the clean comparison alone must not clear the repair.
        self.patch_digests()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_workspace_write_guard_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
            mock.patch.object(
                repository_evidence, "compare_workspace_snapshots", return_value={}
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_comparison",
                return_value=({}, []),
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_write_guard_evidence",
                return_value={"passed": True, "violations": []},
            ),
            mock.patch.object(
                workspace_evidence,
                "_session_expansion_evidence",
                return_value={"digest": "now"},
            ),
            mock.patch.object(
                workspace_evidence,
                "_workspace_scope_repair_is_fresh",
                return_value=False,
            ),
        ):
            baselines = self.capture(
                retained_scope_repair={
                    "session_id": SESSION_ID,
                    "snapshot_before": {},
                    "expansion_before_repair": {"digest": "then"},
                }
            )
        self.assertTrue(baselines.paused)
        self.assertIsNone(baselines.pending_exception_guard)
        self.assertEqual([], self.ctx.persistence.events)
        (pause,) = self.ctx.pauses
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION, pause["reason"]
        )
        self.assertFalse(
            pause["details"]["workspace_write_guard"]["expansion_fresh"],
        )

    def test_a_retained_repair_with_no_baseline_cannot_clear_itself(self) -> None:
        self.patch_digests()
        with (
            mock.patch.object(
                handoff_guards,
                "_capture_workspace_write_guard_snapshot",
                return_value=repository_evidence.WorkspaceSnapshot(),
            ),
            mock.patch.object(
                workspace_evidence, "_workspace_snapshot_unavailable", return_value={}
            ),
            mock.patch.object(
                repository_evidence, "compare_workspace_snapshots"
            ) as compare,
            mock.patch.object(
                workspace_evidence,
                "_session_expansion_evidence",
                return_value={"digest": "now"},
            ),
        ):
            baselines = self.capture(
                retained_scope_repair={
                    "session_id": SESSION_ID,
                    "snapshot_before": None,
                }
            )
        compare.assert_not_called()
        self.assertTrue(baselines.paused)
        (pause,) = self.ctx.pauses
        guard = pause["details"]["workspace_write_guard"]["current_scope_evidence"]
        self.assertEqual("repair-baseline-missing", guard["unavailable"]["."]["reason"])


if __name__ == "__main__":
    unittest.main()
