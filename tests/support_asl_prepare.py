"""Shared fixtures for the prepare test modules."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    authoring,
    harnesses,
    prepare,
    prepare_models,
    prepare_results,
)
from tests.support import (
    _write_prepare_source_plan,
    init_git_repo,
    write_multi_repo_plan,
)


class PrepareValidatorFixtures(unittest.TestCase):
    """Versioned validator result schema (`agent_session_loop.prepare`)."""

    @staticmethod
    def _write_registered_plan(
        plan_dir: Path,
        specs: tuple[tuple[str, str, str], ...],
    ) -> None:
        write_multi_repo_plan(plan_dir, ("pending", "pending"), specs=specs)
        for session_path in sorted(plan_dir.glob("session-*.md")):
            text = session_path.read_text(encoding="utf-8")
            heading, remainder = text.split("\n", 1)
            session_path.write_text(
                f"{heading}\n{authoring.CONTEXT_REFERENCE_LINE}\n{remainder}",
                encoding="utf-8",
            )

    @staticmethod
    def _registered_envelope(session: dict) -> dict:
        return {
            "version": prepare_models.PREPARE_VALIDATION_VERSION,
            "workspace_root": "/workspace",
            "repositories": [
                {
                    "alias": "SVC",
                    "path": "svc",
                    "origin": "github.com/org/svc",
                    "root": "/workspace/svc",
                }
            ],
            "sessions": [session],
        }

    @staticmethod
    def _removal_overrides(*, repo: str | None = None) -> dict:
        claim: dict[str, Any] = {
            "claim": "retry backoff branch",
            "claim_type": "R",
            "location": "foo/bar.py — added in a1b2c3d, covered by test_retry.py:40",
            "line": 12,
            "to_be_created": False,
        }
        issue: dict[str, Any] = {
            "issue_type": "removal-unjustified",
            "summary": "no rationale for dropping the backoff",
            "session_ref": "delete the backoff branch",
            "actual": "no commit or covering test explains the branch",
            "fix": "record the origin rationale before removing it",
        }
        if repo is not None:
            claim["repo"] = repo
            issue["repo"] = repo
        return {"claims": [claim], "issues": [issue]}

    def _scripted_validator(
        self, *messages: str
    ) -> tuple[tuple[Any, mock.Mock], list[str]]:
        prompts: list[str] = []
        replies = iter(messages)

        def run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            prompts.append(str(kwargs["prompt"]))
            return harnesses.HarnessResult(
                argv=[name],
                returncode=0,
                stdout="",
                stderr="",
                last_message=next(replies),
                name=name,
            )

        packet = mock.Mock(codebase_root="/repo", plan_dir="/plan")
        packet.render.return_value = "PACKET"
        launch = mock.patch.object(harnesses, "run_harness", side_effect=run_harness)
        return (launch, packet), prompts


class PrepareEndToEndFixtures(unittest.TestCase):
    """`prepare_plan` end-to-end pipeline behavior."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.base_dir = self.tmp_path / "plans"
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.repo = self.tmp_path / "repo"
        init_git_repo(self.repo)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_source(self, **kwargs: object) -> Path:
        return _write_prepare_source_plan(self.tmp_path / "demo-plan.md", **kwargs)

    def _prepare(
        self,
        source: Path | None,
        validator,
        **kwargs: object,
    ) -> prepare_results.PrepareResult:
        options: dict[str, Any] = {
            "cwd": self.repo,
            "harness": "codex",
            "harness_model": "gpt-5",
            "validator_harness": "claude",
            "review_harness": "claude",
            "review_harness_model": "claude-opus-4",
            "base_dir": self.base_dir,
            "validator": validator,
            "state_dir": self.state_dir,
        }
        options.update(kwargs)
        return prepare.prepare_plan(source, **options)

    def _write_registered_source(
        self,
        specs: tuple[tuple[str, str, str], ...],
    ) -> Path:
        lines = [
            "# Registered Prepare Demo",
            "",
            "## Repositories",
            "",
            "| Alias | Path | Origin |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
        )
        aliases = ", ".join(alias for alias, _path, _origin in specs)
        for number, (alias, relative_path, _origin) in enumerate(specs, start=1):
            check = {
                "name": f"check-{number}",
                "type": "command",
                "repo": alias,
                "argv": [
                    "python3",
                    "-c",
                    (
                        "from pathlib import Path; "
                        f"raise SystemExit(Path.cwd().name != {relative_path!r})"
                    ),
                ],
                "timeout_seconds": 30,
                "baseline_expectation": "pass",
            }
            lines.extend(
                [
                    "",
                    f"## Session {number}: Step {number}",
                    "",
                    f"**Reads repositories**: {aliases}",
                    f"**Writes repositories**: {alias}",
                    "",
                    f"Implement `{alias}:demo/module.py`.",
                    "",
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "",
                    f"- Step {number} is complete.",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    json.dumps({"session": str(number), "checks": [check]}),
                    "```",
                ]
            )
        source = self.tmp_path / "registered-plan.md"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return source

    @staticmethod
    def _plan_file_bytes(plan_dir: Path) -> dict[str, bytes]:
        return {
            path.relative_to(plan_dir).as_posix(): path.read_bytes()
            for path in sorted(plan_dir.rglob("*"))
            if path.is_file()
        }
