"""Shared fixtures and helpers for the preflight test modules."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    baseline,
    baseline_cache,
    contracts,
    plans,
    preflight,
    preflight_models,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    REPO_ROOT,
    _contract_block,
    _write_loop_ready_plan,
    init_git_repo,
    init_multi_repo_workspace,
)


class PreflightCoreFixtures(unittest.TestCase):
    def _progress_findings(
        self,
        report: preflight_models.PreflightReport,
    ) -> list[preflight_models.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "progress-journal-missing"
        ]

    def _progress_entry(
        self,
        session_id: str,
        *,
        status: str = "complete",
    ) -> plans.ProgressEntry:
        return plans.ProgressEntry(
            session_id=plans.parse_session_id(session_id),
            status=status,
            completed_at="2026-06-19T15:00:00Z",
            files_modified=(),
            key_decisions=None,
            discovered_facts=(),
            open_questions=(),
            blockers=None,
        )

    def _baseline_findings(
        self, report: preflight_models.PreflightReport
    ) -> list[preflight_models.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category.startswith("baseline-")
        ]

    def _record_baseline(
        self,
        plan_dir: Path,
        repo_root: Path,
        result: dict,
        *,
        session: str = "1",
    ) -> None:
        contract = contracts.read_completion_contract(plan_dir, session)
        assert contract is not None
        check = contract.verify.checks[0]
        key = baseline.baseline_identity_key(check, repo_root)
        path = plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
        cache = baseline_cache.BaselineCache.load(path)
        cache.put(
            key,
            baseline_cache.BaselineRecord(
                key=key,
                recipe=baseline.normalize_recipe(baseline._single_check_recipe(check)),
                result=result,
            ),
        )
        cache.save()

    def _fresh_result(self, repo_root: Path, **overrides: object) -> dict:
        result = {
            "check_name": "cmd",
            "kind": "command",
            "expectation": "pass",
            "status": "pass",
            "satisfied": True,
            "duration_seconds": 0.1,
            "budget_seconds": 120.0,
            "near_timeout": False,
            "detail": "command exited 0",
            "repository_fingerprint": baseline.repository_fingerprint(repo_root),
            "recorded_at": "2026-06-19T00:00:00Z",
        }
        result.update(overrides)
        return result


class RepositoryIntentFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "README.md").write_text("x\n", encoding="utf-8")
        self.plan_dir = self.root / "plan"

    def _write_plan(
        self, *paths: Path, statuses: tuple[str, ...] = ("pending",)
    ) -> None:
        contract = _contract_block(
            ", ".join(
                json.dumps(
                    {
                        "type": "file_exists",
                        "path": str(path),
                        "name": f"check-{index}",
                    }
                )
                for index, path in enumerate(paths)
            ),
            session="1",
        )
        _write_loop_ready_plan(self.plan_dir, statuses, contracts_by_id={"1": contract})

    def _set_handoff(
        self, session_id: str, *, status: str, files_modified: str
    ) -> None:
        path = self.plan_dir / f"session-{session_id}.md"
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("- **Status**: "):
                line = f"- **Status**: {status}"
            elif line.startswith("- **Files modified**: "):
                line = f"- **Files modified**: {files_modified}"
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _intent_findings(
        self, report: preflight_models.PreflightReport
    ) -> list[preflight_models.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "repository-registry-required"
        ]

    def _declare_source(
        self, value: str, *, cited_in_session: str | None = None
    ) -> None:
        """Record a `**Split from**` pointer, optionally citing it in prose."""
        manifest = self.plan_dir / "manifest.md"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + f"\n**Split from**: `{value}`\n",
            encoding="utf-8",
        )
        if cited_in_session is not None:
            session = self.plan_dir / "session-1.md"
            session.write_text(
                session.read_text(encoding="utf-8")
                + f"\nRestated from {cited_in_session} for context.\n",
                encoding="utf-8",
            )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))

    def _footprint_findings(
        self, report: preflight_models.PreflightReport
    ) -> list[preflight_models.PreflightFinding]:
        return [
            finding
            for finding in report.findings
            if finding.category == "repository-footprint"
        ]

    def _registry_required_report(self) -> preflight_models.PreflightReport:
        self._write_plan(
            self.first / "README.md", self.second / "src" / "new_module.py"
        )
        plans.save_plan_metadata(self.plan_dir, str(self.first))
        return preflight.run_core_preflight(self.plan_dir)


def _finding(
    severity: str,
    category: str,
    message: str,
    *,
    detail: str = "",
    path: str | None = None,
) -> preflight_models.PreflightFinding:
    return preflight_models.PreflightFinding(
        severity=severity,
        category=category,
        message=message,
        detail=detail,
        path=path,
    )


def _report(
    *findings: preflight_models.PreflightFinding,
) -> preflight_models.PreflightReport:
    return preflight_models.PreflightReport(
        plan_dir=Path("/plan"),
        findings=tuple(findings),
    )


def _backtick_commands(text: str) -> list[str]:
    """Return every substring wrapped in single backticks in ``text``."""
    return re.findall(r"`([^`]+)`", text)


def _extract_backtick_command(text: str, prefix: str) -> str:
    """Return the first backtick command in ``text`` starting with ``prefix``."""
    for command in _backtick_commands(text):
        if command.startswith(prefix):
            return command
    raise AssertionError(f"no `{prefix}...` command found in: {text!r}")


class PreflightCliFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.xdg_config = self.tmp_path / "xdg-config"
        self.xdg_state = self.tmp_path / "xdg-state"
        self.path_dir = self.tmp_path / "path"
        self.xdg_config.mkdir()
        self.xdg_state.mkdir()
        self.path_dir.mkdir()
        self.plan_dir = self.tmp_path / "plan"
        self.repo_root = self.tmp_path / "repo"
        init_git_repo(self.repo_root)
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                    session="1",
                ),
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _path_value(self, *names: str) -> str:
        for name in names:
            target = self.path_dir / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        return os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])

    def _run_preflight(
        self,
        *,
        json_output: bool = True,
        cwd: Path | None = None,
        harness: str | None = None,
        commit_mode: str | None = None,
        mark_loop_ready: bool = False,
        max_pending_sessions: int | None = None,
        path_value: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "preflight",
            str(self.plan_dir),
            "--state-dir",
            str(self.xdg_state),
        ]
        if cwd is not None:
            argv.extend(["--cwd", str(cwd)])
        if harness is not None:
            argv.extend(["--harness", harness])
        if commit_mode == "per-session":
            argv.append("--commit-per-session")
        elif commit_mode == "end":
            argv.append("--commit-at-end")
        if max_pending_sessions is not None:
            argv.extend(["--max-pending-sessions", str(max_pending_sessions)])
        if json_output:
            argv.append("--json")
        if mark_loop_ready:
            argv.append("--mark-loop-ready")
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(self.xdg_config)
        env["XDG_STATE_HOME"] = str(self.xdg_state)
        if path_value is not None:
            env["PATH"] = path_value
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
