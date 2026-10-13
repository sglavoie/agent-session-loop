"""Shared fixtures for the ParserAndStateTests modules

Split from tests/test_asl_core.py."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    plans,
    preflight_models,
    readiness_gate,
    run_commit,
    state,
    turn_execution,
)


class ParserAndStateTestsBase(unittest.TestCase):
    def _session_file(self, plan_dir: Path, session_id: str = "1") -> plans.SessionFile:
        return plans.SessionFile(
            session_id=plans.parse_session_id(session_id),
            path=plan_dir / f"session-{session_id}.md",
        )

    def _parallel_contract_text(self, parallel_literal: str | None) -> str:
        parallel_line = (
            [] if parallel_literal is None else [f'  "parallel": {parallel_literal},']
        )
        return "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                *parallel_line,
                '  "checks": [{"type": "file_exists", "path": "a"}]',
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

    def _active_lock(self) -> state.LockInfo:
        return state.LockInfo(
            path=Path("/tmp/plan-lock.json"),
            pid=os.getpid(),
            plan_dir="/tmp/plan",
            created_at="2026-07-24T12:00:00+00:00",
            stale=False,
            reason="active",
            raw={},
        )

    @staticmethod
    def _snapshot_dict(pending: int) -> dict[str, object]:
        return {
            "plan_dir": "/plan",
            "plan_hash": "abc",
            "content_sha256": "def",
            "path_hash": "hash",
            "counts": {"pending": pending, "complete": 1, "empty": 0, "unknown": 0},
            "next_pending": 2,
            "next_pending_id": "2",
            "sessions": [
                {
                    "id": str(index),
                    "status": "pending",
                    "raw_value": "pending",
                    "line_number": index,
                    "path": f"/plan/session-{index}.md",
                    "number": index,
                }
                for index in range(1, pending + 2)
            ],
        }

    def _verification_check(self) -> dict[str, object]:
        return {
            "name": "tests",
            "kind": "command",
            "status": "fail",
            "failure_reason": "command-failed",
            "returncode": 1,
            "duration_seconds": 1.5,
            "stdout": "x" * 8192,
            "stderr": "y" * 8192,
            "stdout_byte_count": 8192,
            "stderr_byte_count": 8192,
            "stdout_sha256": "s" * 64,
            "stderr_sha256": "t" * 64,
            "stdout_artifact_path": "/artifacts/tests-stdout.gz",
            "stderr_artifact_path": "/artifacts/tests-stderr.gz",
        }

    def _detailed_turn(self) -> dict[str, object]:
        check = self._verification_check()
        return {
            "version": turn_execution.RESULT_SCHEMA_VERSION,
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "session_id": "1",
            "harness": "codex",
            "model": "gpt-5",
            "model_source": "flag",
            "argv": ["codex", "run"],
            "returncode": 0,
            "status_result": "complete",
            "stdout_path": "/artifacts/stdout.log",
            "stderr_path": "/artifacts/stderr.log",
            "result_path": "/artifacts/result.json",
            "artifact_dir": "/artifacts",
            "total_duration_seconds": 1.2,
            "before": self._snapshot_dict(2),
            "after": self._snapshot_dict(1),
            "verification": {
                "session": "1",
                "reason": "verify-failed",
                "passed": False,
                "recipe": {"check_count": 1, "kinds": ["command"], "names": ["tests"]},
                "checks": [check],
            },
        }

    def _base_state_payload(self, state_path: Path) -> dict[str, object]:
        return {
            "version_absent_is_v1": True,
            "state_path": str(state_path),
            "plan_dir": str(state_path.parent),
            "run_id": "run-1",
            "harness": "codex",
            "ok": True,
            "paused": False,
            "reason": "",
            "run_started_at": "2026-06-20T10:00:00+00:00",
            "run_finished_at": None,
            "duration_seconds": None,
            "active_turn": {"session": 1, "turn_index": 1, "harness": "codex"},
            "active_verification": None,
            "control": {"next_harness": "codex", "last_action": "harness-selected"},
            "final_snapshot": self._snapshot_dict(0),
            "completed_sessions": [1],
            "completed_session_ids": ["1"],
            "turns": [self._detailed_turn()],
        }

    def _turn(
        self,
        *,
        harness: str = "claude",
        session_kind: str = "implementation",
        model: str = "native CLI default",
        model_source: str = "unresolved",
        observed_model: str | None = "claude-opus-5",
    ) -> dict[str, object]:
        return {
            "harness": harness,
            "session_kind": session_kind,
            "model": model,
            "model_source": model_source,
            "observed_model": observed_model,
        }

    def _fold(self, *turns: dict[str, object]) -> list[dict[str, Any]]:
        projection: list[dict[str, Any]] = []
        for turn in turns:
            projection = run_commit._merged_model_projection(projection, turn)
        return projection

    def _live_action_required(self) -> dict[str, object]:
        return {
            "run_id": "run-1",
            "turn_index": 1,
            "session": 1,
            "reason": "runner-pause",
            "message": "RUNNER_PAUSE: blocked - needs a decision",
            "source": "stdout",
            "matched": events.RUNNER_PAUSE_PREFIX,
        }

    def _baseline_missing_gate(self, plan_dir: Path, cwd: Path) -> dict[str, Any]:
        report = preflight_models.PreflightReport(
            plan_dir=plan_dir,
            findings=(
                preflight_models.PreflightFinding(
                    severity="red",
                    category="baseline-missing",
                    message="session 1 declares a baseline expectation",
                    session_id="1",
                    detail="no baseline evidence recorded",
                ),
            ),
        )
        return readiness_gate._readiness_gate_payload(report, cwd=cwd)
