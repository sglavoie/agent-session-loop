"""Provenance a repair child's commit preflight may sanction dirt against.

The end-to-end transitions (ASI-170, ASI-168) live in
``tests/test_asl_repair_provenance_e2e.py``. What is pinned here is the safety
half those runs cannot show: the sanction is made by content, never by name, so
an edit after the record, a path the record never admitted, a status change and
content that cannot be digested all still refuse.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    repair_provenance,
    review_guard,
    review_guard_provenance,
    verification_models,
)
from tests.support import assert_no_runner_import, init_git_repo


def _unused_workspace_evidence(**_kwargs: Any) -> dict[str, Any]:
    raise AssertionError("the workspace evidence probe must not be called")


def _legacy_guard(*paths: str, tracked: tuple[str, ...] = ()) -> dict[str, Any]:
    """A passing legacy write-guard record naming the paths its turn wrote."""
    return {
        "passed": True,
        "scope_mode": "legacy-single-repository",
        "changed": {
            ".": {
                "tracked": list(tracked),
                "untracked_added": list(paths),
                "untracked_changed": [],
                "untracked_removed": [],
            }
        },
        "violations": [],
        "unavailable": {},
    }


class _RepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name).resolve() / "repo"
        init_git_repo(self.repo)
        self.plan_dir = self.repo / "docs" / "plan"
        self.plan_dir.mkdir(parents=True)
        (self.plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "plan")
        self.context = verification_models.VerificationContext.legacy(self.repo)

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, capture_output=True, check=True)

    def _write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _preflight(self, provenance: dict[str, str] | None) -> dict[str, Any]:
        return review_guard._repair_commit_preflight(
            self.repo,
            verification_context=self.context,
            session_scope=None,
            workspace_evidence=_unused_workspace_evidence,
            provenance=provenance,
        )

    def _refusal(self, provenance: dict[str, str] | None) -> Any:
        with self.assertRaises(review_guard.RepairCommitPreflightError) as caught:
            self._preflight(provenance)
        return caught.exception


class ContentDigestTests(_RepositoryCase):
    """The measurement both sanctions are made with."""

    def test_the_module_stays_inside_the_review_guard_boundary(self) -> None:
        assert_no_runner_import(self, review_guard_provenance)
        source = Path(review_guard_provenance.__file__ or "").read_text(
            encoding="utf-8"
        )
        for writer in ("write_text(", "open(", "mkdir(", "unlink(", "subprocess"):
            self.assertNotIn(writer, source, msg=writer)

    def test_digests_name_status_and_content(self) -> None:
        self._write("new.txt", "one\n")
        self._write("README.md", "changed\n")
        evidence = review_guard_provenance._dirty_path_evidence(self.repo)
        self.assertEqual({"new.txt": "??", "README.md": " M"}, evidence.statuses)
        before = evidence.digest("new.txt")
        assert before is not None
        self.assertTrue(before.startswith("??|file:100644:"))
        self._write("new.txt", "two\n")
        self.assertNotEqual(before, evidence.digest("new.txt"))

    def test_a_deleted_path_is_absent_and_a_directory_is_unprovable(self) -> None:
        (self.repo / "README.md").unlink()
        nested = self.repo / "embedded"
        nested.mkdir()
        evidence = review_guard_provenance.DirtyPathEvidence(
            toplevel=self.repo, statuses={"README.md": " D", "embedded/": "??"}
        )
        self.assertEqual(" D|absent", evidence.digest("README.md"))
        self.assertIsNone(evidence.digest("embedded/"))
        self.assertIsNone(evidence.digest("not-dirty.txt"))

    def test_a_subdirectory_root_digests_against_the_top_level(self) -> None:
        self._write("docs/plan/progress.md", "progress\n")
        evidence = review_guard_provenance._dirty_path_evidence(self.plan_dir)
        self.assertEqual(self.repo, evidence.toplevel.resolve())
        self.assertIsNotNone(evidence.digest("docs/plan/progress.md"))

    def test_a_rename_names_both_sides_with_its_status(self) -> None:
        self.assertEqual(
            {"new.md": "R ", "old.md": "R ", "x": "??"},
            review_guard_provenance._porcelain_status_entries(
                "R  new.md\0old.md\0?? x\0"
            ),
        )


class PreflightProvenanceTests(_RepositoryCase):
    """Dirt is sanctioned only while it still holds the recorded content."""

    def _record_plan_artifacts(self) -> dict[str, str]:
        ledger = repair_provenance.RepairProvenance()
        ledger.record_review_cycle(
            cwd=self.repo, plan_dir=self.plan_dir, verification_context=self.context
        )
        return ledger.baseline_for("2.1")

    def test_recorded_plan_artifacts_are_sanctioned(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        self._write("docs/plan/manifest.md", "# Manifest\n- 2.1\n")
        details = self._preflight(self._record_plan_artifacts())
        self.assertEqual(
            ["docs/plan/manifest.md", "docs/plan/final-review.md"],
            details["provenance"]["sanctioned_dirty_paths"],
        )
        (row,) = details["repositories"]
        # The row still reports the tree as it is: dirty, and sanctioned.
        self.assertFalse(row["clean"])
        self.assertEqual(row["dirty_paths"], row["provenance_sanctioned_paths"])

    def test_an_artifact_edited_after_the_record_refuses(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        provenance = self._record_plan_artifacts()
        self._write("docs/plan/final-review.md", "Status: All clear\n")
        error = self._refusal(provenance)
        self.assertIn("content changed after the run recorded it", str(error))
        self.assertEqual(
            ["docs/plan/final-review.md"],
            error.details["provenance"]["content_drifted_dirty_paths"],
        )
        self.assertEqual(
            ["docs/plan/final-review.md"],
            error.details["dirty_repositories"][0]["dirty_paths"],
        )

    def test_unrelated_dirt_beside_sanctioned_artifacts_still_refuses(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        self._write("src/unrelated.py", "operator work\n")
        # The review-cycle record never admits a path outside the plan
        # directory, even one that was already dirty when it was taken.
        provenance = self._record_plan_artifacts()
        self.assertEqual(["docs/plan/final-review.md"], sorted(provenance))
        error = self._refusal(provenance)
        self.assertEqual(
            ["src/unrelated.py"],
            error.details["dirty_repositories"][0]["dirty_paths"],
        )
        self.assertEqual(
            ["docs/plan/final-review.md"],
            error.details["provenance"]["sanctioned_dirty_paths"],
        )

    def test_a_plan_artifact_that_appeared_after_the_record_refuses(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        provenance = self._record_plan_artifacts()
        self._write("docs/plan/notes.md", "added later\n")
        error = self._refusal(provenance)
        self.assertEqual(
            ["docs/plan/notes.md"],
            error.details["dirty_repositories"][0]["dirty_paths"],
        )

    def test_staging_a_recorded_path_changes_what_a_commit_would_do(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        provenance = self._record_plan_artifacts()
        self._git("add", "docs/plan/final-review.md")
        error = self._refusal(provenance)
        self.assertEqual(
            ["docs/plan/final-review.md"],
            error.details["provenance"]["content_drifted_dirty_paths"],
        )

    def test_without_provenance_the_gate_is_unchanged(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        error = self._refusal(None)
        self.assertNotIn("provenance", error.details)
        self.assertNotIn("content changed", str(error))

    def test_a_plan_outside_the_repository_records_nothing(self) -> None:
        outside = Path(self._tmp.name).resolve() / "plan"
        outside.mkdir()
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        ledger = repair_provenance.RepairProvenance()
        ledger.record_review_cycle(
            cwd=self.repo, plan_dir=outside, verification_context=self.context
        )
        self.assertEqual({}, ledger.baseline_for("2.1"))


class ContinuationProvenanceTests(_RepositoryCase):
    """A child's relaunch carries exactly what its cleared turn left behind."""

    def _record(
        self,
        ledger: repair_provenance.RepairProvenance,
        guard: dict[str, Any] | None,
        session_id: str = "2.1",
    ) -> None:
        ledger.record_continuation(
            session_id=session_id,
            cwd=self.repo,
            plan_dir=self.plan_dir,
            verification_context=self.context,
            session_scope=None,
            guard=guard,
        )

    def test_the_turns_retained_work_is_sanctioned_for_the_same_child(self) -> None:
        self._write("src/fix.py", "fixed\n")
        self._write("README.md", "updated\n")
        ledger = repair_provenance.RepairProvenance()
        self._record(ledger, _legacy_guard("src/fix.py", tracked=("README.md",)))
        details = self._preflight(ledger.baseline_for("2.1"))
        self.assertEqual(
            ["README.md", "src/fix.py"],
            sorted(details["provenance"]["sanctioned_dirty_paths"]),
        )
        # A sibling child never inherits it.
        self.assertEqual({}, ledger.baseline_for("2.2"))
        self._refusal(ledger.baseline_for("2.2"))

    def test_dirt_the_guard_did_not_attribute_to_the_turn_is_never_recorded(
        self,
    ) -> None:
        self._write("src/fix.py", "fixed\n")
        self._write("src/stray.py", "not the turn's\n")
        ledger = repair_provenance.RepairProvenance()
        self._record(ledger, _legacy_guard("src/fix.py"))
        self.assertEqual(["src/fix.py"], sorted(ledger.baseline_for("2.1")))
        error = self._refusal(ledger.baseline_for("2.1"))
        self.assertEqual(
            ["src/stray.py"], error.details["dirty_repositories"][0]["dirty_paths"]
        )

    def test_an_edit_during_the_reply_wait_refuses(self) -> None:
        self._write("src/fix.py", "fixed\n")
        ledger = repair_provenance.RepairProvenance()
        self._record(ledger, _legacy_guard("src/fix.py"))
        self._write("src/fix.py", "operator rewrote it\n")
        error = self._refusal(ledger.baseline_for("2.1"))
        self.assertEqual(
            ["src/fix.py"],
            error.details["provenance"]["content_drifted_dirty_paths"],
        )

    def test_a_guard_that_cannot_vouch_drops_the_record(self) -> None:
        self._write("src/fix.py", "fixed\n")
        ledger = repair_provenance.RepairProvenance()
        self._record(ledger, _legacy_guard("src/fix.py"))
        self.assertTrue(ledger.baseline_for("2.1"))
        for guard in (
            None,
            {**_legacy_guard("src/fix.py"), "passed": False},
            {**_legacy_guard("src/fix.py"), "unavailable": {".": {"x": 1}}},
            {**_legacy_guard("src/fix.py"), "violations": [{"kind": "x"}]},
        ):
            with self.subTest(guard=guard):
                self._record(ledger, _legacy_guard("src/fix.py"))
                self._record(ledger, guard)
                self.assertEqual({}, ledger.baseline_for("2.1"))

    def test_a_later_turn_keeps_paths_an_earlier_turn_left_untouched(self) -> None:
        self._write("src/first.py", "first\n")
        ledger = repair_provenance.RepairProvenance()
        self._record(ledger, _legacy_guard("src/first.py"))
        # The second turn's own guard names only what it changed.
        self._write("src/second.py", "second\n")
        self._record(ledger, _legacy_guard("src/second.py"))
        details = self._preflight(ledger.baseline_for("2.1"))
        self.assertEqual(
            ["src/first.py", "src/second.py"],
            sorted(details["provenance"]["sanctioned_dirty_paths"]),
        )

    def test_plan_artifacts_the_turn_rewrote_are_carried_with_it(self) -> None:
        self._write("docs/plan/final-review.md", "Status: 1 Findings\n")
        ledger = repair_provenance.RepairProvenance()
        ledger.record_review_cycle(
            cwd=self.repo, plan_dir=self.plan_dir, verification_context=self.context
        )
        # The child's turn updates its own plan files, which no write guard
        # observes, and leaves one source change the guard did observe.
        self._write("docs/plan/manifest.md", "# Manifest\n- handoff\n")
        self._write("src/fix.py", "fixed\n")
        self._record(ledger, _legacy_guard("src/fix.py"))
        details = self._preflight(ledger.baseline_for("2.1"))
        self.assertEqual(
            [
                "docs/plan/final-review.md",
                "docs/plan/manifest.md",
                "src/fix.py",
            ],
            sorted(details["provenance"]["sanctioned_dirty_paths"]),
        )

    def test_retirement_and_the_payload_mirror(self) -> None:
        self._write("src/fix.py", "fixed\n")
        ledger = repair_provenance.RepairProvenance()
        self._record(ledger, _legacy_guard("src/fix.py"))
        payload = ledger.to_payload()
        self.assertEqual(
            ["src/fix.py"],
            sorted(payload[repair_provenance.SOURCE_REPAIR_CONTINUATION]["2.1"]),
        )
        ledger.retire("2.1")
        self.assertEqual({}, ledger.baseline_for("2.1"))
        self.assertEqual({}, ledger.to_payload()["repair-continuation"])


class GuardWrittenPathTests(unittest.TestCase):
    """Only what a passing guard observed its turn writing is vouched for."""

    def test_registered_paths_are_qualified_by_alias(self) -> None:
        guard = {
            "passed": True,
            "changed": {
                "repos/a": {"alias": "A", "tracked": ["x.py"]},
                "repos/b": {
                    "alias": "B",
                    "worktree_of": "repos/a",
                    "tracked": ["y.py"],
                },
            },
        }
        self.assertEqual(
            frozenset({"A:x.py"}),
            repair_provenance.guard_written_paths(guard, registered=True),
        )

    def test_ignore_sources_are_not_content(self) -> None:
        guard = {
            "passed": True,
            "changed": {".": {"worktree_ignore": [".gitignore"], "tracked": []}},
        }
        self.assertEqual(
            frozenset(), repair_provenance.guard_written_paths(guard, registered=False)
        )


if __name__ == "__main__":
    unittest.main()
