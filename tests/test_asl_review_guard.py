"""The review-guard boundary: every git the loop shells out to, and its refusals.

The behavioural coverage of the review mutation guard lives where it always has
— ``tests/test_asl_review_lifecycle.py`` captures and compares real repository
fingerprints, ``tests/test_asl_turn_commit.py`` drives auto-commit through real
checkouts, and ``tests/test_asl_run_gates.py`` runs the repair-child preflight
inside a whole run. What is asserted here is the boundary those modules cannot
state, because they exercise the loop rather than the seam:

* the module does not import the runner, and needs nothing from it;
* it writes no durable record at all, and spawns ``git`` from exactly two
  places, so a caller can never meet a raw ``OSError``; and
* the refusals themselves — the allowlist, the clean-worktree gates, and the
  proof a retained-scope sanction requires.

That last group is the reason this file exists. A review-guard bug does not
fail a gate: it reports a repository as unchanged when it was rewritten, or it
lets a repair child commit work it never wrote, and both look exactly like a
clean run from the outside.
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import plans, review_guard, verification
from agent_session_loop.run_errors import RunError
from tests.support import assert_no_runner_import, init_git_repo, write_plan


def _args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {}
    values.update(overrides)
    return argparse.Namespace(**values)


def _unused_workspace_evidence(**_kwargs: Any) -> dict[str, Any]:
    raise AssertionError("the workspace evidence probe must not be called")


class ReviewGuardBoundaryTests(unittest.TestCase):
    """``review_guard`` is runner-free, writes nothing, and owns the git shell."""

    def setUp(self) -> None:
        self.source = Path(review_guard.__file__ or "").read_text(encoding="utf-8")

    def test_review_guard_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, review_guard)

    def test_the_module_writes_no_durable_record(self) -> None:
        # Every function here is a projection of the filesystem, a parsed
        # namespace, or a prior durable record. The event log, the state
        # payload, and the pause all stay with the run loop.
        for writer in ("write_text(", "open(", "mkdir(", "unlink("):
            self.assertNotIn(writer, self.source, msg=writer)

    def test_git_is_spawned_from_exactly_two_places(self) -> None:
        # `_run_git` and `_run_git_bytes` are the only spawns, both through the
        # shared runner with `error=RunError`, which is what converts a missing
        # executable and an OS failure into `RunError`; a direct
        # `subprocess.run` would be a caller that can still meet `OSError`.
        self.assertEqual(0, self.source.count("subprocess.run("))
        self.assertEqual(
            2, self.source.count("run_git_or_raise(cwd, args, error=RunError")
        )

    def test_the_workspace_evidence_projection_arrives_as_a_seam(self) -> None:
        # The projection that classifies a comparison against a write scope is
        # owned by `workspace_evidence`, so it arrives as a structural callback
        # rather than as an import this module may not make.
        self.assertIn("class WorkspaceEvidenceProbe(Protocol):", self.source)
        self.assertEqual(
            2, self.source.count("workspace_evidence: WorkspaceEvidenceProbe")
        )
        self.assertNotIn("_workspace_write_guard_evidence", self.source)


class ReviewGuardExcludeTests(unittest.TestCase):
    """The allowlist is a refusal, not a filter."""

    def test_the_environment_and_the_flag_merge_into_one_sorted_tuple(self) -> None:
        args = _args(review_guard_exclude=["notes/", "claude"])
        with mock.patch.dict(
            os.environ,
            {review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR: os.pathsep.join(["b", "a"])},
        ):
            self.assertEqual(
                ("a", "b", "claude", "notes"),
                review_guard._review_guard_excludes(args),
            )

    def test_blank_entries_are_dropped_rather_than_widening_the_guard(self) -> None:
        args = _args(review_guard_exclude=["  ", "", "kept"])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR, None)
            self.assertEqual(("kept",), review_guard._review_guard_excludes(args))

    def test_an_absolute_path_is_refused_instead_of_normalized(self) -> None:
        args = _args(review_guard_exclude=["/etc"])
        with self.assertRaisesRegex(RunError, "must be repo-relative"):
            review_guard._review_guard_excludes(args)

    def test_a_parent_segment_is_refused_instead_of_resolved(self) -> None:
        args = _args(review_guard_exclude=["notes/../../secrets"])
        with self.assertRaisesRegex(RunError, r"must not contain '\.\.' segments"):
            review_guard._review_guard_excludes(args)

    def test_a_missing_namespace_attribute_reads_as_no_allowlist(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(review_guard.REVIEW_GUARD_EXCLUDE_ENV_VAR, None)
            self.assertEqual((), review_guard._review_guard_excludes(_args()))
        self.assertFalse(review_guard._review_guard_include_ignored(_args()))
        self.assertTrue(
            review_guard._review_guard_include_ignored(
                _args(review_guard_include_ignored=True)
            )
        )


class GitShellTests(unittest.TestCase):
    """Both shells classify their own failures so no caller meets ``OSError``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        init_git_repo(self.repo)

    def test_a_missing_git_executable_becomes_a_run_error(self) -> None:
        with mock.patch.object(
            review_guard.subprocess, "run", side_effect=FileNotFoundError("git")
        ):
            with self.assertRaisesRegex(RunError, "git executable not found on PATH"):
                review_guard._run_git(self.repo, ["status"])
            with self.assertRaisesRegex(RunError, "git executable not found on PATH"):
                review_guard._run_git_bytes(self.repo, ["status"])

    def test_an_os_failure_names_the_argv_it_failed_on(self) -> None:
        with mock.patch.object(
            review_guard.subprocess, "run", side_effect=OSError("too many files")
        ):
            with self.assertRaisesRegex(
                RunError, "could not run git status --porcelain: too many files"
            ):
                review_guard._run_git(self.repo, ["status", "--porcelain"])
            with self.assertRaisesRegex(
                RunError, "could not run git status --porcelain: too many files"
            ):
                review_guard._run_git_bytes(self.repo, ["status", "--porcelain"])

    def test_the_bytes_shell_does_not_translate_line_endings(self) -> None:
        # Text mode collapses `\r\n` and a lone `\r` onto `\n`, which would
        # report two distinct file contents as one fingerprint value.
        (self.repo / "crlf.txt").write_bytes(b"one\r\ntwo\rthree\n")
        result = review_guard._run_git_bytes(
            self.repo, ["hash-object", "-w", "--", "crlf.txt"]
        )
        self.assertEqual(0, result.returncode)
        blob = review_guard._run_git_bytes(
            self.repo, ["cat-file", "blob", result.stdout.decode().strip()]
        )
        self.assertEqual(b"one\r\ntwo\rthree\n", blob.stdout)

    def test_the_head_reading_carries_the_failure_it_saw(self) -> None:
        unborn = Path(self._tmp.name) / "unborn"
        init_git_repo(unborn)
        review_guard._run_git(unborn, ["update-ref", "-d", "HEAD"])
        payload = review_guard._read_git_head(unborn)
        self.assertIsNone(payload["head"])
        self.assertNotEqual(0, payload["returncode"])

        head = review_guard._read_git_head(self.repo)
        self.assertEqual(40, len(str(head["head"])))
        self.assertEqual(0, head["returncode"])


class WorktreeGateTests(unittest.TestCase):
    """The clean-worktree gates the run takes before auto-commit."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        init_git_repo(self.repo)

    def test_a_directory_outside_a_repository_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(RunError, "not inside a git repository"):
            review_guard._validate_git_worktree(outside)

    def test_a_clean_repository_passes_both_gates(self) -> None:
        review_guard._validate_git_worktree(self.repo)
        review_guard._validate_clean_git_status(self.repo)
        self.assertTrue(review_guard._git_status_is_clean(self.repo))

    def test_a_dirty_repository_raises_where_the_predicate_reports(self) -> None:
        (self.repo / "dirty.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(RunError, "must be empty before auto-commit"):
            review_guard._validate_clean_git_status(self.repo)
        self.assertFalse(review_guard._git_status_is_clean(self.repo))

    def test_the_legacy_preflight_reports_no_repository_rows(self) -> None:
        context = verification.VerificationContext.legacy(self.repo)
        self.assertEqual(
            {},
            review_guard._validate_commit_preflight(
                self.repo, verification_context=context
            ),
        )


class PorcelainParsingTests(unittest.TestCase):
    """NUL-framed porcelain names every dirty path, including a rename source."""

    def test_each_entry_is_named_once_and_renames_name_both_sides(self) -> None:
        output = "\0".join(
            [
                " M kept.txt",
                "?? new.txt",
                "R  after.txt",
                "before.txt",
                "",
            ]
        )
        self.assertEqual(
            ["kept.txt", "new.txt", "after.txt", "before.txt"],
            review_guard._porcelain_dirty_paths(output),
        )

    def test_a_repeated_path_collapses_to_one_entry(self) -> None:
        output = "\0".join([" M same.txt", "?? same.txt", ""])
        self.assertEqual(["same.txt"], review_guard._porcelain_dirty_paths(output))


class RetainedEvidenceTests(unittest.TestCase):
    """A retained-scope sanction reads the guard's own proof, and nothing else."""

    def test_only_qualified_changed_paths_count_as_retained(self) -> None:
        record = {
            "changed": {
                "repo-a": {"qualified_paths": ["A:one.txt", "A:two.txt"]},
                "repo-b": {"qualified_paths": "not-a-sequence"},
                "repo-c": "not-a-mapping",
            },
            "violations": [{"path": "A:never-read.txt"}],
        }
        self.assertEqual(
            frozenset({"A:one.txt", "A:two.txt"}),
            review_guard._retained_turn_changed_paths(record),
        )

    def test_a_record_without_changed_evidence_proves_nothing(self) -> None:
        self.assertEqual(
            frozenset(), review_guard._retained_turn_changed_paths({"violations": []})
        )

    def test_unavailable_evidence_renders_its_reason(self) -> None:
        evidence = review_guard._RetainedContentEvidence(False, reason="no snapshot")
        self.assertEqual(
            {
                "available": False,
                "drifted_paths": [],
                "repositories": [],
                "reason": "no snapshot",
            },
            evidence.payload(),
        )

    def test_available_evidence_omits_the_reason_key(self) -> None:
        evidence = review_guard._RetainedContentEvidence(
            True, drifted=frozenset({"A:b.txt"}), repositories=("repo-a",)
        )
        self.assertEqual(
            {
                "available": True,
                "drifted_paths": ["A:b.txt"],
                "repositories": ["repo-a"],
            },
            evidence.payload(),
        )


class RepairCommitPreflightTests(unittest.TestCase):
    """A repair child never carries dirt it is not recorded as having written."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        init_git_repo(self.repo)
        self.context = verification.VerificationContext.legacy(self.repo)

    def test_the_error_is_a_run_error_that_carries_its_details(self) -> None:
        error = review_guard.RepairCommitPreflightError("boom", {"key": "value"})
        self.assertIsInstance(error, RunError)
        self.assertEqual({"key": "value"}, error.details)

    def test_a_clean_legacy_checkout_records_its_one_repository(self) -> None:
        details = review_guard._repair_commit_preflight(
            self.repo,
            verification_context=self.context,
            session_scope=None,
            workspace_evidence=_unused_workspace_evidence,
        )
        self.assertEqual("per-child", details["repair_commit_mode"])
        (row,) = details["repositories"]
        self.assertIsNone(row["alias"])
        self.assertTrue(row["clean"])
        self.assertEqual([], row["dirty_paths"])

    def test_dirt_the_child_did_not_author_refuses_before_the_harness(self) -> None:
        (self.repo / "unrelated.txt").write_text("operator work\n", encoding="utf-8")
        with self.assertRaises(review_guard.RepairCommitPreflightError) as caught:
            review_guard._repair_commit_preflight(
                self.repo,
                verification_context=self.context,
                session_scope=None,
                workspace_evidence=_unused_workspace_evidence,
            )
        self.assertIn("requires clean writable repositories", str(caught.exception))
        self.assertEqual(
            ["unrelated.txt"],
            caught.exception.details["dirty_repositories"][0]["dirty_paths"],
        )

    def test_a_legacy_checkout_cannot_recover_a_retained_scope(self) -> None:
        # The retained-scope sanction is defined against a registered
        # workspace's write scope. Without one there is no evidence to read,
        # so the branch refuses rather than sanctioning on the pathnames alone.
        (self.repo / "retained.txt").write_text("retained\n", encoding="utf-8")
        with self.assertRaises(review_guard.RepairCommitPreflightError) as caught:
            review_guard._repair_commit_preflight(
                self.repo,
                verification_context=self.context,
                session_scope=None,
                workspace_evidence=_unused_workspace_evidence,
                retained_scope_repair={
                    "session_id": "3",
                    "changed": {"repo": {"qualified_paths": ["retained.txt"]}},
                },
            )
        details = caught.exception.details
        self.assertFalse(
            details["retained_scope_repair"]["content_evidence"]["available"]
        )
        self.assertEqual(
            ["retained.txt"],
            details["retained_scope_repair"]["unsanctioned_dirty_paths"],
        )
        self.assertIn("without durable evidence", str(caught.exception))

    def test_a_directory_that_is_not_a_worktree_refuses(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        with self.assertRaises(review_guard.RepairCommitPreflightError) as caught:
            review_guard._repair_commit_preflight(
                outside,
                verification_context=verification.VerificationContext.legacy(outside),
                session_scope=None,
                workspace_evidence=_unused_workspace_evidence,
            )
        self.assertIn("is not a Git worktree", str(caught.exception))


class CommitAtEndScopeTests(unittest.TestCase):
    """``--commit-at-end`` and ``--max-sessions`` may not silently truncate."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan_dir = Path(self._tmp.name) / "plan"
        write_plan(self.plan_dir, ("pending", "pending", "complete"))
        self.snapshot = plans.inspect_plan(self.plan_dir)

    def test_no_session_cap_is_always_in_scope(self) -> None:
        review_guard._validate_commit_at_end_scope(
            snapshot=self.snapshot, max_sessions=None
        )

    def test_a_cap_that_covers_every_pending_session_is_in_scope(self) -> None:
        review_guard._validate_commit_at_end_scope(
            snapshot=self.snapshot, max_sessions=2
        )

    def test_a_cap_below_the_pending_count_is_refused(self) -> None:
        with self.assertRaisesRegex(RunError, "--commit-at-end cannot be combined"):
            review_guard._validate_commit_at_end_scope(
                snapshot=self.snapshot, max_sessions=1
            )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
