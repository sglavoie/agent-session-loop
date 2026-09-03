"""How much a re-review must read, asserted at the seam rather than through a run.

The behavioural coverage of delta scoping already lives where it always has —
``tests/test_asl_review_lifecycle.py`` drives a real run through every
degradation reason a single repository can produce, ``tests/test_asl_cli.py``
owns the registered combined delta, and ``tests/test_asl_review.py`` owns the
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
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    handoff_guards,
    plans,
    review,
    review_continuation,
    review_delta,
    review_session_body,
    run_persistence,
    runner,
    state,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    assert_no_runner_import,
    git_stdout,
    init_git_repo,
    init_multi_repo_workspace,
    read_jsonl,
    write_completed_review_plan,
    write_multi_repo_plan,
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


class _DeltaHarness(unittest.TestCase):
    """A real repository, a completed review on disk, and an event log."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo_root = root / "repo"
        init_git_repo(self.repo_root)
        self.plan_dir = root / "plan"
        self.finding = _structured_review_finding()
        write_completed_review_plan(self.plan_dir, [self.finding])
        self.state_root = root / "state"
        self.event_path = (
            state.plan_state_dir(self.plan_dir, self.state_root)
            / events.EVENTS_FILE_NAME
        )

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def record_repair_cycle(self, **overrides: Any) -> None:
        record: dict[str, Any] = {
            "cycle": 1,
            "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            "review_session_id": "2",
            "repair_session_ids": ["2.1"],
            "fingerprints": ["finding:F1"],
            "worktree_dirty": False,
            "dirty_paths": [],
            "repo_head": self.head(),
        }
        record.update(overrides)
        events.append_event(
            self.event_path, events.Event(events.REVIEW_CYCLE_EVENT, record)
        )

    def compute(self) -> tuple[dict[str, Any] | None, str | None]:
        return review_delta._compute_review_delta_context(
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
        )


class PriorCycleEvidenceTests(_DeltaHarness):
    """What the boundary is willing to treat as the last repair."""

    def test_no_cycle_at_all_is_not_a_degraded_scope(self) -> None:
        # `None` rather than a full-scope delta: there is no prior review to
        # re-verify, so the caller writes an ordinary review, not a re-review.
        self.assertIsNone(
            review_delta._last_repairs_materialized_cycle(self.event_path)
        )
        self.assertEqual((None, "no-prior-repair-cycle"), self.compute())

    def test_a_cycle_that_did_not_materialize_repairs_is_skipped(self) -> None:
        self.record_repair_cycle(cycle=1, disposition="exhausted")
        self.assertIsNone(
            review_delta._last_repairs_materialized_cycle(self.event_path)
        )

    def test_the_newest_materialized_reopen_supersedes_its_own_findings(self) -> None:
        self.record_repair_cycle()
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "materialized": True,
                    "repo_head": self.head(),
                    "findings": ["stale"],
                    "current_findings": ["fresh"],
                    "repositories": ["stale"],
                    "current_repositories": ["fresh"],
                },
            ),
        )
        cycle = review_delta._last_repairs_materialized_cycle(self.event_path)
        assert cycle is not None
        self.assertEqual(["fresh"], cycle["findings"])
        self.assertEqual(["fresh"], cycle["repositories"])

    def test_findings_are_read_from_the_review_still_on_disk(self) -> None:
        self.assertEqual(
            [self.finding], review_delta._prior_review_findings(self.plan_dir)
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()
        self.assertEqual([], review_delta._prior_review_findings(self.plan_dir))

    def test_an_unreadable_artifact_digest_is_none_not_a_raised_error(self) -> None:
        # A `PlanError` here would abort the caller mid-comparison and let a
        # read failure look like a cycle result.
        self.assertNotIn(None, review_delta._review_artifact_digests(self.plan_dir))
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).unlink()
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).unlink()
        self.assertEqual(
            (None, None), review_delta._review_artifact_digests(self.plan_dir)
        )


class FailClosedScopeTests(_DeltaHarness):
    """Every probe that can fail degrades to full scope under its own name."""

    def _degrades_with(self, reason: str) -> None:
        delta, fallback = self.compute()
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertEqual([self.finding], delta["prior_findings"])
        self.assertEqual(reason, fallback)

    def test_a_missing_recorded_base_degrades(self) -> None:
        self.record_repair_cycle(repo_head=None)
        self._degrades_with("no-recorded-base")

    def test_a_dirty_base_with_no_path_evidence_degrades(self) -> None:
        self.record_repair_cycle(worktree_dirty=True, dirty_paths=None)
        self._degrades_with("dirty-base-evidence-missing")

    def test_an_unresolvable_base_degrades(self) -> None:
        self.record_repair_cycle(repo_head="0" * 40)
        self._degrades_with("base-unresolvable")

    def test_a_base_that_is_no_longer_an_ancestor_degrades(self) -> None:
        # A rebase, reset or amend moves the recorded base off the history the
        # repair actually landed on; diffing against it would misreport scope.
        base = self.head()
        (self.repo_root / "a.py").write_text("one\n", encoding="utf-8")
        self.git("add", "a.py")
        self.git("commit", "-m", "a")
        orphan = self.head()
        self.git("reset", "--hard", base)
        self.record_repair_cycle(repo_head=orphan)
        self._degrades_with("base-not-ancestor")


class RegisteredScopeIsAllOrNothingTests(unittest.TestCase):
    """One unprovable repository degrades the whole workspace, not just itself.

    ``tests/test_asl_cli.py`` owns the combined delta's happy path. What is
    asserted here is the part that has no happy path to observe: a narrowed
    scope covering three of four repositories would read like a complete file
    list while saying nothing at all about the fourth.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.workspace_root = root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace_root, specs=self.SPECS
        )
        self.plan_dir = root / "plan"
        write_multi_repo_plan(self.plan_dir, ("complete",), specs=self.SPECS)
        self.finding = _structured_review_finding()
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "# Final review\n\nStatus: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {"version": 2, "outcome": "findings", "findings": [self.finding]}
            ),
            encoding="utf-8",
        )
        context = verification.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        assert context.workspace is not None
        self.workspace = context.workspace
        self.event_path = root / "events.jsonl"

    def _record_bases(self, bases: list[dict[str, Any]]) -> None:
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                    ),
                    "repositories": bases,
                },
            ),
        )

    def _head(self, alias: str) -> str:
        return git_stdout(self.repositories[alias], "rev-parse", "HEAD")

    def _compute(self) -> tuple[dict[str, Any] | None, str | None]:
        return review_delta._compute_registered_review_delta_context(
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            workspace=self.workspace,
        )

    def _degrades_with(self, reason: str) -> None:
        delta, fallback = self._compute()
        assert delta is not None
        self.assertEqual(review_session_body.REVIEW_SCOPE_FULL, delta["mode"])
        self.assertEqual([self.finding], delta["prior_findings"])
        self.assertNotIn("repositories", delta)
        self.assertEqual(reason, fallback)

    def test_a_cycle_that_recorded_no_repository_bases_degrades(self) -> None:
        # A single-repository cycle carries `repo_head` and no `repositories`
        # list at all, which is exactly the record a workspace cannot narrow
        # against.
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {
                    "disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
                    ),
                    "repo_head": self._head("SVC"),
                },
            ),
        )
        self._degrades_with("no-recorded-repository-bases")

    def test_an_empty_recorded_base_list_still_names_the_first_repository(
        self,
    ) -> None:
        # An empty list is a list: the loop runs and the first registry entry
        # is the one that cannot be proved, so the reason names it rather than
        # reporting the vaguer "no bases recorded".
        self._record_bases([])
        self._degrades_with("base-missing:SVC")

    def test_one_repository_missing_its_base_degrades_the_whole_workspace(
        self,
    ) -> None:
        # SVC is fully provable; OAK is not. The scope narrows for neither.
        self._record_bases(
            [
                {
                    "alias": "SVC",
                    "head": self._head("SVC"),
                    "worktree_dirty": False,
                    "dirty_paths": [],
                }
            ]
        )
        self._degrades_with("base-missing:OAK")

    def test_one_unresolvable_base_degrades_the_whole_workspace(self) -> None:
        self._record_bases(
            [
                {
                    "alias": alias,
                    "head": self._head(alias) if alias == "SVC" else "0" * 40,
                    "worktree_dirty": False,
                    "dirty_paths": [],
                }
                for alias in ("SVC", "OAK")
            ]
        )
        self._degrades_with("base-unresolvable:OAK")

    def test_one_unprovable_dirty_base_degrades_the_whole_workspace(self) -> None:
        self._record_bases(
            [
                {
                    "alias": "SVC",
                    "head": self._head("SVC"),
                    "worktree_dirty": False,
                    "dirty_paths": [],
                },
                {"alias": "OAK", "head": self._head("OAK"), "worktree_dirty": True},
            ]
        )
        self._degrades_with("dirty-base-evidence-missing:OAK")


class NarrowedScopeTests(_DeltaHarness):
    """What a narrowed scope is allowed to leave out — which is very little."""

    def test_the_changed_set_unions_worktree_index_untracked_and_baseline(
        self,
    ) -> None:
        base = self.head()
        (self.repo_root / "committed.py").write_text("one\n", encoding="utf-8")
        self.git("add", "committed.py")
        self.git("commit", "-m", "committed repair")
        (self.repo_root / "staged.py").write_text("two\n", encoding="utf-8")
        self.git("add", "staged.py")
        (self.repo_root / "untracked.py").write_text("three\n", encoding="utf-8")
        (self.repo_root / "README.md").write_text("dirty\n", encoding="utf-8")
        self.record_repair_cycle(
            repo_head=base,
            worktree_dirty=True,
            dirty_paths=["reviewed-dirty.py"],
        )

        delta, fallback = self.compute()
        assert delta is not None
        self.assertIsNone(fallback)
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, delta["mode"])
        self.assertEqual(base, delta["base_sha"])
        self.assertTrue(delta["baseline_dirty"])
        self.assertEqual(
            [
                "README.md",
                "committed.py",
                "reviewed-dirty.py",
                "staged.py",
                "untracked.py",
            ],
            delta["changed_files"],
        )

    def test_an_assume_unchanged_entry_joins_the_scope_wholesale(self) -> None:
        # Git compares such an entry against nothing, so the same blindness
        # that hides the repair makes it impossible to tell whether one
        # happened. The path joins the scope rather than being trusted.
        base = self.head()
        (self.repo_root / "hidden.py").write_text("one\n", encoding="utf-8")
        self.git("add", "hidden.py")
        self.git("commit", "-m", "hidden")
        self.git("update-index", "--assume-unchanged", "hidden.py")
        self.record_repair_cycle(repo_head=base)

        delta, fallback = self.compute()
        assert delta is not None
        self.assertIsNone(fallback)
        self.assertIn("hidden.py", delta["changed_files"])


class PendingScopeRefreshTests(_DeltaHarness):
    """A pending review's file list is rebuilt from current evidence."""

    def setUp(self) -> None:
        super().setUp()
        self.persistence = run_persistence._RunPersistence(write_state=False)

    def _write_pending_review_session(self, delta: dict[str, Any] | None) -> Path:
        path = self.plan_dir / "session-3.md"
        path.write_text(
            review_session_body.build_generated_review_session_body(
                plans.parse_session_id("3"),
                review_harness="codex",
                plan_dir=self.plan_dir,
                delta=delta,
            ),
            encoding="utf-8",
        )
        return path

    def _session_status(self, session_id: str) -> plans.SessionStatus:
        snapshot = plans.inspect_plan(self.plan_dir)
        return next(
            session for session in snapshot.sessions if session.id == session_id
        )

    def _refresh(self, path: Path) -> bool:
        return review_delta._refresh_pending_review_scope(
            session=self._session_status(path.stem.removeprefix("session-")),
            event_path=self.event_path,
            plan_dir=self.plan_dir,
            cwd=self.repo_root,
            workspace=None,
            run_id="run-1",
            persistence=self.persistence,
        )

    def test_a_full_scope_session_is_never_refreshed(self) -> None:
        # A session that already tells the reviewer to read everything cannot
        # be widened, so there is nothing to rewrite and no event to append.
        path = self._write_pending_review_session(None)
        before = path.read_text(encoding="utf-8")

        self.assertFalse(self._refresh(path))
        self.assertEqual(before, path.read_text(encoding="utf-8"))
        self.assertFalse(self.event_path.exists())

    def test_a_stale_file_list_is_rebuilt_and_the_rewrite_is_recorded(self) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        path = self._write_pending_review_session(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": base,
                "changed_files": ["stale.py"],
                "baseline_dirty": False,
                "prior_findings": [self.finding],
            }
        )
        # Created after the session was authored: invisible to the file list
        # the reviewer would otherwise have been handed.
        (self.repo_root / "appeared.py").write_text("new\n", encoding="utf-8")

        self.assertTrue(self._refresh(path))
        rewritten = path.read_text(encoding="utf-8")
        self.assertIn("appeared.py", rewritten)
        self.assertNotIn("stale.py", rewritten)
        # The session keeps its identity and its pending handoff.
        self.assertIn(review.REVIEW_SESSION_MARKER, rewritten)
        self.assertIn("- **Status**: pending", rewritten)

        recorded = [
            record
            for record in read_jsonl(self.event_path)
            if record["name"] == events.REVIEW_SCOPE_EVENT
        ]
        self.assertEqual(1, len(recorded))
        payload = recorded[0]["payload"]
        self.assertEqual("run-1", payload["run_id"])
        self.assertEqual("3", payload["session_id"])
        self.assertEqual(review_session_body.REVIEW_SCOPE_DELTA, payload["scope"])
        self.assertEqual(base, payload["base_sha"])
        self.assertIsNone(payload["fallback_reason"])
        self.assertIs(True, payload["refreshed"])

    def test_an_unchanged_scope_writes_nothing(self) -> None:
        base = self.head()
        self.record_repair_cycle(repo_head=base)
        path = self._write_pending_review_session(
            {
                "mode": review_session_body.REVIEW_SCOPE_DELTA,
                "base_sha": base,
                "changed_files": [],
                "baseline_dirty": False,
                "prior_findings": [self.finding],
            }
        )

        self.assertFalse(self._refresh(path))
        self.assertEqual(
            [],
            [
                record
                for record in read_jsonl(self.event_path)
                if record["name"] == events.REVIEW_SCOPE_EVENT
            ],
        )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    unittest.main()
