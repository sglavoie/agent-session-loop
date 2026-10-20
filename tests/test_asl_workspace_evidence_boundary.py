"""Extraction-contract tests for `agent_session_loop.workspace_evidence`.

See `tests/test_asl_workspace_evidence_messages.py` and
`tests/test_asl_workspace_evidence_scope_repair.py` for the payload-only
functions; this module owns the runner-free, no-alias, one-durable-write
contract.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_session_scope,
    runner,
    workspace_evidence,
    workspace_evidence_bases,
    workspace_evidence_guard,
    workspace_evidence_scope_repair,
)
from tests.asl_workspace_evidence_support import _workspace
from tests.support import assert_no_runner_import, called_names

#: The symbols this module took from the runner, plus the ones authored here
#: since. The program's no-alias rule says the runner keeps none of them, so a
#: re-export added later to rescue a stale patch target fails here rather than
#: in review, and a function that grows in this boundary without being declared
#: fails the ownership assertion below.
MOVED_SYMBOLS = (
    "_annotate_history_transitions",
    "_workspace_review_bases",
    "_workspace_snapshot_unavailable",
    "_unavailable_path_and_reason",
    "_workspace_unavailable_summary",
    "_workspace_unavailable_message",
    "_legacy_write_guard_failure_message",
    "_workspace_write_guard_evidence",
    "_legacy_implementation_write_guard_evidence",
    "_workspace_guard_violation_summary",
    "_workspace_repair_required",
    "_session_expansion_evidence",
    "_latest_workspace_scope_repair_requirement",
    "_workspace_scope_repair_is_fresh",
    "_workspace_scope_repair_still_unauthorized",
    "_workspace_scope_repair_restored",
    "_workspace_scope_repair_comparison",
    "_regenerated_repair_child_write_scope",
)

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

#: Which module-level function makes which durable write. Exactly one of the
#: twelve writes anything, and stating that per function rather than as a flat
#: prohibition is what makes a *second* writer appearing here a test failure
#: instead of a review catch: everything else in this boundary reads a payload
#: and returns a verdict for its caller to persist.
DURABLE_WRITES: dict[str, frozenset[str]] = {
    "_regenerated_repair_child_write_scope": frozenset({"write_text"}),
}


class WorkspaceEvidenceBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes in one place."""

    def setUp(self) -> None:
        self.modules = (
            workspace_evidence_bases,
            workspace_evidence_guard,
            workspace_evidence_scope_repair,
        )
        self.trees = tuple(
            ast.parse(Path(module.__file__ or "").read_text(encoding="utf-8"))
            for module in self.modules
        )

    def test_the_module_does_not_import_the_runner(self) -> None:
        for module in self.modules:
            with self.subTest(module=module.__name__):
                assert_no_runner_import(self, module)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(workspace_evidence, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        defined = {
            node.name
            for tree in self.trees
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(set(MOVED_SYMBOLS), defined)

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for tree in self.trees:
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef):
                    continue
                writes = called_names(node) & DURABLE_WRITE_NAMES
                if writes:
                    observed[node.name] = frozenset(writes)
        self.assertEqual(DURABLE_WRITES, observed)

    def test_the_probe_seam_review_guard_declared_is_satisfied_here(self) -> None:
        # Session 9 typed `_repair_commit_preflight` against a structural
        # `WorkspaceEvidenceProbe` rather than importing the projection it may
        # not depend on. This is the module that owns and supplies it, so
        # a signature drift on either side is caught here rather than at the
        # one runtime call site.
        from agent_session_loop import review_guard

        probe: review_guard.WorkspaceEvidenceProbe = (
            workspace_evidence._workspace_write_guard_evidence
        )

        self.assertEqual(
            [],
            probe(
                workspace=_workspace("SVC"),
                scope=plans_session_scope.SessionScope(reads=("SVC",), writes=("SVC",)),
                comparison={},
                exclude=(),
                include_ignored=False,
            )["violations"],
        )


if __name__ == "__main__":
    unittest.main()
