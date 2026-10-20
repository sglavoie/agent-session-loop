"""How much a re-review must read, asserted at the seam rather than through a run.

The behavioural coverage of delta scoping already lives where it always has —
``tests/test_asl_review_lifecycle_*.py`` drives a real run through every
degradation reason a single repository can produce, ``tests/test_asl_cli.py``
owns the registered combined delta, and ``tests/test_asl_review_*.py`` owns the
cycle accounting these readers sit on top of. What is asserted here is the
contract those modules cannot state, because they exercise the run rather than
the boundary:

* the module does not import the runner, and needs nothing from it;
* the only file it writes is the pending review session body, and the one event
  it appends goes through the ``_RunPersistence`` handle the caller supplied,
  under that handle's lock;
* ``handoff_guards._read_review_verdict`` still satisfies ``ReviewVerdictReader``,
  because that Protocol is structural and the reader does not move into a module
  of its own until Session 19; and
* the fail-closed rule is a rule, not a habit — every probe that can fail names
  its own reason, and a combined delta degrades the *whole* workspace rather
  than narrowing the repositories that happened to succeed.

The last two are why this file exists. Both failure modes are silent: a verdict
reader whose signature drifted still type-checks at the one call site that
passes it by keyword, and a combined delta that narrowed per repository would
produce a review that reads a real file list and never mentions the repository
it could not prove anything about.
"""

from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

from agent_session_loop import (
    handoff_guards,
    review_delta,
    runner,
)
from tests.support import (
    assert_no_runner_import,
)

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_review_broad_failure_acceptance",
    "_verification_repair_state",
    "_read_prior_review_cycles",
    "_review_cycle_state",
    "_review_artifact_digests",
    "_last_repairs_materialized_cycle",
    "_prior_review_findings",
    "_reviewed_baseline_dirty_paths",
    "_compute_review_delta_context",
    "_compute_registered_review_delta_context",
    "_refresh_pending_review_scope",
)


def _call_receivers(source: str, *attributes: str) -> set[str]:
    """Every receiver of a call to one of ``attributes``, as source text."""
    receivers: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in attributes:
            continue
        receivers.add(ast.unparse(node.func.value))
    return receivers


class ReviewDeltaBoundaryTests(unittest.TestCase):
    """The structural contract of the boundary itself."""

    @property
    def source(self) -> str:
        return Path(review_delta.__file__ or "").read_text(encoding="utf-8")

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, review_delta)

    def test_the_only_file_it_writes_is_the_pending_review_session(self) -> None:
        # Scoping is a read-heavy boundary with exactly one durable rewrite:
        # the `## Delta Review Scope` section of a pending review session. Any
        # other file this module learned to open would be a record nobody else
        # in the run can see.
        self.assertEqual({"session.path"}, _call_receivers(self.source, "write_text"))
        for reach in ("open(", "mkdir(", "unlink(", "durability."):
            self.assertNotIn(reach, self.source, msg=reach)

    def test_every_event_write_goes_through_a_persistence_handle(self) -> None:
        self.assertEqual(
            {"persistence"},
            _call_receivers(self.source, "append_event", "write_state"),
        )

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(review_delta, name))

    def test_the_runner_verdict_reader_still_satisfies_the_seam(self) -> None:
        """``ReviewVerdictReader`` is structural, so drift here is silent.

        Nothing raises when a Protocol stops being satisfied by a function
        passed as a plain callable. The deferral is the one place a *failed*
        verification can still be accepted, so a reader that quietly stopped
        honouring ``require_artifact`` would accept a deferral on evidence the
        caller believed it had demanded.
        """
        declared = inspect.signature(review_delta.ReviewVerdictReader.__call__)
        actual = inspect.signature(handoff_guards._read_review_verdict)

        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )


class ReviewedBaselineDirtyPathTests(unittest.TestCase):
    """Three answers, not two: proved-clean, proved-dirty, and cannot-prove."""

    def test_a_clean_reviewed_tree_is_the_recorded_commit(self) -> None:
        self.assertEqual(
            [], review_delta._reviewed_baseline_dirty_paths({"worktree_dirty": False})
        )

    def test_a_dirty_tree_with_path_evidence_reports_those_paths(self) -> None:
        self.assertEqual(
            ["a.py", "b.py"],
            review_delta._reviewed_baseline_dirty_paths(
                {"worktree_dirty": True, "dirty_paths": ["a.py", "", "b.py"]}
            ),
        )

    def test_a_dirty_tree_without_path_evidence_cannot_be_proved(self) -> None:
        # `None` is not "nothing was dirty" — it is "this record cannot say
        # what the reviewer read", which is what forces full scope.
        for cycle in (
            {"worktree_dirty": True},
            {"worktree_dirty": True, "dirty_paths": "a.py"},
            {},
            {"worktree_dirty": "yes"},
        ):
            with self.subTest(cycle=cycle):
                self.assertIsNone(review_delta._reviewed_baseline_dirty_paths(cycle))


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
