"""Boundary tests for `agent_session_loop.write_guards`.

The behavioural coverage of these guards is end to end and lives elsewhere:
`tests/test_asl_run_protected_path_guard.py` drives real runs against real checkouts and
asserts what an operator sees after a violation, and `tests/test_asl_turn_commit_*.py`
pins that the commit happens after every guard. Neither can cheaply state the
things this module owns, so they are stated here: the extraction contract
(runner-free, no alias left behind, the seams required rather than defaulted and
supplied at every call site), the five-step refusal every enforcement path
performs in one order, and the four functions whose inputs are payloads rather
than repositories.

Nothing here spawns a process. The two functions that would reach Git --
`_protected_path_review_changed_paths` through the review fingerprint and
`_regenerate_stale_repair_child_scopes` through the scope reader -- are exercised
against their collaborators' seams rather than against checkouts, which is what
keeps this module in `PORTABLE_MODULES`.
"""

from __future__ import annotations

import ast
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    guards,
    handoff_correction,
    handoff_guards,
    plans_errors,
    plans_session_scope,
    plans_snapshot,
    review_continuation,
    review_cycle,
    review_guard,
    run_loop_finalization,
    run_prompts,
    run_termination,
    runner,
    session_verification,
    turn_postflight,
    workspace_evidence,
    workspaces,
    write_guards,
)
from tests.support import assert_no_runner_import, called_names, write_plan

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_regenerate_stale_repair_child_scopes",
    "_pending_repair_child_continuation",
    "_enforce_workspace_write_guard",
    "_enforce_legacy_implementation_write_guard",
    "_protected_path_changed_paths",
    "_protected_path_review_changed_paths",
    "_enforce_protected_path_guard",
)

#: Moved with them, because its sole reader is
#: `_pending_repair_child_continuation`.
MOVED_CONSTANTS = ("REPAIR_SCOPE_STALE_BLOCKER",)

#: The three enforcement functions, which are the only ones that take seams and
#: the only ones that can refuse a turn.
ENFORCEMENT_FUNCTIONS = (
    "_enforce_workspace_write_guard",
    "_enforce_legacy_implementation_write_guard",
    "_enforce_protected_path_guard",
)

#: The collaborators this boundary does not own -- supplied by
#: ``handoff_correction._append_handoff_note`` and
#: ``session_verification._reinspect_after_revert`` -- and the names this
#: module receives them under. Neither may acquire a default: a refusal that
#: silently skipped the note or the re-read would leave a session `complete`
#: after the guard refused it.
SEAM_PARAMETERS = ("append_handoff_note", "reinspect")

#: Every call this boundary could make that outlives the process. Anything a
#: function here learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when
#: a new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "mkdir",
        "rename",
        "replace",
        "rmdir",
        "touch",
        "unlink",
        "utime",
        "write_bytes",
        "write_json_file",
        "write_state",
        "write_text",
    }
)

#: Which module-level function writes directly, and what it writes. Only the
#: turn result is written here: the session-file revert belongs to
#: `handoff_guards`, the scope widening to `workspace_evidence`, and the two
#: events to `turn_execution_commit` and `run_termination`, each of which owns that
#: write at its own boundary. Stating it per function rather than as a flat
#: prohibition is what makes a *fourth* writer appearing here a test failure
#: instead of a review catch.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_enforce_workspace_write_guard": frozenset({"write_json_file"}),
    "_enforce_legacy_implementation_write_guard": frozenset({"write_json_file"}),
    "_enforce_protected_path_guard": frozenset({"write_json_file"}),
}


def _workspace(*aliases: str) -> workspaces.ResolvedWorkspace:
    """A resolved workspace built from names, with no checkout behind it."""
    root = Path("/tmp/workspace")
    return workspaces.ResolvedWorkspace(
        root=root,
        repositories=tuple(
            workspaces.ResolvedRepository(
                repository=workspaces.Repository(
                    alias,
                    alias.lower(),
                    f"git@github.com:example/{alias.lower()}.git",
                ),
                root=root / alias.lower(),
                origin=f"git@github.com:example/{alias.lower()}.git",
            )
            for alias in aliases
        ),
    )


def _resume(
    *,
    action: str = "run-repairs",
    eligible: bool = False,
    blocker: str | None = write_guards.REPAIR_SCOPE_STALE_BLOCKER,
    gaps: tuple[dict[str, Any], ...] | None = ({"repair_session_id": "2"},),
) -> review_continuation.ReviewResume:
    """One review continuation projection, named only by what the gate reads."""
    return review_continuation.ReviewResume(
        source_cycle=1,
        review_session_id="3",
        source_disposition="findings",
        action=cast(Any, action),
        eligible=eligible,
        blocker=blocker,
        historical_materializations=1,
        minimum_required_cap=1,
        selected_cap=1,
        repair_scope_gaps=gaps,
    )


class WriteGuardsBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes in one place."""

    def setUp(self) -> None:
        source_path = Path(write_guards.__file__ or "")
        self.source = source_path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, write_guards)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_FUNCTIONS + MOVED_CONSTANTS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(write_guards, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        defined = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.assertEqual(set(MOVED_FUNCTIONS), defined)

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = called_names(node) & DURABLE_WRITE_NAMES
            if writes:
                observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)


class RefusalShapeTests(unittest.TestCase):
    """A refusal is five steps, and every enforcement path performs all five."""

    def _source(self, name: str) -> str:
        return inspect.getsource(getattr(write_guards, name))

    def test_every_refusal_reverts_rereads_writes_records_then_pauses(self) -> None:
        for name in ENFORCEMENT_FUNCTIONS:
            with self.subTest(name=name):
                source = self._source(name)
                reverted = source.index(
                    "handoff_guards._revert_session_status_to_pending("
                )
                reinspected = source.index("inspected = reinspect(")
                # The pass lane writes the result too, so the refusal's own
                # write is the last one rather than the first.
                written = source.rindex("durability.write_json_file(")
                recorded = source.rindex("turn_execution_commit.record_turn_rejected(")
                paused = source.index("run_termination._pause_run(")
                self.assertLess(reverted, reinspected)
                self.assertLess(reinspected, written)
                self.assertLess(written, recorded)
                self.assertLess(recorded, paused)

    def test_every_refusal_republishes_the_snapshot_it_just_reread(self) -> None:
        # The revert moved a status, so the snapshot the run publishes must be
        # the post-revert one; publishing the pre-revert snapshot would report
        # a session as complete that the guard had just reopened.
        for name in ENFORCEMENT_FUNCTIONS:
            with self.subTest(name=name):
                self.assertIn(
                    'payload["final_snapshot"] = '
                    "plans_payloads.snapshot_payload(inspected)",
                    self._source(name),
                )


class SeamTests(unittest.TestCase):
    """The two runner-owned collaborators enter as required keyword-only seams."""

    def test_both_seams_are_required_keyword_only_everywhere(self) -> None:
        for name in ENFORCEMENT_FUNCTIONS:
            signature = inspect.signature(getattr(write_guards, name))
            for seam in SEAM_PARAMETERS:
                with self.subTest(name=name, seam=seam):
                    parameter = signature.parameters[seam]
                    self.assertIs(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                    self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_the_seam_protocols_are_satisfied_by_the_symbols_the_loop_passes(
        self,
    ) -> None:
        # Session 23 moved the re-read into `session_verification` and Session 24
        # moved the appender into `handoff_correction`; both stay seams because
        # `handoff_correction` imports both of those modules, so this is what
        # checks the two declared shapes against the symbols actually passed,
        # rather than only the loop's call sites doing so at runtime.
        appender: handoff_guards.HandoffNoteAppender = (
            handoff_correction._append_handoff_note
        )
        reinspect: run_termination.SnapshotReinspector = (
            session_verification._reinspect_after_revert
        )
        self.assertEqual(
            ["- **Notes**:", "  - guard refused"],
            appender(
                ["## Handoff", "", "- **Notes**:"],
                handoff_index=0,
                section_end=3,
                note="guard refused",
            )[2:],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            plan_dir = Path(tmp_dir).resolve() / "plan"
            write_plan(plan_dir, ("pending",))
            fallback = plans_snapshot.inspect_plan(plan_dir)
            # Nothing was reverted, so the fallback snapshot is returned as it
            # stands rather than re-read.
            self.assertIs(
                fallback,
                reinspect(plan_dir, fallback, None, plans_snapshot.PlanSnapshotCache()),
            )

    def test_the_loop_passes_both_seams_at_every_enforcement_call(self) -> None:
        # The two paths live in two modules since Session 34: the finalization
        # sequence moved into `turn_postflight`, while the crash path stayed
        # with the loop's own `except BaseException` handler -- which Session
        # 37 carried out of the runner into `run_loop`. Both are scanned, and
        # each is counted on its own, so a call that merely moved between them
        # cannot pass by keeping the total.
        calls: dict[str, int] = {}
        for module in (run_loop_finalization, turn_postflight):
            tree = ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not isinstance(func, ast.Attribute):
                    continue
                if (
                    not isinstance(func.value, ast.Name)
                    or func.value.id != "write_guards"
                ):
                    continue
                if func.attr not in ENFORCEMENT_FUNCTIONS:
                    continue
                calls[module.__name__] = calls.get(module.__name__, 0) + 1
                passed = {keyword.arg for keyword in node.keywords}
                with self.subTest(module=module.__name__, line=node.lineno):
                    self.assertLessEqual(set(SEAM_PARAMETERS), passed)
        # One per guard on each path: the finalization sequence and the crash
        # path each reach all three.
        self.assertEqual(
            {
                "agent_session_loop.run_loop_finalization": 3,
                "agent_session_loop.turn_postflight": 3,
            },
            calls,
        )


class ProtectedPathChangedPathsTests(unittest.TestCase):
    """What the guard reads out of the write guard's own recorded evidence."""

    def test_a_missing_comparison_cannot_answer_the_question(self) -> None:
        self.assertIsNone(write_guards._protected_path_changed_paths(None))

    def test_a_partial_inventory_cannot_answer_the_question(self) -> None:
        # An unavailable lane means the guard did not look everywhere, so an
        # empty `changed` map is not evidence that nothing moved.
        self.assertIsNone(
            write_guards._protected_path_changed_paths(
                {
                    "changed": {},
                    "unavailable": {"svc": {"after": {"reason": "unreadable"}}},
                }
            )
        )

    def test_a_comparison_without_a_changed_map_cannot_answer_it_either(self) -> None:
        self.assertIsNone(write_guards._protected_path_changed_paths({}))
        self.assertIsNone(
            write_guards._protected_path_changed_paths({"changed": ["svc"]})
        )

    def test_a_complete_comparison_with_nothing_changed_clears_every_rule(
        self,
    ) -> None:
        self.assertEqual(
            (),
            write_guards._protected_path_changed_paths(
                {"changed": {}, "unavailable": {}}
            ),
        )

    def test_qualified_paths_are_collected_across_repositories_in_order(self) -> None:
        self.assertEqual(
            ("svc:a.py", "svc:b.py", "ops:c.py"),
            write_guards._protected_path_changed_paths(
                {
                    "changed": {
                        "svc": {"qualified_paths": ["svc:a.py", "svc:b.py"]},
                        "ops": {"qualified_paths": ["ops:c.py"]},
                    }
                }
            ),
        )

    def test_the_same_path_named_twice_is_reported_once(self) -> None:
        self.assertEqual(
            ("svc:a.py",),
            write_guards._protected_path_changed_paths(
                {
                    "changed": {
                        "svc": {"qualified_paths": ["svc:a.py"]},
                        "svc-worktree": {"qualified_paths": ["svc:a.py"]},
                    }
                }
            ),
        )

    def test_an_entry_that_names_no_paths_contributes_nothing(self) -> None:
        self.assertEqual(
            ("svc:a.py",),
            write_guards._protected_path_changed_paths(
                {
                    "changed": {
                        "svc": {"qualified_paths": ["svc:a.py"]},
                        "ops": {"qualified_paths": "ops:c.py"},
                        "docs": ["not a mapping"],
                    }
                }
            ),
        )


class ProtectedPathReviewChangedPathsTests(unittest.TestCase):
    """The recapture a crashing legacy review turn leaves the guard to make."""

    def setUp(self) -> None:
        self.context: dict[str, Any] = {
            "review_fingerprint_before": {"git": True, "head": "abc"},
            "cwd": Path("/tmp/repo"),
            "before": SimpleNamespace(plan_dir=Path("/tmp/plan")),
        }

    def _changed(self, **overrides: Any) -> tuple[str, ...] | None:
        context = {**self.context, **overrides}
        return write_guards._protected_path_review_changed_paths(
            context, exclude=(), include_ignored=False
        )

    def test_no_turn_start_fingerprint_cannot_answer_the_question(self) -> None:
        self.assertIsNone(self._changed(review_fingerprint_before=None))

    def test_a_recapture_that_raised_cannot_answer_it_either(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            side_effect=OSError("boom"),
        ):
            self.assertIsNone(self._changed())

    def test_a_recapture_that_came_back_unusable_is_unavailable(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            return_value={"git": True, "unavailable": {"reason": "unreadable"}},
        ):
            self.assertIsNone(self._changed())

    def test_an_unmoved_fingerprint_clears_every_rule_at_once(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            return_value={"git": True, "head": "abc"},
        ):
            self.assertEqual((), self._changed())

    def test_a_moved_fingerprint_names_its_paths_qualified_as_legacy(self) -> None:
        with mock.patch.object(
            review_guard,
            "_capture_review_repo_fingerprint",
            return_value={
                "git": True,
                "head": "abc",
                "tracked_patches": [["src/app.py", "sha-after"]],
                "untracked": [["notes.md", "file", "sha"]],
            },
        ):
            self.assertEqual(
                (
                    f"{guards.LEGACY_PATH_QUALIFIER}:src/app.py",
                    f"{guards.LEGACY_PATH_QUALIFIER}:notes.md",
                ),
                self._changed(),
            )


class RegenerateStaleRepairChildScopesTests(unittest.TestCase):
    """Which pending children a scope-stale projection gets to widen."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        plan_dir = Path(self._tmp.name).resolve() / "plan"
        write_plan(plan_dir, ("complete", "pending", "pending"))
        self.snapshot = plans_snapshot.inspect_plan(plan_dir)
        self.workspace = _workspace("SVC")

    def _regenerate(
        self,
        gaps: tuple[dict[str, Any], ...],
        *,
        regenerated: Any = None,
        scope_error: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        widened: list[str] = []

        def widen(*, session: Any, plan_dir: Any, workspace: Any, scope: Any) -> Any:
            widened.append(session.id)
            return regenerated

        scope = plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",))
        with (
            mock.patch.object(
                run_prompts,
                "_session_scope",
                side_effect=plans_errors.PlanError("unparseable")
                if scope_error
                else None,
                return_value=scope,
            ),
            mock.patch.object(
                workspace_evidence,
                "_regenerated_repair_child_write_scope",
                side_effect=widen,
            ),
        ):
            records = write_guards._regenerate_stale_repair_child_scopes(
                snapshot=self.snapshot,
                workspace=self.workspace,
                gaps=gaps,
            )
        return records, widened

    def test_every_pending_child_the_projection_named_is_widened(self) -> None:
        records, widened = self._regenerate(
            ({"repair_session_id": "2"}, {"repair_session_id": "3"}),
            regenerated={"widened": True},
        )
        self.assertEqual(["2", "3"], widened)
        self.assertEqual([{"widened": True}, {"widened": True}], records)

    def test_a_child_that_is_not_pending_is_left_alone(self) -> None:
        # Session 1 is complete: rewriting a finished session's persisted scope
        # would rewrite a record the run already acted on.
        _, widened = self._regenerate(({"repair_session_id": "1"},))
        self.assertEqual([], widened)

    def test_a_gap_naming_no_session_this_plan_holds_is_skipped(self) -> None:
        _, widened = self._regenerate(
            ({"repair_session_id": "99"}, {"repair_session_id": None}, {})
        )
        self.assertEqual([], widened)

    def test_a_scope_that_cannot_be_parsed_keeps_the_blocker(self) -> None:
        # Fail open: a child whose scope nobody can read is not rewritten from
        # a scope nobody could derive.
        records, widened = self._regenerate(
            ({"repair_session_id": "2"},), scope_error=True
        )
        self.assertEqual([], widened)
        self.assertEqual([], records)

    def test_a_child_the_widener_declined_contributes_no_record(self) -> None:
        records, widened = self._regenerate(
            ({"repair_session_id": "2"},), regenerated=None
        )
        self.assertEqual(["2"], widened)
        self.assertEqual([], records)


class PendingRepairChildContinuationTests(unittest.TestCase):
    """When the gate clears a stale scope, and what it returns afterwards."""

    def setUp(self) -> None:
        self.workspace = _workspace("SVC")
        self.refreshed = 0

    def _project(
        self,
        *projections: review_continuation.ReviewResume,
        regenerated: list[dict[str, Any]] | None = None,
        legacy: bool = False,
    ) -> tuple[review_continuation.ReviewResume, int, int]:
        widened = 0

        def regenerate(**_: Any) -> list[dict[str, Any]]:
            nonlocal widened
            widened += 1
            return list(regenerated or [])

        def refresh(*_: Any) -> None:
            self.refreshed += 1

        with (
            mock.patch.object(
                review_cycle, "_set_review_resume", side_effect=list(projections)
            ),
            mock.patch.object(
                write_guards,
                "_regenerate_stale_repair_child_scopes",
                side_effect=regenerate,
            ),
            mock.patch.object(
                write_guards.readiness_gate,
                "_refresh_active_readiness_state",
                side_effect=refresh,
            ),
        ):
            resume = write_guards._pending_repair_child_continuation(
                snapshot=cast(Any, SimpleNamespace(plan_dir=Path("/tmp/plan"))),
                event_path=Path("/tmp/events.jsonl"),
                payload={},
                persistence=cast(Any, SimpleNamespace()),
                workspace=None if legacy else self.workspace,
                max_repair_group_attempts=3,
                max_review_fix_cycles=3,
                review_requested=True,
            )
        return resume, widened, self.refreshed

    def test_a_stale_scope_is_cleared_once_and_the_gate_reprojects(self) -> None:
        cleared = _resume(eligible=True, blocker=None)
        resume, widened, refreshed = self._project(
            _resume(), cleared, regenerated=[{"session_id": "2"}]
        )
        self.assertIs(cleared, resume)
        self.assertEqual(1, widened)
        self.assertEqual(1, refreshed)

    def test_the_second_projections_verdict_is_returned_even_when_it_still_blocks(
        self,
    ) -> None:
        # Revalidate rather than assume: the rewrite is widening-only and
        # fail-open, so the reprojection is the only proof the gap closed.
        still_blocked = _resume()
        resume, widened, _ = self._project(
            _resume(), still_blocked, regenerated=[{"session_id": "2"}]
        )
        self.assertIs(still_blocked, resume)
        self.assertEqual(1, widened)

    def test_a_regeneration_that_widened_nothing_does_not_reproject(self) -> None:
        first = _resume()
        resume, widened, refreshed = self._project(first, regenerated=[])
        self.assertIs(first, resume)
        self.assertEqual(1, widened)
        self.assertEqual(0, refreshed)

    def test_a_legacy_run_with_no_workspace_never_rewrites_a_scope(self) -> None:
        first = _resume()
        resume, widened, _ = self._project(first, legacy=True)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_a_continuation_that_is_not_running_repairs_is_left_alone(self) -> None:
        first = _resume(action="review")
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_an_eligible_continuation_is_left_alone(self) -> None:
        first = _resume(eligible=True)
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_another_blocker_is_not_this_gates_business(self) -> None:
        first = _resume(blocker="repair-group-exhausted")
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)

    def test_a_stale_blocker_naming_no_gaps_rewrites_nothing(self) -> None:
        first = _resume(gaps=())
        resume, widened, _ = self._project(first)
        self.assertIs(first, resume)
        self.assertEqual(0, widened)


if __name__ == "__main__":
    unittest.main()
