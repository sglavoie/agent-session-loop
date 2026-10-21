"""Handoff-correction tests: boundary."""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from agent_session_loop import (
    handoff_correction,
    handoff_guard_artifacts,
    handoff_guard_mailbox,
    handoff_guard_review,
    handoff_guard_sessions,
    handoff_guard_snapshots,
    handoff_guards,
    runner,
    session_verification,
)
from tests.support import (
    assert_no_runner_import,
    called_names,
)

from .asl_handoff_correction_support import (
    DURABLE_WRITE_NAMES,
    DURABLE_WRITES,
    MOVED_CLASSES,
    MOVED_CONSTANTS,
    MOVED_FUNCTIONS,
)


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
            writes = called_names(node) & DURABLE_WRITE_NAMES
            if writes:
                observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)

    def test_no_function_here_writes_a_session_file(self) -> None:
        # `handoff_guards._revert_session_status_to_pending` is the single
        # writer of a `session-<id>.md`. All three repairs rewrite a status
        # line, and every one of them delegates that write rather than
        # performing it, so a second writer cannot open a window where the
        # status and the note disagree.
        self.assertNotIn("write_text", called_names(self.tree))
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
        for module in (
            handoff_guards,
            handoff_guard_artifacts,
            handoff_guard_mailbox,
            handoff_guard_review,
            handoff_guard_sessions,
            handoff_guard_snapshots,
            session_verification,
        ):
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
