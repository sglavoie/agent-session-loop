"""Multi-repository commit selection and partial-commit recovery tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    plans_handoff,
    plans_metadata,
    plans_models,
    plans_registry,
    plans_session_ids,
    review_repair,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _contract_block,
    git_stdout,
    init_multi_repo_workspace,
    read_jsonl,
    write_multi_repo_plan,
)
from tests.support_asl_cli_multi_repo import MultiRepoRunnerHelpers


class MultiRepoCommitRecoveryTests(MultiRepoRunnerHelpers, unittest.TestCase):
    """Multi-repository commit selection and partial-commit recovery tests."""

    def test_multi_repo_real_turn_allows_only_write_alias_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            target = repositories["SVC"] / "authorized.txt"
            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                env_overrides=self._fake_codex_env(
                    tmp_dir,
                    FAKE_CODEX_MUTATION_PATH=str(target),
                ),
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            guard = payload["turns"][0]["workspace_write_guard"]
            self.assertTrue(guard["passed"])
            self.assertEqual(["SVC"], guard["authorized_aliases"])
            self.assertEqual(
                ["SVC:authorized.txt"],
                guard["changed"]["tundra_svc"]["qualified_paths"],
            )
            self.assertEqual(
                "complete",
                plans_handoff.read_session_status(
                    plans_models.SessionFile(
                        session_id=plans_session_ids.SessionId.parse("1"),
                        path=plan_dir / "session-1.md",
                    )
                ).status,
            )

    def test_multi_repo_per_session_commit_uses_manifest_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            commit_log = tmp_dir / "fake-commit.jsonl"
            mutations = [
                {"path": str(repositories[alias] / "changed.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]
            env = self._fake_codex_env(
                tmp_dir,
                FAKE_CODEX_COMMIT_LOG=str(commit_log),
                FAKE_CODEX_MUTATIONS_JSON=json.dumps(mutations),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                ["SVC", "OAK"],
                [commit["repository_alias"] for commit in payload["commits"]],
            )
            group = payload["commit_groups"][0]
            self.assertEqual("complete", group["status"])
            self.assertEqual(["SVC", "OAK"], group["ordered_aliases"])
            self.assertEqual(
                ["SVC", "OAK"],
                [row["alias"] for row in group["successful_repositories"]],
            )
            records = read_jsonl(commit_log)
            self.assertEqual(
                [
                    str(repositories["SVC"].resolve()),
                    str(repositories["OAK"].resolve()),
                ],
                [record["cwd"] for record in records],
            )

    def test_multi_repo_end_commit_selects_all_changed_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8")
                .replace(
                    "**Reads repositories**: SVC",
                    "**Reads repositories**: SVC, OAK",
                )
                .replace(
                    "**Writes repositories**: SVC",
                    "**Writes repositories**: SVC, OAK",
                ),
                encoding="utf-8",
            )
            mutations = [
                {"path": str(repositories[alias] / "end.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]
            env = self._fake_codex_env(
                tmp_dir,
                FAKE_CODEX_MUTATIONS_JSON=json.dumps(mutations),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-at-end",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                ["SVC", "OAK"],
                [commit["repository_alias"] for commit in payload["commits"]],
            )
            group = payload["commit_groups"][0]
            self.assertEqual("end", group["scope"])
            self.assertIsNone(group["session_id"])
            self.assertEqual(["SVC", "OAK"], group["ordered_aliases"])

    def test_multi_repo_per_session_commit_skips_unchanged_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, _repositories = self._workspace_plan(tmp_dir)
            commit_log = tmp_dir / "fake-commit.jsonl"
            env = self._fake_codex_env(
                tmp_dir,
                FAKE_CODEX_COMMIT_LOG=str(commit_log),
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual([], payload["commits"])
            self.assertEqual([], payload["commit_groups"][0]["ordered_aliases"])
            self.assertEqual("complete", payload["commit_groups"][0]["status"])
            self.assertFalse(commit_log.exists())

    def test_multi_repo_per_session_commit_skips_harness_committed_head(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            commit_log = tmp_dir / "fake-commit.jsonl"
            head_before = git_stdout(repositories["SVC"], "rev-parse", "HEAD")
            env = self._fake_codex_env(
                tmp_dir,
                FAKE_CODEX_COMMIT_LOG=str(commit_log),
                FAKE_CODEX_MUTATION_PATH=str(repositories["SVC"]),
                FAKE_CODEX_MUTATION_ACTION="commit-head",
            )

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertNotEqual(
                head_before,
                git_stdout(repositories["SVC"], "rev-parse", "HEAD"),
            )
            self.assertEqual([], payload["commits"])
            self.assertEqual([], payload["commit_groups"][0]["ordered_aliases"])
            self.assertEqual("complete", payload["commit_groups"][0]["status"])
            self.assertFalse(commit_log.exists())

    def test_multi_repo_commit_preflight_requires_a_needed_repository_clean(
        self,
    ) -> None:
        """Dirt in a repository the session declares stops the run before a turn.

        The dirty repository is SVC because SVC is what this fixture's session
        reads and writes. This test used to dirty OAK and assert the same
        refusal, and it stopped asserting anything once per-session commits
        narrowed the gate to the aliases the next session needs: OAK is outside
        that scope, so the run went on and the returncode assertion was the only
        thing left holding the test up.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            (repositories["SVC"] / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            env = self._fake_codex_env(tmp_dir)

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(1, completed.returncode)
            payload = json.loads(completed.stdout)
            self.assertIn(
                payload["pause_reason"], {"not-loop-ready", "commit-preflight"}
            )
            self.assertIn("SVC", json.dumps(payload["pause_details"]))
            self.assertEqual([], payload["turns"])

    def test_multi_repo_commit_preflight_leaves_dirt_outside_the_scope_alone(
        self,
    ) -> None:
        """Dirt the next session never touches is not this gate's business.

        The complement of the refusal above, end to end rather than at the
        function: per-session commits only commit what the session declared, so
        a repository the session neither reads nor writes may be dirty without
        stopping a run that will not touch it. What the run must not do is
        quietly fold that dirt into a commit of its own.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            workspace_root, plan_dir, repositories = self._workspace_plan(tmp_dir)
            (repositories["OAK"] / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            head_before = git_stdout(repositories["OAK"], "rev-parse", "HEAD")
            env = self._fake_codex_env(tmp_dir)

            completed = self._run(
                plan_dir,
                workspace_root,
                tmp_dir / "state",
                "--commit-per-session",
                env_overrides=env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertIsNone(payload["pause_reason"])
            self.assertEqual(1, len(payload["turns"]))
            self.assertEqual([], payload["commits"])
            self.assertEqual(
                head_before, git_stdout(repositories["OAK"], "rev-parse", "HEAD")
            )
            self.assertIn(
                "dirty.txt",
                git_stdout(repositories["OAK"], "status", "--porcelain"),
            )

    def test_multi_repo_partial_commit_recovers_before_next_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_dir / "workspace"
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_dir / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session="1",
            )
            contract_2 = _contract_block(
                '{"type": "file_exists", "repo": "SVC", '
                '"path": "README.md", "name": "svc-readme"}',
                session="2",
            )
            write_multi_repo_plan(
                plan_dir,
                ("pending", "pending"),
                specs=specs,
                contracts_by_id={"1": contract, "2": contract_2},
            )
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir, workspace_root, registry.digest
            )
            commit_log = tmp_dir / "fake-commit.jsonl"
            state_dir = tmp_dir / "state"
            base_env = self._fake_codex_env(
                tmp_dir,
                FAKE_CODEX_COMMIT_LOG=str(commit_log),
            )
            mutations = [
                {"path": str(repositories[alias] / "changed.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]
            # `--max-sessions 1` ends the first run while the group is still
            # owed; without it the run would retry the group itself and the
            # second process would have nothing to recover.
            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                "--commit-per-session",
                "--max-sessions",
                "1",
                env_overrides={
                    **base_env,
                    "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                    "FAKE_CODEX_COMMIT_FAIL_CWD_BASENAME": repositories["OAK"].name,
                },
            )

            self.assertEqual(0, first.returncode, first.stderr)
            first_payload = json.loads(first.stdout)
            self.assertFalse(first_payload["paused"])
            first_group = first_payload["commit_groups"][0]
            self.assertEqual("failed", first_group["status"])
            self.assertEqual("OAK", first_group["failed_alias"])
            self.assertEqual(["OAK"], first_group["remaining_aliases"])
            self.assertEqual(
                ["OAK"],
                [
                    summary["failed_alias"]
                    for summary in first_payload["unresolved_commit_groups"]
                ],
            )

            second = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                "--resume",
                env_overrides=base_env,
            )

            self.assertEqual(0, second.returncode, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertTrue(second_payload["commit_group_recovery"]["success"])
            recovered = second_payload["commit_group_recovery"]["group"]
            self.assertEqual("complete", recovered["status"])
            records = read_jsonl(commit_log)
            # SVC committed once; OAK refused twice in the first run — the
            # commit itself and the in-run retry — and landed on the third
            # turn, the one the second process ran before anything else.
            self.assertEqual(
                [
                    repositories["SVC"].name,
                    repositories["OAK"].name,
                    repositories["OAK"].name,
                    repositories["OAK"].name,
                ],
                [Path(record["cwd"]).name for record in records],
            )

    def test_partial_repair_commit_recovers_under_null_commit_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            specs = DEFAULT_MULTI_REPO_SPECS[:2]
            workspace_root = tmp_dir / "workspace"
            repositories = init_multi_repo_workspace(workspace_root, specs)
            plan_dir = tmp_dir / "plan"
            write_multi_repo_plan(
                plan_dir,
                ("pending",),
                specs=specs,
                contracts_by_id={
                    "1": _contract_block(
                        '{"type": "file_exists", "repo": "SVC", '
                        '"path": "README.md", "name": "svc-readme"}',
                        session="1",
                    )
                },
            )
            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                review_repair.REVIEW_REPAIR_SESSION_MARKER
                + "\n"
                + review_repair.review_repair_session_version_marker()
                + "\n"
                + session_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            registry = plans_registry.read_repository_registry(plan_dir)
            assert registry is not None
            plans_metadata.save_workspace_plan_metadata(
                plan_dir, workspace_root, registry.digest
            )
            commit_log = tmp_dir / "fake-commit.jsonl"
            state_dir = tmp_dir / "state"
            base_env = self._fake_codex_env(
                tmp_dir,
                FAKE_CODEX_COMMIT_LOG=str(commit_log),
            )
            mutations = [
                {"path": str(repositories[alias] / "changed.txt"), "text": alias}
                for alias in ("SVC", "OAK")
            ]

            first = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                env_overrides={
                    **base_env,
                    "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                    "FAKE_CODEX_COMMIT_FAIL_CWD_BASENAME": repositories["OAK"].name,
                },
            )

            self.assertEqual(0, first.returncode, first.stderr)
            first_payload = json.loads(first.stdout)
            self.assertIsNone(first_payload["commit_mode"])
            self.assertEqual("per-child", first_payload["repair_commit_mode"])
            self.assertFalse(first_payload["paused"])
            self.assertEqual(
                ["OAK"], first_payload["commit_groups"][0]["remaining_aliases"]
            )
            self.assertEqual(
                ["OAK"],
                [
                    summary["failed_alias"]
                    for summary in first_payload["unresolved_commit_groups"]
                ],
            )

            second = self._run(
                plan_dir,
                workspace_root,
                state_dir,
                "--resume",
                env_overrides=base_env,
            )

            self.assertEqual(0, second.returncode, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertTrue(second_payload["commit_group_recovery"]["success"])
            self.assertEqual(
                "complete", second_payload["commit_group_recovery"]["group"]["status"]
            )


if __name__ == "__main__":
    unittest.main()
