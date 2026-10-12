"""Shared fixtures for the review repair window and contract gate tests."""

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    authoring,
    plans,
    progress_repair,
    review,
    review_repair,
    review_session_body,
)
from tests.support import (
    _contract_block,
    _structured_review_finding,
    init_git_repo,
)


class _SimulatedCrash(BaseException):
    """Stands in for a process death: nothing in-process gets to clean up."""


def _committed_repo(path: Path) -> Path:
    """Initialize a Git repository holding one committed file."""
    init_git_repo(path)
    (path / "README.md").write_text("readme\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=x",
            "commit",
            "-q",
            "-m",
            "readme",
        ],
        check=True,
    )
    return path


def _write_repair_ready_plan(
    plan_dir: Path,
    repo: Path,
    *,
    findings: list[dict[str, Any]] | None = None,
    specs: tuple[tuple[str, str, str], ...] | None = None,
) -> None:
    """Write a completed two-session plan whose review carries repair findings.

    The plan is loop-ready material: session 1 is complete with a contract that
    passes against ``repo``, and session 2 is a completed generated review whose
    artifact holds ``findings``. Passing ``specs`` registers those repositories,
    which is what makes the plan take the workspace-resolved verification path.
    """
    registered = specs is not None
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")

    manifest = authoring.render_manifest(
        authoring.ManifestSpec(
            plan_title="Test Plan",
            source_path="/src/plan.md",
            output_dir=str(plan_dir),
            plan_id="pid",
            source_fingerprint="fp",
            content_sha256="csha",
            sessions=(
                authoring.ManifestSession(
                    plans.parse_session_id("1"), "One", "complete"
                ),
                authoring.ManifestSession(
                    plans.parse_session_id("2"), "Review", "complete"
                ),
            ),
            split_date="2026-01-01",
            readiness=plans.ReadinessMetadata("draft", "sha", "2026-01-01T00:00:00Z"),
        )
    )
    if registered:
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
        )
        manifest += (
            "\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n"
        )
    (plan_dir / "manifest.md").write_text(manifest, encoding="utf-8")

    aliases = (
        ", ".join(alias for alias, _path, _origin in specs) if registered else None
    )
    check_payload = (
        '{"type": "file_exists", "path": "README.md", "name": "readme-1"'
        + (f', "repo": "{specs[0][0]}"' if registered else "")
        + "}"
    )
    session_one = ["# Session 1: One", ""]
    if registered:
        session_one.extend(
            [
                f"**Reads repositories**: {aliases}",
                f"**Writes repositories**: {aliases}",
                "",
            ]
        )
    session_one.extend(
        [
            "## Instructions",
            "",
            "Do the first thing.",
            "",
            _contract_block(check_payload, session="1"),
            "",
            "## Handoff",
            "",
            "- **Status**: complete",
            "- **Files modified**: (none)",
            "- **Key decisions**: (none)",
            "- **Blockers for next session**: (none)",
            "- **Notes**:",
            "",
        ]
    )
    (plan_dir / "session-1.md").write_text("\n".join(session_one), encoding="utf-8")

    review_body = review_session_body.build_generated_review_session_body(
        plans.parse_session_id("2"),
        review_harness="codex",
        plan_dir=plan_dir,
    ).replace("- **Status**: pending", "- **Status**: complete")
    if registered:
        heading, remainder = review_body.split("\n", 1)
        review_body = (
            f"{heading}\n\n**Reads repositories**: {aliases}\n"
            f"**Writes repositories**: (none)\n{remainder}"
        ).replace("- **Files modified**: (none yet)", "- **Files modified**: (none)")
    (plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")

    if findings is None:
        findings = [
            _structured_review_finding(
                finding_id="F1",
                repair_group="docs",
                evidence=[{"path": "README.md"}],
                done_criteria=["README stays present."],
                verification_checks=[
                    {
                        "type": "file_exists",
                        "name": "readme-present",
                        "path": str(repo / "README.md"),
                    }
                ],
            )
        ]
    (plan_dir / "final-review.md").write_text(
        "# Review\n\n## Findings\n\nSee README.md.\n\n"
        f"Status: {len(findings)} Findings ({len(findings)} high, 0 med, 0 low)\n",
        encoding="utf-8",
    )
    (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
        json.dumps(
            {
                "version": (
                    review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION if registered else 2
                ),
                "outcome": "findings",
                "findings": findings,
            }
        ),
        encoding="utf-8",
    )


class PublicationWindowFixtures:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo = _committed_repo(self.tmp_path / "repo")
        _write_repair_ready_plan(self.plan_dir, self.repo)
        progress_repair.repair_progress_journal(self.plan_dir)

    def _materialize(self, **kwargs: Any) -> review_repair.ReviewRepairResult:
        return review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, cwd=self.repo, **kwargs
        )

    def _plan_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in self.plan_dir.rglob("*")
            if path.is_file()
        }

    def _window_dir(self) -> Path:
        return self.plan_dir / review_repair.REPAIR_PUBLICATION_DIR_NAME

    def _blocked_gate(self, *, non_bypassable: list[str]) -> dict[str, Any]:
        return {
            "loop_ready": False,
            "blockers": ["repository-footprint: injected refusal"],
            "non_bypassable_blockers": non_bypassable,
            "baseline_summary": {"ok": False},
        }

    def _crash_mid_publication(self, phase: str) -> dict[str, bytes]:
        """Die mid-publication: no in-process rollback, window left on disk."""
        before = self._plan_bytes()
        target = {
            "authoring": (review_repair.authoring, "refresh_readiness_metadata"),
            "validating": (review_repair, "_make_children_executable"),
        }[phase]
        with (
            mock.patch.object(
                review_repair, "_rollback_publication_window", return_value={}
            ),
            mock.patch.object(*target, side_effect=_SimulatedCrash()),
            self.assertRaises(_SimulatedCrash),
        ):
            self._materialize()
        self.assertTrue(self._window_dir().is_dir())
        return before


class ContractGateFixtures:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.repo = _committed_repo(self.tmp_path / "repo")
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"

    def _write_plan_with_check(self, check: dict[str, Any]) -> None:
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            evidence=[{"path": "README.md"}],
            done_criteria=["README stays present."],
            verification_checks=[check],
        )
        _write_repair_ready_plan(self.plan_dir, self.repo, findings=[finding])
        progress_repair.repair_progress_journal(self.plan_dir)

    def _plan_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in self.plan_dir.rglob("*")
            if path.is_file()
        }

    def _materialize(self) -> review_repair.ReviewRepairResult:
        return review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, cwd=self.repo
        )
