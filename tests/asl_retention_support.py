"""Shared fixtures for the run-retention test modules."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_session_loop import (
    plans_files,
    retention_projection,
    state,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
DEAD_PID = 4_242_424

RUN_OLD = "20260601T000000000000Z-11"
RUN_NEW = "20260803T000000000000Z-22"
RUN_ABSENT = "20260602T000000000000Z-33"
AUDIT_STAMP = "2026-06-01T00:00:00+00:00"


def _stamp(days_ago: float, *, seconds: float = 0.0) -> str:
    return (NOW - timedelta(days=days_ago, seconds=seconds)).isoformat()


class RetentionTestBase(unittest.TestCase):
    """Builds a throwaway state root holding one plan's run directories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.plan_dir = self._make_plan("plan")
        self.plan_state = state.plan_state_dir(self.plan_dir, self.state_root)

    def _make_plan(self, name: str, **state_fields: object) -> Path:
        plan_dir = self.tmp_path / name
        plan_dir.mkdir()
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True)
        payload = {
            "plan_dir": str(plans_files.canonicalize_path(plan_dir)),
            **state_fields,
        }
        (plan_state / "state.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        return plan_dir

    def _write_lock(self, *, plan_dir: Path | None = None, **fields: object) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        lock_path = state.plan_state_dir(target, self.state_root) / state.LOCK_FILE_NAME
        payload = {
            "plan_dir": str(plans_files.canonicalize_path(target)),
            "created_at": "2026-08-01T00:00:00+00:00",
            **fields,
        }
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return lock_path

    def _write_run(
        self,
        run_id: str,
        *,
        finished_at: str | None,
        plan_dir: Path | None = None,
        log_bytes: int = 0,
        write_result: bool = True,
        result_text: str | None = None,
    ) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        run_dir = (
            state.plan_state_dir(target, self.state_root) / state.RUNS_DIR_NAME / run_id
        )
        turn_dir = run_dir / "turn-0001-session-1"
        turn_dir.mkdir(parents=True)
        (turn_dir / "stderr.log").write_bytes(b"x" * log_bytes)
        if result_text is not None:
            (turn_dir / "result.json").write_text(result_text, encoding="utf-8")
        elif write_result:
            payload: dict[str, object] = {"status_result": "ok"}
            if finished_at is not None:
                payload["finished_at"] = finished_at
            (turn_dir / "result.json").write_text(
                json.dumps(payload, sort_keys=True), encoding="utf-8"
            )
        return run_dir

    def _report(self, **kwargs: object) -> retention_projection.PlanRetention:
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("live_runs", frozenset())
        return retention_projection.plan_retention(
            self.plan_dir, state_dir=self.state_root, **kwargs
        )

    def _runs_by_id(
        self, report: retention_projection.PlanRetention
    ) -> dict[str, retention_projection.RunCandidate]:
        return {run.run_id: run for run in report.runs}
