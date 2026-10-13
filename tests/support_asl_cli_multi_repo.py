"""Shared workspace plan and fake-harness helpers for the multi-repo CLI tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from typing import TYPE_CHECKING

from agent_session_loop import config, plans_metadata, plans_registry
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    init_multi_repo_workspace,
    install_fake_codex,
    write_multi_repo_plan,
)

if TYPE_CHECKING:
    _Base = unittest.TestCase
else:
    _Base = object


class MultiRepoRunnerHelpers(_Base):
    """Shared workspace plan and fake-harness helpers for the multi-repo CLI tests."""

    def _workspace_plan(self, tmp_dir: Path) -> tuple[Path, Path, dict[str, Path]]:
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
        session_text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            session_text.replace(
                "**Reads repositories**: SVC, OAK\n**Writes repositories**: SVC, OAK",
                "**Reads repositories**: SVC\n**Writes repositories**: SVC",
            ),
            encoding="utf-8",
        )
        registry = plans_registry.read_repository_registry(plan_dir)
        assert registry is not None
        plans_metadata.save_workspace_plan_metadata(
            plan_dir,
            workspace_root,
            registry.digest,
        )
        return workspace_root, plan_dir, repositories

    def _run(
        self,
        plan_dir: Path,
        workspace_root: Path,
        state_dir: Path,
        *extra: str,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env.update(env_overrides or {})
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "run",
                str(plan_dir),
                "--cwd",
                str(workspace_root),
                "--state-dir",
                str(state_dir),
                "--json",
                *extra,
            ],
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def _fake_codex_env(self, tmp_dir: Path, **extra: str) -> dict[str, str]:
        fake_codex = install_fake_codex(tmp_dir)
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        return {
            "XDG_CONFIG_HOME": str(config_home),
            "FAKE_CODEX_MODE": "success",
            **extra,
        }

    def _fake_review_env(self, tmp_dir: Path, **extra: str) -> dict[str, str]:
        env = self._fake_codex_env(tmp_dir, **extra)
        config_path = (
            Path(env["XDG_CONFIG_HOME"]) / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["claude"] = dict(payload["codex"])
        config_path.write_text(json.dumps(payload), encoding="utf-8")
        return env
