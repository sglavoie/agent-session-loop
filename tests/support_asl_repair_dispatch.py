"""Shared fixture for generated repair-child dispatch tests."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    events,
    harnesses,
    plans,
    review,
    review_repair,
    run_loop,
    state,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    fake_commit_harness_env,
    init_multi_repo_workspace,
    patched_env,
    read_jsonl,
)


class GeneratedRepairDispatchFixture(unittest.TestCase):
    """A completed multi-repository review whose repair children can be run.

    Both dispatch gates only exist inside a real ``run_locked`` turn, so these
    tests build the whole artifact chain -- registry, final review artifact,
    materialized children -- and drive the runner rather than the helpers.
    """

    SPECS = DEFAULT_MULTI_REPO_SPECS[:2]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp_path = Path(tmp.name)
        self.workspace_root = self.tmp_path / "workspace"
        self.repositories = init_multi_repo_workspace(self.workspace_root, self.SPECS)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.child = self.plan_dir / "session-2.1.md"
        self.prompts: list[str] = []
        self.commit_env = fake_commit_harness_env(self.tmp_path)

    def _finding(
        self,
        evidence_aliases: tuple[str, ...],
        *,
        finding_id: str = "F1",
        repair_group: str = "manifest-contract",
        repo: str = "OAK",
    ) -> dict[str, Any]:
        return _structured_review_finding(
            finding_id=finding_id,
            repair_group=repair_group,
            repo=repo,
            evidence=[
                {"path": f"{alias}:pkg/{finding_id}.py", "symbol": "target"}
                for alias in evidence_aliases
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "repo": repo,
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )

    def _write_review_artifact(self, findings: list[dict[str, Any]]) -> None:
        counts = {
            severity: sum(1 for item in findings if item["severity"] == severity)
            for severity in ("high", "med", "low")
        }
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "# Final review\n\n"
            f"Status: {len(findings)} Findings ({counts['high']} high, "
            f"{counts['med']} med, {counts['low']} low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
                    "outcome": "findings",
                    "findings": findings,
                }
            ),
            encoding="utf-8",
        )

    def _pin_workspace(self) -> None:
        registry = plans.read_repository_registry(self.plan_dir)
        self.assertIsNotNone(registry, "the plan must carry a repository registry")
        assert registry is not None
        plans.save_workspace_plan_metadata(
            self.plan_dir, self.workspace_root, registry.digest
        )

    def _materialize_children(self) -> None:
        result = review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir
        )
        self.assertTrue(result.ok, result.error)

    def _write_scope(self, path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("**Writes repositories**:"):
                return line.split(":", 1)[1].strip()
        self.fail(f"{path.name} declares no write scope")

    def _narrow(self, path: Path, aliases: str) -> None:
        text = path.read_text(encoding="utf-8")
        current = self._write_scope(path)
        path.write_text(
            text.replace(
                f"**Writes repositories**: {current}",
                f"**Writes repositories**: {aliases}",
                1,
            ),
            encoding="utf-8",
        )

    def _prompt_field(self, prompt: str, label: str) -> str:
        for line in prompt.splitlines():
            if line.startswith(f"- {label}:"):
                return line.split(":", 1)[1].strip()
        self.fail(f"the task packet carries no {label!r} line")

    def _record_prompt(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        self.prompts.append(str(kwargs.get("prompt", "")))
        return harnesses.HarnessResult(
            name=name, argv=["fake"], returncode=0, stdout="ok\n", stderr=""
        )

    def _refuse(self, name: str, **kwargs: Any) -> harnesses.HarnessResult:
        raise AssertionError(f"the harness must not be reached; got {name}")

    def _event_path(self) -> Path:
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        return plan_state / events.EVENTS_FILE_NAME

    def _pause_reasons(self) -> list[str]:
        event_path = self._event_path()
        if not event_path.exists():
            return []
        return [
            record["payload"]["reason"]
            for record in read_jsonl(event_path)
            if record["name"] == events.RUN_PAUSED_EVENT
        ]

    def _run(self, run_id: str, harness: Any) -> tuple[int, dict[str, Any]]:
        args = argparse.Namespace(
            plan_dir=str(self.plan_dir),
            cwd=str(self.workspace_root),
            harness="codex",
            state_dir=str(self.state_dir),
            json=True,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=None,
            model=None,
            effort=None,
            note=None,
            allow_not_loop_ready=True,
        )
        context = verification.VerificationContext.from_plan(
            self.plan_dir, self.workspace_root
        )
        lock_info = state.acquire_lock(self.plan_dir, self.state_dir)
        try:
            with (
                patched_env(**self.commit_env),
                mock.patch.object(harnesses, "run_harness", side_effect=harness),
            ):
                return run_loop.run_locked(
                    args=args,
                    cwd=self.workspace_root,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=None,
                    lock_info=lock_info,
                    run_id=run_id,
                    verification_context=context,
                )
        finally:
            state.release_lock(self.plan_dir, self.state_dir)
