"""Shared fixture for the run readiness gate tests."""

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
    plans_models,
    plans_readiness,
    state,
)
from tests.support import (
    REPO_ROOT,
    _command_contract,
    _contract_block,
    init_git_repo,
    install_fake_codex,
    read_jsonl,
    write_plan,
)


class RunReadinessGateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...] = ("pending",),
        *,
        readiness: str | None = "loop-ready",
        sha_override: str | None = None,
        contracts_by_id: dict[str, str] | None = None,
    ) -> plans_models.ReadinessMetadata | None:
        contracts = contracts_by_id or {
            str(index): _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{index}"}}',
                session=str(index),
            )
            for index in range(1, len(statuses) + 1)
        }
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts)
        if readiness is None:
            return None
        sha = sha_override or plans_content_hash.stable_readiness_sha256(self.plan_dir)
        metadata = plans_models.ReadinessMetadata(
            state=readiness,
            readiness_sha256=sha,
            checked_at="2026-06-19T00:00:00Z",
        )
        plans_readiness.write_readiness_metadata(self.plan_dir, metadata)
        return metadata

    def _run(
        self,
        *,
        mode: str = "success",
        max_sessions: int | None = None,
        max_pending_sessions: int | None = None,
        allow_not_loop_ready: bool = False,
        turn_timeout: int | None = None,
        env_overrides: dict[str, str] | None = None,
        process_timeout: int = 20,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if max_sessions is not None:
            argv.extend(["--max-sessions", str(max_sessions)])
        if max_pending_sessions is not None:
            argv.extend(["--max-pending-sessions", str(max_pending_sessions)])
        if turn_timeout is not None:
            argv.extend(["--timeout", str(turn_timeout)])
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        env.update(env_overrides or {})
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=process_timeout,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _add_origin(self, repo: Path, origin: str) -> None:
        subprocess.run(
            ["git", "remote", "add", "origin", origin],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        )

    def _plan_dir_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in sorted(self.plan_dir.rglob("*"))
            if path.is_file()
        }

    def _write_cross_repo_plan(self, statuses: tuple[str, ...] = ("pending",)) -> Path:
        """A legacy plan whose session 1 check lives in a second repository."""
        other_repo = self.tmp_path / "other"
        init_git_repo(other_repo)
        contracts_by_id = {
            "1": _contract_block(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": str(other_repo / "README.md"),
                        "name": "cross-repo",
                    }
                ),
                session="1",
            )
        }
        for index in range(2, len(statuses) + 1):
            contracts_by_id[str(index)] = _contract_block(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": "README.md",
                        "name": f"readme-{index}",
                    }
                ),
                session=str(index),
            )
        self._write_plan(statuses, contracts_by_id=contracts_by_id)
        return other_repo

    def _preflight_skipped_events(self) -> list[dict[str, Any]]:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        return [
            record
            for record in read_jsonl(event_path)
            if record["name"] == events.PREFLIGHT_SKIPPED_EVENT
        ]

    _UNRUNNABLE_EXECUTABLE = "asl-missing-executable-for-probe-regression"

    def _write_plan_with_unrunnable_probe(
        self, *, readiness: str, sha_override: str | None = None
    ) -> None:
        """Write a 2-session plan whose second contract fails command probes.

        Only the full preflight probes command runnability, and the run never
        reaches session 2, so the finding appears if and only if the core
        preflight ran.
        """
        self._write_plan(
            ("pending", "pending"),
            readiness=readiness,
            sha_override=sha_override,
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                ),
                "2": _contract_block(
                    '{"type": "command", "argv": '
                    f'["{self._UNRUNNABLE_EXECUTABLE}"], "name": "probe-2"}}',
                    session="2",
                ),
            },
        )

    def _command_runnability_findings(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [
            finding
            for finding in payload["readiness_gate"]["findings"]
            if finding["category"] == "command-not-runnable"
        ]

    def _declare_source(self, source: Path, *, expansion_cites: str) -> None:
        """Point the manifest at ``source`` and cite a path in an expansion note.

        Readiness is rewritten last: both edits change the artifact digests the
        readiness SHA covers.
        """
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"\n**Split from**: `{source}`\n",
            encoding="utf-8",
        )
        (self.plan_dir / "session-1.expand.md").write_text(
            f"# Session 1 Expansion\n\n- Restated from {expansion_cites}\n",
            encoding="utf-8",
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _write_baseline_gated_plan(self) -> None:
        contract = _command_contract('["true"]', session="1", expectation="pass")
        write_plan(self.plan_dir, ("pending",), contracts_by_id={"1": contract})
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
