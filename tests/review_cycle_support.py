"""Shared fixtures for the review repair cycle test modules."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    config,
    events,
    plans_content_hash,
    plans_files,
    plans_models,
    plans_readiness,
    plans_session_ids,
    plans_snapshot,
    review,
    review_cycle,
    review_repair,
    review_session_body,
    run_persistence,
    session_verification,
)
from tests.support import (
    REPO_ROOT,
    _contract_block,
    _structured_review_finding,
    init_git_repo,
    install_fake_codex,
    install_fake_harness,
    read_jsonl,
    write_plan,
    write_plan_with_ids,
)


class RepairCycleFixtures:
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

    def _run_args(self, **overrides: object) -> argparse.Namespace:
        defaults: dict[str, Any] = {
            "cwd": str(self.repo_root),
            "timeout": None,
            "max_sessions": None,
            "max_pending_sessions": None,
            "commit_mode": None,
            "harness": "codex",
            "review_harness": None,
            "max_review_fix_cycles": None,
            "max_review_fix_plans": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def _advance_blocked_review_cycle(
        self, *, allow_not_loop_ready: bool
    ) -> tuple[str, Any, dict[str, Any], Path]:
        """Run one review cycle whose loop-ready gate refuses the children."""
        finding = _structured_review_finding()
        write_plan_with_ids(self.plan_dir, (("1", "complete"),))
        review_body = review_session_body.build_generated_review_session_body(
            plans_session_ids.parse_session_id("2"),
            review_harness="claude",
            plan_dir=self.plan_dir,
        ).replace("- **Status**: pending", "- **Status**: complete")
        (self.plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
        (self.plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            "## Findings\n"
            "- High issue in agent_session_loop/runner.py.\n"
            "- Context in tests/test_asl_review.py.\n"
            "- Context in docs/agent-session-loop.md.\n"
            "Status: 1 Findings (1 high, 0 med, 0 low)\n",
            encoding="utf-8",
        )
        (self.plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "findings", "findings": [finding]}),
            encoding="utf-8",
        )
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.state_dir / "events.jsonl"
        payload: dict[str, Any] = {
            "run_id": "run-1",
            "run_dir": str(self.tmp_path / "run"),
            "review_cycles": [],
        }
        snapshot = plans_snapshot.inspect_plan(self.plan_dir)

        with mock.patch.object(
            review_repair,
            "_make_children_executable",
            return_value={
                "loop_ready": False,
                "blockers": ["worktree-dirty: uncommitted changes"],
                "non_bypassable_blockers": [],
                "baseline_summary": None,
            },
        ):
            action, after = review_cycle._advance_review_cycle(
                event_path=event_path,
                payload=payload,
                persistence=run_persistence._RunPersistence(),
                args=argparse.Namespace(
                    state_dir=str(self.state_dir),
                    allow_not_loop_ready=allow_not_loop_ready,
                ),
                cwd=self.repo_root,
                session_id="2",
                session_label="session 2",
                session_details={"session": "2"},
                snapshot=snapshot,
                turn_payload={
                    "review_verdict": {
                        "passed": True,
                        "outcome": "findings",
                        "total_findings": 1,
                        "findings": [finding],
                    }
                },
                max_review_fix_cycles=2,
                plan_cache=plans_snapshot.PlanSnapshotCache(),
                reinspect=session_verification._reinspect_after_plan_mutation,
            )
        return action, after, payload, event_path

    def _write_plan(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", "name": "readme-1"}',
                session="1",
            )
        }
        write_plan(self.plan_dir, ("pending",), contracts_by_id=contracts_by_id)
        sha = plans_content_hash.stable_readiness_sha256(self.plan_dir)
        plans_readiness.write_readiness_metadata(
            self.plan_dir,
            plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run_cycles(
        self,
        *,
        review_outcomes: str,
        extra_args: list[str] | None = None,
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
            "--review-harness",
            "claude",
            "--state-dir",
            str(self.state_dir),
            "--json",
            *(extra_args or []),
        ]
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success-with-review",
                "FAKE_CODEX_REVIEW_OUTCOMES": review_outcomes,
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def _events_path(self) -> Path:
        plan_state = self.state_dir / plans_files.path_sha256(
            plans_files.canonicalize_path(self.plan_dir)
        )
        return plan_state / events.EVENTS_FILE_NAME

    def _review_cycle_events(self) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == events.REVIEW_CYCLE_EVENT
        ]

    def _review_reopen_events(self) -> list[dict[str, Any]]:
        return [
            record["payload"]
            for record in read_jsonl(self._events_path())
            if record["name"] == events.REVIEW_REPAIR_REOPENED_EVENT
        ]

    def _write_completed_review_history(
        self,
        plan_dir: Path,
        *,
        review_ids: tuple[str, ...],
        repair_ids: tuple[str, ...] = (),
    ) -> None:
        session_ids = ("1", *review_ids, *repair_ids)
        write_plan_with_ids(
            plan_dir,
            tuple(
                (session_id, "complete")
                for session_id in sorted(
                    session_ids,
                    key=lambda item: plans_session_ids.parse_session_id(item).sort_key,
                )
            ),
        )
        for review_id in review_ids:
            review_body = review_session_body.build_generated_review_session_body(
                plans_session_ids.parse_session_id(review_id),
                review_harness="claude",
                plan_dir=plan_dir,
            ).replace("- **Status**: pending", "- **Status**: complete")
            (plan_dir / f"session-{review_id}.md").write_text(
                review_body, encoding="utf-8"
            )
        for repair_id in repair_ids:
            repair_path = plan_dir / f"session-{repair_id}.md"
            repair_path.write_text(
                review_repair.REVIEW_REPAIR_SESSION_MARKER
                + "\n"
                + review_repair.REVIEW_REPAIR_SESSION_VERSION_MARKER_TEMPLATE.format(
                    version=review_repair.GENERATED_REVIEW_REPAIR_SESSION_VERSION
                )
                + "\n"
                + repair_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
            f"# Final review\n\n{review.REVIEW_STATUS_ALL_CLEAR}\n",
            encoding="utf-8",
        )
        (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
            json.dumps({"version": 2, "outcome": "all-clear", "findings": []}),
            encoding="utf-8",
        )

    def _resplit_repair_child(
        self,
        plan_dir: Path,
        repair_id: str,
        child_statuses: tuple[tuple[str, str], ...],
    ) -> None:
        """Archive a repair child out of the plan and nest children under it.

        What ``session-resplit`` leaves behind: the parent is gone from the
        plan root and the children are authored fresh, so they carry no repair
        marker of their own.
        """
        (plan_dir / f"session-{repair_id}.md").unlink()
        for child_id, status in child_statuses:
            body = (plan_dir / "session-1.md").read_text(encoding="utf-8")
            (plan_dir / f"session-{child_id}.md").write_text(
                body.replace("# Session 1:", f"# Session {child_id}:").replace(
                    "- **Status**: complete", f"- **Status**: {status}"
                ),
                encoding="utf-8",
            )

    def _append_repair_lineage(self, repair_ids: tuple[str, ...]) -> Path:
        """Record the reopen that names the repair children history requires."""
        event_path = self._events_path()
        events.append_event(
            event_path,
            events.Event(
                events.REVIEW_REPAIR_REOPENED_EVENT,
                {
                    "source_cycle": None,
                    "source_review_session_id": "2",
                    "source_disposition": None,
                    "repair_session_ids": list(repair_ids),
                    "materialized": True,
                    "result": "ok",
                    "after_max_review_fix_cycles": 1,
                },
            ),
        )
        return event_path
