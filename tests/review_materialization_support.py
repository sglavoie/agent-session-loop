"""Shared fixtures for the review repair materialization test modules."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    authoring_manifest,
    events,
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    plans_session_ids,
    plans_snapshot,
    review,
    review_continuation,
    review_repair,
    review_session_body,
    state,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _structured_review_finding,
    init_git_repo,
)


class MaterializationFixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"

    def _finding(
        self,
        finding_id: str,
        repair_group: str,
        *,
        severity: str = "high",
        **overrides: object,
    ) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "repair_group": repair_group,
            "title": f"Fix {finding_id}",
            "evidence": [{"path": f"pkg/{finding_id}.py", "symbol": "target"}],
            "explanation": f"{finding_id} is broken.",
            "done_criteria": [f"{finding_id} is fixed and tested."],
            "verification_checks": [
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        }
        defaults.update(overrides)
        return _structured_review_finding(
            finding_id=finding_id, severity=severity, **defaults
        )

    def _status_line(self, findings: list[dict[str, Any]]) -> str:
        if not findings:
            return review.REVIEW_STATUS_ALL_CLEAR
        high = sum(1 for f in findings if f["severity"] == "high")
        med = sum(1 for f in findings if f["severity"] == "med")
        low = sum(1 for f in findings if f["severity"] == "low")
        return f"Status: {len(findings)} Findings ({high} high, {med} med, {low} low)"

    def _write_plan(
        self,
        findings: list[dict[str, Any]],
        *,
        with_manifest_table: bool = True,
        review_status: str = "complete",
        session_one_contract: str | None = None,
        outcome: str = "findings",
    ) -> None:
        self.plan_dir.mkdir(parents=True, exist_ok=True)
        (self.plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")

        if with_manifest_table:
            sessions = (
                authoring_manifest.ManifestSession(
                    plans_session_ids.parse_session_id("1"), "One", "complete"
                ),
                authoring_manifest.ManifestSession(
                    plans_session_ids.parse_session_id("2"), "Review", "complete"
                ),
            )
            manifest = authoring_manifest.render_manifest(
                authoring_manifest.ManifestSpec(
                    plan_title="Test Plan",
                    source_path="/src/plan.md",
                    output_dir=str(self.plan_dir),
                    plan_id="pid",
                    source_fingerprint="fp",
                    content_sha256="csha",
                    sessions=sessions,
                    split_date="2026-01-01",
                    readiness=plans_models.ReadinessMetadata(
                        "draft", "sha", "2026-01-01T00:00:00Z"
                    ),
                )
            )
        else:
            manifest = "# Manifest\n\n**Sessions**: 2\n"
        (self.plan_dir / "manifest.md").write_text(manifest, encoding="utf-8")

        session_one = [
            "# Session 1: One",
            "",
            "## Instructions",
            "",
            "Do the first thing.",
            "",
        ]
        if session_one_contract:
            session_one.extend([session_one_contract, ""])
        session_one.extend(
            [
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
        (self.plan_dir / "session-1.md").write_text(
            "\n".join(session_one), encoding="utf-8"
        )

        review_body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("2"),
            review_harness="codex",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", f"- **Status**: {review_status}")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")

        (self.plan_dir / "final-review.md").write_text(
            "# Review\n\n## Findings\n\n"
            "See pkg/a.py and pkg/b.py and README.md.\n\n"
            f"{self._status_line(findings)}\n",
            encoding="utf-8",
        )
        (self.plan_dir / "final-review.json").write_text(
            json.dumps({"version": 2, "outcome": outcome, "findings": findings}),
            encoding="utf-8",
        )

    def _make_registered_finding(
        self,
        finding_id: str,
        repair_group: str,
        repo: str,
    ) -> dict[str, Any]:
        finding = self._finding(
            finding_id,
            repair_group,
            evidence=[{"path": f"{repo}:pkg/{finding_id}.py", "symbol": "target"}],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}",
                    "repo": repo,
                    "argv": ["python3", "-c", "pass"],
                }
            ],
        )
        finding["repo"] = repo
        return finding

    def _register_plan(
        self,
        findings: list[dict[str, Any]],
        *,
        artifact_version: int = review.REPOSITORY_AWARE_REVIEW_ARTIFACT_VERSION,
    ) -> None:
        self._write_plan(findings)
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |" for alias, path, origin in specs
        )
        (self.plan_dir / "manifest.md").write_text(
            "# Manifest\n\n## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n",
            encoding="utf-8",
        )
        for session_id, writes in (("1", "SVC, OAK"), ("2", "(none)")):
            path = self.plan_dir / f"session-{session_id}.md"
            heading, remainder = path.read_text(encoding="utf-8").split("\n", 1)
            body = (
                f"{heading}\n\n**Reads repositories**: SVC, OAK\n"
                f"**Writes repositories**: {writes}\n{remainder}"
            )
            if session_id == "2":
                body = body.replace(
                    "- **Files modified**: (none yet)",
                    "- **Files modified**: (none)",
                )
            path.write_text(body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps(
                {
                    "version": artifact_version,
                    "outcome": "findings",
                    "findings": findings,
                }
            ),
            encoding="utf-8",
        )

    def _materialize(self, **kwargs: object) -> review_repair.ReviewRepairResult:
        return review_repair.materialize_review_repair_sessions(
            self.plan_dir, state_dir=self.state_dir, **kwargs
        )

    def _plan_bytes(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.plan_dir)): path.read_bytes()
            for path in self.plan_dir.rglob("*")
            if path.is_file()
        }

    def _write_review_events(self, *payloads: dict[str, Any]) -> Path:
        event_path = (
            state.plan_state_dir(self.plan_dir, self.state_dir)
            / events.EVENTS_FILE_NAME
        )
        event_path.parent.mkdir(parents=True, exist_ok=True)
        event_path.write_text(
            "".join(
                json.dumps(
                    {
                        "name": events.REVIEW_CYCLE_EVENT,
                        "payload": payload,
                    }
                )
                + "\n"
                for payload in payloads
            ),
            encoding="utf-8",
        )
        return event_path

    def _plan_artifact_path(self, name: str) -> str:
        return str(plans_files.canonicalize_path(self.plan_dir) / name)

    def _cross_repo_finding(
        self,
        finding_id: str,
        repair_group: str,
        repo: str,
        *,
        evidence_repos: tuple[str, ...],
        check_repos: tuple[str, ...],
    ) -> dict[str, Any]:
        finding = self._finding(
            finding_id,
            repair_group,
            evidence=[
                {"path": f"{alias}:pkg/{finding_id}.py", "symbol": "target"}
                for alias in evidence_repos
            ],
            verification_checks=[
                {
                    "type": "command",
                    "name": f"verify-{finding_id}-{alias}",
                    "repo": alias,
                    "argv": ["python3", "-c", "pass"],
                }
                for alias in check_repos
            ],
        )
        finding["repo"] = repo
        return finding

    def _committed_git_repo(self, name: str) -> Path:
        repo = self.tmp_path / name
        init_git_repo(repo)
        (repo / "README.md").write_text("readme\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
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
        return repo

    def _pending_repairs_ready_to_run(self) -> Path:
        """A plan whose pending repair children the continuation calls eligible."""
        self._write_plan([self._finding("F1", "runner")])
        self.assertTrue(self._materialize().ok)
        event_path = self._write_review_events(
            {
                "cycle": 1,
                "disposition": "not-loop-ready",
                "repair_session_ids": ["2.1"],
                "max_review_fix_cycles": 1,
            }
        )
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=plans_content_hash.stable_readiness_sha256(
                    self.plan_dir
                ),
                checked_at="2026-08-17T00:00:00Z",
            ),
        )
        baseline = review_continuation.evaluate_review_continuation(
            plans_snapshot.inspect_plan(self.plan_dir), event_path
        )
        self.assertTrue(baseline.eligible, baseline.blocker)
        return event_path

    def _append_run(self, event_path: Path, run_id: str, *, paused: str | None) -> None:
        """Record a run that opened and, optionally, paused the way runs do."""
        events.append_event(
            event_path, events.Event(events.RUN_STARTED_EVENT, {"run_id": run_id})
        )
        if paused is not None:
            events.append_event(
                event_path,
                events.Event(
                    events.RUN_PAUSED_EVENT, {"run_id": run_id, "reason": paused}
                ),
            )

    def _withdrawn_cycle_events(self) -> Path:
        """The cycle a `not-loop-ready` gate withdrew every child from."""
        return self._write_review_events(
            {
                "cycle": 1,
                "review_session_id": "2",
                "disposition": "not-loop-ready",
                "fingerprints": [],
                "repair_session_ids": [],
                "repair_groups": [],
                "withdrawn_repair_session_ids": ["2.1"],
                "repair_children_rolled_back": True,
                "max_review_fix_cycles": 8,
            }
        )
