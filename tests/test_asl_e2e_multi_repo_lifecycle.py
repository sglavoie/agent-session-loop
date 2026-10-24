"""Four-repository end-to-end lifecycle and workspace guard tests.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json
from pathlib import Path

from agent_session_loop import (
    events,
)
from tests.asl_e2e_multi_repo_support import MultiRepositoryEndToEndBase
from tests.support import (
    git_stdout,
)

ASL_TEST_LANE = "integration"


class MultiRepositoryEndToEndQATests(MultiRepositoryEndToEndBase):
    """Core lifecycle and dirty-workspace matrix for four repositories."""

    def test_four_repository_public_core_lifecycle(self) -> None:
        workspace, plan_dir, state_dir, repositories, unregistered = self._prepare_case(
            "core", dirty_unregistered=True
        )

        baseline_completed, baseline_payload = self._cli(
            "baseline",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--json",
        )
        self.assertEqual(0, baseline_completed.returncode, baseline_completed.stderr)
        self.assertEqual(5, baseline_payload["check_count"])
        self.assertEqual(
            ["SVC", "OAK", "OWL", "Kiln", "Kiln"],
            [check["repo"] for check in baseline_payload["checks"]],
        )

        preflight_completed, preflight_payload = self._cli(
            "preflight",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
        )
        self.assertEqual(0, preflight_completed.returncode, preflight_completed.stderr)
        self.assertTrue(preflight_payload["ok"])

        dry_completed, dry_payload = self._run_plan(
            plan_dir, workspace, state_dir, "--dry-run"
        )
        self.assertEqual(0, dry_completed.returncode, dry_completed.stderr)
        self.assertEqual(str(workspace.resolve()), dry_payload["workspace_root"])
        self.assertIn(
            "Writable aliases: SVC, OAK, OWL", dry_payload["planned"]["prompt"]
        )

        mutations = [
            {
                "path": str(repositories[alias] / "session-1.txt"),
                "text": f"{alias}\n",
            }
            for alias in ("SVC", "OAK", "OWL")
        ]
        run_completed, run_payload = self._run_plan(
            plan_dir,
            workspace,
            state_dir,
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(mutations),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {
                        "1": [
                            "SVC:session-1.txt",
                            "OAK:session-1.txt",
                            "OWL:session-1.txt",
                        ],
                        "2": [],
                    }
                ),
            },
        )
        self.assertEqual(
            0, run_completed.returncode, run_completed.stderr or run_completed.stdout
        )
        self.assertEqual("all-complete", run_payload["reason"])
        self.assertEqual([1, 2], run_payload["completed_sessions"])
        self.assertTrue(run_payload["turns"][0]["workspace_write_guard"]["passed"])
        self.assertEqual(
            ["SVC", "OAK", "OWL"],
            run_payload["turns"][0]["workspace_write_guard"]["authorized_aliases"],
        )
        read_only_guard = run_payload["turns"][1]["workspace_write_guard"]
        self.assertTrue(read_only_guard["passed"])
        self.assertEqual([], read_only_guard["write_aliases"])
        self.assertTrue((unregistered / "scratch.txt").is_file())

        for session_id in ("1", "2"):
            verified, verify_payload = self._cli(
                "verify",
                str(plan_dir),
                "--session",
                session_id,
                "--cwd",
                str(workspace),
                "--json",
            )
            self.assertEqual(0, verified.returncode, verified.stderr)
            self.assertTrue(verify_payload["passed"])

    def test_dirty_registered_preflight_and_workspace_guard_matrix(self) -> None:
        workspace, plan_dir, state_dir, _repositories, unregistered = (
            self._prepare_case(
                "dirty-preflight",
                dirty_registered=True,
                dirty_unregistered=True,
            )
        )
        clean_mode, clean_payload = self._cli(
            "preflight",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
        )
        self.assertEqual(0, clean_mode.returncode, clean_mode.stderr)
        self.assertTrue(clean_payload["ok"])

        commit_mode, commit_payload = self._cli(
            "preflight",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--commit-per-session",
            "--state-dir",
            str(state_dir),
            "--json",
        )
        self.assertEqual(1, commit_mode.returncode)
        dirty_messages = "\n".join(
            finding["message"]
            for finding in commit_payload["findings"]
            if finding["category"] == "env-git"
        )
        self.assertIn("OAK", dirty_messages)
        self.assertNotIn(unregistered.name, dirty_messages)

        mutation_cases = (
            (
                "tracked",
                lambda repos, _workspace, _unregistered: {
                    "path": str(repos["OAK"] / "README.md"),
                    "text": "tracked unauthorized\n",
                },
                "OAK:README.md",
            ),
            (
                "staged",
                lambda repos, _workspace, _unregistered: {
                    "path": str(repos["OAK"] / "staged.txt"),
                    "action": "stage",
                },
                "OAK:staged.txt",
            ),
            (
                "untracked",
                lambda repos, _workspace, _unregistered: {
                    "path": str(repos["OAK"] / "untracked.txt"),
                },
                "OAK:untracked.txt",
            ),
            (
                "repository-removed",
                lambda repos, case_workspace, _unregistered: {
                    "path": str(repos["Kiln"]),
                    "action": "rename",
                    "destination": str(case_workspace.parent / "retained"),
                },
                "repository-removed",
            ),
        )
        for case_name, mutation_factory, expected in mutation_cases:
            with self.subTest(case=case_name):
                (
                    case_workspace,
                    case_plan,
                    case_state,
                    case_repositories,
                    case_unregistered,
                ) = self._prepare_case(f"guard-{case_name}", first_writes=("SVC",))
                mutation = mutation_factory(
                    case_repositories, case_workspace, case_unregistered
                )
                completed, payload = self._run_plan(
                    case_plan,
                    case_workspace,
                    case_state,
                    "--max-sessions",
                    "1",
                    env_updates={"FAKE_CODEX_MUTATIONS_JSON": json.dumps([mutation])},
                )
                self.assertEqual(1, completed.returncode, completed.stderr)
                self.assertEqual(
                    events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                    payload["pause_reason"],
                )
                guard = payload["turns"][0]["workspace_write_guard"]
                self.assertFalse(guard["passed"])
                if expected.startswith("OAK:"):
                    self.assertEqual(
                        "read-only-repository", guard["violations"][0]["kind"]
                    )
                    self.assertIn(expected, guard["violations"][0]["qualified_paths"])
                    status = git_stdout(
                        case_repositories["OAK"], "status", "--porcelain"
                    )
                    self.assertIn(Path(str(mutation["path"])).name, status)
                else:
                    self.assertEqual(expected, guard["violations"][0]["kind"])
                    retained = Path(str(mutation["destination"]))
                    self.assertTrue((retained / ".git").is_dir())
                self.assertIn(
                    "- **Status**: pending",
                    (case_plan / "session-1.md").read_text(encoding="utf-8"),
                )
