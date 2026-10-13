"""Commit observability projected from recorded run history."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    state,
    supervisor_audit_checks,
    supervisor_audit_evidence_constants,
    supervisor_audit_vocabulary,
)
from agent_session_loop.assurance_models_findings import DeterministicResult
from tests.support_supervisor_audit import RunHistoryFixture


class CommitObservabilityTests(RunHistoryFixture, unittest.TestCase):
    """A commit the guard saw is projected even when the runner made none."""

    HEAD_BEFORE = "ab503cce84cf73bcaadb3c5e09f395f370ca5bc0"
    HEAD_AFTER = "ede6829cedf2d8e91d1aaf91c0a8cc401dfdbe8d"

    def _worker_committed(
        self, tmp: str, **changes: Any
    ) -> tuple[Path, Path, Path, Path]:
        history = self._history(Path(tmp))
        workspace, _, state_dir, run_dir = history
        state_payload = self._read_state(state_dir)
        state_payload["commit_mode"] = None
        self._write_state(state_dir, state_payload)

        turn_dir = run_dir / "turn-0001-01"
        result = self._clean_result(workspace, turn_dir)
        result["workspace_write_guard"]["changed"] = {
            "app": changes
            or {
                "head_changed": True,
                "head_transition": {
                    "before": self.HEAD_BEFORE,
                    "after": self.HEAD_AFTER,
                    "relation": "fast-forward",
                    "commits_added": 1,
                    "commits_removed": 0,
                    "history_rewrite": False,
                },
            }
        }
        self._write_result(turn_dir, result)
        return history

    def test_a_worker_commit_under_a_null_commit_mode_is_projected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(tmp)

            commits = self._bundle(history).payload["commits"]

            self.assertIsNone(commits["commit_mode"])
            observed = commits["observed_head_transitions"]
            self.assertEqual(1, len(observed))
            self.assertEqual("app", observed[0]["repository"])
            self.assertEqual("turn-0001-01", observed[0]["turn"])
            transition = observed[0]["transition"]
            self.assertEqual("fast-forward", transition["relation"])
            self.assertEqual(1, transition["commits_added"])
            self.assertEqual(self.HEAD_BEFORE, transition["before"])
            self.assertEqual(self.HEAD_AFTER, transition["after"])

            # The move the guard saw is what landed, so the projections a
            # reader reaches for first no longer contradict it.
            self.assertEqual(1, len(commits["attempts"]))
            self.assertEqual(1, len(commits["commits"]))
            self.assertEqual(1, len(commits["commit_groups"]))
            projected = commits["commits"][0]
            self.assertEqual(
                supervisor_audit_evidence_constants.OBSERVED_COMMIT_ORIGIN,
                projected["origin"],
            )
            self.assertEqual("app", projected["repository_alias"])
            self.assertEqual(self.HEAD_BEFORE, projected["head_before"])
            self.assertEqual(self.HEAD_AFTER, projected["head_after"])
            self.assertEqual(self.HEAD_AFTER, projected["commit_sha"])
            attempt = commits["attempts"][0]
            self.assertEqual("turn-0001-01", attempt["name"])
            self.assertEqual("run/turn-0001-01", attempt["reference"])
            self.assertEqual(["app"], commits["commit_groups"][0]["ordered_aliases"])

    def test_a_projected_commit_takes_its_range_from_one_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A continuation merge leaves the entry's own heads on the session
            # baseline, older than this turn's move, while the transition and
            # its count describe the turn alone. Mixing the two claimed the
            # count over a range it was never measured on (ASI-158).
            baseline = "c" * 40
            history = self._worker_committed(
                tmp,
                head_changed=True,
                head_before=baseline,
                head_after=self.HEAD_AFTER,
                head_transition={
                    "before": self.HEAD_BEFORE,
                    "after": self.HEAD_AFTER,
                    "relation": "fast-forward",
                    "commits_added": 1,
                    "commits_removed": 0,
                    "history_rewrite": False,
                },
            )

            commits = self._bundle(history).payload["commits"]

            projected = commits["commits"][0]
            self.assertEqual(self.HEAD_BEFORE, projected["head_before"])
            self.assertEqual(self.HEAD_AFTER, projected["head_after"])
            self.assertEqual(1, projected["commits_added"])

    def test_a_projected_commit_without_a_transition_keeps_the_entry_heads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(
                tmp,
                head_changed=True,
                head_before=self.HEAD_BEFORE,
                head_after=self.HEAD_AFTER,
            )

            projected = self._bundle(history).payload["commits"]["commits"][0]

            self.assertEqual(self.HEAD_BEFORE, projected["head_before"])
            self.assertEqual(self.HEAD_AFTER, projected["head_after"])
            self.assertIsNone(projected["commits_added"])

    def _worker_committed_under_key(
        self, tmp: str, key: str, *, registered: bool = True
    ) -> tuple[Path, Path, Path, Path]:
        """A worker commit the guard filed under `key` rather than an alias."""
        history = self._worker_committed(tmp)
        _, _, _, run_dir = history
        turn_dir = run_dir / "turn-0001-01"
        result = json.loads(
            (turn_dir / state.RESULT_FILE_NAME).read_text(encoding="utf-8")
        )
        guard = result["workspace_write_guard"]
        guard["changed"] = {key: next(iter(guard["changed"].values()))}
        if registered:
            guard["registered_repositories"] = [
                {"alias": "app", "path": "1_dev_projects/app"}
            ]
        self._write_result(turn_dir, result)
        return history

    def _mutation_guard_verdict(
        self, history: tuple[Path, Path, Path, Path]
    ) -> DeterministicResult:
        results = {
            result.check: result
            for result in supervisor_audit_checks.run_deterministic_checks(
                self._bundle(history)
            )
        }
        return results["mutation-commit-guards"]

    def test_snapshot_artifacts_do_not_prove_a_critical_guard_failure(self) -> None:
        for name in (
            supervisor_audit_vocabulary.MUTATION_ARTIFACT_NAME,
            supervisor_audit_vocabulary.WORKSPACE_CHANGE_ARTIFACT_NAME,
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                history = self._worker_committed(tmp)
                payload = copy.deepcopy(self._bundle(history).payload)
                reference = f"run/{name}"
                payload["evidence"].append(
                    {"kind": "artifact", "locator": reference, "digest": "a" * 64}
                )
                verdict = {
                    r.check: r
                    for r in supervisor_audit_checks.run_deterministic_checks(payload)
                }["mutation-commit-guards"]
                self.assertEqual("unavailable", verdict.status)
                self.assertIn("identify no writer", verdict.detail)
                self.assertIn(reference, verdict.evidence)
                for scope in payload["scopes"]["sessions"]:
                    scope["writes"] = []
                verdict = {
                    r.check: r
                    for r in supervisor_audit_checks.run_deterministic_checks(payload)
                }["mutation-commit-guards"]
                self.assertEqual("fail", verdict.status)
                self.assertIn("which no session declares as writable", verdict.detail)

    def test_a_change_keyed_by_repository_path_is_named_by_its_alias(self) -> None:
        # The change map is keyed by path; session scopes are keyed by alias.
        # Comparing the two namespaces read a commit to a declared repository
        # as a commit to an undeclared one.
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed_under_key(tmp, "1_dev_projects/app")

            commits = self._bundle(history).payload["commits"]

            projected = commits["commits"][0]
            self.assertEqual("app", projected["repository_alias"])
            self.assertEqual("1_dev_projects/app", projected["repository_key"])
            self.assertEqual("pass", self._mutation_guard_verdict(history).status)

    def test_a_commit_in_a_linked_worktree_is_named_by_its_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = "1_dev_projects/app::worktree::/elsewhere/feature-worktree"
            history = self._worker_committed_under_key(tmp, key)

            commits = self._bundle(history).payload["commits"]

            # A linked worktree shares its repository's object store, so the
            # commit landed in the registered repository the scope declares.
            projected = commits["commits"][0]
            self.assertEqual("app", projected["repository_alias"])
            self.assertEqual(key, projected["repository_key"])
            self.assertEqual("pass", self._mutation_guard_verdict(history).status)

    def test_an_unregistered_repository_is_still_reported_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed_under_key(tmp, "1_dev_projects/other")

            commits = self._bundle(history).payload["commits"]

            self.assertEqual(
                "1_dev_projects/other", commits["commits"][0]["repository_alias"]
            )
            verdict = self._mutation_guard_verdict(history)
            self.assertEqual("fail", verdict.status)
            self.assertIn(
                "committed to 1_dev_projects/other, which no session declares "
                "as writable",
                verdict.detail,
            )

    def test_a_guard_naming_no_registry_leaves_its_key_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed_under_key(
                tmp, "1_dev_projects/app", registered=False
            )

            commits = self._bundle(history).payload["commits"]

            self.assertEqual(
                "1_dev_projects/app", commits["commits"][0]["repository_alias"]
            )

    def test_a_projected_commit_is_not_a_commit_mode_contradiction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(tmp)

            results = {
                result.check: result
                for result in supervisor_audit_checks.run_deterministic_checks(
                    self._bundle(history)
                )
            }

            # Nobody asked the runner to make this commit, so the commit mode
            # it would have run under is absent for the same reason it is.
            self.assertEqual("pass", results["mutation-commit-guards"].status)
            self.assertIn(
                "1 commit attempt(s)", results["mutation-commit-guards"].detail
            )

    def test_a_recorded_commit_is_not_projected_a_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(tmp)
            _, _, state_dir, _ = history
            state_payload = self._read_state(state_dir)
            state_payload["commits"] = [
                {"repository_alias": "app", "head_after": self.HEAD_AFTER}
            ]
            self._write_state(state_dir, state_payload)

            commits = self._bundle(history).payload["commits"]

            self.assertEqual(1, len(commits["commits"]))
            self.assertNotIn("origin", commits["commits"][0])
            self.assertEqual([], commits["attempts"])

    def test_a_moved_ref_is_projected_even_when_head_stayed_put(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._worker_committed(
                tmp,
                head_changed=False,
                refs_changed=[
                    {
                        "ref": "refs/heads/feature",
                        "before": self.HEAD_BEFORE,
                        "after": self.HEAD_AFTER,
                    }
                ],
            )

            commits = self._bundle(history).payload["commits"]

            self.assertEqual([], commits["observed_head_transitions"])
            self.assertEqual(1, len(commits["commits"]))
            self.assertEqual(
                ["refs/heads/feature"], commits["commits"][0]["refs_changed"]
            )

    def test_a_run_whose_head_never_moved_projects_no_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            commits = self._bundle(self._history(Path(tmp))).payload["commits"]

            self.assertEqual([], commits["observed_head_transitions"])

    def test_a_head_move_without_a_transition_is_projected_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            workspace, _, _, run_dir = history
            turn_dir = run_dir / "turn-0001-01"
            result = self._clean_result(workspace, turn_dir)
            result["workspace_write_guard"]["changed"] = {"app": {"head_changed": True}}
            self._write_result(turn_dir, result)

            observed = self._bundle(history).payload["commits"][
                "observed_head_transitions"
            ]

            self.assertEqual(1, len(observed))
            self.assertIsNone(observed[0]["transition"])

    def _failed_commit_attempt(
        self,
        history: tuple[Path, Path, Path, Path],
        *,
        retain_streams: bool = True,
        success: bool = False,
    ) -> Path:
        """One commit attempt whose result names retained stdout and stderr."""
        commit_dir = history[3] / "commit-0001-session-01-app"
        commit_dir.mkdir()
        stdout_path = commit_dir / "stdout.log"
        stderr_path = commit_dir / "stderr.log"
        if retain_streams:
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(
                "error: pathspec 'docs/' did not match any file(s)\n", encoding="utf-8"
            )
        (commit_dir / state.RESULT_FILE_NAME).write_text(
            json.dumps(
                {
                    "success": success,
                    "returncode": 0 if success else 1,
                    "repository_alias": "app",
                    "session_id": "01",
                    "status_result": "success" if success else "commit-failed",
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                }
            ),
            encoding="utf-8",
        )
        return commit_dir

    def test_a_failed_commits_retained_streams_are_indexed(self) -> None:
        """ASI-137: a named artifact an auditor cannot locate is not evidence."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._failed_commit_attempt(history)

            bundle = self._bundle(history)
            locators = {entry["locator"] for entry in bundle.payload["evidence"]}

            self.assertIn("run/commit-0001-session-01-app/stderr.log", locators)
            self.assertIn("run/commit-0001-session-01-app/stdout.log", locators)

    def test_an_unretained_commit_stream_is_recorded_as_an_omission(self) -> None:
        """A path that is not on disk is stated, not passed over in silence."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._failed_commit_attempt(history, retain_streams=False)

            bundle = self._bundle(history)
            locators = {entry["locator"] for entry in bundle.payload["evidence"]}
            omitted = [
                entry
                for entry in bundle.payload["omissions"]
                if entry["section"] == "commits"
            ]

            self.assertNotIn("run/commit-0001-session-01-app/stderr.log", locators)
            self.assertEqual(1, len(omitted))
            self.assertEqual(
                supervisor_audit_evidence_constants.REASON_MISSING, omitted[0]["reason"]
            )
            self.assertEqual(2, omitted[0]["count"])

    def test_a_successful_commits_streams_are_left_unindexed(self) -> None:
        """Only a refused commit has a diagnostic worth locating."""
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            self._failed_commit_attempt(history, success=True)

            bundle = self._bundle(history)
            locators = {entry["locator"] for entry in bundle.payload["evidence"]}

            self.assertNotIn("run/commit-0001-session-01-app/stderr.log", locators)
            self.assertEqual(
                [],
                [
                    entry
                    for entry in bundle.payload["omissions"]
                    if entry["section"] == "commits"
                ],
            )


if __name__ == "__main__":
    unittest.main()
