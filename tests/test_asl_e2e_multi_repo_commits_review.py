"""Four-repository end-to-end commit, review, and rebind tests.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json
from pathlib import Path

from agent_session_loop import (
    events,
    plans_metadata,
    prepare_results,
    review,
)
from tests.asl_e2e_multi_repo_support import MultiRepositoryEndToEndBase
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    git_stdout,
    init_multi_repo_workspace,
    read_jsonl,
)


class MultiRepositoryCommitReviewRebindTests(MultiRepositoryEndToEndBase):
    """Commit modes, combined review, and rebind atomicity for four repositories."""

    def test_per_session_and_end_commit_modes_use_manifest_order(self) -> None:
        for commit_flag, scope in (
            ("--commit-per-session", "session"),
            ("--commit-at-end", "end"),
        ):
            with self.subTest(commit_flag=commit_flag):
                name = commit_flag.removeprefix("--")
                workspace, plan_dir, state_dir, repositories, _unregistered = (
                    self._prepare_case(name)
                )
                commit_log = self.tmp_path / f"{name}.jsonl"
                mutations = [
                    {
                        "path": str(repositories[alias] / "committed.txt"),
                        "text": f"{alias}\n",
                    }
                    for alias in ("SVC", "OAK", "OWL")
                ]
                completed, payload = self._run_plan(
                    plan_dir,
                    workspace,
                    state_dir,
                    commit_flag,
                    env_updates={
                        "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                        "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                            {
                                "1": [
                                    "SVC:committed.txt",
                                    "OAK:committed.txt",
                                    "OWL:committed.txt",
                                ],
                                "2": [],
                            }
                        ),
                        "FAKE_CODEX_COMMIT_LOG": str(commit_log),
                    },
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(
                    ["SVC", "OAK", "OWL"],
                    [commit["repository_alias"] for commit in payload["commits"]],
                )
                group = next(
                    group
                    for group in payload["commit_groups"]
                    if group["ordered_aliases"]
                )
                self.assertEqual(scope, group["scope"])
                self.assertEqual(["SVC", "OAK", "OWL"], group["ordered_aliases"])
                self.assertEqual(
                    [
                        str(repositories[alias].resolve())
                        for alias in ("SVC", "OAK", "OWL")
                    ],
                    [record["cwd"] for record in read_jsonl(commit_log)],
                )

    def test_partial_commit_failure_recovers_in_a_new_process(self) -> None:
        workspace, plan_dir, state_dir, repositories, _unregistered = (
            self._prepare_case("partial-commit")
        )
        commit_log = self.tmp_path / "partial-commit.jsonl"
        mutations = [
            {
                "path": str(repositories[alias] / "partial.txt"),
                "text": f"{alias}\n",
            }
            for alias in ("SVC", "OAK", "OWL")
        ]
        first, first_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--commit-per-session",
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {
                        "1": [
                            "SVC:partial.txt",
                            "OAK:partial.txt",
                            "OWL:partial.txt",
                        ]
                    }
                ),
                "FAKE_CODEX_COMMIT_LOG": str(commit_log),
                "FAKE_CODEX_COMMIT_FAIL_CWD_BASENAME": repositories["OAK"].name,
            },
        )
        # The refused commit does not stop the run where it fails: it is retried
        # to the end of the group's budget first. Spending that budget is what
        # stops it, because the alternative is selecting the next session over a
        # commit this run could not make. The group is still left owed and said
        # out loud, which is what the next process recovers from.
        self.assertEqual(1, first.returncode, first.stderr)
        self.assertTrue(first_payload["paused"])
        self.assertEqual("commit-failed", first_payload["pause_reason"])
        self.assertEqual(
            ["OAK"],
            [
                summary["failed_alias"]
                for summary in first_payload["unresolved_commit_groups"]
            ],
        )
        failed_group = first_payload["commit_groups"][0]
        self.assertEqual("OAK", failed_group["failed_alias"])
        self.assertEqual(["OAK", "OWL"], failed_group["remaining_aliases"])
        self.assertEqual(
            ["SVC"],
            [row["alias"] for row in failed_group["successful_repositories"]],
        )
        svc_head = git_stdout(repositories["SVC"], "rev-parse", "HEAD")

        dry, dry_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--resume",
            "--dry-run",
            env_updates={"FAKE_CODEX_COMMIT_LOG": str(commit_log)},
        )
        self.assertEqual(0, dry.returncode, dry.stderr)
        self.assertTrue(dry_payload["commit_group_recovery"]["pending"])

        resumed, resumed_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--resume",
            env_updates={"FAKE_CODEX_COMMIT_LOG": str(commit_log)},
        )
        self.assertEqual(0, resumed.returncode, resumed.stderr)
        self.assertTrue(resumed_payload["commit_group_recovery"]["success"])
        # SVC committed once and OAK refused three times in the first run —
        # the commit itself plus the two in-run retries the group is worth —
        # before the second process completed the group it was still owed.
        self.assertEqual(
            [
                repositories["SVC"].name,
                repositories["OAK"].name,
                repositories["OAK"].name,
                repositories["OAK"].name,
                repositories["OAK"].name,
                repositories["OWL"].name,
            ],
            [Path(record["cwd"]).name for record in read_jsonl(commit_log)],
        )
        self.assertEqual(svc_head, git_stdout(repositories["SVC"], "rev-parse", "HEAD"))
        for repository in repositories.values():
            self.assertEqual("", git_stdout(repository, "status", "--porcelain"))

    def test_combined_review_repairs_rereviews_and_rejects_mutation(self) -> None:
        workspace, plan_dir, state_dir, _repositories, _unregistered = (
            self._prepare_case("review-cycle")
        )
        completed, payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            "--review-harness",
            "claude",
            "--max-review-fix-cycles",
            "1",
            env_updates={
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_REVIEW_OUTCOMES": "findings,all-clear",
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps({"1": [], "2": []}),
            },
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            ["1", "2", "3", "3.1", "4"],
            [turn["session_id"] for turn in payload["turns"]],
        )
        self.assertEqual("all-clear", payload["review_fix_disposition"])
        review_artifact = json.loads(
            (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(3, review_artifact["version"])
        repair_body = (plan_dir / "session-3.1.md").read_text(encoding="utf-8")
        self.assertIn("**Reads repositories**: SVC, OAK, OWL, Kiln", repair_body)
        self.assertIn("**Writes repositories**: SVC", repair_body)
        for session_id in ("3", "3.1", "4"):
            self.assertTrue(
                next(
                    turn["workspace_write_guard"]["passed"]
                    for turn in payload["turns"]
                    if turn["session_id"] == session_id
                )
            )

        (
            mutation_workspace,
            mutation_plan,
            mutation_state,
            mutation_repositories,
            _mutation_unregistered,
        ) = self._prepare_case("review-mutation")
        rejected, rejected_payload = self._run_plan(
            mutation_plan,
            mutation_workspace,
            mutation_state,
            "--review-harness",
            "claude",
            "--max-review-fix-cycles",
            "0",
            env_updates={
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_REVIEW_OUTCOMES": "all-clear",
                "FAKE_CODEX_REVIEW_MUTATION_PATH": str(
                    mutation_repositories["OAK"] / "review-mutation.txt"
                ),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps({"1": [], "2": []}),
            },
        )
        self.assertEqual(1, rejected.returncode, rejected.stderr)
        self.assertEqual(
            events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
            rejected_payload["pause_reason"],
        )
        rejected_guard = rejected_payload["turns"][-1]["workspace_write_guard"]
        self.assertFalse(rejected_guard["passed"])
        self.assertEqual(
            "read-only-repository", rejected_guard["violations"][0]["kind"]
        )
        self.assertIn(
            "OAK:review-mutation.txt",
            rejected_guard["violations"][0]["qualified_paths"],
        )
        self.assertIn(
            "- **Status**: pending",
            (mutation_plan / "session-3.md").read_text(encoding="utf-8"),
        )

    def test_rebind_transport_equivalence_and_failure_stages_are_atomic(self) -> None:
        old_workspace, plan_dir, state_dir, _repositories, _unregistered = (
            self._prepare_case("rebind-source")
        )
        before = self._plan_file_bytes(plan_dir)

        wrong_specs = list(DEFAULT_MULTI_REPO_SPECS)
        alias, relative_path, _origin = wrong_specs[0]
        wrong_specs[0] = (alias, relative_path, "https://example.com/wrong/repo.git")
        invalid_workspace = self.tmp_path / "rebind-invalid-origin"
        init_multi_repo_workspace(invalid_workspace, tuple(wrong_specs))
        invalid, invalid_payload = self._rebind_plan(
            plan_dir, invalid_workspace, state_dir
        )
        self.assertEqual(1, invalid.returncode, invalid.stderr)
        self.assertEqual(
            prepare_results.PREPARE_STAGE_WORKSPACE,
            invalid_payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(plan_dir))

        validator_workspace = self.tmp_path / "rebind-validator-failure"
        init_multi_repo_workspace(validator_workspace, DEFAULT_MULTI_REPO_SPECS)
        validator_failed, validator_payload = self._rebind_plan(
            plan_dir,
            validator_workspace,
            state_dir,
            env_updates={"FAKE_CODEX_VALIDATOR_MODE": "failure"},
        )
        self.assertEqual(1, validator_failed.returncode, validator_failed.stderr)
        self.assertEqual(
            prepare_results.PREPARE_STAGE_VALIDATION,
            validator_payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(plan_dir))

        baseline_workspace = self.tmp_path / "rebind-baseline-failure"
        baseline_repositories = init_multi_repo_workspace(
            baseline_workspace, DEFAULT_MULTI_REPO_SPECS
        )
        (baseline_repositories["Kiln"] / "README.md").unlink()
        baseline_failed, baseline_payload = self._rebind_plan(
            plan_dir, baseline_workspace, state_dir
        )
        self.assertEqual(1, baseline_failed.returncode, baseline_failed.stderr)
        self.assertEqual(
            prepare_results.PREPARE_STAGE_BASELINE,
            baseline_payload["workspace_rebind"]["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(plan_dir))

        equivalent_specs = tuple(
            (
                alias,
                relative_path,
                origin.replace("git@github.com:", "https://github.com/"),
            )
            for alias, relative_path, origin in DEFAULT_MULTI_REPO_SPECS
        )
        equivalent_workspace = self.tmp_path / "rebind-equivalent"
        init_multi_repo_workspace(equivalent_workspace, equivalent_specs)
        rebound, rebound_payload = self._rebind_plan(
            plan_dir, equivalent_workspace, state_dir
        )
        self.assertEqual(0, rebound.returncode, rebound.stderr)
        rebind = rebound_payload["workspace_rebind"]
        self.assertEqual(str(old_workspace.resolve()), rebind["old"]["workspace_root"])
        self.assertEqual(
            str(equivalent_workspace.resolve()), rebind["new"]["workspace_root"]
        )
        self.assertIsNone(rebind["failure_stage"])
        metadata = plans_metadata.load_plan_metadata(plan_dir)
        self.assertIsInstance(metadata, plans_metadata.WorkspacePlanMetadata)
        self.assertEqual(str(equivalent_workspace.resolve()), metadata.workspace_root)
        self.assertNotIn("git@", json.dumps(rebind))
