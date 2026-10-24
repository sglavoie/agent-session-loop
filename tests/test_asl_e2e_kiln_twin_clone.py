"""End-to-end sibling-clone refusal regression tests.

Split from tests/test_asl_e2e.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    config,
    events,
    plans_content_hash,
    plans_files,
    plans_metadata,
    plans_models,
    plans_readiness,
    plans_registry,
)
from tests.asl_e2e_support import (
    run_to_completion,
)
from tests.support import (
    KILN_TWIN_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    git_stdout,
    init_multi_repo_workspace,
    install_fake_codex,
    write_multi_repo_plan,
    write_plan,
)

ASL_TEST_LANE = "integration"


class KilnTwinCloneRegressionTests(unittest.TestCase):
    """The original failure: a legacy plan reaching a same-origin sibling.

    ``tundra_Kiln`` and ``tundra_Kiln_testing`` are two checkouts of
    one remote sitting side by side. ``workspaces.origins_equal`` holds between
    them, so origin comparison cannot tell them apart; only the resolved Git
    top-level can. These tests pin that the refusal rests on the resolved root,
    that it survives a loop-ready plan whose readiness skips core preflight, and
    that a registry naming both clones still executes.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.config_home = self.tmp_path / "config"
        fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        self.workspace = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, KILN_TWIN_REPO_SPECS
        )
        self.kiln = self.repositories["Kiln"]
        self.kiln_testing = self.repositories["KilnTesting"]
        self.state_dir = self.tmp_path / "state"

    def _run(
        self,
        plan_dir: Path,
        cwd: Path,
        *extra: str,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                "FAKE_CODEX_MODE": "success",
                **(env_updates or {}),
            }
        )
        completed = run_to_completion(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "run",
                str(plan_dir),
                "--cwd",
                str(cwd),
                "--harness",
                "codex",
                "--state-dir",
                str(self.state_dir),
                "--json",
                *extra,
            ],
            cwd=REPO_ROOT,
            env=env,
            budget_seconds=120,
            label="run",
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    def _mark_loop_ready(self, plan_dir: Path) -> None:
        plans_readiness.write_readiness_metadata(
            plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _assert_clones_untouched(self, *markers: Path) -> None:
        for marker in markers:
            self.assertFalse(marker.exists(), f"{marker} was written")
        for repository in (self.kiln, self.kiln_testing):
            self.assertEqual(
                "",
                git_stdout(repository, "status", "--porcelain"),
                f"{repository.name} was mutated",
            )

    def _sibling_mutations(self) -> list[dict[str, str]]:
        return [
            {"path": str(self.kiln / "harness-ran.txt"), "text": "kiln\n"},
            {
                "path": str(self.kiln_testing / "harness-ran.txt"),
                "text": "testing\n",
            },
        ]

    def test_legacy_plan_declaring_the_sibling_clone_launches_no_harness(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "legacy-plan"
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(self.kiln_testing / "README.md"),
                            "name": "sibling-readme",
                        }
                    ),
                    session="1",
                )
            },
        )
        self._mark_loop_ready(plan_dir)

        # ``--allow-not-loop-ready`` waives readiness, which is the other gate
        # that could refuse this run. What is left is the hard gate.
        completed, payload = self._run(
            plan_dir,
            self.kiln,
            "--allow-not-loop-ready",
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(self._sibling_mutations())
            },
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        report = payload["pause_details"]["repository_intent"]
        self.assertEqual(2, report["git_root_count"])
        self.assertEqual(
            sorted(
                str(plans_files.canonicalize_path(path))
                for path in (self.kiln, self.kiln_testing)
            ),
            sorted(report["git_roots"]),
        )
        self._assert_clones_untouched(
            *(Path(mutation["path"]) for mutation in self._sibling_mutations())
        )

    def test_expansion_note_redirect_to_the_sibling_clone_launches_no_harness(
        self,
    ) -> None:
        # The plan-level scan skips expansion notes, so a redirect that lives
        # only there reaches the worker through the task packet. The per-turn
        # recheck is the only surface that sees it.
        plan_dir = self.tmp_path / "expansion-plan"
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": "README.md",
                            "name": "kiln-readme",
                        }
                    ),
                    session="1",
                )
            },
        )
        (plan_dir / "session-1.expand.md").write_text(
            "# Session 1 expansion\n\n"
            "The integration fixture actually lives in the testing checkout: "
            f"write it to {self.kiln_testing}/src/fixture.py.\n",
            encoding="utf-8",
        )
        self._mark_loop_ready(plan_dir)

        completed, payload = self._run(
            plan_dir,
            self.kiln,
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(self._sibling_mutations())
            },
        )

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        self.assertEqual([], payload["turns"])
        self.assertEqual(
            2, payload["pause_details"]["repository_intent"]["git_root_count"]
        )
        self._assert_clones_untouched(
            *(Path(mutation["path"]) for mutation in self._sibling_mutations())
        )

    def test_refusal_diagnostics_name_roots_without_leaking_credentials(
        self,
    ) -> None:
        secret = "s3cr3t-clone-token"
        plan_dir = self.tmp_path / "credential-plan"
        write_plan(
            plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "path": str(self.kiln_testing / "README.md"),
                            "name": "sibling-readme",
                        }
                    ),
                    session="1",
                )
            },
        )
        session_path = plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "Instructions.",
                "Instructions. The testing checkout came from "
                f"https://kiln-bot:{secret}@github.com/tundra/Kiln.git.",
            ),
            encoding="utf-8",
        )
        self._mark_loop_ready(plan_dir)

        completed, payload = self._run(plan_dir, self.kiln)

        self.assertEqual(1, completed.returncode, completed.stderr)
        self.assertEqual(
            events.PAUSE_REASON_REPOSITORY_REGISTRY_REQUIRED,
            payload["pause_reason"],
        )
        # The diagnostic identifies the conflict by filesystem root, so nothing
        # it carries can echo a remote URL back to the operator.
        self.assertNotIn(secret, completed.stdout)
        self.assertNotIn(secret, completed.stderr)
        self.assertIn(
            str(plans_files.canonicalize_path(self.kiln_testing)),
            payload["pause_details"]["repository_intent"]["git_roots"],
        )

    def test_registered_workspace_targeting_the_testing_clone_succeeds(
        self,
    ) -> None:
        plan_dir = self.tmp_path / "registered-plan"
        write_multi_repo_plan(
            plan_dir,
            ("pending",),
            specs=KILN_TWIN_REPO_SPECS,
            contracts_by_id={
                "1": _contract_block(
                    json.dumps(
                        {
                            "type": "file_exists",
                            "repo": "KilnTesting",
                            "path": "fixture.py",
                            "name": "testing-fixture",
                        }
                    ),
                    session="1",
                )
            },
        )
        session_path = plan_dir / "session-1.md"
        session_path.write_text(
            session_path.read_text(encoding="utf-8").replace(
                "**Writes repositories**: Kiln, KilnTesting",
                "**Writes repositories**: KilnTesting",
            ),
            encoding="utf-8",
        )
        registry = plans_registry.read_repository_registry(plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            plan_dir, self.workspace, registry.digest
        )
        fixture = self.kiln_testing / "fixture.py"

        completed, payload = self._run(
            plan_dir,
            self.workspace,
            env_updates={
                "FAKE_CODEX_MUTATIONS_JSON": json.dumps(
                    [{"path": str(fixture), "text": "fixture = True\n"}]
                ),
                "FAKE_CODEX_HANDOFF_FILES_JSON": json.dumps(
                    {"1": ["KilnTesting:fixture.py"]}
                ),
            },
        )

        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        guard = payload["turns"][0]["workspace_write_guard"]
        self.assertTrue(guard["passed"])
        self.assertEqual(["KilnTesting"], guard["authorized_aliases"])
        self.assertTrue(fixture.is_file())
        self.assertEqual("", git_stdout(self.kiln, "status", "--porcelain"))
