"""Boundary tests for `agent_session_loop.handoff_correction`.

The per-function behavioural coverage of this boundary already exists and stays
where it is: `tests/test_asl_turn_finalization.py` owns the defect
classification table, the preserved-artifact digests and the attempt counter,
and `tests/test_asl_run_gates.py` drives a real malformed handoff through a real
run and asserts what an operator sees afterwards. Neither of them can cheaply
state the things this module owns, so they are stated here: the extraction
contract (runner-free, no alias left behind, exactly the symbols the session
moved), the per-function durable-write list, the import direction that keeps
`handoff_guards.HandoffNoteAppender` a Protocol rather than an import, and the
one property the session file names outright -- that a correction turn is armed
*without* disturbing the review artifacts the malformed handoff says nothing
about.

The correction turn is driven for real against a plan on disk, with the
persistence handle's state writes switched off and the events appended to a
temporary path. Nothing here reaches Git or `verification.run_verification`, so
nothing spawns a process, which is what keeps this module in
`PORTABLE_MODULES`; `test_the_correction_turn_spawns_no_process` asserts that
rather than assuming it.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    handoff_correction,
    handoff_guards,
    plans,
    review,
    run_persistence,
    runner,
    session_verification,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    assert_no_runner_import,
    read_jsonl,
    write_multi_repo_plan,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_FUNCTIONS = (
    "_classify_mechanical_handoff_defect",
    "_preserved_review_artifact_evidence",
    "_handoff_correction_attempt",
    "_handoff_correction_note",
    "_correct_malformed_handoff",
    "_uninspectable_handoff_note",
    "_reopen_uninspectable_handoff",
    "_paused_handoff_status_note",
    "_repair_paused_handoff_status",
    "_append_handoff_note",
)

#: The class and the four constants that moved with them.
MOVED_CLASSES = ("_HandoffDefect",)
MOVED_CONSTANTS = (
    "MAX_HANDOFF_CORRECTION_ATTEMPTS",
    "HANDOFF_DEFECT_MISSING_FILES",
    "HANDOFF_DEFECT_PENDING_PLACEHOLDER",
    "HANDOFF_DEFECT_UNQUALIFIED_ENTRY",
)

#: Every call this boundary could make that outlives the process. Anything a
#: function here learned to call that is not on this list is invisible to
#: :data:`DURABLE_WRITES` below, so the list is the first thing to extend when a
#: new record joins the boundary.
DURABLE_WRITE_NAMES = frozenset(
    {
        "append_event",
        "append_progress_entry",
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

#: Which module-level function writes directly, and what it writes. The
#: session-file rewrite belongs to `handoff_guards`, which owns that write at
#: its own boundary and is the single writer of a `session-<id>.md`; what is
#: left here is the correction turn's own bookkeeping and the reopen event the
#: paused-status repair records on its own behalf. Stating it per function
#: rather than as a flat prohibition is what makes a *third* writer appearing
#: here a test failure instead of a review catch.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_correct_malformed_handoff": frozenset(
        {"write_json_file", "append_event", "write_state"}
    ),
    "_repair_paused_handoff_status": frozenset({"append_event"}),
}


def _called_names(node: ast.AST) -> set[str]:
    """Every name called under ``node``, whether bare or through an attribute.

    Matched on the parse tree rather than the text, so a name that also occurs
    as a substring of a longer call does not read as a match.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


def _resolved_workspace(root: Path) -> workspaces.ResolvedWorkspace:
    """A registered workspace built from names, with no checkout behind it.

    Only ``registry.aliases`` is read by this boundary, so the repositories can
    be declared rather than cloned -- which is what keeps this module portable.
    """
    repositories = tuple(
        workspaces.ResolvedRepository(
            repository=workspaces.Repository(alias, path, origin),
            root=root / path,
            origin=origin,
        )
        for alias, path, origin in DEFAULT_MULTI_REPO_SPECS
    )
    return workspaces.ResolvedWorkspace(root=root, repositories=repositories)


def _set_handoff(session_path: Path, *, status: str, files_modified: str) -> None:
    """Rewrite one session's handoff status and ``Files modified`` line."""
    lines = session_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        if line.startswith("- **Status**:"):
            rewritten.append(f"- **Status**: {status}")
        elif line.startswith("- **Files modified**:"):
            rewritten.append(f"- **Files modified**: {files_modified}")
        else:
            rewritten.append(line)
    session_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


class HandoffCorrectionBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes in two places."""

    def setUp(self) -> None:
        source_path = Path(handoff_correction.__file__ or "")
        self.source = source_path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, handoff_correction)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_FUNCTIONS + MOVED_CLASSES + MOVED_CONSTANTS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(handoff_correction, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        functions = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        classes = {
            node.name for node in self.tree.body if isinstance(node, ast.ClassDef)
        }
        constants = {
            target.id
            for node in self.tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(set(MOVED_FUNCTIONS), functions)
        self.assertEqual(set(MOVED_CLASSES), classes)
        self.assertEqual(set(MOVED_CONSTANTS), constants)

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = _called_names(node) & DURABLE_WRITE_NAMES
            if writes:
                observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)

    def test_no_function_here_writes_a_session_file(self) -> None:
        # `handoff_guards._revert_session_status_to_pending` is the single
        # writer of a `session-<id>.md`. All three repairs rewrite a status
        # line, and every one of them delegates that write rather than
        # performing it, so a second writer cannot open a window where the
        # status and the note disagree.
        self.assertNotIn("write_text", _called_names(self.tree))
        reverts = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_revert_session_status_to_pending"
        ]
        self.assertEqual(3, len(reverts))

    def test_every_result_file_write_is_taken_under_the_run_mutex(self) -> None:
        # The turn result is the record a resumed run reads back to find out
        # whether a correction turn was armed. A write outside
        # `persistence.lock` races the heartbeat threads writing the same run's
        # state.
        guarded: set[int] = set()
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.With):
                continue
            if not any(
                ast.unparse(item.context_expr) == "persistence.lock"
                for item in node.items
            ):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "write_json_file"
                ):
                    guarded.add(id(child))
        every = [
            child
            for child in ast.walk(self.tree)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "write_json_file"
        ]
        self.assertTrue(every)
        self.assertEqual(
            [],
            [ast.unparse(call) for call in every if id(call) not in guarded],
        )


class ImportDirectionTests(unittest.TestCase):
    """Why the note appender stays a Protocol seam after it has a home.

    Session 19 declared `handoff_guards.HandoffNoteAppender` because the
    implementation was still a runner symbol, and asked whether the Protocol
    should survive this session. It has to: this module imports `handoff_guards`
    for the revert and `session_verification` for the post-revert re-read, so
    either of them importing `_append_handoff_note` back would close a cycle.
    The seam is now a structural fact rather than a placeholder, which is worth
    asserting because the collapse looks safe from either module's side alone.
    """

    def test_this_module_imports_both_seam_holders(self) -> None:
        source = Path(handoff_correction.__file__ or "").read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                imported.update(alias.name for alias in node.names)
        self.assertIn("handoff_guards", imported)
        self.assertIn("session_verification", imported)

    def test_neither_seam_holder_imports_this_module_back(self) -> None:
        # Both of them name this module in prose, to say why the seam exists,
        # so the check is on the import graph rather than on the text.
        for module in (handoff_guards, session_verification):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__ or "").read_text(encoding="utf-8")
                imported: set[str] = set()
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.ImportFrom):
                        imported.update(alias.name for alias in node.names)
                        if node.module:
                            imported.add(node.module)
                    elif isinstance(node, ast.Import):
                        imported.update(alias.name for alias in node.names)
                self.assertNotIn("handoff_correction", imported)
                self.assertNotIn("agent_session_loop.handoff_correction", imported)

    def test_the_appender_satisfies_the_protocol_it_is_passed_as(self) -> None:
        declared = inspect.signature(handoff_guards.HandoffNoteAppender.__call__)
        actual = inspect.signature(handoff_correction._append_handoff_note)
        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )


class CorrectionTurnTests(unittest.TestCase):
    """One malformed handoff, driven through a real bounded correction turn."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending", "pending"))
        # Review artifacts with their own validator, written beside the plan.
        # They are what the correction turn must leave alone.
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Final review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )
        self.session_path = self.plan_dir / "session-1.md"
        self.before = plans.inspect_plan(self.plan_dir)
        self.event_path = root / "events.jsonl"
        self.result_path = root / "result.json"
        self.persistence = run_persistence._RunPersistence(write_state=False)
        self.plan_cache = plans.PlanSnapshotCache()
        self.workspace = _resolved_workspace(root)
        self.payload: dict[str, Any] = {"run_id": "run-1"}
        self.turn_payload: dict[str, Any] = {}

    def correct(self) -> tuple[plans.PlanSnapshot | None, dict[str, Any] | None]:
        return handoff_correction._correct_malformed_handoff(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
            plan_cache=self.plan_cache,
            before=self.before,
            workspace=self.workspace,
            run_id="run-1",
            turn_index=3,
            session_id="1",
            plan_error=f"{self.session_path.name}: unqualified path 'README.md'",
            turn_payload=self.turn_payload,
            paths={"result": self.result_path},
        )

    def malform(self) -> None:
        _set_handoff(self.session_path, status="complete", files_modified="README.md")

    def artifact_digests(self) -> tuple[str, str]:
        return (
            review.final_review_output_sha256(self.plan_dir),
            review.final_review_artifact_sha256(self.plan_dir),
        )

    def test_the_correction_turn_preserves_the_review_artifacts(self) -> None:
        self.malform()
        before_digests = self.artifact_digests()

        corrected, evidence = self.correct()

        assert corrected is not None
        assert evidence is not None
        self.assertFalse(evidence["exhausted"])
        preserved = evidence["preserved_review_artifacts"]
        # The digests were taken before the session was reverted, and both
        # artifacts are byte-for-byte what they were: the handoff is what
        # failed to parse, and the artifacts have their own validator.
        self.assertEqual(before_digests[0], preserved["review_output_sha256"])
        self.assertEqual(before_digests[1], preserved["review_artifact_sha256"])
        self.assertEqual(before_digests, self.artifact_digests())
        self.assertTrue(preserved["review_verdict_ok"])
        self.assertEqual("all-clear", preserved["review_outcome"])

    def test_the_correction_note_tells_the_next_turn_to_leave_them_alone(
        self,
    ) -> None:
        self.malform()

        self.correct()

        note = self.session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", note)
        self.assertIn("handoff correction required", note)
        self.assertIn("leave them exactly as they are", note)
        # The claim is withdrawn; the work is not touched.
        self.assertIn("the session's work is already done", note)

    def test_the_correction_records_its_evidence_and_arms_one_turn(self) -> None:
        self.malform()

        corrected, evidence = self.correct()

        assert corrected is not None
        assert evidence is not None
        self.assertEqual(1, evidence["attempt"])
        self.assertEqual(
            handoff_correction.MAX_HANDOFF_CORRECTION_ATTEMPTS,
            evidence["max_attempts"],
        )
        self.assertEqual(
            handoff_correction.HANDOFF_DEFECT_UNQUALIFIED_ENTRY, evidence["defect"]
        )
        self.assertEqual(str(self.session_path), evidence["session_path"])
        self.assertEqual(
            evidence,
            json.loads(self.result_path.read_text(encoding="utf-8"))[
                "handoff_correction"
            ],
        )
        recorded = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.TURN_HANDOFF_CORRECTION_EVENT
        ]
        self.assertEqual(1, len(recorded))
        self.assertEqual(evidence, recorded[0]["payload"])
        # The re-read plan is what the loop publishes, and it parses again.
        self.assertIn("final_snapshot", self.payload)
        self.assertEqual(
            "pending",
            next(item.status for item in corrected.sessions if item.id == "1"),
        )

    def test_plan_artifact_correction_preserves_repository_validation(self) -> None:
        session_text = self.session_path.read_text(encoding="utf-8")
        aliases = ", ".join(alias for alias, _path, _origin in DEFAULT_MULTI_REPO_SPECS)
        for repository_entry in (None, "SVC:README.md", "OAK:README.md"):
            with self.subTest(repository_entry=repository_entry):
                writes = "SVC" if repository_entry else "(none)"
                self.session_path.write_text(
                    session_text.replace(
                        f"**Writes repositories**: {aliases}",
                        f"**Writes repositories**: {writes}",
                    ),
                    encoding="utf-8",
                )
                self.before = plans.inspect_plan(self.plan_dir)
                # Authored review sessions do not have the generated-review
                # marker that projects these artifact names away on read.
                inline = "final-review.md, final-review.json"
                if repository_entry:
                    inline += f", {repository_entry}"
                _set_handoff(
                    self.session_path, status="complete", files_modified=inline
                )
                if self.event_path.exists():
                    self.event_path.unlink()
                before_digests = self.artifact_digests()
                with self.assertRaises(plans.PlanError):
                    plans.inspect_plan(self.plan_dir)

                corrected, evidence = self.correct()

                self.assertIsNotNone(corrected)
                assert evidence is not None
                self.assertFalse(evidence["exhausted"])
                note = self.session_path.read_text(encoding="utf-8")
                self.assertIn("move its provenance to Notes", note)
                self.assertIn("Files modified: (none)", note)
                self.assertIn("retain every such file as an Alias:path entry", note)

                # Apply the documented representation: only the repository
                # entry remains, with artifact provenance in the handoff notes.
                _set_handoff(
                    self.session_path,
                    status="complete",
                    files_modified=repository_entry or "(none)",
                )
                self.session_path.write_text(
                    self.session_path.read_text(encoding="utf-8").replace(
                        "- **Notes**:",
                        "- **Notes**: Plan-local artifacts: "
                        "final-review.md, final-review.json.",
                        1,
                    ),
                    encoding="utf-8",
                )
                if repository_entry == "OAK:README.md":
                    with self.assertRaisesRegex(
                        plans.PlanError, "outside Writes repositories"
                    ):
                        plans.inspect_plan(self.plan_dir)
                else:
                    snapshot = plans.inspect_plan(self.plan_dir)
                    handoff = plans.read_session_handoff(snapshot.sessions[0].session)
                    self.assertEqual("complete", handoff.status)
                    self.assertEqual(
                        (repository_entry or "(none)",), handoff.files_modified
                    )
                    self.assertIn("Plan-local artifacts:", handoff.notes or "")
                self.assertEqual(before_digests, self.artifact_digests())

    def test_a_second_correction_is_refused_but_still_reports(self) -> None:
        self.malform()
        self.persistence.append_event(
            self.event_path,
            events.TURN_HANDOFF_CORRECTION_EVENT,
            {"session_id": "1"},
        )
        before_digests = self.artifact_digests()

        corrected, evidence = self.correct()

        self.assertIsNone(corrected)
        assert evidence is not None
        self.assertTrue(evidence["exhausted"])
        self.assertEqual(2, evidence["attempt"])
        # The exhausted case still carries the preserved-artifact evidence, so
        # the pause that follows says what was tried and what was left intact.
        self.assertIn("preserved_review_artifacts", evidence)
        self.assertEqual(before_digests, self.artifact_digests())
        self.assertIn("- **Status**: complete", self.session_path.read_text("utf-8"))

    def test_a_plan_error_naming_another_file_corrects_nothing(self) -> None:
        self.malform()

        corrected, evidence = handoff_correction._correct_malformed_handoff(
            event_path=self.event_path,
            payload=self.payload,
            persistence=self.persistence,
            plan_cache=self.plan_cache,
            before=self.before,
            workspace=self.workspace,
            run_id="run-1",
            turn_index=3,
            session_id="1",
            plan_error="manifest.md: registry row is malformed",
            turn_payload=self.turn_payload,
            paths={"result": self.result_path},
        )

        self.assertIsNone(corrected)
        self.assertIsNone(evidence)
        self.assertIn("- **Status**: complete", self.session_path.read_text("utf-8"))

    def test_a_legacy_run_without_a_workspace_corrects_nothing(self) -> None:
        self.malform()
        self.workspace = None  # type: ignore[assignment]

        corrected, evidence = self.correct()

        self.assertIsNone(corrected)
        self.assertIsNone(evidence)

    def test_the_correction_turn_spawns_no_process(self) -> None:
        # `PORTABLE_MODULES` membership is a claim about this boundary, made
        # here on evidence rather than by reading the imports.
        self.malform()
        boom = mock.Mock(side_effect=AssertionError("spawned a process"))
        with (
            mock.patch.object(subprocess, "Popen", boom),
            mock.patch.object(subprocess, "run", boom),
            mock.patch.object(subprocess, "check_output", boom),
            mock.patch.object(subprocess, "check_call", boom),
        ):
            corrected, evidence = self.correct()

        self.assertIsNotNone(corrected)
        self.assertIsNotNone(evidence)


class UninspectableReopenTests(unittest.TestCase):
    """The lane that covers everything the bounded correction does not."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("pending", "pending"))
        self.session_path = self.plan_dir / "session-1.md"
        self.before = plans.inspect_plan(self.plan_dir)
        self.plan_cache = plans.PlanSnapshotCache()

    def reopen(self, *, exhausted: bool) -> tuple[Any, dict[str, Any] | None]:
        return handoff_correction._reopen_uninspectable_handoff(
            plan_cache=self.plan_cache,
            before=self.before,
            session_id="1",
            plan_error=f"{self.session_path.name}: handoff is unreadable",
            correction_exhausted=exhausted,
        )

    def test_a_completed_handoff_is_returned_to_pending_with_the_reason(self) -> None:
        _set_handoff(self.session_path, status="complete", files_modified="README.md")

        reinspected, evidence = self.reopen(exhausted=False)

        assert reinspected is not None
        assert evidence is not None
        self.assertTrue(evidence["reopened"])
        self.assertTrue(evidence["repairable"])
        text = self.session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertIn("the plan could not be inspected", text)

    def test_an_exhausted_correction_reopens_but_promises_nothing(self) -> None:
        _set_handoff(self.session_path, status="complete", files_modified="README.md")

        _reinspected, evidence = self.reopen(exhausted=True)

        assert evidence is not None
        self.assertTrue(evidence["reopened"])
        self.assertFalse(evidence["repairable"])

    def test_a_session_that_never_claimed_complete_is_left_alone(self) -> None:
        original = self.session_path.read_text(encoding="utf-8")

        reinspected, evidence = self.reopen(exhausted=False)

        self.assertIsNone(reinspected)
        self.assertIsNone(evidence)
        self.assertEqual(original, self.session_path.read_text(encoding="utf-8"))


class AppendHandoffNoteTests(unittest.TestCase):
    """The pure line-list transform all three repairs render their note through."""

    def test_an_existing_notes_bullet_gains_the_note_at_its_end(self) -> None:
        lines = ["## Handoff", "", "- **Notes**:", "  - first", "", "## Next"]

        rewritten = handoff_correction._append_handoff_note(
            lines, handoff_index=0, section_end=5, note="second"
        )

        self.assertEqual(
            ["## Handoff", "", "- **Notes**:", "  - first", "  - second", ""],
            rewritten[:6],
        )

    def test_a_missing_notes_bullet_is_created_inside_the_section(self) -> None:
        lines = ["## Handoff", "", "- **Status**: pending", "", "## Next"]

        rewritten = handoff_correction._append_handoff_note(
            lines, handoff_index=0, section_end=4, note="reopened"
        )

        self.assertEqual(
            [
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Notes**:",
                "  - reopened",
                "",
                "## Next",
            ],
            rewritten,
        )

    def test_the_note_never_lands_past_the_section_end(self) -> None:
        # The bound is the whole point: a bullet list that continues past the
        # handoff heading belongs to the next section, and a note appended to
        # its end would land in the next session's instructions. `section_end`
        # stops the scan at the heading, so the note goes in above it.
        lines = ["## Handoff", "- **Notes**:", "  - first", "## Next", "  - decoy"]

        rewritten = handoff_correction._append_handoff_note(
            lines, handoff_index=0, section_end=3, note="second"
        )

        self.assertEqual(
            [
                "## Handoff",
                "- **Notes**:",
                "  - first",
                "  - second",
                "## Next",
                "  - decoy",
            ],
            rewritten,
        )

    def test_the_transform_touches_no_file(self) -> None:
        source = inspect.getsource(handoff_correction._append_handoff_note)
        for call in ("open(", "write_text(", "Path("):
            with self.subTest(call=call):
                self.assertNotIn(call, source)


if __name__ == "__main__":
    unittest.main()
