"""Shared four-repository fixture for the multi-repository end-to-end tests.

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
    prepare_models,
    prepare_packet,
)
from tests.asl_e2e_support import (
    run_to_completion,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    init_git_repo,
    init_multi_repo_workspace,
    install_fake_codex,
    install_fake_harness,
)


class MultiRepositoryEndToEndBase(unittest.TestCase):
    """Public-CLI acceptance matrix for a four-repository workspace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.config_home = self.tmp_path / "config"
        self.fake_codex = install_fake_codex(self.tmp_path)
        self.fake_claude = install_fake_harness(self.tmp_path, "claude")
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(self.fake_codex)},
                    "claude": {"executable": str(self.fake_claude)},
                }
            ),
            encoding="utf-8",
        )

    def _env(self, **updates: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "PATH": str(self.tmp_path) + os.pathsep + env.get("PATH", ""),
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
                "FAKE_CODEX_MODE": "success",
                **updates,
            }
        )
        return env

    def _cli(
        self,
        *args: str,
        env_updates: dict[str, str] | None = None,
        budget_seconds: float = 60,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        completed = run_to_completion(
            [sys.executable, "-m", "agent_session_loop", *args],
            cwd=REPO_ROOT,
            env=self._env(**(env_updates or {})),
            budget_seconds=budget_seconds,
            label=f"cli {args[0] if args else ''}".strip(),
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    @staticmethod
    def _source_text(*, first_writes: tuple[str, ...]) -> str:
        registry_rows = "\n".join(
            f"| {alias} | {path} | {origin} |"
            for alias, path, origin in DEFAULT_MULTI_REPO_SPECS
        )
        aliases = ", ".join(alias for alias, _path, _origin in DEFAULT_MULTI_REPO_SPECS)
        writes = ", ".join(first_writes) if first_writes else "(none)"
        return (
            "# Four Repository E2E Plan\n\n"
            "Shared public-CLI acceptance context.\n\n"
            "## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{registry_rows}\n\n"
            "## Session 1: Multi-write implementation\n\n"
            f"**Reads repositories**: {aliases}\n"
            f"**Writes repositories**: {writes}\n\n"
            "Exercise qualified paths across the registered workspace.\n\n"
            "## Session 2: Read-only validation\n\n"
            f"**Reads repositories**: {aliases}\n"
            "**Writes repositories**: (none)\n\n"
            "Validate the completed workspace without mutating repositories.\n"
        )

    @staticmethod
    def _contracts() -> dict[str, Any]:
        first_checks: list[dict[str, Any]] = []
        for number, (alias, relative_path, _origin) in enumerate(
            DEFAULT_MULTI_REPO_SPECS, start=1
        ):
            first_checks.append(
                {
                    "name": f"repo-cwd-{number}",
                    "type": "command",
                    "repo": alias,
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "raise SystemExit(Path.cwd().name != "
                            f"{relative_path!r})"
                        ),
                    ],
                    "timeout_seconds": 30,
                    "baseline_expectation": "pass",
                }
            )
        return {
            "1": {
                "done_when": ["All registered command checks pass."],
                "verify": {"session": "1", "checks": first_checks},
            },
            "2": {
                "done_when": ["Read-only repository evidence is valid."],
                "verify": {
                    "session": "2",
                    "checks": [
                        {
                            "name": "kiln-readme",
                            "type": "file_exists",
                            "repo": "Kiln",
                            "path": "README.md",
                            "baseline_expectation": "pass",
                        }
                    ],
                },
            },
        }

    def _validation_payload(self, plan_dir: Path, workspace: Path) -> dict:
        packet = prepare_packet.build_validation_packet(
            plan_dir,
            ("1", "2"),
            codebase_root=workspace,
        )
        sessions = []
        for session_id, alias in (("1", "SVC"), ("2", "Kiln")):
            sessions.append(
                {
                    "session_id": session_id,
                    "title": (
                        "Multi-write implementation"
                        if session_id == "1"
                        else "Read-only validation"
                    ),
                    "claims": [
                        {
                            "claim": "README.md exists",
                            "claim_type": "P",
                            "location": "README.md",
                            "line": 1,
                            "to_be_created": False,
                            "repo": alias,
                        }
                    ],
                    "issues": [],
                    "details": [],
                    "patterns": [],
                    "dependencies": [],
                    "contract_checked": True,
                    "contract_problems": [],
                    "corrections": [],
                }
            )
        return {
            "version": prepare_models.PREPARE_VALIDATION_VERSION,
            "workspace_root": str(workspace.resolve()),
            "repositories": [repo.to_dict() for repo in packet.repositories],
            "sessions": sessions,
        }

    def _prepare_case(
        self,
        name: str,
        *,
        first_writes: tuple[str, ...] = ("SVC", "OAK", "OWL"),
        dirty_registered: bool = False,
        dirty_unregistered: bool = False,
    ) -> tuple[Path, Path, Path, dict[str, Path], Path]:
        case_dir = self.tmp_path / name
        case_dir.mkdir()
        workspace = case_dir / "workspace"
        repositories = init_multi_repo_workspace(workspace, DEFAULT_MULTI_REPO_SPECS)
        unregistered = workspace / "unregistered"
        init_git_repo(unregistered)
        if dirty_registered:
            (repositories["OAK"] / "preexisting.txt").write_text(
                "registered dirty state\n", encoding="utf-8"
            )
        if dirty_unregistered:
            (unregistered / "scratch.txt").write_text(
                "unregistered dirty state\n", encoding="utf-8"
            )
        self.assertFalse((workspace / ".git").exists())

        source = case_dir / "plan.md"
        source.write_text(
            self._source_text(first_writes=first_writes), encoding="utf-8"
        )
        contracts_path = case_dir / "contracts.json"
        contracts_path.write_text(json.dumps(self._contracts()), encoding="utf-8")
        split, split_payload = self._cli(
            "split",
            "--plan-file",
            str(source),
            "--contracts-file",
            str(contracts_path),
            "--base-dir",
            str(case_dir / "plans"),
            "--skill-prefix",
            "codex",
            "--json",
        )
        self.assertEqual(0, split.returncode, split.stderr or split.stdout)
        self.assertEqual(
            ["SVC", "OAK", "OWL", "Kiln"],
            [row["alias"] for row in split_payload["repositories"]],
        )
        plan_dir = Path(str(split_payload["output_dir"]))

        batch_path = case_dir / "validation.json"
        batch_path.write_text(
            json.dumps(self._validation_payload(plan_dir, workspace)),
            encoding="utf-8",
        )
        ingest, ingest_payload = self._cli(
            "expand-ingest",
            str(plan_dir),
            "--batch-file",
            str(batch_path),
            "--json",
        )
        self.assertEqual(0, ingest.returncode, ingest.stderr or ingest.stdout)
        self.assertEqual(["1", "2"], ingest_payload["sessions"])

        state_dir = case_dir / "state"
        prepared, prepared_payload = self._cli(
            "prepare",
            str(plan_dir),
            "--resume",
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--validator-harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            budget_seconds=120,
        )
        self.assertEqual(0, prepared.returncode, prepared.stderr or prepared.stdout)
        self.assertTrue(prepared_payload["marked_loop_ready"])
        self.assertTrue((plan_dir / "session-1.expand.md").is_file())
        self.assertTrue((plan_dir / "session-2.expand.md").is_file())
        return workspace, plan_dir, state_dir, repositories, unregistered

    def _run_plan(
        self,
        plan_dir: Path,
        workspace: Path,
        state_dir: Path,
        *extra: str,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        return self._cli(
            "run",
            str(plan_dir),
            "--cwd",
            str(workspace),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            *extra,
            env_updates=env_updates,
            budget_seconds=120,
        )

    @staticmethod
    def _plan_file_bytes(plan_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(plan_dir).as_posix(): path.read_bytes()
            for path in sorted(plan_dir.rglob("*"))
            if path.is_file()
        }

    def _rebind_plan(
        self,
        plan_dir: Path,
        workspace: Path,
        state_dir: Path,
        *,
        env_updates: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        return self._cli(
            "prepare",
            str(plan_dir),
            "--resume",
            "--cwd",
            str(workspace),
            "--rebind-workspace",
            "--harness",
            "codex",
            "--validator-harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
            env_updates=env_updates,
            budget_seconds=120,
        )
