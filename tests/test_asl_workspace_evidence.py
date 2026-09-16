"""Boundary tests for `agent_session_loop.workspace_evidence`.

The behavioural coverage of the two guard projections lives in
`tests/test_asl_repository_evidence.py`, which drives them through the write
guard and needs real Git checkouts to do it, and the call-ordering assertions
live in `tests/test_asl_run_gates.py` against the loop that makes the calls.
This module owns what neither can: the extraction contract (runner-free, no
alias left behind, exactly one durable write), and the functions whose only
inputs are payloads -- the two bounded pause suffixes, the expansion
fingerprint, the event-log lookup, and the freshness rule. None of them spawns
a process and the resolved workspace they need is built from names rather than
checkouts, which is what keeps this module in `PORTABLE_MODULES`.
"""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    events,
    plans,
    runner,
    workspace_evidence,
    workspaces,
)
from tests.support import assert_no_runner_import

#: The symbols this module took from the runner, plus the ones authored here
#: since. The program's no-alias rule says the runner keeps none of them, so a
#: re-export added later to rescue a stale patch target fails here rather than
#: in review, and a function that grows in this boundary without being declared
#: fails the ownership assertion below.
MOVED_SYMBOLS = (
    "_annotate_history_transitions",
    "_workspace_review_bases",
    "_workspace_snapshot_unavailable",
    "_workspace_unavailable_summary",
    "_workspace_unavailable_message",
    "_workspace_write_guard_evidence",
    "_legacy_implementation_write_guard_evidence",
    "_workspace_guard_violation_summary",
    "_session_expansion_evidence",
    "_latest_workspace_scope_repair_requirement",
    "_workspace_scope_repair_is_fresh",
    "_workspace_scope_repair_still_unauthorized",
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


def _called_names(node: ast.AST) -> set[str]:
    """Every name called under ``node``, whether bare or through an attribute.

    Matched on the parse tree rather than the text, because the substring
    ``replace(`` also occurs inside ``plans.replaced_session_scope_text(`` --
    the kind of false positive that gets a real assertion deleted rather than
    fixed.
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


class WorkspaceEvidenceBoundaryTests(unittest.TestCase):
    """The module is runner-free, aliased nowhere, and writes in one place."""

    def setUp(self) -> None:
        source_path = Path(workspace_evidence.__file__ or "")
        self.tree = ast.parse(source_path.read_text(encoding="utf-8"))

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, workspace_evidence)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(workspace_evidence, name))

    def test_the_module_owns_exactly_the_symbols_the_session_moved(self) -> None:
        defined = {
            node.name
            for node in self.tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.assertEqual(set(MOVED_SYMBOLS), defined)

    def test_each_function_makes_exactly_the_durable_writes_it_declares(self) -> None:
        observed: dict[str, frozenset[str]] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            writes = _called_names(node) & DURABLE_WRITE_NAMES
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
                scope=plans.SessionScope(reads=("SVC",), writes=("SVC",)),
                comparison={},
                exclude=(),
                include_ignored=False,
            )["violations"],
        )


class WorkspaceUnavailableSummaryTests(unittest.TestCase):
    def test_a_repository_is_named_by_its_alias_and_failing_node(self) -> None:
        summary = workspace_evidence._workspace_unavailable_summary(
            {"svc": {"after": {"path": "src/main.py", "reason": "unreadable"}}},
            workspace=_workspace("SVC"),
        )

        self.assertEqual(": SVC:src/main.py (unreadable)", summary)

    def test_the_after_phase_outranks_the_before_phase(self) -> None:
        # Both phases can fail; the after-state is the one the guard is judging,
        # so it is the reason an operator is shown.
        summary = workspace_evidence._workspace_unavailable_summary(
            {
                "svc": {
                    "before": {"reason": "before-reason"},
                    "after": {"reason": "after-reason"},
                }
            },
            workspace=_workspace("SVC"),
        )

        self.assertEqual(": SVC (after-reason)", summary)

    def test_an_unregistered_repository_is_named_by_its_path(self) -> None:
        summary = workspace_evidence._workspace_unavailable_summary(
            {"vendor/other": {"reason": "repository-unavailable"}},
            workspace=_workspace("SVC"),
        )

        self.assertEqual(": vendor/other (repository-unavailable)", summary)

    def test_the_suffix_is_bounded_to_three_subjects_and_a_count(self) -> None:
        summary = workspace_evidence._workspace_unavailable_summary(
            {f"repo{index}": {"reason": "unreadable"} for index in range(5)},
            workspace=_workspace("SVC"),
        )

        self.assertEqual(3, summary.count("(unreadable)"))
        self.assertTrue(summary.endswith("(+2 more)"), summary)

    def test_nothing_unavailable_makes_no_suffix(self) -> None:
        self.assertEqual(
            "",
            workspace_evidence._workspace_unavailable_summary(
                {}, workspace=_workspace("SVC")
            ),
        )


class WorkspaceUnavailableMessageTests(unittest.TestCase):
    def test_external_authority_is_not_a_repository_capture_failure(self) -> None:
        message = workspace_evidence._workspace_unavailable_message(
            {"pi-gradle.py": {"reason": "external-write-unclassified"}},
            workspace=_workspace("SVC"),
        )
        self.assertIn("external paths have unresolved write authority", message)
        self.assertIn("pi-gradle.py (external-write-unclassified)", message)
        self.assertNotIn("repository", message)

    def test_repository_failures_still_name_the_capture_failure(self) -> None:
        message = workspace_evidence._workspace_unavailable_message(
            {"svc": {"after": {"reason": "unreadable"}}},
            workspace=_workspace("SVC"),
        )
        self.assertIn("repository evidence could not be captured safely", message)
        self.assertIn("SVC (unreadable)", message)
        self.assertNotIn("external paths", message)

    def test_mixed_failures_explain_both_surfaces(self) -> None:
        message = workspace_evidence._workspace_unavailable_message(
            {
                "svc": {"before": {"reason": "unreadable"}},
                "external.py": {"reason": "external-write-unclassified"},
            },
            workspace=_workspace("SVC"),
        )
        self.assertIn("external paths have unresolved write authority", message)
        self.assertIn("repository evidence could not be captured safely", message)


class WorkspaceGuardViolationSummaryTests(unittest.TestCase):
    def test_qualified_paths_are_preferred_over_the_repository_name(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {
                        "kind": "read-only-repository",
                        "alias": "OAK",
                        "repository": "oak",
                        "qualified_paths": ["OAK:src/a.py", "OAK:src/b.py"],
                    }
                ]
            }
        )

        self.assertEqual(": OAK:src/a.py, OAK:src/b.py", summary)

    def test_a_violation_with_no_paths_falls_back_to_its_alias(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {
                        "kind": "repository-added",
                        "alias": "OAK",
                        "repository": "oak",
                        "qualified_paths": [],
                    }
                ]
            }
        )

        self.assertEqual(": OAK", summary)

    def test_a_violation_with_neither_paths_nor_alias_names_the_repository(
        self,
    ) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {
                        "kind": "outside-read-scope",
                        "alias": None,
                        "repository": "vendor/other",
                        "qualified_paths": [],
                    }
                ]
            }
        )

        self.assertEqual(": vendor/other", summary)

    def test_the_same_path_named_by_two_violations_is_listed_once(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {
                "violations": [
                    {"qualified_paths": ["OAK:src/a.py"]},
                    {"qualified_paths": ["OAK:src/a.py"]},
                ]
            }
        )

        self.assertEqual(": OAK:src/a.py", summary)

    def test_the_suffix_is_bounded_to_three_paths_and_a_count(self) -> None:
        summary = workspace_evidence._workspace_guard_violation_summary(
            {"violations": [{"qualified_paths": [f"OAK:{i}.py" for i in range(5)]}]}
        )

        self.assertEqual(": OAK:0.py, OAK:1.py, OAK:2.py (+2 more)", summary)

    def test_a_passing_evidence_payload_makes_no_suffix(self) -> None:
        self.assertEqual(
            "",
            workspace_evidence._workspace_guard_violation_summary({"violations": []}),
        )


class SessionExpansionEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name)

    def test_an_absent_expansion_is_absent_rather_than_unavailable(self) -> None:
        record = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertFalse(record["present"])
        self.assertNotIn("unavailable", record)
        self.assertTrue(record["path"].endswith("session-7.expand.md"))

    def test_a_present_expansion_carries_its_mtime_and_content_digest(self) -> None:
        path = self.plan_dir / plans.session_id_to_expansion_filename("7")
        path.write_text("# expansion\n", encoding="utf-8")

        record = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertTrue(record["present"])
        self.assertIsInstance(record["mtime_ns"], int)
        self.assertEqual(64, len(record["sha256"]))

    def test_rewriting_the_same_bytes_leaves_the_digest_alone(self) -> None:
        # The freshness rule below compares mtimes, not digests; the digest is
        # what tells an operator whether the re-expansion changed anything.
        path = self.plan_dir / plans.session_id_to_expansion_filename("7")
        path.write_text("# expansion\n", encoding="utf-8")
        before = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")
        path.write_text("# expansion\n", encoding="utf-8")

        after = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertEqual(before["sha256"], after["sha256"])

    def test_a_directory_in_the_expansion_slot_reports_unavailable(self) -> None:
        (self.plan_dir / plans.session_id_to_expansion_filename("7")).mkdir()

        record = workspace_evidence._session_expansion_evidence(self.plan_dir, "7")

        self.assertTrue(record["present"])
        self.assertTrue(record["unavailable"])
        self.assertNotIn("sha256", record)


class WorkspaceScopeRepairFreshnessTests(unittest.TestCase):
    def test_an_expansion_written_after_the_violation_is_fresh(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 100},
                {"present": True, "mtime_ns": 101},
            )
        )

    def test_an_expansion_that_did_not_move_is_not_fresh(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 100},
                {"present": True, "mtime_ns": 100},
            )
        )

    def test_an_expansion_that_did_not_exist_before_is_fresh(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": False}, {"present": True, "mtime_ns": 1}
            )
        )

    def test_an_expansion_that_vanished_is_not_fresh(self) -> None:
        # Fail closed: the repair is only credited when there is a readable
        # expansion to credit it to.
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 1}, {"present": False}
            )
        )

    def test_an_unreadable_expansion_is_not_fresh(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True, "mtime_ns": 1},
                {"present": True, "unavailable": "Is a directory"},
            )
        )

    def test_a_missing_mtime_on_either_side_is_not_fresh(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_is_fresh(
                {"present": True}, {"present": True}
            )
        )


class WorkspaceScopeRepairReconciliationTests(unittest.TestCase):
    """Whether a retained violation still names something out of scope."""

    def _record(self, *aliases: str | None, **extra: object) -> dict[str, object]:
        return {
            "violations": [
                {
                    "kind": "outside-read-scope",
                    "alias": alias,
                    "repository": alias or "unregistered",
                    "qualified_paths": [],
                }
                for alias in aliases
            ],
            **extra,
        }

    def _scope(self, *aliases: str) -> plans.SessionScope:
        return plans.SessionScope(reads=aliases, writes=aliases)

    def test_a_violation_the_widened_scope_now_writes_is_reconciled(self) -> None:
        self.assertFalse(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"), self._scope("brainnotes", "scripts")
            )
        )

    def test_a_violation_the_scope_still_excludes_is_not_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"), self._scope("brainnotes")
            )
        )

    def test_one_unreconciled_violation_holds_the_whole_record(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts", "vendor"),
                self._scope("brainnotes", "scripts"),
            )
        )

    def test_a_read_only_alias_is_not_reconciled(self) -> None:
        # The violation is about writing, so widening reads alone answers
        # nothing.
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"),
                plans.SessionScope(reads=("brainnotes", "scripts"), writes=()),
            )
        )

    def test_an_unregistered_repository_can_never_be_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record(None), self._scope("brainnotes", "scripts")
            )
        )

    def test_an_incomplete_observation_is_not_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts", unavailable={".": {"reason": "unreadable"}}),
                self._scope("scripts"),
            )
        )

    def test_a_record_with_nothing_to_judge_is_not_reconciled(self) -> None:
        for record in ({}, {"violations": []}, {"violations": "scripts"}):
            with self.subTest(record=record):
                self.assertTrue(
                    workspace_evidence._workspace_scope_repair_still_unauthorized(
                        record, self._scope("scripts")
                    )
                )

    def test_an_unreadable_scope_is_not_reconciled(self) -> None:
        self.assertTrue(
            workspace_evidence._workspace_scope_repair_still_unauthorized(
                self._record("scripts"), None
            )
        )


class LatestWorkspaceScopeRepairRequirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.event_path = Path(self._tmp.name) / "events.jsonl"

    def _write(self, *records: dict[str, object]) -> None:
        self.event_path.write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )

    @staticmethod
    def _violation(session_id: str) -> dict[str, object]:
        return {
            "name": events.RUN_PAUSED_EVENT,
            "payload": {
                "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                "details": {
                    "workspace_write_guard": {
                        "session_id": session_id,
                        "violations": [{"alias": "OAK"}],
                    }
                },
            },
        }

    def test_a_missing_event_log_requires_nothing(self) -> None:
        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "7"
            )
        )

    def test_the_latest_uncleared_violation_is_returned(self) -> None:
        self._write(self._violation("7"))

        record = workspace_evidence._latest_workspace_scope_repair_requirement(
            self.event_path, "7"
        )

        self.assertEqual("7", (record or {})["session_id"])

    def test_a_repair_event_after_the_violation_clears_it(self) -> None:
        self._write(
            self._violation("7"),
            {
                "name": events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                "payload": {"details": {"session_id": "7"}},
            },
        )

        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "7"
            )
        )

    def test_another_sessions_repair_does_not_clear_this_one(self) -> None:
        # The scan walks backwards and stops at the first record about *this*
        # session, so a repair recorded for a sibling must not be read as one.
        self._write(
            self._violation("7"),
            {
                "name": events.WORKSPACE_WRITE_SCOPE_REPAIR_EVENT,
                "payload": {"details": {"session_id": "8"}},
            },
        )

        record = workspace_evidence._latest_workspace_scope_repair_requirement(
            self.event_path, "7"
        )

        self.assertEqual("7", (record or {})["session_id"])

    def test_a_violation_paused_for_another_session_is_ignored(self) -> None:
        self._write(self._violation("8"))

        self.assertIsNone(
            workspace_evidence._latest_workspace_scope_repair_requirement(
                self.event_path, "7"
            )
        )

    def test_a_truncated_line_is_skipped_rather_than_raised(self) -> None:
        self.event_path.write_text(
            f"{json.dumps(self._violation('7'))}\n{{not json\n", encoding="utf-8"
        )

        record = workspace_evidence._latest_workspace_scope_repair_requirement(
            self.event_path, "7"
        )

        self.assertEqual("7", (record or {})["session_id"])


class WorkspaceScopeRepairComparisonTests(unittest.TestCase):
    def test_registering_the_written_repository_is_not_an_addition(self) -> None:
        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            {"added": ["oak"], "removed": []}, frozenset({"oak"})
        )

        self.assertEqual([], repaired["added"])
        self.assertEqual(["oak"], suppressed)

    def test_deregistering_a_repository_is_not_a_removal(self) -> None:
        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            {"added": [], "removed": ["oak"]}, frozenset({"svc"})
        )

        self.assertEqual([], repaired["removed"])
        self.assertEqual(["oak"], suppressed)

    def test_topology_change_to_a_repository_still_registered_survives(self) -> None:
        # The suppression must never hide real topology change to a repository
        # inside the guard's domain.
        repaired, suppressed = workspace_evidence._workspace_scope_repair_comparison(
            {"added": ["vendor/other"], "removed": ["svc"]}, frozenset({"svc"})
        )

        self.assertEqual(["vendor/other"], repaired["added"])
        self.assertEqual(["svc"], repaired["removed"])
        self.assertEqual([], suppressed)

    def test_every_other_key_is_carried_through_unchanged(self) -> None:
        repaired, _ = workspace_evidence._workspace_scope_repair_comparison(
            {"changed": {"svc": {"qualified_paths": ["svc:a.py"]}}}, frozenset()
        )

        self.assertEqual(
            {"svc": {"qualified_paths": ["svc:a.py"]}}, repaired["changed"]
        )


class WorkspaceReviewBaseTests(unittest.TestCase):
    def test_a_registered_repository_with_no_fingerprint_raises(self) -> None:
        # An empty base would silently exempt the repository from the review,
        # so the absence is an error rather than a projection.
        with self.assertRaises(runner.RunError):
            workspace_evidence._workspace_review_bases(_workspace("SVC"), {})

    def test_a_missing_registered_repository_is_reported_unavailable(self) -> None:
        unavailable = workspace_evidence._workspace_snapshot_unavailable(
            {}, required_repository_paths=("svc",)
        )

        self.assertEqual(
            {"svc": {"reason": "registered-repository-missing"}}, unavailable
        )


if __name__ == "__main__":
    unittest.main()
